"""Original DOCX body structures and narrowly edited derivatives; no rendered-output oracle."""
from pathlib import Path
from xml.dom import minidom
from zipfile import ZipFile
from oxml import w
from corpus_helpers import open_original, saved_edit

FIXTURES = Path(__file__).parent/'fixtures/body'
W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
XML = 'http://www.w3.org/XML/1998/namespace'

def nodes(node, name): return list(node.getElementsByTagNameNS(W, name))
def children(node, name): return [n for n in node.childNodes if n.namespaceURI == W and n.localName == name]
def text(node): return ''.join(n.firstChild.data for n in nodes(node, 't'))

def test_split_formatted_runs_preserve_spaces_proofing_and_breaks(tmp_path):
    filename = 'inline_formatting.docx'
    doc, parts = open_original(FIXTURES/filename, 'word/document.xml')
    tree, expected = doc.main.xml, parts['word/document.xml']
    paragraph = nodes(expected, 'p')[0]
    runs = children(paragraph, 'r')
    assert [text(r) for r in runs] == ['Regular text ', 'italics', ' ', 'bold ', 'bold', ' italics', '.']
    assert [n.localName for n in paragraph.childNodes] == ['r', 'r', 'r', 'r', 'proofErr', 'r', 'proofErr', 'r', 'r']
    assert [n.localName for n in children(runs[4], 'rPr')[0].childNodes] == ['b', 'i']
    assert nodes(runs[5], 't')[0].getAttributeNS(XML, 'space') == 'preserve'
    assert len(nodes(expected, 'br')) == 1 and len(nodes(expected, 'bookmarkStart')) == 1
    target = next(t for t in tree.elements(w.Text) if t.value == 'bold')
    target.value = 'strong'
    nodes(runs[4], 't')[0].firstChild.data = 'strong'
    reopened = saved_edit(doc, FIXTURES/filename, tmp_path/filename, {'word/document.xml': expected}).main.xml
    assert [t.value for t in reopened.elements(w.Text)][:7] == ['Regular text ', 'italics', ' ', 'bold ', 'strong', ' italics', '.']

def test_character_style_edit_preserves_references_and_direct_overrides(tmp_path):
    filename, part = 'char_styles.docx', 'word/styles.xml'
    doc, parts = open_original(FIXTURES/filename, part)
    tree, expected = doc.package.part('/'+part).xml, parts[part]
    emphasis = next(s for s in nodes(expected, 'style') if s.getAttributeNS(W, 'styleId') == 'Emphasis')
    assert emphasis.getAttributeNS(W, 'type') == 'character'
    assert nodes(emphasis, 'basedOn')[0].getAttributeNS(W, 'val') == 'DefaultParagraphFont'
    assert [n.localName for n in children(emphasis, 'rPr')[0].childNodes] == ['i', 'iCs']
    with ZipFile(FIXTURES/filename) as package: body = minidom.parseString(package.read('word/document.xml'))
    unitalicized = [r for r in nodes(body, 'r') if any(n.getAttributeNS(W, 'val') == '0' for n in nodes(r, 'i'))]
    assert [text(r) for r in unitalicized] == ['style', 'words']
    assert all(nodes(r, 'rStyle')[0].getAttributeNS(W, 'val') == 'Emphasis' for r in unitalicized)
    style = next(s for s in tree.elements(w.Style) if s.style_id == 'Emphasis')
    properties = next(c for c in style.children if isinstance(c, w.StyleRunProperties))
    italic = next(c for c in properties.children if isinstance(c, w.Italic))
    italic.val = False
    nodes(emphasis, 'i')[0].setAttributeNS(W, 'w:val', 'false')
    reopened = saved_edit(doc, FIXTURES/filename, tmp_path/filename, {part: expected}).package.part('/'+part).xml
    assert next(i for i in reopened.elements(w.Italic) if i.parent.parent.style_id == 'Emphasis').val is False

