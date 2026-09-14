"""Separate real DOCX stories and validation of reachable secondary parts."""
from pathlib import Path
from collections import Counter
import pytest
from oxml import Document, e

FIXTURES = Path(__file__).parent/'fixtures'
W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/'

@pytest.mark.parametrize('fixture,counts', [
    ('crosspart/headerPic.docx', {'document': 1, 'hdr': 1, 'footnote': 2, 'endnote': 2}),
    ('crosspart/footer-contain-hyperlink.docx', {'document': 1, 'ftr': 1, 'footnote': 2, 'endnote': 2}),
    ('crosspart/notes.docx', {'document': 1, 'footnote': 3, 'endnote': 3}),
    ('pandoc/track_changes_scrubbed_metadata.docx', {'document': 1, 'comment': 1}),
])
def test_real_stories_are_distinct_live_containers(fixture, counts):
    path = FIXTURES/fixture
    doc = Document.open(path)
    stories = list(doc.stories())
    assert Counter(s.element.raw['qname'][1] for s in stories) == counts
    assert all(s.element._tree.xml.same_state(doc.package.part(s.part_uri).xml.xml) for s in stories)
    for local in ('footnote', 'endnote'):
        if local in counts:
            expected = ['-1', '0', '1'] if counts[local] == 3 else ['0', '1']
            assert [s.element.attribute(W, 'id') for s in stories if s.element.raw['qname'][1] == local] == expected
    if 'comment' in counts:
        assert stories[0].text == 'Here is a test document.'
        assert next(doc.stories(view='original')).text == 'Here is a dummy document.'
        assert stories[1].element.attribute(W, 'id') == '3'
    assert doc.bytes() == path.read_bytes()

def test_shared_relocated_footer_is_one_story_and_validated_once():
    doc = Document.open(FIXTURES/'crosspart/footer-contain-hyperlink.docx')
    rel = next(r for r in doc.package.relationships(doc.main.uri) if r['type'] == R+'footer')
    old = doc.package.part(doc.package.relationship_part(doc.main.uri, rel['id']))
    content_type, data, links = old.content_type, old.read_bytes(), doc.package.relationships(old.uri)
    doc.package.remove_part(old.uri)
    part = doc.package.add_part('/elsewhere/shared.xml', content_type, data)
    doc.package.add_relationship(doc.main.uri, R+'footer', part.uri, relationship_id=rel['id'])
    doc.package.add_relationship(doc.main.uri, R+'footer', part.uri)
    for r in links: doc.package.add_relationship(part.uri, r['type'], r['target'], r['target_mode'], r['id'])
    footers = [s for s in doc.stories() if s.element.raw['qname'][1] == 'ftr']
    assert len(footers) == 1 and footers[0].part_uri == part.uri
    assert not [i for i in doc.validate()['issues'] if i['part_uri'] == part.uri]
    part.xml.root(e.body(), index=part.xml.xml.child_count(part.xml.root.node_id))  # Deliberately invalid footer child.
    before = doc.bytes()
    report = doc.validate()
    assert report['scope']['part_uris'].count(part.uri) == 1
    assert [(i['rule_id'], i['node']) for i in report['issues'] if i['part_uri'] == part.uri and i['category'] == 'schema'] == [('child-particle', part.xml.root.node_id)]
    assert doc.bytes() == before
    part.replace(e.document(e.body()).bytes())  # Valid XML vocabulary, but the wrong root for a footer part.
    assert any(i['rule_id'] == 'part-root' and i['part_uri'] == part.uri for i in doc.validate()['issues'])

def test_malformed_secondary_xml_is_reported_while_other_parts_are_validated():
    doc = Document.open(FIXTURES/'crosspart/notes.docx')
    doc.package.replace_part('/word/footnotes.xml', b'<w:footnotes')
    before = doc.bytes()
    report = doc.validate()
    assert any(i['rule_id'] == 'part-xml' and i['part_uri'] == '/word/footnotes.xml' for i in report['issues'])
    assert any(i['part_uri'] == '/word/footnotes.xml' for i in report['coverage']['incomplete_dependencies'])
    assert '/word/document.xml' in report['scope']['part_uris'] and '/word/endnotes.xml' in report['scope']['part_uris']
    assert '/word/footnotes.xml' not in report['scope']['part_uris']
    assert doc.bytes() == before
    with pytest.raises(ValueError): doc.validate('Office2000')

def test_note_validation_has_absolute_package_dependencies_and_opaque_parts_stay_bytes():
    doc = Document.open(FIXTURES/'crosspart/notes.docx')
    opaque = doc.package.add_part('/custom.xml', 'application/xml', b'not XML')
    doc.package.add_relationship(doc.main.uri, 'urn:opaque', opaque.uri)
    before = doc.bytes()
    report = doc.validate()
    assert not [i for i in report['issues'] if i['category'] == 'semantic' and i['part_uri'] in ('/word/footnotes.xml', '/word/endnotes.xml')]
    assert {'/word/footnotes.xml', '/word/endnotes.xml'} <= set(report['scope']['part_uris'])
    assert 'opaque-part:/custom.xml' in report['coverage']['gaps'] and '/custom.xml' not in report['scope']['part_uris']
    assert doc.bytes() == before
