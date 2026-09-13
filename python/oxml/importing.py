"""Bounded paragraph/table import with explicit package dependencies.

Destination theme and document defaults remain in effect. This copies explicit
definitions and XML, not a rendered formatting cascade or an entire package.
"""
from posixpath import basename, dirname, splitext
from .model import Element, Tree, metadata, _walk, _choose_prefix
from .styles import _W, _child, _position, _attribute
from .numbering import _find
from .revisions import _context, _revision_name

_NS = metadata['namespaces']
_R, _W14, _WP, _PIC, _MC = (_NS[p] for p in ('r', 'w14', 'wp', 'pic', 'mc'))
_O = 'urn:schemas-microsoft-com:office:office'
_STYLE_REFS = {'pStyle', 'rStyle', 'tblStyle', 'basedOn', 'next', 'link', 'styleLink', 'numStyleLink'}
_UNSUPPORTED = {'commentRangeStart', 'commentRangeEnd', 'commentReference', 'annotationRef', 'footnoteReference', 'endnoteReference',
                'sectPr', 'altChunk', 'object', 'sdt', 'fldSimple', 'fldChar', 'instrText', 'dataBinding', 'lvlPicBulletId',
                'permStart', 'permEnd'}

def _fresh(value, used, limit=None):
    candidate, index = value, 0
    while candidate in used:
        index += 1
        suffix = f'_imported{index}'
        candidate = (value[:limit-len(suffix)] if limit else value)+suffix
    used.add(candidate)
    return candidate

def _allocate(used, hexadecimal=False, start=1):
    while start in used: start += 1
    used.add(start)
    return f'{start:08X}' if hexadecimal else str(start)

def _inherit_ignorable(original, copied):
    """Carry ancestor Ignorable namespace URIs, not prefixes that may have been rebound."""
    namespaces, ancestor = set(), original.parent
    while ancestor is not None:
        bindings = dict(ancestor.raw['namespaces'])
        for uri, local, value in ancestor.raw['attributes']:
            if uri != _MC: continue
            if local != 'Ignorable': raise NotImplementedError('Inherited MC directives other than Ignorable are unsupported')
            for prefix in value.split():
                if prefix not in bindings: raise ValueError('Unbound inherited Ignorable prefix')
                namespaces.add(bindings[prefix])
        ancestor = ancestor.parent
    if not namespaces: return
    bindings = dict(copied.raw['namespaces'])
    def prefix_for(uri):
        prefix = _choose_prefix(bindings, uri, 'imported')
        if prefix not in bindings:
            copied._tree.xml.declare_namespace(copied.node_id, prefix, uri)
            bindings[prefix] = uri
        return prefix
    prefixes = (copied.attribute(_MC, 'Ignorable') or '').split()
    for uri in sorted(namespaces):
        prefix = prefix_for(uri)
        if prefix not in prefixes: prefixes.append(prefix)
    copied._tree.xml.set_attribute(copied.node_id, _MC, 'Ignorable', ' '.join(prefixes), prefix_for(_MC))

