"""Real DOCX imports: independent references and payload checks, not rendered appearance."""
from pathlib import Path
import xml.etree.ElementTree as ET
import pytest
from oxml import Document, E, w, import_content
from corpus_helpers import parts

FIXTURES = Path(__file__).parent/'fixtures'
W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
W14 = '{http://schemas.microsoft.com/office/word/2010/wordml}'
WP = '{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}'
A = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
MC = '{http://schemas.openxmlformats.org/markup-compatibility/2006}'

def body(doc): return next(doc.main.xml.elements(w.Body))
def xml(element): return ET.fromstring(element._tree.xml.subtree_bytes(element.node_id))


def test_styles_and_real_table_import_preserve_destination_definitions_and_source():
    source = Document.open(FIXTURES/'body/char_styles.docx')
    destination = Document.open(FIXTURES/'body/char_styles.docx')
    before_source, before = source.bytes(), parts(destination)
    selected = [p for p in source.main.xml.elements(w.Paragraph) if any(e.tag == W+'rStyle' for e in xml(p).iter())]
    copied = import_content(source, selected, destination, body(destination))
    assert copied and source.bytes() == before_source
    old, new = ET.fromstring(before['word/styles.xml']), ET.fromstring(destination._part('StyleDefinitionsPart').read_bytes())
    assert [ET.tostring(e) for e in old] == [ET.tostring(e) for e in new[:len(old)]]
    added = {e.get(W+'styleId'): e for e in new[len(old):]}
    assert added and set(added).isdisjoint(e.get(W+'styleId') for e in old)
    assert {e.find(W+'name').get(W+'val') for e in added.values()}.isdisjoint(e.get(W+'val') for e in old.findall(W+'style/'+W+'name'))
    for paragraph, original in zip(copied, selected):
        actual, expected = xml(paragraph), xml(original)
        actual.attrib.pop(MC+'Ignorable', None)  # Inherited source directives become explicit on each imported root.
        for a, e in zip(actual.iter(), expected.iter()):
            if e.get(W14+'paraId') is not None: a.set(W14+'paraId', e.get(W14+'paraId'))
            if e.tag in {W+'bookmarkStart', W+'bookmarkEnd'}: a.attrib = e.attrib.copy()
        for a, e in zip(actual.iter(W+'rStyle'), expected.iter(W+'rStyle')):
            assert a.get(W+'val') in added
            a.set(W+'val', e.get(W+'val'))
        assert ET.tostring(actual) == ET.tostring(expected)
    after = parts(destination)
    assert all(after[n] == data for n, data in before.items() if n not in {'word/document.xml', 'word/styles.xml'})
    table_source = Document.open(FIXTURES/'body/table_header_rowspan.docx')
    table = next(table_source.main.xml.elements(w.Table))
    copied_table, = import_content(table_source, table, destination, body(destination))
    assert [e.attrib for e in xml(copied_table).iter(W+'vMerge')] == [e.attrib for e in xml(table).iter(W+'vMerge')]
    assert [e.text for e in xml(copied_table).iter(W+'t')] == [e.text for e in xml(table).iter(W+'t')]


