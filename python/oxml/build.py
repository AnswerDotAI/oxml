"""Detached XML expressions: construct first, then attach a complete subtree once."""
from functools import lru_cache
from xml.sax.saxutils import escape, quoteattr

from ._core import Xml
from .model import Element, metadata

_XML = 'http://www.w3.org/XML/1998/namespace'

@lru_cache(maxsize=1024)
def _name(value):
    Xml.check_qname(value)
    return value

class E:
    """Build `E('w:p', E('w:r', E('w:t', 'text')), attrs={...}, ns={...})`.

    Children are expressions, parsed Elements (snapshotted now), strings (escaped,
    in order), or None (omitted). Element snapshots copy XML and namespace context,
    not package dependencies; relationship/document IDs remain unchanged.
    Names use `prefix:local` or unqualified XML names; known SDK prefixes resolve
    automatically. `ns` supplies custom/default bindings, including prefixes used
    only in attribute values. Attributes are raw lexical values, not typed setters.
    """
    __slots__ = ('_name', '_children', '_attrs', '_ns')

    def __init__(self, name, *children, attrs=None, ns=None):
        self._name = _name(name)
        children = tuple(c for c in children if c is not None)
        if any(not isinstance(c, (E, Element, str)) for c in children): raise TypeError('children require E, Element, str or None')
        self._children = tuple(c._tree.xml.subtree_bytes(c.node_id) if isinstance(c, Element) else c for c in children)
        self._attrs = tuple((_name(k), str(v)) for k, v in (attrs or {}).items())
        if any(k == 'xmlns' or k.startswith('xmlns:') for k, _ in self._attrs): raise ValueError('Use ns for namespace declarations')
        self._ns = tuple((k, v) for k, v in (ns or {}).items())
        for prefix, uri in self._ns:
            if prefix: _name(prefix)
            if ':' in prefix or not isinstance(uri, str): raise ValueError('ns requires prefix-to-URI strings')

    def _render(self, inherited, output, root=False):
        context = {**inherited, **dict(self._ns)}
        for name in (self._name, *(k for k, _ in self._attrs)):
            prefix, colon, _ = name.partition(':')
            if not colon or prefix in context: continue
            if prefix not in metadata['namespaces']: raise ValueError(f'Unknown XML prefix: {prefix!r}; supply ns')
            context[prefix] = metadata['namespaces'][prefix]
        output.append(f'<{self._name}')
        for prefix, uri in context.items():
            if inherited.get(prefix) != uri or root and prefix == '':
                declaration = f'xmlns:{prefix}' if prefix else 'xmlns'
                output.append(f' {declaration}={quoteattr(uri)}')
        for name, value in self._attrs: output.append(f' {name}={quoteattr(value)}')
        if not self._children:
            output.append('/>')
            return
        output.append('>')
        for child in self._children:
            if isinstance(child, E): child._render(context, output)
            elif isinstance(child, bytes): output.append(child.decode('utf-8'))
            else: output.append(escape(child, {'\r': '&#13;'}))
        output.append(f'</{self._name}>')

    def bytes(self):
        """Serialize the expression, independently of a destination's namespaces.

        Full XML well-formedness/resource checks run when parsed or attached.
        """
        output = []
        self._render({'': '', 'xml': _XML}, output, root=True)
        return ''.join(output).encode('utf-8')

    def append_to(self, parent, index=None):
        """Attach one complete subtree and return its live contextual typed view."""
        data = self.bytes()
        nodes = parent.append_xml(data) if index is None else parent.insert_xml(index, data)
        return nodes[0]