class _Import:
    def __init__(self, source, destination, parent):
        self.source, self.destination, self.parent = source, destination, parent
        self.target_uri = destination.package._owner(parent)
        self.styles, self.nums, self.abstracts = {}, {}, {}
        self.entries, self.relationships, self.images = [], {}, {}
        self.source_rels = {}
        self.target_numbering = destination.numbering._root()
        existing_styles = list(destination.styles)
        self.style_ids = {s.id for s in existing_styles}
        self.style_names = {s.name for s in existing_styles}
        self.part_names = {name.lower() for name in destination.package.part_names()}
        self.ids = {kind: set() for kind in ('bookmark', 'drawing', 'paragraph', 'num', 'abstract', 'durable', 'nsid')}
        self.bookmarks, self.bookmark_names, self.anchors = {}, set(), []
        for story in destination.stories():
            for e in _walk(story.element):
                uri, local = e.qname
                if uri == _W and local in {'bookmarkStart', 'bookmarkEnd'}:
                    self.ids['bookmark'].add(int(e.attribute(_W, 'id')))
                    if local == 'bookmarkStart': self.bookmark_names.add(e.attribute(_W, 'name'))
                if (uri, local) in {(_WP, 'docPr'), (_PIC, 'cNvPr')}:
                    self.ids['drawing'].add(int(e.attribute('', 'id')))
                if (value := e.attribute(_W14, 'paraId')) is not None: self.ids['paragraph'].add(int(value, 16))
        if self.target_numbering is not None:
            for e in _walk(self.target_numbering):
                for local, attr, kind, base in [('num', 'numId', 'num', 10), ('abstractNum', 'abstractNumId', 'abstract', 10),
                                                ('num', 'durableId', 'durable', 10), ('nsid', 'val', 'nsid', 16)]:
                    if e.qname == (_W, local) and (value := e.attribute(_W, attr)) is not None:
                        self.ids[kind].add(int(value, base))

    def _copy(self, element, kind):
        source_uri = self.source.package._owner(element)
        tree = Tree(element._tree.xml.subtree_bytes(element.node_id))
        _inherit_ignorable(element, tree.root)
        self.entries.append((kind, tree, source_uri))
        return tree.root, source_uri

    def style(self, ident):
        if ident in self.styles: return self.styles[ident]
        original = self.source.styles[ident]
        self.styles[ident] = _fresh(ident, self.style_ids)
        element, uri = self._copy(original.element, 'styles')
        _attribute(element, 'styleId', self.styles[ident])
        element.remove_attribute(_W, 'default')  # Import must not change the destination's implicit style defaults.
        name = _child(element, 'name')
        if name is not None: _attribute(name, 'val', _fresh(name.attribute(_W, 'val'), self.style_names))
        self.scan(element, uri, 'styles')
        return self.styles[ident]

    def num(self, ident):
        ident = int(ident)
        if ident == 0: return '0'  # Explicitly no numbering.
        if ident in self.nums: return self.nums[ident]
        original = self.source.numbering[ident]
        self.nums[ident] = _allocate(self.ids['num'])
        element, uri = self._copy(original.element, 'numbering')
        _attribute(element, 'numId', self.nums[ident])
        if element.attribute(_W, 'durableId') is not None: _attribute(element, 'durableId', _allocate(self.ids['durable']))
        self.scan(element, uri, 'numbering')
        return self.nums[ident]

    def abstract(self, ident):
        ident = int(ident)
        if ident in self.abstracts: return self.abstracts[ident]
        root = self.source.numbering._root()
        original = _find(root, 'abstractNum', 'abstractNumId', ident) if root is not None else None
        if original is None: raise ValueError('Missing abstract numbering definition')
        self.abstracts[ident] = _allocate(self.ids['abstract'], start=0)
        element, uri = self._copy(original, 'numbering')
        _attribute(element, 'abstractNumId', self.abstracts[ident])
        self.scan(element, uri, 'numbering')
        return self.abstracts[ident]

    def relationship(self, element, source_uri, target_kind, uri, local, ident):
        key = source_uri, target_kind, ident
        if key not in self.relationships:
            if source_uri not in self.source_rels:
                self.source_rels[source_uri] = {r['id']: r for r in self.source.package.relationships(source_uri)}
            rel = self.source_rels[source_uri].get(ident)
            if rel is None: raise ValueError(f'Missing relationship {ident} in {source_uri}')
            if rel['type'] not in {_R+'/image', _R+'/hyperlink'}:
                raise NotImplementedError('Unsupported imported relationship: '+rel['type'])
            target = rel['target']
            if rel['target_mode'] == 'Internal':
                if rel['type'] != _R+'/image': raise NotImplementedError('Internal hyperlink package targets are unsupported')
                target = self.source.package.relationship_part(source_uri, ident)
                if target not in self.images:
                    if self.source.package.relationships(target): raise NotImplementedError('Image has additional package dependencies')
                    stem, extension = splitext(dirname(self.destination.main.uri)+'/media/'+basename(target))
                    name, index = stem+extension, 0
                    while name.lower() in self.part_names:
                        index += 1
                        name = f'{stem}_{index}{extension}'
                    self.part_names.add(name.lower())
                    self.images[target] = name, self.source.package.content_type(target), self.source.package.read_part(target)
                target = self.images[target][0]
            self.relationships[key] = rel, target, []
        self.relationships[key][2].append((element, uri, local))

    def scan(self, root, source_uri, target_kind):
        for e in _walk(root):
            uri, local = e.qname
            if _revision_name(e) or uri == _W and local in _UNSUPPORTED:
                raise NotImplementedError('Unsupported imported structure: '+local)
            if uri == 'urn:schemas-microsoft-com:vml' and local == 'shape':
                raise NotImplementedError('VML shape identity/dependencies are unsupported')
            if uri == _NS['a'] and local in {'stCxn', 'endCxn'}:
                raise NotImplementedError('Drawing connector identities are unsupported')
            if uri == _W:
                value = e.attribute(_W, 'val')
                if local in _STYLE_REFS:
                    if value is None: raise ValueError('Style reference has no value')
                    _attribute(e, 'val', self.style(value))
                elif local == 'numId': _attribute(e, 'val', self.num(value))
                elif local == 'abstractNumId': _attribute(e, 'val', self.abstract(value))
                elif local == 'nsid': _attribute(e, 'val', _allocate(self.ids['nsid'], True))
                elif local in {'bookmarkStart', 'bookmarkEnd'}:
                    key = source_uri, int(e.attribute(_W, 'id'))
                    if key not in self.bookmarks: self.bookmarks[key] = [_allocate(self.ids['bookmark']), None, 0, 0]
                    bookmark = self.bookmarks[key]
                    _attribute(e, 'id', bookmark[0])
                    bookmark[2 if local == 'bookmarkStart' else 3] += 1
                    if local == 'bookmarkStart':
                        name = e.attribute(_W, 'name')
                        if not name: raise ValueError('Bookmark has no name')
                        new_name = _fresh(name, self.bookmark_names, 40)
                        bookmark[1] = name, new_name
                        _attribute(e, 'name', new_name)
                elif local == 'hyperlink' and e.attribute(_W, 'anchor') is not None and e.attribute(_R, 'id') is None:
                    self.anchors.append((e, e.attribute(_W, 'anchor')))
            if (uri, local) in {(_WP, 'docPr'), (_PIC, 'cNvPr')}:
                e.set_attribute('', 'id', _allocate(self.ids['drawing']))
            if e.attribute(_W14, 'paraId') is not None: e.set_attribute(_W14, 'paraId', _allocate(self.ids['paragraph'], True))
            for attr_uri, attr_local, value in e.raw['attributes']:
                if attr_uri == _R or attr_uri == _O and attr_local == 'relid':
                    self.relationship(e, source_uri, target_kind, attr_uri, attr_local, value)

    def finish(self, index):
        names = {}
        for _, name, starts, ends in self.bookmarks.values():
            if starts != 1 or ends != 1: raise NotImplementedError('Import must include complete, unique bookmark ranges')
            old, new = name
            if old in names: raise ValueError('Ambiguous imported bookmark name')
            names[old] = new
        for element, name in self.anchors:
            if name not in names: raise NotImplementedError('Internal hyperlink target is outside the imported content')
            _attribute(element, 'anchor', names[name])
        # All source dependencies/refusals were checked while staging only the selected subtrees.
        targets = {'content': (self.parent, self.target_uri)}
        for kind, part_name in [('styles', 'StyleDefinitionsPart'), ('numbering', 'NumberingDefinitionsPart')]:
            if any(k == kind for k, _, _ in self.entries):
                part = self.destination._part(part_name, True)
                targets[kind] = part.xml.root, part.uri
        for name, content_type, data in self.images.values(): self.destination.package.add_part(name, content_type, data)
        for (_, target_kind, _), (rel, target, refs) in self.relationships.items():
            ident = self.destination.package.add_relationship(targets[target_kind][1], rel['type'], target, rel['target_mode'])
            for element, uri, local in refs: element.set_attribute(uri, local, ident)
        result = []
        for kind, tree, _ in self.entries:
            parent = targets[kind][0]
            if kind == 'content':
                result.append(tree.root.copy_to(parent, index))
                index += 1
            else: tree.root.copy_to(parent, _position(parent, tree.root.qname[1]))
        return result

