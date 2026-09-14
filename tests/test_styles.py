"""Explicit style edits over a Pandoc fixture, with python-docx API examples and preserved direct overrides."""
from pathlib import Path
import xml.etree.ElementTree as ET
import pytest
from oxml import Document, e, w
from oxml.styles import Styles
from corpus_helpers import parts

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
FIXTURE = Path(__file__).parent/'fixtures/body/char_styles.docx'

# python-docx tests/styles/test_styles.py: find/create styles, missing IDs and type mismatch;
# original char_styles.docx retains its independent character-style/direct-formatting combinations.
def test_create_find_apply_styles_with_relocated_part_and_direct_overrides(tmp_path):
    doc = Document.open(FIXTURE)
    old = doc.part('StyleDefinitionsPart')
    rel, = [r for r in doc.package.relationships(doc.main.uri) if doc.package.relationship_part(doc.main.uri, r['id']) == old.uri]
    doc.package.add_part('/custom/styles.xml', old.content_type, old.read_bytes())
    doc.package.remove_part(old.uri)
    doc.package.add_relationship(doc.main.uri, rel['type'], '/custom/styles.xml')
    before = parts(doc)
    styles = doc.styles
    assert styles.find('Emphasis', 'character').id == 'Emphasis' and styles.find('Absent') is None
    clause = styles.add('Clause', name='Legal clause', based_on='Normal', paragraph=[e.keepNext()])
    emphasis = styles.add('ClauseEmphasis', kind='character', based_on='Emphasis', run=[e.i(val='0')])
    paragraph, run = next(doc.main.xml.elements(w.Paragraph)), next(doc.main.xml.elements(w.Run))
    old_run = ET.fromstring(doc.main.xml.xml.subtree_bytes(run.node_id))
    clause.apply(paragraph)
    emphasis.apply(run)
    after = parts(doc)
    assert {n: b for n, b in before.items() if n not in {'custom/styles.xml', 'word/document.xml'}} == {
        n: b for n, b in after.items() if n not in {'custom/styles.xml', 'word/document.xml'}}
    old_styles, new_styles = ET.fromstring(before['custom/styles.xml']), ET.fromstring(after['custom/styles.xml'])
    assert [ET.tostring(e) for e in old_styles] == [ET.tostring(e) for e in new_styles[:-2]]
    assert [e.tag for e in new_styles[-2]] == [W+'name', W+'basedOn', W+'pPr']
    assert new_styles[-1].find(W+'rPr/'+W+'i').get(W+'val') == '0'
    new_run = ET.fromstring(doc.main.xml.xml.subtree_bytes(run.node_id))
    for element in (old_run, new_run):
        props = element.find(W+'rPr')
        if props is not None:
            for ref in props.findall(W+'rStyle'): props.remove(ref)
            if not len(props) and not props.attrib: element.remove(props)
    assert ET.tostring(new_run) == ET.tostring(old_run)
    assert ET.fromstring(after['word/document.xml']).find('.//'+W+'pPr/'+W+'pStyle').get(W+'val') == 'Clause'
    path = tmp_path/'styled.docx'
    doc.save(path)
    assert Styles(Document.open(path))['Clause'].name == 'Legal clause'

def test_style_creation_collision_and_refusals():
    doc = Document.new()
    doc.package.add_part('/word/styles.xml', 'application/octet-stream', b'occupied')
    paragraph = doc.main.xml.root.children[0](e.p(e.r(e.t('Text'))))
    styles = Styles(doc)
    style = styles.add('Body')
    assert doc.part('StyleDefinitionsPart').uri != '/word/styles.xml'
    style.apply(paragraph)
    before = doc.bytes()
    style.apply(paragraph)
    assert doc.bytes() == before
    with pytest.raises(ValueError): styles.add('Body')
    with pytest.raises(KeyError): styles.add('Child', based_on='Missing')
    with pytest.raises(ValueError): style.apply(paragraph.children[-1])
    with pytest.raises(ValueError): styles.add('Bad', name='\x00')
    other = Document.new()
    foreign = other.main.xml.root.children[0](e.p())
    with pytest.raises(ValueError): style.apply(foreign)
    assert doc.bytes() == before and doc.package.read_part('/word/styles.xml') == b'occupied'
