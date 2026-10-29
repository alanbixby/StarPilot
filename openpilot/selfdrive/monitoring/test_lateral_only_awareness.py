"""Manual acceleration and AOL proof gaps must not suppress lateral monitoring."""

from types import SimpleNamespace

import pytest

from opendbc.car.structs import car
from openpilot.cereal import log, messaging
from openpilot.common.prefix import OpenpilotPrefix
from openpilot.common.realtime import DT_DMON
from openpilot.selfdrive.monitoring import dmonitoringd
from openpilot.selfdrive.monitoring.policy import AlertLevel, DriverMonitoring, MonitoringPolicy
from openpilot.starpilot.aol.runtime import monitoring_lateral_engaged
from openpilot.starpilot.aol.wire import SafetyState, encode_safety


@pytest.fixture
def monitoring(monkeypatch):
  with OpenpilotPrefix():
    dm = DriverMonitoring(always_on=False)
    services = ['carState', 'carControl', 'selfdriveState', 'modelV2', 'extrinsicsCalibration', 'driverStateV2',
                'aolAxisState', 'aolSafetyWire']
    sm = messaging.SubMaster(services, ignore_avg_freq=services)
    tick = 0
    now = 0
    pending_tick = False
    sent = []

    class TickComplete(Exception):
      pass

    def update():
      nonlocal pending_tick
      if not pending_tick:
        raise TickComplete
      pending_tick = False

    def subscribe(selected_services, *, poll):
      assert 'carControl' in selected_services
      assert poll == 'driverStateV2'
      return sm

    # Run the production daemon for one sample, retaining the real policy across
    # samples. Only the IPC transport, clock and process setup are substituted.
    monkeypatch.setattr(sm, 'update', update)
    monkeypatch.setattr(dmonitoringd, 'config_realtime_process', lambda *args: None)
    monkeypatch.setattr(dmonitoringd, 'DriverMonitoring', lambda **kwargs: dm)
    monkeypatch.setattr(dmonitoringd.messaging, 'SubMaster', subscribe)
    monkeypatch.setattr(dmonitoringd.messaging, 'PubMaster', lambda *args: SimpleNamespace(send=lambda service, dat: sent.append(dat)))
    monkeypatch.setattr(dmonitoringd.time, 'monotonic_ns', lambda: now)

    def step(*, main_on=False, enabled=False, gas=True, steering=False, actual_lateral=False, aol_replay=True, proof='fresh'):
      nonlocal tick, now, pending_tick
      tick += 1
      now = 10_000_000_000 + tick * int(DT_DMON * 1e9)
      monkeypatch.setenv('AOL_REPLAY_RUNTIME', '1' if aol_replay else '0')
      messages = {service: messaging.new_message(service, size=0 if service == 'aolSafetyWire' else None,
                                               valid=True, logMonoTime=now) for service in services}
      state = car.CarState(vEgo=20., gearShifter=car.CarState.GearShifter.drive,
                           gasPressed=gas, steeringPressed=steering)
      state.cruiseState.available = main_on
      state.cruiseState.enabled = enabled
      messages['carState'].carState = state
      messages['carControl'].carControl.latActive = bool(actual_lateral)
      messages['selfdriveState'].selfdriveState.enabled = enabled
      messages['modelV2'].modelV2.meta.disengagePredictions.brakeDisengageProbs = [0.]
      messages['extrinsicsCalibration'].extrinsicsCalibration.rpyCalib = [0., 0., 0.]
      driver = log.DriverStateV2.new_message()
      driver.leftDriverData.faceOrientation = [0., 0., 0.]
      driver.leftDriverData.facePosition = [0., 0.]
      driver.leftDriverData.faceOrientationStd = [0., 0., 0.]
      driver.leftDriverData.facePositionStd = [0., 0.]
      messages['driverStateV2'].driverStateV2 = driver
      proof_ns = now - 50_000_000 if proof == 'stale' else now + 50_000_000 if proof == 'future' else now
      messages['aolAxisState'].logMonoTime = messages['aolSafetyWire'].logMonoTime = proof_ns
      axis = messages['aolAxisState'].aolAxisState
      axis.qualified = axis.nativeAcknowledged = axis.desiredLateral = axis.lateralActive = True
      axis.sessionId = 'awareness-session'
      axis.sourceCarStateMonoTime = axis.observedMonoTime = proof_ns
      axis.validUntilMonoTime = proof_ns + 30_000_000
      # Sonata hybrid classic LONG + AOL + physical LDA profile; LONG remains off.
      messages['aolSafetyWire'].aolSafetyWire = encode_safety(SafetyState(
        1, proof != 'incompatible', proof_ns, proof_ns + 200_000_000, int(car.CarParams.SafetyModel.hyundai), 0xC06,
        True, False, True, False, 'awareness-panda', axis.sessionId))
      if proof == 'invalid':
        messages['aolAxisState'].valid = False
      if proof == 'missing':
        del messages['aolAxisState'], messages['aolSafetyWire']
      if actual_lateral is None:
        del messages['carControl']
      sm.update_msgs(now / 1e9, [message.as_reader() for message in messages.values()])
      if proof == 'fresh' and actual_lateral is not None:
        assert all(sm.valid.values()) and all(sm.seen.values()) and all(sm.alive.values())
      assert sm.all_checks(['carState', 'selfdriveState', 'modelV2', 'extrinsicsCalibration', 'driverStateV2'])
      assert monitoring_lateral_engaged(sm, now_ns=now) == (proof == 'fresh'), 'Only fresh, matching AOL proof qualifies'
      source_times = dict(sm.logMonoTime)
      pending_tick = True
      with pytest.raises(TickComplete):
        dmonitoringd.dmonitoringd_thread()
      assert len(sent) == tick
      assert sm.logMonoTime == source_times, 'Monitoring must not renew source timestamps'
      assert sent[-1].valid
      packet = sent[-1].driverMonitoringState
      assert packet.activePolicy == MonitoringPolicy.wheeltouch
      assert not packet.alwaysOn
      return packet

    yield dm, step


