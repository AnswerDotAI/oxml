"""Explicit numbering definitions and instances; counters and labels are rendered by Office."""
from dataclasses import dataclass
from ._core import Xml
from .build import e
from .model import w, _one, _position
from .styles import _W, _child, _ensure, _attribute, _value

def _integer(value, name, maximum=2147483647):
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= maximum:
        raise ValueError(f'{name} must be an integer between 0 and {maximum}')

def _find(root, local, attr, value):
    return _one((e for e in root.children if e.qname == (_W, local) and int(e.attribute(_W, attr)) == value), local+' ID')

def _next_id(root, local, attr, start=0):
    used = {int(value) for e in root.children if e.qname == (_W, local) and (value := e.attribute(_W, attr)) is not None}
    while start in used: start += 1
    _integer(start, attr)
    return start

@dataclass
class Level:
    """One level: twip indents; restart is OOXML's one-based earlier level (0 means never)."""
    format: str = 'decimal'
    text: str | None = None
    start: int = 1
    indent: int | None = None
    hanging: int = 360
    restart: int | None = None

    def _xml(self, index):
        format = self.format.value if isinstance(self.format, w.NumberFormatValues) else self.format
        if format not in {v.value for v in w.NumberFormatValues}: raise ValueError('Unknown numbering format')
        text = self.text if self.text is not None else ('•' if format == 'bullet' else f'%{index+1}.')
        if not isinstance(text, str): raise TypeError('Level text requires str')
        indent = self.indent if self.indent is not None else 720*(index+1)
        for name, value in [('start', self.start), ('indent', indent), ('hanging', self.hanging)]: _integer(value, name)
        if self.restart is not None: _integer(self.restart, 'restart', index)
        return e.lvl(e.start(val=self.start), e.numFmt(val=format),
                     e.lvlRestart(val=self.restart) if self.restart is not None else None,
                     e.lvlText(val=text), e.lvlJc(val='left'),
                     e.pPr(e.tabs(e.tab(val='num', pos=indent)), e.ind(left=indent, hanging=self.hanging)), ilvl=index)

class Numbering:
    """Instances indexed by numId; reuse an instance to continue the same list."""
    def __init__(self, doc): self.doc = doc

    def _root(self, create=False):
        part = self.doc._part('NumberingDefinitionsPart', create)
        return part.xml.root if part is not None else None

    def __iter__(self):
        root = self._root()
        if root is not None:
            for e in root.children:
                if e.qname == (_W, 'num'): yield NumberingInstance(self, e)

    def __getitem__(self, ident):
        root = self._root()
        element = _find(root, 'num', 'numId', int(ident)) if root is not None else None
        if element is None: raise KeyError(ident)
        return NumberingInstance(self, element)

    def add(self, levels):
        """Create one abstract definition and a concrete instance for 1–9 Level values."""
        levels = list(levels)
        if not 1 <= len(levels) <= 9 or not all(isinstance(level, Level) for level in levels):
            raise ValueError('Numbering needs 1–9 Level values')
        root = self._root()
        abstract_id = _next_id(root, 'abstractNum', 'abstractNumId') if root is not None else 0
        num_id = _next_id(root, 'num', 'numId', 1) if root is not None else 1
        abstract = e.abstractNum(e.multiLevelType(val='singleLevel' if len(levels) == 1 else 'multilevel'),
                                 *(level._xml(index) for index, level in enumerate(levels)), abstractNumId=abstract_id)
        Xml(abstract.bytes())
        root = self._root(True)
        root(abstract)
        element = root(e.num(e.abstractNumId(val=abstract_id), numId=num_id))
        return NumberingInstance(self, element)

class NumberingInstance:
    def __init__(self, numbering, element): self.numbering, self.element = numbering, element
    @property
    def id(self): return int(self.element.attribute(_W, 'numId'))
    @property
    def definition(self):
        reference = _child(self.element, 'abstractNumId')
        if reference is None: raise ValueError('Numbering instance has no abstractNumId')
        definition = _find(self.numbering._root(), 'abstractNum', 'abstractNumId', int(reference.attribute(_W, 'val')))
        if definition is None: raise ValueError('Abstract numbering definition does not exist')
        return definition

    def _level(self, level):
        _integer(level, 'level', 8)
        override = _find(self.element, 'lvlOverride', 'ilvl', level)
        if override is not None and _child(override, 'lvl') is not None: return
        if _find(self.definition, 'lvl', 'ilvl', level) is not None: return
        if _child(self.definition, 'numStyleLink') is not None: raise NotImplementedError('Linked numbering styles are not resolved')
        raise ValueError('Level is not defined by this numbering instance')

    def apply(self, paragraph, level=0):
        """Continue this instance at the chosen zero-based level, retaining direct formatting."""
        self.numbering.doc.package._owner(paragraph)
        if paragraph.qname != (_W, 'p'): raise ValueError('Numbering requires a paragraph')
        self._level(level)
        ident = self.id
        properties = _child(paragraph, 'pPr')
        if properties is not None: properties = _child(properties, 'numPr')
        if properties is not None:
            for local in ('ilvl', 'numId'): _child(properties, local)
        properties = _ensure(_ensure(paragraph, 'pPr'), 'numPr')
        _value(properties, 'ilvl', level)
        _value(properties, 'numId', ident)
        return paragraph

    def restart(self, start=1, level=0):
        """Return a fresh list instance with a level start override; leave the original list unchanged."""
        self._level(level)
        _integer(start, 'start')
        override = _find(self.element, 'lvlOverride', 'ilvl', level)
        if override is not None: _child(override, 'startOverride')
        root = self.numbering._root()
        ident = _next_id(root, 'num', 'numId', 1)
        durable = _next_id(root, 'num', 'durableId', 1) if self.element.attribute(_W, 'durableId') is not None else None
        element = self.element.copy_to(root, _position(root, self.element.qname))
        _attribute(element, 'numId', ident)
        if durable is not None: _attribute(element, 'durableId', durable)
        override = _find(element, 'lvlOverride', 'ilvl', level)
        if override is None: override = element(e.lvlOverride(ilvl=level))
        _value(override, 'startOverride', start)
        return NumberingInstance(self.numbering, element)
