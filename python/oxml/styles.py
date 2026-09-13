"""Explicit style definitions/references, without computing a formatting cascade."""
from ._core import Xml
from .build import E
from .model import metadata, _expanded, _one, _choose_prefix

_W = metadata['namespaces']['w']
_TARGETS = {'paragraph': ('p', 'pPr', 'pStyle'), 'character': ('r', 'rPr', 'rStyle'), 'table': ('tbl', 'tblPr', 'tblStyle')}

def _child(element, local):
    return _one((e for e in element.children if e.qname == (_W, local)), local)

def _particle_names(particle):
    if 'Name' in particle: yield _expanded(particle['Name'].rsplit('/', 1)[-1])
    for item in particle.get('Items', []): yield from _particle_names(item)

def _position(parent, local):
    """Insert a known property/definition in imported schema order; never reorder siblings."""
    particle = metadata['types'].get(parent.type_id, {}).get('particle') or {}
    order = {name: index for index, name in enumerate(dict.fromkeys(_particle_names(particle)))}
    if (_W, local) not in order: raise ValueError(f'No declared position for w:{local}')
    xml = parent._tree.xml
    return next((xml.children(parent.node_id).index(e.node_id) for e in parent.children
                 if order.get(e.qname, -1) > order[(_W, local)]), xml.child_count(parent.node_id))

def _ensure(parent, local):
    child = _child(parent, local)
    return child if child is not None else E('w:'+local).append_to(parent, _position(parent, local))

def _attribute(element, local, value):
    value = str(value)
    if element.attribute(_W, local) == value: return
    bindings = dict(element.raw['namespaces'])
    prefix = _choose_prefix(bindings, _W, 'w')
    element._tree.xml.set_attribute(element.node_id, _W, local, value, prefix)

def _value(parent, local, value): _attribute(_ensure(parent, local), 'val', value)

class Styles:
    """Live styles indexed by exact styleId; find() uses the stored display name."""
    def __init__(self, doc): self.doc = doc

    def _root(self, create=False):
        part = self.doc._part('StyleDefinitionsPart', create)
        return part.xml.root if part is not None else None

    def __iter__(self):
        root = self._root()
        if root is not None:
            for element in root.children:
                if element.qname == (_W, 'style'): yield Style(self, element)

    def __getitem__(self, style_id):
        style = _one((s for s in self if s.id == style_id), 'style ID')
        if style is None: raise KeyError(style_id)
        return style

    def find(self, name, kind=None):
        return _one((s for s in self if s.name == name and (kind is None or s.kind == kind)), 'style name')

    def add(self, style_id, *, name=None, kind='paragraph', based_on=None, paragraph=(), run=()):
        """Create a style; paragraph/run are E or Element property children, not computed formatting."""
        if not isinstance(style_id, str) or not style_id: raise ValueError('style_id requires a nonempty string')
        if name is not None and not isinstance(name, str): raise TypeError('Style name requires str')
        if kind not in _TARGETS: raise ValueError('Style kind must be paragraph, character or table')
        if any(s.id == style_id for s in self): raise ValueError('Style ID already exists')
        if based_on is not None and self[based_on].kind != kind: raise ValueError('Base style must have the same kind')
        paragraph, run = tuple(paragraph), tuple(run)
        if kind == 'character' and paragraph: raise ValueError('Character styles cannot have paragraph properties')
        expression = E('w:style', E('w:name', attrs={'w:val': name if name is not None else style_id}),
                       E('w:basedOn', attrs={'w:val': based_on}) if based_on is not None else None,
                       E('w:pPr', *paragraph) if paragraph else None, E('w:rPr', *run) if run else None,
                       attrs={'w:type': kind, 'w:styleId': style_id, 'w:customStyle': '1'})
        Xml(expression.bytes())
        root = self._root(True)
        return Style(self, expression.append_to(root, _position(root, 'style')))

class Style:
    def __init__(self, styles, element): self.styles, self.element = styles, element
    @property
    def id(self): return self.element.attribute(_W, 'styleId')
    @property
    def kind(self): return self.element.attribute(_W, 'type') or 'paragraph'
    @property
    def name(self):
        name = _child(self.element, 'name')
        return name.attribute(_W, 'val') if name is not None else None

    def apply(self, element):
        """Set only the style reference; preserve direct formatting and other properties."""
        self.styles.doc.package._owner(element)
        if self.kind not in _TARGETS: raise NotImplementedError('This style kind cannot be applied directly')
        target, properties, reference = _TARGETS[self.kind]
        if element.qname != (_W, target): raise ValueError(f'{self.kind} style requires a w:{target}')
        if not self.id: raise ValueError('Style has no styleId')
        _value(_ensure(element, properties), reference, self.id)
        return element
