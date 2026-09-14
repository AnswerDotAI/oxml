'Footnote bodies and reference runs; native code owns ids, marks, separators and removal.'
from . import _core
from .build import _bytes, e
from .model import Tree

class Footnotes:
    "Live footnotes indexed by numeric id; `add` references live text and `create` builds a note for a detached reference run"
    def __init__(self, doc): self.doc, self._native = doc, _core.NativeFootnotes(doc.package._native)
    def __iter__(self): return (Footnote(self, native) for native in self._native.items())
    def __getitem__(self, ident): return Footnote(self, self._native.get(int(ident)))

    def _blocks(self, content):
        "Marshal text or block expressions for native footnote creation"
        if isinstance(content, str): content = [e.p(e.r(e.t(content, xml__space='preserve')))]
        return [_bytes(block) for block in content]

    def create(self, content):
        "A new footnote holding `content`; attach `note.reference()` where its mark belongs"
        return Footnote(self, self._native.create(self._blocks(content)))

    def add(self, span, content):
        "A new footnote whose mark follows `span`"
        return Footnote(self, self._native.add(span._native, self._blocks(content)))

class Footnote:
    def __init__(self, footnotes, native): self.footnotes, self._native = footnotes, native
    @property
    def element(self): return Tree._from_native(self._native.xml)._element(self._native.element_id)
    @property
    def id(self): return self._native.id
    @property
    def text(self): return self._native.text
    def reference(self):
        "Detached run carrying this footnote's reference mark"
        return Tree._from_native(self._native.reference()).root
    def delete(self):
        "Remove the note and every run referencing it; returns how many references went"
        return self._native.delete()
