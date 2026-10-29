from dataclasses import replace

import pytest

from opendbc.car.hyundai.escc import EsccCanFrame, EsccOfflineRadar
from opendbc.car.hyundai.tests.test_escc_protocol import VECTORS


# Deliberately a test policy, not a recommended/qualified production timeout.
TIMEOUT_NS = 123_456_789
START_NS = 1_000_000_000


def frame(timestamp_ns=START_NS, *, valid=True, status=1):
  data = bytearray.fromhex(VECTORS[1]['data_hex'])
  data[2] = int(valid) | status << 1
  return EsccCanFrame(timestamp_ns, 0x2AB, bytes(data), 0)


def radar():
  return EsccOfflineRadar(timeout_ns=TIMEOUT_NS)


def test_explicit_policy_and_never_received():
  with pytest.raises(TypeError):
    EsccOfflineRadar()
  for timeout in (0, -1, True, 1.5):
    with pytest.raises(ValueError):
      EsccOfflineRadar(timeout_ns=timeout)
  observation = radar().update(START_NS)
  assert observation.source == 'legacyEscc2ab'
  assert not observation.transport_fresh
  assert observation.packet is observation.lead is observation.packet_age_ns is None
  assert observation.emergency_source_age_ns is None


def test_lead_scaling_and_silent_loss_boundary_reacquisition():
  receiver = radar()
  first = receiver.update(START_NS, [frame()])
  assert first.transport_fresh and first.packet_age_ns == 0
  assert first.lead.distance_m == pytest.approx(22.2)
  assert first.lead.relative_speed_mps == pytest.approx(-4.2)
  assert first.lead.lateral_position_m == pytest.approx(0.)
  assert receiver.update(START_NS + TIMEOUT_NS - 1).lead.track_id == first.lead.track_id
  assert receiver.update(START_NS + TIMEOUT_NS).transport_fresh
  lost = receiver.update(START_NS + TIMEOUT_NS + 1)
  assert not lost.transport_fresh and lost.packet is lost.lead is None
  assert lost.packet_age_ns == TIMEOUT_NS + 1
  new_time = START_NS + TIMEOUT_NS + 2
  new = receiver.update(new_time, [frame(new_time)])
  assert new.lead.track_id != first.lead.track_id
  # Previously returned observations are immutable snapshots.
  assert first.transport_fresh and first.lead.distance_m == pytest.approx(22.2)


@pytest.mark.parametrize('valid,status', [(False, 0), (False, 1), (False, 2), (False, 3), (True, 0)])
def test_valid_packet_without_lead_clears_prior_lead_without_losing_link(valid, status):
  receiver = radar()
  first = receiver.update(START_NS, [frame()])
  now = START_NS + 1
  absent = receiver.update(now, [frame(now, valid=valid, status=status)])
  assert absent.transport_fresh and absent.packet_age_ns == 0 and absent.packet is not None
  assert absent.lead is None
  restored = receiver.update(now + 1, [frame(now + 1)])
  assert restored.lead.track_id != first.lead.track_id


@pytest.mark.parametrize('status', [1, 2, 3])
def test_nonzero_status_is_not_assigned_an_undocumented_enum_meaning(status):
  observation = radar().update(START_NS, [frame(status=status)])
  assert observation.packet.ACC_ObjStatus == status
  assert observation.lead is not None


@pytest.mark.parametrize('changes', [
  {'address': 0x2AC}, {'address': 0x800002AB}, {'bus': 1}, {'bus': 2},
  {'is_extended': True}, {'is_fd': True}, {'data': bytes(7)}, {'data': bytes(9)},
  {'timestamp_ns': START_NS - 1}, {'timestamp_ns': START_NS}, {'timestamp_ns': START_NS + TIMEOUT_NS + 2},
])
def test_bad_or_replayed_frames_cannot_refresh_packet_liveness(changes):
  receiver = radar()
  receiver.update(START_NS, [frame()])
  now = START_NS + TIMEOUT_NS + 1
  bad = replace(frame(now), **changes)
  observation = receiver.update(now, [bad])
  assert not observation.transport_fresh and observation.lead is None
  assert observation.last_packet_ns == START_NS
  # A rejected future timestamp cannot poison later good input.
  assert receiver.update(now + 1, [frame(now + 1)]).transport_fresh


