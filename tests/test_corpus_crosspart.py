"""Real secondary parts and opaque compatibility markup; no rendering or validity oracle."""
from hashlib import sha256
from pathlib import Path
from zipfile import ZipFile
from oxml import w
from corpus_helpers import open_original, saved_edit

FIXTURES = Path(__file__).parent/'fixtures'
W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
WP = 'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing'
A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
MC = 'http://schemas.openxmlformats.org/markup-compatibility/2006'
XMLNS = 'http://www.w3.org/2000/xmlns/'

def nodes(node, name, uri=W): return list(node.getElementsByTagNameNS(uri, name))

def test_header_drawing_edit_retains_scoped_image_relationship_and_jpeg(tmp_path):
    filename, part = 'crosspart/headerPic.docx', 'word/header1.xml'
    doc, expected = open_original(FIXTURES/filename, part)
    package = doc.package
    reference = next(doc.main.xml.elements(w.HeaderReference))
    assert reference.attribute(R, 'id') == 'rId6' and reference.attribute(W, 'type') == 'default'
    assert package.relationship_part(doc.main.uri, 'rId6') == '/'+part
    assert nodes(expected[part], 'blip', A)[0].getAttributeNS(R, 'embed') == 'rId1'
    assert package.relationships('/'+part) == [
        {'id': 'rId1', 'type': R+'/image', 'target': 'media/image1.jpeg', 'target_mode': 'Internal'}]
    assert package.relationship_part('/'+part, 'rId1') == '/word/media/image1.jpeg'
    assert package.relationship_part(doc.main.uri, 'rId1') == '/word/styles.xml'
    picture = package.read_part('/word/media/image1.jpeg')
    assert len(picture) == 2188 and sha256(picture).hexdigest() == '4799801a6351128527f0bb7b4a406ee8fc3893ad334141c942839f6e4a51f5b0'
    properties = nodes(expected[part], 'docPr', WP)[0]
    assert properties.getAttribute('name') == 'Рисунок 1' and properties.getAttribute('descr') == 'DOZOR'
    tree = package.part('/'+part).xml
    target = next(e for e in tree.elements() if e.raw['qname'] == (WP, 'docPr'))
    tree.xml.set_attribute(target.node_id, '', 'descr', 'Header image description')
    properties.setAttribute('descr', 'Header image description')
    reopened = saved_edit(doc, FIXTURES/filename, tmp_path/'header.docx', expected)
    assert reopened.package.relationship_part('/'+part, 'rId1') == '/word/media/image1.jpeg'

def test_footer_link_text_edit_preserves_external_target_in_footer_scope(tmp_path):
    filename, part = 'crosspart/footer-contain-hyperlink.docx', 'word/footer1.xml'
    doc, expected = open_original(FIXTURES/filename, part)
    reference = next(doc.main.xml.elements(w.FooterReference))
    assert reference.attribute(R, 'id') == 'rId6' and reference.attribute(W, 'type') == 'default'
    assert doc.package.relationship_part(doc.main.uri, 'rId6') == '/'+part
    assert nodes(expected[part], 'hyperlink')[0].getAttributeNS(R, 'id') == 'rId1'
    relationship = [{'id': 'rId1', 'type': R+'/hyperlink', 'target': 'http://www.google.com/', 'target_mode': 'External'}]
    assert doc.package.relationships('/'+part) == relationship
    assert doc.package.relationship_part('/'+part, 'rId1') is None
    assert doc.package.relationship_part(doc.main.uri, 'rId1') == '/word/styles.xml'
    target, = doc.package.part('/'+part).xml.elements(w.Text)
    assert target.value == nodes(expected[part], 't')[0].firstChild.data == 'www.google.com'
    target.value = 'Search the web'
    nodes(expected[part], 't')[0].firstChild.data = 'Search the web'
    reopened = saved_edit(doc, FIXTURES/filename, tmp_path/'footer.docx', expected)
    assert reopened.package.relationships('/'+part) == relationship
    assert next(reopened.package.part('/'+part).xml.elements(w.Text)).value == 'Search the web'

