"""Literal text ranges over one live Word story, not rendered text or persistent positions.

Paragraphs use '\\n', tabs '\\t', line breaks '\\v', and opaque structures U+FFFC.
Unsupported structures stay explicit in read-only text; edits never flatten them.
"""
import json
from .build import E
from .model import Element, metadata, _walk
from ._core import Xml

_W = metadata['namespaces']['w']
_XML = 'http://www.w3.org/XML/1998/namespace'
_OPAQUE = '\ufffc'
_CONTAINERS = {'document', 'body', 'hdr', 'ftr', 'footnote', 'endnote', 'comment', 'tbl', 'tr', 'tc'}
_PROPERTIES = {'pPr', 'tblPr', 'tblGrid', 'trPr', 'tcPr', 'sectPr'}
_UNSAFE = {'fldChar', 'instrText', 'fldSimple', 'sdt', 'txbxContent', 'moveFrom', 'moveTo', 'delText'}
_MARKERS = {'bookmarkStart', 'bookmarkEnd', 'commentRangeStart', 'commentRangeEnd'}
_RUN_MARKERS = {'annotationRef', 'commentReference'}

def _name(element):
    uri, name = element.qname
    return name if uri == _W else None

def _inside(story, element):
    if element._tree is story.element._tree:
        ancestor = element
        while ancestor is not None:
            if ancestor.node_id == story.element.node_id: return
            ancestor = ancestor.parent
    raise ValueError('Target is outside this story')

def _content(element):
    xml = element._tree.xml
    return [json.loads(xml.node(i)) for i in xml.children(element.node_id)]

def _token(node):
    if node['kind'] != 'element': return '' if node['kind'] != 'text' or not node['text'].strip() else _OPAQUE
    uri, name = node['qname']
    if uri != _W: return _OPAQUE
    if name == 'rPr' or name in _RUN_MARKERS: return ''
    if name in ('t', 'delText'): return node['text']
    if name == 'tab': return '\t'
    if name in ('br', 'cr'):
        attrs = {(uri, local): value for uri, local, value in node['attributes']}
        return '\v' if attrs.get((_W, 'type'), 'textWrapping') == 'textWrapping' else _OPAQUE
    return _OPAQUE

def _unsafe(element):
    if _name(element) in {'ins', 'del', 'rPr', 'pPr'}: return False
    if _name(element) in _UNSAFE: return True
    return any(_unsafe(child) for child in element.children)

def _history(run): return any(_name(e) == 'rPrChange' for e in _walk(run))

def _paragraph(element, view='current', positions=None):
    """Projected text, physical child spans, and protected intervals; nothing is cached."""
    text, spans, barriers = '', [], []
    def remember(identity):
        if positions is not None: positions[identity] = len(text)
    def visit(parent, visible=True):
        nonlocal text
        for node in _content(parent):
            remember(node['id'])
            child = element._tree._element(node['id']) if node['kind'] == 'element' else None
            name, start = _name(child) if child is not None else None, len(text)
            if name == 'r':
                for item in _content(child):
                    remember(item['id'])
                    if not visible: continue
                    value = _token(item)
                    if value == _OPAQUE and item.get('qname') not in ([_W, 't'], [_W, 'delText']):
                        barriers.append((len(text), len(text)+1, 'opaque'))
                    if item['kind'] in {'comment', 'pi'} or item.get('qname') == [_W, 'annotationRef']:
                        barriers.append((len(text), len(text), 'opaque'))
                    if item.get('qname') in ([_W, 't'], [_W, 'delText']):
                        if any(n['kind'] != 'text' for n in _content(element._tree._element(item['id']))):
                            barriers.append((len(text), len(text)+max(len(value), 1), 'opaque'))
                    text += value
                if visible and _history(child): barriers.append((start, len(text), 'revision'))
            elif name in {'ins', 'del', 'hyperlink'}:
                visit(child, visible and (name == 'hyperlink' or (name == 'ins') == (view == 'current')))
                barriers.append((start, len(text), 'revision'))
            elif visible and name not in _MARKERS | {'pPr', 'proofErr'}:
                if node['kind'] in {'comment', 'pi'}: barriers.append((len(text), len(text), 'opaque'))
                if child is not None or node['kind'] == 'text' and node['text'].strip():
                    barriers.append((len(text), len(text)+1, 'opaque'))
                    text += _OPAQUE
            if parent.node_id == element.node_id: spans.append((child, start, len(text)))
    visit(element)
    return text, spans, barriers

