"""Adapted from SDK test/DocumentFormat.OpenXml.Tests/ofapiTest/OpenXmlValidatorTest.cs.

Parameter IDs name the upstream tests; retain their outcomes, not .NET error wording.
SDK MIT notice: python/oxml/SDK-LICENSE. Every group pairs valid and invalid inputs.
"""
import pytest
from oxml import E, Tree, w


@pytest.mark.parametrize('name,attribute,good,bad', [
    pytest.param('c:overlay', 'val', ['true', 'false', '0', '1', ' true\t', '\n0\r'], ['', 'FALSE', 'off', '00', '\u00a0true'],
                 id='BooleanAttributeValidationTest'),
    pytest.param('c:bubbleScale', 'val', ['0', '100', '300'], ['abc', '301', '600'],
                 id='UInt32AttributeValidationTest'),
    pytest.param('w:family', 'w:val', ['auto', 'decorative', 'swiss'], ['', 'Noo', 'Auto'],
                 id='EnumAttributeValidationTest'),
    pytest.param('b:Sources', 'StyleName', ['', 'Style1', '1'*255], ['1'*256],
                 id='StringAttributeValidationTest-MaxLength'),
    pytest.param('w:color', 'w:val', ['auto', '123456', 'FF12AB'], ['', 'auto1', '1234567', '1234'],
                 id='UnionAttributeValidationTest'),
    pytest.param('w:divId', 'w:val', ['1', '-1', '2147483647', '-2147483648'], ['', '0', '-0', 'ABC'],
                 id='UnionAttributeValidationTest2'),
    pytest.param('w:cnfStyle', 'w:val', ['010101010101'], ['010101010102'],
                 id='StringAttributeValidationTest-Pattern'),
    pytest.param('c:majorUnit', 'val', ['10000.001', '10.23e4', 'INF'], ['abc', '0', '-4.9406564584124654E-324', '-INF', 'NaN'],
                 id='DoubleAttributeValidationTest-MinExclusive'),
    pytest.param('c:logBase', 'val', ['2.0', '200', '1000.0'], ['1.9', '1000.1'],
                 id='DoubleAttributeValidationTest-InclusiveBounds'),
    pytest.param('v:arc', 'startangle', ['+100000.002', '-100000.002', '0', '79228162514264337593543950335'],
                 ['abc', '79228162514264337593543950336'],
                 id='DecimalAttributeValidationTest'),
    pytest.param('w:date', 'w:fullDate', ['0001-01-01T00:00:00', '9999-12-31T23:59:59.9999999', '2024-01-02T03:04:05Z'],
                 ['abc', '123', '2024-02-30T00:00:00Z'], id='DateTimeAttributeValidationTest'),
    pytest.param('w:documentProtection', 'w:hash', ['', 'fUmpYmCMpTxTA4pfvlhKSAgB848=', 'R3k/CLjN768ujxMXkKZOuw=='],
                 ['0', 'R3k/CLjN768ujxMXkKZOuw==$'], id='Base64BinaryAttributeValidationTest'),
    pytest.param('dgm:presOf', 'st', ['1 -2', '+123 456', '123 -4  56'], ['', 'a 1', '1 a', '1 23 4a'],
                 id='ListAttributeValidationTest'),
])
def test_attribute_values(name, attribute, good, bad):
    for valid, values in [(True, good), (False, bad)]:
        for value in values:
            tree = Tree(E(name, attrs={attribute: value}).bytes())
            assert tree.root.type_id is not None
            errors = [i for i in tree.validate(target='Office2007')['issues'] if i['category'] == 'schema']
            if valid: assert not errors, (value, errors)
            else:
                assert errors, value
                assert any(i['node'] == tree.root.node_id and i['expected'].get('QName') ==
                           (attribute if ':' in attribute else ':'+attribute) for i in errors), errors


def test_boolean_getters_share_validation_but_onoff_does_not_trim():
    # SDK BooleanValue.Parse uses XmlConvert; OnOffValue.Parse accepts exact tokens only.
    for value, expected in [(' true\t', True), ('\n0\r', False)]:
        tree = Tree(E('c:overlay', attrs={'val': value}).bytes())
        assert tree.root.val is expected and not tree.validate()['issues']
        tree.root.val = not expected
        assert tree.root.val is not expected
    assert Tree(E('w:b', attrs={'w:val': 'on'}).bytes()).root.val is True
    tree = Tree(E('w:b', attrs={'w:val': ' true '}).bytes())
    with pytest.raises(ValueError): _ = tree.root.val
    assert tree.validate()['issues']


@pytest.mark.parametrize('target,good,bad', [
    ('Office2007', ['10'], ['foo', '10%']),
    ('Office2010', ['10', '10%'], ['foo']),
    ('Microsoft365', ['10', '10%'], ['foo']),
])
def test_changed_attribute_type(target, good, bad):
    # ChangedAttributeValueTypeValidationO14SupportTest: integer -> integer/percentage union.
    for valid, values in [(True, good), (False, bad)]:
        for value in values:
            tree = Tree(E('w:zoom', attrs={'w:percent': value}).bytes())
            errors = [i for i in tree.validate(target=target)['issues'] if i['category'] == 'schema']
            assert bool(errors) != valid, (target, value, errors)
            if errors: assert all(i['node'] == tree.root.node_id and i['expected']['QName'] == 'w:percent' for i in errors)


def test_custom_xml_ncname_attribute():
    # NcnameAttributeValidationTest: CustomXmlRun needs paragraph context when parsed.
    for value, valid in [('a', True), ('_b-a', True), ('A'*255, True), ('a:b', False), ('A'*256, False)]:
        tree = Tree(E('w:p', E('w:customXml', attrs={'w:element': value})).bytes())
        node = next(tree.elements(w.CustomXmlRun))
        errors = [i for i in tree.validate(target='Office2007')['issues'] if i['category'] == 'schema']
        assert bool(errors) != valid, (value, errors)
        if errors: assert all(i['node'] == node.node_id and i['expected']['QName'] == 'w:element' for i in errors)


def test_element_text_pattern():
    # StringPatternAttributeValidationTest actually validates VTCurrency's element text.
    for value, valid in [('.1234', True), (' .1234 ', True), ('456.1234', True), ('', False), ('12.345', False), ('12.34567', False)]:
        tree = Tree(E('vt:cy', value).bytes())
        errors = [i for i in tree.validate(target='Office2007')['issues'] if i['category'] == 'schema']
        assert bool(errors) != valid, (value, errors)
        if errors: assert all(i['node'] == tree.root.node_id for i in errors)
