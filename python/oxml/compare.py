"""Tracked body-text/direct-format comparison, implemented by the native revision core."""
from . import _core
from ._dates import lexical
from .document import Document, Package

def compare(original, revised, *, author, date=None):
    """Return tracked differences; changed opaque content and dependencies remain unsupported."""
    native = _core.compare(original.package._native, revised.package._native, author, lexical(date))
    return Document(Package._from_native(native))
