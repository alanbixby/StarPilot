"""Real owner captures and owned-carrier session/source boundaries for MADS."""
import os
from unittest.mock import patch

import pytest

from opendbc.car import structs
from opendbc.car.hyundai.values import CAR
from openpilot.cereal import custom, messaging
from openpilot.common.params import Params
from openpilot.common.prefix import OpenpilotPrefix
from openpilot.starpilot.aol.tests.classic_startup_test_helpers import prepared_classic_card
from openpilot.starpilot.aol.tests.test_classic_long_profiles import params as classic_params
from openpilot.starpilot.aol.tests import test_ioniq_availability as caller_fixture
from openpilot.starpilot.aol.wire import IntentState, decode_intent, encode_intent
from openpilot.starpilot.car.hyundai.sonata_mads import MadsDisplayState

BASE = 10_000_000_000


@pytest.fixture
def isolated_prefix():
  with OpenpilotPrefix():
    yield


class OneIteration:
  def __init__(self):
    self.calls = 0

  def is_set(self):
    self.calls += 1
    return self.calls > 1


def wire(tick, *, selected, producer='card-startup', armed=True, legacy=False):
  stamp = BASE + tick * 10_000_000
  event = messaging.new_message('aolIntentWire', 0, valid=True, logMonoTime=stamp)
  intent = IntentState(producer, tick + 1, stamp, stamp, stamp + 200_000_000,
                       armed, False, False, True, armed, False, selected)
  if legacy:
    # Omitted additive fields decode as false, exactly as a pre-MADS writer.
    old = custom.AolAxisState.IntentWire.new_message(kind=2, version=1, producerSessionId=producer,
      sequence=tick + 1, carStateLogMonoTime=stamp, observedMonoTime=stamp, validUntilMonoTime=stamp + 200_000_000,
      allowedLatch=armed, settingsQualified=True, lateralArmed=armed)
    event.aolIntentWire = old.to_bytes()
  else:
    event.aolIntentWire = encode_intent(intent)
  return event


