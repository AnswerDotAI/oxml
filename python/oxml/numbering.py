"""Native numbering definitions and instances; Office renders counters and labels."""
from . import _core
from ._core import Level
from .model import Tree

class Numbering:
    "Instances indexed by numId; reuse an instance to continue the same list."
    def __init__(self, doc): self.doc = doc
    def __iter__(self): return (NumberingInstance(self, n) for n in _core.numbering_items(self.doc.package._native))
    def __getitem__(self, ident): return NumberingInstance(self, _core.numbering_get(self.doc.package._native, int(ident)))
    def add(self, levels): return NumberingInstance(self, _core.numbering_add(self.doc.package._native, list(levels)))

class NumberingInstance:
    def __init__(self, numbering, native): self.numbering, self._native = numbering, native
    @property
    def element(self): return Tree._from_native(self._native.xml)._element(self._native.node_id)
    @property
    def id(self): return self._native.id
    @property
    def definition(self): return Tree._from_native(self._native.xml)._element(self._native.definition)
    def apply(self, paragraph, level=0):
        self._native.apply(paragraph._tree.xml, paragraph.node_id, level)
        return paragraph
    def restart(self, start=1, level=0): return NumberingInstance(self.numbering, self._native.restart(start, level))
