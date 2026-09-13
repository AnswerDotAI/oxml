"""Detached construction and XML-only copying into differently bound trees."""
from xml.etree.ElementTree import fromstring
from xml.dom.minidom import parseString

import pytest
from oxml import Document, E, Tree, e, w

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'

def test_callable_attachment_places_definitions_and_keeps_content_order():
    tree = Tree(e.numbering(e.num(e.abstractNumId(val=0), numId=1), e.numIdMacAtCleanup(val=1)).bytes())
    root = tree.root
    root.insert_xml(0, b'<!--keep-->\n')
    revision = tree.xml.revision
    added = root(e.num(e.abstractNumId(val=0), numId=2))
    assert isinstance(added, w.NumberingInstance) and added.parent.node_id == root.node_id
    assert tree.xml.revision == revision + 1
    root(e.abstractNum(e.multiLevelType(val='singleLevel'), abstractNumId=0))
    assert [c.qname[1] for c in root.children] == ['abstractNum', 'num', 'num', 'numIdMacAtCleanup']
    assert parseString(tree.bytes()).documentElement.firstChild.nodeValue == 'keep'
    assert [c.attribute(W, 'numId') for c in root.children[1:3]] == ['1', '2']

    body = Tree(e.body(e.p(), e.tbl(), e.sectPr()).bytes()).root
    paragraph = body(e.p(e.r(e.t('After the table'))))
    assert [c.qname[1] for c in body.children] == ['p', 'tbl', 'p', 'sectPr']
    assert body.children[2].node_id == paragraph.node_id
    properties = paragraph(e.pPr(e.jc(val='right')))
    assert paragraph.children[0].node_id == properties.node_id
    assert paragraph(e.r(e.t('Before')), index=1).parent.node_id == paragraph.node_id

def test_callable_attachment_requires_explicit_placement_when_order_is_unknown():
    root = Tree(b'<root/>').root
    with pytest.raises(ValueError, match='index'): root(e.p())
    child = root(e.p(), index=0)
    with pytest.raises(TypeError, match='copy_to'): root(child, index=0)

    body = Tree(e.body(e.p(), e.sectPr()).bytes()).root
    unknown = E('keep', ns={'keep': 'urn:keep'}).opaque()
    body(unknown, index=1)
    before = body._tree.bytes()
    with pytest.raises(ValueError, match='index'): body(e.p())
    assert body._tree.bytes() == before
    paragraph = body.children[0]
    with pytest.raises(ValueError, match='index'): paragraph(E('w14').conflictIns())

def test_nested_paragraph_table_construction_and_docx_roundtrip():
    doc = Document.new()
    tree = doc.main.xml
    body = next(tree.elements(w.Body))
    revision = tree.xml.revision
    text = ' <&>\r café😀 '
    source = Document.new()
    source_e = E('w', attr_ns='w', ns={'q': 'urn:opaque'})
    source_body = next(source.main.xml.elements(w.Body))
    imported = source_body(source_e.r(e.rPr(e.b()), e.t(text, xml__space='preserve'), E().plain(),
                                     r__id='rId9', attrs_={'kind': 'q:Type'}), index=0)
    original = source.bytes()
    destination_e = E('w', attr_ns='w', ns={'': 'urn:destination', 'q': 'urn:conflict'})
    paragraph = destination_e.p(e.pPr(e.jc(val='center')), imported)
    assert source.bytes() == original
    next(source.main.xml.elements(w.Text)).value = 'changed after snapshot'
    imported.delete()
    source.main.replace(b'<root/>')
    with pytest.raises(ReferenceError): e.p(imported)
    table = e.tbl(e.tblPr(), e.tblGrid(e.gridCol(w=2000) for _ in range(2)),
                  (e.tr(e.tc(e.p(e.r(e.t(value)))) for value in row) for row in [('A', 'B'), ('C', 'D')]))
    assert tree.xml.revision == revision
    live = body(paragraph)
    assert isinstance(live, w.Paragraph) and tree.xml.revision == revision + 1
    assert isinstance(body(table), w.Table) and tree.xml.revision == revision + 2
    reopened = Document.from_bytes(doc.bytes())
    assert [t.value for t in reopened.main.xml.elements(w.Text)] == [text, 'A', 'B', 'C', 'D']
    assert fromstring(reopened.main.read_bytes()).find(f'.//{{{W}}}gridCol').attrib == {f'{{{W}}}w': '2000'}
    copied = parseString(reopened.main.read_bytes()).getElementsByTagNameNS(W, 'r')[0]
    assert copied.getAttribute('xmlns:q') == 'urn:opaque' and copied.getAttribute('kind') == 'q:Type'
    assert copied.getAttribute('r:id') == 'rId9' and copied.getElementsByTagName('plain')[0].namespaceURI is None

def test_builder_mixed_content_custom_namespaces_and_default_isolation():
    tree = Tree(b'<root xmlns="urn:destination" xmlns:x="urn:conflict"/>')
    custom = E(ns={'x': 'urn:chosen', 'q': 'urn:opaque'})
    expression = custom.plain('left<&>\r', custom('x:é', 'middle', x__kind='q:Type', plain='a\t\n\r"&'), 'right', None)
    child = tree.root(expression, index=0)
    assert child.raw['qname'] == ('', 'plain') and dict(child.raw['namespaces'])['q'] == 'urn:opaque'
    parsed = fromstring(tree.bytes())[0]
    assert parsed.tag == 'plain' and parsed.text == 'left<&>\r' and parsed[0].tail == 'right'
    assert parsed[0].tag == '{urn:chosen}é' and parsed[0].attrib == {'{urn:chosen}kind': 'q:Type', 'plain': 'a\t\n\r"&'}
    before, revision = tree.bytes(), tree.xml.revision
    for make in (lambda: E().r('\x00'), lambda: E()('missing:r'), lambda: E(ns={'xml': 'urn:wrong'}).r()):
        with pytest.raises(ValueError): tree.root(make(), index=0)
        assert tree.bytes() == before and tree.xml.revision == revision
    with pytest.raises(ValueError): E()('r injected="yes"')
    with pytest.raises(TypeError): E().r(b'<xml/>')

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
    holder = E(ns={'': 'urn:holder'})
    destination.root(holder.holder('before', plain, None, holder.after(), 'tail'), index=2)
    parsed = fromstring(destination.bytes())[2]
    assert parsed.text == 'before' and parsed[0].tag == 'plain' and parsed[0][0].tag == 'child'
    assert parsed[1].tag == '{urn:holder}after' and parsed[1].tail == 'tail'
