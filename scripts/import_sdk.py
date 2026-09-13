"""Import the pinned SDK declarations used by oxml, without .NET or a separate audit pipeline."""

import argparse
import io
import json
import re
import subprocess
import tarfile
from pathlib import Path


REPO = 'https://github.com/dotnet/Open-XML-SDK'
HEAD = '431ab05cf160248cc3885a4a766026d4f8243792'
VERSIONS = 'None Office2007 Office2010 Office2013 Office2016 Office2019 Office2021 Microsoft365'.split()
KEYS = {
    'schema': 'TargetNamespace Types Enums',
    'type': 'Name ClassName Summary Part CompositeType BaseClass IsAbstract IsDerived IsLeafText IsLeafElement Version Attributes Children AdditionalElements Validators Particle',
    'attribute': 'QName PropertyName Type Version PropertyComments Validators',
    'child': 'Name PropertyName PropertyComments',
    'particle': 'Name Kind Namespace Occurs InitialVersion Items RequireFilter',
    'occurrence': 'Min Max IncludeVersion Version',
    'validator': 'Arguments Type IsList Name Version UnionId IsInitialVersion',
    'argument': 'Type Name Value',
    'enum': 'Type Name Summary Facets Version',
    'facet': 'Version Comments Value Name',
    'part': 'Name Base ContentType RelationshipType Target Version Root RootElement Extension Paths Children',
    'part_child': 'MinOccursIsNonZero MaxOccursGreatThanOne IsDataPartReference ApiName Name HasFixedContent IsSpecialEmbeddedPart',
    'paths': 'General Word Excel PowerPoint',
    'namespace': 'Prefix Uri Version',
    'typed_namespace': 'Prefix Namespace',
    'typed': 'Name ClassName PartClassName',
    'semantic': 'Context Test Version App',
}
NESTED = {
    'schema': {'Types': 'type', 'Enums': 'enum'},
    'type': {'Attributes': 'attribute', 'Children': 'child', 'Validators': 'validator', 'Particle': 'particle'},
    'attribute': {'Validators': 'validator'}, 'particle': {'Occurs': 'occurrence', 'Items': 'particle'},
    'validator': {'Arguments': 'argument'}, 'enum': {'Facets': 'facet'},
    'part': {'Paths': 'paths', 'Children': 'part_child'},
}
VALUES = {
    ('particle', 'Kind'): 'Element Any Choice Sequence All Group'.split(),
    ('type', 'CompositeType'): 'Other OneSequence OneChoice OneAll'.split(),
    ('validator', 'Name'): 'EnumValidator NumberValidator OfficeVersionValidator RequiredValidator StringValidator'.split(),
    ('argument', 'Type'): 'None Type Integer Version Long Double Boolean String'.split(),
    ('argument', 'Name'): 'IsId IsNcName IsNonNegative IsPositive IsQName IsRequired IsToken IsUri Length MaxExclusive MaxInclusive MaxLength MinExclusive MinInclusive MinLength Pattern'.split(),
    ('semantic', 'App'): 'All Word Excel PowerPoint'.split(),
}
SIMPLE_TYPES = set('Base64BinaryValue BooleanValue ByteValue DateTimeValue DecimalValue DoubleValue HexBinaryValue Int16Value Int32Value Int64Value IntegerValue OnOffValue SingleValue StringValue TrueFalseValue TrueFalseBlankValue UInt16Value UInt32Value UInt64Value SByteValue'.split())
ENGINE_BASES = set('OpenXmlElement OpenXmlCompositeElement OpenXmlLeafElement OpenXmlLeafTextElement OpenXmlPartRootElement'.split())
SIMPLE_SOURCE = 'gen/DocumentFormat.OpenXml.Generator.Models/Generators/Elements/SimpleTypeExtensions.cs'
# Literal attributes in src/DocumentFormat.OpenXml/Schema/Wordprocessing/CustomXmlElement.cs, ConfigureMetadata.
# Other inherited base knowledge remains explicitly unresolved; this is not a C# metadata extractor.
CUSTOM_XML_ATTRIBUTES = [
    {'QName': ':uri', 'PropertyName': 'Uri', 'Type': 'StringValue'},
    {'QName': 'w:element', 'PropertyName': 'Element', 'Type': 'StringValue',
     'Validators': [{'Name': 'StringValidator', 'Arguments': [{'Name': 'IsNcName', 'Type': 'Boolean', 'Value': 'True'}]}]},
]


def git(repo, *args, binary=False):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=not binary, timeout=60)


