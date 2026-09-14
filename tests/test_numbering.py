"""Word numbering definitions/instances checked independently of rendered counter labels."""
from pathlib import Path
import xml.etree.ElementTree as ET
import pytest
from oxml import Document, e, w
from oxml.numbering import Numbering, Level
from corpus_helpers import parts

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
FIXTURE = Path(__file__).parent/'fixtures/body/lists_level_override.docx'

def test_multilevel_definition_continuation_and_new_instance_restart():
    doc = Document.new()
    doc.package.add_part('/word/numbering.xml', 'application/octet-stream', b'occupied')
    body = doc.main.xml.root.children[0]
    paragraphs = [body(e.p(e.pPr(e.keepNext(), e.spacing(after='120')), e.r(e.t(text)))) for text in ('Top', 'Sub', 'Restart')]
    numbering = doc.numbering
    instance = numbering.add([Level(), Level(format='lowerLetter', text='%1.%2)', restart=1)])
    instance.apply(paragraphs[0])
    instance.apply(paragraphs[1], level=1)
    restarted = instance.restart(start=4)
    restarted.apply(paragraphs[2])
    package = parts(doc)
    assert package['word/numbering.xml'] == b'occupied'
    tree = doc.part('NumberingDefinitionsPart').xml
    assert not tree.validate()['issues']
    root = ET.fromstring(tree.bytes())
    levels = root.find(W+'abstractNum').findall(W+'lvl')
    assert [e.find(W+'lvlText').get(W+'val') for e in levels] == ['%1.', '%1.%2)']
    assert [e.find(W+'pPr/'+W+'ind').get(W+'left') for e in levels] == ['720', '1440']
    assert levels[1].find(W+'lvlRestart').get(W+'val') == '1'
    first, second = root.findall(W+'num')
    assert first.find(W+'abstractNumId').attrib == second.find(W+'abstractNumId').attrib
    assert first.find(W+'lvlOverride') is None and second.find(W+'lvlOverride/'+W+'startOverride').get(W+'val') == '4'
    main = ET.fromstring(package['word/document.xml'])
    assert instance.id != restarted.id
    assert [e.get(W+'val') for e in main.iter(W+'numId')] == [str(instance.id), str(instance.id), str(restarted.id)]
    assert [e.get(W+'val') for e in main.iter(W+'ilvl')] == ['0', '1', '0']
    assert [e.tag for e in main.find('.//'+W+'pPr')] == [W+'keepNext', W+'numPr', W+'spacing']
    before = doc.bytes()
    with pytest.raises(ValueError): instance.apply(paragraphs[0], level=8)
    with pytest.raises(ValueError): instance.restart(start=-1)
    with pytest.raises(ValueError): numbering.add([Level(text='\x00')])
    assert doc.bytes() == before
    for parent, local, operation in [
        (paragraphs[0].children[0].children[1], 'numId', lambda: instance.apply(paragraphs[0], level=1)),
        (restarted.element.children[1], 'startOverride', restarted.restart),
    ]:
        duplicate = parent(e(local, val='9'), index=parent._tree.xml.child_count(parent.node_id))
        before = doc.bytes()
        with pytest.raises(ValueError): operation()
        assert doc.bytes() == before
        duplicate.delete()
    sublevel_restart = restarted.restart(start=7, level=1)
    overrides = ET.fromstring(sublevel_restart.element._tree.xml.subtree_bytes(sublevel_restart.element.node_id)).findall(W+'lvlOverride')
    assert {e.get(W+'ilvl'): e.find(W+'startOverride').get(W+'val') for e in overrides} == {'0': '4', '1': '7'}
    instance.element.set_attribute(W[1:-1], 'numId', 'invalid')
    before = doc.bytes()
    with pytest.raises(ValueError): instance.apply(paragraphs[0], level=1)
    assert doc.bytes() == before


# Pandoc's lists_level_override.docx supplies six list instances with independent start overrides.
# python-docx oxml/numbering.py models restart as num/lvlOverride/startOverride, not an abstract-definition mutation.
def test_restart_preserves_real_overrides_definition_and_opaque_metadata(tmp_path):
    doc = Document.open(FIXTURE)
    part = doc.part('NumberingDefinitionsPart')
    rel, = [r for r in doc.package.relationships(doc.main.uri) if doc.package.relationship_part(doc.main.uri, r['id']) == part.uri]
    doc.package.add_part('/custom/lists.xml', part.content_type, part.read_bytes())
    doc.package.remove_part(part.uri)
    doc.package.add_relationship(doc.main.uri, rel['type'], '/custom/lists.xml')
    numbering = Numbering(doc)
    original = numbering[2]
    original.element._tree.xml.set_attribute(original.element.node_id, 'urn:keep', 'opaque', 'original', 'keep')
    original.element.set_attribute(W[1:-1], 'durableId', '11')
    doc.part('NumberingDefinitionsPart').xml.root(e.numIdMacAtCleanup(val='6'))
    before = parts(doc)
    restarted = original.restart(start=12)
    restarted.apply(list(doc.main.xml.elements(w.Paragraph))[1])
    bullets = numbering.add([Level(format='bullet')])
    assert original.id == 2 and restarted.id != bullets.id
    assert {restarted.id, bullets.id}.isdisjoint(range(1, 7))
    after = parts(doc)
    assert {n: b for n, b in before.items() if n not in {'custom/lists.xml', 'word/document.xml'}} == {
        n: b for n, b in after.items() if n not in {'custom/lists.xml', 'word/document.xml'}}
    old, new = ET.fromstring(before['custom/lists.xml']), ET.fromstring(after['custom/lists.xml'])
    assert [ET.tostring(e) for e in old.findall(W+'abstractNum')] == [ET.tostring(e) for e in new.findall(W+'abstractNum')[:6]]
    assert [ET.tostring(e) for e in old.findall(W+'num')] == [ET.tostring(e) for e in new.findall(W+'num')[:6]]
    added = new.findall(W+'num')[6]
    assert added.get('{urn:keep}opaque') == 'original' and added.get(W+'durableId') != '11'
    assert added.find(W+'lvlOverride/'+W+'startOverride').get(W+'val') == '12'
    assert new[-1].tag == W+'numIdMacAtCleanup'
    assert max(i for i, e in enumerate(new) if e.tag == W+'abstractNum') < min(i for i, e in enumerate(new) if e.tag == W+'num')
    path = tmp_path/'lists.docx'
    doc.save(path)
    assert Numbering(Document.open(path))[restarted.id].definition.attribute(W[1:-1], 'abstractNumId') == original.definition.attribute(W[1:-1], 'abstractNumId')
