"""Import selected blocks and their explicit dependencies through the native document owner."""
from . import _core
from .model import Element

def import_content(source, elements, destination, parent, index=None):
    """Import paragraphs/tables, explicit styles, numbering, images and links; retain destination defaults."""
    if isinstance(elements, Element): elements = [elements]
    selected = [(element._tree.xml, element.node_id) for element in elements]
    ids = _core.import_content(source.package._native, selected, destination.package._native, parent._tree.xml, parent.node_id, index)
    return [parent._tree._element(node_id) for node_id in ids]
