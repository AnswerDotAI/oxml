"""Thin Python access to native tracked text, paragraph and formatting operations."""
from . import _core
from ._dates import lexical
from .text import Story, Range
from .model import namespace_uris

_W = namespace_uris['w']

class Revision:
    "One live revision; accepting or rejecting it consumes its XML element."
    def __init__(self, element): self.element = element

    @property
    def kind(self): return self.element.qname[1]
    @property
    def id(self): return self.element.attribute(_W, 'id')
    @property
    def author(self): return self.element.attribute(_W, 'author')
    @property
    def date(self): return self.element.attribute(_W, 'date')
    @property
    def text(self): return _core.revision_info(self.element._tree.xml, self.element.node_id)[0]
    @property
    def is_boundary(self): return _core.revision_info(self.element._tree.xml, self.element.node_id)[1]
    @property
    def previous(self):
        ids = _core.revision_properties(self.element._tree.xml, self.element.node_id)
        return self.element._tree._element(ids[1])
    @property
    def current(self):
        ids = _core.revision_properties(self.element._tree.xml, self.element.node_id)
        return self.element._tree._element(ids[0])

    def accept(self): _core.revision_apply(self.element._tree.xml, self.element.node_id, True)
    def reject(self): _core.revision_apply(self.element._tree.xml, self.element.node_id, False)

class Revisions:
    "Inspect and edit tracked changes in one Story; unsupported families are refused."
    def __init__(self, story):
        if not isinstance(story, Story): raise TypeError('Revisions requires a Story')
        self.story = story

    def __iter__(self):
        tree = self.story.element._tree
        return (Revision(tree._element(id)) for id in _core.revision_ids(self.story._native))

    def accept_all(self): return _core.revisions_apply(self.story._native, True)
    def reject_all(self): return _core.revisions_apply(self.story._native, False)

    def format(self, target, properties, *, author, date=None):
        from .formatting import create
        return Revision(create(self.story, target, properties, author=author, date=date))

    def replace(self, span, text, *, author, date=None):
        if not isinstance(span, Range): raise TypeError('Expected a text Range')
        ids = _core.revisions_replace(self.story._native, span._native, text, author, lexical(date))
        return tuple(Revision(self.story.element._tree._element(id)) for id in ids)
