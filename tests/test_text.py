"""Literal story/range workflows; independent XML assertions, no rendering assumptions."""
from pathlib import Path
from xml.etree.ElementTree import fromstring
from xml.dom.minidom import parseString
from zipfile import ZipFile
import pytest
from oxml import Document, e, Tree, Story, w
from oxml.text import _run_text

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
XML = 'http://www.w3.org/XML/1998/namespace'
FIXTURE = Path(__file__).parent/'fixtures/body/inline_formatting.docx'

def paragraph(*children):
    doc = Document.new()
    p = next(doc.main.xml.elements(w.Body))(e.p(*children))
    return doc, Story(p)

def texts(run): return ''.join(n.text or '' for n in run.findall(f'{{{W}}}t'))

def test_real_formatted_cross_run_replacement_preserves_other_parts(tmp_path):
    doc = Document.open(FIXTURE)
    story = doc.story
    original_story = story.text
    assert original_story.split('\n')[0] == 'Regular text italics bold bold italics.'
    selected = story.find('text italics bo')
    assert selected.text == 'text italics bo'
    replacement = selected.replace('café😀 ')
    assert replacement.text == 'café😀 ' and story.text == original_story.replace('text italics bo', 'café😀 ', 1)
    with pytest.raises(ReferenceError): _ = selected.text
    root = fromstring(doc.main.read_bytes())
    runs = root.findall(f'.//{{{W}}}p')[0].findall(f'{{{W}}}r')
    assert [texts(r) for r in runs] == ['Regular ', 'café😀 ', 'ld ', 'bold', ' italics', '.']
    assert runs[1].find(f'{{{W}}}rPr') is None  # First affected run was unformatted.
    assert runs[2].find(f'{{{W}}}rPr/{{{W}}}b') is not None
    assert runs[2].find(f'{{{W}}}t').get(f'{{{XML}}}space') == 'preserve'
    output = tmp_path/'ranges.docx'
    doc.save(output)
    with ZipFile(FIXTURE) as before, ZipFile(output) as after:
        assert all(before.read(n) == after.read(n) for n in before.namelist() if n != 'word/document.xml')
    assert Document.open(output).story.text == story.text

def test_unicode_caret_tabs_breaks_and_empty_paragraph():
    doc, story = paragraph(e.r(e.rPr(e.b()), e.t('a😀bc')))
    assert story.range(1, 2).text == '😀'
    inserted = story.range(2, 2).replace(' é\tX\vY ')
    assert inserted.text == ' é\tX\vY ' and story.text == 'a😀 é\tX\vY bc'
    runs = fromstring(doc.main.read_bytes()).findall(f'.//{{{W}}}r')
    assert [texts(r) for r in runs] == ['a😀', ' éXY ', 'bc']
    assert all(r.find(f'{{{W}}}rPr/{{{W}}}b') is not None for r in runs)
    assert [n.tag.split('}')[-1] for n in runs[1]] == ['rPr', 't', 'tab', 't', 'br', 't']
    story.find('\tX\v').replace('')
    assert story.text == 'a😀 éY bc'
    empty, blank = paragraph(e.pPr(e.jc(val='center')))
    assert blank.range(0, 0).replace('start').text == 'start'
    assert fromstring(empty.main.read_bytes()).find(f'.//{{{W}}}p')[0].tag == f'{{{W}}}pPr'

def test_isolation_preserves_run_xml_context_and_opaque_siblings():
    source = f'<w:p xmlns:w="{W}" xmlns:q="urn:opaque"><w:r token="q:Type"><w:rPr><w:b/></w:rPr>' \
             '<w:t>ab</w:t><!--between--><?keep value?><w:t>cdef</w:t><q:other/></w:r></w:p>'
    tree = Tree(source.encode())
    story = Story(tree.root)
    assert story.text == 'abcdef\ufffc'
    original = tree.bytes()
    with pytest.raises(NotImplementedError): story.range(1, 4).replace('would discard XML metadata')
    assert tree.bytes() == original
    selected = story.range(3, 4)
    p, runs, index, template = selected._isolate()
    assert [''.join(t.value for t in r.children if isinstance(t, w.Text)) for r in runs] == ['d']
    assert index == 1 and template.attribute('', 'token') == 'q:Type'
    replacement = p(_run_text(template, 'NEW'), index=index)
    for run in runs: run.delete()
    assert Story(p).text == 'abcNEWef\ufffc'
    parsed = fromstring(tree.bytes())
    assert len(parsed.findall('.//{urn:opaque}other')) == 1
    assert all(r.get('token') == 'q:Type' and r.find(f'{{{W}}}rPr/{{{W}}}b') is not None for r in parsed)
    assert dict(replacement.raw['namespaces'])['q'] == 'urn:opaque'
    children = parseString(tree.bytes()).getElementsByTagNameNS(W, 'r')[0].childNodes
    assert [n.data for n in children if n.nodeType in (n.COMMENT_NODE, n.PROCESSING_INSTRUCTION_NODE)] == ['between', 'value']

@pytest.mark.parametrize('unsupported', [e.fldSimple(e.r(e.t('result'))),
    e.r(e.fldChar(fldCharType='begin')), e.sdt(e.sdtContent()),
    e.r(e.drawing(e.txbxContent()))])
