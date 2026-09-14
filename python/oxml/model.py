'Thin Python views and nominal classes over the native XML and schema model.'
import json, re
from collections import Counter
from enum import Enum
from types import MappingProxyType, SimpleNamespace
from fastcore.xml import XML as _Expression
from . import _core

namespace_uris = _core.namespace_bindings()
_prefixes = {uri: prefix for prefix, uri in namespace_uris.items()}
namespaces = {prefix: SimpleNamespace() for prefix in namespace_uris}
types, enums = {}, {}

def _snake(name): return re.sub(r'(?<!^)(?=[A-Z][a-z])|(?<=[a-z0-9])(?=[A-Z])', '_', name).lower()

def _freeze(value):
    if isinstance(value, dict): return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list): return tuple(_freeze(v) for v in value)
    return value

class Element:
    "A nominal view of a native element; all views share the native tree's state."
    __slots__ = ('_tree', '_id')
    type_id = None

    def __init__(self, tree, node_id): self._tree, self._id = tree, node_id

    @property
    def node_id(self):
        _core.check_element_type(self._tree.xml, self._id, self.type_id)
        return self._id

    @property
    def raw(self):
        "Immutable snapshot of this one node: qname, attributes, namespace bindings and child ids"
        return _freeze(json.loads(self._tree.xml.node(self.node_id)))
    @property
    def qname(self): return self._tree.xml.qname(self.node_id)
    @property
    def children(self): return [self._tree._element(i) for i in self._tree.xml.element_children(self.node_id)]
    @property
    def parent(self):
        parent = self._tree.xml.parent(self.node_id)
        return None if parent is None else self._tree._element(parent)
    @property
    def text(self):
        "Text of the direct text-node children, without descendant text"
        return self._tree.xml.text(self.node_id)

    def attribute(self, uri, local): return self._tree.xml.attribute(self.node_id, uri, local)
    def set_attribute(self, uri, local, value, prefix=None):
        "Set an attribute; an unbound `uri` is declared with `prefix`, or else with the SDK's prefix for it"
        if prefix or not uri: return self._tree.xml.set_attribute(self.node_id, uri, local, value, prefix)
        self._tree.xml.set_attribute_ns(self.node_id, uri, local, value, _prefixes.get(uri, ''))
    def remove_attribute(self, uri, local): self._tree.xml.remove_attribute(self.node_id, uri, local)

    def insert_xml(self, index, data): return self._tree._elements(self._tree.xml.insert_xml(self.node_id, index, data))

    def append_xml(self, data): return self.insert_xml(self._tree.xml.child_count(self.node_id), data)

    def __call__(self, expression, *, index=None):
        "Attach an expression or copy an element; use schema order unless index is explicit."
        if not isinstance(expression, (_Expression, Element)): raise TypeError('Expected an XML expression or Element')
        if isinstance(expression, Element): ident = self._tree.xml.attach_element(self.node_id, expression._tree.xml, expression.node_id, index)
        else: ident = self._tree.xml.attach_xml(self.node_id, expression.bytes(), index)
        return self._tree._element(ident)

    def reorder(self, deep=False):
        "Sort the children into schema order, keeping their relative order within a slot; `deep` also sorts every descendant that can be ranked"
        _core.reorder_children(self._tree.xml, self.node_id, deep)

    def child(self, cls):
        "The direct child of type `cls`, or None; more than one is an error"
        ident = _core.child_of_type(self._tree.xml, self.node_id, cls.type_id)
        return None if ident is None else self._tree._element(ident)

    def delete(self): self._tree.xml.delete(self.node_id)
    def replace(self, data): return self._tree._elements(self._tree.xml.replace_node(self.node_id, data))

    def move_to(self, parent, index): self._tree.xml.move_to(self.node_id, parent._tree.xml, parent.node_id, index)

    def copy_to(self, parent, index=None):
        "Copy XML, without remapping package relationships or document-wide IDs."
        if index is None: index = parent._tree.xml.child_count(parent.node_id)
        return parent._tree._element(self._tree.xml.copy_to(self.node_id, parent._tree.xml, parent.node_id, index))

    @property
    def index(self): return self._tree.xml.position(self.node_id)[1]

    def elements(self, cls=None):
        "Typed views of descendants, like `Tree.elements` but within this element"
        return (self._tree._element(i) for i in _core.elements_of_type(self._tree.xml, cls and cls.type_id, self.node_id))

    def bytes(self):
        "Serialized subtree, declaring the namespaces it needs"
        return self._tree.xml.subtree_bytes(self.node_id)
    def __str__(self): return self.bytes().decode()
    def __repr__(self):
        return f'{type(self).__name__}(node_id={self.node_id})'


