"""Native paragraph edits in one immediate Word container."""
from . import _core

def split_paragraph(paragraph, offset):
    "Split at a Unicode offset; return the new left and original right paragraph."
    ids = _core.split(paragraph._tree.xml, paragraph.node_id, offset)
    return tuple(paragraph._tree._element(id) for id in ids)

def join_paragraphs(first, second):
    "Join adjacent paragraphs, retaining the second paragraph and its properties."
    if not first._tree.xml.same_state(second._tree.xml): raise ValueError('Paragraphs belong to different XML trees')
    return first._tree._element(_core.join_pair(first._tree.xml, first.node_id, second.node_id))
