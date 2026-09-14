"Core document properties, backed by the native package."
from collections.abc import MutableMapping
from datetime import datetime
from . import _core
from ._dates import lexical

class Properties(MutableMapping):
    "Core properties such as `title`, `creator`, `lastModifiedBy` and `created`; the part is created on first write"
    def __init__(self, doc): self._native = _core.NativeProperties(doc.package._native)
    def __getitem__(self, name): return self._native.get(name)
    def __setitem__(self, name, value): self._native.set(name, lexical(value) if isinstance(value, datetime) else value)
    def __delitem__(self, name): self._native.remove(name)
    def __iter__(self): return iter(self._native.keys())
    def __len__(self): return len(self._native.keys())
    def __repr__(self): return f'Properties({dict(self)})'
