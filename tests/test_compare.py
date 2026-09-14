"""Comparison endpoints for text, paragraph, formatting, table, field, hyperlink and bookmark changes.

Unlike upstream PowerTools' compare-again sanity checks, endpoint expectations use independent XML.
"""
from datetime import datetime, timezone
from pathlib import Path
import xml.etree.ElementTree as ET
import pytest
from oxml import Document, e, Story, compare, namespace_uris, w
from corpus_helpers import parts

W = f"{{{namespace_uris['w']}}}"
DATE = datetime(2026, 1, 2, tzinfo=timezone.utc)

def views(result): return (Story(result.main.xml.root, view='original').text, result.story.text)

def paragraph(*runs, align='left'):
    return e.p(e.pPr(e.jc(val=align)),
               (e.r(e.rPr(e(style)) if style else None, e.t(text)) for text, style in runs))

def document(*children):
    doc = Document.new()
    body = next(doc.main.xml.elements(w.Body))
    for child in children: body(child)
    return doc

def endpoint(doc):
    result = []
    for p in ET.fromstring(doc.main.read_bytes()).findall(W+'body/'+W+'p'):
        chars = []
        for run in p.findall(W+'r'):
            properties = run.find(W+'rPr')
            formatting = () if properties is None else tuple((c.tag, tuple(sorted(c.attrib.items()))) for c in properties)
            for node in run:
                text = (node.text or '') if node.tag == W+'t' else '\t' if node.tag == W+'tab' else '\v' if node.tag == W+'br' else ''
                chars.extend((char, formatting) for char in text)
        jc = p.find(W+'pPr/'+W+'jc')
        result.append((tuple(chars), None if jc is None else jc.get(W+'val')))
    return result

@pytest.mark.parametrize('accept', [False, True])
def test_text_and_formatting_endpoints_preserve_equal_and_opaque_regions(accept):
    table = e.tbl(e.tr(e.tc(e.p(e.r(e.t('Opaque table'))))))
    before = document(paragraph(('Préavis: ', ''), ('four', 'b'), ('teen', 'i'), (' days.', '')), table,
                      paragraph(('A unchanged Z', '')))
    after = document(paragraph(('Préavis: ', 'i'), ('thir', 'b'), ('ty', 'i'), (' days!', ''), align='right'), table,
                     paragraph(('B unchanged Y', '')))
    original, revised = parts(before), parts(after)
    result = compare(before, after, author='Reviewer', date=DATE)
    assert views(result) == (before.story.text, after.story.text)
    first = ET.fromstring(result.main.read_bytes()).find(W+'body/'+W+'p')
    assert [''.join(r.itertext()) for r in first.findall(W+'del')] == ['fourteen', '.']
    assert [''.join(r.itertext()) for r in first.findall(W+'ins')] == ['thirty', '!']
    changes = list(result.revisions)
    assert {'ins', 'del', 'rPrChange', 'pPrChange'} <= {r.kind for r in changes}
    assert all(r.author == 'Reviewer' and r.date == '2026-01-02T00:00:00Z' for r in changes)
    assert parts(before) == original and parts(after) == revised
    assert {n: p for n, p in parts(result).items() if n != 'word/document.xml'} == {
        n: p for n, p in original.items() if n != 'word/document.xml'}
    result = Document.from_bytes(result.bytes())
    result.revisions.accept_all() if accept else result.revisions.reject_all()
    assert endpoint(result) == endpoint(after if accept else before)
    assert not list(result.revisions)
    root = ET.fromstring(result.main.read_bytes())
    assert ET.tostring(root.find(W+'body/'+W+'tbl')) == ET.tostring(ET.fromstring(original['word/document.xml']).find(W+'body/'+W+'tbl'))
    assert any(t.text == ' unchanged ' for t in root.iter(W+'t'))

@pytest.mark.parametrize('lines', [(['abc def'], ['abc', 'new', ' def']), (['abc ', 'def'], ['abc def']),
                                 (['', 'z'], ['z']), ([''], ['a', 'b'])])
@pytest.mark.parametrize('accept', [False, True])
def test_paragraph_split_join_insert_and_delete_endpoints(lines, accept):
    before, after = [document(*(paragraph((text, 'b')) for text in values)) for values in lines]
    result = compare(before, after, author='Reviewer')
    assert views(result) == (before.story.text, after.story.text)
    if ''.join(lines[0]) == ''.join(lines[1]): assert all(r.is_boundary for r in result.revisions)
    result.revisions.accept_all() if accept else result.revisions.reject_all()
    assert endpoint(result) == endpoint(after if accept else before)

