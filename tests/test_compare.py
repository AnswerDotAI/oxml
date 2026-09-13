"""PowerTools WmlComparerTests WC-1020/1210/1560: text, paragraph and formatting endpoints.

Unlike upstream's compare-again sanity checks, endpoint expectations use independent XML.
"""
from datetime import datetime, timezone
from pathlib import Path
import xml.etree.ElementTree as ET
import pytest
from oxml import Document, e, Story, compare, w
from corpus_helpers import parts

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
DATE = datetime(2026, 1, 2, tzinfo=timezone.utc)

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
    before = document(paragraph(('Terms: ', ''), ('fourteen', 'b'), (' days.', '')), table,
                      paragraph(('A unchanged Z', '')))
    after = document(paragraph(('Terms: ', 'i'), ('twenty-', 'b'), ('one', 'i'), (' days!', ''), align='right'), table,
                     paragraph(('B unchanged Y', '')))
    original, revised = parts(before), parts(after)
    result = compare(before, after, author='Reviewer', date=DATE)
    assert result.story.text == after.story.text
    assert Story(result.main.xml.root, view='original').text == before.story.text
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

@pytest.mark.parametrize('lines', [(['abc def'], ['abc', 'new', ' def']), (['abc', 'def'], ['abcdef']),
                                 (['', 'z'], ['z']), ([''], ['a', 'b'])])
@pytest.mark.parametrize('accept', [False, True])
def test_paragraph_count_endpoints(lines, accept):
    before, after = [document(*(paragraph((text, 'b')) for text in values)) for values in lines]
    result = compare(before, after, author='Reviewer')
    assert result.story.text == after.story.text
    assert Story(result.main.xml.root, view='original').text == before.story.text
    result.revisions.accept_all() if accept else result.revisions.reject_all()
    assert endpoint(result) == endpoint(after if accept else before)

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
    settings = after._part('DocumentSettingsPart').xml.root
    for child in settings.children:
        if child.qname == (W[1:-1], 'rsids'): child.delete()
    settings(e.rsids(e.rsid(val='01020304')))
    result = compare(before, after, author='Reviewer')
    assert result.package.read_part(core) == before.package.read_part(core)
    result.revisions.accept_all()
    assert endpoint(result) == endpoint(after)
    after.package.replace_part(style, after.package.read_part(style).replace(b'<w:styles', b'<w:styles test="changed"', 1))
    with pytest.raises(NotImplementedError, match='dependencies'): compare(before, after, author='Reviewer')

def test_unsupported_changes_and_existing_revisions_are_not_flattened():
    before = document(paragraph(('abc', '')))
    after = Document.from_bytes(before.bytes())
    next(after.main.xml.elements(w.Paragraph))(e.fldSimple(instr='DATE'))
    with pytest.raises(NotImplementedError, match='ordinary runs'): compare(before, after, author='Reviewer')
    before.revisions.replace(before.story.find('b'), 'B', author='Other')
    with pytest.raises(NotImplementedError, match='existing revisions'): compare(before, before, author='Reviewer')

def test_break_metadata_and_foreign_xml_whitespace_are_not_silently_discarded():
    before = document(paragraph(('abc', '')))
    after = Document.from_bytes(before.bytes())
    next(after.main.xml.elements(w.Run))(e.br(clear='all'))
    with pytest.raises(NotImplementedError, match='metadata'): compare(before, after, author='Reviewer')
    after = Document.from_bytes(before.bytes())
    for doc, spaces in [(before, ' '), (after, '  ')]:
        doc.package.add_part('/customXml/item.xml', 'application/xml',
            f'<x:root xmlns:x="urn:custom" xml:space="preserve"><x:item/>{spaces}</x:root>'.encode())
    with pytest.raises(NotImplementedError, match='dependencies'): compare(before, after, author='Reviewer')
