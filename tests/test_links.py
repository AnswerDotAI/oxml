"""Existing footer links and live bookmark/link lifecycle, without opening external targets."""
from pathlib import Path
from xml.etree.ElementTree import fromstring, tostring
from zipfile import ZipFile
import pytest
from oxml import Document, E, Story, w
from oxml.links import Bookmarks, Hyperlinks

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
FIXTURE = Path(__file__).parent/'fixtures/crosspart/footer-contain-hyperlink.docx'

@pytest.mark.parametrize('owner,properties,revision', [('tr', 'trPr', 'del'), ('tc', 'tcPr', 'cellDel')])
def test_edits_in_revised_rows_and_cells_are_refused(owner, properties, revision):
    doc = Document.new()
    E('w:tbl', E('w:tr', E('w:tc', E('w:p', E('w:r', E('w:t', 'plain ')),
      E('w:hyperlink', E('w:r', E('w:t', 'link')), attrs={'w:anchor': 'target'}))))).append_to(doc.story.element.children[0])
    target = next(e for e in doc.main.xml.elements() if e.qname == (W, owner))
    E('w:'+properties, E('w:'+revision, attrs={'w:id': '1', 'w:author': 'Reviewer'})).append_to(target, 0)
    link, = doc.hyperlinks
    before = doc.bytes()
    for edit in (lambda: doc.story.find('plain').replace('changed'), lambda: doc.hyperlinks.add(doc.story.find('plain'), '#target'), link.remove):
        with pytest.raises(NotImplementedError, match='revision context'): edit()
        assert doc.bytes() == before

def test_existing_footer_target_reuse_and_scoped_cleanup_preserve_runs(tmp_path):
    doc = Document.open(FIXTURE)
    footer = doc.package.part('/word/footer1.xml').xml
    story = Story(footer.root)  # Identity-based owner discovery, without a supplied part URI.
    links = Hyperlinks(doc.package, story)
    link, = links
    assert link.target == 'http://www.google.com/' and link.text == story.text == 'www.google.com'
    original_run = fromstring(footer.bytes()).find(f'.//{{{W}}}hyperlink/{{{W}}}r')
    assert doc.bytes() == FIXTURE.read_bytes()
    E('w:r', E('w:t', ' second')).append_to(next(footer.elements(w.Paragraph)))
    other = links.add(story.find('second'), link.target)
    assert len(doc.package.relationships('/word/footer1.xml')) == 1
    assert other.element.attribute(R, 'id') == link.element.attribute(R, 'id') == 'rId1'
    before = doc.bytes()
    with pytest.raises(NotImplementedError): story.find('www.google.com').replace('protected')
    assert doc.bytes() == before
    link.remove()
    assert len(doc.package.relationships('/word/footer1.xml')) == 1  # Still used by other.
    other.remove()
    assert doc.package.relationships('/word/footer1.xml') == []
    assert doc.package.relationship_part(doc.main.uri, 'rId1') == '/word/styles.xml'
    assert story.text == 'www.google.com second' and list(links) == []
    assert tostring(fromstring(footer.bytes()).find(f'.//{{{W}}}r')) == tostring(original_run)
    output = tmp_path/'footer-links.docx'
    doc.save(output)
    with ZipFile(FIXTURE) as before, ZipFile(output) as after:
        assert all(before.read(n) == after.read(n) for n in before.namelist()
                   if n not in {'word/footer1.xml', 'word/_rels/footer1.xml.rels'})
    assert Story(Document.open(output).package.part('/word/footer1.xml').xml.root).text == story.text

