"""Bookmarks and hyperlink wrappers over live stories, without field evaluation."""
from ._core import Xml
from .build import E
from .model import metadata, _walk, _one
from .text import Story, Range, _name, _paragraph, _run_text, _inside

_W, _R = metadata['namespaces']['w'], metadata['namespaces']['r']
_HYPERLINK = _R+'/hyperlink'
_STRICT_HYPERLINK = 'http://purl.oclc.org/ooxml/officeDocument/relationships/hyperlink'

def _editable(story):
    if story.view != 'current': raise NotImplementedError('Original text view is read-only')

def _selection(story, span):
    if not isinstance(span, Range): raise TypeError('Expected a text Range')
    _editable(story)
    paragraph, _, _, _ = span._preflight()
    _inside(story, paragraph)

def _name_value(name):
    if not isinstance(name, str) or not name or any(c.isspace() for c in name):
        raise ValueError('Bookmark name requires a nonempty string without whitespace')

class Bookmarks:
    """Live bookmarks indexed by exact name in one story; IDs are allocated across its XML part."""
    def __init__(self, story):
        if not isinstance(story, Story): raise TypeError('Bookmarks requires a Story')
        self.story = story

    def __iter__(self):
        for element in _walk(self.story.element):
            if _name(element) == 'bookmarkStart': yield Bookmark(self, element)

    def find(self, name): return _one((b for b in self if b.name == name), 'bookmark name')

    def __getitem__(self, name):
        bookmark = self.find(name)
        if bookmark is None: raise KeyError(name)
        return bookmark

    def add(self, span, name):
        """Anchor a single-paragraph range or caret without replacing its text."""
        _selection(self.story, span)
        _name_value(name)
        markers = [e for e in span.story.element._tree.elements() if _name(e) in {'bookmarkStart', 'bookmarkEnd'}]
        if any(e.attribute(_W, 'name') == name for e in markers): raise ValueError('Bookmark name already exists')
        used = {int(e.attribute(_W, 'id')) for e in markers}
        ident = str(next(i for i in range(len(used)+1) if i not in used))
        start = E('w:bookmarkStart', attrs={'w:id': ident, 'w:name': name})
        end = E('w:bookmarkEnd', attrs={'w:id': ident})
        Xml(E('markers', start, end).bytes())
        paragraph, runs, index, _ = span._isolate()
        xml = paragraph._tree.xml
        stop = xml.children(paragraph.node_id).index(runs[-1].node_id)+1 if runs else index
        end.append_to(paragraph, stop)
        return Bookmark(self, start.append_to(paragraph, index))

class Bookmark:
    def __init__(self, bookmarks, element): self.bookmarks, self.element = bookmarks, element
    @property
    def name(self): return self.element.attribute(_W, 'name')
    @property
    def id(self): return int(self.element.attribute(_W, 'id'))

    def _end(self):
        ident = self.id
        starts = [b for b in self.bookmarks if b.id == ident]
        if len(starts) != 1: raise ValueError('Ambiguous bookmark ID')
        end = _one((e for e in _walk(self.bookmarks.story.element) if _name(e) == 'bookmarkEnd' and int(e.attribute(_W, 'id')) == ident),
                   'bookmark end')
        if end is None: raise ValueError('Bookmark end is missing from this story')
        return end

    @property
    def range(self):
        story = self.bookmarks.story
        return story.range(story._position(self.element), story._position(self._end()))

    def remove(self):
        """Remove only this bookmark's two markers, not text or hyperlinks targeting it."""
        _editable(self.bookmarks.story)
        end = self._end()
        self.range  # Refuse reversed/out-of-scope anchors before removing either one.
        end.delete()
        self.element.delete()

    def ref(self, text=None):
        """Detached REF field with a cached result; no field evaluation or recalculation."""
        if not self.name.isidentifier(): raise ValueError('REF construction requires a simple identifier bookmark name')
        return E('w:fldSimple', _run_text(None, self.range.text if text is None else text),
                 attrs={'w:instr': f' REF {self.name} ', 'w:dirty': 'true'})

