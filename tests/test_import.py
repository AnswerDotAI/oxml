"""SDK import, inheritance and unsupported-input checks, without .NET."""

import json
import runpy
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
API = runpy.run_path(str(ROOT / 'scripts/import_sdk.py'))


@pytest.fixture(scope='module')
def imported():
    if not (ROOT / 'links/Open-XML-SDK/.git').exists(): pytest.skip('SDK checkout required for importer integration tests')
    source, files = API['snapshot'](ROOT / 'links/Open-XML-SDK', API['HEAD'])
    return files, API['import_snapshot'](source, files)


def test_full_import_matches_generated_descriptor(imported):
    _, descriptor = imported
    assert descriptor == json.loads((ROOT / 'schema/metadata.json').read_text())


def test_inheritance_context_and_modern_shared_types(imported):
    _, descriptor = imported
    types = descriptor['types']
    deleted = types['w:CT_TrackChange/w:del']
    assert deleted['base'] == 'w:CT_TrackChange/'
    assert 'w16du:dateUtc' in [a['QName'] for a in deleted['attributes']]
    assert types['w:CT_RunTrackChange/w:del']['class_name'] != deleted['class_name']
    assert len({t['class_name'] for t in types.values() if t['qname'] == 'w:bottom'}) > 1
    assert types['cx:CT_Offset/cx:offset']['version'] == 'Office2016'
    assert types['m:CT_OMath/m:oMath']['children']
    assert all('PropertyName' in a for t in types.values() for a in t['attributes'])
    assert descriptor['enums']['w:ST_Border']['full_name'] == 'DocumentFormat.OpenXml.Wordprocessing.BorderValues'


@pytest.mark.parametrize('kind,obj,construct', [
    ('particle', {'Kind': 'Interleave'}, 'particle:unknown_value'),
    ('particle', {'Kind': 'Any', 'ProcessContents': 'lax'}, 'particle:unknown_field'),
    ('validator', {'Name': 'FutureValidator'}, 'validator:unknown_value'),
    ('argument', {'Name': 'WhiteSpace', 'Type': 'String', 'Value': 'collapse'}, 'argument:unknown_value'),
    ('type', {'Version': 'Office2027'}, 'type:unknown_value'),
])
def test_unknown_constructs_are_explicit(kind, obj, construct):
    with pytest.raises(ValueError, match=f'new-source.json#/new/.*{construct}'):
        API['Inputs']().check(obj, kind, 'new-source.json#/new')


def test_primitive_table_changes_fail_closed(imported):
    files, _ = imported
    content = files[API['SIMPLE_SOURCE']].replace(b'{ "w:CT_Text", StringValue },', b'{ "w:CT_Text", ComputeType() },')
    with pytest.raises(ValueError, match='unknown_primitive_mapping_syntax'):
        API['simple_types'](content, API['Inputs']())