def test_footnote_and_endnote_edits_preserve_reference_ids_and_separators(tmp_path):
    filename = 'crosspart/notes.docx'
    doc, expected = open_original(FIXTURES/filename, 'word/footnotes.xml', 'word/endnotes.xml')
    assert next(doc.main.xml.elements(w.FootnoteReference)).id == 1
    assert next(doc.main.xml.elements(w.EndnoteReference)).id == 1
    for kind, rel_id, original, replacement in [
        ('footnote', 'rId5', ' My note.', ' Revised footnote.'),
        ('endnote', 'rId6', ' This is an endnote at the end of the document.', ' Revised endnote.')]:
        part = f'word/{kind}s.xml'
        assert doc.package.relationship_part(doc.main.uri, rel_id) == '/'+part
        assert [(n.getAttributeNS(W, 'id'), n.getAttributeNS(W, 'type')) for n in nodes(expected[part], kind)] == [
            ('-1', 'separator'), ('0', 'continuationSeparator'), ('1', '')]
        assert len(nodes(expected[part], 'separator')) == len(nodes(expected[part], 'continuationSeparator')) == 1
        value, = nodes(expected[part], 't')
        assert value.firstChild.data == original and value.getAttributeNS('http://www.w3.org/XML/1998/namespace', 'space') == 'preserve'
        target, = doc.package.part('/'+part).xml.elements(w.Text)
        assert target.value == original
        target.value = replacement
        value.firstChild.data = replacement
    reopened = saved_edit(doc, FIXTURES/filename, tmp_path/'notes.docx', expected)
    assert next(reopened.main.xml.elements(w.FootnoteReference)).id == next(reopened.main.xml.elements(w.EndnoteReference)).id == 1

def test_mc_branches_and_prerelease_unknown_extensions_survive_adjacent_edit(tmp_path):
    filename, part = 'sdk/mcdoc.docx', 'word/document.xml'
    doc, expected = open_original(FIXTURES/filename, part)
    dom = expected[part]
    old_w14 = 'http://schemas.microsoft.com/office/word/2008/9/12/wordml'
    old_wps = 'http://schemas.microsoft.com/office/word/2008/6/28/wordprocessingShape'
    assert dom.documentElement.getAttributeNS(XMLNS, 'w14') == old_w14
    choice, fallback = nodes(dom, 'Choice', MC)[0], nodes(dom, 'Fallback', MC)[0]
    assert choice.getAttribute('Requires') == 'wps' and len(nodes(choice, 'wsp', old_wps)) == 1
    assert nodes(choice, 'drawing')[0].getAttributeNS(MC, 'MustUnderstand') == 'wps'
    assert nodes(fallback, 't')[0].firstChild.data == 'hello'
    assert nodes(dom, 'pPr')[0].getAttributeNS(MC, 'PreserveAttributes') == 'w14:myattr'
    assert nodes(dom, 'r')[0].getAttributeNS(MC, 'PreserveAttributes') == 'w14:*'
    spacing = nodes(dom, 'spacing')[0]
    assert spacing.getAttributeNS(old_w14, 'myattr') == 'myattr'
    assert spacing.getAttributeNS(old_w14, 'myanotherAttr') == 'anotherattr'
    target = next(doc.main.xml.elements(w.SpacingBetweenLines))
    assert target.attribute(W, 'after') == '156'
    target.set_attribute(W, 'after', '240')
    spacing.setAttributeNS(W, 'w:after', '240')
    saved_edit(doc, FIXTURES/filename, tmp_path/'mc.docx', expected)

def test_font_edit_keeps_namespaces_used_only_in_mc_attribute_values(tmp_path):
    filename, part = 'sdk/HelloO14.docx', 'word/fontTable.xml'
    doc, expected = open_original(FIXTURES/filename, part)
    dom = expected[part]
    w14, wpc = 'http://schemas.microsoft.com/office/word/2010/wordml', 'http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas'
    assert dom.documentElement.getAttributeNS(XMLNS, 'w14') == w14
    assert dom.documentElement.getAttributeNS(XMLNS, 'wpc') == wpc
    assert dom.documentElement.getAttributeNS(MC, 'Ignorable') == 'w14'
    assert nodes(dom, 'Choice', MC)[0].getAttribute('Requires') == 'wpc'
    assert not dom.getElementsByTagNameNS(w14, '*') and not dom.getElementsByTagNameNS(wpc, '*')
    assert not any(a.namespaceURI in (w14, wpc) for e in dom.getElementsByTagName('*') for a in e.attributes.values())
    assert [f.getAttributeNS(W, 'name') for f in nodes(dom, 'font')] == ['Calibri', 'Calibri1', '宋体', 'Times New Roman', 'Cambria']
    target = next(doc.package.part('/'+part).xml.elements(w.Font))
    assert target.name == 'Calibri'
    target.name = 'Aptos'
    nodes(dom, 'font')[0].setAttributeNS(W, 'w:name', 'Aptos')
    saved_edit(doc, FIXTURES/filename, tmp_path/'fonts.docx', expected)
