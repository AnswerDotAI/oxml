"""Detached construction and XML-only copying into differently bound trees."""
from xml.etree.ElementTree import fromstring
from xml.dom.minidom import parseString

import pytest
from oxml import Document, E, Tree, w

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'

def test_nested_paragraph_table_construction_and_docx_roundtrip():
    doc = Document.new()
    tree = doc.main.xml
    body = next(tree.elements(w.Body))
    revision = tree.xml.revision
    text = ' <&>\r café😀 '
    source = Document.new()
    imported = E('w:r', E('w:rPr', E('w:b')), E('w:t', text, attrs={'xml:space': 'preserve'}), E('plain'),
                 attrs={'r:id': 'rId9', 'kind': 'q:Type'}, ns={'q': 'urn:opaque'}).append_to(next(source.main.xml.elements(w.Body)))
    original = source.bytes()
    paragraph = E('w:p', E('w:pPr', E('w:jc', attrs={'w:val': 'center'})),
                  imported, ns={'': 'urn:destination', 'q': 'urn:conflict'})
    assert source.bytes() == original
    next(source.main.xml.elements(w.Text)).value = 'changed after snapshot'
    imported.delete()
    source.main.replace(b'<root/>')
    with pytest.raises(ReferenceError): E('w:p', imported)
    table = E('w:tbl', E('w:tblPr'), E('w:tblGrid', *(E('w:gridCol', attrs={'w:w': 2000}) for _ in range(2))),
              *(E('w:tr', *(E('w:tc', E('w:p', E('w:r', E('w:t', value)))) for value in row)) for row in [('A', 'B'), ('C', 'D')]))
    assert tree.xml.revision == revision
    live = paragraph.append_to(body, 0)
    assert isinstance(live, w.Paragraph) and tree.xml.revision == revision + 1
    assert isinstance(table.append_to(body, 1), w.Table) and tree.xml.revision == revision + 2
    reopened = Document.from_bytes(doc.bytes())
    assert [t.value for t in reopened.main.xml.elements(w.Text)] == [text, 'A', 'B', 'C', 'D']
    assert fromstring(reopened.main.read_bytes()).find(f'.//{{{W}}}gridCol').attrib == {f'{{{W}}}w': '2000'}
    copied = parseString(reopened.main.read_bytes()).getElementsByTagNameNS(W, 'r')[0]
    assert copied.getAttribute('xmlns:q') == 'urn:opaque' and copied.getAttribute('kind') == 'q:Type'
    assert copied.getAttribute('r:id') == 'rId9' and copied.getElementsByTagName('plain')[0].namespaceURI is None

def test_builder_mixed_content_custom_namespaces_and_default_isolation():
    tree = Tree(b'<root xmlns="urn:destination" xmlns:x="urn:conflict"/>')
    expression = E('plain', 'left<&>\r', E('x:é', 'middle', attrs={'x:kind': 'q:Type', 'plain': 'a\t\n\r"&'}),
                   'right', None, ns={'x': 'urn:chosen', 'q': 'urn:opaque'})
    child = expression.append_to(tree.root)
    assert child.raw['qname'] == ('', 'plain') and dict(child.raw['namespaces'])['q'] == 'urn:opaque'
    parsed = fromstring(tree.bytes())[0]
    assert parsed.tag == 'plain' and parsed.text == 'left<&>\r' and parsed[0].tail == 'right'
    assert parsed[0].tag == '{urn:chosen}é' and parsed[0].attrib == {'{urn:chosen}kind': 'q:Type', 'plain': 'a\t\n\r"&'}
    before, revision = tree.bytes(), tree.xml.revision
    for expression in (E('r', '\x00'), E('missing:r'), E('r', ns={'xml': 'urn:wrong'})):
        with pytest.raises(ValueError): expression.append_to(tree.root)
        assert tree.bytes() == before and tree.xml.revision == revision
    with pytest.raises(ValueError): E('r injected="yes"')
    with pytest.raises(TypeError): E('r', b'<xml/>')

def test_cross_tree_copy_keeps_inherited_and_opaque_namespace_bindings():
    source = Tree(b'<root xmlns="urn:source" xmlns:p="urn:old" xmlns:q="urn:opaque" '
                  b'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                  b'<p:item refs="q:Type" r:id="rId9">left<!--keep--><?pi data?><plain/>right</p:item></root>')
    original = source.bytes()
    item = source.root.children[0]
    destination = Tree(b'<root xmlns="urn:destination" xmlns:p="urn:new" xmlns:q="urn:other"/>')
    copied = item.copy_to(destination.root)
    bindings = dict(copied.raw['namespaces'])
    assert bindings['p'] == 'urn:old' and bindings['q'] == 'urn:opaque' and bindings[''] == 'urn:source'
    parsed = fromstring(destination.bytes())[0]
    assert parsed.tag == '{urn:old}item' and parsed[0].tag == '{urn:source}plain'
    assert parsed.attrib['{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'] == 'rId9'
    assert b'<!--keep--><?pi data?>' in destination.bytes() and source.bytes() == original
    plain = Tree(b'<plain><child/></plain>').root.copy_to(destination.root)
    assert plain.raw['qname'] == ('', 'plain') and fromstring(destination.bytes())[1][0].tag == 'child'
    E('holder', 'before', plain, None, E('after'), 'tail', ns={'': 'urn:holder'}).append_to(destination.root)
    parsed = fromstring(destination.bytes())[2]
    assert parsed.text == 'before' and parsed[0].tag == 'plain' and parsed[0][0].tag == 'child'
    assert parsed[1].tag == '{urn:holder}after' and parsed[1].tail == 'tail'