def import_content(source, elements, destination, parent, index=None):
    """Import paragraphs/tables and their explicit styles, numbering, images and links.

    Bookmarks must be complete within this call; review/section/field/other package
    dependencies are refused. Existing destination theme/document defaults apply.
    """
    if source is destination: raise ValueError('Use copy_to for copies within one document')
    elements = [elements] if isinstance(elements, Element) else list(elements)
    if not elements: return []
    destination.package._owner(parent)
    if parent.qname not in {(_W, n) for n in ('body', 'hdr', 'ftr', 'tc')}:
        raise ValueError('Import destination must be a body, header, footer or table cell')
    _context(parent)
    for element in elements:
        source.package._owner(element)
        if element.qname not in {(_W, 'p'), (_W, 'tbl')}: raise ValueError('Import accepts paragraph/table blocks')
        _context(element)
    xml = parent._tree.xml
    if index is None:
        section = _child(parent, 'sectPr')
        index = xml.children(parent.node_id).index(section.node_id) if section is not None else xml.child_count(parent.node_id)
    if not isinstance(index, int) or not 0 <= index <= xml.child_count(parent.node_id): raise ValueError('Invalid import index')
    if parent.qname == (_W, 'tc') and index == xml.child_count(parent.node_id) and elements[-1].qname != (_W, 'p'):
        raise ValueError('A table cell must end in a paragraph; insert before its final paragraph')
    # Prevent copying a block twice via overlapping parent/child selections.
    selected = {(id(e._tree), e.node_id) for e in elements}
    if len(selected) != len(elements): raise ValueError('Import selection contains duplicate blocks')
    for element in elements:
        ancestor = element.parent
        while ancestor is not None:
            if (id(ancestor._tree), ancestor.node_id) in selected: raise ValueError('Import selection contains nested blocks')
            ancestor = ancestor.parent
    operation = _Import(source, destination, parent)
    for element in elements:
        copied, uri = operation._copy(element, 'content')
        operation.scan(copied, uri, 'content')
    return operation.finish(index)
