"""Mixed MAIN-off events through real SelfdriveD, Controls and Card feedback.

Transport/clock and DM/deviceMotion envelopes are synthetic host inputs. Native
permissions come from the real C06 hooks after actual bus-0 BCM gestures, with
all required RX kept live. No event, decision, check counter or actuation flag
is injected. This is host regression coverage, not device qualification.
"""
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from opendbc.car.hyundai.values import CAR
from opendbc.safety.tests.libsafety import libsafety_py
from opendbc.safety.tests.test_hyundai import checksum
from opendbc.safety.tests.test_sonata_mads_native_contract import SonataNative
from openpilot.cereal import log, messaging
from openpilot.common.params import Params
from openpilot.common.prefix import OpenpilotPrefix
from openpilot.selfdrive.selfdrived.events import EventName
from openpilot.selfdrive.selfdrived.helpers import MIN_EXCESSIVE_ACTUATION_COUNT, MIN_LATERAL_ENGAGE_BUFFER
from openpilot.selfdrive.selfdrived.selfdrived import SelfdriveD
from openpilot.starpilot.aol.tests import test_ioniq_availability as caller_fixture
from openpilot.starpilot.aol.tests.test_classic_long_profiles import params as classic_params
from openpilot.starpilot.aol.tests.test_sonata_mads_sessions import BASE, wire
from openpilot.starpilot.aol.transport_pause import TransportPauseFeedback
from openpilot.starpilot.aol.wire import PandaSlot, SafetyState, encode_safety


