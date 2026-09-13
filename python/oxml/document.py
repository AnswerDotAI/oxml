"""DOCX package access and typed XML over one mutable state per loaded part."""
from pathlib import Path
from posixpath import dirname
from functools import cache
from collections import deque
from . import _core
from .model import Tree, metadata, _expanded

_TARGETS = {t['version'] for t in metadata['types'].values()}

@cache
def _root_name(name):
    info = metadata['parts'][name]
    local = info.get('RootElement')
    if info.get('Root'): return info['Root'].split(':')[0]+':'+local
    names = {t['qname'] for t in metadata['types'].values() if t['qname'].split(':')[-1] == local}
    if len(names) != 1: raise NotImplementedError(f'No unambiguous XML root descriptor for {info["Name"]}')
    return names.pop()

def _part_rules(name):
    parts = metadata['parts']
    return {parts[c['Name']]['RelationshipType']: (c, parts[c['Name']])
            for c in parts.get(name, {}).get('Children', []) if c['Name'] in parts}

def _rel_type(value):
    return value.replace('http://purl.oclc.org/ooxml/officeDocument/relationships/',
                         'http://schemas.openxmlformats.org/officeDocument/2006/relationships/', 1)

def _available(info, target):
    version = info.get('Version', 'Office2007')
    return target == 'Microsoft365' or version != 'Microsoft365' and version <= target

class Part:
    def __init__(self, package, uri):
        self._package, self.uri = package, uri
        self._version = package._versions.get(uri, 0)

    def _check(self):
        if self._version != self._package._versions.get(self.uri, 0): raise ReferenceError('Package part was removed or replaced')

    @property
    def content_type(self):
        self._check()
        return self._package.content_type(self.uri)

    @property
    def xml(self):
        self._check()
        return self._package._tree(self.uri)

    def read_bytes(self):
        self._check()
        return self._package.read_part(self.uri)

    def replace(self, data):
        self._check()
        return self._package.replace_part(self.uri, data)

