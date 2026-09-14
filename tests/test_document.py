'Real Python package/editor integration; no Office interoperability claim.'
from pathlib import Path
from zipfile import ZipFile
from io import BytesIO
from datetime import datetime, timezone
import pytest, struct, zlib
from oxml import Document, Tree, e, w, namespace_uris
from xml.etree.ElementTree import fromstring
from corpus_helpers import parts

FIXTURES = Path(__file__).parent/'fixtures'

def test_open_typed_raw_edit_validate_save_real_docx(tmp_path):
    path = FIXTURES/'pandoc/track_changes_scrubbed_metadata.docx'
    doc = Document.open(path)
    tree = doc.main.xml
    report = doc.validate()
    assert report['coverage']['semantic_checks'] and not [i for i in report['issues'] if i['category'] == 'semantic']
    assert doc.bytes() == path.read_bytes()  # Parsing/typing/validation must not dirty any part.
    reference = next(tree.elements(w.CommentReference))
    reference.id = '987654'
    assert [i for i in doc.validate()['issues'] if i['category'] == 'semantic']
    reference.id = '3'
    text = next(tree.elements(w.Text))
    identity = text.node_id
    text.value = 'A typed edit'
    tree.xml.set_text(identity, 'The same state, edited raw')
    assert text.value == 'The same state, edited raw' and text.node_id == identity
    assert b'The same state, edited raw' in doc.main.read_bytes()
    output = tmp_path/'edited.docx'
    doc.save(output)
    with ZipFile(path) as before, ZipFile(output) as after:
        assert before.namelist() == after.namelist()
        assert all(before.read(n) == after.read(n) for n in before.namelist() if n != doc.main.uri.lstrip('/'))
    reopened = Document.open(output)
    assert next(reopened.main.xml.elements(w.Text)).value == text.value
    assert not [i for i in reopened.validate()['issues'] if i['category'] == 'semantic']

def test_new_document_structural_edits_preserve_live_handles(tmp_path):
    doc = Document.new()
    tree = doc.main.xml
    body = next(tree.elements(w.Body))
    paragraph, = body.append_xml(b'<w:p><w:r><w:t>one</w:t></w:r></w:p>')
    assert isinstance(paragraph, w.Paragraph) and paragraph.parent.node_id == body.node_id
    text = next(tree.elements(w.Text))
    text.value = 'two'
    copied = paragraph.copy_to(body)
    copied.move_to(body, 0)
    assert paragraph.children[0].children[0].value == copied.children[0].children[0].value == 'two'
    copied.delete()
    with pytest.raises(ReferenceError): _ = copied.raw
    assert paragraph.node_id and text.value == 'two'
    text.replace(b'<w:t>replacement</w:t>')
    with pytest.raises(ReferenceError): _ = text.value
    paragraph.paragraph_id = '0000000A'
    assert paragraph.attribute(namespace_uris['w14'], 'paraId') == '0000000A' and b'w14:paraId="0000000A"' in doc.main.read_bytes()
    assert ('w14', namespace_uris['w14']) in doc.main.xml.root.raw['namespaces']  # Declared once, at the root.
    second = body(e.p(e.r(e.t('before'))), index=paragraph.index)
    assert [p.index for p in body.elements(w.Paragraph)] == [0, 1] and second.index == 0
    paragraph.move_to(body, second.index)
    assert [next(p.elements(w.Text)).value for p in body.elements(w.Paragraph)] == ['replacement', 'before']
    headers = [doc.add_part('HeaderPart') for _ in range(2)]
    assert len({h.uri for h in headers}) == 2
    with pytest.raises(ValueError, match='Ambiguous'): doc.part('HeaderPart')
    assert doc.package.relationship_part(doc.main.uri, doc.package.relationship_id(doc.main.uri, headers[1].uri)) == headers[1].uri
    doc.properties.update(title='Agreement', creator='Drafter')
    assert dict(doc.properties) == {'title': 'Agreement', 'creator': 'Drafter'} and '/docProps/core.xml' in doc.package.part_names()
    doc.properties['title'] = 'Services agreement'
    del doc.properties['creator']
    doc.properties['created'] = datetime(2026, 9, 14, tzinfo=timezone.utc)
    assert doc.properties['created'] == '2026-09-14T00:00:00+00:00'
    assert not doc.validate()['issues']
    output = tmp_path/'new.docx'
    doc.save(output)
    assert next(Document.open(output).main.xml.elements(w.Text)).value == 'replacement'

