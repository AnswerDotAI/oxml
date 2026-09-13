"""SDK particle assertions, scoped to the parent as in upstream's particle-only tests."""
import pytest
from oxml import Tree

NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
      'xmlns:ap="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
      'xmlns:v="urn:schemas-microsoft-com:vml"')

def check_content(name, children, valid, target='Office2007'):
    source = f'<{name} {NS}>{children}</{name}>'
    # sectPr also names the historical revision payload; the body selects SectionProperties.
    tree = Tree((f'<w:body {NS}>{source}</w:body>' if name == 'w:sectPr' else source).encode())
    parent = tree.root.children[0] if name == 'w:sectPr' else tree.root
    assert parent.type_id is not None
    report = tree.validate(target=target)
    errors = [i for i in report['issues'] if i['node'] == parent.node_id and i['rule_id'] == 'child-particle']
    assert [i['category'] for i in errors] == ([] if valid else ['schema']), report['issues']

# ofapiTest/SequenceParticleValidatorTest.cs: TestSimpleSequence, TestSimpleSequence2, TestSimpleSequence3.
@pytest.mark.parametrize('name,good,bad', [
    ('w:ddList', '', '<w:p/>'),
    ('w:ddList', '<w:result/><w:default/><w:listEntry/><w:listEntry/>', '<w:default/><w:result/>'),
    ('w:ddList', '<w:result/><w:default/>', '<w:result/><w:default/><w:default/>'),
    ('w:ruby', '<w:rubyPr/><w:rt/><w:rubyBase/>', '<w:rubyPr/><w:rt/>'),
    ('w:ruby', '<w:rubyPr/><w:rt/><w:rubyBase/>', '<w:rubyPr/><w:rubyBase/>'),
    ('w:divs', '<w:div/>', ''),
    ('w:divs', '<w:div/><w:div/><w:div/>', '<w:div/><w:p/><w:div/>'),
])
def test_sequence(name, good, bad):
    check_content(name, good, True)
    check_content(name, bad, False)

# ofapiTest/ChoiceParticleValidatorTest.cs: TestSimpleChoice and TestSimpleChoice3.
@pytest.mark.parametrize('name,good,bad', [
    ('w:fldChar', '', '<w:p/>'),
    ('w:fldChar', '<w:fldData/>', '<w:fldData/><w:fldData/>'),
    ('w:fldChar', '<w:ffData/>', '<w:ffData/><w:fldData/>'),
    ('w:fldChar', '<w:numberingChange/>', '<w:numberingChange/><w:p/>'),
    ('w:ffData', '', '<w:p/>'),
    ('w:ffData', '<w:name/><w:name/>', '<w:helpText/><w:p/><w:checkBox/>'),
    ('w:ffData', '<w:statusText/><w:helpText/><w:helpText/><w:enabled/><w:checkBox/><w:textInput/>', '<w:p/>'),
])
def test_choice(name, good, bad):
    check_content(name, good, True)
    check_content(name, bad, False)

# ofapiTest/GroupParticleValidatorTest.cs: TestSimpleGroup, TestSimpleGroup2.
# CompositeParticleValidatorTest.cs: ValidateBody (section properties must be last).
@pytest.mark.parametrize('name,good,bad', [
    ('w:hdr', '', '<w:r/>'),
    ('w:hdr', '<w:p/><w:tbl/><w:p/><w:sdt/><w:altChunk/><w:altChunk/><w:p/>', '<w:p/><w:r/>'),
    ('w:sectPr', '<w:headerReference/>' * 6, '<w:headerReference/>' * 7),
    ('w:sectPr', '<w:headerReference/><w:paperSrc/>', '<w:headerReference/><w:paperSrc/><w:type/>'),
    ('w:body', '<w:altChunk/><w:p/><w:altChunk/><w:sectPr/>', '<w:altChunk/><w:sectPr/><w:p/>'),
])
def test_group(name, good, bad):
    check_content(name, good, True)
    check_content(name, bad, False)

# ofapiTest/AllParticleValidatorTest.cs: TestSimpleAll; extended document properties allow any order, no duplicates.
@pytest.mark.parametrize('bad', ['<ap:Company/><ap:Company/>', '<ap:Properties/>'])
def test_all(bad):
    check_content('ap:Properties', '<ap:Company/><ap:Template/><ap:HyperlinkBase/>', True)
    check_content('ap:Properties', bad, False)

# ofapiTest/AnyParticleValidatorTest.cs: AnyParticleValidateTest; only one unqualified wildcard child is permitted.
@pytest.mark.parametrize('bad', ['<v:textbox/>', '<test xmlns="http://test"/>', '<test/><test/>'])
def test_local_wildcard(bad):
    check_content('v:textbox', '<test/>', True)
    check_content('v:textbox', bad, False)

# Packaging.Tests/ParticleTests.cs: ValidateExpectedParticles, data/Particles.json: TableGridChange and DocParts.
# These SDK snapshots specify opposite minOccurs changes in Office2010; do not derive expectations from our metadata.
@pytest.mark.parametrize('name,child,empty_in_2007', [('w:tblGridChange', 'w:tblGrid', False), ('w:docParts', 'w:docPart', True)])
def test_versioned_occurrence(name, child, empty_in_2007):
    for target in ['Office2007', 'Office2010', 'Microsoft365']:
        check_content(name, f'<{child}/>', True, target)
        check_content(name, '', empty_in_2007 == (target == 'Office2007'), target)

# ofapiTest/OpenXmlValidatorTest.cs: O14SchemaConstraintDataTest; a new child in an existing namespace.
def test_versioned_child():
    for target in ['Office2007', 'Office2010', 'Microsoft365']:
        check_content('w:tblBorders', '<w:top w:val="apples"/>', True, target)
        check_content('w:tblBorders', '<w:top w:val="apples"/><w:start w:val="archedScallops"/>', target != 'Office2007', target)

# ofapiTest/DocumentValidatorTests.cs: LeafElementValidateTest (comments valid, element children invalid).
def test_leaf_content():
    tree = Tree(f'<w:r {NS}><w:rPr><w:strike/><w:vanish><!-- ok --></w:vanish>'
                '<w:webHidden><w:invalidChild/></w:webHidden></w:rPr><w:t>Run Text.</w:t>'
                '<w:t><!-- ok -->Text 2</w:t><w:t>Text 3.<invalidElement/></w:t></w:r>'.encode())
    errors = [i for i in tree.validate()['issues'] if i['category'] == 'schema']
    nodes = {e.node_id: e.raw['qname'][1] for e in tree.elements()}
    assert len(errors) == 2 and {nodes[i['node']] for i in errors} == {'webHidden', 't'}, errors