@pytest.fixture
def mads(monkeypatch):
  case = caller_fixture.IoniqAvailabilityTests(methodName='runTest')
  with OpenpilotPrefix(), patch.dict(os.environ, {'SIMULATION': '0', 'AOL_REPLAY_RUNTIME': '1'}):
    Params().put_bool('SonataMadsButtons', True, block=True)
    native = libsafety_py.libsafety
    try:
      sd, controls, step = case._authority_callers(False,
        cp=classic_params(CAR.HYUNDAI_SONATA_HYBRID, lda=True), genuine_events=True)
      assert sd.update_events.__func__ is SelfdriveD.update_events
      config = sd.CP.safetyConfigs[0]
      assert config.safetyParam == 0xC06 and sd.CP.alternativeExperience == 32
      assert not sd.aol_cruise_main_required and not sd.CP.notCar
      car = SonataNative(native)
      # Match the joined fixture's post-hook libsafety initialization: the
      # board-owned relay transition timer is already elapsed. This neither
      # grants controls/axis permissions nor changes a safety callback.
      native.init_tests()
      native.set_alternative_experience(sd.CP.alternativeExperience)
      car.now, car.next_safety_tick = BASE // 1000 - 90_000, BASE // 1000 + 1_000_000
      car.warm()
      car.arm_lfa()
      assert not native.get_controls_allowed()
      feedback = TransportPauseFeedback(sd.CP, mads_buttons=True)
      feedback_sm = messaging.SubMaster(['aolAxisState', 'onroadEvents', 'aolSafetyWire', 'pandaStates'])
      inputs = SimpleNamespace(dm_valid=True, terminal=False, lockout=False, always_on_lockout=False, yaw_rate=0.)
      update_msgs = sd.sm.update_msgs

      def sensors(timestamp, samples):
        # Replace only arriving sensor envelopes, retaining the fixture's real
        # service cadence, stamps, validity/frequency checks and subscriptions.
        replaced = []
        for sample in samples:
          if sample.which() == 'driverMonitoringState':
            sample = sample.as_builder()
            sample.valid = inputs.dm_valid
            sample.driverMonitoringState.activePolicy = 'vision'
            sample.driverMonitoringState.alertLevel = 'three' if inputs.terminal else 'none'
            sample.driverMonitoringState.lockout = inputs.lockout
            sample.driverMonitoringState.alwaysOnLockout = inputs.always_on_lockout
            sample = sample.as_reader()
          elif sample.which() == 'deviceMotion':
            sample = sample.as_builder()
            sample.deviceMotion.angularVelocityDevice.z = inputs.yaw_rate
            sample = sample.as_reader()
          replaced.append(sample)
        return update_msgs(timestamp, replaced)

      monkeypatch.setattr(sd.sm, 'update_msgs', sensors)
      tick = -1
      requested = 1  # Claimed by the real neutral -> BCM press above.

      def drive(*, main=False, armed=True, fault=None, gesture=False):
        nonlocal tick, requested
        tick += 1
        now = BASE + tick * 10_000_000
        if gesture:
          # Fresh physical gesture, not a forged native acknowledgment.
          native.set_aol_test_heartbeat(True)
          car.arm_lfa()
          requested = 1
        native.set_aol_test_heartbeat(bool(requested))
        native.aol_set_host_request(requested)
        car.frame(omit='TCS13' if fault == 'rx' else None)
        if fault == 'rx':
          # Corrupt an actual required input's checksum, without setting a
          # host event or directly changing native permission/health flags.
          address, data, bus = checksum(car.packer.make_can_msg('TCS13', 0,
            {'AliveCounterTCS': (car.counter - 1) % 8}))
          data = bytearray(data)
          data[0] ^= 1
          assert not native.safety_rx_hook(libsafety_py.make_CANPacket(address, bus, data))
        elif fault == 'relay':
          assert car.rx('LKAS11', {'CR_Lkas_StrToqReq': 0})
          assert native.get_relay_malfunction()
        mask = int(native.aol_get_permission_mask())
        if gesture:
          assert mask == 1, 'Fresh entry must be denied by DM despite actual native LAT permission'
        rx_invalid = bool(native.safety_test_toyota_rx_summary_invalid())
        relay = bool(native.get_relay_malfunction())
        pandas = messaging.new_message('pandaStates', 1, valid=True, logMonoTime=now)
        panda = pandas.pandaStates[0]
        panda.safetyModel, panda.safetyParam = config.safetyModel, config.safetyParam
        panda.alternativeExperience = sd.CP.alternativeExperience
        panda.controlsAllowed, panda.safetyRxChecksInvalid = native.get_controls_allowed(), rx_invalid
        if relay:
          panda.faults = ['relayMalfunction']
        safety = messaging.new_message('aolSafetyWire', 0, valid=True, logMonoTime=now)
        slot = PandaSlot(0, 'authority-panda', int(config.safetyModel.raw), config.safetyParam,
          sd.CP.alternativeExperience, native.get_controls_allowed(), rx_invalid, False, int(relay))
        safety.aolSafetyWire = encode_safety(SafetyState(1, True, now, now + 30_000_000,
          int(config.safetyModel.raw), config.safetyParam, bool(mask & 1), bool(mask & 2),
          bool(native.aol_get_request_mask() & 1), bool(native.aol_get_request_mask() & 2),
          'authority-panda', sd.aol_session_id, now, (slot,)))
        state = messaging.new_message('carState', valid=True, logMonoTime=now)
        state.carState = caller_fixture.car_state()
        state.carState.cruiseState.available = main
        if fault in ('parkingBrake', 'steeringDisengage'):
          setattr(state.carState, fault, True)
        elif fault == 'nonAdaptive':
          state.carState.cruiseState.nonAdaptive = True
        intent = wire(tick, selected=True, armed=armed)
        result = step(tick, car_state_message=state, intent_message=intent, companion=False,
          panda_state_message=pandas, safety_message=safety,
          panda_controls_allowed=native.get_controls_allowed(), native_request_long=False)
        axis, _, command = result
        requested = int(axis.desiredLateral) | int(axis.desiredLongitudinal) << 1
        feedback_sm.update_msgs(now / 1e9,
          [pandas.as_reader(), safety.as_reader(), sd.pm.messages['aolAxisState'], sd.pm.messages['onroadEvents']])
        feedback.observe(feedback_sm, sd.CP, state.carState, now, latched=armed, intent_producer_session='card-startup')
        assert not axis.longitudinalActive and not command.longActive
        assert not native.get_controls_allowed(), 'No SET/RES gesture ever authorizes LONG'
        return result

      yield SimpleNamespace(sd=sd, controls=controls, drive=drive, inputs=inputs, feedback=feedback, native=native)
    finally:
      case.doCleanups()
      native.set_alternative_experience(0)


def published_events(sd):
  return {int(event.name.raw): event for event in sd.pm.messages['onroadEvents'].onroadEvents}


def assert_withdrawn(result):
  axis, state, command = result
  assert not state.enabled
  assert not axis.lateralActive and not axis.longitudinalActive
  assert not command.latActive and not command.longActive
  assert command.actuators.torque == command.actuators.accel == 0.


@pytest.mark.parametrize('fault,event,kind', [
  ('rx', EventName.controlsMismatch, 'immediateDisable'),
  ('relay', EventName.relayMalfunction, 'immediateDisable'),
  ('parkingBrake', EventName.parkBrake, 'userDisable'),
  ('nonAdaptive', EventName.wrongCruiseMode, 'userDisable'),
  ('steeringDisengage', EventName.steerDisengage, 'userDisable'),
])
def test_main_off_mixed_fault_survives_real_event_production_and_card_feedback(mads, fault, event, kind):
  for _ in range(12):
    axis, _, command = mads.drive()
    assert axis.lateralActive and command.latActive
  assert mads.feedback.lateral_only_baseline and not mads.feedback.failed
  assert set(published_events(mads.sd)) == {EventName.wrongCarMode}
  # RX summary is production safety_tick-owned (1 Hz), so allow that tick;
  # native's immediate permission check may withdraw before the summary event.
  for _ in range(110 if fault == 'rx' else 1):
    result = mads.drive(fault=fault)
    if event in published_events(mads.sd):
      break
  events = published_events(mads.sd)
  assert EventName.wrongCarMode in events and events[EventName.wrongCarMode].userDisable
  assert event in events and getattr(events[event], kind) and events[event].noEntry
  assert_withdrawn(result)
  assert mads.feedback.failed and not mads.feedback.lateral_only_baseline


