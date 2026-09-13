"""Small structural operations on rectangular Word tables; merged/revised grids are refused."""
from .build import e
from .model import Element, _walk, metadata
from .text import _name, _run_text, _shell
from .revisions import _revision_name

def _children(element, name): return [c for c in element.children if _name(c) == name]

def _cell(template, text=''):
    if not isinstance(text, str): raise TypeError('Cell values require str')
    paragraphs = [] if template is None else _children(template, 'p')
    paragraph = paragraphs[0] if paragraphs else None
    runs = [] if paragraph is None else _children(paragraph, 'r')
    properties = [] if paragraph is None else _children(paragraph, 'pPr')
    content = [e.p(*properties, _run_text(runs[0] if runs else None, line)) for line in text.split('\n')]
    return e.tc(*([] if template is None else _children(template, 'tcPr')), *content)

class Table:
    """Live table rows/cells. Use Story(cell) for text; column edits affect every row.

    New rows/cells copy adjacent properties, not content or paragraph IDs. No merged
    cells, tracked table changes, layout calculation or implicit review cleanup.
    """
    def __init__(self, element):
        if not isinstance(element, Element) or _name(element) != 'tbl': raise TypeError('Table requires a live w:tbl Element')
        self.element = element

    @classmethod
    def add(cls, parent, values, widths, index=None):
        """Insert a rectangular text table; column widths are positive integer twips."""
        if _name(parent) not in {'body', 'hdr', 'ftr', 'footnote', 'endnote', 'comment', 'tc'}:
            raise ValueError('Expected a block-content container')
        widths, values = list(widths), [list(row) for row in values]
        if not widths or any(type(v) is not int or v <= 0 for v in widths): raise ValueError('Widths require positive integer twips')
        if not values or any(len(row) != len(widths) for row in values): raise ValueError('Rows must match the nonempty column grid')
        expression = e.tbl(e.tblPr(), e.tblGrid(e.gridCol(w=v) for v in widths),
                           (e.tr(_cell(None, text) for text in row) for row in values))
        if index is None and _name(parent) == 'tc' and parent.children and _name(parent.children[-1]) == 'p':
            index = parent._tree.xml.children(parent.node_id).index(parent.children[-1].node_id)
        return cls(parent(expression, index=index))

    @property
    def rows(self): return _children(self.element, 'tr')

    def cells(self, row): return _children(self.rows[row], 'tc')

    def _grid(self):
        grids, rows = _children(self.element, 'tblGrid'), self.rows
        if len(grids) != 1 or not rows: raise NotImplementedError('Table needs one grid and at least one row')
        columns = grids[0].children
        if not columns or any(_name(c) != 'gridCol' for c in columns): raise NotImplementedError('Unsupported table grid')
        if any(_name(c) not in {'tblPr', 'tblGrid', 'tr'} for c in self.element.children):
            raise NotImplementedError('Table contains unsupported structural children')
        for row in rows:
            cells = _children(row, 'tc')
            if len(cells) != len(columns) or any(_name(c) not in {'trPr', 'tc'} for c in row.children):
                raise NotImplementedError('Only rectangular tables are supported')
            properties = _children(row, 'trPr') + [p for c in cells for p in _children(c, 'tcPr')]
            for prop in properties:
                if any(_name(e) in {'gridSpan', 'vMerge', 'hMerge', 'gridBefore', 'gridAfter'} for e in prop.children):
                    raise NotImplementedError('Merged or offset cells require explicit grid editing')
        if any(_revision_name(e) for e in _walk(self.element)): raise NotImplementedError('Revised tables require explicit acceptance/rejection first')
        return grids[0], rows

    def insert_row(self, index, values=None):
        """Insert a row, copying the adjacent row/cell formatting."""
        grid, rows = self._grid()
        if not 0 <= index <= len(rows): raise IndexError(index)
        template = rows[min(index, len(rows)-1)]
        cells = _children(template, 'tc')
        values = ['']*len(cells) if values is None else list(values)
        if len(values) != len(cells): raise ValueError('Row values must match the column grid')
        expression = _shell(template, *_children(template, 'trPr'), *(_cell(c, text) for c, text in zip(cells, values)))
        xml = self.element._tree.xml
        position = xml.children(self.element.node_id).index(template.node_id) + (index == len(rows))
        row = self.element(expression, index=position)
        for local in ('paraId', 'textId'): row.remove_attribute(metadata['namespaces']['w14'], local)
        return row

    def delete_row(self, index):
        _, rows = self._grid()
        if len(rows) == 1: raise ValueError('Delete the table element to remove its last row')
        self._check_removal([rows[index]])
        rows[index].delete()

    def insert_column(self, index):
        """Insert a blank column using the adjacent column's width and cell properties."""
        grid, rows = self._grid()
        columns = grid.children
        if not 0 <= index <= len(columns): raise IndexError(index)
        adjacent = min(index, len(columns)-1)
        expressions = [_cell(_children(row, 'tc')[adjacent]) for row in rows]
        columns[adjacent].copy_to(grid, grid._tree.xml.children(grid.node_id).index(columns[adjacent].node_id)+(index == len(columns)))
        result = []
        for row, expression in zip(rows, expressions):
            cell = _children(row, 'tc')[adjacent]
            position = row._tree.xml.children(row.node_id).index(cell.node_id)+(index == len(columns))
            result.append(row(expression, index=position))
        return result

    def delete_column(self, index):
        grid, rows = self._grid()
        if len(grid.children) == 1: raise ValueError('Delete the table element to remove its last column')
        cells = [_children(row, 'tc')[index] for row in rows]
        column = grid.children[index]
        self._check_removal(cells)
        for cell in cells: cell.delete()
        column.delete()

    @staticmethod
    def _check_removal(elements):
        markers = {'bookmarkStart', 'bookmarkEnd', 'commentRangeStart', 'commentRangeEnd', 'commentReference'}
        if any(_name(e) in markers for element in elements for e in _walk(element)):
            raise NotImplementedError('Remove or relocate bookmarks/comments before deleting their table cells')