@pytest.mark.parametrize('card_selected,sd_selected', [(False, False), (False, True), (True, False), (True, True)])
def test_real_owners_capture_once_and_disagreement_latches(card_selected, sd_selected, monkeypatch):
  case = caller_fixture.IoniqAvailabilityTests(methodName='runTest')
  card = None
  with OpenpilotPrefix(), patch.dict(os.environ, {'SIMULATION': '0', 'AOL_REPLAY_RUNTIME': '1'}):
    saved = Params()
    for key in ('OpenpilotEnabledToggle', 'AlwaysOnLateral', 'AlphaLongitudinalEnabled'):
      saved.put_bool(key, True, block=True)
    saved.put_bool('SonataMadsButtons', card_selected, block=True)
    try:
      card = prepared_classic_card(classic_params(CAR.HYUNDAI_SONATA_HYBRID, lda=True))
      card.slc_producer_session = 'card-startup'
      original_cp = card.CP.to_dict()
      saved.put_bool('SonataMadsButtons', sd_selected, block=True)  # Between the real constructors.
      sd, _, drive = case._authority_callers(False, cp=card.CP.as_reader().as_builder(), genuine_events=True)
      assert card.sonata_mads_buttons == card_selected
      assert card.CI.CS.sonata_mads_buttons == card_selected
      assert sd.sonata_mads_buttons == sd_selected
      assert sd.aol_settings is not card.aol_settings
      assert sd.aol_settings.mads_buttons == sd_selected
      # Actual refresh loops must keep their own startup choices after saved edits.
      saved.put_bool('SonataMadsButtons', not sd_selected, block=True)
      monkeypatch.setattr('time.sleep', lambda _: None)
      card.params_thread(OneIteration())
      sd.params_thread(OneIteration())
      assert card.aol_card_intent.settings.mads_buttons == card_selected
      assert sd.aol_settings.mads_buttons == sd_selected
      assert sd.sonata_mads_agreement.selected == sd_selected
      assert card.CP.to_dict() == original_cp
      captured = {}
      def capture(service, event):
        captured[service] = messaging.log_from_bytes(event if isinstance(event, bytes) else event.to_bytes())
      monkeypatch.setattr(card.pm, 'send', capture)
      cs = structs.CarState(canValid=True, gearShifter='drive')
      card.state_publish(cs, None)
      published = decode_intent(captured['aolIntentWire'].aolIntentWire)
      assert published is not None and published.madsButtons == card_selected
      assert not published.allowedLatch and not published.lateralArmed, 'Constructor/restart is not a driver gesture'
      for tick in range(8):
        axis, _, command = drive(tick, intent_message=wire(tick, selected=published.madsButtons))
      disagreed = card_selected != sd_selected
      assert sd.sonata_mads_agreement.rejected == disagreed
      assert (sd.aol_last_intent is None) == disagreed
      if disagreed:
        assert not axis.lateralActive and not axis.longitudinalActive and not command.latActive and not command.longActive
        # A matching later bit and replacement axis session cannot erase this producer's rejection.
        sd.aol_session_id = 'replacement-axis-session'
        for tick in range(8, 16):
          axis, _, command = drive(tick, intent_message=wire(tick, selected=sd_selected))
          assert sd.sonata_mads_agreement.rejected and sd.aol_last_intent is None
          assert not axis.lateralActive and not axis.longitudinalActive and not command.latActive and not command.longActive
      # Restart the actual Card constructor in this namespace, releasing only
      # its own publishers/transaction. The replacement producer starts unarmed.
      prior_producer = card.slc_producer_session
      card.vehicle_startup.close()
      card.pm.sock.clear()
      saved.put_bool('SonataMadsButtons', sd_selected, block=True)
      card = prepared_classic_card(classic_params(CAR.HYUNDAI_SONATA_HYBRID, lda=True))
      assert card.slc_producer_session != prior_producer
      assert card.sonata_mads_buttons == sd_selected
      assert not card.aol_card_intent.allowed_latch and not card.sonata_mads_display.sources
      monkeypatch.setattr(card.pm, 'send', capture)
      card.state_publish(cs, None)
      replacement = decode_intent(captured['aolIntentWire'].aolIntentWire)
      assert replacement is not None and not replacement.allowedLatch and not replacement.lateralArmed
      for tick in range(16, 25):
        axis, _, command = drive(tick, intent_message=wire(tick, selected=replacement.madsButtons,
          producer=replacement.producerSessionId, armed=replacement.allowedLatch),
          native_request_lat=False, native_request_long=False)
        assert not axis.lateralActive and not axis.longitudinalActive and not command.latActive and not command.longActive
      assert not sd.sonata_mads_agreement.rejected
      assert sd.aol_last_intent is not None and not sd.aol_last_intent.allowedLatch
    finally:
      case.doCleanups()
      if card is not None:
        card.vehicle_startup.close()


@pytest.mark.parametrize('selected', [False, True])
def test_missing_legacy_profile_bit_is_off_and_cannot_requalify_same_session(selected):
  case = caller_fixture.IoniqAvailabilityTests(methodName='runTest')
  with OpenpilotPrefix(), patch.dict(os.environ, {'SIMULATION': '0', 'AOL_REPLAY_RUNTIME': '1'}):
    saved = Params()
    saved.put_bool('SonataMadsButtons', selected, block=True)
    cp = classic_params(CAR.HYUNDAI_SONATA_HYBRID, lda=True)
    try:
      sd, _, drive = case._authority_callers(False, cp=cp, genuine_events=True)
      for tick in range(8):
        legacy = wire(tick, selected=False, legacy=True)
        assert decode_intent(legacy.aolIntentWire).madsButtons is False
        axis, _, command = drive(tick, intent_message=legacy)
      assert sd.sonata_mads_agreement.rejected == selected
      assert (sd.aol_last_intent is None) == selected
      for tick in range(8, 16):
        axis, _, command = drive(tick, intent_message=wire(tick, selected=selected))
        if selected:
          assert sd.aol_last_intent is None and sd.sonata_mads_agreement.rejected
          assert not axis.lateralActive and not axis.longitudinalActive and not command.latActive and not command.longActive
    finally:
      case.doCleanups()


