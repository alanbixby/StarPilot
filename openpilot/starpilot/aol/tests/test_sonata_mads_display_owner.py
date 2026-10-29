"""Card's display publisher and consumer bind the same accepted axis session."""
import os
from unittest.mock import patch

from opendbc.car import structs
from opendbc.car.hyundai.values import CAR
from openpilot.cereal import messaging
from openpilot.common.params import Params
from openpilot.common.prefix import OpenpilotPrefix
from openpilot.starpilot.aol.tests.classic_startup_test_helpers import prepared_classic_card
from openpilot.starpilot.aol.tests.test_classic_long_profiles import params as classic_params
from openpilot.starpilot.aol.tests.test_sonata_mads_sessions import BASE, display_sm


def test_card_display_uses_accepted_session_despite_unaccepted_axis_observation(monkeypatch):
  with OpenpilotPrefix(), patch.dict(os.environ, {'SIMULATION': '0', 'AOL_REPLAY_RUNTIME': '1'}):
    saved = Params()
    for key in ('OpenpilotEnabledToggle', 'AlwaysOnLateral', 'SonataMadsButtons', 'AlphaLongitudinalEnabled'):
      saved.put_bool(key, True, block=True)
    card = prepared_classic_card(classic_params(CAR.HYUNDAI_SONATA_HYBRID, lda=True))
    try:
      # This is the accepted feedback identity. Merely observing a different
      # axis on SubMaster must neither admit it nor erase this session's ring.
      card.aol_transport_feedback.session = 'accepted-axis'
      now = [BASE]
      original_message = messaging.new_message

      def clocked_message(*args, **kwargs):
        kwargs.setdefault('logMonoTime', now[0])
        return original_message(*args, **kwargs)

      monkeypatch.setattr(messaging, 'new_message', clocked_message)
      monkeypatch.setattr('time.monotonic_ns', lambda: now[0])
      monkeypatch.setattr('openpilot.selfdrive.car.card.REPLAY', False)
      cs = structs.CarState(canValid=True, gearShifter='drive')
      card.CI.CS.out = cs
      monkeypatch.setattr(card.CI, 'apply', lambda *args, **kwargs: card.CI.CC.lateral_display)

      def axis(session, source):
        sample = display_sm(source=source, producer=card.slc_producer_session, session=session, observed=now[0])
        event = original_message('aolAxisState', valid=True, logMonoTime=now[0])
        event.aolAxisState = sample['aolAxisState']
        card.sm.update_msgs(now[0] / 1e9, [event.as_reader()])

      axis('unaccepted-axis', BASE)
      card.state_publish(cs, None)
      display = card.apply_with_lateral_display(structs.CarControl(), now[0])
      assert display.available and not display.valid and not display.enabled
      now[0] += 10_000_000
      card.state_publish(cs, None)
      now[0] += 10_000_000
      axis('accepted-axis', BASE + 10_000_000)
      display = card.apply_with_lateral_display(structs.CarControl(), now[0])
      assert display.available and display.valid and display.enabled
      # The same original source expires at 30 ms; no observation renews it.
      now[0] = BASE + 40_000_001
      axis('accepted-axis', BASE + 10_000_000)
      assert not card.apply_with_lateral_display(structs.CarControl(), now[0]).valid
    finally:
      card.vehicle_startup.close()