def _shell(element, *children):
    if element is None: return E('w:r', *children)
    raw = element.raw
    name = ':'.join(filter(None, (raw['prefix'], raw['qname'][1])))
    attrs = {':'.join(filter(None, (prefix, local))): value
             for prefix, (_, local, value) in zip(raw['attribute_prefixes'], raw['attributes'])}
    return E(name, *children, attrs=attrs, ns=dict(raw['namespaces']))

def _run_text(template, text):
    """Make replacement run XML using the affected run's properties/attributes/context."""
    if not isinstance(text, str): raise TypeError('replacement requires str')
    if '\n' in text or '\r' in text: raise NotImplementedError('Paragraph insertion/joining is unsupported; use \\v for a line break')
    children = [] if template is None else [c for c in template.children if _name(c) == 'rPr']
    start = 0
    for index, char in enumerate(text):
        if char not in '\t\v': continue
        if start < index: children.append(E('w:t', text[start:index], attrs={'xml:space': 'preserve'}, ns={'w': _W}))
        children.append(E('w:tab' if char == '\t' else 'w:br', ns={'w': _W}))
        start = index + 1
    if start < len(text): children.append(E('w:t', text[start:], attrs={'xml:space': 'preserve'}, ns={'w': _W}))
    return _shell(template, *children)

def _split_run(run, offset):
    """Split an ordinary run without flattening its ordered XML or namespace context."""
    paragraph, xml = run.parent, run._tree.xml
    index = xml.children(paragraph.node_id).index(run.node_id)
    nodes = _content(run)
    right = run.copy_to(paragraph, index+1)
    copied = xml.children(right.node_id)
    position = 0
    for node, other in zip(nodes, copied):
        value = _token(node)
        end = position + len(value)
        if node['kind'] == 'element' and node['qname'] == [_W, 'rPr']: continue
        if position < offset < end:
            # Preflight established this is plain w:t, never an opaque boundary.
            for identity, fragment in ((node['id'], value[:offset-position]), (other, value[offset-position:])):
                xml.set_text(identity, fragment)
                xml.set_attribute(identity, _XML, 'space', 'preserve')
        elif end <= offset and (value or position < offset): xml.delete(other)
        else: xml.delete(node['id'])
        position = end
    return index+1

def _boundary(paragraph, offset):
    text, spans, _ = _paragraph(paragraph)
    xml = paragraph._tree.xml
    for index, (child, start, end) in enumerate(spans):
        if start == offset < end: return index
        if start < offset < end:
            if child is None or _name(child) != 'r' or _history(child): raise NotImplementedError('Boundary is inside protected XML')
            return _split_run(child, offset-start)
    if offset == len(text): return xml.child_count(paragraph.node_id)
    raise NotImplementedError('Cannot place a boundary inside opaque XML')

class Story:
    """Text of one Word story/container or paragraph, projected without rendering.

    Current includes insertions and omits deletions; original does the reverse and
    is read-only. Other unsupported structures appear as U+FFFC. Markers are zero-width.
    """
    def __init__(self, element, view='current', *, part_uri=None):
        if not isinstance(element, Element): raise TypeError('Story requires a live Element')
        if _name(element) not in _CONTAINERS | {'p'}: raise ValueError('Expected a Word story container or paragraph')
        if view not in {'current', 'original'}: raise ValueError('view must be current or original')
        self.element, self._view, self.part_uri = element, view, part_uri

    @property
    def view(self): return self._view

    def _paragraphs(self):
        from .paragraphs import _separator
        def walk(element, adjacent=False):
            if _name(element) == 'p':
                yield element, *_paragraph(element, self.view), adjacent
                return
            previous = None
            for child in element.children:
                name = _name(child)
                if name in _CONTAINERS | {'p'}: yield from walk(child, name == previous == 'p')
                elif name not in _PROPERTIES: yield None, _OPAQUE, [], [(0, 1, 'opaque')], False
                previous = name
        position, previous, first = 0, None, True
        for paragraph, text, runs, barriers, adjacent in walk(self.element):
            if not first: position += len(_separator(previous, self.view, adjacent))
            yield paragraph, position, text, runs, barriers
            position += len(text)
            previous, first = paragraph, False

    @property
    def text(self):
        result, end = [], 0
        for _, position, text, _, _ in self._paragraphs():
            result.extend(('\n'*(position-end), text))
            end = position+len(text)
        return ''.join(result)

    def _position(self, marker):
        """Compute a live zero-width marker's projected position in this story."""
        if marker._tree is not self.element._tree or _name(marker) not in _MARKERS | _RUN_MARKERS:
            raise ValueError('Expected a zero-width Word marker in this story')
        for paragraph, position, _, _, _ in self._paragraphs():
            if paragraph is None: continue
            positions = {}
            _paragraph(paragraph, self.view, positions)
            if marker.node_id in positions: return position+positions[marker.node_id]
        raise ValueError('Marker is outside the projected story')

    def range(self, start, end): return Range(self, start, end)

    def find(self, literal, start=0):
        """Find the first nonempty literal, including matches spanning ordinary runs."""
        if not isinstance(literal, str) or not literal: raise ValueError('find requires a nonempty literal string')
        position = self.text.find(literal, start)
        return None if position < 0 else self.range(position, position+len(literal))