@pytest.mark.parametrize('accept', [False, True])
def test_block_alignment_around_tables_and_container_ends(accept):
    table = e.tbl(e.tr(e.tc(e.p(e.r(e.t('Opaque table'))))))
    before = document(paragraph(('Heading', 'b'), align='center'), paragraph(('alpha', '')), table,
        paragraph(('beta', '')), paragraph(('gamma', 'i')), paragraph(('delta', '')))
    after = document(paragraph(('Heading', 'b'), align='center'), paragraph(('alpha', '')), paragraph(('inserted', 'i')), table,
        paragraph(('beta changed', '')), paragraph(('delta', '')), paragraph(('epsilon', ''), align='right'))
    result = compare(before, after, author='Reviewer')
    assert views(result) == (before.story.text, after.story.text)
    texts = [r.text for r in result.revisions if r.kind in ('ins', 'del') and not r.is_boundary]
    assert {'inserted', 'epsilon'} <= set(texts) and not {'Heading', 'alpha', 'delta'} & set(texts)
    result.revisions.accept_all() if accept else result.revisions.reject_all()
    assert endpoint(result) == endpoint(after if accept else before)
    assert not list(result.revisions)

@pytest.mark.parametrize('accept', [False, True])
def test_table_cells_compare_locally_beside_changed_paragraphs(accept):
    def table(*cells):
        return e.tbl(e.tblPr(e.tblW(type='auto', w=0)), e.tblGrid(*(e.gridCol(w=2000) for _ in cells)),
            e.tr(*(e.tc(e.tcPr(e.tcW(type='dxa', w=2000)), e.p(e.r(e.t(cell)))) for cell in cells)))
    before = document(paragraph(('Fees', '')), table('Remote desk', '4 hours'), paragraph(('after', '')))
    after = document(paragraph(('Fees and levels', '')), table('Remote desk', '2 hours'), paragraph(('after', '')))
    result = compare(before, after, author='Reviewer')
    assert views(result) == (before.story.text, after.story.text)
    assert [r.text for r in result.revisions if not r.is_boundary] == [' and levels', '4', '2']
    result.revisions.accept_all() if accept else result.revisions.reject_all()
    assert result.story.text == (after if accept else before).story.text
    assert not result.validate()['issues']

@pytest.mark.parametrize('accept', [False, True])
def test_hyperlink_targets_and_field_instructions_replace_whole_spans(accept):
    docs = []
    for target, switches, text in [('v1', r'\w \h', 'guide'), ('v2', r'\r \h', 'guides')]:
        doc = document(paragraph(('see the ', ''), ('policy', ''), (' now', '')), paragraph(('read the ', ''), (text, '')))
        doc.hyperlinks.add(doc.story.find('policy'), f'https://example.com/{target}')
        doc.hyperlinks.add(doc.story.find(text), 'https://example.com/guide')
        next(doc.main.xml.elements(w.Body))(e.p(doc.bookmarks.ref('Cap', '1.1', switches)))
        next(doc.main.xml.elements(w.Body))(paragraph(('keep ', ''), ('link', '')))
        doc.hyperlinks.add(doc.story.find('link'), 'https://example.com/keep')
        docs.append(doc)
    before, after = docs
    result = compare(before, after, author='Reviewer')
    assert result.main.xml.count(w.Hyperlink) == 1  # An unchanged linked paragraph is retained as it was.
    assert views(result) == (before.story.text, after.story.text)
    assert [r.text for r in result.revisions if not r.is_boundary] == ['policy', 'policy', 'guide', 'guides', '1.1', '1.1']
    assert [f.value for f in result.main.xml.elements(w.FieldCode)] == [' HYPERLINK "https://example.com/v2" ', ' HYPERLINK "https://example.com/guide" ', r' REF Cap \r \h ']
    assert [f.value for f in result.main.xml.elements(w.DeletedFieldCode)] == [' HYPERLINK "https://example.com/v1" ', r' REF Cap \w \h ']
    result.revisions.accept_all() if accept else result.revisions.reject_all()
    assert result.story.text == (after if accept else before).story.text
    assert [f.value for f in result.main.xml.elements(w.FieldCode)] == (
        [' HYPERLINK "https://example.com/v2" ', ' HYPERLINK "https://example.com/guide" ', r' REF Cap \r \h '] if accept else
        [' HYPERLINK "https://example.com/v1" ', ' HYPERLINK "https://example.com/guide" ', r' REF Cap \w \h '])
    assert result.main.xml.count(w.DeletedFieldCode) == 0 and not result.validate()['issues']

