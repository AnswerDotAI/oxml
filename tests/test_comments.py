"""Comment operations checked against real Word/LibreOffice markup, without launching Word."""
from datetime import datetime, timezone
from pathlib import Path
import xml.etree.ElementTree as ET
import pytest
from oxml import Document, E, e, Story, Comments
from corpus_helpers import parts

e16cex = E('w16cex', attr_ns='w16cex')

FIXTURES = Path(__file__).parent/'fixtures/reviews/libreoffice'
W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
W14 = '{http://schemas.microsoft.com/office/word/2010/wordml}'
W15 = '{http://schemas.microsoft.com/office/word/2012/wordml}'
CID = '{http://schemas.microsoft.com/office/word/2016/wordml/cid}'
CEX = '{http://schemas.microsoft.com/office/word/2018/wordml/cex}'
DATE = datetime(2026, 9, 13, 2, 3, 4, tzinfo=timezone.utc)

def plain():
    doc = Document.new()
    p = doc.main.xml.root.children[0](e.p(e.r(e.t('alpha ', xml__space='preserve')),
                                        e.r(e.rPr(e.b()), e.t('beta'))))
    return doc, Story(p)

def test_classic_comment_creation_anchors_formatting_and_package_ownership(tmp_path):
    doc, story = plain()
    doc.package.add_part('/word/comments.xml', 'application/octet-stream', b'occupied')
    doc.package.add_relationship(doc.main.uri, 'urn:unrelated', '/word/comments.xml', relationship_id='rId1')
    comment = doc.comments.add(story.find('ha be'), 'Review\nsecond paragraph', 'Jeremy', initials='JH', date=DATE)
    assert (comment.id, comment.author, comment.text, comment.parent, comment.resolved) == (0, 'Jeremy', 'Review\nsecond paragraph', None, False)
    assert comment.range.text == 'ha be'
    package = parts(doc)
    unchanged = doc.bytes()
    comment.resolve(False)
    assert doc.bytes() == unchanged
    assert package['word/comments.xml'] == b'occupied'
    body = ET.fromstring(package['word/document.xml']).find('.//'+W+'p')
    text, anchors = '', {}
    for child in body:
        if child.tag in {W+'commentRangeStart', W+'commentRangeEnd'}: anchors[child.tag] = len(text)
        text += ''.join(t.text or '' for t in child.iter(W+'t'))
    assert text == 'alpha beta' and text[anchors[W+'commentRangeStart']:anchors[W+'commentRangeEnd']] == 'ha be'
    assert len(body.findall('.//'+W+'b')) == 2  # Both split pieces retain the bold run's properties.
    reference, = body.iter(W+'commentReference')
    assert reference.get(W+'id') == '0'
    comments = ET.fromstring(package['word/comments1.xml'])
    assert comments[0].get(W+'date') == '2026-09-13T02:03:04Z' and len(comments[0].findall(W+'p')) == 2
    assert not any('commentsExtended' in n for n in package)
    path = tmp_path/'comments.docx'
    doc.save(path)
    assert Comments(Document.open(path))[0].text == comment.text