def test_merged_header_cell_structural_edit_preserves_grid_and_continuations(tmp_path):
    filename = 'table_header_rowspan.docx'
    doc, parts = open_original(FIXTURES/filename, 'word/document.xml')
    tree, expected = doc.main.xml, parts['word/document.xml']
    table = nodes(expected, 'tbl')[0]
    rows = children(table, 'tr')
    first, second = children(rows[0], 'tc'), children(rows[1], 'tc')
    assert [text(c) for c in first] == ['A', 'B', 'C', 'D', 'E', 'F']
    assert [text(c) for c in second] == ['', '', '', '', 'G', 'H', 'I', '']
    assert [n.getAttributeNS(W, 'w') for n in nodes(children(table, 'tblGrid')[0], 'gridCol')] == [
        '3150', '1400', '1027', '996', '792', '727', '728', '1440']
    assert nodes(first[0], 'vMerge')[0].getAttributeNS(W, 'val') == 'restart'
    assert not nodes(second[0], 'vMerge')[0].hasAttributeNS(W, 'val')
    assert nodes(first[4], 'gridSpan')[0].getAttributeNS(W, 'val') == '3'
    cell = next(tree.elements(w.TableCell))
    paragraph, = cell.append_xml(b'<w:p><w:r><w:t>Header detail</w:t></w:r></w:p>')
    assert isinstance(paragraph, w.Paragraph) and paragraph.parent.node_id == cell.node_id
    added = expected.createElementNS(W, 'w:p')
    run, value = expected.createElementNS(W, 'w:r'), expected.createElementNS(W, 'w:t')
    value.appendChild(expected.createTextNode('Header detail'))
    run.appendChild(value)
    added.appendChild(run)
    first[0].appendChild(added)
    reopened = saved_edit(doc, FIXTURES/filename, tmp_path/filename, {'word/document.xml': expected}).main.xml
    assert [p.children[-1].children[-1].value for p in next(reopened.elements(w.TableCell)).children if isinstance(p, w.Paragraph)] == [
        'A', 'Header detail']

def test_numbering_override_edit_preserves_abstract_levels_and_body_ids(tmp_path):
    filename, part = 'lists_level_override.docx', 'word/numbering.xml'
    doc, parts = open_original(FIXTURES/filename, part)
    tree, expected = doc.package.part('/'+part).xml, parts[part]
    instances = nodes(expected, 'num')
    assert [(n.getAttributeNS(W, 'numId'), nodes(n, 'abstractNumId')[0].getAttributeNS(W, 'val'),
             nodes(n, 'startOverride')[0].getAttributeNS(W, 'val')) for n in instances] == [
        ('1', '3', '1'), ('2', '5', '2'), ('3', '1', '3'), ('4', '0', '4'), ('5', '4', '5'), ('6', '2', '6')]
    assert len(nodes(expected, 'abstractNum')) == 6 and len(nodes(expected, 'lvl')) == 54
    assert [n.val for n in doc.main.xml.elements(w.NumberingId)] == [1, 2, 3, 4, 5, 6]
    assert [n.val for n in doc.main.xml.elements(w.NumberingLevelReference)] == [0]*6
    target = next(n for n in tree.elements(w.StartOverrideNumberingValue) if n.val == 2)
    target.val = 12
    nodes(instances[1], 'startOverride')[0].setAttributeNS(W, 'w:val', '12')
    reopened = saved_edit(doc, FIXTURES/filename, tmp_path/filename, {part: expected}).package.part('/'+part).xml
    assert [n.val for n in reopened.elements(w.StartOverrideNumberingValue)] == [1, 12, 3, 4, 5, 6]

def test_move_paragraph_within_section_preserves_boundaries_and_tables(tmp_path):
    filename = 'sct-inner-content.docx'
    doc, parts = open_original(FIXTURES/filename, 'word/document.xml')
    tree, expected = doc.main.xml, parts['word/document.xml']
    body = nodes(expected, 'body')[0]
    assert [(n.localName, text(n)) for n in body.childNodes] == [
        ('p', 'P1'), ('tbl', 'T2'), ('p', 'P3'), ('tbl', 'T4'), ('p', 'P5'), ('p', 'P6'),
        ('p', 'P7'), ('p', 'P8'), ('p', 'P9'), ('sectPr', '')]
    assert len(nodes(expected, 'sectPr')) == 3
    assert [text(p) for p in children(body, 'p') if nodes(p, 'sectPr')] == ['P3', 'P6']
    assert [(p.width, p.height) for p in tree.elements(w.PageSize)] == [(12240, 15840)]*3
    value = next(t for t in tree.elements(w.Text) if t.value == 'P5')
    paragraph, target_body = value.parent.parent, next(tree.elements(w.Body))
    identity = paragraph.node_id
    paragraph.move_to(target_body, 3)
    body.insertBefore(body.childNodes[4], body.childNodes[3])
    assert paragraph.node_id == identity and value.value == 'P5'
    reopened = saved_edit(doc, FIXTURES/filename, tmp_path/filename, {'word/document.xml': expected}).main.xml
    assert [c.raw['qname'][1] for c in next(reopened.elements(w.Body)).children] == [
        'p', 'tbl', 'p', 'p', 'tbl', 'p', 'p', 'p', 'p', 'sectPr']
