"""Paragraph endpoints and properties, independent of the live text projector."""
import xml.etree.ElementTree as ET
import pytest
from oxml import Document, e, Story, w
from oxml.paragraphs import split_paragraph, join_paragraphs

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
W14 = '{http://schemas.microsoft.com/office/word/2010/wordml}'

def document(lines):
    doc = Document.new()
    body = next(doc.main.xml.elements(w.Body))
    for i, line in enumerate(lines):
        body(e.p(e.pPr(e.pStyle(val=f'Style{i}')), e.r(e.t(line)), w14__paraId=f'{i+1:08X}'))
    return doc

def endpoints(doc):
    return [(''.join(e.text or '' for e in p.iter(W+'t')), p.find(W+'pPr/'+W+'pStyle').get(W+'val'))
            for p in ET.fromstring(doc.main.read_bytes()).findall(W+'body/'+W+'p')]

def test_split_join_retain_last_properties_and_do_not_duplicate_paragraph_ids():
    doc = document(['one two', 'tail'])
    first, last = list(doc.main.xml.elements(w.Paragraph))
    left, right = split_paragraph(first, 4)
    assert right.node_id == first.node_id
    assert endpoints(doc) == [('one ', 'Style0'), ('two', 'Style0'), ('tail', 'Style1')]
    paragraphs = ET.fromstring(doc.main.read_bytes()).findall(W+'body/'+W+'p')
    assert [p.get(W14+'paraId') for p in paragraphs] == [None, '00000001', '00000002']
    assert join_paragraphs(left, right).node_id == first.node_id
    assert join_paragraphs(right, last).node_id == last.node_id
    assert endpoints(doc) == [('one twotail', 'Style1')]

CASES = [
    (['alpha beta', 'gamma delta'], 6, 16, 'B\nG\nH', [('alpha B', 'Style0'), ('G', 'Style0'), ('H delta', 'Style1')]),
    (['abc', 'def'], 3, 4, 'X', [('abcXdef', 'Style1')]),
    (['abcdef'], 2, 4, 'X\nY', [('abX', 'Style0'), ('Yef', 'Style0')]),
    (['', 'z'], 0, 1, '\nX', [('', 'Style0'), ('Xz', 'Style1')]),
]

@pytest.mark.parametrize('lines,start,end,replacement,expected', CASES)
def test_multiline_plain_replacement(lines, start, end, replacement, expected):
    doc = document(lines)
    result = doc.story.range(start, end).replace(replacement)
    assert endpoints(doc) == expected and result.text == replacement

@pytest.mark.parametrize('lines,start,end,replacement,expected', CASES)
@pytest.mark.parametrize('accept', [False, True])
def test_multiline_tracked_endpoints(lines, start, end, replacement, expected, accept):
    doc = document(lines)
    original = endpoints(doc)
    created = doc.revisions.replace(doc.story.range(start, end), replacement, author='Reviewer')
    assert any(revision.is_boundary for revision in created)
    assert doc.story.text == '\n'.join(text for text, _ in expected)
    assert Story(doc.main.xml.root, view='original').text == '\n'.join(lines)
    assert len({revision.id for revision in created}) == len(created)
    doc.revisions.accept_all() if accept else doc.revisions.reject_all()
    assert endpoints(doc) == (expected if accept else original)
    assert not list(doc.revisions)

def test_container_and_section_boundaries_refuse_before_editing():
    doc = Document.new()
    next(doc.main.xml.elements(w.Body))(e.tbl(e.tr(e.tc(e.p(e.r(e.t(value)))) for value in ('a', 'b'))))
    original = doc.bytes()
    with pytest.raises(NotImplementedError): doc.story.range(0, 3).replace('x')
    assert doc.bytes() == original
    first = next(doc.main.xml.elements(w.Paragraph))
    first(e.pPr(e.sectPr()))
    original = doc.bytes()
    with pytest.raises(NotImplementedError): split_paragraph(first, 1)
    assert doc.bytes() == original