def test_numbering_dependency_cycles_and_instance_dedup_keep_real_restart_override():
    source = Document.open(FIXTURES/'body/lists_level_override.docx')
    destination = Document.open(FIXTURES/'body/lists_level_override.docx')
    selected = next(p for p in source.main.xml.elements(w.Paragraph)
                    if any(e.get(W+'val') == '2' for e in xml(p).iter(W+'numId')))
    duplicate = selected.copy_to(body(source), 0)
    for e in duplicate.children:
        if e.raw['qname'][1] in {'bookmarkStart', 'bookmarkEnd'}: e.delete()
    instance = source.numbering[2]
    # A numbering-style dependency cycle must import once, not recurse forever or lose its reference.
    root = source._part('StyleDefinitionsPart').xml.root
    E('w:style', E('w:name', attrs={'w:val': 'ListLink'}), E('w:pPr', E('w:numPr', E('w:numId', attrs={'w:val': '2'}))),
      attrs={'w:type': 'numbering', 'w:styleId': 'ListLink'}).append_to(root)
    E('w:styleLink', attrs={'w:val': 'ListLink'}).append_to(instance.definition, 0)
    before_source, before = source.bytes(), ET.fromstring(destination._part('NumberingDefinitionsPart').read_bytes())
    copied = import_content(source, [selected, duplicate], destination, body(destination))
    assert source.bytes() == before_source
    after = ET.fromstring(destination._part('NumberingDefinitionsPart').read_bytes())
    assert len(after.findall(W+'num')) == 7 and len(after.findall(W+'abstractNum')) == 7
    for kind in ('num', 'abstractNum'):
        assert [ET.tostring(e) for e in before.findall(W+kind)] == [ET.tostring(e) for e in after.findall(W+kind)[:6]]
    imported = after.findall(W+'num')[-1]
    ident = imported.get(W+'numId')
    assert ident not in {e.get(W+'numId') for e in before.findall(W+'num')}
    assert [xml(p).find('.//'+W+'numId').get(W+'val') for p in copied] == [ident, ident]
    assert imported.find(W+'lvlOverride/'+W+'startOverride').get(W+'val') == '2'
    assert imported.find(W+'abstractNumId').get(W+'val') == after.findall(W+'abstractNum')[-1].get(W+'abstractNumId')
    assert after.findall(W+'abstractNum')[-1].find(W+'styleLink').get(W+'val') == 'ListLink'
    assert xml(destination.styles['ListLink'].element).find('.//'+W+'numId').get(W+'val') == ident


def test_header_images_and_footer_hyperlinks_resolve_in_their_actual_part_scopes(tmp_path):
    source = Document.open(FIXTURES/'crosspart/headerPic.docx')
    destination = Document.open(FIXTURES/'crosspart/headerPic.docx')
    header = source.package.part('/word/header1.xml').xml
    paragraph = next(header.elements(w.Paragraph))
    duplicate = paragraph.copy_to(header.root)
    before_source, before = source.bytes(), parts(destination)
    copied = import_content(source, [paragraph, duplicate], destination, body(destination))
    assert source.bytes() == before_source
    refs = [next(xml(p).iter(A+'blip')).get(R+'embed') for p in copied]
    assert refs[0] == refs[1] and refs[0] != 'rId1'
    image = destination.package.relationship_part(destination.main.uri, refs[0])
    assert image.lstrip('/') not in before
    assert destination.package.read_part(image) == before['word/media/image1.jpeg']
    old_ids = {e.get('id') for e in ET.fromstring(before['word/header1.xml']).iter(WP+'docPr')}
    new_ids = [next(xml(p).iter(WP+'docPr')).get('id') for p in copied]
    assert len(set(new_ids)) == 2 and not old_ids.intersection(new_ids)
    footer_source = Document.open(FIXTURES/'crosspart/footer-contain-hyperlink.docx')
    footer = footer_source.package.part('/word/footer1.xml').xml
    target_header = destination.package.part('/word/header1.xml').xml.root
    link_paragraph, = import_content(footer_source, next(footer.elements(w.Paragraph)), destination, target_header)
    link_id = next(xml(link_paragraph).iter(W+'hyperlink')).get(R+'id')
    relation, = [r for r in destination.package.relationships('/word/header1.xml') if r['id'] == link_id]
    assert relation['target'] == 'http://www.google.com/' and relation['target_mode'] == 'External'
    assert link_id != 'rId1'  # Existing rId1 here is the header image, not the footer's hyperlink.
    after = parts(destination)
    changed = {'[Content_Types].xml', 'word/document.xml', 'word/header1.xml', 'word/styles.xml',
               'word/_rels/document.xml.rels', 'word/_rels/header1.xml.rels'}
    assert all(after[n] == data for n, data in before.items() if n not in changed)
    path = tmp_path/'imported.docx'
    destination.save(path)
    assert Document.open(path).package.read_part(image) == before['word/media/image1.jpeg']


