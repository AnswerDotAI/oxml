"""Explicit native style definitions and references, without a formatting cascade."""
from . import _core
from .build import _bytes
from .model import Tree

class Styles:
    "Live styles indexed by exact styleId; find() uses the stored display name."
    def __init__(self, doc): self.doc = doc
    def __iter__(self): return (Style(self, s) for s in _core.style_items(self.doc.package._native))
    def __getitem__(self, style_id): return Style(self, _core.style_get(self.doc.package._native, style_id))
    def find(self, name, kind=None):
        result = _core.style_find(self.doc.package._native, name, kind)
        return None if result is None else Style(self, result)
    def add(self, style_id, *, name=None, kind='paragraph', based_on=None, paragraph=(), run=()):
        return Style(self, _core.style_add(self.doc.package._native, style_id, name, kind, based_on,
                                           [_bytes(p) for p in paragraph], [_bytes(r) for r in run]))

class Style:
    def __init__(self, styles, native): self.styles, self._native = styles, native
    @property
    def element(self): return Tree._from_native(self._native.xml)._element(self._native.node_id)
    @property
    def id(self): return self._native.id
    @property
    def kind(self): return self._native.kind
    @property
    def name(self): return self._native.name
    def apply(self, element):
        self._native.apply(element._tree.xml, element.node_id)
        return element
