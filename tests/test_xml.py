"""General XML behavior, independent of OOXML schema validity."""
import json
from xml.etree.ElementTree import fromstring

import pytest

from oxml._core import Xml


def rows(xml): return [node(xml, id) for id in xml.element_ids()]
def node(xml, id): return json.loads(xml.node(id))
def elements(xml): return xml.element_ids()


def test_noop_bytes_ordered_content_and_stable_edits():
    original = (b"<?xml version='1.0' encoding='UTF-8'?><?before data?><d xmlns='urn:main' xmlns:x='urn:ext'>"
                b"<t xml:space='preserve'>before <![CDATA[&]]> after</t>prefix<!--keep--><?custom data?>"
                b"<x:opaque x:refs='x:Type'>left<inner/>right</x:opaque>suffix</d><!--after-->")
    xml = Xml(original)
    root, text, opaque, inner = elements(xml)
    assert xml.bytes() == original
    assert [node(xml, id)['kind'] for id in xml.document_children()] == ['pi', 'element', 'comment']
    assert rows(xml)[1]['text'] == 'before & after'
    assert rows(xml)[0]['content'][2:4] == [['comment', 'keep'], ['pi', ['custom', 'data']]]
    assert rows(xml)[2]['content'] == [['text', 'left'], ['element', inner], ['text', 'right']]
    xml.set_text(text, 'new <&>\r😀')
    assert node(xml, text)['text'] == 'new <&>\r😀'
    assert elements(xml) == [root, text, opaque, inner]
    assert node(xml, opaque)['attributes'] == [['urn:ext', 'refs', 'x:Type']]
    assert rows(Xml(xml.bytes()))[1]['text'] == 'new <&>\r😀'
    assert b'<!--keep--><?custom data?>' in xml.bytes()
    assert xml.bytes().endswith(b'</d><!--after-->')
    assert xml.revision == 1


@pytest.mark.parametrize('encoding,bom', [('utf-16-le', b'\xff\xfe'), ('utf-16-be', b'\xfe\xff'),
                                          ('utf-16-le', b''), ('utf-16-be', b'')])
def test_utf16_decoding_and_declared_utf8_serialization(encoding, bom):
    original = bom + '<?xml version="1.0" encoding="UTF-16"?><r a="old">café😀</r>'.encode(encoding)
    xml = Xml(original)
    assert xml.bytes() == original
    assert 'encoding="UTF-8"' in xml.document_text()
    assert xml.bytes() == original  # Read-only serialization does not dirty the original.
    xml.set_attribute(xml.root, '', 'a', 'tab\tline\nreturn\r<&"\'')
    assert 'encoding="UTF-8"' in xml.bytes().decode()
    assert rows(Xml(xml.bytes()))[0]['attributes'][0][2] == 'tab\tline\nreturn\r<&"\''
    assert rows(xml)[0]['text'] == 'café😀'


def test_insert_replace_delete_and_monotonic_node_ids():
    xml = Xml(b'<r xmlns="u" xmlns:p="v"><a/><b/></r>')
    root, a, b = elements(xml)
    inserted = xml.insert_xml(root, 1, b'left<!--keep--><?custom data?><p:new p:a="x"/>right')
    assert [node(xml, id)['kind'] for id in inserted] == ['text', 'comment', 'pi', 'element', 'text']
    new = inserted[3]
    assert xml.parent(new) == root
    assert node(xml, new)['qname'] == ['v', 'new']
    assert xml.children(root) == [a, *inserted, b]
    replacement, = xml.replace_node(a, b'<replaced><child/></replaced>')
    assert replacement > max(inserted)
    with pytest.raises(ReferenceError): xml.node(a)
    xml.delete(replacement)
    assert node(xml, b)['qname'] == ['u', 'b']
    with pytest.raises(ReferenceError): xml.node(replacement)
    old_ids = elements(xml)
    xml.replace(b'\xef\xbb\xbf<replacement/>')
    assert xml.bytes() == b'\xef\xbb\xbf<replacement/>' and xml.root > max(old_ids)
    for id in old_ids:
        with pytest.raises(ReferenceError): xml.node(id)
    current = xml.root
    xml.invalidate()
    for read in (xml.bytes, lambda: xml.node(current), xml.element_ids, lambda: xml.root):
        with pytest.raises(ReferenceError): read()


