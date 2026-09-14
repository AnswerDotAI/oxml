'Inline review endpoints, with independent XML checks and unchanged real DOCX inputs.'
from datetime import datetime, timezone
from pathlib import Path
import xml.etree.ElementTree as ET, pytest
from oxml import Document, e, Tree, w, Story, Revisions
from corpus_helpers import parts

FIXTURES = Path(__file__).parent/'fixtures'
W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
DATE = datetime(2026, 9, 13, 10, 30, tzinfo=timezone.utc)

def run_text(run):
    return ''.join(e.text or '' if e.tag in {W+'t', W+'delText'} else '\t' if e.tag == W+'tab' else '\v'
        for e in run if e.tag in {W+'t', W+'delText', W+'tab', W+'br', W+'cr'})

def test_respond_to_existing_review_without_consuming_earlier_changes(tmp_path):
    doc = Document.open(FIXTURES/'pandoc/track_changes_scrubbed_metadata.docx')
    changes = doc.revisions.replace(doc.story.find('document'), 'agreement', author='Drafter', date=DATE)
    comment = doc.comments[3]
    reply = comment.reply('Updated the terminology.', 'Drafter', date=DATE)
    comment.resolve()
    for change in changes: change.accept()
    output = tmp_path/'response.docx'
    doc.save(output)
    reopened = Document.open(output)
    assert reopened.story.text == 'Here is a test agreement.'
    assert Story(reopened.story.element, view='original').text == 'Here is a dummy agreement.'
    assert reopened.comments[3].resolved and reopened.comments[reply.id].parent.id == 3
    assert reopened.comments[3].range.text == 'agreement'
    xml = ET.fromstring(reopened.main.read_bytes())
    assert [e.text for e in xml.iter(W+'delText')] == ['dummy']
    assert [''.join(e.itertext()) for e in xml.iter(W+'ins')] == ['test']

@pytest.mark.parametrize('accept,word', [(True, 'test'), (False, 'dummy')])
def test_existing_pandoc_revision_endpoints(accept, word, tmp_path):
    # Pandoc track_changes_scrubbed_metadata.docx: inline substitution with surrounding bookmarks/comments.
    path = FIXTURES/'pandoc/track_changes_scrubbed_metadata.docx'
    doc = Document.open(path)
    before = parts(doc)
    revisions = doc.revisions
    assert [(r.kind, r.id, r.author, r.date, r.text) for r in revisions] == [
        ('del', '1', 'Author', None, 'dummy'), ('ins', '2', 'Author', None, 'test')]
    assert doc.bytes() == path.read_bytes()
    assert (revisions.accept_all() if accept else revisions.reject_all()) == 2
    output = tmp_path/'reviewed.docx'
    doc.save(output)
    reopened = Document.open(output)
    after = parts(reopened)
    assert {k: v for k, v in after.items() if k != 'word/document.xml'} == {
        k: v for k, v in before.items() if k != 'word/document.xml'}
    paragraph = ET.fromstring(after['word/document.xml']).find('.//'+W+'p')
    assert [e.tag.removeprefix(W) for e in paragraph] == [
        'bookmarkStart', 'bookmarkEnd', 'r', 'r', 'r', 'commentRangeStart', 'r', 'commentRangeEnd', 'r', 'r']
    assert [run_text(r) for r in paragraph.findall(W+'r')] == ['Here is a ', word, ' ', 'document', '', '.']
    assert paragraph.find(W+'commentRangeStart').get(W+'id') == paragraph.find(W+'commentRangeEnd').get(W+'id') == '3'
    assert paragraph.find(W+'r/'+W+'commentReference').get(W+'id') == '3'
    assert not list(reopened.revisions)

@pytest.mark.parametrize('accept', [True, False])
def test_tracked_replacement_across_formatted_runs(accept):
    doc = Document.new()
    body = next(doc.main.xml.elements(w.Body))
    paragraph = body(e.p(e.bookmarkStart(id='0', name='retained'), e.r(e.rPr(e.b()), e.t('alpha be')), e.r(e.rPr(e.i()), e.t('ta gamma')),
        e.bookmarkEnd(id='0')))
    story = Story(paragraph)
    revisions = Revisions(story)
    deletion, insertion = revisions.replace(story.find('beta'), 'B\tC\vD', author='Jeremy', date=DATE)
    assert [(r.kind, r.id, r.author, r.date, r.text) for r in (deletion, insertion)] == [
        ('del', '1', 'Jeremy', '2026-09-13T10:30:00Z', 'beta'), ('ins', '2', 'Jeremy', '2026-09-13T10:30:00Z', 'B\tC\vD')]
    root = ET.fromstring(doc.main.read_bytes()).find('.//'+W+'p')
    assert [e.text for e in root.findall(W+'del/'+W+'r/'+W+'delText')] == ['be', 'ta']
    assert root.find(W+'del/'+W+'r/'+W+'t') is None
    for revision in (deletion, insertion): revision.accept() if accept else revision.reject()
    root = ET.fromstring(doc.main.read_bytes()).find('.//'+W+'p')
    runs = [(run_text(r), 'bold' if r.find(W+'rPr/'+W+'b') is not None else 'italic') for r in root.findall(W+'r')]
    assert runs == ([('alpha ', 'bold'), ('B\tC\vD', 'bold'), (' gamma', 'italic')] if accept else
        [('alpha ', 'bold'), ('be', 'bold'), ('ta', 'italic'), (' gamma', 'italic')])
    assert root[0].tag == W+'bookmarkStart' and root[-1].tag == W+'bookmarkEnd'
    with pytest.raises(ReferenceError): _ = deletion.text