def snapshot(repo, revision):
    """Read committed files, never the checkout, and never alter the reference repo."""
    commit, date, subject = git(repo, 'show', '-s', '--format=%H%n%cs%n%s', revision).strip().split('\n', 2)
    archive = git(repo, 'archive', commit, 'data', SIMPLE_SOURCE, 'LICENSE', binary=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        files = {m.name: tar.extractfile(m).read() for m in tar if m.isfile() and (m.name.endswith(('.json', '.cs')) or m.name == 'LICENSE')}
    return {'commit': commit, 'date': date, 'subject': subject, 'repo': REPO, 'license': 'MIT'}, files


class Inputs:
    def unknown(self, source, construct, value):
        raise ValueError(f'{source}: {construct}: {value!r}')

    def check(self, obj, kind, source):
        if not isinstance(obj, dict):
            self.unknown(source, kind + ':expected_object', obj)
        for key, value in obj.items():
            loc = f'{source}/{key}'
            if key not in KEYS[kind].split(): self.unknown(loc, kind + ':unknown_field', value)
            allowed = VERSIONS if key in ('Version', 'InitialVersion') else VALUES.get((kind, key))
            values = value.split(', ') if kind == 'semantic' and key == 'App' else [value]
            if allowed is not None and any(v not in allowed for v in values): self.unknown(loc, kind + ':unknown_value', value)
            if kind == 'attribute' and key == 'Type':
                root = value.split('<')[0]
                if root not in SIMPLE_TYPES | {'EnumValue', 'ListValue'}: self.unknown(loc, 'unknown_simple_type', value)
            nested = NESTED.get(kind, {}).get(key)
            if nested:
                if isinstance(value, list):
                    for i, item in enumerate(value): self.check(item, nested, f'{loc}/{i}')
                else: self.check(value, nested, loc)

    def read(self, path, content, kind):
        value = json.loads(content)
        if isinstance(value, list):
            for i, item in enumerate(value): self.check(item, kind, f'{path}#/{i}')
        else: self.check(value, kind, path + '#')
        return value


def particles(particle):
    if particle:
        yield particle
        for item in particle.get('Items', []): yield from particles(item)


def simple_types(content, inv):
    """The SDK's primitive map is a literal table in C#, not in its JSON files."""
    text, result = content.decode('utf-8-sig'), {}
    constants = dict(re.findall(r'private const string (\w+) = "([^"]+)";', text))
    table = text.split('_mapping = new Dictionary<string, string>()', 1)[1].split('};', 1)[0]
    for line in table.splitlines():
        if line.strip() in ('', '{'): continue
        match = re.fullmatch(r'\s*\{ "([^"]+)", (\w+|"[^"]+") \},\s*', line)
        if not match:
            inv.unknown(SIMPLE_SOURCE, 'unknown_primitive_mapping_syntax', line)
        name, value = match.groups()
        if value.startswith('"'): result[name] = value.strip('"')
        elif value in constants: result[name] = constants[value]
        else: inv.unknown(SIMPLE_SOURCE, 'unknown_primitive_mapping_constant', value)
    return result


def import_snapshot(source, files):
    inv, namespaces, typed_ns, types, enums, parts, semantics = Inputs(), {}, {}, {}, {}, {}, []
    raw_types, typed_names, local_classes, enum_sources = {}, {}, {}, {}
    for path, content in sorted(files.items()):
        if not path.startswith('data/') or not path.endswith('.json'): continue
        if path == 'data/namespaces.json':
            namespaces = {n['Prefix']: n['Uri'] for n in inv.read(path, content, 'namespace')}
        elif path == 'data/typed/namespaces.json':
            typed_ns = {n['Prefix']: n['Namespace'] for n in inv.read(path, content, 'typed_namespace')}
        elif path.startswith('data/typed/'):
            for obj in inv.read(path, content, 'typed'): typed_names[obj['Name']] = obj
        elif path.startswith('data/schemas/'):
            schema = inv.read(path, content, 'schema')
            for i, obj in enumerate(schema.get('Types', [])):
                name, location = obj['Name'], {'path': path, 'pointer': f'/Types/{i}'}
                if name in raw_types and raw_types[name][0] != obj: inv.unknown(location, 'conflicting_contextual_type', name)
                raw_types[name] = (obj, location)
                local_classes[(path, obj.get('ClassName'))] = name
            for obj in schema.get('Enums', []):
                if obj['Type'] in enums: inv.unknown(path, 'duplicate_enum', obj['Type'])
                enums[obj['Type']] = obj
                enum_sources[obj['Type']] = {'path': path, 'target_namespace': schema['TargetNamespace']}
        elif path.startswith('data/parts/'):
            obj = inv.read(path, content, 'part')
            parts[obj['Name']] = obj
        elif path == 'data/schematrons.json': semantics = inv.read(path, content, 'semantic')
        else: inv.unknown(path, 'unknown_input_file', None)
    class_index = {}
    for name, (obj, _) in raw_types.items():
        cls = typed_names.get(name, {}).get('ClassName', obj.get('ClassName'))
        prefix = (name.split('/', 1)[1] or name.split('/', 1)[0]).split(':', 1)[0]
        class_index[(typed_ns.get(prefix), cls)] = name
    active = set()

    def build(name):
        if name in types: return types[name]
        if name in active: raise ValueError(f'Inheritance cycle: {name}')
        active.add(name)
        obj, location = raw_types[name]
        prefix = (name.split('/', 1)[1] or name.split('/', 1)[0]).split(':', 1)[0]
        base = obj.get('BaseClass')
        base_ns, base_cls = base.rsplit('.', 1) if base and '.' in base else (typed_ns.get(prefix), base)
        base_id = local_classes.get((location['path'], base)) or class_index.get((base_ns, base_cls))
        parent = build(base_id) if base_id else {}
        attrs = {a['QName']: a for a in parent.get('attributes', [])}
        if base_cls == 'CustomXmlElement': attrs.update({a['QName']: a for a in CUSTOM_XML_ATTRIBUTES})
        for a in obj.get('Attributes', []):
            local = a['QName'].split(':')[-1]
            attrs[a['QName']] = {'PropertyName': local[:1].upper() + local[1:], **a}
        child_ids = set(parent.get('children', [])) | set(obj.get('AdditionalElements', []))
        child_ids.update(c['Name'] for c in obj.get('Children', []))
        child_ids.update(p['Name'] for p in particles(obj.get('Particle')) if p.get('Name'))
        result = {
            'class_name': typed_names.get(name, {}).get('ClassName', obj.get('ClassName')),
            'namespace': typed_ns.get(prefix), 'qname': name.split('/', 1)[1],
            'base': base_id, 'unresolved_base': base if base and not base_id and base not in ENGINE_BASES else None,
            'is_abstract': obj.get('IsAbstract', False),
            'is_leaf': obj.get('IsLeafElement', parent.get('is_leaf', False)) or obj.get('IsLeafText', parent.get('is_text', False)),
            'is_text': obj.get('IsLeafText', parent.get('is_text', False)), 'version': obj.get('Version', 'Office2007'),
            'attributes': list(attrs.values()), 'children': sorted(child_ids),
            'particle': obj.get('Particle', parent.get('particle')), 'source': location, 'validators': obj.get('Validators', []),
        }
        types[name] = result
        active.remove(name)
        return result

    for name in sorted(raw_types): build(name)
    for name, obj in types.items():
        for child in obj['children']:
            if child not in types: inv.unknown(obj['source'], 'unresolved_child', child)
    primitives = simple_types(files[SIMPLE_SOURCE], inv)
    for name, obj in enums.items():
        prefix = next(p for p, uri in namespaces.items() if uri == enum_sources[name]['target_namespace'])
        obj.update(namespace=typed_ns[prefix], full_name=f"{typed_ns[prefix]}.{obj['Name']}", source=enum_sources[name])
    for name, (obj, location) in raw_types.items():
        for value in [obj, *obj.get('Attributes', [])]:
            for validator in value.get('Validators', []):
                qname = validator.get('Type')
                if qname and qname not in primitives and qname not in enums: inv.unknown(location, 'unknown_validator_simple_type', qname)
    return {'source': source, 'namespaces': namespaces, 'types': types, 'enums': enums,
            'parts': parts, 'semantics': semantics, 'simple_types': primitives}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sdk', type=Path, default=Path('links/Open-XML-SDK'))
    parser.add_argument('--revision', default=HEAD)
    parser.add_argument('--output', type=Path, default=Path('schema/metadata.json'))
    args = parser.parse_args()
    source, files = snapshot(args.sdk, args.revision)
    descriptor = import_snapshot(source, files)
    args.output.write_text(json.dumps(descriptor, sort_keys=True, ensure_ascii=False, separators=(',', ':')) + '\n')
    print(f"Wrote {args.output}: {len(descriptor['types'])} types from SDK {source['commit']}")


if __name__ == '__main__': main()