def test_move_copy_preserve_opaque_namespace_context_and_unqualified_names():
    xml = Xml(b'<r><from xmlns:p="old" xmlns:q="opaque"><p:x refs="q:Type p:Type"><plain/></p:x></from>'
              b'<to xmlns="new-default" xmlns:p="new" xmlns:fresh="new-scope"/></r>')
    root, source, x, plain, dest = elements(xml)
    xml.move_node(x, dest, 0)
    assert xml.parent(x) == dest and xml.parent(plain) == x
    assert node(xml, x)['qname'] == ['old', 'x']
    assert dict(node(xml, x)['namespaces'])['q'] == 'opaque'
    assert dict(node(xml, x)['namespaces'])['fresh'] == 'new-scope'
    copied = xml.copy(x, source, 0)
    assert copied > dest and node(xml, copied)['attributes'] == node(xml, x)['attributes']
    reparsed = Xml(xml.bytes())
    assert [r['qname'] for r in rows(reparsed)] == [r['qname'] for r in rows(xml)]
    for r in rows(reparsed):
        if r['qname'] == ['old', 'x']:
            assert dict(r['namespaces'])['p'] == 'old' and dict(r['namespaces'])['q'] == 'opaque'
    assert fromstring(xml.bytes()).find('{new-default}to/{old}x/plain') is not None
    xml.delete(x)
    with pytest.raises(ReferenceError): xml.node(plain)
    assert node(xml, copied)['qname'] == ['old', 'x']


def test_named_edits_and_ordered_text_comment_pi_nodes():
    xml = Xml(b'<r xmlns="old" xmlns:p="v"><t p:xmlns="legal"/></r>')
    root, t = elements(xml)
    xml.set_attribute(t, 'v', 'xmlns', 'ordinary attribute')
    xml.set_attribute(t, 'new', 'a', 'value', prefix='q')
    xml.set_attribute(t, '', 'a', 'unqualified')
    xml.rename(t, '', 'renamed')
    xml.remove_attribute(t, 'new', 'a')
    xml.declare_namespace(t, 'opaque', 'opaque-values')
    txt = xml.insert_text(t, 0, 'left')
    comment = xml.insert_comment(t, 1, 'keep')
    pi = xml.insert_pi(t, 2, 'custom', 'data')
    tail = xml.insert_text(t, 3, 'right')
    xml.set_text(txt, 'changed')
    xml.set_text(comment, 'edited')
    assert xml.children(t) == [txt, comment, pi, tail]
    assert node(xml, t)['content'] == [['text', 'changed'], ['comment', 'edited'], ['pi', ['custom', 'data']], ['text', 'right']]
    parsed = fromstring(xml.bytes())[0]
    assert parsed.tag == 'renamed' and parsed.attrib == {'{v}xmlns': 'ordinary attribute', 'a': 'unqualified'}
    assert node(xml, t)['parent'] == root
    xml.move_node(tail, t, 0)
    assert xml.children(t) == [tail, txt, comment, pi]
    xml.move_node(tail, t, 4)
    assert xml.children(t) == [txt, comment, pi, tail]


