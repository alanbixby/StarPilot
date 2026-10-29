"""Saved MADS never takes MAIN from an owner with the AOL master off."""
import os
from unittest.mock import patch

import pytest

from opendbc.car.hyundai.classic_long_aol import independent_buttons_qualified
from opendbc.car.hyundai.interface import CarInterface
from opendbc.car.hyundai.tests.test_sonata_alpha_implicit import classic_frames
from opendbc.car.hyundai.values import CAR
from openpilot.common.params import Params
from openpilot.common.prefix import OpenpilotPrefix
from openpilot.selfdrive.selfdrived.selfdrived import SelfdriveD
from openpilot.starpilot.aol.tests.classic_startup_test_helpers import prepared_classic_card
from openpilot.starpilot.aol.tests.test_classic_long_profiles import params as classic_params
from openpilot.starpilot.aol.tests import test_ioniq_availability as caller_fixture
from openpilot.starpilot.aol.tests.test_intent_projection import intent_receive
from openpilot.starpilot.aol.tests.test_sonata_mads_sessions import OneIteration, wire


def ordinary_main(ci, monkeypatch):
  for tick in range(1, 151):
    now = 10_000_000_000 + tick * 10_000_000
    monkeypatch.setattr('time.clock_gettime_ns', lambda clock, stamp=now: stamp)
    cs = ci.update([(now, classic_frames(ci, tick, main=int(121 <= tick <= 130)))])
  assert cs.canValid and not cs.accFaulted
  assert cs.cruiseState.available, 'Healthy ordinary cruise MAIN must remain usable'
  return cs


@pytest.mark.parametrize('auxiliary', [0, 3])
@pytest.mark.parametrize('prior_mads_drive', [False, True])
def test_real_owners_master_off_preserves_saved_profile_and_ordinary_main(auxiliary, prior_mads_drive, monkeypatch):
  with OpenpilotPrefix(), patch.dict(os.environ, {'SIMULATION': '0', 'AOL_REPLAY_RUNTIME': '1'}):
    saved = Params()
    for key in ('OpenpilotEnabledToggle', 'AlphaLongitudinalEnabled', 'SonataMadsButtons'):
      saved.put_bool(key, True, block=True)
    keys = ('LKASButtonControl', 'MainCruiseButtonControl', 'CancelButtonControl', 'LongCancelButtonControl',
            'VeryLongCancelButtonControl', 'DistanceButtonControl', 'LongDistanceButtonControl',
            'VeryLongDistanceButtonControl', 'ModeButtonControl', 'LongModeButtonControl', 'VeryLongModeButtonControl',
            'StarButtonControl', 'LongStarButtonControl', 'VeryLongStarButtonControl')
    for key in keys:
      saved.put(key, 0, block=True)
    saved.put('AolBrakePauseSpeedMps', 10., block=True)
    card = None
    try:
      if prior_mads_drive:
        for key, value in (('LKASButtonControl', 8), ('MainCruiseButtonControl', 9), ('CancelButtonControl', 8)):
          saved.put(key, value, block=True)
        saved.put_bool('AlwaysOnLateral', True, block=True)
        card = prepared_classic_card(classic_params(CAR.HYUNDAI_SONATA_HYBRID, lda=True))
        assert card.sonata_mads_buttons and independent_buttons_qualified(card.CP)
        # Disabling the master affects the next constructor, not this drive.
        saved.put_bool('AlwaysOnLateral', False, block=True)
        assert card.sonata_mads_buttons and card.CI.CS.sonata_mads_buttons
        card.vehicle_startup.close()
        card.pm.sock.clear()
      saved.put_bool('AlwaysOnLateral', False, block=True)
      saved.put('LongDistanceButtonControl', auxiliary, block=True)
      before = {key: saved.get(key) for key in (*keys, 'SonataMadsButtons', 'AolBrakePauseSpeedMps')}
      card = prepared_classic_card(classic_params(CAR.HYUNDAI_SONATA_HYBRID, lda=True))
      cs = ordinary_main(card.CI, monkeypatch)
      assert not card.sonata_mads_buttons and not card.CI.CS.sonata_mads_buttons
      assert independent_buttons_qualified(card.CP) == bool(auxiliary)
      assert not card.sonata_mads_display.available
      assert not card.aol_settings.enabled and not card.aol_settings.mads_buttons
      if card.aol_card_intent is not None:
        card.aol_card_intent.update(cs, now_ns=11_500_000_000, fault_active=False)
        assert not card.aol_card_intent.allowed_latch
      sd = SelfdriveD(CP=card.CP.as_reader())
      assert not sd.sonata_mads_buttons and not sd.sonata_mads_agreement.selected
      assert not sd.aol_settings.enabled and not sd.aol_settings.mads_buttons
      assert {key: saved.get(key) for key in before} == before
      # A saved edit after both owners start cannot acquire MADS ownership.
      saved.put_bool('AlwaysOnLateral', True, block=True)
      assert not card.sonata_mads_buttons and not sd.sonata_mads_buttons
    finally:
      if card is not None:
        card.vehicle_startup.close()


