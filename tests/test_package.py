"""Package tests use unchanged curated originals and explicitly constructed ZIP derivatives."""
from io import BytesIO
from contextlib import nullcontext
from pathlib import Path
from struct import pack_into, unpack_from
from zipfile import ZIP_STORED, ZipFile, ZipInfo
import pytest
from oxml._core import Package

FIXTURES = Path(__file__).parent / 'fixtures'
REL_NS = 'http://schemas.openxmlformats.org/package/2006/relationships'
REL_TYPE = 'https://example.org/opaque'


def entries(data):
    with ZipFile(BytesIO(data)) as z: return {n: z.read(n) for n in z.namelist()}


def rewrite(data, updates=(), extra=()):
    """Named derivatives only: copy original entries and apply explicit payload edits."""
    result = BytesIO()
    with ZipFile(BytesIO(data)) as source, ZipFile(result, 'w') as target:
        target.comment = source.comment
        for info in source.infolist(): target.writestr(info, dict(updates).get(info.filename, source.read(info)))
        for name, value in extra: target.writestr(name, value)
    return result.getvalue()


@pytest.mark.parametrize('fixture', sorted(FIXTURES.rglob('*.docx')), ids=lambda p: p.name)
def test_original_noop_is_archive_identical(fixture, tmp_path):
    original = fixture.read_bytes()
    package = Package(original)
    assert package.main_part == '/word/document.xml'
    assert package.bytes() == original
    assert {n[1:]: package.read_part(n) for n in package.part_names()} == {n: v for n, v in entries(original).items() if not n.endswith('/')}
    package.replace_part(package.main_part, package.read_part(package.main_part))
    package.save(str(tmp_path / fixture.name))
    assert (tmp_path / fixture.name).read_bytes() == original


def test_create_edit_and_preserve_opaque_payloads(tmp_path):
    package = Package.new()
    package.add_part('/word/embeddings/object.bin', 'application/octet-stream', bytes(range(256)))
    package.add_part('/unknown.xml', 'application/x-unknown+xml', b'<?pi x?><opaque><!--retain--></opaque>')
    package.set_content_type('/unknown.xml', 'application/vnd.example+xml')
    assert package.content_type('/unknown.xml') == 'application/vnd.example+xml'
    baseline = package.bytes()
    package = Package(baseline)
    changed = package.read_part(package.main_part).replace(b'<w:body/>', b'<w:body><w:p/></w:body>')
    package.replace_part(package.main_part, changed)
    package.save(str(tmp_path / 'created.docx'))
    result = (tmp_path / 'created.docx').read_bytes()
    before, after = entries(baseline), entries(result)
    assert after.pop('word/document.xml') == changed
    before.pop('word/document.xml')
    assert after == before
    assert Package(result).read_part('/WORD/EMBEDDINGS/OBJECT.BIN') == bytes(range(256))


@pytest.mark.parametrize('strict', [False, True])
def test_main_discovery_uses_relationship_not_filename(strict):
    data = entries(Package.new().bytes())
    data['alternate/main.xml'] = data.pop('word/document.xml')
    data['[Content_Types].xml'] = data['[Content_Types].xml'].replace(b'/word/document.xml', b'/alternate/main.xml')
    data['_rels/.rels'] = data['_rels/.rels'].replace(b'word/document.xml', b'alternate/main.xml')
    if strict:
        data['_rels/.rels'] = data['_rels/.rels'].replace(b'http://schemas.openxmlformats.org/officeDocument/2006/relationships/', b'http://purl.oclc.org/ooxml/officeDocument/relationships/')
        data['alternate/main.xml'] = data['alternate/main.xml'].replace(b'http://schemas.openxmlformats.org/wordprocessingml/2006/main', b'http://purl.oclc.org/ooxml/wordprocessingml/main')
    result = BytesIO()
    with ZipFile(result, 'w') as z:
        for name, value in data.items(): z.writestr(name, value)
    package = Package(result.getvalue())
    assert package.main_part == '/alternate/main.xml'
    assert package.bytes() == result.getvalue()