def display_sm(source=BASE, *, producer='card', session='axis', observed=BASE):
  sm = messaging.SubMaster(['aolAxisState'])
  event = messaging.new_message('aolAxisState', valid=True, logMonoTime=observed)
  axis = event.aolAxisState
  axis.sessionId, axis.intentProducerSessionId = session, producer
  axis.sequence, axis.qualified = 1, True
  axis.sourceCarStateMonoTime, axis.observedMonoTime, axis.validUntilMonoTime = source, observed, observed + 30_000_000
  axis.displayIntentValid = axis.displayIntentEnabled = True
  sm.update_msgs(observed / 1e9, [event.as_reader()])
  return sm


@pytest.mark.parametrize('fault', ['source', 'producer', 'axis_session', 'source_lease', 'axis_lease', 'invalid_publication',
                                  'producer_restart', 'axis_restart', 'ci_reset', 'current_can_invalid'])
def test_display_joins_own_previous_publication_and_rejects_wrong_or_stale_owner(fault, isolated_prefix):
  owner = MadsDisplayState(available=True)
  context = ('ci', 'startup')
  def record(stamp, valid=True, producer='card', session='axis', ctx=context):
    owner.record(stamp, valid=valid, producer=producer, session=session, context=ctx)
  record(BASE)
  record(BASE + 10_000_000)
  sm = display_sm(observed=BASE + 10_000_000)
  now = BASE + 20_000_000
  options = {'now_ns': now, 'producer': 'card', 'session': 'axis', 'context': context, 'source_healthy': True}
  # Normal preceding-command ordering: the axis describes our previous valid
  # carState publication, not the most recently published CAN source.
  assert owner.snapshot(sm, **options).valid
  assert owner.snapshot(sm, **options).enabled
  if fault == 'source':
    sm = display_sm(source=BASE - 1, observed=BASE + 10_000_000)
  elif fault == 'producer':
    sm = display_sm(producer='foreign-card', observed=BASE + 10_000_000)
  elif fault == 'axis_session':
    sm = display_sm(session='foreign-axis', observed=BASE + 10_000_000)
  elif fault == 'source_lease':
    options['now_ns'] = BASE + 30_000_001
  elif fault == 'axis_lease':
    options['now_ns'] = BASE + 40_000_001
  elif fault == 'invalid_publication':
    record(BASE + 20_000_000, valid=False)
  elif fault == 'producer_restart':
    record(BASE + 20_000_000, producer='new-card')
    options['producer'] = 'new-card'
    sm = display_sm(producer='new-card', observed=BASE + 10_000_000)
  elif fault == 'axis_restart':
    record(BASE + 20_000_000, session='new-axis')
    options['session'] = 'new-axis'
    sm = display_sm(session='new-axis', observed=BASE + 10_000_000)
  elif fault == 'ci_reset':
    record(BASE + 20_000_000, ctx=('new-ci', 'startup'))
    options['context'] = ('new-ci', 'startup')
  elif fault == 'current_can_invalid':
    options['source_healthy'] = False
  display = owner.snapshot(sm, **options)
  assert display.available and not display.valid and not display.enabled


@pytest.mark.parametrize('selected', [False, True])
def test_changed_profile_bit_after_matching_activation_withdraws_and_never_rearms(selected):
  case = caller_fixture.IoniqAvailabilityTests(methodName='runTest')
  with OpenpilotPrefix(), patch.dict(os.environ, {'SIMULATION': '0', 'AOL_REPLAY_RUNTIME': '1'}):
    saved = Params()
    saved.put_bool('SonataMadsButtons', selected, block=True)
    cp = classic_params(CAR.HYUNDAI_SONATA_HYBRID, lda=True)
    try:
      sd, _, drive = case._authority_callers(False, cp=cp, genuine_events=True)
      for tick in range(10):
        axis, _, command = drive(tick, intent_message=wire(tick, selected=selected))
      assert sd.aol_last_intent is not None and axis.lateralActive and command.latActive
      for tick in range(10, 20):
        advertised = not selected if tick == 10 else selected
        axis, state, command = drive(tick, intent_message=wire(tick, selected=advertised))
        assert sd.sonata_mads_agreement.rejected and sd.aol_last_intent is None
        assert not axis.lateralActive and not axis.longitudinalActive and not command.latActive and not command.longActive
        assert not state.enabled
    finally:
      case.doCleanups()