class Range:
    """Python Unicode code-point offsets; any XML edit makes this range stale."""
    def __init__(self, story, start, end):
        if not isinstance(start, int) or not isinstance(end, int): raise TypeError('range positions require integers')
        if not 0 <= start <= end <= len(story.text): raise ValueError('Range positions are outside the story')
        self.story, self.start, self.end = story, start, end
        self._revision = story.element._tree.xml.revision

    def _check(self):
        self.story.element.node_id
        if self._revision != self.story.element._tree.xml.revision: raise ReferenceError('Text range is stale; find or select it again')

    @property
    def text(self):
        self._check()
        return self.story.text[self.start:self.end]

    def _preflight(self):
        from .revisions import _context
        self._check()
        if self.story.view != 'current': raise NotImplementedError('Original text view is read-only')
        for paragraph, position, text, runs, barriers in self.story._paragraphs():
            if not position <= self.start <= self.end <= position+len(text): continue
            if paragraph is None: raise NotImplementedError('Range contains an opaque story structure')
            start, end = self.start-position, self.end-position
            _context(paragraph)
            if _unsafe(paragraph): raise NotImplementedError('Fields, content controls, textboxes and moves are unsupported')
            for a, b, kind in barriers:
                if kind == 'revision': blocked = start < b and end > a if start != end else a < start < b
                else: blocked = start < b and end > a or start == end and a <= start < b or a == b and start <= a <= end
                if a == b and kind == 'revision': blocked = start < a < end
                if blocked: raise NotImplementedError('Range touches a revision, hyperlink or opaque XML boundary')
            ordinary = [(r, a, b) for r, a, b in runs if r is not None and _name(r) == 'r' and not _history(r)]
            template = next((r for r, a, b in ordinary if a <= start < b or a >= start), ordinary[-1][0] if ordinary else None)
            return paragraph, start, end, template
        raise NotImplementedError('Cross-paragraph replacement is unsupported')

    def _isolate(self, *, extract_references=False):
        """Return (paragraph, selected_runs, content_index, formatting_run_or_None).

        Split run boundaries after read-only preflight. Carets select no runs.
        Destructive callers extract comment references so deleting/wrapping text cannot remove them.
        The returned views are live; this Range becomes stale if splitting edited XML.
        Callers must validate their own operation before invoking this mutating helper.
        """
        paragraph, start, end, template = self._preflight()
        _boundary(paragraph, end)
        index = _boundary(paragraph, start)
        _, runs, _ = _paragraph(paragraph)
        selected = [run for run, a, b in runs if run is not None and _name(run) == 'r' and a < end and b > start]
        for run in selected:
            markers = [c for c in run.children if _name(c) == 'commentReference'] if extract_references else []
            if not markers: continue
            xml = paragraph._tree.xml
            marker_run = _shell(run, *(c for c in run.children if _name(c) == 'rPr')).append_to(
                paragraph, xml.children(paragraph.node_id).index(run.node_id)+1)
            for marker in markers: marker.move_to(marker_run, xml.child_count(marker_run.node_id))
        return paragraph, selected, index, template

    def replace(self, text):
        """Replace within one ordinary paragraph, using the first affected run's format.

        A caret uses the run to its right, or the final run at paragraph end. Unknown
        siblings remain untouched; unsupported boundaries are refused before mutation.
        """
        self._check()
        if self.story.view != 'current': raise NotImplementedError('Original text view is read-only')
        if not isinstance(text, str): raise TypeError('replacement requires str')
        single = any(p is not None and pos <= self.start <= self.end <= pos+len(value)
                     for p, pos, value, _, _ in self.story._paragraphs())
        if '\n' in text or not single:
            from .paragraphs import _replace_range
            return _replace_range(self, text)
        _, _, _, template = self._preflight()
        expression = _run_text(template, text)
        Xml(expression.bytes())  # Validate the small detached replacement before splitting live runs.
        if self.start == self.end and not text: return self
        paragraph, runs, index, _ = self._isolate(extract_references=True)
        if text: expression.append_to(paragraph, index)
        for run in runs: run.delete()
        return self.story.range(self.start, self.start+len(text))