def test_main_off_invalid_dm_envelope_withdraws_and_denies_new_entry(mads):
  for _ in range(12):
    axis, _, command = mads.drive()
    assert axis.lateralActive and command.latActive
  mads.inputs.dm_valid = False
  for _ in range(10):
    result = mads.drive()
  assert not mads.sd.sm.all_checks(['driverMonitoringState'])
  events = published_events(mads.sd)
  assert EventName.wrongCarMode in events
  assert events[EventName.commIssue].noEntry and events[EventName.commIssue].softDisable
  assert_withdrawn(result)
  mads.drive(armed=False)
  assert_withdrawn(mads.drive(gesture=True))


@pytest.mark.parametrize('main', [False, True])
@pytest.mark.parametrize('lockout', ['lockout', 'always_on_lockout'])
def test_terminal_dm_warning_survives_and_fresh_entry_is_denied(mads, main, lockout):
  for _ in range(12):
    axis, _, command = mads.drive(main=main)
    assert axis.lateralActive and command.latActive
  mads.inputs.terminal = True
  setattr(mads.inputs, lockout, True)
  for _ in range(10):
    result = mads.drive(main=main)
  events = published_events(mads.sd)
  assert events[EventName.driverDistracted3].permanent
  assert events[EventName.tooDistracted].noEntry
  assert (EventName.wrongCarMode in events) == (not main)
  axis, state, command = result
  assert state.alertType == 'driverDistracted3/permanent'
  assert state.alertSound == log.SelfdriveState.AudibleAlert.warningImmediate
  # Existing armed MAIN-on retention is intentional; MAIN-off has no such
  # exception. Neither case may silence the permanent terminal warning.
  assert axis.lateralActive == command.latActive == main
  mads.drive(main=main, armed=False)
  result = mads.drive(main=main, gesture=True)
  assert_withdrawn(result)
  assert EventName.driverDistracted3 in published_events(mads.sd)
  assert EventName.tooDistracted in published_events(mads.sd)


def test_sustained_measured_excessive_lateral_acceleration_main_off_long_off(mads):
  for _ in range(MIN_LATERAL_ENGAGE_BUFFER + 12):
    axis, _, command = mads.drive()
    assert axis.lateralActive and command.latActive
    assert not mads.sd.sm['carControl'].longActive
  sd = mads.sd
  assert sd.pose_calibrator.calib_valid and sd.calibrated_pose is not None
  assert sd.excessive_actuation_check._engaged_counter > MIN_LATERAL_ENGAGE_BUFFER
  assert not sd.excessive_actuation and sd.params.get('Offroad_ExcessiveActuation') is None
  assert sd.excessive_actuation_check._excessive_counter == 0
  # vEgo=20 m/s, measured calibrated yaw=0.4 rad/s, roll=0: 8 m/s²,
  # beyond the existing 6 m/s² threshold. Long acceleration and aEgo agree.
  mads.inputs.yaw_rate = .4
  for _ in range(MIN_EXCESSIVE_ACTUATION_COUNT + 10):
    result = mads.drive()
    if sd.excessive_actuation:
      break
    assert EventName.excessiveActuation not in published_events(sd)
    assert sd.excessive_actuation_check._excessive_counter <= MIN_EXCESSIVE_ACTUATION_COUNT
    assert result[0].lateralActive and result[2].latActive
  assert sd.calibrated_pose.angular_velocity.yaw == pytest.approx(.4)
  assert sd.calibrated_pose.acceleration.x == sd.CS_prev.aEgo == 0.
  assert sd.sm['vehicleParameters'].roll == 0.
  assert sd.excessive_actuation_check._excessive_counter == MIN_EXCESSIVE_ACTUATION_COUNT + 1
  assert sd.excessive_actuation and sd.params.get('Offroad_ExcessiveActuation') is not None
  events = published_events(sd)
  assert EventName.wrongCarMode in events
  assert events[EventName.excessiveActuation].softDisable and events[EventName.excessiveActuation].noEntry
  assert_withdrawn(result)
  # Intent retention is not output permission: subsequent real decisions must
  # stay withdrawn while the persistent excessive-actuation event remains.
  for _ in range(2):
    assert_withdrawn(mads.drive())
    assert published_events(sd)[EventName.excessiveActuation].noEntry