def test_refused_edits_are_atomic():
    original = b'<r xmlns="u"><a>before<!--keep--><c/>after</a><b/></r>'
    xml = Xml(original)
    root, a, c, b = elements(xml)
    edits = [lambda: xml.set_attribute(root, 'v', 'a', 'x'), lambda: xml.set_attribute(root, '', 'xmlns', 'x'),
             lambda: xml.set_attribute(root, '', 'a', '\x00'), lambda: xml.rename(a, '', 'a="x" b'),
             lambda: xml.set_text(a, 'lost'), lambda: xml.delete(root), lambda: xml.move_node(a, c, 0),
             lambda: xml.declare_namespace(root, '', 'changed'), lambda: xml.insert_xml(a, 0, b'<broken>'),
             lambda: xml.replace_node(c, b'<p:unbound/>'), lambda: xml.replace_node(root, b'<a/><b/>'),
             lambda: xml.insert_comment(a, 0, 'bad--comment'), lambda: xml.insert_pi(a, 0, 'xml', 'data'),
             lambda: xml.insert_pi(a, 0, 'p', 'bad?>'), lambda: xml.replace(b'<broken>')]
    for edit in edits:
        with pytest.raises(ValueError): edit()
        assert xml.bytes() == original and xml.revision == 0 and elements(xml) == [root, a, c, b]
    with pytest.raises(ReferenceError): xml.set_text(999, 'missing')
    with pytest.raises(IndexError): xml.insert_text(a, 999, 'missing')
    assert xml.bytes() == original and xml.revision == 0


def test_named_edits_cannot_rebind_opaque_prefixes():
    original = b'<r xmlns:p="old" token="p:Type"><p:child/></r>'
    for edit in ('attribute', 'rename', 'own-prefix'):
        xml = Xml(original)
        root, child = elements(xml)
        with pytest.raises(ValueError, match='binding'):
            if edit == 'attribute': xml.set_attribute(root, 'new', 'a', 'value', prefix='p')
            else: xml.rename(child if edit == 'own-prefix' else root, 'new', 'renamed', prefix='p')
        assert xml.bytes() == original and xml.revision == 0
    xml.set_attribute(root, 'new', 'a', 'value', prefix='fresh')
    xml.rename(child, 'new', 'renamed', prefix='fresh')
    assert dict(node(xml, root)['namespaces'])['p'] == 'old'
    assert dict(node(xml, child)['namespaces'])['p'] == 'old'
    xml.declare_namespace(root, 'p', 'intentional')
    assert dict(node(xml, root)['namespaces'])['p'] == 'intentional'
    assert dict(node(xml, child)['namespaces'])['p'] == 'old'


@pytest.mark.parametrize('source', [
    b'<r><a></r>', b'<r/><r/>', b'<r>', b'', b'text<r/>', b'<r/>text', b'<p:r/>', b'<:r/>', b'<a:b:c/>', b'<1bad/>',
    b'<r a="1"b="2"/>', b'<r a="<"/>', b'<r>]]></r>', b'<r xmlns:a="u" xmlns:b="u" a:x="1" b:x="2"/>',
    b'<r xmlns="u" xmlns="v"/>', b'<r xmlns:xml="wrong"/>', b'<r xmlns:xmlns="u"/>', b'<r xmlns:p=""/>',
    b'<r xmlns="http://www.w3.org/XML/1998/namespace"/>', b'<r xmlns:p="http://www.w3.org/2000/xmlns/"/>',
    b'<r>\x00</r>', b'<r>&#0;</r>', b'<r>&#xD800;</r>', b'<r>&#1114112;</r>', b'<r a="&#xDFFF;"/>', b'<r>&dangling</r>',
    b'<r><!--bad--comment--></r>', b'<r><!--bad---></r>', b'<r>&undefined;</r>', b'<!DOCTYPE r><r/>',
    b'<!DOCTYPE r SYSTEM "https://example.invalid/external"><r/>', b'<!DOCTYPE r [<!ENTITY e "value">]><r>&e;</r>',
    b'<?XML data?><r/>', b'<r><?p?data?></r>', b'<?xml\x0bversion="1.0"?><r/>', b'<?xml version="1.1"?><r/>',
    b'<?xml versionfoo="1.0"?><r/>', b'<?xml version="1.0" standalone="perhaps"?><r/>', b' <?xml version="1.0"?><r/>',
    b'<?xml version="1.0" version="1.0"?><r/>', b'<?xml version="1.0" encoding="UTF-16"?><r/>',
    b'<?xml version="1.0" encoding="latin1"?><r/>', b'\xff\xfe<', b'\xff\xfe\x00\xd8', b'<r>\xff</r>',
])
def test_invalid_xml_is_rejected(source):
    with pytest.raises(ValueError): Xml(source)


