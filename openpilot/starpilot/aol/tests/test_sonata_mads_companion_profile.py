"""Immutable MADS selection survives preceding sources and restriction folds."""
from dataclasses import replace
import os
from unittest.mock import patch

import pytest

from opendbc.car.hyundai.values import CAR
from openpilot.common.params import Params
from openpilot.common.prefix import OpenpilotPrefix
from openpilot.starpilot.aol.intent_companion import IntentCompanion
from openpilot.starpilot.aol.runtime import current_intent
from openpilot.starpilot.aol.tests import test_ioniq_availability as caller_fixture
from openpilot.starpilot.aol.tests.test_classic_long_profiles import params as classic_params
from openpilot.starpilot.aol.tests.test_intent_projection import intent_receive
from openpilot.starpilot.aol.tests.test_sonata_mads_sessions import BASE, wire
from openpilot.starpilot.aol.wire import decode_intent, encode_intent
from openpilot.starpilot.car.hyundai.sonata_mads import MadsProfileAgreement


class SM(dict):
  seen = valid = alive = {'aolIntentWire': True}

  def __init__(self, event):
    super().__init__(aolIntentWire=bytes(event.aolIntentWire))
    self.logMonoTime = {'aolIntentWire': int(event.logMonoTime)}


@pytest.mark.parametrize('selected', [False, True])
@pytest.mark.parametrize('order', ['source_sm', 'latest_sm', 'restored_latest_pulse', 'verified_aged_sm'])
def test_real_selfdrived_preceding_source_cannot_hide_fresh_profile_conflict(selected, order):
  case = caller_fixture.IoniqAvailabilityTests(methodName='runTest')
  with OpenpilotPrefix(), patch.dict(os.environ, {'SIMULATION': '0', 'AOL_REPLAY_RUNTIME': '1'}):
    saved = Params()
    saved.put_bool('SonataMadsButtons', selected, block=True)
    try:
      sd, _, drive = case._authority_callers(False,
        cp=classic_params(CAR.HYUNDAI_SONATA_HYBRID, lda=True), genuine_events=True)
      original = wire(0, selected=selected)
      with intent_receive(sd, side_effect=[original.as_reader(), None]):
        axis, _, command = drive(0, intent_message=original)
      assert axis.lateralActive and command.latActive
      epoch = sd.aol_intent_companion.integrity_epoch
      if order == 'verified_aged_sm':
        source = wire(2, selected=selected)
        changed = wire(4, selected=not selected)
        received = [source.as_reader(), changed.as_reader(), None]
        projected, source_stamp, offset = original, BASE + 20_000_000, 30_000_000
      else:
        changed = wire(1, selected=not selected)
        received = [changed.as_reader()]
        projected = original if order == 'source_sm' else changed
        if order == 'restored_latest_pulse':
          projected = wire(2, selected=selected)
          received.append(projected.as_reader())
        received.append(None)
        source_stamp, offset = BASE, 10_000_000 if order == 'restored_latest_pulse' else 0
      with intent_receive(sd, side_effect=received):
        axis, state, command = drive(1, state_stamp=source_stamp, intent_message=projected, offset_ns=offset)
      assert sd.aol_intent_companion.integrity_epoch == epoch, 'The contradiction must survive ordinary valid ordering'
      assert not sd.aol_intent_companion.mads_profile_consistent
      assert sd.sonata_mads_agreement.rejected and sd.aol_last_intent is None
      assert not axis.lateralActive and not axis.longitudinalActive and not command.latActive and not command.longActive
      assert not state.enabled
      # Advancing to a fresh matching source after the conflict still cannot rearm.
      for tick in range(6, 12):
        restored = wire(tick, selected=selected)
        with intent_receive(sd, side_effect=[restored.as_reader(), None]):
          axis, _, command = drive(tick, intent_message=restored)
        assert sd.sonata_mads_agreement.rejected and sd.aol_last_intent is None
        assert not axis.lateralActive and not command.latActive and not command.longActive
    finally:
      case.doCleanups()