def _value_property():
    def get(self): return self.text
    def set(self, value): self._tree.xml.set_text(self.node_id, value)
    return property(get, set)


def _property(name, value_type):
    def get(self):
        value = _core.typed_attribute(self._tree.xml, self._id, self.type_id, name)
        if value is None: return None
        return value == 'true' if value_type is bool else value_type(value)
    def set(self, value):
        if not isinstance(value, value_type) or value_type is int and isinstance(value, bool): raise TypeError(f'expected {value_type.__name__}')
        lexical = value.value if isinstance(value, Enum) else ('true' if value else 'false') if value_type is bool else str(value)
        _core.set_typed_attribute(self._tree.xml, self._id, self.type_id, name, lexical)
    return property(get, set)


def _attribute_type(kind):
    if kind in ('BooleanValue', 'OnOffValue'): return bool
    if kind in ('Int16Value', 'Int32Value', 'Int64Value', 'IntegerValue', 'UInt16Value', 'UInt32Value', 'UInt64Value', 'ByteValue', 'SByteValue'): return int
    if kind.startswith('EnumValue<'): return enums[kind[len('EnumValue<'):-1]]
    return str

for _full_name, _prefix, _name, _members in _core.facade_enums():
    _cls = enums[_full_name] = Enum(_name, dict(_members), type=str)
    if _prefix in namespaces: setattr(namespaces[_prefix], _name, _cls)

for _id, _prefix, _name, _is_text, _properties in _core.facade_types():
    _attrs = {_snake(name): _property(name, _attribute_type(kind)) for name, kind in _properties}
    _attrs.update(type_id=_id, __module__=__name__, __slots__=())
    if _is_text: _attrs['value'] = _value_property()
    _cls = types[_id] = type(_name, (Element,), _attrs)
    if _prefix in namespaces: setattr(namespaces[_prefix], _name, _cls)

w = namespaces['w']

class Tree:
    "A Python view of one native XML tree, standalone or owned by a package."
    def __init__(self, data):
        "Parse XML bytes, or a detached expression serialized with `.bytes()`"
        self.xml = _core.Xml(data.bytes() if isinstance(data, _Expression) else data)

    @classmethod
    def _from_native(cls, xml):
        tree = cls.__new__(cls)
        tree.xml = xml
        return tree

    def _element(self, index): return types.get(_core.element_type(self.xml, index), Element)(self, index)
    def _elements(self, ids): return [self._element(i) for i in ids if self.xml.is_element(i)]
    @property
    def root(self): return self._element(self.xml.root)

    def elements(self, cls=Element):
        ids = self.xml.element_ids() if cls is Element else _core.elements_of_type(self.xml, cls.type_id)
        return (self._element(i) for i in ids)

    def count(self, cls=Element):
        "Count matching elements, including the root, without constructing result lists or Python views."
        return _core.count_elements(self.xml, cls.type_id)

    def validate(self, target='Microsoft365', dependencies=None, part_uri='', *, relationships=None, complete_dependencies=False):
        native = {name: tree.xml for name, tree in (dependencies or {}).items()}
        report = json.loads(_core.analyze(self.xml, target, native, relationships, complete_dependencies))
        for issue in report['issues']: issue.update(part_uri=part_uri, target=target)
        return Report(report)

    def bytes(self): return self.xml.bytes()


class Report(dict):
    "Validation report keyed like the native JSON; its repr leads with the issue count and tallies gaps by family"
    def __repr__(self):
        def tally(items, key): return ', '.join(f'{k} {n}' for k, n in Counter(map(key, items)).most_common())
        issues, coverage = self['issues'], self['coverage']
        gaps = coverage['gaps']
        parts = [f"{len(issues)} issues" + (f" ({tally(issues, lambda i: i['category'])})" if issues else '')]
        parts.append(f"{len(gaps)} gaps" + (f" ({tally(gaps, lambda g: g.split(':')[0])})" if gaps else ''))
        parts.append(f"{coverage['schema_nodes_checked']} schema nodes, {coverage['semantic_checks']} semantic checks and {coverage['xsd_roots_checked']} XSD roots checked")
        if coverage['skipped_regions']: parts.append(f"{len(coverage['skipped_regions'])} skipped regions")
        return 'Report: ' + '; '.join(parts)