@pytest.mark.parametrize('proof,aol_replay', [
  ('intermittent', True),
  ('invalid', True),
  ('incompatible', True),
  ('future', True),
  ('missing', False),
], ids=['stale_gaps', 'invalid_axis', 'incompatible_native', 'future_proof', 'aol_off'])
def test_actual_lateral_keeps_awareness_decaying_through_proof_gaps(monitoring, proof, aol_replay):
  dm, step = monitoring
  awareness = []
  levels = []
  # MAIN and longitudinal remain off, gas held, no steering input or face.
  for tick in range(int(31. / DT_DMON)):
    tick_proof = ('fresh' if tick % 2 == 0 else 'stale') if proof == 'intermittent' else proof
    packet = step(actual_lateral=True, aol_replay=aol_replay, proof=tick_proof)
    awareness.append(dm.awareness)
    levels.append(packet.alertLevel)
    assert not packet.visionPolicyState.faceDetected
    assert not packet.wheeltouchPolicyState.driverInteracting

  assert 0 < awareness[0] < 1
  assert all(current <= previous for previous, current in zip(awareness, awareness[1:], strict=False))
  assert [levels[int(seconds / DT_DMON) - 1] for seconds in (2., 10., 20., 26.)] == [
    AlertLevel.none, AlertLevel.one, AlertLevel.two, AlertLevel.three]
  assert dm.awareness <= 0.
  assert packet.noResponseForceDecel and packet.noResponseCount == 1
  assert packet.lockout and packet.lockoutCount == 1


@pytest.mark.parametrize('proof,aol_replay', [
  ('missing', True), ('stale', True), ('future', True), ('incompatible', True), ('invalid', True), ('fresh', False),
])
@pytest.mark.parametrize('actual_lateral', [False, None], ids=['actual_false', 'car_control_unseen'])
def test_inactive_without_actual_lateral_or_qualified_requested_proof(monitoring, proof, aol_replay, actual_lateral):
  dm, step = monitoring
  for _ in range(int(2. / DT_DMON)):
    packet = step(actual_lateral=actual_lateral, aol_replay=aol_replay, proof=proof)
    assert dm.awareness == 1.
    assert packet.wheeltouchPolicyState.awarenessPercent == 100
    assert packet.alertLevel == AlertLevel.none
    assert not packet.lockout and not packet.noResponseForceDecel


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
@pytest.mark.parametrize('actual_lateral,proof', [(False, 'fresh'), (True, 'stale')], ids=['fresh_proof', 'actual_lateral'])
def test_real_interaction_recovers_positive_awareness(monitoring, main_on, enabled, gas, steering, actual_lateral, proof):
  _, step = monitoring
  for _ in range(int(20. / DT_DMON)):
    packet = step(main_on=main_on, enabled=enabled, gas=False, actual_lateral=actual_lateral, proof=proof)
  assert packet.alertLevel == AlertLevel.two
  assert 0 < packet.wheeltouchPolicyState.awarenessPercent < 100

  for _ in range(int(31. / DT_DMON)):
    packet = step(main_on=main_on, enabled=enabled, gas=gas, steering=steering, actual_lateral=actual_lateral, proof=proof)
    assert packet.wheeltouchPolicyState.awarenessPercent == 100
    assert packet.alertLevel == AlertLevel.none
    assert not packet.lockout and not packet.noResponseForceDecel


@pytest.mark.parametrize('main_on,enabled,gas,steering', [
  (True, True, True, False),
  (False, False, False, True),
], ids=['ordinary_engaged_gas', 'main_off_steering'])
@pytest.mark.parametrize('actual_lateral,proof', [(False, 'fresh'), (True, 'stale')], ids=['fresh_proof', 'actual_lateral'])
def test_interaction_cannot_clear_terminal_awareness(monitoring, main_on, enabled, gas, steering, actual_lateral, proof):
  _, step = monitoring
  for _ in range(int(26. / DT_DMON)):
    packet = step(main_on=main_on, enabled=enabled, gas=False, actual_lateral=actual_lateral, proof=proof)
  assert packet.alertLevel == AlertLevel.three

  for _ in range(int(5. / DT_DMON)):
    packet = step(main_on=main_on, enabled=enabled, gas=gas, steering=steering, actual_lateral=actual_lateral, proof=proof)
    assert packet.alertLevel == AlertLevel.three
    assert packet.wheeltouchPolicyState.awarenessPercent == 0
  assert packet.noResponseForceDecel and packet.noResponseCount == 1
  assert packet.lockout and packet.lockoutCount == 1


def test_ordinary_enabled_monitoring_and_gas_recovery_without_lateral_proof(monitoring):
  dm, step = monitoring
  for _ in range(int(20. / DT_DMON)):
    packet = step(main_on=True, enabled=True, gas=False, proof='missing', aol_replay=False)
  assert 0 < dm.awareness < 1.
  assert packet.alertLevel == AlertLevel.two

  packet = step(main_on=True, enabled=True, proof='missing', aol_replay=False)
  assert dm.awareness == 1.
  assert packet.alertLevel == AlertLevel.none
  assert not packet.lockout and not packet.noResponseForceDecel