@pytest.mark.parametrize('selected', [False, True])
def test_public_current_intent_folds_profile_conflict_without_changing_original_lease(selected):
  older = decode_intent(wire(0, selected=selected).aolIntentWire)
  newer = wire(1, selected=not selected)
  result = current_intent(SM(newer), car_state_ns=BASE, now_ns=BASE + 10_000_000, previous=older)
  assert result is not None and not result.madsButtonsConsistent
  assert result.carStateLogMonoTime == older.carStateLogMonoTime
  assert result.observedMonoTime == older.observedMonoTime and result.validUntilMonoTime == older.validUntilMonoTime
  owner = MadsProfileAgreement(selected)
  assert owner.accept(result) is None and owner.rejected
  assert owner.accept(decode_intent(wire(2, selected=selected).aolIntentWire)) is None
  with pytest.raises(ValueError, match='producer wire'):
    encode_intent(result)


@pytest.mark.parametrize('selected', [False, True])
def test_verified_sm_projection_keeps_older_immutable_profile_veto(selected):
  companion = IntentCompanion(None)
  old = wire(0, selected=not selected)
  companion._observe(old.logMonoTime, True, bytes(old.aolIntentWire))
  companion.last_drain_ok = True
  assert current_intent(SM(old), car_state_ns=BASE, now_ns=BASE, companion=companion, drain=False) is not None
  for tick in (2, 4):
    fresh = wire(tick, selected=selected)
    companion._observe(fresh.logMonoTime, True, bytes(fresh.aolIntentWire))
  result = current_intent(SM(old), car_state_ns=BASE + 20_000_000, now_ns=BASE + 40_000_000,
                          companion=companion, drain=False)
  assert result is not None and not result.madsButtonsConsistent
  assert result.carStateLogMonoTime == BASE + 20_000_000
  assert companion.verified_sm_projection[0] == BASE
  owner = MadsProfileAgreement(selected)
  assert owner.accept(result) is None and owner.rejected


@pytest.mark.parametrize('selected', [False, True])
def test_real_reconciled_source_restriction_latches_profile_rejection(selected):
  case = caller_fixture.IoniqAvailabilityTests(methodName='runTest')
  with OpenpilotPrefix(), patch.dict(os.environ, {'SIMULATION': '0', 'AOL_REPLAY_RUNTIME': '1'}):
    saved = Params()
    saved.put_bool('SonataMadsButtons', selected, block=True)
    try:
      sd, _, drive = case._authority_callers(False,
        cp=classic_params(CAR.HYUNDAI_SONATA_HYBRID, lda=True), genuine_events=True)
      old = wire(0, selected=selected)
      with intent_receive(sd, side_effect=[old.as_reader(), None]):
        axis, _, command = drive(0, intent_message=old)
      assert axis.lateralActive and command.latActive
      proof = sd.aol_published_input_proof
      assert proof is not None
      sd.aol_step_input_proof = proof
      sd.aol_reconciled_sources = (BASE,)
      older_restriction = replace(sd.aol_last_intent, madsButtons=not selected)
      fresh = decode_intent(wire(1, selected=selected).aolIntentWire)
      result = sd.reconciled_intent(fresh, sd.aol_last_intent, {BASE: (BASE, True, older_restriction)},
                                   sd.aol_intent_companion.integrity_epoch, BASE + 10_000_000)
      assert result is not None and not result.madsButtonsConsistent
      assert sd.sonata_mads_agreement.accept(result) is None
      # The real owner step cannot silently accept a later matching source.
      sd.aol_reconciled_sources = ()
      for tick in range(2, 8):
        restored = wire(tick, selected=selected)
        with intent_receive(sd, side_effect=[restored.as_reader(), None]):
          axis, _, command = drive(tick, intent_message=restored)
        assert sd.sonata_mads_agreement.rejected and sd.aol_last_intent is None
        assert not axis.lateralActive and not command.latActive and not command.longActive
    finally:
      case.doCleanups()


def test_profile_rejection_survives_producer_round_trip_and_has_bounded_memory():
  owner = MadsProfileAgreement(True)
  rejected = decode_intent(wire(0, selected=False).aolIntentWire)
  assert owner.accept(rejected) is None
  assert owner.accept(decode_intent(wire(1, selected=True, producer='new-card', armed=False).aolIntentWire)) is not None
  assert owner.accept(decode_intent(wire(2, selected=True).aolIntentWire)) is None
  for index in range(20):
    owner.accept(replace(rejected, producerSessionId=f'wrong-card-{index}'))
  assert len(owner.rejected_producers) == 16 and owner.rejection_overflow
  assert owner.accept(decode_intent(wire(3, selected=True, producer='another-new-card').aolIntentWire)) is None
