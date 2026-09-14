'Namespace-bound XML construction and attachment to live document trees.'
from fastcore.xml import E as _E, XML as _XML
from . import _core
from .model import Element, Tree, namespace_uris

def _bytes(value):
    if isinstance(value, Element): return value._tree.xml.subtree_bytes(value.node_id)
    if isinstance(value, _XML): return value.bytes()
    raise TypeError('Expected an XML expression or Element')

class _Snapshot(_XML):
    def __init__(self, element):
        uri, local = element.qname
        prefix = next(p for p, u in element.raw['namespaces'] if u == uri)
        self.tag, self.ns, self.attrs, self.children = (f'{prefix}:{local}' if prefix else local), {prefix: uri}, {}, ()
        self._data = element._tree.xml.subtree_bytes(element.node_id).decode('utf-8')
    def _render(self, inherited, output): output.append(self._data)

class XML(_XML):
    "A detached expression that accepts snapshots of live elements."
    def _child(self, child): return _Snapshot(child) if isinstance(child, Element) else super()._child(child)
    def bytes(self):
        "Serialize for insertion into a live tree, declaring every binding so unqualified names stay unqualified"
        return self.render({}).encode('utf-8')
_onoff = set(_core.on_off_attributes())


class E(_E):
    "Create an XML factory with the SDK's namespace prefixes; on/off-only `val` attributes take booleans as `on`/`off`"
    _node_cls = XML

    def _namespace(self, prefix):
        if prefix not in self._ns and prefix in namespace_uris: return namespace_uris[prefix]
        return super()._namespace(prefix)

    def attr_value(self, tag, name, value):
        if isinstance(value, bool) and (tag, name) in _onoff: return 'on' if value else 'off'
        return super().attr_value(tag, name, value)

e = E('w', attr_ns='w')

def field(instr, text, rpr=None):
    "A simple field with instruction `instr` and cached result `text`, formatted by run properties `rpr` and marked dirty so Word refreshes it"
    return Tree._from_native(_core.field(instr, text, None if rpr is None else _bytes(rpr))).root