def test_embed_image_preserves_existing_media_and_uses_owning_part_relationships():
    doc = Document.open(FIXTURES/'crosspart/headerPic.docx')
    image = doc.package.read_part('/word/media/image1.jpeg')
    before = parts(doc)
    for index, part in enumerate((doc.main, doc.package.part('/word/header1.xml')), 10):
        drawing = part.add_image(image, width=914400, height=457200, description='A <picture> & caption')
        markup = fromstring(drawing._tree.bytes())
        assert markup.find('.//wp:extent', namespace_uris).attrib == dict(cx='914400', cy='457200')
        assert markup.find('.//wp:docPr', namespace_uris).get('descr') == 'A <picture> & caption'
        rid = markup.find('.//a:blip', namespace_uris).get(f"{{{namespace_uris['r']}}}embed")
        uri = doc.package.relationship_part(part.uri, rid)
        assert uri.lstrip('/') not in before and doc.package.read_part(uri) == image
        next(part.xml.elements(w.Paragraph))(e.r(drawing))
    result = Document.from_bytes(doc.bytes())
    assert result.package.read_part('/word/media/image1.jpeg') == image
    drawing, = result.main.xml.elements(w.Drawing)
    assert result.package.part('/word/header1.xml').xml.count(w.Drawing) == 2

def test_part_replacement_and_removal_invalidate_old_xml_handles():
    doc = Document.new()
    old_part = doc.main
    old_tree = old_part.xml
    root = old_tree.root
    doc.package.replace_part(old_part.uri, old_part.read_bytes())
    with pytest.raises(ReferenceError): _ = root.raw
    with pytest.raises(ReferenceError): old_part.read_bytes()
    part = doc.package.add_part('/custom/item.xml', 'application/xml', b'<r/>')
    opaque = part.xml.root
    doc.package.remove_part(part.uri)
    with pytest.raises(ReferenceError): _ = opaque.raw
    new = doc.package.add_part(part.uri, 'application/xml', b'<replacement/>')
    with pytest.raises(ReferenceError): _ = part.xml
    assert new.xml.root.raw['qname'][1] == 'replacement'

def test_contextual_views_detect_retyping_without_invalidating_raw_identity():
    source = b'<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:del w:id="1" w:author="a"/><w:pPr><w:rPr/></w:pPr></w:p>'
    tree = Tree(source)
    deletion = next(tree.elements(w.DeletedRun))
    identity = deletion.node_id
    with pytest.raises(ReferenceError, match='type changed'): _ = w.Deleted(tree, identity).raw
    properties = next(tree.elements(w.ParagraphMarkRunProperties))
    deletion.move_to(properties, 0)
    with pytest.raises(ReferenceError, match='type changed'): _ = deletion.raw
    mark = next(tree.elements(w.Deleted))
    assert mark.node_id == identity
    tree.xml.rename(identity, 'http://schemas.openxmlformats.org/wordprocessingml/2006/main', 'ins')
    with pytest.raises(ReferenceError, match='type changed'): _ = mark.qname
    assert next(tree.elements(w.Inserted)).node_id == identity

def test_equivalent_part_names_share_xml_state_and_invalidation():
    doc = Document.new()
    alias = doc.package.part('/WORD/DOCUMENT.XML')
    main = doc.main
    assert alias.uri == main.uri and alias.xml.xml.same_state(main.xml.xml)
    root = alias.xml.root
    alias.xml.xml.set_attribute(root.node_id, '', 'flag', 'edited')
    assert b'flag="edited"' in doc.package.read_part('/Word/Document.xml')
    replacement = doc.package.replace_part('/WORD/DOCUMENT.XML', main.read_bytes())
    with pytest.raises(ReferenceError): _ = root.raw
    with pytest.raises(ReferenceError): alias.read_bytes()
    assert replacement.uri == main.uri

@pytest.mark.parametrize('occupied', ['/CUSTOMXML/ITEM1.XML', '/CUSTOMXML/ITEMPROPS1.XML'])
def test_set_custom_xml_preserves_other_stores_and_reuses_id(occupied):
    doc = Document.new()
    doc.package.add_part(occupied, 'application/xml', b'<keep/>')
    item_id = '{8E2C9A44-7D31-4E5B-9C0D-1A6F2B3C4D5E}'
    item = doc.set_custom_xml(item_id, b'<fields xmlns="urn:fields"/>', schema_uri='urn:fields')
    doc.set_custom_xml('{11111111-2222-3333-4444-555555555555}', b'<other/>')
    rel, = doc.package.relationships(item.uri)
    props = doc.package.part(doc.package.relationship_part(item.uri, rel['id'])).xml.root
    ds = 'http://schemas.openxmlformats.org/officeDocument/2006/customXml'
    assert props.attribute(ds, 'itemID') == item_id
    assert props.children[0].children[0].attribute(ds, 'uri') == 'urn:fields'
    before = parts(doc)
    assert before[occupied.lstrip('/')] == b'<keep/>'
    doc = Document.from_bytes(doc.bytes())
    data = b'<fields xmlns="urn:fields"><name>Jeremy</name></fields>'
    changed = doc.set_custom_xml(item_id.lower(), data)
    assert changed.uri == item.uri and changed.read_bytes() == data
    after = parts(doc)
    assert after.pop(item.uri.lstrip('/')) == data
    before.pop(item.uri.lstrip('/'))
    assert after == before
    assert not doc.validate()['issues']

