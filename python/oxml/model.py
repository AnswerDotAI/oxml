"""SDK-derived typed views over the internal mutable XML editor.

All nominal element classes and typed properties below come from the same imported
metadata used by the native validator. Stable views share one native XML state.
"""
import json
import re
from enum import Enum
from types import MappingProxyType, SimpleNamespace
from . import _core

metadata = json.loads(_core.metadata_json())
namespaces = {prefix: SimpleNamespace() for prefix in metadata['namespaces']}
types, enums = {}, {}

def _snake(name): return re.sub(r'(?<!^)(?=[A-Z][a-z])|(?<=[a-z0-9])(?=[A-Z])', '_', name).lower()

def _expanded(qname):
    prefix, _, local = qname.rpartition(':')
    return metadata['namespaces'].get(prefix, ''), local

def _walk(element):
    yield element
    for child in element.children: yield from _walk(child)

def _one(items, description):
    items = list(items)
    if len(items) > 1: raise ValueError(f'Ambiguous {description}')
    return items[0] if items else None

def _choose_prefix(bindings, uri, preferred):
    chosen = next((p for p, value in bindings.items() if p and value == uri), preferred)
    while chosen in bindings and bindings[chosen] != uri: chosen += '_'
    return chosen

def _freeze(value):
    if isinstance(value, dict): return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list): return tuple(_freeze(v) for v in value)
    return value

class Element:
    """A contextual typed view over a stable native element identity."""
    __slots__ = ('_tree', '_id', '_checked_revision')
    type_id = None

    def __init__(self, tree, node_id): self._tree, self._id, self._checked_revision = tree, node_id, None

    def _check(self):
        revision = self._tree.xml.revision
        if self._checked_revision == revision: return
        if self.type_id != _core.element_type(self._tree.xml, self._id):
            raise ReferenceError('XML element type changed; reacquire its typed view')
        self._checked_revision = revision

    @property
    def node_id(self):
        self._check()
        return self._id

    @property
    def raw(self):
        self._check()
        return _freeze(json.loads(self._tree.xml.node(self._id)))

    @property
    def qname(self): return self._tree.xml.qname(self.node_id)

    @property
    def children(self): return [self._tree._element(i) for i in self._tree.xml.element_children(self.node_id)]

    @property
    def parent(self):
        parent = self._tree.xml.parent(self.node_id)
        return None if parent is None else self._tree._element(parent)

    @property
    def text(self): return self.raw['text']

    def attribute(self, uri, local): return self._tree.xml.attribute(self.node_id, uri, local)

    def set_attribute(self, uri, local, value):
        self._check()
        self._tree.xml.set_attribute(self._id, uri, local, value)

    def remove_attribute(self, uri, local): self._tree.xml.remove_attribute(self.node_id, uri, local)

    def insert_xml(self, index, data):
        ids = self._tree.xml.insert_xml(self.node_id, index, data)
        return [self._tree._element(i) for i in ids if json.loads(self._tree.xml.node(i))['kind'] == 'element']

    def append_xml(self, data): return self.insert_xml(self._tree.xml.child_count(self.node_id), data)

    def delete(self): self._tree.xml.delete(self.node_id)

    def replace(self, data):
        ids = self._tree.xml.replace_node(self.node_id, data)
        return [self._tree._element(i) for i in ids if json.loads(self._tree.xml.node(i))['kind'] == 'element']

    def move_to(self, parent, index):
        if parent._tree is not self._tree: raise ValueError('Cross-part movement is not supported')
        self._tree.xml.move_node(self.node_id, parent.node_id, index)

    def copy_to(self, parent, index=None):
        """Copy XML only; package relationships and document-wide IDs are not remapped."""
        if index is None: index = parent._tree.xml.child_count(parent.node_id)
        if parent._tree is not self._tree:
            copied, = parent.insert_xml(index, self._tree.xml.subtree_bytes(self.node_id))
            return copied
        return self._tree._element(self._tree.xml.copy(self.node_id, parent.node_id, index))

    def __repr__(self): return f'{type(self).__name__}(id={self._id})'