def test_relationship_scopes_removal_and_metadata_preservation():
    original = Package.new().bytes()
    ct = entries(original)['[Content_Types].xml'].replace(b'</Types>', b'<!--keep--><?retain data?><x:extra xmlns:x="urn:opaque">A &amp; B</x:extra></Types>')
    package = Package(rewrite(original, [('[Content_Types].xml', ct)]))
    package.add_part('/custom/a.bin', 'application/octet-stream', b'a')
    package.add_part('/custom/b.bin', 'application/octet-stream', b'b')
    package.add_relationship('/word/document.xml', REL_TYPE, '../custom/a.bin', relationship_id='same')
    package.add_relationship('/custom/b.bin', REL_TYPE, 'a.bin', relationship_id='same')
    package.add_relationship('/custom/a.bin', REL_TYPE, 'https://example.org/no-fetch', 'External', 'a·1')
    assert package.relationships('/custom/a.bin') == [{'id': 'a·1', 'type': REL_TYPE, 'target': 'https://example.org/no-fetch', 'target_mode': 'External'}]
    assert package.relationship_part('/custom/a.bin', 'a·1') is None
    assert package.relationship_part('/custom/b.bin', 'same') == '/custom/a.bin'
    assert package.content_type('/custom/a.bin') == 'application/octet-stream'
    package.remove_relationship('/custom/a.bin', 'a·1')
    assert package.relationships('/custom/a.bin') == []
    package.remove_part('/custom/a.bin')
    assert package.relationships('/word/document.xml') == package.relationships('/custom/b.bin') == []
    assert '/custom/_rels/a.bin.rels' not in package.part_names()
    assert package.read_part('/custom/b.bin') == b'b'  # No orphan collection.
    changed_ct = package.read_part('/[Content_Types].xml')
    assert b'<!--keep--><?retain data?>' in changed_ct and b'urn:opaque' in changed_ct
    assert b'PartName="/custom/a.bin"' not in changed_ct and b'PartName="/custom/_rels/a.bin.rels"' not in changed_ct
    Package(package.bytes())


def test_invalid_operations_are_atomic():
    package = Package.new()
    original = package.bytes()
    for operation in [lambda: package.add_part('/bad.bin', 'bad', b'bad'),
                      lambda: package.remove_part(package.main_part),
                      lambda: package.replace_part('/_rels/.rels', b'bad'),
                      lambda: package.add_relationship('/', REL_TYPE, '../outside.bin'),
                      lambda: package.add_relationship('/', REL_TYPE, 'missing.bin'),
                      lambda: package.remove_relationship('/', 'rId1')]:
        with pytest.raises((ValueError, KeyError)): operation()
        assert package.bytes() == original


def test_illegal_new_metadata_values_are_atomic():
    package = Package.new()
    original = package.bytes()
    with pytest.raises(ValueError): package.add_part('/bad.bin', 'application/\ufffe', b'opaque')
    assert package.bytes() == original
    with pytest.raises(ValueError): package.add_relationship('/', REL_TYPE, 'https://example.org/a\tb', 'External')
    assert package.bytes() == original


def test_mismatched_declared_size_is_bounded_and_refused():
    source = bytearray(rewrite(Package.new().bytes(), extra=[('opaque.bin', b'opaque payload' * 1024)]))
    central = source.rfind(b'PK\x01\x02')
    pack_into('<I', source, central + 24, 1)
    package = Package(bytes(source))  # Non-control payloads remain lazy.
    with pytest.raises(ValueError, match='size|checksum'): package.read_part('/opaque.bin')


def test_utf16_opc_metadata_rewrites_as_utf8():
    original = Package.new().bytes()
    ct = '<?xml version="1.0" encoding="UTF-16"?>' + entries(original)['[Content_Types].xml'].decode()
    source = rewrite(original, [('[Content_Types].xml', ct.encode('utf-16'))])
    package = Package(source)
    assert package.bytes() == source
    package.add_part('/new.bin', 'application/octet-stream', b'opaque')
    assert b'encoding="UTF-8"' in package.read_part('/[Content_Types].xml')
    assert Package(package.bytes()).read_part('/new.bin') == b'opaque'


@pytest.mark.parametrize('kind', ['misplaced_declaration', 'bad_name', 'literal_angle', 'unseparated_attributes', 'encoding_mismatch'])
def test_malformed_control_xml_is_refused(kind):
    original = Package.new().bytes()
    ct = entries(original)['[Content_Types].xml']
    malformed = {
        'misplaced_declaration': b' <?xml version="1.0"?>' + ct,
        'bad_name': ct.replace(b'</Types>', b'<x:1bad xmlns:x="urn:opaque"/></Types>'),
        'literal_angle': ct.replace(b'<Types ', b'<Types extra="a<b" '),
        'unseparated_attributes': ct.replace(b'<Types ', b'<Types a="1"b="2" '),
        'encoding_mismatch': b'<?xml version="1.0" encoding="UTF-16"?>' + ct,
    }[kind]
    with pytest.raises(ValueError): Package(rewrite(original, [('[Content_Types].xml', malformed)]))


