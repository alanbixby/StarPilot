import pytest

from opendbc.can import CANParser
from opendbc.car import Bus, gen_empty_fingerprint, structs
from opendbc.car.hyundai.classic_long_aol import aol_word
from opendbc.car.hyundai.interface import CarInterface
from opendbc.car.hyundai.lateral_display import LateralDisplay
from opendbc.car.hyundai.values import CAR, DBC
from opendbc.safety.tests import test_hyundai_classic_long_aol as native_fixture
from opendbc.safety.tests.libsafety.libsafety_py import make_CANPacket

VisualAlert = structs.CarControl.HUDControl.VisualAlert
MADS = {'valid': True, 'available': True, 'enabled': True}


def controller(identity=CAR.HYUNDAI_SONATA_HYBRID, *, lfa=True):
  fp = gen_empty_fingerprint()
  fp[0][0x391] = 8
  if lfa:
    fp[2][0x485] = 4
  cp = CarInterface.get_params(identity, fp, [], True, False, False)
  cp.safetyConfigs[0].safetyParam = aol_word(cp)
  cp.alternativeExperience = 32
  ci = CarInterface(cp)
  ci.update([])
  return ci


def parser(identity=CAR.HYUNDAI_SONATA_HYBRID):
  return CANParser(DBC[identity][Bus.pt], [('LKAS11', 0), ('LFAHDA_MFC', 0)], 0)


def step(ci, can, *, enabled=False, lateral=False, display=None, alert=VisualAlert.none):
  cc = structs.CarControl(enabled=enabled, latActive=lateral, hudControl={'visualAlert': alert, 'leadVisible': True,
                          'leftLaneVisible': True, 'rightLaneVisible': True})
  if display is not None:
    ci.CC.lateral_display = LateralDisplay(**display)  # as Card sets it before each apply
  stamp = 10_000_000_000 + ci.CC.frame * 10_000_000
  actuators, packets = ci.CC.update(cc.as_reader(), ci.CS, stamp)
  can.update((stamp, packets))
  return actuators, packets


def icons(can):
  return can.vl['LKAS11']['CF_Lkas_FcwOpt_USM'], can.vl['LFAHDA_MFC']['LFA_Icon_State']


# (CC.enabled, CC.latActive, retained intent) -> deployed sunnypilot (LKAS FcwOpt_USM, LFA wheel) after any blink
@pytest.mark.parametrize('enabled,lateral,intent,expected', [
  (False, False, False, (1, 0)),  # disabled
  (False, True, True, (2, 2)),    # lateral only, or CANCEL/brake with steering retained
  (True, True, True, (2, 2)),     # joined
  (True, False, False, (1, 0)),   # lane button turned steering off, speed control continues
  (False, False, True, (1, 1)),   # steering output paused with intent retained
  (True, False, True, (1, 1)),    # same pause with speed control engaged
])
def test_steady_mads_icons_follow_final_lateral_and_retained_intent(enabled, lateral, intent, expected):
  ci, can = controller(), parser()
  for _ in range(10):
    step(ci, can, enabled=enabled, lateral=lateral, display={**MADS, 'enabled': intent})
  assert icons(can) == expected
  # SysState keeps following CC.enabled, as in deployed sunnypilot.
  assert can.vl['LKAS11']['CF_Lkas_LdwsSysState'] == (3 if enabled else 4)


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('intent,after', [(False, (1, 0)), (True, (1, 1))])
def test_lateral_fall_blinks_one_second_then_settles(enabled, intent, after):
  ci, can = controller(), parser()
  for frame in range(101):
    lateral = frame == 0
    step(ci, can, enabled=enabled, lateral=lateral, display={**MADS, 'enabled': lateral or intent})
    expected = (2, 2) if frame == 0 else (3, 3) if frame < 100 else after
    assert can.vl['LKAS11']['CF_Lkas_FcwOpt_USM'] == expected[0], frame
    if frame % 5 == 0:
      assert can.vl['LFAHDA_MFC']['LFA_Icon_State'] == expected[1], frame


def test_reengaging_during_blink_restores_active_and_rearms_next_blink():
  ci, can = controller(), parser()
  for lateral in [True] + [False] * 50 + [True] * 10 + [False] * 5:
    step(ci, can, lateral=lateral, display=MADS)
  assert icons(can) == (3, 3)
  for _ in range(100):
    step(ci, can, display=MADS)
  assert icons(can) == (1, 1)


# Invalid facts only withhold the armed/paused wheel. Final CC.latActive still owns active and its falling-edge blink,
# and cruise state never substitutes for either.
@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('claimed', [False, True])
def test_invalid_display_source_falls_back_to_final_lateral_only(enabled, claimed):
  stale = {'valid': False, 'available': True, 'enabled': claimed}
  ci, can = controller(), parser()
  for _ in range(10):
    step(ci, can, enabled=enabled, display=stale)
  assert icons(can) == (1, 0)
  for _ in range(10):
    step(ci, can, enabled=enabled, lateral=True, display=stale)
  assert icons(can) == (2, 2)


