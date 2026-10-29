"""Existing Sonata HEV native authority underneath the host's software MAIN policy.

0xC06 / alternativeExperience=32 already selects classic OP-long, HEV gas and
physical LDA authorization. TCS13 ACCEnable is hardware health, not the host's
software MAIN latch. BCM 0x391 liveness is required even for a CLU13 gesture.
Positive acknowledgments carry actual native permissions;
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
    self.next_safety_tick = 2_000_000
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
      # C06's verified required source is BCM. CLU13 can supply a physical
      # gesture while BCM stays neutral/live; it cannot replace BCM health.
      ('BCM_PO_11', {'LDA_BTN': int(lda and self.source == 'BCM_PO_11')}, False),
    )
    if self.source != 'BCM_PO_11':
      frames += ((self.source, {self.signal: int(lda)}, False),)
    for name, values, integrity in frames:
      if name != omit:
        assert self.rx(name, values, integrity=integrity,
                       bus=tcs_bus if name == 'TCS13' else 0), name
    if self.now >= self.next_safety_tick:
      self.safety.safety_tick()
      self.next_safety_tick += 1_000_000

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


@pytest.mark.parametrize('observation', ['permission', 'lkas-tx'])
def test_claimed_bcm_source_loss_revokes_with_other_inputs_and_requests_fresh(native, observation):
  car = SonataNative(native)
  car.warm()
  car.arm_lfa()
  assert car.steer()
  # BCM stops after a real neutral->press. All other native required sources,
  # heartbeat and host requests stay fresh, with the regular 1 Hz safety tick.
  for _ in range(120):
    car.frame(omit='BCM_PO_11')
    native.set_aol_test_heartbeat(True)
    native.aol_set_host_request(1)
  if observation == 'permission':
    assert native.aol_get_permission_mask() == 0
  else:
    assert not car.steer(), 'Real LKAS TX must reject a claimed token after BCM source loss'


def test_bcm_health_allows_200ms_then_denies_before_the_next_periodic_tick(native):
  car = SonataNative(native)
  car.warm()
  car.arm_lfa()
  for _ in range(20):
    car.frame(omit='BCM_PO_11')
    assert car.request(1) == 1
  assert car.steer()
  car.frame(omit='BCM_PO_11')
  assert car.now < car.next_safety_tick
  assert car.request(1) == 0
  assert not car.steer()


def test_verified_bcm_at_50hz_keeps_lateral_health_current(native):
  car = SonataNative(native)
  car.warm()
  car.arm_lfa()
  for tick in range(100):
    car.frame(lda=True, omit='BCM_PO_11' if tick % 2 == 0 else None)
    assert car.request(1) == 1
    assert car.steer()


@pytest.mark.parametrize('combined', [False, True])
@pytest.mark.parametrize('returned_held', [False, True])
@pytest.mark.parametrize('poll_permission', [False, True])
def test_first_returned_bcm_cannot_hide_gap_or_revive_either_axis(native, combined, returned_held, poll_permission):
  car = SonataNative(native)
  car.warm()
  car.arm_lfa()
  mask = 3 if combined else 1
  if combined:
    car.frame(button=2, lda=True)
    car.frame(lda=True)
  assert car.request(mask) == mask
  assert bool(native.get_controls_allowed()) == combined
  # All other physical sources/requests continue through the exact 200 ms
  # boundary. The first frame after it is BCM itself, before any permission
  # poll or periodic tick can detect the gap. Its new RX timestamp must not
  # conceal the old source epoch, including prior SET-authorized controls.
  for _ in range(20):
    car.frame(omit='BCM_PO_11')
    native.aol_set_host_request(mask)
    if poll_permission:
      assert native.aol_get_permission_mask() == mask
  car.now += 1_000
  native.set_timer(car.now)
  assert car.rx('BCM_PO_11', {'LDA_BTN': int(returned_held)})
  assert not native.get_controls_allowed()
  assert car.request(mask) == 0
  assert not car.steer()
  assert not car.accel()
  # A held return and a neutral return are both health evidence, not an arm.
  for _ in range(6):
    car.frame(lda=returned_held)
    assert car.request(mask) == 0
  car.arm_lfa()
  assert car.steer()
  assert not car.accel()
  if combined:
    car.frame(button=2)
    assert not native.get_controls_allowed()
    car.frame()
    assert car.request(3) == 3
    assert car.accel()


@pytest.mark.parametrize('prior_reset', ['heartbeat', 'host-lease'])
def test_bcm_clock_survives_a_prior_host_authorization_reset(native, prior_reset):
  car = SonataNative(native)
  car.warm()
  car.arm_lfa()
  car.frame(button=2, lda=True)
  car.frame(lda=True)
  assert car.request(3) == 3
  if prior_reset == 'heartbeat':
    native.set_aol_test_heartbeat(False)
  else:
    # Physical BCM remains current while only the old host lease expires.
    for _ in range(31):
      car.frame(lda=True)
  assert native.aol_get_request_mask() == 0
  native.set_aol_test_heartbeat(True)
  assert native.get_controls_allowed(), 'Exercise the existing retained SET proof, never force it'
  # The independent physical BCM clock must survive clearing the host's
  # token/neutral history, or the first returning frame can hide this gap.
  for _ in range(20):
    car.frame(omit='BCM_PO_11')
    native.aol_set_host_request(3)
  car.now += 1_000
  native.set_timer(car.now)
  assert car.rx('BCM_PO_11', {'LDA_BTN': 1})
  observed = (bool(native.get_controls_allowed()), car.request(3), bool(car.steer()), bool(car.accel()))
  assert observed == (False, 0, False, False), 'A host reset cannot erase the physical BCM receive clock'


@pytest.mark.parametrize('replacement', ['none', 'wrong-bus', 'short', 'long', 'clu-only'])
def test_other_shapes_or_clu_cannot_renew_required_bcm(native, replacement):
  car = SonataNative(native)
  car.warm()
  car.arm_lfa()
  for _ in range(21):
    car.frame(omit='BCM_PO_11')
    if replacement == 'wrong-bus':
      assert car.rx('BCM_PO_11', {'LDA_BTN': 1}, bus=1)
    elif replacement in ('short', 'long'):
      length = 7 if replacement == 'short' else 12
      packet = libsafety_py.make_CANPacket(0x391, 0, b'\x10' + bytes(length - 1))
      assert native.safety_rx_hook(packet)
    elif replacement == 'clu-only':
      assert car.rx('CLU13', {'CF_Clu_LdwsLkasSW': 1})
    native.aol_set_host_request(1)
  assert car.request(1) == 0
  assert not native.aol_rx_healthy()
  assert not car.steer()


def test_clu13_only_is_not_a_required_bcm_health_substitute(native):
  car = SonataNative(native, LDA_SOURCES[1])
  for _ in range(6):
    car.frame(omit='BCM_PO_11')
  assert car.request(0) == 0
  car.frame(lda=True, omit='BCM_PO_11')
  assert car.request(3) == 0
  assert not car.steer()
  assert not car.accel()
  # BCM returning neutral while CLU stays held does not manufacture a press.
  car.frame(lda=True)
  assert car.request(1) == 0
  car.arm_lfa()
  assert car.steer()


@pytest.mark.parametrize('button', [1, 2])
def test_enable_held_across_bcm_gap_requires_healthy_neutral_then_fresh_press(native, button):
  car = SonataNative(native)
  car.warm()
  car.arm_lfa()
  car.frame(button=button, lda=True)
  car.frame(lda=True)
  assert car.request(3) == 3
  # An enable press started before the source was lost cannot engage again
  # when released after BCM returns. Neither axis may reuse that session.
  for _ in range(21):
    car.frame(button=button, omit='BCM_PO_11')
    native.aol_set_host_request(3)
  assert not native.get_controls_allowed()
  car.frame(button=button, lda=True)
  assert car.request(3) == 0
  car.frame(lda=True)
  assert not native.get_controls_allowed()
  assert car.request(3) == 0
  car.frame(button=button, lda=True)
  assert not native.get_controls_allowed()
  car.frame(lda=True)
  assert car.request(3) == 3
  assert car.accel()


@pytest.mark.parametrize('button', [1, 2])
def test_set_resume_started_without_bcm_cannot_authorize_on_source_return(native, button):
  car = SonataNative(native)
  for _ in range(6):
    car.frame(button=button, omit='BCM_PO_11')
  assert car.request(3) == 0
  assert not native.get_controls_allowed()
  car.frame(button=button)
  car.frame()
  assert car.request(3) == 0
  assert not native.get_controls_allowed()
  car.frame(button=button)
  car.frame()
  assert car.request(3) == 3
  assert car.accel()


@pytest.mark.parametrize('word', [0x406, 0xC04, 0xC05, 0xC46])
def test_other_classic_long_words_keep_their_existing_physical_rx_contract(native, word):
  from opendbc.safety.tests import test_hyundai_classic_long_aol as fixture

  car = fixture.TestHyundaiClassicLongAol(methodName='runTest')
  car.setUp()
  try:
    car.reset(word)
    for _ in range(6):
      car.feed(word)
    car.feed(word, button=2)
    car.feed(word)
    assert native.get_controls_allowed()
    assert car.request(3) == 3
    assert native.safety_tx_hook(car.packer.make_can_msg_safety('SCC12', 0, {'aReqRaw': .1, 'aReqValue': .1}))
  finally:
    car.tearDown()


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
