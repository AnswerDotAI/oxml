"""Namespace-bound XML construction and attachment to live document trees."""
from fastcore.xml import E as _E, XML as _XML
from .model import Element, metadata

class _Snapshot(_XML):
    def __init__(self, element): self._data = element._tree.xml.subtree_bytes(element.node_id).decode('utf-8')
    def _render(self, inherited, output): output.append(self._data)

class XML(_XML):
    "A detached expression that accepts snapshots of live elements."
    def _child(self, child):
        return _Snapshot(child) if isinstance(child, Element) else super()._child(child)

class E(_E):
    "Create an XML factory with the SDK's namespace prefixes."
    _node_cls = XML

    def _namespace(self, prefix):
        if prefix not in self._ns and prefix in metadata['namespaces']: return metadata['namespaces'][prefix]
        return super()._namespace(prefix)

e = E('w', attr_ns='w')