@pytest.mark.parametrize('start,end,text,kind,accepted', [(1, 1, 'X', 'ins', 'aXbc'), (1, 2, '', 'del', 'ac')])
def test_insertion_and_deletion(start, end, text, kind, accepted):
    for accept in (False, True):
        tree = Tree(e.p(e.r(e.t('abc'))).bytes())
        story = Story(tree.root)
        revision, = Revisions(story).replace(story.range(start, end), text, author='Reviewer', date=DATE)
        assert revision.kind == kind
        revision.accept() if accept else revision.reject()
        assert ''.join(e.text or '' for e in ET.fromstring(tree.bytes()).iter(W+'t')) == (accepted if accept else 'abc')

def test_unsupported_move_revisions_do_not_mutate():
    doc = Document.open(FIXTURES/'reviews/pandoc/track_changes_move.docx')
    original = doc.bytes()
    revisions = Revisions(Story(doc.main.xml.root))
    assert {r.kind for r in revisions} >= {'moveFrom', 'moveTo'}  # Listing never preflights acceptance.
    for operation in (revisions.accept_all, revisions.reject_all):
        with pytest.raises(NotImplementedError): operation()
        assert doc.bytes() == original

@pytest.mark.parametrize('accept,expected', [(True, ['This is a', ' splitParagraph.']), (False, ['This is a split', 'Paragraph.'])])
def test_existing_pandoc_paragraph_boundaries(accept, expected):
    doc = Document.open(FIXTURES/'pandoc/paragraph_insertion_deletion.docx')
    before = parts(doc)
    assert [(r.kind, r.is_boundary, r.text) for r in doc.revisions] == [('ins', True, '\n'), ('del', True, '\n')]
    doc.revisions.accept_all() if accept else doc.revisions.reject_all()
    after = parts(doc)
    # PowerTools collapses paragraph contents verbatim, without inserting whitespace at a removed mark.
    assert [''.join(e.text or '' for e in p.iter(W+'t')) for p in ET.fromstring(after['word/document.xml']).findall(W+'body/'+W+'p')] == expected
    assert {k: v for k, v in after.items() if k != 'word/document.xml'} == {k: v for k, v in before.items() if k != 'word/document.xml'}

def test_bulk_preflight_and_invalid_new_metadata():
    good = e.ins(e.r(e.t('keep')), id='1', author='Reviewer')
    nested = e.del_(good, id='2', author='Reviewer')
    tree = Tree(e.body(e.p(good), e.p(nested)).bytes())
    original = tree.bytes()
    assert [r.kind for r in Revisions(Story(tree.root))] == ['ins', 'del']  # Listing never preflights acceptance.
    for operation in (Revisions(Story(tree.root)).accept_all, Revisions(Story(tree.root)).reject_all):
        with pytest.raises(NotImplementedError): operation()
        assert tree.bytes() == original
    ordinary = Tree(e.p(e.r(e.t('abc'))).bytes())
    story, original = Story(ordinary.root), ordinary.bytes()
    with pytest.raises(ValueError): Revisions(story).replace(story.find('b'), 'x', author='bad\x00author', date=DATE)
    assert ordinary.bytes() == original

def test_creation_inside_revision_marked_cell_is_refused():
    # PowerTools RP034/RP035 use cellDel/cellIns in tcPr: editing text must not silently treat that cell as ordinary.
    tree = Tree(e.tbl(e.tr(e.tc(e.tcPr(e.cellDel(id='0', author='Reviewer')), e.p(e.r(e.t('abc')))))).bytes())
    story, original = Story(next(tree.elements(w.Paragraph))), tree.bytes()
    with pytest.raises(NotImplementedError): Revisions(story).replace(story.find('b'), 'x', author='Reviewer', date=DATE)
    assert tree.bytes() == original
