"""Python conversion for native direct-property revision creation."""
from . import _core
from ._dates import lexical
from .build import _bytes

def create(story, target, properties, *, author, date=None):
    "Replace direct run/paragraph properties and retain the old properties as a revision."
    if not story.element._tree.xml.same_state(target._tree.xml): raise ValueError('Target is outside this story')
    id = _core.revisions_format(story._native, target.node_id, _bytes(properties), author, lexical(date))
    return target._tree._element(id)
