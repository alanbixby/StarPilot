from contextlib import ExitStack
from unittest import mock

import pytest

from opendbc.car.hyundai.canfd_angle_aol import ANGLE_ACK_TIMEOUT_NS, MODELS
from openpilot.common.params import Params
from openpilot.common.prefix import OpenpilotPrefix
from openpilot.starpilot.aol.intent import read_settings
from openpilot.starpilot.aol.tests import test_ioniq_availability as fixture


@pytest.mark.parametrize('identity', sorted(MODELS))
@pytest.mark.parametrize('startup_threshold,live_threshold,paused', [(0., 5., True), (5., 0., False)])
def test_card_live_brake_pause_preserves_native_ack_deadline(identity, startup_threshold, live_threshold, paused):
  case = fixture.IoniqAvailabilityTests(methodName='runTest')
  with ExitStack() as cleanup:
    cleanup.callback(case.doCleanups)
    with OpenpilotPrefix():
      saved = Params()
      saved.put('LKASButtonControl', 9, block=True)
      saved.put('MainCruiseButtonControl', 0, block=True)
      saved.put('AolBrakePauseSpeedMps', startup_threshold, block=True)
      sd, _, step, card, receive = case._ioniq_availability_callers(full_op=False, stock_angle=identity)
      sd.aol_settings = read_settings(saved)
      assert sd.aol_settings == card.aol_card_intent.settings == card.aol_settings

      # Change the preference onroad through Card's actual refresh path.
      saved.put('AolBrakePauseSpeedMps', live_threshold, block=True)
      event = mock.Mock()
      event.is_set.side_effect = [False, True]
      with mock.patch('openpilot.selfdrive.car.card.time.sleep'):
        card.params_thread(event)
      sd.aol_settings = read_settings(saved)
      assert card.aol_card_intent.settings == sd.aol_settings
      assert card.aol_settings.pause_brake_mps == startup_threshold
      assert card.aol_card_intent.settings.pause_brake_mps == live_threshold
      assert not card.aol_card_intent.pause_lateral

      braking = fixture.car_state(brake=True)
      braking.vEgo = braking.vEgoRaw = 3.
      start = 10_020_000_000
      # Fresh neutral Panda echoes model intentional withdrawal when paused,
      # and a missing acknowledgment when steering is expected after lowering.
      for tick in range(2, 24):
        now = 10_000_000_000 + tick * 10_000_000
        axis, _, command = step(tick, observed_state=braking, allowed=False, native_request_lat=False,
                                native_request_long=False, panda_controls_allowed=False)
        if tick == 2:
          assert axis.desiredLateral == (not paused)
        assert not axis.lateralActive and not command.latActive
        assert not axis.longitudinalActive and not command.longActive
        expected_latch = paused or now - start < ANGLE_ACK_TIMEOUT_NS
        assert receive(now, state=braking, axis=sd.pm.messages['aolAxisState'],
                       event=sd.pm.messages['onroadEvents']) == expected_latch, (tick, paused, card.aol_card_intent.latch_clear_reason)
      assert card.aol_card_intent.latch_clear_reason == ('' if paused else 'native_rejection')

      if paused:
        # Intentional pause survives beyond the deadline and resumes without
        # another button press when the brake is released and Panda grants it.
        rolling = fixture.car_state()
        rolling.vEgo = rolling.vEgoRaw = 3.
        axis, _, command = step(24, observed_state=rolling, native_request_long=False)
        assert axis.lateralActive and command.latActive
        assert receive(10_240_000_000, state=rolling, axis=sd.pm.messages['aolAxisState'],
                       event=sd.pm.messages['onroadEvents'])