@pytest.mark.parametrize('enabled', [False, True])
def test_lateral_fall_into_stale_source_blinks_then_shows_no_wheel(enabled):
  ci, can = controller(), parser()
  for _ in range(10):
    step(ci, can, enabled=enabled, lateral=True, display=MADS)
  stale = {'valid': False, 'available': True, 'enabled': True}
  for frame in range(100):
    step(ci, can, enabled=enabled, display=stale)
    expected = (3, 3) if frame < 99 else (1, 0)
    assert can.vl['LKAS11']['CF_Lkas_FcwOpt_USM'] == expected[0], frame
    if ci.CC.frame % 5 == 1:
      assert can.vl['LFAHDA_MFC']['LFA_Icon_State'] == expected[1], frame


def test_missed_card_refresh_cannot_keep_showing_retained_intent():
  ci, can = controller(), parser()
  for _ in range(10):
    step(ci, can, display=MADS)
  assert icons(can) == (1, 1)
  for _ in range(5):
    step(ci, can)  # Card stopped refreshing; the profile stays selected for this drive
  assert icons(can) == (1, 0)
  for _ in range(5):
    step(ci, can, enabled=True)
  assert icons(can) == (1, 0)  # never falls back to cruise-driven icons
  for _ in range(5):
    step(ci, can, lateral=True)
  assert icons(can) == (2, 2)


def test_unavailable_profile_is_byte_identical_to_existing_controller():
  unavailable, existing = controller(), controller()
  existing.CC.mads_dash_icons = None  # the pre-MADS display path
  new_can, old_can = parser(), parser()
  sequence = [(False, False)] * 5 + [(False, True)] * 20 + [(True, True)] * 20 + [(True, False)] * 120 + \
             [(False, True)] * 10 + [(False, False)] * 120
  for enabled, lateral in sequence:
    new = step(unavailable, new_can, enabled=enabled, lateral=lateral,
               display={'valid': True, 'available': False, 'enabled': True})
    old = step(existing, old_can, enabled=enabled, lateral=lateral)
    assert new[0].to_dict() == old[0].to_dict()
    assert new[1] == old[1]


@pytest.mark.parametrize('identity', [CAR.HYUNDAI_SONATA, CAR.HYUNDAI_ELANTRA_2021])
def test_other_classic_cars_ignore_display_facts(identity):
  with_facts, without = controller(identity), controller(identity)
  with_can, without_can = parser(identity), parser(identity)
  for lateral in [True] * 10 + [False] * 120:
    _, new_packets = step(with_facts, with_can, lateral=lateral, display=MADS)
    _, old_packets = step(without, without_can, lateral=lateral)
    assert new_packets == old_packets


def test_without_detected_lfa_message_only_lkas_icon_changes():
  ci, can = controller(lfa=False), parser()
  for _ in range(10):
    _, packets = step(ci, can, lateral=True, display=MADS)
    assert all(address != 0x485 for address, _, _ in packets)
  assert can.vl['LKAS11']['CF_Lkas_FcwOpt_USM'] == 2


@pytest.mark.parametrize('enabled,lateral,intent', [(False, False, False), (False, False, True), (False, True, True),
                                                    (True, False, False), (True, False, True), (True, True, True)])
@pytest.mark.parametrize('alert', [VisualAlert.none, VisualAlert.steerRequired, VisualAlert.fcw])
def test_only_lateral_icon_fields_and_lkas_checksum_change(enabled, lateral, intent, alert):
  mads, ordinary = controller(), controller()
  mads_can, ordinary_can = parser(), parser()
  for _ in range(25):
    new_actuators, new_packets = step(mads, mads_can, enabled=enabled, lateral=lateral, alert=alert,
                                      display={**MADS, 'enabled': intent})
    old_actuators, old_packets = step(ordinary, ordinary_can, enabled=enabled, lateral=lateral, alert=alert)
    assert new_actuators.to_dict() == old_actuators.to_dict()
    assert [(a, b) for a, _, b in new_packets] == [(a, b) for a, _, b in old_packets]
    for new, old in zip(new_packets, old_packets, strict=True):
      if new[0] not in (0x340, 0x485):
        assert new == old  # SCC/FCA acceleration, lead, AEB status, options and diagnostics
  for message, excluded in [('LKAS11', {'CF_Lkas_FcwOpt_USM', 'CF_Lkas_Chksum'}), ('LFAHDA_MFC', {'LFA_Icon_State'})]:
    assert {k: v for k, v in mads_can.vl[message].items() if k not in excluded} == {
      k: v for k, v in ordinary_can.vl[message].items() if k not in excluded}


def test_lateral_only_mads_icons_pass_native_lateral_only_permission():
  case = native_fixture.TestHyundaiClassicLongAol(methodName='runTest')
  case.setUp()
  try:
    ci, can = controller(), parser()
    assert ci.CP.safetyConfigs[0].safetyParam == 0xC06
    case.reset(0xC06)
    for _ in range(6):
      case.feed(0xC06)
    case.rx('BCM_PO_11', {'LDA_BTN': 0})
    case.rx('BCM_PO_11', {'LDA_BTN': 1})
    permission = case.request(3)
    assert permission == 1 and not case.safety.get_controls_allowed()
    for _ in range(5):
      actuators, packets = step(ci, can, lateral=bool(permission & 1), display=MADS)
      for address, data, bus in packets:
        assert case.safety.safety_tx_hook(make_CANPacket(address, bus, data))
    assert icons(can) == (2, 2)
    assert actuators.torque == 0 and actuators.accel == 0
  finally:
    case.tearDown()
