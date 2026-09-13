"""Property history endpoints from PowerTools RevisionProcessor; independent XML expectations."""
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET
import pytest
from oxml import E, e, Tree, Story, Revisions, w
from corpus_helpers import open_original, saved_edit

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
DATE = datetime(2026, 9, 13, tzinfo=timezone.utc)
FIXTURE = Path(__file__).parent/'fixtures/reviews/powertools/RP024-ParagraphMark-rPr-Change.docx'

@pytest.mark.parametrize('accept', [True, False])
def test_existing_paragraph_mark_formatting(accept, tmp_path):
    doc, expected = open_original(FIXTURE, 'word/document.xml')
    revision, = doc.revisions
    assert (revision.kind, revision.author, revision.date) == ('rPrChange', 'Eric White', '2017-03-26T05:02:00Z')
    assert [c.raw['qname'][1] for c in revision.previous.children] == ['lang']
    assert [c.raw['qname'][1] for c in revision.current.children] == ['b', 'lang', 'rPrChange']
    history = expected['word/document.xml'].getElementsByTagNameNS(W[1:-1], 'rPrChange')[0]
    current = history.parentNode
    if accept: current.removeChild(history)
    else: current.parentNode.replaceChild(history.getElementsByTagNameNS(W[1:-1], 'rPr')[0].cloneNode(True), current)
    assert (doc.revisions.accept_all() if accept else doc.revisions.reject_all()) == 1
    with pytest.raises(ReferenceError): _ = revision.current
    assert not list(saved_edit(doc, FIXTURE, tmp_path/'formatting.docx', expected).revisions)

@pytest.mark.parametrize('kind', ['r', 'p'])
@pytest.mark.parametrize('accept', [True, False])
def test_create_direct_formatting_and_restore_snapshot(kind, accept):
    x = E('w', attr_ns='w', ns={'q': 'urn:opaque'})
    source = x.p(x.pPr(x.jc(val='right'), x.rPr(x.lang(val='en-US')), x.sectPr(x.cols(space='720'))),
        x.r(x.rPr(x.b(), x('q:opaque'), attrs_={'token': 'q:Type'}), x.t('unchanged'))).bytes()
    tree = Tree(source)
    target = tree.root if kind == 'p' else next(tree.elements(w.Run))
    properties = e.pPr(e.jc(val='center')) if kind == 'p' else e.rPr(e.i())
    if kind == 'r': properties = Tree(properties.bytes()).root  # Parsed input is snapshotted, not moved.
    revisions = Revisions(Story(tree.root))
    change = revisions.format(target, properties, author='Reviewer', date=DATE)
    assert (change.kind, change.id, change.author, change.date) == (kind+'PrChange', '0', 'Reviewer', '2026-09-13T00:00:00Z')
    before = change.previous
    assert before.raw['qname'][1] == kind+'Pr'
    if kind == 'r':
        assert before.attribute('', 'token') == 'q:Type' and dict(before.raw['namespaces'])['q'] == 'urn:opaque'
        properties.append_xml(e.b().bytes())
        assert [c.raw['qname'][1] for c in change.current.children] == ['i', 'rPrChange']
    else: assert [c.raw['qname'][1] for c in before.children] == ['jc']
    change.accept() if accept else change.reject()
    result = ET.fromstring(tree.bytes())
    assert Story(tree.root).text == 'unchanged' and not list(revisions)
    assert result.find(W+'pPr/'+W+'rPr/'+W+'lang').get(W+'val') == 'en-US'
    assert result.find(W+'pPr/'+W+'sectPr/'+W+'cols').get(W+'space') == '720'
    if not accept:
        assert ET.tostring(result) == ET.tostring(ET.fromstring(source))
        assert dict(target.children[0].raw['namespaces'])['q'] == 'urn:opaque'
    elif kind == 'p': assert result.find(W+'pPr/'+W+'jc').get(W+'val') == 'center'
    else: assert [c.tag for c in result.find(W+'r/'+W+'rPr')] == [W+'i']

def test_missing_properties_and_conflicting_history_refuse_before_mutation():
    tree = Tree(e.p(e.r(e.t('text'))).bytes())
    target, revisions = next(tree.elements(w.Run)), Revisions(Story(tree.root))
    change = revisions.format(target, e.rPr(e.b()), author='Reviewer', date=DATE)
    assert not change.previous.children
    original = tree.bytes()
    for properties in (e.rPr(e.i()), e.pPr()):
        with pytest.raises((ValueError, NotImplementedError)): revisions.format(target, properties, author='Other', date=DATE)
        assert tree.bytes() == original
    nested, = change.previous.append_xml(e.rPrChange(e.rPr(), id='9', author='Other').bytes())
    original = tree.bytes()
    for operation in (change.accept, change.reject):
        with pytest.raises(NotImplementedError): operation()
        assert tree.bytes() == original
    nested.delete()
    change.reject()
    assert not target.children[0].children and Story(tree.root).text == 'text'
    original = tree.bytes()
    with pytest.raises(ValueError): revisions.format(target, e.rPr(), author='bad\x00author', date=DATE)
    assert tree.bytes() == original
