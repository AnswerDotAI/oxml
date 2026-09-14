"""M0 fixture workflows. Invalid variants are made in tests from unchanged original DOCX files."""
from pathlib import Path
import json
from zipfile import ZipFile
from xml.etree import ElementTree as ET
import pytest
from oxml import Tree, w, namespaces, _core
from oxml.model import types

FIXTURES = Path(__file__).parent/'fixtures'
metadata = json.loads((Path(__file__).parents[1]/'schema/metadata.json').read_text())
W = metadata['namespaces']['w']

def part(path, name='word/document.xml'):
    with ZipFile(FIXTURES/path) as z: return z.read(name)

def rules(report): return {i['rule_id'] for i in report['issues']}

def parsed(source): return ET.fromstring(source, parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True)))

def test_whole_vocabulary_nominal_types_and_contextual_dispatch():
    assert len(types) == sum(not t['is_abstract'] for t in metadata['types'].values())
    for type_id, cls in types.items():
        prefix = type_id.rsplit('/', 1)[-1].split(':')[0]
        assert getattr(namespaces[prefix], metadata['types'][type_id]['class_name']) is cls
    assert w.Deleted is not w.DeletedRun
    inline = Tree(part('pandoc/track_changes_scrubbed_metadata.docx'))
    paragraph = Tree(part('pandoc/paragraph_insertion_deletion.docx'))
    assert inline.count(w.DeletedRun) > 0 and inline.count(w.Deleted) == 0
    assert paragraph.count(w.Deleted) > 0
    styles = Tree(part('sdk/simpleSdt.docx', 'word/styles.xml'))
    border, margin = next(styles.elements(w.BottomBorder)), next(styles.elements(w.BottomMargin))
    assert border.val is w.BorderValues.Single and border.size == 6
    assert margin.width == '0' and border.type_id != margin.type_id

def test_native_element_count():
    tree = Tree(f'<w:document xmlns:w="{W}" xmlns:x="urn:other"><w:body><!--ignored-->'
                '<w:p><w:r><w:t>text</w:t></w:r></w:p><w:p/><x:p/></w:body></w:document>'.encode())
    assert tree.count() == 7 and tree.count(w.Document) == 1
    assert tree.count(w.Paragraph) == 2 and tree.count(w.Table) == 0
    next(tree.elements(w.Paragraph)).delete()
    assert tree.count() == 4 and tree.count(w.Paragraph) == 1

def test_typed_raw_edit_preserves_unknown_content():
    original = part('sdk/mcdoc.docx')
    tree = Tree(original)
    assert tree.bytes() == original
    text = next(tree.elements(w.Text))
    with pytest.raises(TypeError): text.raw['text'] = 'not an XML edit'
    old_id = text.node_id
    text.value = 'M0 replacement <&> 😀'
    assert text.value == 'M0 replacement <&> 😀' and text.node_id == old_id
    refreshed = next(tree.elements(w.Text))
    assert refreshed.value == 'M0 replacement <&> 😀'
    changed = tree.bytes()
    expected = parsed(original)
    expected.find(f'.//{{{W}}}t').text = refreshed.value
    assert ET.tostring(parsed(changed)) == ET.tostring(expected)

def test_lexical_union_required_and_invalid_existing_values():
    source = part('pandoc/track_changes_scrubbed_metadata.docx')
    tree = Tree(source)
    reference = next(tree.elements(w.CommentReference))
    assert reference.id == '3'
    with pytest.raises(ValueError): reference.id = '-1'
    assert tree.bytes() == source
    reference.set_attribute(W, 'id', 'bad')
    bad = next(tree.elements(w.CommentReference))
    assert bad.attribute(W, 'id') == 'bad'
    with pytest.raises(ValueError): _ = bad.id
    assert 'attribute-union' in rules(tree.validate())
    # Synthetic negative: remove required w:id from the actual comment reference only.
    missing = Tree(source.replace(b'<w:commentReference w:id="3"', b'<w:commentReference', 1))
    assert 'attribute-required' in rules(missing.validate())

