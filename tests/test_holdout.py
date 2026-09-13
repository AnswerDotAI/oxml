"""Post-implementation H1: upstream fixture mismatch and datatype-aware imported facets."""
from pathlib import Path
import json
from zipfile import ZipFile
import pytest
from oxml import Tree, namespaces, w
from oxml.model import types

metadata = json.loads((Path(__file__).parents[1]/'schema/metadata.json').read_text())

def test_symex_holdout_typing_preservation_and_binary_length():
    with ZipFile(Path(__file__).parent/'fixtures/sdk/Of16-10-SymEx.docx') as z: source = z.read('word/document.xml')
    original = Tree(source)
    cls = namespaces['w16se'].SymEx
    assert types[cls.type_id] is cls and not list(original.elements(cls))
    assert next(original.elements(w.Run)).children[0].type_id is None
    report = original.validate()
    assert any(r['reason'] == 'ignorable-unknown' for r in report['coverage']['skipped_regions'])
    assert not report['coverage']['complete'] and original.bytes() == source
    # Metadata-shaped derivative, not a load-time repair: no SDK parent actually declares this type as a child.
    declared = source.replace(b'<w16se:sym ', b'<w16se:symEx ').replace(b'w:font="Webdings"', b'w16se:font="Webdings"')
    declared = declared.replace(b'w:char="F04E"', b'w16se:char="0000F04E"')
    assert not list(Tree(declared).elements(cls))
    assert not any(cls.type_id in t['children'] for t in metadata['types'].values())
    # Standalone construction context isolates datatype facets from the unresolved parent-model source gap.
    start = declared.index(b'<w16se:symEx ')
    fragment = declared[start:declared.index(b'/>', start)+2]
    fragment = fragment.replace(b'<w16se:symEx ', f'<w16se:symEx xmlns:w16se="{metadata["namespaces"]["w16se"]}" '.encode())
    tree = Tree(fragment)
    symbol = next(tree.elements(cls))
    assert symbol.font == 'Webdings'
    symbol.font = 'Wingdings'
    assert symbol.font == next(tree.elements(cls)).font == 'Wingdings'
    assert next(Tree(tree.bytes()).elements(cls)).font == 'Wingdings'
    provenance = metadata['types'][cls.type_id]['source']
    wrong_length = fragment.replace(b'0000F04E', b'F04E')
    reports = [Tree(xml).validate() for xml in (fragment, wrong_length)]
    # SDK HexBinaryValue.Length counts bytes, not the number of lexical hex digits.
    assert [any(i['rule_provenance'] == provenance for i in r['issues']) for r in reports] == [False, True]
    assert next(Tree(fragment).elements(cls)).char == '0000F04E'
    with pytest.raises(ValueError): next(tree.elements(cls)).char = 'F04E'
    assert any(i['rule_id'] == 'element-version' and i['rule_provenance'] == provenance
               for i in Tree(fragment).validate(target='Office2013')['issues'])
