"""Thin Python views of native Word stories and Unicode text ranges."""
from . import _core
from .model import Element, Tree

class Story:
    "Current or original text of one Word story, container or paragraph; no rendering."
    def __init__(self, element, view='current', *, part_uri=None):
        if not isinstance(element, Element): raise TypeError('Story requires a live Element')
        self._native = _core.NativeStory(element._tree.xml, element.node_id, view)
        self.element, self.part_uri = element, part_uri

    @classmethod
    def _from_native(cls, native, part_uri=None):
        result = cls.__new__(cls)
        result._native, result.part_uri = native, part_uri
        result.element = Tree._from_native(native.xml)._element(native.element_id)
        return result

    @property
    def view(self): return self._native.view
    @property
    def text(self): return self._native.text

    def range(self, start, end): return Range._from_native(self._native.range(start, end), self.part_uri)
    def find(self, literal, start=0):
        native = self._native.find(literal, start)
        return None if native is None else Range._from_native(native, self.part_uri)

    def _position(self, marker):
        if not self._native.xml.same_state(marker._tree.xml): raise ValueError('Marker belongs to another XML tree')
        return self._native.position(marker.node_id)

class Range:
    "Unicode code-point offsets; any XML edit makes the range stale."
    def __init__(self, story, start, end):
        self._native, self.story = _core.NativeRange(story._native, start, end), story

    @classmethod
    def _from_native(cls, native, part_uri=None):
        result = cls.__new__(cls)
        result._native, result.story = native, Story._from_native(native.story, part_uri)
        return result

    @property
    def start(self): return self._native.start
    @property
    def end(self): return self._native.end
    @property
    def text(self): return self._native.text

    def replace(self, text): return Range._from_native(self._native.replace(text), self.story.part_uri)