def test_bookmark_anchor_ref_and_hyperlink_removal_preserve_formatted_text():
    doc = Document.new()
    E('w:p', E('w:r', E('w:rPr', E('w:b')), E('w:t', 'alpha be')),
      E('w:r', E('w:rPr', E('w:i')), E('w:t', 'ta gamma'))).append_to(next(doc.main.xml.elements(w.Body)))
    bookmark = doc.bookmarks.add(doc.story.find('beta'), 'clause')
    for marker in (bookmark.element, next(doc.main.xml.elements(w.BookmarkEnd))): marker.set_attribute(W, 'id', f'{bookmark.id:03}')
    assert bookmark.range.text == doc.bookmarks['clause'].range.text == 'beta'
    assert doc.bookmarks.find('missing') is None
    with pytest.raises(ValueError): doc.bookmarks.add(doc.story.find('alpha'), 'clause')
    link = doc.hyperlinks.add(bookmark.range, '#clause')
    assert link.target == '#clause' and link.anchor == 'clause' and link.text == bookmark.range.text == 'beta'
    assert doc.package.relationships(doc.main.uri) == []
    field = fromstring(bookmark.ref('cached').bytes())
    assert field.tag == f'{{{W}}}fldSimple' and field.get(f'{{{W}}}instr') == ' REF clause '
    assert field.get(f'{{{W}}}dirty') == 'true' and ''.join(field.itertext()) == 'cached'
    link.remove()
    runs = fromstring(doc.main.read_bytes()).findall(f'.//{{{W}}}r')
    assert [(r.find(f'{{{W}}}t').text, r.find(f'{{{W}}}rPr')[0].tag) for r in runs] == [
        ('alpha ', f'{{{W}}}b'), ('be', f'{{{W}}}b'), ('ta', f'{{{W}}}i'), (' gamma', f'{{{W}}}i')]
    bookmark.remove()
    assert doc.story.text == 'alpha beta gamma' and list(doc.bookmarks) == []
    caret = doc.bookmarks.add(doc.story.range(0, 0), 'point')
    assert caret.range.start == caret.range.end == 0
    with pytest.raises(NotImplementedError): Bookmarks(Story(doc.story.element, view='original'))['point'].remove()
    caret.remove()
    assert doc.story.text == 'alpha beta gamma'

def test_foreign_ranges_and_claimed_owners_cannot_change_relationships():
    docs = [Document.new(), Document.new()]
    for doc in docs: E('w:p', E('w:r', E('w:t', 'text'))).append_to(next(doc.main.xml.elements(w.Body)))
    doc, foreign = docs
    before = [d.bytes() for d in docs]
    with pytest.raises(ValueError): doc.hyperlinks.add(foreign.story.find('text'), 'https://example.com/')
    claimed = Story(foreign.story.element, part_uri=doc.main.uri)
    with pytest.raises(ValueError): Hyperlinks(doc.package, claimed).add(claimed.find('text'), 'https://example.com/')
    with pytest.raises(ValueError): doc.bookmarks.add(foreign.story.find('text'), 'wrong')
    assert [d.bytes() for d in docs] == before
    link = doc.hyperlinks.add(doc.story.find('text'), 'https://example.com/')
    ident = link.element.attribute(R, 'id')
    next(doc.main.xml.elements(w.Paragraph)).set_attribute('', 'opaque-reference', ident)
    link.remove()
    assert doc.package.relationships(doc.main.uri)[0]['id'] == ident  # Unknown attributes conservatively retain the relationship.

def test_linking_preserves_interior_bookmark_and_comment_ranges_and_reference_position():
    doc = Document.new()
    E('w:p', E('w:r', E('w:t', 'a')), E('w:bookmarkStart', attrs={'w:id': '1', 'w:name': 'inside'}),
      E('w:commentRangeStart', attrs={'w:id': '7'}), E('w:r', E('w:t', 'b'), E('w:commentReference', attrs={'w:id': '7'}), E('w:t', 'c')),
      E('w:commentRangeEnd', attrs={'w:id': '7'}), E('w:bookmarkEnd', attrs={'w:id': '1'}), E('w:r', E('w:t', 'd'))).append_to(
          next(doc.main.xml.elements(w.Body)))
    link = doc.hyperlinks.add(doc.story.find('abcd'), 'https://example.com/')
    assert link.text == 'abcd' and doc.bookmarks['inside'].range.text == 'bc'
    start, end = next(doc.main.xml.elements(w.CommentRangeStart)), next(doc.main.xml.elements(w.CommentRangeEnd))
    assert doc.story.range(doc.story._position(start), doc.story._position(end)).text == 'bc'
    assert doc.story._position(next(doc.main.xml.elements(w.CommentReference))) == 2
    names = [c.tag.split('}')[-1] for c in fromstring(link.element._tree.xml.subtree_bytes(link.element.node_id))]
    assert names == ['r', 'bookmarkStart', 'commentRangeStart', 'r', 'commentRangeEnd', 'bookmarkEnd', 'r']
    link.remove()
    assert doc.story.text == 'abcd' and doc.bookmarks['inside'].range.text == 'bc'
