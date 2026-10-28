"""GM CC lead qualification applies to the final command, independently of AOL."""
from contextlib import ExitStack
import os
from unittest.mock import patch

import pytest

from opendbc.car.gm.tests.test_conventional_pedal import params as conventional_params
from opendbc.car.gm.tests.test_ordinary_cc import malibu_f1_params
from opendbc.car.gm.values import ORDINARY_CC_CAR
from openpilot.cereal import messaging
from openpilot.common.prefix import OpenpilotPrefix
from openpilot.starpilot.aol.tests import test_ioniq_availability as fixture
from openpilot.starpilot.aol.tests.test_gm_volt_f1_lifecycle import automatic_params


def configuration(profile):
  if profile == 'volt-cc-f1':
    return automatic_params()
  if profile == 'malibu-cc-f1':
    return malibu_f1_params(release=False)
  return conventional_params(sorted(ORDINARY_CC_CAR)[0], removed=False, disabled=False)


@pytest.mark.parametrize('simulation', [None, '0', '1'])
def test_deferred_authority_cleanup_preserves_outer_environment_and_ipc(simulation):
  with patch.dict(os.environ, {'AOL_REPLAY_RUNTIME': '0'}), OpenpilotPrefix():
    if simulation is None:
      os.environ.pop('SIMULATION', None)
    else:
      os.environ['SIMULATION'] = simulation
    outer_prefix = os.environ['OPENPILOT_PREFIX']
    case = fixture.IoniqAvailabilityTests(methodName='runTest')
    try:
      with OpenpilotPrefix(), patch.dict(os.environ, {'AOL_REPLAY_RUNTIME': '1'}):
        _, controls, _ = case._authority_callers(False)
        assert not controls.sm.simulation
    finally:
      # Callers defer this until after their inner contexts (and IPC directory)
      # have exited. Cleanup must restore only the helper-owned simulation key.
      case.doCleanups()

    assert (os.environ['OPENPILOT_PREFIX'], os.environ['AOL_REPLAY_RUNTIME']) == (outer_prefix, '0')
    assert os.environ.get('SIMULATION') == simulation
    subscriber = messaging.SubMaster(['carState'])
    assert subscriber.simulation == (simulation == '1')
    assert not subscriber.seen['carState']
    del subscriber


@pytest.mark.parametrize('profile', ['volt-cc-f1', 'malibu-cc-f1', 'conventional-pedal'])
@pytest.mark.parametrize('aol, normal_enabled', [(False, True), (True, True), (True, False)])
@pytest.mark.parametrize('primary, planned', [(True, False), (False, True), (True, True), (False, False)])
def test_lead_agreement_qualifies_longitudinal_without_withdrawing_lateral(profile, aol, normal_enabled, primary, planned):
  case = fixture.IoniqAvailabilityTests(methodName='runTest')
  with ExitStack() as cleanup:
    cleanup.callback(case.doCleanups)
    cleanup.enter_context(OpenpilotPrefix())
    cleanup.enter_context(patch.dict('os.environ', {'REPLAY': '0'}))
    clock = cleanup.enter_context(patch('openpilot.starpilot.longitudinal.inputs.clock_pair_ns',
                                       return_value=(10_000_000_000, 10_000_000_000)))
    _, controls, step = case._authority_callers(True, cp=configuration(profile))
    controls.lane_centering_host = None
    assert controls.longitudinal_inputs.gm_cc_enabled
    assert controls.aol_replay

    # Warm the real plan frequency tracker at its production 20 Hz cadence.
    for stamp in range(9_000_000_000, 10_000_000_000, 50_000_000):
      plan = messaging.new_message('longitudinalPlan', valid=True, logMonoTime=stamp)
      controls.sm.update_msgs(stamp / 1e9, [plan.as_reader()])

    # The first real qualification establishes the clock epoch. Only subsequent
    # producer messages may authorize longitudinal control in that epoch.
    step(0)
    now = 10_010_000_000
    clock.return_value = (now, now)
    axis, state, _ = step(1)
    assert axis.longitudinalActive and state.enabled and state.active

    radar = messaging.new_message('radarState', valid=True, logMonoTime=now)
    radar.radarState.leadOne.present = primary
    radar.radarState.leadTwo.present = not primary
    plan = messaging.new_message('longitudinalPlan', valid=True, logMonoTime=now)
    plan.longitudinalPlan.hasLead = planned
    plan.longitudinalPlan.aTarget = 0.5
    controls.sm.update_msgs(now / 1e9, [radar.as_reader(), plan.as_reader()])
    controls.aol_replay = aol
    if not normal_enabled:
      # Axis authority is independent of the ordinary state-machine flag. A
      # longitudinal evidence check must not reintroduce that flag as a veto.
      selfdrive = messaging.new_message('selfdriveState', valid=True, logMonoTime=now)
      selfdrive.selfdriveState = controls.sm['selfdriveState']
      selfdrive.selfdriveState.enabled = selfdrive.selfdriveState.active = False
      controls.sm.update_msgs(now / 1e9, [selfdrive.as_reader()])

    command, _ = controls.state_control()
    assert command.enabled == normal_enabled
    assert command.latActive
    assert command.longActive == (primary == planned)
    if primary == planned:
      assert command.actuators.accel > 0
    else:
      assert command.actuators.accel == 0