@pytest.mark.parametrize('accept', [False, True])
def test_moved_bookmarked_paragraph_keeps_one_bookmark(accept):
    before = document(paragraph(('cap', '')), paragraph(('first', '')), paragraph(('last', '')))
    after = document(paragraph(('first', '')), paragraph(('last', '')), paragraph(('cap', '')))
    for doc in (before, after): doc.bookmarks.add(doc.story.find('cap'), 'Cap')
    result = compare(before, after, author='Reviewer')
    assert views(result) == (before.story.text, after.story.text)
    assert [b.name for b in result.bookmarks] == ['Cap'] and result.bookmarks['Cap'].range.text == 'cap'
    result.revisions.accept_all() if accept else result.revisions.reject_all()
    assert result.story.text == (after if accept else before).story.text
    assert all(b.name == 'Cap' and b.range.text == 'cap' for b in result.bookmarks)
    assert not result.validate()['issues']

def test_real_docx_noop_metadata_noise_and_changed_dependency_refusal():
    path = Path(__file__).parent/'fixtures/body/inline_formatting.docx'
    before = Document.open(path)
    assert compare(before, before, author='Reviewer').bytes() == path.read_bytes()
    after = Document.from_bytes(before.bytes())
    after.story.find('Regular').replace('Ordinary')
    core = next(n for n in after.package.part_names() if n.endswith('/core.xml'))
    after.package.replace_part(core, after.package.read_part(core).replace(b'</cp:coreProperties>',
        b'<cp:keywords>Changed metadata</cp:keywords></cp:coreProperties>'))
    assert after.package.read_part(core) != before.package.read_part(core)
    style = next(n for n in after.package.part_names() if n.endswith('/styles.xml'))
    after.package.replace_part(style, after.package.read_part(style).replace(b'><', b'>\n<'))
    settings = after.part('DocumentSettingsPart').xml.root
    for child in settings.children:
        if child.qname == (namespace_uris['w'], 'rsids'): child.delete()
    settings(e.rsids(e.rsid(val='01020304')))
    result = compare(before, after, author='Reviewer')
    assert result.package.read_part(core) == before.package.read_part(core)
    result.revisions.accept_all()
    assert endpoint(result) == endpoint(after)
    after.package.replace_part(style, after.package.read_part(style).replace(b'<w:styles', b'<w:styles test="changed"', 1))
    with pytest.raises(NotImplementedError, match='dependencies'): compare(before, after, author='Reviewer')

def test_empty_field_insertion_and_existing_revision_refusal():
    before = document(paragraph(('abc', '')))
    after = Document.from_bytes(before.bytes())
    next(after.main.xml.elements(w.Paragraph))(e.fldSimple(instr='DATE'))
    result = compare(before, after, author='Reviewer')
    assert result.story.text == 'abc￼' and [f.value for f in result.main.xml.elements(w.FieldCode)] == ['DATE']
    result.revisions.reject_all()
    assert result.story.text == 'abc' and not result.validate()['issues']
    before.revisions.replace(before.story.find('b'), 'B', author='Other')
    with pytest.raises(NotImplementedError, match='existing revisions'): compare(before, before, author='Reviewer')

def test_break_metadata_and_foreign_xml_whitespace_are_not_silently_discarded():
    before = document(paragraph(('abc', '')))
    after = Document.from_bytes(before.bytes())
    next(after.main.xml.elements(w.Run))(e.br(clear='all'))
    result = compare(before, after, author='Reviewer')
    assert next(result.main.xml.elements(w.Break)).attribute(namespace_uris['w'], 'clear') == 'all'
    after = Document.from_bytes(before.bytes())
    for doc, spaces in [(before, ' '), (after, '  ')]:
        doc.package.add_part('/customXml/item.xml', 'application/xml',
            f'<x:root xmlns:x="urn:custom" xml:space="preserve"><x:item/>{spaces}</x:root>'.encode())
    with pytest.raises(NotImplementedError, match='dependencies'): compare(before, after, author='Reviewer')
