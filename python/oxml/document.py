'Python conveniences over native package ownership and document operations.'
import json
from pathlib import Path
from . import _core
from .model import Report, Tree

class Part:
    def __init__(self, native): self._native = native
    @property
    def uri(self): return self._native.uri
    @property
    def content_type(self): return self._native.content_type
    @property
    def xml(self): return Tree._from_native(self._native.xml)
    def read_bytes(self): return self._native.read_bytes()
    def replace(self, data): return Part(self._native.replace(data))
    def add_image(self, data, *, width=None, height=None, description='', content_type=None):
        "Embed image bytes and return detached drawing XML; EMU sizes default to the image's own size at its resolution, and one given size keeps the aspect ratio"
        return Tree._from_native(self._native.add_image(data, content_type, width, height, description)).root

class Package:
    def __init__(self, data: bytes): self._native = _core.Package(data)

    @classmethod
    def _from_native(cls, native):
        package = cls.__new__(cls)
        package._native = native
        return package

    @classmethod
    def open(cls, path): return cls(Path(path).read_bytes())
    @classmethod
    def new(cls): return cls._from_native(_core.Package.new())
    @property
    def main_part(self): return self._native.main_part

    def part_names(self): return self._native.part_names()
    def part(self, uri): return Part(self._native.part(uri))
    def content_type(self, uri): return self._native.content_type(uri)
    def set_content_type(self, uri, content_type): self._native.set_content_type(uri, content_type)
    def read_part(self, uri): return self._native.read_part(uri)
    def add_part(self, uri, content_type, data): return Part(self._native.add_part(uri, content_type, data))
    def replace_part(self, uri, data): return Part(self._native.replace_part(uri, data))
    def remove_part(self, uri): self._native.remove_part(uri)
    def relationships(self, source_uri='/'): return self._native.relationships(source_uri)
    def relationship_part(self, source_uri, relationship_id): return self._native.relationship_part(source_uri, relationship_id)

    def add_relationship(self, source_uri, relationship_type, target, target_mode='Internal', relationship_id=None):
        return self._native.add_relationship(source_uri, relationship_type, target, target_mode, relationship_id)

    def remove_relationship(self, source_uri, relationship_id): self._native.remove_relationship(source_uri, relationship_id)
    def relationship_id(self, source_uri, target_uri): return self._native.relationship_id(source_uri, target_uri)
    def bytes(self): return self._native.bytes()
    def save(self, path): self._native.save(str(path))

class Document:
    def __init__(self, package: Package): self.package = package
    @classmethod
    def open(cls, path): return cls(Package.open(path))
    @classmethod
    def from_bytes(cls, data): return cls(Package(data))
    @classmethod
    def new(cls): return cls(Package.new())
    @property
    def main(self): return self.package.part(self.package.main_part)
    @property
    def story(self):
        from .text import Story
        return Story(self.main.xml.root, part_uri=self.main.uri)
    @property
    def comments(self):
        from .comments import Comments
        return Comments(self)
    @property
    def revisions(self):
        from .revisions import Revisions
        return Revisions(self.story)
    @property
    def styles(self):
        from .styles import Styles
        return Styles(self)
    @property
    def numbering(self):
        from .numbering import Numbering
        return Numbering(self)
    @property
    def bookmarks(self):
        from .links import Bookmarks
        return Bookmarks(self.story)
    @property
    def hyperlinks(self):
        from .links import Hyperlinks
        return Hyperlinks(self.package, self.story)
    @property
    def footnotes(self):
        from .footnotes import Footnotes
        return Footnotes(self)

    def bytes(self): return self.package.bytes()
    def save(self, path): self.package.save(path)

    def set_custom_xml(self, item_id, data, *, schema_uri=None):
        "Set a native custom XML datastore by GUID, retaining other stores and existing properties."
        return Part(self.package._native.set_custom_xml(item_id, data, schema_uri))

    def part(self, name, create=False):
        "Find a main-document related XML part by SDK name, optionally creating it."
        part = _core.declared_part(self.package._native, name, create)
        return None if part is None else Part(part)

    def add_part(self, name):
        "Create another main-document related XML part by SDK name, such as a second `HeaderPart`"
        return Part(_core.add_declared_part(self.package._native, name))

    @property
    def properties(self):
        from .properties import Properties
        return Properties(self)
    @property
    def settings(self):
        from .settings import Settings
        return Settings(self)

    def stories(self, view='current'):
        from .text import Story
        for native, node_id in _core.package_stories(self.package._native):
            part = Part(native)
            yield Story(part.xml._element(node_id), view=view, part_uri=part.uri)

    def validate(self, target='Microsoft365'): return Report(json.loads(_core.validate_package(self.package._native, target)))
