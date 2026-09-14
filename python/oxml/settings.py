'Document settings, a mapping over the flat `w:val` children of the settings part.'
from collections.abc import MutableMapping
from . import _core
from .model import Tree

class Settings(MutableMapping):
    "Flat settings by local name; schema-defined on/off values read as bool, other values as strings"
    def __init__(self, doc): self._native = _core.NativeSettings(doc.package._native)
    @property
    def root(self):
        "The live `w:settings` element, created on first use, for settings with children of their own"
        return Tree._from_native(self._native.root).root
    def __getitem__(self, name): return self._native.get(name)
    def __setitem__(self, name, value): self._native.set(name, value if isinstance(value, bool) else str(value))
    def __delitem__(self, name): self._native.remove(name)
    def __iter__(self): return iter(self._native.keys())
    def __len__(self): return len(self._native.keys())
    def __repr__(self): return f'Settings({dict(self)})'
