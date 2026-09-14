'Thin bookmark and hyperlink views; native code owns anchoring and relationships.'
from fastcore.xml import _xml_flatten
from . import _core
from ._core import bookmark_name
from .build import _bytes
from .model import Tree
from .text import Range

class Bookmarks:
    def __init__(self, story): self.story, self._native = story, _core.NativeBookmarks(story._native)
    def __iter__(self): return (Bookmark(self, native) for native in self._native.items())

    def find(self, name):
        native = self._native.find(name)
        return None if native is None else Bookmark(self, native)

    def __getitem__(self, name):
        bookmark = self.find(name)
        if bookmark is None: raise KeyError(name)
        return bookmark

    def add(self, span, name): return Bookmark(self, self._native.add(span._native, name))

    def ref(self, name, text=None, switches=''):
        "Detached `REF` field for bookmark `name`, which need not exist yet when `text` supplies the cached result"
        return Tree._from_native(self._native.reference(name, text, switches)).root

class Bookmark:
    def __init__(self, bookmarks, native): self.bookmarks, self._native = bookmarks, native
    @property
    def element(self): return Tree._from_native(self._native.xml)._element(self._native.element_id)
    @property
    def name(self): return self._native.name
    @property
    def id(self): return self._native.id
    @property
    def range(self): return Range._from_native(self._native.range, self.bookmarks.story.part_uri)

    def remove(self): self._native.remove()
    def ref(self, text=None, switches=''):
        "Detached `REF` field showing `text` (default: the bookmarked text) with optional field switches such as `\\w \\h`"
        return Tree._from_native(self._native.reference(text, switches)).root

class Hyperlinks:
    def __init__(self, package, story):
        self.package, self.story = package, story
        self._native = _core.NativeHyperlinks(package._native, story._native, story.part_uri)

    def __iter__(self): return (Hyperlink(self, native) for native in self._native.items())
    def add(self, span, target): return Hyperlink(self, self._native.add(span._native, target))

    def link(self, target, *children, part_uri=None):
        "Detached `w:hyperlink` around `children`: a `#bookmark` anchor, or a URL whose relationship is allocated once per part, the story's unless `part_uri`"
        return Tree._from_native(self._native.build(target, [_bytes(c) for c in _xml_flatten(children)], part_uri)).root

class Hyperlink:
    def __init__(self, hyperlinks, native): self.hyperlinks, self._native = hyperlinks, native
    @property
    def element(self): return Tree._from_native(self._native.xml)._element(self._native.element_id)
    @property
    def anchor(self): return self._native.anchor
    @property
    def text(self): return self._native.text
    @property
    def target(self): return self._native.target
    @target.setter
    def target(self, value): self._native.target = value

    def remove(self): self._native.remove()
