'Existing footer links and live bookmark/link lifecycle, without opening external targets.'
from pathlib import Path
from xml.etree.ElementTree import fromstring, tostring
from zipfile import ZipFile
import pytest
from oxml import Document, e, Story, w, bookmark_name
from oxml.links import Bookmarks, Hyperlinks

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
FIXTURE = Path(__file__).parent/'fixtures/crosspart/footer-contain-hyperlink.docx'

@pytest.mark.parametrize('owner,properties,revision', [('tr', 'trPr', 'del'), ('tc', 'tcPr', 'cellDel')])
def test_edits_in_revised_rows_and_cells_are_refused(owner, properties, revision):
    doc = Document.new()
    doc.story.element.children[0](e.tbl(e.tr(e.tc(e.p(e.r(e.t('plain ')),
        e.hyperlink(e.r(e.t('link')), anchor='target'))))))
    target = next(e for e in doc.main.xml.elements() if e.qname == (W, owner))
    target(e(properties, e(revision, id='1', author='Reviewer')))
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
    paragraph = next(footer.elements(w.Paragraph))
    paragraph(e.r(e.t(' second')), index=footer.xml.child_count(paragraph.node_id))
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
    next(doc.main.xml.elements(w.Body))(e.p(e.r(e.rPr(e.b()), e.t('alpha be')), e.r(e.rPr(e.i()), e.t('ta gamma'))))
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
    paragraph = next(doc.main.xml.elements(w.Paragraph))
    clause = doc.bookmarks.add(doc.story.find('alpha'), 'clause')
    field = paragraph(clause.ref('1.1', switches=r'\w \h'))
    assert isinstance(field, w.SimpleField) and field.instruction == r' REF clause \w \h ' and next(field.elements(w.Text)).value == '1.1'
    assert doc.bookmarks.add(doc.story.range(0, len(doc.story.text)), 'whole').range.text == 'alpha beta gamma1.1'  # The anchor encloses the field and its cached result.
    doc.story.find('beta').replace('edited beside a field')
    assert doc.story.text == 'alpha edited beside a field gamma1.1' and doc.main.xml.count(w.SimpleField) == 1
    later = paragraph(doc.bookmarks.ref('later', '2.1'))
    assert later.instruction == ' REF later ' and next(later.elements(w.Text)).value == '2.1'
    with pytest.raises(KeyError): doc.bookmarks.ref('later')
    assert doc.bookmarks.ref('clause').bytes() == clause.ref().bytes()
    next(doc.main.xml.elements(w.Body))(e.p(e.r(e.t('see policy'))))
    url = doc.hyperlinks.add(doc.story.find('policy'), 'https://example.com/v1')
    rid = url.element.attribute(R, 'id')
    url.target = 'https://example.com/v2'
    assert url.target == 'https://example.com/v2' and [r['target'] for r in doc.package.relationships(doc.main.uri)] == ['https://example.com/v2'] and url.element.attribute(R, 'id') == rid
    url.target = '#clause'
    assert url.anchor == 'clause' and doc.package.relationships(doc.main.uri) == []

def test_foreign_ranges_and_claimed_owners_cannot_change_relationships():
    docs = [Document.new(), Document.new()]
    for doc in docs: next(doc.main.xml.elements(w.Body))(e.p(e.r(e.t('text'))))
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
    paragraph = e.p(e.r(e.t('a')), e.bookmarkStart(id='1', name='inside'), e.commentRangeStart(id='7'),
        e.r(e.t('b'), e.commentReference(id='7'), e.t('c')), e.commentRangeEnd(id='7'), e.bookmarkEnd(id='1'), e.r(e.t('d')))
    next(doc.main.xml.elements(w.Body))(paragraph)
    link = doc.hyperlinks.add(doc.story.find('abcd'), 'https://example.com/')
    assert link.text == 'abcd' and doc.bookmarks['inside'].range.text == 'bc'
    start, end = next(doc.main.xml.elements(w.CommentRangeStart)), next(doc.main.xml.elements(w.CommentRangeEnd))
    assert doc.story.range(doc.story._position(start), doc.story._position(end)).text == 'bc'
    assert doc.story._position(next(doc.main.xml.elements(w.CommentReference))) == 2
    names = [c.tag.split('}')[-1] for c in fromstring(link.element._tree.xml.subtree_bytes(link.element.node_id))]
    assert names == ['r', 'bookmarkStart', 'commentRangeStart', 'r', 'commentRangeEnd', 'bookmarkEnd', 'r']
    link.remove()
    assert doc.story.text == 'abcd' and doc.bookmarks['inside'].range.text == 'bc'

def test_bookmark_names_are_word_legal():
    assert (bookmark_name('sec-pay'), bookmark_name('1st clause'), bookmark_name('')) == ('sec_pay', 'B1st_clause', 'B')
    assert len(bookmark_name('x' * 50)) == 40

def test_detached_links_share_one_relationship_per_url():
    doc = Document.new()
    body = next(doc.main.xml.elements(w.Body))
    body(e.p(doc.hyperlinks.link('https://example.com/a', e.r(e.t('a'))), doc.hyperlinks.link('#top', e.r(e.t('top')))))
    body(e.p(doc.hyperlinks.link('https://example.com/a', [e.r(e.t('again')), None])))
    assert [link.target for link in doc.hyperlinks] == ['https://example.com/a', '#top', 'https://example.com/a']
    assert sum(r['type'].endswith('/hyperlink') for r in doc.package.relationships(doc.main.uri)) == 1
    assert not doc.validate()['issues']
    with pytest.raises(ValueError): doc.hyperlinks.link('', e.r(e.t('x')))
    with pytest.raises(ValueError): doc.hyperlinks.link('#', e.r(e.t('x')))