def test_unmarked_parser_does_not_accept_mads_main_ownership(monkeypatch):
  cp = classic_params(CAR.HYUNDAI_SONATA_HYBRID, lda=True)
  assert not independent_buttons_qualified(cp)
  ci = CarInterface(cp)
  ci.CS.sonata_mads_buttons = True  # An invalid owner cannot manufacture native qualification.
  ordinary_main(ci, monkeypatch)


@pytest.mark.parametrize('card_master', [False, True])
def test_master_changes_between_real_owners_latch_profile_disagreement(card_master, monkeypatch):
  case = caller_fixture.IoniqAvailabilityTests(methodName='runTest')
  card = None
  with OpenpilotPrefix(), patch.dict(os.environ, {'SIMULATION': '0', 'AOL_REPLAY_RUNTIME': '1'}):
    saved = Params()
    for key in ('OpenpilotEnabledToggle', 'AlphaLongitudinalEnabled', 'SonataMadsButtons'):
      saved.put_bool(key, True, block=True)
    # A legitimate auxiliary owner keeps CP marked even with the master off.
    saved.put('LongDistanceButtonControl', 3, block=True)
    saved.put_bool('AlwaysOnLateral', card_master, block=True)
    try:
      card = prepared_classic_card(classic_params(CAR.HYUNDAI_SONATA_HYBRID, lda=True))
      sd, _, drive = case._authority_callers(False, cp=card.CP.as_reader().as_builder(),
        genuine_events=True, aol_enabled=not card_master)
      assert independent_buttons_qualified(card.CP)
      assert card.sonata_mads_buttons == card.CI.CS.sonata_mads_buttons == card_master
      assert sd.sonata_mads_buttons == sd.sonata_mads_agreement.selected == (not card_master)
      saved.put_bool('AlwaysOnLateral', card_master, block=True)
      monkeypatch.setattr('time.sleep', lambda _: None)
      card.params_thread(OneIteration())
      sd.params_thread(OneIteration())
      assert card.aol_card_intent.settings.mads_buttons == card_master
      assert sd.aol_settings.mads_buttons == (not card_master)
      for tick in range(8):
        event = wire(tick, selected=card_master if tick == 0 else not card_master)
        with intent_receive(sd, side_effect=[event.as_reader(), None]):
          axis, state, command = drive(tick, intent_message=event)
        assert sd.sonata_mads_agreement.rejected and sd.aol_last_intent is None
        assert not axis.lateralActive and not command.latActive and not command.longActive and not state.enabled
      assert saved.get_bool('SonataMadsButtons')
    finally:
      case.doCleanups()
      if card is not None:
        card.vehicle_startup.close()