# LibreOffice ooxmlexport16.cxx:testCommentReply; ooxmlexport21.cxx:testCommentWithChildrenTdf163092.
def test_existing_reply_thread_uses_last_para_ids_and_discovered_parts():
    doc = Document.open(FIXTURES/'CommentReply.docx')
    for old, new in [('comments.xml', '/custom/body.xml'), ('commentsExtended.xml', '/custom/thread.xml')]:
        rel, = [r for r in doc.package.relationships(doc.main.uri) if doc.package.relationship_part(doc.main.uri, r['id']) == '/word/'+old]
        part = doc.package.part('/word/'+old)
        doc.package.add_part(new, part.content_type, part.read_bytes())
        doc.package.remove_part(part.uri)
        doc.package.add_relationship(doc.main.uri, rel['type'], new)
    comments = Comments(doc)
    assert comments[0].parent.id == 1 and [c.id for c in comments[1].replies] == [0]
    before = parts(doc)
    reply = comments[0].reply('A nested reply', 'Jeremy', date=DATE)
    assert reply.id == 2 and reply.parent.id == 0 and comments[0].replies[0].id == 2
    comments[1].resolve()
    assert comments[1].resolved
    after = parts(doc)
    changed = {'word/document.xml', 'custom/body.xml', 'custom/thread.xml'}
    assert {n: b for n, b in before.items() if n not in changed} == {n: b for n, b in after.items() if n not in changed}
    body, extended = ET.fromstring(after['custom/body.xml']), ET.fromstring(after['custom/thread.xml'])
    new_para_id = body[-1].findall(W+'p')[-1].get(W14+'paraId')
    record, = [e for e in extended if e.get(W15+'paraId') == new_para_id]
    assert record.get(W15+'paraIdParent') == '02000000'
    assert extended[0].attrib == ET.fromstring(before['custom/thread.xml'])[0].attrib
    main = ET.fromstring(after['word/document.xml'])
    for local in ('commentRangeStart', 'commentRangeEnd', 'commentReference'):
        assert sum(e.get(W+'id') == '2' for e in main.iter(W+local)) == 1
    paragraph = main.find('.//'+W+'p')
    index = next(i for i, e in enumerate(paragraph) if e.tag == W+'commentRangeEnd' and e.get(W+'id') == '2')
    assert paragraph[index+1].find(W+'commentReference').get(W+'id') == '2'
    assert paragraph[index+2].tag == W+'commentRangeEnd' and paragraph[index+2].get(W+'id') == '0'


# Original CommentDone fixture supplies Word's w14 -> w15/w16cid -> w16cex linkage.
def test_modern_resolve_reply_and_utc_metadata_preserve_other_records():
    doc = Document.open(FIXTURES/'CommentDone.docx')
    extensible = doc.package.part('/word/commentsExtensible.xml').xml.root
    keep = E('keep', ns={'keep': 'urn:keep'})
    extensible(e16cex.extLst(keep.payload('opaque')))
    comments, before = Comments(doc), parts(doc)
    comments[0].resolve(False)
    assert not comments[0].resolved
    resolved = parts(doc)
    assert {n for n in before if before[n] != resolved[n]} == {'word/commentsExtended.xml'}
    reply = comments[0].reply('New\nresponse', 'Jeremy', date=DATE)
    after = parts(doc)
    changed = {'word/document.xml', 'word/comments.xml', 'word/commentsExtended.xml', 'word/commentsIds.xml', 'word/commentsExtensible.xml'}
    assert {n: b for n, b in before.items() if n not in changed} == {n: b for n, b in after.items() if n not in changed}
    body = ET.fromstring(after['word/comments.xml'])
    para_id = body[-1].findall(W+'p')[-1].get(W14+'paraId')
    extended = ET.fromstring(after['word/commentsExtended.xml'])
    assert extended[-1].get(W15+'paraIdParent') == '78D78669'  # Last paragraph, not the comment's first paragraph.
    ids, cex = (ET.fromstring(after['word/'+name]) for name in ('commentsIds.xml', 'commentsExtensible.xml'))
    durable = ids[-1].get(CID+'durableId')
    assert ids[-1].get(CID+'paraId') == para_id and cex[-2].attrib == {CEX+'durableId': durable, CEX+'dateUtc': '2026-09-13T02:03:04Z'}
    assert cex[-1].tag == CEX+'extLst' and cex[-1][0].text == 'opaque'
    for name in ('comments.xml', 'commentsIds.xml', 'commentsExtensible.xml'):
        old, new = ET.fromstring(before['word/'+name]), ET.fromstring(after['word/'+name])
        assert [ET.tostring(e) for e in old[:2]] == [ET.tostring(e) for e in new[:2]]
    assert Comments(Document.from_bytes(doc.bytes()))[reply.id].text == 'New\nresponse'