def test_literal_markup_and_xml_normalization():
    source = b'<?xml\nversion="1.0"?><r a="a\r\nb&#13;c"><!--&#xD800; <!DOCTYPE--><![CDATA[&#xD800; <!DOCTYPE]]><?p &#xD800;?></r>'
    xml = Xml(source)
    assert xml.bytes() == source
    assert rows(xml)[0]['text'] == '&#xD800; <!DOCTYPE'
    assert rows(xml)[0]['attributes'] == [['', 'a', 'a b\rc']]
    assert fromstring(b'<r xmlns:p="urn:extension" p:xmlns="ordinary attribute"/>').attrib == {'{urn:extension}xmlns': 'ordinary attribute'}
    assert rows(Xml(b'<r xmlns:p="urn:extension" p:xmlns="ordinary attribute"/>'))[0]['attributes'][0][:2] == ['urn:extension', 'xmlns']


def test_processing_instruction_payload_whitespace_and_unicode_names():
    xml = Xml('<é:根 xmlns:é="urn:名" é:属="值"><?p  data\r\nmore?></é:根>'.encode())
    assert rows(xml)[0]['qname'] == ['urn:名', '根']
    pi, = xml.children(xml.root)
    assert node(xml, pi)['text'] == ' data\nmore'
    comment = xml.insert_comment(xml.root, 1, 'a\r\nb\rc')
    added = xml.insert_pi(xml.root, 2, 'p', ' leading\r\nspace')
    assert node(xml, comment)['text'] == 'a\nb\nc'
    assert node(xml, added)['text'] == ' leading\nspace'
    assert rows(Xml(xml.bytes()))[0]['content'] == rows(xml)[0]['content']


def test_resource_limits_apply_to_inputs_and_structural_edits():
    with pytest.raises(ValueError, match='depth limit'): Xml(b'<r>' * 257 + b'</r>' * 257)
    with pytest.raises(ValueError, match='1 MiB'): Xml(b'<r a="' + b'x' * (1024 * 1024 + 1) + b'"/>')
    xml = Xml(b'<r>' * 256 + b'</r>' * 256)
    original = xml.bytes()
    deepest = elements(xml)[-1]
    with pytest.raises(ValueError, match='depth limit'): xml.insert_xml(deepest, 0, b'<too-deep/>')
    assert xml.bytes() == original and xml.revision == 0
    xml.replace_node(xml.root, original)  # Fragment's internal wrapper does not consume the user's depth budget.
    assert len(elements(xml)) == 256


def test_structural_preflight_preserves_state_and_copy_into_descendant_is_bounded():
    xml = Xml(b'<r><a><b/></a><c/></r>')
    root, a, b, c = elements(xml)
    copied = xml.copy(a, b, 0)
    assert xml.children(copied) and xml.children(xml.children(copied)[0]) == []
    source = xml.bytes()
    with pytest.raises(ValueError): xml.replace_node(root, b'<replacement/>invalid-root-text')
    with pytest.raises(IndexError): xml.copy(a, c, 1)
    with pytest.raises(ValueError): xml.move_node(a, copied, 0)
    assert xml.bytes() == source and xml.revision == 1
    xml = Xml(b'<r><a/>' + b'<d>' * 255 + b'</d>' * 255 + b'</r>')
    a, deepest = elements(xml)[1], elements(xml)[-1]
    for edit in (xml.copy, xml.move_node):
        with pytest.raises(ValueError, match='depth limit'): edit(a, deepest, 0)
    assert xml.revision == 0


def test_serialized_size_limit_is_deferred_until_bytes_and_allows_recovery():
    xml = Xml(b'<r/>')
    xml.set_text(xml.root, '&' * (7 * 1024 * 1024))
    assert xml.revision == 1  # Escaping exceeds the output budget, but ordinary setters do not serialize.
    with pytest.raises(ValueError, match='serialized limit'): xml.bytes()
    xml.set_text(xml.root, 'recovered')
    assert xml.bytes() == b'<r>recovered</r>' and xml.revision == 2
