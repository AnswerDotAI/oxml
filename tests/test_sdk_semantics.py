"""SDK package assertions and explicitly identified derived semantic cases; no Office runtime."""
import pytest
from oxml import Document, E, e, w

R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
CT = 'application/vnd.openxmlformats-officedocument.wordprocessingml.'

def related(doc, kind, expression, suffix=''):
    part = doc.package.add_part(f'/word/{kind}{suffix}.xml', CT+kind+'+xml', expression.bytes())
    doc.package.add_relationship(doc.main.uri, R+kind, part.uri)
    return part

# OpenXmlValidatorTest.cs / PackageStructureValidatingTest: separate its two DOCX errors.
@pytest.mark.parametrize('case', [
    'one-comments-part', 'duplicate-comments-parts', 'main-to-main', 'wrong-content-type', 'unknown-extension',
])
def test_package_part_cardinality_and_allowed_relationships(case):
    doc = Document.new()
    related(doc, 'comments', e.comments())
    if case == 'duplicate-comments-parts': related(doc, 'comments', e.comments(), '2')
    if case == 'main-to-main': doc.package.add_relationship(doc.main.uri, R+'officeDocument', doc.main.uri)
    if case == 'wrong-content-type': doc.package.set_content_type('/word/comments.xml', 'application/xml')
    if case == 'unknown-extension':
        doc.package.add_part('/unknown.bin', 'application/octet-stream', b'opaque')
        doc.package.add_relationship(doc.main.uri, 'urn:opaque', '/unknown.bin')
    issues = Document.from_bytes(doc.bytes()).validate()['issues']
    if case in ('one-comments-part', 'unknown-extension'): assert not issues
    else:
        assert len(issues) == 1
        assert issues[0]['category'] == 'package' and issues[0]['part_uri'] == doc.main.uri

# Derived from SDK HeaderReference.ConfigureMetadata and Relationship{Type,Exist}Constraint.cs.
# A header reference must use a header relationship from THIS part, not merely an existing ID elsewhere.
@pytest.mark.parametrize('kind,scope', [
    ('header', 'main'), ('footer', 'main'), ('header', 'package'), ('header', 'missing'),
])
def test_header_relationship_type_and_scope(kind, scope):
    doc = Document.new()
    part = doc.package.add_part('/word/target.xml', CT+kind+'+xml', e('hdr' if kind == 'header' else 'ftr').bytes())
    if scope != 'missing':
        doc.package.add_relationship(doc.main.uri if scope == 'main' else '/', R+kind, part.uri, relationship_id='rIdHeader')
    next(doc.main.xml.elements(w.Body))(e.sectPr(e.headerReference(type='default', r__id='rIdHeader')))
    reference = next(doc.main.xml.elements(w.HeaderReference))
    issues = [i for i in doc.validate()['issues'] if i['category'] == 'semantic']
    assert bool(issues) == (kind != 'header' or scope != 'main')
    assert all(i['node'] == reference.node_id and i['part_uri'] == doc.main.uri for i in issues)

# Derived from SDK Style.ConfigureMetadata / UniqueAttributeValueConstraint.cs: case-sensitive, part-scoped,
# and empty IDs are ignored. Exercise standalone secondary-part validation too.
@pytest.mark.parametrize('ids,invalid', [
    (('Example', 'example'), False), (('Example', 'Example'), True), (('', ''), False),
])
def test_style_id_uniqueness(ids, invalid):
    doc = Document.new()
    part = related(doc, 'styles', e.styles(e.style(styleId=i) for i in ids))
    tree = part.xml
    issues = [i for i in tree.validate(part_uri=part.uri)['issues'] if i['category'] == 'semantic']
    assert len(issues) == int(invalid)
    assert all(i['node'] in {e.node_id for e in tree.elements(w.Style)} and i['part_uri'] == part.uri for i in issues)

# UniqueAttributeValueConstraint enumerates only the selected MC branch, not both stored alternatives.
@pytest.mark.parametrize('target,invalid', [('Office2007', False), ('Office2010', True)])
def test_duplicate_ids_only_in_selected_compatibility_content(target, invalid):
    doc = Document.new()
    mc = E('mc', ns={'w14': 'http://schemas.microsoft.com/office/word/2010/wordml'})
    expression = e.styles(e.style(styleId='Example'),
        mc.AlternateContent(
            mc.Choice(e.style(styleId='Example'), Requires='w14'),
            mc.Fallback(e.style(styleId='Other'))))
    part = related(doc, 'styles', expression)
    issues = [i for i in part.xml.validate(target, part_uri=part.uri)['issues'] if i['category'] == 'semantic']
    assert len(issues) == int(invalid)

# PackageValidator.cs treats parts introduced after the target as ExtendedPart, including cardinality.
@pytest.mark.parametrize('target,invalid', [('Office2010', False), ('Office2013', True)])
def test_part_cardinality_respects_availability(target, invalid):
    doc = Document.new()
    for name in ('one', 'two'):
        part = doc.package.add_part(f'/word/{name}.xml', CT+'commentsExtended+xml', E('w15').commentsEx().bytes())
        doc.package.add_relationship(doc.main.uri, 'http://schemas.microsoft.com/office/2011/relationships/commentsExtended', part.uri)
    issues = doc.validate(target)['issues']
    assert len(issues) == int(invalid)
    assert all(i['rule_id'] == 'part-cardinality' for i in issues)

# Derived from SDK FootnoteReference.ConfigureMetadata / ReferenceExistConstraint.cs. Unlike a caller omitting
# Tree dependencies, a complete Document with no footnotes part definitively has an unresolved reference.
@pytest.mark.parametrize('target_id', ['1', '2', None])
def test_footnote_reference_uses_related_part(target_id):
    doc = Document.new()
    next(doc.main.xml.elements(w.Body))(e.p(e.r(e.footnoteReference(id='1')) for _ in range(64)))
    if target_id is not None: related(doc, 'footnotes', e.footnotes(e.footnote(e.p(), id=target_id)))
    references = {e.node_id for e in doc.main.xml.elements(w.FootnoteReference)}
    issues = [i for i in doc.validate()['issues'] if i['category'] == 'semantic']
    assert {i['node'] for i in issues} == (references if target_id != '1' else set())
    assert all(i['part_uri'] == doc.main.uri for i in issues)