def test_missing_dependency_is_reported_without_discarding_main_validation():
    source = FIXTURES/'pandoc/track_changes_scrubbed_metadata.docx'
    data = BytesIO()
    with ZipFile(source) as before, ZipFile(data, 'w') as after:
        for entry in before.infolist():
            if entry.filename != 'word/comments.xml': after.writestr(entry, before.read(entry))
    report = Document.from_bytes(data.getvalue()).validate()
    assert any(i['target'] == 'comments.xml' for i in report['coverage']['incomplete_dependencies'])
    assert any(i['rule_id'] == 'relationship-target' and i['category'] == 'package' for i in report['issues'])
    assert '/word/document.xml' in report['scope']['part_uris'] and not report['coverage']['complete']

def test_many_typed_edits_share_live_nodes_without_rebuilding_document_views():
    doc = Document.new()
    tree = doc.main.xml
    body = next(tree.elements(w.Body))
    body.append_xml(b'<w:p><w:r><w:t>before</w:t></w:r></w:p>' * 2000)
    texts = list(tree.elements(w.Text))
    for i, text in enumerate(texts):
        identity = text.node_id
        snapshot = text.raw
        text.value = str(i)
        assert text.value == str(i) and text.node_id == identity and snapshot['text'] == 'before'
    reopened = Document.from_bytes(doc.bytes())
    assert [text.value for text in reopened.main.xml.elements(w.Text)] == [str(i) for i in range(2000)]

def test_validation_report_summarises_itself():
    report = Document.new().validate()
    assert report['issues'] == [] and repr(report).startswith('Report: 0 issues')
    report['coverage']['gaps'] = ['pattern:a', 'pattern:b', 'enum:c']
    assert '3 gaps (pattern 2, enum 1)' in repr(report)

def test_settings_mapping_creates_and_replaces_flat_values():
    doc = Document.new()
    doc.settings['updateFields'] = True
    doc.settings['defaultTabStop'] = 720
    assert doc.settings['updateFields'] is True and doc.settings['defaultTabStop'] == '720'
    doc.settings['updateFields'] = False
    assert doc.settings['updateFields'] is False
    assert [n for n in doc.settings if n in ('updateFields', 'defaultTabStop')] == ['defaultTabStop', 'updateFields']
    doc.settings['defaultTabStop'] = 1
    with pytest.raises(ValueError): doc.settings['defaultTabStop'] = True
    assert doc.settings['defaultTabStop'] == '1'
    compat = doc.settings.root(e.compat())
    with pytest.raises(NotImplementedError, match='settings.root'): _ = doc.settings['compat']
    assert 'compat' not in list(doc.settings) and compat.parent.node_id == doc.settings.root.node_id
    del doc.settings['defaultTabStop']
    assert 'defaultTabStop' not in doc.settings and not doc.validate()['issues']

def test_images_are_sniffed_sized_and_numbered():
    def png(width, height, dpi=None):
        def chunk(kind, body): return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body))
        physical = chunk(b'pHYs', struct.pack('>IIB', round(dpi / 0.0254), round(dpi / 0.0254), 1)) if dpi else b''
        rows = zlib.compress(b''.join(b'\0' * (1 + 3 * width) for _ in range(height)))
        return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) + physical + chunk(b'IDAT', rows) + chunk(b'IEND', b'')
    doc = Document.new()
    body = next(doc.main.xml.elements(w.Body))
    facts = []
    for data, sizes in ((png(4, 2), {}), (png(4, 2, dpi=192), {}), (png(4, 2), dict(width=914400))):
        drawing = doc.main.add_image(data, **sizes)
        markup = fromstring(drawing._tree.bytes())
        extent = markup.find('.//wp:extent', namespace_uris)
        facts.append((int(extent.get('cx')), int(extent.get('cy')), markup.find('.//wp:docPr', namespace_uris).get('id')))
        body(e.p(e.r(drawing)))
    assert facts == [(38100, 19050, '1'), (19050, 9525, '2'), (914400, 457200, '3')]
    embed = markup.find('.//a:blip', namespace_uris).get(f"{{{namespace_uris['r']}}}embed")
    assert doc.package.content_type(doc.package.relationship_part(doc.main.uri, embed)) == 'image/png'
    reopened = Document.from_bytes(doc.bytes())
    again = fromstring(reopened.main.add_image(png(1, 1))._tree.bytes())
    assert again.find('.//wp:docPr', namespace_uris).get('id') == '4' and not reopened.validate()['issues']
    with pytest.raises(ValueError, match='content_type'): doc.main.add_image(b'not an image')
