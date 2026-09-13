"""Real review markup survives unrelated edits; no reply/resolve/accept/reject operation or Word oracle."""
from pathlib import Path
from zipfile import ZipFile
from xml.dom import minidom
import xml.etree.ElementTree as ET
import pytest
from oxml import Document, w
from corpus_helpers import saved_edit, parts

FIXTURES = Path(__file__).parent/'fixtures'
NS = {
    'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
    'w14': 'http://schemas.microsoft.com/office/word/2010/wordml',
    'w15': 'http://schemas.microsoft.com/office/word/2012/wordml',
    'cid': 'http://schemas.microsoft.com/office/word/2016/wordml/cid',
    'cex': 'http://schemas.microsoft.com/office/word/2018/wordml/cex',
}
W, W14, W15, CID, CEX = (f'{{{NS[p]}}}' for p in ('w', 'w14', 'w15', 'cid', 'cex'))
CASES = [
    ('inline', 'pandoc/track_changes_scrubbed_metadata.docx', 'Here is a '),
    ('paragraph', 'pandoc/paragraph_insertion_deletion.docx', 'This is a'),
    ('moves', 'reviews/pandoc/track_changes_move.docx', 'Here is some text.'),
    ('formatting', 'reviews/powertools/RP024-ParagraphMark-rPr-Change.docx',
     'Video provides a powerful way to help you prove your point.'),
    ('reply', 'reviews/libreoffice/CommentReply.docx', 'text'),
    ('done', 'reviews/libreoffice/CommentDone.docx', 'Lorem ipsum dolor sit '),
]

def _review_features(case, parts):
    root = ET.fromstring(parts['word/document.xml'])
    if case in ('inline', 'paragraph'):
        prefix = './/w:p/' if case == 'inline' else './/w:pPr/w:rPr/'
        for tag, ident in (('del', '1'), ('ins', '2' if case == 'inline' else '0')):
            revision, = root.findall(prefix+f'w:{tag}', NS)
            expected = {W+'id': ident, W+'author': 'Author' if case == 'inline' else 'Seeley, Jason'}
            if case == 'paragraph': expected[W+'date'] = '2017-09-17T16:39:00Z'
            assert revision.attrib == expected
            assert ''.join(revision.itertext()) == ({'del': 'dummy', 'ins': 'test'}[tag] if case == 'inline' else '')
    if case == 'moves':
        move_nodes = [e for e in root.iter() if e.tag.startswith(W+'move')]
        assert [(e.tag[len(W):], e.get(W+'id')) for e in move_nodes] == [
            ('moveToRangeStart', '0'), ('moveTo', '1'), ('moveToRangeEnd', '0'),
            ('moveFromRangeStart', '3'), ('moveFrom', '4'), ('moveFromRangeEnd', '3')]
        for direction in ('To', 'From'):
            start, = root.iter(W+f'move{direction}RangeStart')
            assert start.get(W+'name') == 'move322414172'
            moved, = root.iter(W+f'move{direction}')
            assert moved.get(W+'author') == 'Jesse Rosenthal' and moved.get(W+'date') == '2016-04-16T08:20:00Z'
            assert ''.join(moved.itertext()) == 'Here is the text to be moved.'
    if case == 'formatting':
        change, = root.findall('.//w:pPr/w:rPr/w:rPrChange', NS)
        assert change.attrib == {W+'id': '0', W+'author': 'Eric White', W+'date': '2017-03-26T05:02:00Z'}
        assert root.find('.//w:pPr/w:rPr/w:b', NS) is not None  # Current state is bold; prior state is not.
        prior, = change
        assert [(e.tag, e.attrib) for e in prior] == [(W+'lang', {W+'val': 'en-US'})]
    if case not in ('inline', 'reply', 'done'): return
    comments = list(ET.fromstring(parts['word/comments.xml']))
    ids = [e.get(W+'id') for e in comments]
    assert ids == {'inline': ['3'], 'reply': ['1', '0'], 'done': ['0', '1']}[case]
    for tag in ('commentRangeStart', 'commentRangeEnd', 'commentReference'):
        assert sorted(e.get(W+'id') for e in root.iter(W+tag)) == sorted(ids)
    if case == 'inline':
        assert ''.join(comments[0].itertext()) == 'With a comment!'
        return
    para_ids = [[p.get(W14+'paraId') for p in c.findall('w:p', NS)] for c in comments]
    extended = list(ET.fromstring(parts['word/commentsExtended.xml']))
    assert all(e.tag == W15+'commentEx' for e in extended)
    if case == 'reply':
        assert para_ids == [['01000000'], ['02000000']]
        assert [''.join(c.itertext()) for c in comments] == ['Parent', 'Child']
        assert [e.attrib for e in extended] == [{W15+'paraId': '02000000', W15+'paraIdParent': '01000000'}]
    else:
        assert para_ids == [['1C11E5F4', '78D78669'], ['4AF96CD3', '43C517C4', '33436897']]
        assert [e.attrib for e in extended] == [
            {W15+'paraId': '78D78669', W15+'done': '1'}, {W15+'paraId': '33436897', W15+'done': '0'}]
        ids_part = ET.fromstring(parts['word/commentsIds.xml'])
        assert [(e.get(CID+'paraId'), e.get(CID+'durableId')) for e in ids_part] == [
            ('78D78669', '241F501B'), ('33436897', '241F5051')]
        extensible = ET.fromstring(parts['word/commentsExtensible.xml'])
        assert [(e.get(CEX+'durableId'), e.get(CEX+'dateUtc')) for e in extensible] == [
            ('241F501B', '2021-04-12T20:02:00Z'), ('241F5051', '2021-04-12T20:03:00Z')]

