"""Native structural operations on rectangular Word tables."""
from . import _core
from .model import Tree

class Table:
    "Live table rows/cells. Use Story(cell) for text; column edits affect every row."
    def __init__(self, element): self._native = _core.Table(element._tree.xml, element.node_id)
    @classmethod
    def add(cls, parent, values, widths, index=None):
        "Insert a rectangular text table; column widths are positive integer twips."
        result = cls.__new__(cls)
        result._native = _core.Table.add(parent._tree.xml, parent.node_id, [list(r) for r in values], list(widths), index)
        return result
    def _element(self, ident): return Tree._from_native(self._native.xml)._element(ident)
    @property
    def element(self): return self._element(self._native.node_id)
    @property
    def rows(self): return [self._element(i) for i in self._native.rows]
    def cells(self, row): return [self._element(i) for i in self._native.cells(row)]
    def insert_row(self, index, values=None):
        return self._element(self._native.insert_row(index, None if values is None else list(values)))
    def delete_row(self, index): self._native.delete_row(index)
    def insert_column(self, index): return [self._element(i) for i in self._native.insert_column(index)]
    def delete_column(self, index): self._native.delete_column(index)
