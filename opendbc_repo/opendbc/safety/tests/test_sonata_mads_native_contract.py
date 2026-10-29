"""Existing Sonata HEV native authority underneath the host's software MAIN policy.

0xC06 / alternativeExperience=32 already selects classic OP-long, HEV gas and
physical LDA authorization. TCS13 ACCEnable is hardware health, not the host's
software MAIN latch. Positive acknowledgments carry actual native permissions;
controls_allowed is never forced. This is not on-car qualification.
"""

from dataclasses import replace

import pytest

from opendbc.car.structs import CarParams
from opendbc.safety.tests.common import CANPackerSafety
from opendbc.safety.tests.libsafety import libsafety_py
from opendbc.safety.tests.test_hyundai import checksum


SONATA_HEV_LDA_LONG = 0xC06
LDA_SOURCES = (('BCM_PO_11', 'LDA_BTN'), ('CLU13', 'CF_Clu_LdwsLkasSW'))


class SonataNative:
  def __init__(self, safety, source=LDA_SOURCES[0], experience=32):
    self.safety = safety
    self.packer = CANPackerSafety('hyundai_can_generated')
    self.source, self.signal = source
    self.now = 1_000_000
    self.counter = 0
    safety.init_tests()
    safety.set_alternative_experience(experience)
    assert safety.set_safety_hooks(CarParams.SafetyModel.hyundai, SONATA_HEV_LDA_LONG) == 0
    safety.set_timer(self.now)
    safety.set_aol_test_heartbeat(True)

  def rx(self, name, values, *, bus=0, integrity=False):
    return self.safety.safety_rx_hook(self.packer.make_can_msg_safety(
      name, bus, values, fix_checksum=checksum if integrity else None))

  def frame(self, *, main=False, button=0, lda=False, healthy=True, brake=False,
            omit=None, tcs_bus=0):
    """Deliver actual bus-0 physical sources with valid counters/checksums."""
    self.now += 10_000
    self.safety.set_timer(self.now)
    count = self.counter
    self.counter += 1
    frames = (
      ('E_EMS11', {'Accel_Pedal_Pos': 0}, False),
      ('WHL_SPD11', {'WHL_SPD_AliveCounter_LSB': count % 4,
                     'WHL_SPD_AliveCounter_MSB': (count // 4) % 4,
                     'WHL_SPD_FL': 40, 'WHL_SPD_FR': 40,
                     'WHL_SPD_RL': 40, 'WHL_SPD_RR': 40}, True),
      ('TCS13', {'AliveCounterTCS': count % 8, 'ACCEnable': 0 if healthy else 1,
                 'DriverOverride': 2 if brake else 0}, True),
      ('MDPS12', {'CR_Mdps_StrColTq': 0}, False),
      ('CLU11', {'CF_Clu_AliveCnt1': count % 16,
                 'CF_Clu_CruiseSwMain': int(main), 'CF_Clu_CruiseSwState': button}, False),
      (self.source, {self.signal: int(lda)}, False),
    )
    for name, values, integrity in frames:
      if name != omit:
        assert self.rx(name, values, integrity=integrity,
                       bus=tcs_bus if name == 'TCS13' else 0), name

  def warm(self, **kwargs):
    for _ in range(6):
      self.frame(**kwargs)
    assert self.safety.safety_config_valid()
    assert self.safety.aol_rx_healthy()

  def request(self, mask):
    self.safety.aol_set_host_request(mask)
    return self.safety.aol_get_permission_mask()

  def steer(self, torque=1, request=True, bus=0):
    return self.safety.safety_tx_hook(self.packer.make_can_msg_safety('LKAS11', bus, {
      'CR_Lkas_StrToqReq': torque, 'CF_Lkas_ActToi': int(request)}))

  def accel(self, value=.1, **extra):
    return self.safety.safety_tx_hook(self.packer.make_can_msg_safety('SCC12', 0, {
      'aReqRaw': value, 'aReqValue': value, **extra}))

  def arm_lfa(self):
    assert self.request(0) == 0
    self.frame(lda=False)
    self.frame(lda=True)
    assert not self.safety.get_controls_allowed()
    assert self.request(1) == 1


@pytest.fixture
def native():
  safety = libsafety_py.libsafety
  if safety.set_safety_hooks(CarParams.SafetyModel.allOutput, 0) != 0:
    pytest.skip('Existing classic OP-long is DEBUG-only')
  yield safety
  safety.set_alternative_experience(0)


@pytest.mark.parametrize('source', LDA_SOURCES)
def test_main_off_fresh_lfa_grants_only_lateral_with_existing_word(native, source):
  car = SonataNative(native, source)
  car.warm()  # No MAIN press: host software MAIN would still be false.
  assert car.request(3) == 0
  assert not car.steer()
  assert not car.accel()
  car.arm_lfa()
  assert car.request(3) == 1
  assert car.steer()
  assert not car.accel()
  assert car.accel(0)


@pytest.mark.parametrize('source', LDA_SOURCES)
def test_held_wrong_bus_and_transmitted_lfa_do_not_create_authority(native, source):
  car = SonataNative(native, source)
  car.warm(lda=True)
  assert car.request(1) == 0
  assert car.request(0) == 0
  car.frame(lda=False)
  assert car.rx(car.source, {car.signal: 1}, bus=1)
  assert car.request(1) == 0
  assert car.request(0) == 0
  assert not native.safety_tx_hook(car.packer.make_can_msg_safety(car.source, 0, {car.signal: 1}))
  assert car.request(1) == 0
  car.arm_lfa()
  assert car.steer()


def test_main_on_while_lfa_active_and_main_off_then_lfa_need_no_new_profile(native):
  car = SonataNative(native)
  car.warm()
  car.arm_lfa()
  # The host toggles software MAIN from these physical press/release pairs.
  # An existing lateral claim remains valid; MAIN does not grant longitudinal.
  for main in (True, False, True, False):
    car.frame(main=main)
    assert car.request(3) == 1
    assert not native.get_controls_allowed()
    assert car.steer()
    assert not car.accel()
  # Host withdrawal denies TX even though this native session retains its token.
  assert car.request(0) == 0
  assert not car.steer()
  car.arm_lfa()
  assert car.steer()


def test_unclaimed_lfa_expires_while_physical_health_stays_current(native):
  car = SonataNative(native)
  car.warm()
  assert car.request(0) == 0
  car.frame(lda=True)
  # Keep every physical source current; the unclaimed gesture alone expires.
  for _ in range(40):
    car.frame(lda=True)
    assert car.request(0) == 0
  assert native.aol_rx_healthy()
  assert car.request(1) == 0
  assert not car.steer()
  car.arm_lfa()
  assert car.steer()


@pytest.mark.parametrize('button', [1, 2])  # RES / SET
def test_longitudinal_requires_physical_release_and_host_request(native, button):
  car = SonataNative(native)
  car.warm()
  car.arm_lfa()
  car.frame(main=True)
  car.frame()
  car.frame(button=button)
  assert not native.get_controls_allowed()
  assert car.request(3) == 1
  assert not car.accel()
  car.frame()
  assert native.get_controls_allowed()
  assert car.request(1) == 1
  assert not car.accel(), 'Physical authorization alone cannot widen the host request'
  assert car.request(3) == 3
  assert car.accel()
  # Classic HYUNDAI_MAIN_NONE does not track software MAIN. The host must
  # withdraw the longitudinal request when its software MAIN switches off.
  car.frame(main=True)
  car.frame()
  assert native.get_controls_allowed()
  assert car.request(1) == 1
  assert not car.accel()
  assert car.steer()


@pytest.mark.parametrize('withdrawal', ['cancel', 'brake'])
@pytest.mark.parametrize('button', [1, 2])
def test_cancel_or_brake_retains_lateral_without_automatic_long_rearm(native, withdrawal, button):
  car = SonataNative(native)
  car.warm()
  car.arm_lfa()
  car.frame(button=button)
  car.frame()
  assert car.request(3) == 3
  assert car.accel()
  car.frame(button=4 if withdrawal == 'cancel' else 0, brake=withdrawal == 'brake')
  assert not native.get_controls_allowed()
  assert car.request(3) == 1
  assert car.steer()
  assert not car.accel()
  car.frame()
  assert not native.get_controls_allowed()
  assert car.request(3) == 1
  assert not car.accel()
  car.frame(button=button)
  assert not car.accel()
  car.frame()
  assert native.get_controls_allowed()
  assert car.request(3) == 3
  assert car.accel()


@pytest.mark.parametrize('cause', ['tcs-fault', 'tcs-stale', 'wrong-bus-tcs', 'clu-stale',
                                 'bad-tcs-checksum', 'request-stale', 'heartbeat', 'relay'])
def test_lateral_fault_or_lease_loss_revokes_and_recovery_needs_fresh_gesture(native, cause):
  car = SonataNative(native)
  car.warm()
  car.arm_lfa()
  assert car.steer()
  if cause == 'tcs-fault':
    car.frame(healthy=False, lda=True)
  elif cause in ('tcs-stale', 'wrong-bus-tcs', 'clu-stale', 'request-stale'):
    for _ in range(40):
      car.frame(lda=True, omit={'tcs-stale': 'TCS13', 'clu-stale': 'CLU11'}.get(cause),
                tcs_bus=1 if cause == 'wrong-bus-tcs' else 0)
      if cause != 'request-stale':
        car.request(1)
  elif cause == 'bad-tcs-checksum':
    packet = car.packer.make_can_msg_safety('TCS13', 0, {
      'AliveCounterTCS': car.counter % 8}, fix_checksum=checksum)
    packet[0].data[0] ^= 1
    assert not native.safety_rx_hook(packet)
  elif cause == 'heartbeat':
    native.set_aol_test_heartbeat(False)
  else:
    native.set_relay_malfunction(True)
  assert car.request(1) == 0
  assert not car.steer()
  assert not car.accel()
  native.set_aol_test_heartbeat(True)
  native.set_relay_malfunction(False)
  # Recover physical health while the source stays held. Health/display state
  # cannot reconstruct a physical authorization token lost with the session.
  for _ in range(6):
    car.frame(lda=True)
    assert car.request(1) == 0
  car.arm_lfa()
  assert car.steer()
  assert not car.accel()


def test_existing_torque_acceleration_and_aeb_limits_still_apply(native):
  car = SonataNative(native)
  car.warm()
  car.arm_lfa()
  assert not car.steer(4), 'Existing torque rate-up limit is 3 per frame'
  assert not car.steer(385), 'Existing absolute torque limit is 384'
  assert not car.steer(1, request=False)
  assert not car.steer(1, bus=1)
  assert car.steer(1)
  # Real measured driver torque, rather than an injected controls_allowed flag.
  for _ in range(6):
    assert car.rx('MDPS12', {'CR_Mdps_StrColTq': -1000})
  assert not car.steer(3)
  car.frame(button=2)
  car.frame()
  assert car.request(3) == 3
  for value in (-3.5, 2.):
    assert car.accel(value)
  for value in (-3.51, 2.01):
    assert not car.accel(value)
  for fields in ({'AEB_CmdAct': 1}, {'AEB_StopReq': 1}, {'CR_VSM_DecCmd': 1}):
    assert not car.accel(0, **fields)


@pytest.mark.parametrize('experience', [0, 1, 33])
def test_existing_exact_experience_boundary_is_unchanged(native, experience):
  car = SonataNative(native, experience=experience)
  assert not native.safety_config_valid()
  assert car.request(3) == 0
  assert not car.steer(0, request=False)
  assert not car.accel(0)


def test_existing_release_build_denies_marked_classic_long():
  release = libsafety_py.ffi.dlopen(libsafety_py._build_libsafety(release=True))
  car = SonataNative(release)
  assert not release.safety_config_valid()
  assert car.request(3) == 0
  assert not car.steer(0, request=False)
  assert not car.accel(0)


@pytest.mark.parametrize('loss', ['missing', 'invalid', 'expired', 'wrong-session', 'wrong-word', 'heartbeat', 'native-denial'])
def test_host_main_off_lateral_requires_current_real_native_ack(native, loss):
  from opendbc.car import gen_empty_fingerprint, structs
  from opendbc.car.hyundai.interface import CarInterface
  from opendbc.car.hyundai.values import CAR
  from openpilot.starpilot.aol.runtime import current_native, decide_axes
  from openpilot.starpilot.aol.wire import IntentState, PandaSlot, SAFETY_SERVICE, SafetyState, encode_safety
  from openpilot.starpilot.car.hyundai.aol import policy_for

  fingerprint = gen_empty_fingerprint()
  fingerprint[0][0x391] = 8
  cp = CarInterface.get_params(CAR.HYUNDAI_SONATA_HYBRID, fingerprint, [], True, False, False)
  policy = policy_for(cp)
  cp.safetyConfigs[0].safetyParam |= policy.safety_param_addition
  cp.alternativeExperience |= policy.alternative_experience_addition
  assert cp.safetyConfigs[0].safetyParam == SONATA_HEV_LDA_LONG
  assert cp.alternativeExperience == 32
  car = SonataNative(native)
  car.warm()
  car.arm_lfa()
  now = car.now * 1000
  actual_request = native.aol_get_request_mask()
  actual_permission = native.aol_get_permission_mask()
  assert actual_request == actual_permission == 1
  slot = PandaSlot(0, 'sonata-test-panda', int(cp.safetyConfigs[0].safetyModel.raw),
                   SONATA_HEV_LDA_LONG, 32, bool(native.get_controls_allowed()))
  state = SafetyState(1, True, now, now + 30_000_000, slot.safetyModel, slot.safetyParam,
                      bool(actual_permission & 1), bool(actual_permission & 2),
                      bool(actual_request & 1), bool(actual_request & 2),
                      slot.hardwareSerial, 'sonata-test-session', now, (slot,))

  class Subscriber(dict):
    valid = {SAFETY_SERVICE: True}
    alive = {SAFETY_SERVICE: True}
    seen = {SAFETY_SERVICE: True}
    logMonoTime = {SAFETY_SERVICE: now}

  subscriber = Subscriber({SAFETY_SERVICE: encode_safety(state)})
  intent = IntentState('sonata-test-card', 1, now, now, now + 30_000_000, True, False, False, True)
  cs = structs.CarState.new_message(canValid=True, canTimeout=False, gearShifter='drive', vEgo=10.)
  cs.cruiseState.available = False  # Software MAIN off cannot authorize either axis.

  def decision(*, dm_lockout=False):
    acknowledged = current_native(subscriber, cp, now_ns=now, axis_session_id='sonata-test-session')
    return decide_axes(standard_lateral=False, standard_longitudinal=False,
                       intent=intent, native=acknowledged, car_state=cs,
                       initialized=True, model_ready=True, no_entry=False,
                       immediate_disable=False, dm_lockout=dm_lockout, pause_brake_mps=0.)

  assert decision().lateral_active
  assert not decision().longitudinal_active
  assert not decision(dm_lockout=True).lateral_active
  if loss == 'missing':
    subscriber.seen = {SAFETY_SERVICE: False}
  elif loss == 'invalid':
    subscriber.valid = {SAFETY_SERVICE: False}
  elif loss == 'expired':
    subscriber[SAFETY_SERVICE] = encode_safety(replace(state, validUntilMonoTime=now - 1,
                                                    observedMonoTime=now - 30_000_001))
    subscriber.logMonoTime = {SAFETY_SERVICE: now - 30_000_001}
  elif loss == 'wrong-session':
    subscriber[SAFETY_SERVICE] = encode_safety(replace(state, axisSessionId='prior-session'))
  elif loss == 'wrong-word':
    subscriber[SAFETY_SERVICE] = encode_safety(replace(state, safetyParam=0xC04))
  elif loss == 'heartbeat':
    subscriber[SAFETY_SERVICE] = encode_safety(replace(state, pandaInventory=(replace(slot, heartbeatLost=True),)))
  else:
    native.set_aol_test_heartbeat(False)
    assert native.aol_get_permission_mask() == 0
    subscriber[SAFETY_SERVICE] = encode_safety(replace(state, lateralAllowed=False, longitudinalAllowed=False))
  result = decision()
  assert result.desired_lateral, 'Host intent remains; only native acknowledgment is lost'
  assert not result.lateral_active
  assert not result.longitudinal_active
