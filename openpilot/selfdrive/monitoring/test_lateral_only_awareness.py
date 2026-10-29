"""Manual acceleration must not suppress lateral-only wheel-touch monitoring."""

import pytest

from opendbc.car.structs import car
from openpilot.cereal import log, messaging
from openpilot.common.prefix import OpenpilotPrefix
from openpilot.common.realtime import DT_DMON
from openpilot.selfdrive.monitoring.policy import AlertLevel, DriverMonitoring, MonitoringPolicy
from openpilot.starpilot.aol.runtime import monitoring_lateral_engaged
from openpilot.starpilot.aol.wire import SafetyState, encode_safety


@pytest.fixture
def monitoring():
  with OpenpilotPrefix():
    dm = DriverMonitoring(always_on=False)
    services = ['carState', 'selfdriveState', 'modelV2', 'extrinsicsCalibration', 'driverStateV2',
                'aolAxisState', 'aolSafetyWire']
    sm = messaging.SubMaster(services)
    tick = 0

    def step(*, main_on=False, enabled=False, gas=True, steering=False):
      nonlocal tick
      tick += 1
      now = 10_000_000_000 + tick * int(DT_DMON * 1e9)
      messages = {service: messaging.new_message(service, size=0 if service == 'aolSafetyWire' else None,
                                               valid=True, logMonoTime=now) for service in services}
      state = car.CarState(vEgo=20., gearShifter=car.CarState.GearShifter.drive,
                           gasPressed=gas, steeringPressed=steering)
      state.cruiseState.available = main_on
      state.cruiseState.enabled = enabled
      messages['carState'].carState = state
      messages['selfdriveState'].selfdriveState.enabled = enabled
      messages['modelV2'].modelV2.meta.disengagePredictions.brakeDisengageProbs = [0.]
      messages['extrinsicsCalibration'].extrinsicsCalibration.rpyCalib = [0., 0., 0.]
      driver = log.DriverStateV2.new_message()
      driver.leftDriverData.faceOrientation = [0., 0., 0.]
      driver.leftDriverData.facePosition = [0., 0.]
      driver.leftDriverData.faceOrientationStd = [0., 0., 0.]
      driver.leftDriverData.facePositionStd = [0., 0.]
      messages['driverStateV2'].driverStateV2 = driver
      axis = messages['aolAxisState'].aolAxisState
      axis.qualified = axis.nativeAcknowledged = axis.desiredLateral = axis.lateralActive = True
      axis.sessionId = 'awareness-session'
      axis.sourceCarStateMonoTime = axis.observedMonoTime = now
      axis.validUntilMonoTime = now + 30_000_000
      # Sonata hybrid classic LONG + AOL + physical LDA profile; LONG remains off.
      messages['aolSafetyWire'].aolSafetyWire = encode_safety(SafetyState(
        1, True, now, now + 200_000_000, int(car.CarParams.SafetyModel.hyundai), 0xC06,
        True, False, True, False, 'awareness-panda', axis.sessionId))
      sm.update_msgs(now / 1e9, [message.as_reader() for message in messages.values()])
      assert all(sm.valid.values()) and all(sm.seen.values()) and all(sm.alive.values())
      lateral_engaged = monitoring_lateral_engaged(sm, now_ns=now)
      assert lateral_engaged, 'Each DM tick needs a fresh, matching axis/native lateral proof'
      dm.run_step(sm, aol_lateral_engaged=lateral_engaged)
      packet = dm.get_state_packet().driverMonitoringState
      assert packet.activePolicy == MonitoringPolicy.wheeltouch
      assert not packet.alwaysOn
      return packet

    yield dm, step


@pytest.mark.parametrize('main_on', [False, True], ids=['main_off', 'main_on'])
def test_held_gas_lateral_only_decays_awareness_and_escalates(monitoring, main_on):
  dm, step = monitoring
  levels = []
  awareness = []
  # Default wheel-touch red at 25 s, no-response at 30 s; no shortened settings.
  for _ in range(int(31. / DT_DMON)):
    packet = step(main_on=main_on)
    levels.append(packet.alertLevel)
    awareness.append(packet.wheeltouchPolicyState.awarenessPercent)

  assert awareness[0] < 100
  assert all(current <= previous for previous, current in zip(awareness, awareness[1:], strict=False))
  assert [levels[int(seconds / DT_DMON) - 1] for seconds in (2., 10., 20., 26.)] == [
    AlertLevel.none, AlertLevel.one, AlertLevel.two, AlertLevel.three]
  assert dm.awareness <= 0.
  assert packet.noResponseForceDecel and packet.noResponseCount == 1
  assert packet.lockout and packet.lockoutCount == 1


@pytest.mark.parametrize('main_on,enabled,gas,steering', [
  (True, True, True, False),
  (False, False, False, True),
  (True, False, False, True),
], ids=['ordinary_engaged_gas', 'main_off_steering', 'main_on_steering'])
def test_real_interaction_recovers_positive_awareness(monitoring, main_on, enabled, gas, steering):
  _, step = monitoring
  for _ in range(int(20. / DT_DMON)):
    packet = step(main_on=main_on, enabled=enabled, gas=False)
  assert packet.alertLevel == AlertLevel.two
  assert 0 < packet.wheeltouchPolicyState.awarenessPercent < 100

  for _ in range(int(31. / DT_DMON)):
    packet = step(main_on=main_on, enabled=enabled, gas=gas, steering=steering)
    assert packet.wheeltouchPolicyState.awarenessPercent == 100
    assert packet.alertLevel == AlertLevel.none
    assert not packet.lockout and not packet.noResponseForceDecel


@pytest.mark.parametrize('main_on,enabled,gas,steering', [
  (True, True, True, False),
  (False, False, False, True),
], ids=['ordinary_engaged_gas', 'main_off_steering'])
def test_interaction_cannot_clear_terminal_awareness(monitoring, main_on, enabled, gas, steering):
  _, step = monitoring
  for _ in range(int(26. / DT_DMON)):
    packet = step(main_on=main_on, enabled=enabled, gas=False)
  assert packet.alertLevel == AlertLevel.three

  for _ in range(int(5. / DT_DMON)):
    packet = step(main_on=main_on, enabled=enabled, gas=gas, steering=steering)
    assert packet.alertLevel == AlertLevel.three
    assert packet.wheeltouchPolicyState.awarenessPercent == 0
  assert packet.noResponseForceDecel and packet.noResponseCount == 1
  assert packet.lockout and packet.lockoutCount == 1