def test_comment_refusals_do_not_mutate_document():
    doc, story = plain()
    range = story.find('alpha')
    story.element.children[0].set_attribute(W[1:-1], 'rsidR', '00000001')
    before = doc.bytes()
    with pytest.raises(ReferenceError): Comments(doc).add(range, 'stale', 'Jeremy')
    with pytest.raises(ValueError): Comments(doc).add(story.find('alpha'), '\x00', 'Jeremy')
    other, other_story = plain()
    with pytest.raises(ValueError): Comments(doc).add(other_story.find('alpha'), 'foreign', 'Jeremy')
    assert doc.bytes() == before
    modern = Document.open(FIXTURES/'CommentDone.docx')
    modern.package.remove_part('/word/commentsIds.xml')
    before = modern.bytes()
    with pytest.raises(ValueError, match='commentsIds'): Comments(modern)[0].reply('bad linkage', 'Jeremy')
    assert modern.bytes() == before


def test_comment_ids_compare_as_numbers_for_range_and_deletion():
    doc, story = plain()
    comment = doc.comments.add(story.find('alpha'), 'Review', 'Jeremy', date=DATE)
    comment.element.set_attribute(W[1:-1], 'id', '000')
    for element in doc.main.xml.elements():
        if element.raw['qname'][1] in {'commentRangeStart', 'commentRangeEnd', 'commentReference'}:
            element.set_attribute(W[1:-1], 'id', '00')
    assert doc.comments[0].range.text == 'alpha'
    assert doc.comments[0].delete() == 1 and not list(doc.comments)
    assert not [e for e in ET.fromstring(doc.main.read_bytes()).iter() if e.tag in {W+'commentRangeStart', W+'commentRangeEnd', W+'commentReference'}]


def test_delete_modern_thread_removes_only_its_records_and_anchors():
    doc = Document.open(FIXTURES/'CommentDone.docx')
    comments = doc.comments
    parent = comments[0]
    reply = parent.reply('Remove this branch', 'Jeremy', date=DATE)
    nested = reply.reply('Nested response', 'Jeremy', date=DATE)
    before, original_text = parts(doc), doc.story.text
    assert reply.delete() == 2
    after = parts(doc)
    assert [c.id for c in comments] == [0, 1] and not comments[0].replies
    assert doc.story.text == original_text
    for name in ('comments.xml', 'commentsExtended.xml', 'commentsIds.xml', 'commentsExtensible.xml'):
        old, new = ET.fromstring(before['word/'+name]), ET.fromstring(after['word/'+name])
        assert [ET.tostring(e) for e in old[:2]] == [ET.tostring(e) for e in new]
    with pytest.raises(ReferenceError): _ = nested.text
    new_reply = parent.reply('Remove the whole thread', 'Jeremy', date=DATE)
    assert new_reply.delete_thread() == 2
    assert [c.id for c in Document.from_bytes(doc.bytes()).comments] == [1]
    main = ET.fromstring(doc.main.read_bytes())
    assert all(e.get(W+'id') == '1' for local in ('commentRangeStart', 'commentRangeEnd', 'commentReference') for e in main.iter(W+local))
    changed = {'word/document.xml', 'word/comments.xml', 'word/commentsExtended.xml', 'word/commentsIds.xml', 'word/commentsExtensible.xml'}
    final = parts(doc)
    assert {n: b for n, b in before.items() if n not in changed} == {n: b for n, b in final.items() if n not in changed}


def test_delete_refuses_broken_modern_linkage_before_mutation():
    doc = Document.open(FIXTURES/'CommentDone.docx')
    doc.package.part('/word/commentsIds.xml').xml.root.children[0].delete()
    before = doc.bytes()
    with pytest.raises(ValueError, match='durable-ID'): doc.comments[0].delete()
    assert doc.bytes() == before


def test_delete_empty_classic_comment_and_refuse_external_story_anchors():
    doc = Document.open(FIXTURES.parent.parent/'crosspart/footer-contain-hyperlink.docx')
    root = doc.comments._part('comments', True)
    root(e.comment(id='0', author='Jeremy'))
    assert doc.comments[0].delete() == 1 and not list(doc.comments)
    root(e.comment(id='1', author='Jeremy'))
    footer = next(s for s in doc.stories() if s.element.raw['qname'][1] == 'ftr')
    footer.element.children[0](e.commentRangeStart(id='1'), index=0)
    before = doc.bytes()
    with pytest.raises(NotImplementedError, match='outside the main part'): doc.comments[1].delete()
    assert doc.bytes() == before