class Package:
    def __init__(self, data: bytes):
        self._native = _core.Package(data)
        self._xml, self._versions = {}, {}

    @classmethod
    def open(cls, path): return cls(Path(path).read_bytes())

    @classmethod
    def new(cls):
        package = cls.__new__(cls)
        package._native, package._xml, package._versions = _core.Package.new(), {}, {}
        return package

    @property
    def main_part(self): return self._native.main_part

    def part_names(self): return self._native.part_names()

    def part(self, uri):
        return Part(self, self._native.resolve_part(uri))

    def content_type(self, uri): return self._native.content_type(uri)

    def set_content_type(self, uri, content_type): self._native.set_content_type(uri, content_type)

    def _tree(self, uri):
        uri = self._native.resolve_part(uri)
        if uri.lower() == '/[content_types].xml' or '/_rels/' in uri.lower() and uri.lower().endswith('.rels'):
            raise ValueError('Use scoped content-type and relationship APIs for package metadata')
        if uri not in self._xml: self._xml[uri] = (Tree(self._native.read_part(uri)), 0)
        return self._xml[uri][0]

    def _owner(self, element):
        element.node_id
        for uri, (tree, _) in self._xml.items():
            if tree is element._tree: return uri
        raise ValueError('Element does not belong to this package')

    def read_part(self, uri):
        uri = self._native.resolve_part(uri)
        return self._xml[uri][0].bytes() if uri in self._xml else self._native.read_part(uri)

    def _invalidate(self, uri):
        cached = self._xml.pop(uri, None)
        if cached is not None: cached[0].xml.invalidate()
        self._versions[uri] = self._versions.get(uri, 0) + 1

    def add_part(self, uri, content_type, data):
        self._native.add_part(uri, content_type, data)
        uri = self._native.resolve_part(uri)
        self._invalidate(uri)
        return self.part(uri)

    def replace_part(self, uri, data):
        uri = self._native.resolve_part(uri)
        self._native.replace_part(uri, data)
        self._invalidate(uri)
        return self.part(uri)

    def remove_part(self, uri):
        uri = self._native.resolve_part(uri)
        self._native.remove_part(uri)
        self._invalidate(uri)

    def relationships(self, source_uri='/'): return self._native.relationships(source_uri)

    def relationship_part(self, source_uri, relationship_id): return self._native.relationship_part(source_uri, relationship_id)

    def add_relationship(self, source_uri, relationship_type, target, target_mode='Internal', relationship_id=None):
        return self._native.add_relationship(source_uri, relationship_type, target, target_mode, relationship_id)

    def remove_relationship(self, source_uri, relationship_id): self._native.remove_relationship(source_uri, relationship_id)

    def _flush(self):
        for uri, (tree, revision) in list(self._xml.items()):
            if tree.xml.revision != revision:
                self._native.replace_part(uri, tree.bytes())
                self._xml[uri] = (tree, tree.xml.revision)

    def bytes(self):
        self._flush()
        return self._native.bytes()

    def save(self, path):
        self._flush()
        self._native.save(str(path))

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

    def bytes(self): return self.package.bytes()

    def save(self, path): self.package.save(path)

    def set_custom_xml(self, item_id, data, *, schema_uri=None):
        """Set a datastore's XML bytes by GUID, creating it when absent; return its Part.

        Existing properties and unrelated stores are retained. `schema_uri` is used only on creation.
        """
        from .build import E
        package, main = self.package, self.main.uri
        item_info, props_info = metadata['parts']['CustomXmlPart'], metadata['parts']['CustomXmlPropertiesPart']
        item_id = item_id.upper()
        for rel in package.relationships(main):
            if rel['type'] != item_info['RelationshipType'] or rel['target_mode'] == 'External': continue
            item = package.relationship_part(main, rel['id'])
            for rel in package.relationships(item):
                if rel['type'] != props_info['RelationshipType'] or rel['target_mode'] == 'External': continue
                props = package.part(package.relationship_part(item, rel['id'])).xml.root
                if (props.attribute(metadata['namespaces']['ds'], 'itemID') or '').upper() == item_id:
                    return package.replace_part(item, data)
        names, n = {p.lower() for p in package.part_names()}, 1
        while f'/customxml/item{n}.xml' in names or f'/customxml/itemprops{n}.xml' in names: n += 1
        item, props = f'/customXml/item{n}.xml', f'/customXml/itemProps{n}.xml'
        ds = E('ds', attr_ns='ds')
        properties = ds.datastoreItem(
            ds.schemaRefs(ds.schemaRef(uri=schema_uri)) if schema_uri is not None else None, itemID=item_id).bytes()
        part = package.add_part(item, 'application/xml', data)
        package.add_part(props, props_info['ContentType'], properties)
        package.add_relationship(item, props_info['RelationshipType'], props)
        package.add_relationship(main, item_info['RelationshipType'], item)
        return part

    def _part(self, name, create=False):
        """Find or create one declared XML part related directly to the main document."""
        from .build import E
        info, main = metadata['parts'][name], self.main.uri
        relations = [r for r in self.package.relationships(main) if r['type'] == info['RelationshipType']]
        if len(relations) > 1: raise ValueError(f'Ambiguous {name} relationship')
        if not relations and not create: return None
        qname = _root_name(name)
        if not relations:
            base, number = dirname(main)+'/'+info['Target'], 0
            names = {n.lower() for n in self.package.part_names()}
            uri = base+'.xml'
            while uri.lower() in names:
                number += 1
                uri = f'{base}{number}.xml'
            part = self.package.add_part(uri, info['ContentType'], E()(qname).bytes())
            self.package.add_relationship(main, info['RelationshipType'], uri)
        else:
            rel = relations[0]
            if rel['target_mode'] != 'Internal': raise ValueError(f'{name} must be an internal part')
            part = self.package.part(self.package.relationship_part(main, rel['id']))
        prefix, local = qname.split(':')
        if part.content_type != info['ContentType'] or part.xml.root.qname != (metadata['namespaces'][prefix], local):
            raise ValueError(f'Unexpected {name} part structure')
        return part

    def _parts(self):
        """One read-only walk of reachable package relationships; no XML payload parsing."""
        result, pending = {}, deque([('/', 'WordprocessingDocument')])
        main, parts = self.main.uri, metadata['parts']
        while pending:
            uri, name = pending.popleft()
            if uri in result: continue
            entry = result[uri] = {'name': name, 'relationships': [], 'errors': []}
            try: relationships = self.package.relationships(uri)
            except ValueError as e:
                entry['errors'].append({'part_uri': uri, 'error': str(e)})
                continue
            rules = _part_rules(name)
            for original in relationships:
                rel = {**original, 'type': _rel_type(original['type']), 'part_uri': None, 'part_name': None}
                entry['relationships'].append(rel)
                if rel['type'] in rules: rel['part_name'] = rules[rel['type']][1]['Name']
                if rel['target_mode'] == 'External': continue
                try: rel['part_uri'] = self.package._native.relationship_target(uri, rel['target'])
                except KeyError as e:
                    entry['errors'].append({'part_uri': uri, 'relationship_id': rel['id'], 'target': rel['target'], 'error': str(e)})
                    continue
                if rel['part_uri'] == main: rel['part_name'] = 'MainDocumentPart'
                if rel['part_name'] is None:
                    content_type = self.package.content_type(rel['part_uri'])
                    candidates = [n for n, i in parts.items() if i.get('RelationshipType') == rel['type'] and i.get('ContentType') == content_type]
                    if len(candidates) == 1: rel['part_name'] = candidates[0]
                pending.append((rel['part_uri'], rel['part_name']))
        return result

    def stories(self, view='current'):
        """Separate reachable stories, with each note/comment retaining its own element and part URI."""
        from .text import Story
        containers = {'MainDocumentPart': None, 'HeaderPart': None, 'FooterPart': None,
                      'FootnotesPart': 'footnote', 'EndnotesPart': 'endnote', 'WordprocessingCommentsPart': 'comment'}
        for uri, entry in self._parts().items():
            if any('relationship_id' not in e for e in entry['errors']): raise ValueError(f'Cannot read relationships from {uri}')
            for rel in entry['relationships']:
                if rel['part_name'] in containers and rel['part_uri'] is None:
                    raise ValueError(f'Cannot read story target {rel["target"]} from {uri}')
            name = entry['name']
            if name not in containers: continue
            root = self.package.part(uri).xml.root
            local = containers[name]
            elements = [root] if local is None else [e for e in root.children if e.qname == (metadata['namespaces']['w'], local)]
            for element in elements: yield Story(element, view=view, part_uri=uri)

    def validate(self, target='Microsoft365'):
        """Validate reachable declared XML parts and their scoped package relationships."""
        if target not in _TARGETS: raise ValueError('unknown validation target')
        entries, parts, trees, issues, incomplete, gaps, invalid_parts = self._parts(), metadata['parts'], {}, [], [], set(), set()

        def issue(uri, rule, expected, actual):
            issues.append({'rule_id': rule, 'category': 'package', 'severity': 'error', 'node': None,
                'part_uri': uri, 'target': target, 'expected': expected, 'actual': actual,
                'rule_provenance': {'path': f'data/parts/{entries[uri]["name"]}.json'}})

        for uri, entry in entries.items():
            name, rules, counts = entry['name'], _part_rules(entry['name']), {}
            for error in entry['errors']:
                incomplete.append(error)
                issue(uri, 'relationship-target' if 'target' in error else 'relationship-xml', 'readable internal relationships', error)
            for rel in entry['relationships']:
                if rel['target_mode'] == 'External' or rel['part_uri'] is None: continue
                if rel['type'] not in rules:
                    if name is not None and any(i.get('RelationshipType') == rel['type'] and _available(i, target) for i in parts.values()):
                        issue(uri, 'part-not-allowed', f'a permitted {name} relationship', rel)
                    continue
                rule, info = rules[rel['type']]
                if not _available(info, target): continue
                counts[rel['type']] = counts.get(rel['type'], 0) + 1
                actual = self.package.content_type(rel['part_uri'])
                if rule.get('HasFixedContent') and info.get('ContentType') != actual:
                    issue(uri, 'part-content-type', info.get('ContentType'), {'part_uri': rel['part_uri'], 'content_type': actual})
                    invalid_parts.add(rel['part_uri'])
            for rel_type, (rule, info) in rules.items():
                if not _available(info, target): continue
                count = counts.get(rel_type, 0)
                if count > 1 and not rule.get('MaxOccursGreatThanOne'): issue(uri, 'part-cardinality', 'at most one '+rel_type, count)
                if not count and rule.get('MinOccursIsNonZero'): issue(uri, 'part-required', 'at least one '+rel_type, count)

        for uri, entry in entries.items():
            if uri == '/': continue
            info = parts.get(entry['name'], {})
            if not _available(info, target) or uri in invalid_parts:
                gaps.add('unchecked-part:'+uri)
                continue
            if not (info.get('Root') or info.get('RootElement')):
                if not info: gaps.add('opaque-part:'+uri)
                continue
            try:
                tree = self.package.part(uri).xml
                trees[uri] = tree
                try: expected = _expanded(_root_name(entry['name']))
                except NotImplementedError: gaps.add('unknown-part-root:'+uri)
                else:
                    actual = tree.root.qname
                    if actual[0].startswith('http://purl.oclc.org/ooxml/'):
                        gaps.add('strict-part-root:'+uri)
                    elif actual != expected: issue(uri, 'part-root', expected, actual)
            except ValueError as e:
                incomplete.append({'part_uri': uri, 'error': str(e)})
                issue(uri, 'part-xml', 'well-formed XML', str(e))

        def dependency(uri, path):
            if path == '.': return uri
            if path == '..':
                return next((u for u, e in entries.items() if u != '/' and any(r['part_uri'] == uri for r in e['relationships'])), None)
            if path.startswith('/'): uri = '/'
            for name in path.strip('/').split('/'):
                uri = next((r['part_uri'] for r in entries.get(uri, {}).get('relationships', []) if r['part_name'] == name), None)
                if uri is None: break
            return uri

        paths = {r['Test'].split("document('Part:")[1].split("')")[0]
                 for r in metadata['semantics'] if "document('Part:" in r['Test']}
        coverage = {'schema_nodes_checked': 0, 'semantic_checks': 0, 'skipped_regions': [], 'complete': False}
        for uri, tree in trees.items():
            dependencies = {p: trees[d] for p in paths if (d := dependency(uri, p)) in trees}
            relationships = {r['id']: r['type'] for r in entries[uri]['relationships']}
            report = tree.validate(target, dependencies, uri,
                relationships=relationships, complete_dependencies=not incomplete and not invalid_parts)
            issues.extend(report['issues'])
            for key in ('schema_nodes_checked', 'semantic_checks'): coverage[key] += report['coverage'][key]
            coverage['skipped_regions'].extend({**r, 'part_uri': uri} for r in report['coverage']['skipped_regions'])
            gaps.update(report['coverage']['gaps'])
        coverage.update(gaps=sorted(gaps), incomplete_dependencies=incomplete)
        report = {'issues': issues, 'target': target, 'source': metadata['source'], 'coverage': coverage,
                  'scope': {'part_uris': list(trees)}}
        return report