def test_unsupported_paragraphs_refuse_before_mutation(unsupported):
    doc, story = paragraph(e.r(e.t('safe')), unsupported)
    assert story.text == 'safe\ufffc'
    original = doc.bytes()
    with pytest.raises(NotImplementedError): story.range(0, 2).replace('changed')
    assert doc.bytes() == original

def test_paragraph_and_opaque_boundaries_staleness_and_invalid_text_are_explicit():
    doc, first = paragraph(e.r(e.t('one')))
    body = next(doc.main.xml.elements(w.Body))
    body(e.p(e.r(e.t('two'))))
    story = Story(body)
    assert story.text == 'one\ntwo' and story.find('e\nt').text == 'e\nt'
    original = doc.bytes()
    for value in ('\r', '\x00'):
        with pytest.raises((ValueError, NotImplementedError)): first.range(0, 1).replace(value)
        assert doc.bytes() == original
    with pytest.raises(NotImplementedError): story.find('e\nt')._preflight()
    assert doc.bytes() == original
    selected = first.find('one')
    next(doc.main.xml.elements(w.Text)).value = 'raw edit'
    with pytest.raises(ReferenceError): selected.replace('stale')
    assert first.text == 'raw edit'

def test_real_comment_annotation_label_is_not_story_text():
    path = FIXTURE.parent.parent/'reviews/libreoffice/CommentDone.docx'
    comments = Document.open(path).package.part('/word/comments.xml').xml
    comment = next(comments.elements(w.Comment))
    expected = fromstring(comments.xml.subtree_bytes(comment.node_id))
    assert expected.find(f'.//{{{W}}}annotationRef') is not None
    assert Story(comment).text == '\n'.join(''.join(t.text or '' for t in p.iter(f'{{{W}}}t')) for p in expected.iter(f'{{{W}}}p'))
    with pytest.raises(NotImplementedError): Story(comment).range(0, 0)._isolate()

def test_real_current_original_views_edit_beside_reviews_and_keep_comment_anchors(tmp_path):
    path = FIXTURE.parent.parent/'pandoc/track_changes_scrubbed_metadata.docx'
    doc = Document.open(path)
    original = Story(doc.story.element, view='original')
    assert doc.story.text == 'Here is a test document.' and original.text == 'Here is a dummy document.'
    assert doc.bytes() == path.read_bytes()
    before = [fromstring(doc.main.xml.xml.subtree_bytes(r.element.node_id)) for r in doc.revisions]
    for span in (doc.story.find('test'), doc.story.range(12, 12), original.find('dummy')):
        with pytest.raises(NotImplementedError): span.replace('blocked')
        assert doc.bytes() == path.read_bytes()
    doc.story.find('Here').replace('This')
    doc.story.find('document').replace('agreement')
    start = next(doc.main.xml.elements(w.CommentRangeStart))
    end = next(doc.main.xml.elements(w.CommentRangeEnd))
    assert doc.story.range(doc.story._position(start), doc.story._position(end)).text == 'agreement'
    assert original.text == 'This is a dummy agreement.'
    position = doc.story.find('test').start
    doc.story.range(position, position).replace('[')
    position = doc.story.find('test').end
    doc.story.range(position, position).replace(']')
    assert doc.story.text == 'This is a [test] agreement.' and original.text == 'This is a dummy[] agreement.'
    after = [fromstring(doc.main.xml.xml.subtree_bytes(r.element.node_id)) for r in doc.revisions]
    assert [(r.tag, r.attrib, ''.join(r.itertext())) for r in after] == [(r.tag, r.attrib, ''.join(r.itertext())) for r in before]
    output = tmp_path/'review-text.docx'
    doc.save(output)
    with ZipFile(path) as old, ZipFile(output) as saved:
        assert all(old.read(n) == saved.read(n) for n in old.namelist() if n != 'word/document.xml')
    assert Document.open(output).story.text == doc.story.text

def test_interior_markers_survive_replacement_and_hidden_revisions_remain_protected():
    doc, story = paragraph(e.r(e.t('A')), e.commentRangeStart(id='7'),
        e.r(e.t('BC'), e.commentReference(id='7'), e.t('D')),
        e.bookmarkStart(id='8', name='inside'), e.r(e.t('EF')),
        e.bookmarkEnd(id='8'), e.commentRangeEnd(id='7'), e.r(e.t('G')),
        e.del_(e.r(e.delText('old')), id='9', author='a'), e.r(e.t('H')),
        e.ins(e.r(e.t('NEW')), id='10', author='a'))
    assert story.text == 'ABCDEFGHNEW'
    story.find('CDEF').replace('Z')
    assert story.text == 'ABZGHNEW' and Story(story.element, view='original').text == 'ABZGoldH'
    for cls in (w.BookmarkStart, w.BookmarkEnd, w.CommentReference, w.CommentRangeEnd):
        marker, = doc.main.xml.elements(cls)
        assert story._position(marker) == 3
    assert story._position(next(doc.main.xml.elements(w.CommentRangeStart))) == 1
    before = doc.bytes()
    with pytest.raises(NotImplementedError): story.find('GH').replace('crosses hidden deletion')
    assert doc.bytes() == before
    story.range(4, 4).replace('?')
    assert story.text == 'ABZG?HNEW'
