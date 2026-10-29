"""Factory HUD bytes across the real joined Sonata MADS Card display injection, controller and native TX."""
from opendbc.can import CANParser
from opendbc.car import Bus
from opendbc.car.hyundai import carcontroller
from opendbc.car.hyundai.values import CAR, DBC, HyundaiFlags
from opendbc.car.hyundai.interface import CarInterface
from openpilot.starpilot.aol.tests import test_classic_long_profiles as profiles
from openpilot.starpilot.aol.tests import test_sonata_mads_joined as joined
from openpilot.starpilot.aol.tests.hyundai_cpp_order import hkg_pandad_order as hkg_pandad_order


def params_with_lfa(identity, **kwargs):
  # The factory LFA camera frame on bus 2 lets the real detection select SEND_LFA.
  real = CarInterface.get_params

  def get_params(candidate, fingerprint, *args):
    fingerprint[2][0x485] = 4
    return real(candidate, fingerprint, *args)
  CarInterface.get_params = get_params
  try:
    cp = profiles.params(identity, **kwargs)
  finally:
    CarInterface.get_params = real
  assert cp.flags & HyundaiFlags.SEND_LFA
  return cp


def test_joined_mads_sequence_packs_sunnypilot_lateral_icons(monkeypatch, hkg_pandad_order):
  recorded = []
  original = carcontroller.CarController.update

  def update(self, CC, CS, now_nanos):
    # Record what Card actually injected for this apply; the test never assigns it.
    frame, display = self.frame, self.lateral_display
    actuators, packets = original(self, CC, CS, now_nanos)
    recorded.append((frame, CC.enabled, CC.latActive, display.available, display.valid, display.enabled, list(packets)))
    return actuators, packets
  monkeypatch.setattr(carcontroller.CarController, 'update', update)
  monkeypatch.setattr(joined, 'classic_params', params_with_lfa)

  joined.test_mads_buttons_join_parser_card_standard_state_controls_and_native(monkeypatch, hkg_pandad_order)

  hud = CANParser(DBC[CAR.HYUNDAI_SONATA_HYBRID][Bus.pt], [('LKAS11', 0), ('LFAHDA_MFC', 0)], 0)
  previous_lateral, fall_frame, seen, retained_pauses = False, None, set(), []
  for index, (frame, enabled, lateral, available, valid, intent, packets) in enumerate(recorded):
    assert available, index  # profile-selected drive: display mode never depends on freshness
    if lateral:
      fall_frame = None
    elif previous_lateral:
      fall_frame = frame
    previous_lateral = lateral
    blinking = fall_frame is not None and frame - fall_frame < 99
    lateral_icon = 2 if lateral else 3 if blinking else 0
    expected = (lateral_icon or 1, lateral_icon or int(valid and intent))
    addresses = {address for address, _, bus in packets if bus == 0}
    if 0x340 not in addresses:
      continue
    hud.update((10_000_000_000 + index * 10_000_000, packets))
    assert hud.vl['LKAS11']['CF_Lkas_FcwOpt_USM'] == expected[0], (index, frame, enabled, lateral, valid, intent)
    if 0x485 in addresses:
      assert hud.vl['LFAHDA_MFC']['LFA_Icon_State'] == expected[1], (index, frame, enabled, lateral, valid, intent)
      seen.add((enabled, lateral, expected))
      if not lateral and valid and intent:
        retained_pauses.append(expected)

  # The joined scenario must have exercised every distinct factory state.
  assert (False, True, (2, 2)) in seen       # lateral only, MAIN off or after CANCEL/brake
  assert (True, True, (2, 2)) in seen        # joined
  assert any(e == (3, 3) for _, _, e in seen)  # disengage blink
  # The scenario's brief EPS gate retains intent but ends within the disengage blink; the
  # steady white wheel after the blink is covered by the packed controller tests.
  assert retained_pauses and set(retained_pauses) <= {(3, 3), (1, 1)}
  assert (False, False, (1, 0)) in seen      # off