def test_signed_pass_through_only(tmp_path):
    original = Package.new().bytes()
    rels = entries(original)['_rels/.rels'].replace(b'</Relationships>', f'<Relationship Id="sig" Type="{REL_NS}/digital-signature/origin" Target="_xmlsignatures/origin.sigs"/></Relationships>'.encode())
    source = rewrite(original, [('_rels/.rels', rels)], [('_xmlsignatures/origin.sigs', b'opaque signature')])
    package = Package(source)
    assert package.bytes() == source
    package.save(str(tmp_path / 'signed.docx'))
    assert (tmp_path / 'signed.docx').read_bytes() == source
    with pytest.raises(ValueError, match='Signed'): package.replace_part(package.main_part, b'<changed/>')
    assert package.bytes() == source


@pytest.mark.parametrize('name', ['../escape.bin', '/absolute.bin', 'word/../escape.bin', 'C:/escape.bin', 'word\\escape.bin', 'word/%2e%2e/escape.bin', 'word/document.xml'])
def test_unsafe_or_duplicate_zip_names_refused(name):
    with pytest.warns(UserWarning) if name == 'word/document.xml' else nullcontext():
        source = rewrite(Package.new().bytes(), extra=[(name, b'bad')])
    with pytest.raises(ValueError, match='Unsafe|Invalid OPC|Duplicate'): Package(source)


@pytest.mark.parametrize('kind, message', [('encrypted', 'Encrypted'), ('oversize', '256 MiB'), ('count', '10,000'), ('zip64', 'ZIP64')])
def test_archive_preflight_limits(kind, message):
    source = bytearray(rewrite(Package.new().bytes(), extra=[('opaque.bin', b'data')]))
    central = source.rfind(b'PK\x01\x02')
    end = source.rfind(b'PK\x05\x06')
    if kind == 'encrypted': pack_into('<H', source, central + 8, unpack_from('<H', source, central + 8)[0] | 1)
    elif kind == 'oversize': pack_into('<I', source, central + 24, 256 * 1024 * 1024 + 1)
    else:
        count = 10_001 if kind == 'count' else 65_535
        pack_into('<HH', source, end + 8, count, count)
    with pytest.raises(ValueError, match=message): Package(bytes(source))


@pytest.mark.parametrize('kind', ['name', 'flags', 'compression'])
def test_local_header_disagreement_refused(kind):
    original = (FIXTURES / 'sdk/simpleSdt.docx').read_bytes()
    with ZipFile(BytesIO(original)) as z: offset = z.getinfo('word/document.xml').header_offset
    source = bytearray(original)
    if kind == 'name': source[offset + 30] = ord('z')
    elif kind == 'flags': pack_into('<H', source, offset + 6, unpack_from('<H', source, offset + 6)[0] ^ 1)
    else: pack_into('<H', source, offset + 8, 0)
    with pytest.raises(ValueError, match='local-header'): Package(bytes(source))


def test_symlink_zip_entry_refused():
    info = ZipInfo('link.bin')
    info.create_system, info.external_attr = 3, 0o120777 << 16
    with pytest.raises(ValueError, match='Symlink'): Package(rewrite(Package.new().bytes(), extra=[(info, b'target')]))


def test_failed_save_does_not_damage_source_or_destination(tmp_path):
    info = ZipInfo('opaque.bin')
    info.compress_type = ZIP_STORED
    original = rewrite(Package.new().bytes(), extra=[(info, b'opaque payload')])
    source = original.replace(b'opaque payload', b'broken payload')  # Same length, now invalid CRC in an untouched part.
    path = tmp_path / 'source.docx'
    path.write_bytes(source)
    package = Package(source)
    from oxml import Document
    report = Document.from_bytes(source).validate()
    assert any(i['rule_id'] == 'archive-integrity' and i['part_uri'] == '/opaque.bin' for i in report['issues'])
    with pytest.raises(ValueError, match='checksum'): package.read_part('/opaque.bin')
    package.replace_part(package.main_part, b'<changed/>')
    with pytest.raises(ValueError, match='checksum'): package.save(str(path))
    assert path.read_bytes() == source
    with pytest.raises(OSError): Package.new().save(str(tmp_path))
    assert list(tmp_path.iterdir()) == [path]