def test_timeout_gap_within_one_batch_cannot_keep_track_identity():
  receiver = radar()
  first = receiver.update(START_NS, [frame()])
  now = START_NS + TIMEOUT_NS + 2
  later = receiver.update(now, [frame(START_NS + 1), frame(now)])
  assert later.lead.track_id != first.lead.track_id


def test_clock_rewind_discards_cached_packet_and_current_batch():
  receiver = radar()
  first = receiver.update(START_NS, [frame()])
  reset = receiver.update(1, [frame(1)])
  assert not reset.transport_fresh and reset.lead is reset.last_packet_ns is None
  new = receiver.update(2, [frame(2)])
  assert new.lead.track_id != first.lead.track_id


def test_batch_invalid_lead_then_reacquired_and_reordered_frame():
  receiver = radar()
  first = receiver.update(START_NS, [frame()])
  last = receiver.update(START_NS + 3, [frame(START_NS + 1, valid=False), frame(START_NS + 3), frame(START_NS + 2, valid=False)])
  assert last.last_packet_ns == START_NS + 3
  assert last.lead.track_id != first.lead.track_id


def test_legacy_identical_packets_cannot_establish_emergency_source_freshness():
  receiver = radar()
  for index in range(3):
    now = START_NS + index * TIMEOUT_NS
    observation = receiver.update(now, [frame(now)])
    assert observation.transport_fresh
    # Both nonzero emergency demands decode but never gain source-age proof.
    assert observation.packet.CR_VSM_DecCmd_SCC12 == pytest.approx(.37)
    assert observation.packet.CR_VSM_DecCmd_FCA11 == pytest.approx(.83)
    assert observation.emergency_source_age_ns is None


def test_new_adapter_cannot_reuse_other_adapter_cache():
  first = radar()
  first.update(START_NS, [frame()])
  assert not radar().update(START_NS + 1).transport_fresh


def test_zero_timestamp_and_continuous_lead_identity():
  receiver = radar()
  first = receiver.update(0, [frame(0)])
  assert first.transport_fresh and first.last_packet_ns == 0
  next_observation = receiver.update(1, [frame(1)])
  assert next_observation.lead.track_id == first.lead.track_id


def test_old_startup_cache_is_expired_on_same_call():
  observation = radar().update(START_NS + TIMEOUT_NS + 1, [frame()])
  assert not observation.transport_fresh and observation.lead is observation.packet is None


@pytest.mark.parametrize('now_ns', [-1, True, 1.0, float('nan')])
def test_invalid_clock_input_does_not_change_previous_state(now_ns):
  receiver = radar()
  first = receiver.update(START_NS, [frame()])
  with pytest.raises(ValueError):
    receiver.update(now_ns)
  assert receiver.update(START_NS) == first


@pytest.mark.parametrize('raw_mando_present', [False, True])
def test_runtime_factory_and_radar_selection_are_unchanged_by_escc_presence(raw_mando_present):
  from opendbc.car import gen_empty_fingerprint
  from opendbc.car.hyundai.interface import CarInterface
  from opendbc.car.hyundai.radar_interface import RadarInterface
  from opendbc.car.hyundai.values import CAR

  fingerprint = gen_empty_fingerprint()
  if raw_mando_present:
    fingerprint[1].update(dict.fromkeys(range(0x500, 0x520), 8))
  before = CarInterface.get_params(CAR.HYUNDAI_SONATA_HYBRID, fingerprint, [], False, False, False)
  fingerprint[0][0x2AB] = 8
  after = CarInterface.get_params(CAR.HYUNDAI_SONATA_HYBRID, fingerprint, [], False, False, False)
  assert before.to_dict() == after.to_dict()
  assert after.radarUnavailable == (not raw_mando_present)
  runtime_radar = RadarInterface(after)
  assert runtime_radar.rcp.dbc_name == 'hyundai_kia_mando_front_radar_generated'
  assert runtime_radar.rcp.bus == 1
  assert 0x2AB not in runtime_radar.rcp.addresses
