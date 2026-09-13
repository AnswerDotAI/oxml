"""Python views of native comment bodies, anchors and threaded review metadata."""
from . import _core
from ._dates import lexical
from .model import Tree
from .text import Range

class Comments:
    "Live comments indexed by numeric comment ID, not collection position."
    def __init__(self, doc): self.doc, self._native = doc, _core.NativeComments(doc.package._native)
    def __iter__(self): return (Comment(self, native) for native in self._native.items())
    def __getitem__(self, ident): return Comment(self, self._native.get(int(ident)))

    def add(self, range, text, author, *, initials='', date=None):
        return Comment(self, self._native.add(range._native, text, author, initials, lexical(date)))

class Comment:
    def __init__(self, comments, native): self.comments, self._native = comments, native
    @property
    def element(self): return Tree._from_native(self._native.xml)._element(self._native.element_id)
    @property
    def id(self): return self._native.id
    @property
    def author(self): return self._native.author
    @property
    def text(self): return self._native.text
    @property
    def range(self): return Range._from_native(self._native.range, self.comments.doc.main.uri)
    @property
    def parent(self):
        native = self._native.parent
        return None if native is None else Comment(self.comments, native)
    @property
    def replies(self): return [Comment(self.comments, native) for native in self._native.replies]
    @property
    def resolved(self): return self._native.resolved

    def resolve(self, value=True):
        self._native.resolve(value)
        return self

    def reply(self, text, author, *, initials='', date=None):
        return Comment(self.comments, self._native.reply(text, author, initials, lexical(date)))

    def delete(self): return self._native.delete()
    def delete_thread(self): return self._native.delete_thread()
