"Complementary ECMA/MS-DOCX schema checks using the same native public validation API."
from pathlib import Path
from oxml import Document, Tree, e, w

FIXTURES = Path(__file__).parent/'fixtures'
W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'

def xsd_issues(report): return [i for i in report['issues'] if i['category'] == 'xsd']

def test_live_document_xsd_constraints_and_preservation():
    # Equivalent to mdhtml2docx/test_validate.py's unknown-element and invalid-indentation cases.
    doc = Document.new()
    body = next(doc.main.xml.elements(w.Body))
    paragraph = body(e.p(e.pPr(e.ind(left='720')), e.r(e.t('Clause'))))
    original = doc.bytes()
    assert not xsd_issues(doc.validate()) and doc.bytes() == original
    ind = next(doc.main.xml.elements(w.Indentation))
    ind.set_attribute(W, 'left', 'banana')
    assert any(i['node'] == ind.node_id and 'banana' in i['actual'] for i in xsd_issues(doc.validate()))
    ind.set_attribute(W, 'left', '720')
    paragraph.append_xml(b'<w:bogusElement/>')
    assert any('bogusElement' in i['actual'] for i in xsd_issues(doc.validate()))

def test_modern_comment_xsd_checks_and_live_metadata():
    doc = Document.open(FIXTURES/'reviews/libreoffice/CommentReply.docx')
    original = doc.bytes()
    report = doc.validate()
    assert report['coverage']['xsd_roots_checked'] >= 2 and not xsd_issues(report)
    assert doc.bytes() == original
    metadata = doc.part('WordprocessingCommentsExPart').xml
    comment = metadata.root.children[0]
    comment.set_attribute(comment.qname[0], 'paraId', 'not-hex')
    assert xsd_issues(doc.validate())

def test_xsd_uses_selected_mc_branch_and_keeps_unknown_content():
    xml = (f'<w:document xmlns:w="{W}" xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
           'xmlns:x="urn:unknown" mc:Ignorable="x"><w:body><mc:AlternateContent><mc:Choice Requires="x">'
           '<w:bogus/></mc:Choice><mc:Fallback><w:p/></mc:Fallback></mc:AlternateContent><x:keep/></w:body></w:document>').encode()
    tree = Tree(xml)
    assert not xsd_issues(tree.validate()) and tree.bytes() == xml

def test_boolean_element_text_is_not_an_integer():
    # Real extended-properties payload exposed by validating the exporter reference document as a package.
    tree = Tree(b'<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><SharedDoc>false</SharedDoc></Properties>')
    assert not tree.validate()['issues']
    tree.root.children[0].value = 'banana'
    assert tree.validate()['issues']

def test_projection_does_not_invent_missing_content_or_hide_attribute_errors():
    tree = Tree(b'<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
                b'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" wrong="x"><w14:checkbox/></a:graphic>')
    report = tree.validate()
    errors = xsd_issues(report)
    assert errors and all('wrong' in i['actual'] for i in errors)
    assert any(g.startswith('xsd:projected-content-model:') for g in report['coverage']['gaps'])

def test_modern_extension_imports_retain_nested_validation():
    tree = Tree(b'<cex:commentsExtensible xmlns:cex="http://schemas.microsoft.com/office/word/2018/wordml/cex" '
                b'xmlns:w16="http://schemas.microsoft.com/office/word/2018/wordml" xmlns:cei="http://schemas.microsoft.com/office/word/2026/wordml/cei">'
                b'<cex:extLst><w16:ext uri="urn:test"><cei:commentEntityInfo cei:entityType="-1"/></w16:ext></cex:extLst></cex:commentsExtensible>')
    report = tree.validate()
    assert report['coverage']['xsd_roots_checked'] == 1
    assert any('-1' in i['actual'] for i in xsd_issues(report))