class Hyperlinks:
    """Hyperlinks in one story, with relationships scoped to its verified owning XML part."""
    def __init__(self, package, story):
        if not isinstance(story, Story): raise TypeError('Hyperlinks requires a Story')
        self.package, self.story = package, story

    def _owner(self):
        uri = self.package._owner(self.story.element)
        if self.story.part_uri is not None and self.package._native.resolve_part(self.story.part_uri) != uri:
            raise ValueError('Story does not belong to the claimed package part')
        return uri

    def __iter__(self):
        self._owner()
        for element in _walk(self.story.element):
            if _name(element) == 'hyperlink': yield Hyperlink(self, element)

    def add(self, span, target):
        """Wrap existing formatted text. '#name' is a bookmark; other targets are external relationships."""
        _selection(self.story, span)
        if span.start == span.end: raise ValueError('A hyperlink requires a nonempty text range')
        uri = self._owner()
        if not isinstance(target, str) or not target: raise ValueError('Hyperlink target requires a nonempty string')
        Xml(E('target', attrs={'value': target}).bytes())
        if target.startswith('#'):
            _name_value(target[1:])
            attrs = {'w:anchor': target[1:]}
        else:
            relationship = next((r for r in self.package.relationships(uri)
                                 if r['type'] in {_HYPERLINK, _STRICT_HYPERLINK} and r['target_mode'] == 'External' and r['target'] == target), None)
            ident = relationship['id'] if relationship else self.package.add_relationship(uri, _HYPERLINK, target, 'External')
            attrs = {'r:id': ident}
        paragraph, runs, index, _ = span._isolate()
        xml = paragraph._tree.xml
        content = xml.children(paragraph.node_id)
        selected = content[index:content.index(runs[-1].node_id)+1]
        element = E('w:hyperlink', attrs=attrs).append_to(paragraph, index)
        for identity in selected: xml.move_node(identity, element.node_id, xml.child_count(element.node_id))
        return Hyperlink(self, element)

class Hyperlink:
    def __init__(self, hyperlinks, element): self.hyperlinks, self.element = hyperlinks, element
    @property
    def anchor(self): return self.element.attribute(_W, 'anchor')
    @property
    def text(self): return _paragraph(self.element, self.hyperlinks.story.view)[0]

    def _relationship(self):
        ident = self.element.attribute(_R, 'id')
        if ident is None: return None
        return _one((r for r in self.hyperlinks.package.relationships(self.hyperlinks._owner()) if r['id'] == ident), 'hyperlink relationship')

    @property
    def target(self):
        relationship = self._relationship()
        if relationship is None:
            if self.element.attribute(_R, 'id') is not None: raise KeyError('Hyperlink relationship is missing')
            return '#'+self.anchor if self.anchor is not None else None
        if relationship['type'] not in {_HYPERLINK, _STRICT_HYPERLINK}: raise ValueError('Hyperlink ID references a different relationship type')
        if relationship['target_mode'] == 'External': return relationship['target']
        target = self.hyperlinks.package.relationship_part(self.hyperlinks._owner(), relationship['id'])
        _, separator, fragment = relationship['target'].partition('#')
        return target+(separator+fragment if separator else '')

    def remove(self):
        """Unwrap text; remove its relationship only when no owning-part attribute still references it."""
        from .revisions import _context
        owner = self.hyperlinks
        _editable(owner.story)
        _inside(owner.story, self.element)
        uri = owner._owner()
        parent, xml = self.element.parent, self.element._tree.xml
        if parent is None or _name(parent) != 'p': raise NotImplementedError('Only direct paragraph hyperlinks can be unwrapped')
        _context(parent)
        relationship = self._relationship()
        index = xml.children(parent.node_id).index(self.element.node_id)
        for identity in xml.children(self.element.node_id):
            xml.move_node(identity, parent.node_id, index)
            index += 1
        self.element.delete()
        if relationship is not None and relationship['type'] in {_HYPERLINK, _STRICT_HYPERLINK}:
            used = any(value == relationship['id'] for e in parent._tree.elements() for _, _, value in e.raw['attributes'])
            if not used: owner.package.remove_relationship(uri, relationship['id'])
