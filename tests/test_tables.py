"""Rectangular table edits and explicit preservation boundaries on a real merged table."""
from pathlib import Path
from xml.etree.ElementTree import fromstring
import pytest
from oxml import Document, E, Story, Table, w

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'

def test_rectangular_table_rows_columns_and_cell_text_roundtrip():
    doc = Document.new()
    body = next(doc.main.xml.elements(w.Body))
    table = Table.add(body, [['A', 'B'], ['C', 'D']], [2000, 3000])
    properties = E('w:tcPr', E('w:shd', attrs={'w:fill': 'CCCCCC'})).append_to(table.cells(0)[0], 0)
    E('w:pPr', E('w:jc', attrs={'w:val': 'right'})).append_to(table.cells(0)[0].children[1], 0)
    w14 = 'http://schemas.microsoft.com/office/word/2010/wordml'
    table.element._tree.xml.set_attribute(table.rows[1].node_id, w14, 'paraId', '12345678', 'w14')
    inserted = table.insert_row(1, ['E', 'F'])
    assert inserted.attribute(w14, 'paraId') is None and table.rows[2].attribute(w14, 'paraId') == '12345678'
    table.insert_column(1)
    assert [[Story(c).text for c in table.cells(i)] for i in range(3)] == [['A', '', 'B'], ['E', '', 'F'], ['C', '', 'D']]
    Story(table.cells(1)[1]).range(0, 0).replace('new')
    table.delete_row(2)
    table.delete_column(2)
    xml = fromstring(Document.from_bytes(doc.bytes()).main.read_bytes())
    rows = xml.findall('.//'+W+'tr')
    assert [[''.join(t.text or '' for t in c.iter(W+'t')) for c in r.findall(W+'tc')] for r in rows] == [['A', ''], ['E', 'new']]
    assert [c.get(W+'w') for c in xml.findall('.//'+W+'gridCol')] == ['2000', '3000']
    assert rows[0][0].find(W+'tcPr/'+W+'shd').get(W+'fill') == 'CCCCCC'
    assert rows[0][0].find(W+'p/'+W+'pPr/'+W+'jc').get(W+'val') == 'right'
    assert properties.node_id  # Original cell metadata was not rebuilt.
    cell = table.cells(1)[1]
    nested = Table.add(cell, [['inner']], [1000])
    assert cell.children[-1].raw['qname'][1] == 'p' and Story(nested.cells(0)[0]).text == 'inner'
    E('w:bookmarkStart', attrs={'w:id': '7', 'w:name': 'keep'}).append_to(cell.children[-1], 0)
    original = doc.bytes()
    with pytest.raises(NotImplementedError, match='bookmarks/comments'): table.delete_column(1)
    assert doc.bytes() == original


def test_real_merged_table_is_readable_but_structural_edits_are_explicit():
    path = Path(__file__).parent/'fixtures/body/table_header_rowspan.docx'
    doc = Document.open(path)
    table = Table(next(doc.main.xml.elements(w.Table)))
    assert [Story(c).text for c in table.cells(0)] == ['A', 'B', 'C', 'D', 'E', 'F']
    for operation in (lambda: table.insert_row(1), lambda: table.delete_column(0)):
        with pytest.raises(NotImplementedError): operation()
        assert doc.bytes() == path.read_bytes()
