"""Independent XML preservation oracle for curated packages; no layout or validity claim."""
from xml.dom import Node, minidom
from io import BytesIO
from zipfile import ZipFile
from oxml import Document

def parts(doc):
    with ZipFile(BytesIO(doc.bytes())) as package: return {name: package.read(name) for name in package.namelist()}

def xml_snapshot(xml):
    """Compare expanded names, attributes, effective namespaces and ordered content, not XML formatting."""
    def snapshot(node, namespaces):
        if node.nodeType in (Node.TEXT_NODE, Node.CDATA_SECTION_NODE): return ('text', node.data)
        if node.nodeType == Node.COMMENT_NODE: return ('comment', node.data)
        if node.nodeType == Node.PROCESSING_INSTRUCTION_NODE: return ('pi', node.target, node.data)
        attrs, bindings = [], dict(namespaces)
        if node.nodeType == Node.ELEMENT_NODE:
            for attr in node.attributes.values():
                if attr.namespaceURI == 'http://www.w3.org/2000/xmlns/':
                    bindings['' if attr.name == 'xmlns' else attr.localName] = attr.value
                else: attrs.append((attr.namespaceURI or '', attr.localName, attr.value))
        children = []
        for child in node.childNodes:
            value = snapshot(child, bindings)
            if children and value[0] == children[-1][0] == 'text': children[-1] = ('text', children[-1][1] + value[1])
            else: children.append(value)
        name = (node.namespaceURI or '', node.localName) if node.nodeType == Node.ELEMENT_NODE else ('', '#document')
        return (name, tuple(sorted(attrs)), tuple(sorted(bindings.items())), tuple(children))
    root = minidom.parseString(xml) if isinstance(xml, (bytes, str)) else xml
    return snapshot(root, {'xml': 'http://www.w3.org/XML/1998/namespace'})

def open_original(path, *parts):
    doc = Document.open(path)
    doc.main.xml.root
    for part in parts: doc.package.part('/'+part).xml.root
    assert doc.bytes() == path.read_bytes()
    with ZipFile(path) as package: expected = {p: minidom.parseString(package.read(p)) for p in parts}
    return doc, expected

def saved_edit(doc, original, output, expected):
    """Compare changed parts to independent DOMs and retain every other ZIP payload."""
    doc.save(output)
    with ZipFile(original) as before, ZipFile(output) as after:
        assert before.namelist() == after.namelist()
        for name in before.namelist():
            if name in expected: assert xml_snapshot(after.read(name)) == xml_snapshot(expected[name])
            else: assert after.read(name) == before.read(name)
    reopened = Document.open(output)
    for part, dom in expected.items(): assert xml_snapshot(reopened.package.part('/'+part).xml.bytes()) == xml_snapshot(dom)
    assert reopened.bytes() == output.read_bytes()
    return reopened