def _value_property():
    def get(self) -> str: return self.text
    def set(self, value: str):
        if not isinstance(value, str): raise TypeError('text value requires str')
        self._check()
        self._tree.xml.set_text(self._id, value)
    return property(get, set)

def _property(attr, value_type):
    uri, local = _expanded(attr['QName'])
    def get(self):
        value = self.attribute(uri, local)
        if value is None: return None
        check = json.loads(_core.check_attribute(self.type_id, attr['PropertyName'], value))
        if check['errors']: raise ValueError(f'{type(self).__name__}.{attr["PropertyName"]}: {value!r}: {check["errors"]}')
        if value_type is bool: return check['value']
        return value_type(value)
    def set(self, value):
        if not isinstance(value, value_type) or value_type is int and isinstance(value, bool): raise TypeError(f'expected {value_type.__name__}')
        lexical = value.value if isinstance(value, Enum) else ('true' if value else 'false') if value_type is bool else str(value)
        check = json.loads(_core.check_attribute(self.type_id, attr['PropertyName'], lexical))
        if check['errors']: raise ValueError(f'{attr["PropertyName"]}: {check["errors"]}')
        if check['gaps']: raise NotImplementedError(f'Unchecked setter constraints: {check["gaps"]}')
        self.set_attribute(uri, local, lexical)
    get.__annotations__ = {'return': value_type | None}
    set.__annotations__ = {'value': value_type, 'return': None}
    return property(get, set, doc=attr.get('PropertyComments', attr['QName']))

def _attribute_type(attr):
    kind = attr['Type']
    if kind in ('BooleanValue', 'OnOffValue'): return bool
    if kind in ('Int16Value', 'Int32Value', 'Int64Value', 'IntegerValue', 'UInt16Value', 'UInt32Value', 'UInt64Value', 'ByteValue', 'SByteValue'): return int
    if kind.startswith('EnumValue<'): return enums[kind[len('EnumValue<'):-1]]
    return str

for _id, _enum in metadata['enums'].items():
    _members = {f.get('Name') or f['Value']: f['Value'] for f in _enum['Facets']}
    _cls = Enum(_enum['Name'], _members, type=str)
    enums[_enum['full_name']] = _cls
    _prefix = _enum['Type'].split(':')[0]
    if _prefix in namespaces: setattr(namespaces[_prefix], _enum['Name'], _cls)

for _id, _type in metadata['types'].items():
    if _type['is_abstract']: continue
    _attrs = {_snake(a['PropertyName']): _property(a, _attribute_type(a)) for a in _type['attributes']}
    _attrs.update(type_id=_id, __module__=__name__, __slots__=(),
                  __annotations__={_snake(a['PropertyName']): _attribute_type(a) | None for a in _type['attributes']})
    if _type['is_text']: _attrs['value'] = _value_property()
    _cls = types[_id] = type(_type['class_name'], (Element,), _attrs)
    _prefix = _id.rsplit('/', 1)[-1].split(':')[0]
    if _prefix in namespaces: setattr(namespaces[_prefix], _type['class_name'], _cls)

w = namespaces['w']

class Tree:
    """A mutable XML part. Raw edits and typed views share the native `xml` editor."""
    def __init__(self, data: bytes):
        self.xml = _core.Xml(data)

    def _element(self, index):
        element = types.get(_core.element_type(self.xml, index), Element)(self, index)
        element._checked_revision = self.xml.revision
        return element

    @property
    def root(self): return self._element(self.xml.root)

    def elements(self, cls=Element):
        for index in self.xml.element_ids():
            element = self._element(index)
            if isinstance(element, cls): yield element

    def validate(self, target='Microsoft365', dependencies=None, part_uri='', *, relationships=None, complete_dependencies=False):
        """Validate with optional part-name → live Tree dependencies."""
        native = {name: tree.xml for name, tree in (dependencies or {}).items()}
        report = json.loads(_core.analyze(self.xml, target, native, relationships, complete_dependencies))
        for issue in report['issues']: issue.update(part_uri=part_uri, target=target)
        return report

    def bytes(self): return self.xml.bytes()
