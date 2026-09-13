"""Selected SDK MC assertions, adapted to validation without destructive XML processing."""
import pytest
from oxml import Tree, w

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
W14 = 'http://schemas.microsoft.com/office/word/2010/wordml'
MC = 'http://schemas.openxmlformats.org/markup-compatibility/2006'

def paragraph(content, attrs=''):
    return Tree((f'<w:p xmlns:w="{W}" xmlns:w14="{W14}" xmlns:mc="{MC}" xmlns:x="urn:unknown" '
                 f'xmlns:v="urn:schemas-microsoft-com:vml" {attrs}>{content}</w:p>').encode())


# McValidationTest.cs: AcbContentValidationTest2007 / AcbContentValidationTest2010.
@pytest.mark.parametrize('target, branch', [('Office2007', 'pict'), ('Office2010', 'drawing')])
def test_sdk_content_error_is_in_selected_branch(target, branch):
    tree = paragraph('<w:r><mc:AlternateContent>'
                     '<mc:Choice Requires="w14"><w:drawing><v:rect/></w:drawing></mc:Choice>'
                     '<mc:Fallback><w:pict><v:textbox/></w:pict></mc:Fallback></mc:AlternateContent></w:r>',
                     'mc:Ignorable="w14" w:rsidR="00A35C47" w14:paraId="017B6C57" w14:editId="32F17AD3"')
    original = tree.bytes()
    expected = next(e.node_id for e in tree.elements() if e.raw['qname'] == (W, branch))
    errors = tree.validate(target)['issues']
    assert len(errors) == 1 and errors[0]['node'] == expected
    assert tree.bytes() == original


# MarkupCompatibilityTest.cs: MultipleChoice_OneFallback_O12Mode; AcbContentValidationTest* above.
# Invalid attributes identify the selected branch without relying on unimplemented drawing content models.
@pytest.mark.parametrize('target, requires, second, selected', [
    ('Office2007', 'w14', 'w', 1), ('Office2010', 'w14', 'w', 0), ('Microsoft365', 'w14 x', 'w', 1),
    ('Microsoft365', 'x', 'w', 1), ('Microsoft365', 'alias', 'w', 0), ('Office2007', 'w14', 'w14', 2)])
def test_first_choice_requires_every_namespace(target, requires, second, selected):
    tree = paragraph(f'<mc:AlternateContent><mc:Choice xmlns:alias="{W14}" Requires="{requires}">'
                     f'<w:r w:rsidR="first"/></mc:Choice><mc:Choice Requires="{second}"><w:r w:rsidR="second"/></mc:Choice>'
                     '<mc:Fallback><w:r w:rsidR="fallback"/></mc:Fallback></mc:AlternateContent>')
    runs = list(tree.elements(w.Run))
    errors = tree.validate(target)['issues']
    assert errors and {e['node'] for e in errors} == {runs[selected].node_id}


# McValidationTest.cs: AcbSyntaxValidationTest (well-formed XML, invalid MC grammar).
@pytest.mark.parametrize('content, location', [
    ('', 'AlternateContent'),
    ('<mc:Choice Requires="x"/><mc:Fallback/><mc:Fallback/>', 'AlternateContent'),
    ('<mc:Fallback/><mc:Choice Requires="x"/>', 'AlternateContent'),
    ('<mc:Choice Requires="x"/><mc:AlternateContent/>', 'AlternateContent'),
    ('<mc:Choice Requires="undefined x"/>', 'Choice'),
    ('<mc:Choice/>', 'Choice'),
    ('<mc:Choice Requires="x" xml:lang="en-us"/>', 'Choice')])
def test_invalid_alternate_content(content, location):
    assert not paragraph('<mc:AlternateContent><mc:Choice Requires="x"/><mc:Fallback/></mc:AlternateContent>').validate()['issues']
    tree = paragraph(f'<mc:AlternateContent>{content}</mc:AlternateContent>')
    expected = next(e.node_id for e in tree.elements() if e.raw['qname'] == (MC, location))
    assert any(e['node'] == expected for e in tree.validate()['issues'])


