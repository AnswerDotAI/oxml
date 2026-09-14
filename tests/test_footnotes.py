"Footnote creation, live referencing and removal, with Word's separator notes and styles."
from oxml import Document, e, w

def test_footnotes_create_reference_and_delete():
    doc = Document.new()
    body = next(doc.main.xml.elements(w.Body))
    paragraph = body(e.p(e.r(e.t('Payment is due on a Business Day.'))))
    live = doc.footnotes.add(doc.story.find('Business Day'), 'Any day other than a Saturday, Sunday or public holiday.')
    table = e.tbl(e.tblPr(), e.tblGrid(e.gridCol(w=2000)), e.tr(e.tc(e.p(e.r(e.t('cell'))))))
    detached = doc.footnotes.create([e.p(e.r(e.t('Second'))), table])
    paragraph(detached.reference())
    table_first = doc.footnotes.create([table])
    assert [n.id for n in doc.footnotes] == [1, 2, 3] and live.text == 'Any day other than a Saturday, Sunday or public holiday.'
    assert [r.id for r in doc.main.xml.elements(w.FootnoteReference)] == [1, 2]
    notes = doc.part('FootnotesPart').xml
    assert [(n.type, n.id) for n in notes.elements(w.Footnote)] == [('separator', -1), ('continuationSeparator', 0), (None, 1), (None, 2), (None, 3)]
    first = detached.element.children[0]
    assert [c.qname[1] for c in first.children] == ['pPr', 'r', 'r', 'r'] and first.children[1].child(w.FootnoteReferenceMark) is not None
    assert first.child(w.ParagraphProperties).child(w.ParagraphStyleId).val == 'FootnoteText'
    assert [c.qname[1] for c in table_first.element.children] == ['p', 'tbl'] and 'cell' in table_first.text
    assert {s.id for s in doc.styles} >= {'FootnoteText', 'FootnoteReference'} and not doc.validate()['issues']
    assert live.delete() == 1
    assert [n.id for n in doc.footnotes] == [2, 3] and [r.id for r in doc.main.xml.elements(w.FootnoteReference)] == [2]
    reopened = Document.from_bytes(doc.bytes())
    assert 'Second' in reopened.footnotes[2].text and not reopened.validate()['issues']