@pytest.mark.parametrize('case,filename,original_text', CASES, ids=[c[0] for c in CASES])
def test_review_corpus_preservation(case, filename, original_text, tmp_path):
    path = FIXTURES/filename
    with ZipFile(path) as package: before = {name: package.read(name) for name in package.namelist()}
    _review_features(case, before)
    doc = Document.open(path)
    assert doc.main.uri == '/word/document.xml'
    tree = doc.main.xml
    for name in before:
        if name.startswith('word/comments') and name.endswith('.xml'):
            assert doc.package.part('/'+name).xml.bytes() == before[name]
    relationships = doc.package.relationships(doc.main.uri)
    for rel in ET.fromstring(before['word/_rels/document.xml.rels']):
        if 'comment' in rel.get('Type').lower():
            assert doc.package.relationship_part(doc.main.uri, rel.get('Id')) == '/word/'+rel.get('Target')
    report = doc.validate()
    assert doc.bytes() == path.read_bytes()
    unchanged = tmp_path/'unchanged.docx'
    doc.save(unchanged)
    assert unchanged.read_bytes() == path.read_bytes()

    # Explicit derivative lineage: replace only the first ordinary w:t, never review metadata/content.
    # CommentReply's only text is inside the anchored range; changing it must not detach the thread.
    expected = minidom.parseString(before['word/document.xml'])
    text_node = expected.getElementsByTagNameNS(NS['w'], 't')[0].firstChild
    assert text_node.data == original_text
    replacement = original_text.replace(original_text.strip(), 'An unrelated text edit')
    text_node.data = replacement
    text = next(tree.elements(w.Text))
    assert text.value == original_text
    text.value = replacement
    output = tmp_path/'edited.docx'
    reopened = saved_edit(doc, path, output, {'word/document.xml': expected})
    _review_features(case, parts(reopened))
    assert next(reopened.main.xml.elements(w.Text)).value == replacement
    assert reopened.package.relationships(reopened.main.uri) == relationships
    assert reopened.validate()['issues'] == report['issues']