def test_bookmark_and_paragraph_ids_are_remapped_and_unsupported_import_is_read_only():
    source, destination = Document.new(), Document.new()
    expression = E('w:p', E('w:bookmarkStart', attrs={'w:id': '1', 'w:name': 'Clause'}), E('w:r', E('w:t', 'Clause')),
                   E('w:bookmarkEnd', attrs={'w:id': '1'}), E('w:hyperlink', E('w:r', E('w:t', 'See clause')),
                     attrs={'w:anchor': 'Clause'}), E('keep:opaque', attrs={'keep:data': 'kept'}, ns={'keep': 'urn:keep'}),
                   attrs={'w14:paraId': '00000001'})
    original = expression.append_to(body(source))
    source.main.xml.xml.declare_namespace(source.main.xml.root.node_id, 'old', 'urn:ancestor')
    source.main.xml.xml.set_attribute(source.main.xml.root.node_id, MC[1:-1], 'Ignorable', 'old', 'mc')
    original._tree.xml.declare_namespace(original.node_id, 'old', 'urn:shadow')
    expression.append_to(body(destination))
    copied, = import_content(source, original, destination, body(destination))
    actual = xml(copied)
    assert actual.get(W14+'paraId') != '00000001'
    assert actual.find(W+'bookmarkStart').get(W+'id') == actual.find(W+'bookmarkEnd').get(W+'id') != '1'
    assert actual.find(W+'bookmarkStart').get(W+'name') == actual.find(W+'hyperlink').get(W+'anchor') != 'Clause'
    assert actual.find('{urn:keep}opaque').get('{urn:keep}data') == 'kept'
    bindings = dict(copied.raw['namespaces'])
    assert {bindings[p] for p in actual.get(MC+'Ignorable').split()} == {'urn:ancestor'}
    before = destination.bytes()
    original.children[2].delete()  # Incomplete bookmark is not copied as a dangling range.
    with pytest.raises(NotImplementedError, match='complete'): import_content(source, original, destination, body(destination))
    reviews = Document.open(FIXTURES/'reviews/libreoffice/CommentReply.docx')
    with pytest.raises(NotImplementedError, match='comment'):
        import_content(reviews, next(reviews.main.xml.elements(w.Paragraph)), destination, body(destination))
    image_source = Document.open(FIXTURES/'crosspart/headerPic.docx')
    image_source.package.remove_relationship('/word/header1.xml', 'rId1')
    image_source.package.add_relationship('/word/header1.xml', R[1:-1]+'/chart', '/word/media/image1.jpeg', relationship_id='rId1')
    with pytest.raises(NotImplementedError, match='relationship'):
        import_content(image_source, next(image_source.package.part('/word/header1.xml').xml.elements(w.Paragraph)),
                       destination, body(destination))
    assert destination.bytes() == before
    revised_table = Document.open(FIXTURES/'body/table_header_rowspan.docx')
    cell = next(revised_table.main.xml.elements(w.TableCell))
    cell_paragraph = next(e for e in cell.children if e.qname == (W[1:-1], 'p'))
    plain = E('w:p', E('w:r', E('w:t', 'Imported'))).append_to(body(source))
    import_content(source, plain, revised_table, cell)  # Ordinary table nesting remains supported.
    props = next(e for e in cell.children if e.qname == (W[1:-1], 'tcPr'))
    E('w:cellDel', attrs={'w:id': '1', 'w:author': 'Reviewer'}).append_to(props)
    table_before = revised_table.bytes()
    with pytest.raises(NotImplementedError, match='revision context'):
        import_content(revised_table, cell_paragraph, destination, body(destination))
    with pytest.raises(NotImplementedError, match='revision context'): import_content(source, plain, revised_table, cell)
    assert revised_table.bytes() == table_before and destination.bytes() == before
