"""Direct run/paragraph property revisions, without computing inherited formatting.

PowerTools RevisionProcessor's property-change transforms retain current properties on
acceptance and restore the embedded snapshot on rejection. Paragraph-mark rPr and sectPr
are independent of the pPrChange snapshot and must survive paragraph-format rejection.
"""
from .build import XML, e
from .model import Element, Tree, _walk
from .text import _name, _inside
from .revisions import _context, _revision_attrs, _revision_name

_RETAIN = {'rPr', 'sectPr'}

def _copy(element): return Tree(element._tree.xml.subtree_bytes(element.node_id)).root

def _paragraph(properties):
    owner = properties.parent
    expected = {'p'} if _name(properties) == 'pPr' else {'r', 'pPr'}
    if owner is None or _name(owner) not in expected: raise NotImplementedError('Unsupported property revision location')
    if owner is not None and _name(owner) in {'r', 'pPr'}: owner = owner.parent
    if owner is None or _name(owner) != 'p': raise NotImplementedError('Expected properties of an ordinary run or paragraph')
    _context(owner)
    return owner

def _preflight_change(element):
    kind, current = _name(element), element.parent
    if kind not in {'rPrChange', 'pPrChange'} or current is None or _name(current) != kind[:-6]:
        raise NotImplementedError('Expected a run or paragraph property change')
    _paragraph(current)
    if len([c for c in current.parent.children if _name(c) == _name(current)]) != 1:
        raise NotImplementedError('Multiple current property blocks are ambiguous')
    previous = element.children
    if len(previous) != 1 or _name(previous[0]) != _name(current):
        raise NotImplementedError('Property change requires exactly one previous-properties snapshot')
    if any(_revision_name(e) and e.node_id != element.node_id for e in _walk(current)):
        raise NotImplementedError('Nested or conflicting property revision history is unsupported')
    if kind == 'pPrChange' and any(_name(c) in _RETAIN for c in previous[0].children):
        raise NotImplementedError('Paragraph history cannot replace paragraph-mark or section properties')
    return current, previous[0]

def _apply_change(element, accept):
    current, previous = _preflight_change(element)
    if accept:
        element.delete()
        return
    xml, parent = current._tree.xml, current.parent
    restored = previous.copy_to(parent, xml.children(parent.node_id).index(current.node_id))
    if _name(current) == 'pPr':
        for child in current.children:
            if _name(child) in _RETAIN: child.move_to(restored, xml.child_count(restored.node_id))
    current.delete()

def create(story, target, properties, *, author, date=None):
    """Replace direct properties, retaining old ones as a tracked snapshot.

    A run takes an rPr element/expression; a paragraph takes pPr base properties.
    Existing paragraph-mark rPr and sectPr are retained, not accepted as replacement
    input. Missing old properties are represented by the usual empty snapshot.
    """
    if not isinstance(target, Element): raise TypeError('Formatting target requires a live run or paragraph')
    expected = {'r': 'rPr', 'p': 'pPr'}.get(_name(target))
    if expected is None: raise ValueError('Formatting target requires a run or paragraph')
    _inside(story, target)
    paragraph = target if _name(target) == 'p' else target.parent
    if paragraph is None or _name(paragraph) != 'p': raise NotImplementedError('Formatting requires an ordinary paragraph')
    _context(paragraph)
    if not isinstance(properties, (XML, Element)): raise TypeError('Properties require an XML expression or parsed Element')
    replacement = Tree(e(_name(target), properties).bytes()).root.children[0]
    if _name(replacement) != expected: raise ValueError(f'Expected w:{expected} properties')
    if expected == 'pPr' and any(_name(c) in _RETAIN for c in replacement.children):
        raise ValueError('Supply paragraph base properties only; paragraph-mark and section properties are retained')
    existing = [c for c in target.children if _name(c) == expected]
    if len(existing) > 1: raise NotImplementedError('Multiple current property blocks are ambiguous')
    old = existing[0] if existing else None
    for properties in [replacement, *existing]:
        if any(_revision_name(e) for e in _walk(properties)):
            raise NotImplementedError('Nested or conflicting property revision history is unsupported')
    previous = _copy(old) if old is not None else Tree(e(expected).bytes()).root
    if expected == 'pPr':
        for child in previous.children:
            if _name(child) in _RETAIN: child.delete()
        if old is not None:
            for child in old.children:
                if _name(child) in _RETAIN: child.copy_to(replacement)
    kind = expected+'Change'
    replacement(e(kind, previous, attrs_=next(_revision_attrs(target._tree, author, date))))
    xml = target._tree.xml
    index = xml.children(target.node_id).index(old.node_id) if old is not None else 0
    current = replacement.copy_to(target, index)
    if old is not None: old.delete()
    return next(c for c in current.children if _name(c) == kind)
