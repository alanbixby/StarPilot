"""Compiled incumbent IDs/layout and wire regressions for the nested startup type."""
from importlib.resources import files
from pathlib import Path
import copy
import hashlib
import json
import re

import capnp
import pytest
from openpilot.cereal import custom, CEREAL_PATH
from openpilot.starpilot.car.tesla.tests.schema_fixture import layout
from openpilot.starpilot.car.tesla.tests.test_radar_schema_evolution import populated, assert_occupied_equal

FIXTURE = Path(__file__).with_name('fixtures') / 'custom_before_preap_pedal.capnp'
REVIEW = json.loads(FIXTURE.with_name('custom_reviewed_schema_additions.json').read_text())
# Separately reviewed Sonata MADS appends to owned AOL carriers; never overrides a Tesla-reviewed type.
MADS_REVIEW = json.loads(FIXTURE.with_name('custom_reviewed_mads_additions.json').read_text())
APPROVED = {**REVIEW['types'], **MADS_REVIEW['types']}


@pytest.fixture
def historical():
  parser = capnp.SchemaParser()
  yield parser.load(str(FIXTURE), imports=[CEREAL_PATH, str(files('opendbc') / 'car')])


def compare_node(old, new):
  assert old.schema.node.id == new.schema.node.id
  kind = old.schema.node.which()
  assert kind == new.schema.node.which()
  approved = APPROVED.get(hex(old.schema.node.id))
  if kind == 'struct':
    old_layout, new_layout = layout(old.schema), layout(new.schema)
    if approved is None:
      assert old_layout == new_layout
    else:
      assert approved['kind'] == 'struct'
      old_fields = old_layout.pop('fields', [])
      assert len(old_fields) == approved['old_field_count']
      assert old_layout == approved['old_storage']
      expected = {**approved['new_storage'], 'fields': old_fields + approved['added_fields']}
      assert new_layout == expected
  elif kind == 'enum':
    old_layout, new_layout = old.schema.node.enum.to_dict(), new.schema.node.enum.to_dict()
    if approved is None:
      assert old_layout == new_layout
    else:
      assert approved['kind'] == 'enum'
      old_values = old_layout.pop('enumerants')
      assert len(old_values) == approved['old_field_count']
      assert old_layout == approved['old_storage']
      assert new_layout == {**approved['new_storage'], 'enumerants': old_values + approved['added_enumerants']}
  for child in old.schema.node.nestedNodes:
    compare_node(getattr(old, child.name), getattr(new, child.name))


def incumbent_ids(node):
  yield hex(node.schema.node.id)
  for child in node.schema.node.nestedNodes:
    yield from incumbent_ids(getattr(node, child.name))


def test_all_incumbent_custom_type_ids_fields_and_layouts_are_preserved(historical):
  assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == REVIEW['incumbent_fixture_sha256']
  assert MADS_REVIEW['incumbent_fixture_sha256'] == REVIEW['incumbent_fixture_sha256']
  assert not REVIEW['types'].keys() & MADS_REVIEW['types'].keys()
  names = re.findall(r'^(?:struct|enum) (\w+)', FIXTURE.read_text(), re.MULTILINE)
  # Every approval binds an incumbent type; none can admit an unrelated future type.
  assert APPROVED.keys() <= {i for name in names for i in incumbent_ids(getattr(historical, name))}
  for name in names:
    old = getattr(historical, name)
    approved = APPROVED.get(hex(old.schema.node.id))
    current_name = approved['after_name'] if approved else name
    compare_node(old, getattr(custom, current_name))
  # Startup capsules have no parent slots; published phone GPS@3 stays occupied.
  assert set(custom.StarPilotCarControl.schema.fields) == {
    'hudControl', 'steeringLimitInfo', 'navigation', 'phoneGps'}
  assert custom.StarPilotCarControl.PreapPedalStartup.schema.node.id not in {
    getattr(historical, name).schema.node.id for name in
    re.findall(r'^struct (\w+)', FIXTURE.read_text(), re.MULTILINE)}


def test_standalone_startup_does_not_modify_parent_and_old_relay_preserves_navigation(historical):
  parent = populated(custom.StarPilotCarControl)
  original = parent.to_bytes()
  capsule = custom.StarPilotCarControl.PreapPedalStartup.new_message(
    version=1, kind=5, carParamsSha256=bytes(range(32)), safetyModel=39,
    safetyParam=7, factor=1.25, zero=-0.5, pedalBus=2, sourceMonoTime=1234)
  with custom.StarPilotCarControl.PreapPedalStartup.from_bytes(capsule.to_bytes()) as recovered:
    assert recovered.to_dict() == capsule.to_dict()
  parent.clear_write_flag()
  assert parent.to_bytes() == original
  with historical.StarPilotCarControl.from_bytes(original) as old:
    assert_occupied_equal(parent, old)
    relayed = old.as_builder().to_bytes()
  with custom.StarPilotCarControl.from_bytes(relayed) as recovered:
    assert_occupied_equal(parent, recovered)


def test_old_gps_and_pedal_status_survive_current_reader_and_old_relay(historical):
  old = historical.StarPilotCarState.new_message(pedalMaxRegen=True, pedalLongActive=True,
    sourceCarStateMonoTime=123456, gps={'sourceMonoTime': 123456, 'latitude': 41.25,
      'longitude': -87.5, 'altitude': 180., 'speed': 20., 'bearingDeg': 90., 'hasFix': True,
      'unixTimestampMillis': 1700000000000, 'vNED': [1., 2., 3.]})
  with custom.StarPilotCarState.from_bytes(old.to_bytes()) as current:
    assert {name: current.to_dict()[name] for name in old.to_dict()} == old.to_dict()
    relayed = current.as_builder().to_bytes()
  with historical.StarPilotCarState.from_bytes(relayed) as recovered:
    assert recovered.to_dict() == old.to_dict()