def test_cross_part_lookup_and_explicit_incomplete_coverage():
    path = 'pandoc/track_changes_scrubbed_metadata.docx'
    tree = Tree(part(path))
    comments = Tree(part(path, 'word/comments.xml'))
    positive = tree.validate(dependencies={'WordprocessingCommentsPart': comments}, part_uri='/word/document.xml')
    assert positive['coverage']['semantic_checks'] > 0
    assert not [i for i in positive['issues'] if i['category'] == 'semantic']
    next(tree.elements(w.CommentReference)).id = '987654'
    negative = tree.validate(dependencies={'WordprocessingCommentsPart': comments})
    assert [i for i in negative['issues'] if i['category'] == 'semantic']
    assert not negative['coverage']['complete'] and negative['coverage']['gaps']
    absent = tree.validate()
    assert 'dependency:WordprocessingCommentsPart' in absent['coverage']['gaps']
    next(comments.elements(w.Comment)).copy_to(comments.root).id = '987654'
    updated = tree.validate(dependencies={'WordprocessingCommentsPart': comments})
    assert not [i for i in updated['issues'] if i['category'] == 'semantic']

def test_particles_version_constraints_and_preexisting_invalid_content():
    styles = part('sdk/simpleSdt.docx', 'word/styles.xml')
    # Extract an unchanged real paragraph-border subtree; add an explicit namespace context for standalone validation.
    start, end = styles.index(b'<w:pBdr>'), styles.index(b'</w:pBdr>') + len(b'</w:pBdr>')
    fragment = styles[start:end].replace(b'<w:pBdr>', f'<w:pBdr xmlns:w="{W}">'.encode(), 1)
    assert 'child-particle' not in rules(Tree(fragment).validate())
    # Duplicate the real bottom border: maxOccurs=1 comes from the imported nested sequence.
    border_start = fragment.index(b'<w:bottom ')
    border_end = fragment.index(b'/>', border_start) + 2
    invalid = fragment[:border_end] + fragment[border_start:border_end] + fragment[border_end:]
    assert 'child-particle' in rules(Tree(invalid).validate())
    original = Tree(part('sdk/simpleSdt.docx'))
    assert 'element-only-content' in rules(original.validate())
    modern = Tree(part('sdk/HelloO14.docx'))
    assert 'attribute-version' not in rules(modern.validate(target='Office2007'))
    modern.root.remove_attribute('http://schemas.openxmlformats.org/markup-compatibility/2006', 'Ignorable')
    assert 'attribute-version' in rules(modern.validate(target='Office2007'))
    assert 'attribute-version' not in rules(modern.validate())

def test_mark_up_compatibility_keeps_original_branches():
    source = part('sdk/mcdoc.docx')
    tree = Tree(source)
    report = tree.validate()
    assert report['coverage']['skipped_regions']
    assert tree.bytes() == source

@pytest.mark.parametrize('target,required', [('Office2007', True), ('Office2010', False), ('Microsoft365', False)])
def test_imported_required_validator_version_and_flag(target, required):
    tree = Tree(f'<m:brkBinSub xmlns:m="{metadata["namespaces"]["m"]}"/>'.encode())
    assert ('attribute-required' in rules(tree.validate(target=target))) == required

def test_absent_required_attribute_before_its_availability():
    tree = Tree(f'<a14:isCanvas xmlns:a14="{metadata["namespaces"]["a14"]}"/>'.encode())
    assert 'attribute-required' not in rules(tree.validate(target='Office2007'))

@pytest.mark.parametrize('value', ['0.5', '1e2'])
def test_imported_noninteger_number_validator_and_typed_setter(value):
    tree = Tree(f'<c:majorUnit xmlns:c="{metadata["namespaces"]["c"]}" val="{value}"/>'.encode())
    report = tree.validate()
    assert not report['issues'] and all(g.startswith('xsd:') for g in report['coverage']['gaps'])
    tree.root.val = value
    assert tree.root.val == value
    qualified = Tree(tree.bytes().replace(b' val=', b' c:val='))
    assert 'attribute-required' in rules(qualified.validate())