# McValidationTest.cs: CompatibilityRuleAttributesValidationTest.
@pytest.mark.parametrize('directive, invalid', [
    ('Ignorable', 'x undefined'), ('PreserveElements', 'undefined:*'), ('PreserveAttributes', 'undefined:id'),
    ('ProcessContent', 'undefined:span'), ('PreserveElements', 'w14:*'), ('ProcessContent', 'w14:span')])
def test_invalid_compatibility_directive(directive, invalid):
    tree = paragraph('<w:r/>', 'mc:Ignorable="x" mc:PreserveElements=" x:* " mc:PreserveAttributes="x:id" mc:ProcessContent="x:span"')
    assert not tree.validate()['issues']
    tree.root.set_attribute(MC, directive, invalid)
    assert any(e['node'] == tree.root.node_id for e in tree.validate()['issues'])


# McValidationTest.cs: CompatibilityRuleAttributesValidationTest, xml:space + ProcessContent error.
def test_process_content_disallows_xml_space():
    tree = paragraph('<w:r/>', 'mc:Ignorable="x" mc:ProcessContent="x:span"')
    assert not tree.validate()['issues']
    tree.root.set_attribute('http://www.w3.org/XML/1998/namespace', 'space', 'preserve')
    assert any(e['node'] == tree.root.node_id for e in tree.validate()['issues'])


# McValidationTest.cs: CompatibilityRuleAttributesValidationTest, non-ignorable unknown attribute.
def test_unknown_attribute_requires_ignorable():
    tree = paragraph('<w:r/>', 'mc:Ignorable="x" x:id="1"')
    assert not tree.validate()['issues']
    tree.root.remove_attribute(MC, 'Ignorable')
    assert any(e['node'] == tree.root.node_id for e in tree.validate()['issues'])


# McValidationTest.cs: AcbValidationTest; add a prefix-rebinding case to exercise inherited URI scope.
@pytest.mark.parametrize('binding', ['', 'xmlns:x="urn:different"'])
def test_ignorable_is_inherited_by_namespace_not_prefix(binding):
    tree = paragraph(f'<x:span {binding}><w:r w:rsidR="bad"/></x:span>', 'mc:Ignorable="x"')
    errors = tree.validate()['issues']
    if binding: assert any(e['node'] == tree.root.node_id for e in errors)
    else:
        assert not errors
        tree.root.remove_attribute(MC, 'Ignorable')
        assert any(e['node'] == tree.root.node_id for e in tree.validate()['issues'])


# MarkupCompatibilityTest.cs: ProcessContent_Ignored_UnknownElement_O12Mode and its wildcard variant.
# Effective content must validate normally while the wrapper remains in the stored tree.
@pytest.mark.parametrize('name', ['x:span', 'x:*'])
def test_process_content_validates_children_without_removing_wrapper(name):
    tree = paragraph('<w:r><w:rPr><x:span><w:b w:val="true"/></x:span></w:rPr></w:r>',
                     f'mc:Ignorable="x" mc:ProcessContent="{name}"')
    assert not tree.validate()['issues']
    bold = next(tree.elements(w.Bold))
    bold.set_attribute(W, 'val', 'invalid')
    original = tree.bytes()
    assert any(e['node'] == bold.node_id for e in tree.validate()['issues'])
    assert tree.bytes() == original


# CompatibilityRuleAttributesValidator.cs checks bound prefixes, not namespace support (explicit SDK TODO).
# SDK's stronger unsupported-namespace rejection belongs to MC-processing load, not standalone validation.
@pytest.mark.parametrize('target, prefix', [('Microsoft365', 'x'), ('Office2007', 'w14')])
def test_must_understand_prefix_must_be_bound(target, prefix):
    tree = paragraph('<w:r/>', f'mc:Ignorable="{prefix}" mc:MustUnderstand="w {prefix}"')
    assert not tree.validate(target)['issues']
    tree.root.set_attribute(MC, 'MustUnderstand', 'undefined')
    assert any(e['node'] == tree.root.node_id for e in tree.validate(target)['issues'])