def test_additive_feedback_survives_incumbent_reader_relay(historical):
  original = historical.StarPilotCarState.new_message(
    pedalMaxRegen=True, pedalLongActive=True, sourceCarStateMonoTime=123456,
    gps={'latitude': 41.25, 'longitude': -87.5, 'hasFix': True})
  incumbent = original.to_dict()
  with custom.StarPilotCarState.from_bytes(original.to_bytes()) as decoded:
    current = decoded.as_builder()
  feedback = {'version': 1, 'cardSession': bytes(range(16)), 'publicationSequence': 27,
              'sourceCarStateMonoTime': 123456, 'outputKnown': True,
              'reportedTorque': 0.25, 'coBodySha256': bytes(range(32))}
  current.feedbackPublication = feedback
  expected_feedback = current.feedbackPublication.to_dict()
  with historical.StarPilotCarState.from_bytes(current.to_bytes()) as old:
    assert old.to_dict() == incumbent
    relayed = old.as_builder().to_bytes()
  with custom.StarPilotCarState.from_bytes(relayed) as recovered:
    assert {name: recovered.to_dict()[name] for name in incumbent} == incumbent
    assert recovered.feedbackPublication.to_dict() == expected_feedback


@pytest.mark.parametrize(('fields', 'compatible'), [
  ('value @0 :UInt32; label @1 :Text;', True),
  ('value @0 :UInt32; label @1 :Text; extra @2 :Bool;', False),
  ('value @0 :UInt64; label @1 :Text;', False),
  ('value @0 :UInt32 = 42; label @1 :Text;', False),
  ('value @1 :UInt32; label @0 :Text;', False),
  ('value @0 :UInt32;', False),
])
def test_compiled_compatibility_check_rejects_unreviewed_changes(tmp_path, fields, compatible):
  header = '@0x9db79da1a8afbd10; struct Sample @0x9713dcf4d2386e50 {'
  old_path, new_path = tmp_path / 'old.capnp', tmp_path / 'new.capnp'
  old_path.write_text(header + 'value @0 :UInt32; label @1 :Text; }')
  new_path.write_text(header + fields + '}')
  old_parser, new_parser = capnp.SchemaParser(), capnp.SchemaParser()
  old, new = old_parser.load(str(old_path)).Sample, new_parser.load(str(new_path)).Sample
  if compatible:
    compare_node(old, new)
  else:
    with pytest.raises(AssertionError):
      compare_node(old, new)


MADS_SCHEMA_MUTATIONS = {
  'removed_mads_append': [('    madsButtons @13 :Bool;', '')],
  'retyped_mads_append': [('madsButtons @13 :Bool;', 'madsButtons @13 :UInt8;')],
  'defaulted_mads_append': [('madsButtons @13 :Bool;', 'madsButtons @13 :Bool = true;')],
  'renamed_display_append': [('displayIntentValid @18', 'displayIntentFresh @18')],
  'swapped_display_appends': [('displayIntentValid @18', 'displayIntentValid @19'),
                              ('displayIntentEnabled @19', 'displayIntentEnabled @18')],
  'unreviewed_extra_append': [('madsButtons @13 :Bool;', 'madsButtons @13 :Bool; extra @14 :Bool;')],
  'retyped_incumbent_field': [('intentIntegrityEpoch @17 :UInt64;', 'intentIntegrityEpoch @17 :Int64;')],
  'renamed_incumbent_field': [('lateralArmed @11', 'lateralArmedLatched @11')],
  'removed_incumbent_field': [('    optionalSetRelease @12 :Bool;\n', ''), ('madsButtons @13', 'madsButtons @12')],
  'changed_incumbent_type_id': [('AolAxisState @0xfc6241ed8877b611', 'AolAxisState @0xfc6241ed8877b612')],
}


@pytest.mark.parametrize('mutation', [None, *MADS_SCHEMA_MUTATIONS])
def test_mads_owned_carrier_appends_are_exact(historical, tmp_path, mutation):
  source = (Path(CEREAL_PATH) / 'custom.capnp').read_text()
  for old, new in MADS_SCHEMA_MUTATIONS.get(mutation, []):
    assert source.count(old) == 1
    source = source.replace(old, new)
  (tmp_path / 'custom.capnp').write_text(source)
  parser = capnp.SchemaParser()
  current = parser.load(str(tmp_path / 'custom.capnp'), imports=[CEREAL_PATH, str(files('opendbc') / 'car')])
  if mutation is None:
    compare_node(historical.AolAxisState, current.AolAxisState)
  else:
    with pytest.raises(AssertionError):
      compare_node(historical.AolAxisState, current.AolAxisState)


@pytest.mark.parametrize('approval', ['absent', 'offset', 'default', 'storage'])
def test_mads_approval_is_required_and_exact(historical, monkeypatch, approval):
  type_id = hex(custom.AolAxisState.IntentWire.schema.node.id)
  entry = copy.deepcopy(APPROVED[type_id])
  if approval == 'absent':
    monkeypatch.delitem(APPROVED, type_id)
  else:
    if approval == 'offset':
      entry['added_fields'][0]['slot']['offset'] += 1
    elif approval == 'default':
      entry['added_fields'][0]['slot']['defaultValue'] = {'bool': True}
    else:
      entry['new_storage']['dataWordCount'] += 1
    monkeypatch.setitem(APPROVED, type_id, entry)
  with pytest.raises(AssertionError):
    compare_node(historical.AolAxisState, custom.AolAxisState)
