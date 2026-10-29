"""ESCC active-control qualification tripwires for the classic Sonata Hybrid.

These tests record a present/unqualified guard, not a safe ESCC path. When
0x2AB is seen on the powertrain bus while fingerprinting and alpha long would
otherwise be selected, CarParams become dashcam/noOutput before any startup
owner exists: no radar-disable owner, no generic init disable and no LONG word.
There is no automatic stock-ACC fallback.

The guard is a positive-presence veto only. Without 0x2AB the ordinary LONG
path is unchanged and still disables the radar, so a missed or late interceptor
remains a detection limit. The inspected stock-longitudinal path (alpha long
off) is unchanged; its physical behavior with the interceptor is not qualified.

When an ESCC owner is implemented, these assertions are expected to fail and
must be reclassified against docs/research/escc-active-owner-spec.md, never
deleted to make a demonstration pass.
"""
import pytest

from opendbc.can import CANPacker
from opendbc.car import gen_empty_fingerprint, structs, uds
from opendbc.car.hyundai import blended_longitudinal, hyundaican, interface as hyundai_interface
from opendbc.car.hyundai.classic_long_aol import native_accepts, ordinary_word, qualified
from opendbc.car.hyundai.interface import CarInterface
from opendbc.car.hyundai.ordinary_classic_startup import OrdinaryClassicStartup
from opendbc.car.hyundai.values import CAR, HyundaiFlags

ESCC_ADDR = 0x2AB
RADAR_DISABLE = bytes([uds.SERVICE_TYPE.COMMUNICATION_CONTROL, 0x80 | uds.CONTROL_TYPE.DISABLE_RX_DISABLE_TX, uds.MESSAGE_TYPE.NORMAL])
NO_OUTPUT = structs.CarParams.SafetyModel.noOutput


def sonata_params(*, escc, alpha_long, release=False, bus=0, dlc=8, candidate=CAR.HYUNDAI_SONATA_HYBRID):
  fingerprint = gen_empty_fingerprint()
  fingerprint[0][0x391] = 8  # LDA button source
  fingerprint[0][0x38D] = 8  # FCA11, forwarded by the interceptor from the stock radar
  if escc:
    fingerprint[bus][ESCC_ADDR] = dlc
  return CarInterface.get_params(candidate, fingerprint, [], alpha_long, release, False)


def radar_disables(cp, monkeypatch):
  calls = []
  monkeypatch.setattr(hyundai_interface, 'disable_ecu', lambda recv, send, **kwargs: calls.append(kwargs))
  CarInterface.init(cp, lambda *args, **kwargs: [], lambda *args, **kwargs: None)
  return calls


def assert_guarded(cp):
  assert cp.dashcamOnly
  assert not cp.openpilotLongitudinalControl and cp.pcmCruise
  assert [c.safetyModel for c in cp.safetyConfigs] == [NO_OUTPUT]
  assert [int(c.safetyParam) for c in cp.safetyConfigs] == [0]
  assert cp.alphaLongitudinalAvailable, 'the unsupported request stays visible'
  assert not qualified(cp)


def test_escc_present_alpha_long_is_guarded_to_dashcam_no_output():
  plain, escc = sonata_params(escc=False, alpha_long=True), sonata_params(escc=True, alpha_long=True)
  assert_guarded(escc)
  # Without ESCC the ordinary LONG profile is unchanged.
  assert not plain.dashcamOnly and plain.openpilotLongitudinalControl and not plain.pcmCruise
  assert plain.flags & HyundaiFlags.USE_FCA
  assert qualified(plain) and plain.safetyConfigs[0].safetyParam == ordinary_word(plain) == 0x006


def test_escc_present_alpha_long_selects_no_startup_owner(monkeypatch):
  cp = sonata_params(escc=True, alpha_long=True)
  assert not CarInterface.startup_required(cp)
  fallthrough = []
  real_blended = blended_longitudinal.startup_owner

  def blended(cp, callbacks, *, requested):
    owner = real_blended(cp, callbacks, requested=requested)
    fallthrough.append((requested, owner))
    return owner
  monkeypatch.setattr(blended_longitudinal, 'startup_owner', blended)
  frames = []
  owner = CarInterface.startup_owner(cp, (lambda *a, **k: [], frames.extend), requested=True)
  assert owner is None
  assert fallthrough == [(True, None)], 'the guarded CP reaches the final blended fallthrough and still gets no owner'
  assert frames == []
  assert radar_disables(cp, monkeypatch) == []


def test_no_escc_ordinary_long_still_disables_the_radar_detection_limit(monkeypatch):
  # Absence of 0x2AB is not proof of absence: a missed or late interceptor
  # still gets the ordinary radar-disable owner and init disable.
  cp = sonata_params(escc=False, alpha_long=True)
  assert CarInterface.startup_required(cp)
  owner = CarInterface.startup_owner(cp, (lambda *a, **k: [], lambda frames: None), requested=True)
  assert isinstance(owner, OrdinaryClassicStartup)
  assert (owner.address, owner.bus) == (0x7D0, 0)
  calls = radar_disables(cp, monkeypatch)
  assert calls == [{'bus': 0, 'addr': 0x7D0, 'com_cont_req': RADAR_DISABLE}]


def test_ordinary_long_fca12_builder_still_disables_aeb():
  # Builder fact only. The guarded ESCC CP never reaches the controller; see
  # openpilot/starpilot/tests/test_escc_present_guard.py.
  cp = sonata_params(escc=False, alpha_long=True)
  packer = CANPacker('hyundai_can_generated')
  opts = {address: (data, bus) for address, data, bus in hyundaican.create_acc_opt(packer, cp)}
  # With ESCC the interceptor forwards the stock radar's FCA12; this host frame would compete with it.
  assert 0x483 in opts and opts[0x483][1] == 0
  assert packer.dbc.name_to_msg['FCA12'].address == 0x483
  fca_usm = opts[0x483][0][0] & 0x7  # FCA_USM 0|3, 1 = AEB disabled
  assert fca_usm == 1, 'FCA12 builder changed; reclassify the ESCC FCA pass-through gate'


@pytest.mark.parametrize('release', [False, True])
def test_stock_longitudinal_with_escc_is_unchanged_and_sends_no_radar_disable(release, monkeypatch):
  cp = sonata_params(escc=True, alpha_long=False, release=release)
  assert cp.to_dict() == sonata_params(escc=False, alpha_long=False, release=release).to_dict()
  assert not cp.dashcamOnly
  assert not cp.openpilotLongitudinalControl and cp.pcmCruise
  assert not CarInterface.startup_required(cp)
  assert CarInterface.startup_owner(cp, (lambda *a, **k: [], lambda frames: None), requested=False) is None
  assert radar_disables(cp, monkeypatch) == []
  assert not qualified(cp)


@pytest.mark.parametrize('dlc', [0, 1, 4, 8])
def test_guard_matches_escc_at_any_dlc_on_bus_0(dlc):
  assert_guarded(sonata_params(escc=True, alpha_long=True, dlc=dlc))


@pytest.mark.parametrize('bus', [1, 2])
def test_guard_ignores_0x2ab_on_other_buses(bus):
  cp = sonata_params(escc=True, alpha_long=True, bus=bus)
  assert cp.to_dict() == sonata_params(escc=False, alpha_long=True).to_dict()
  assert not cp.dashcamOnly and cp.openpilotLongitudinalControl


@pytest.mark.parametrize('candidate', [CAR.HYUNDAI_SONATA, CAR.HYUNDAI_ELANTRA_HEV_2021, CAR.HYUNDAI_PALISADE])
@pytest.mark.parametrize('alpha_long', [False, True])
def test_guard_ignores_other_candidates(candidate, alpha_long):
  escc = sonata_params(escc=True, alpha_long=alpha_long, candidate=candidate)
  assert escc.to_dict() == sonata_params(escc=False, alpha_long=alpha_long, candidate=candidate).to_dict()


def test_no_escc_profile_is_negotiable():
  cp = sonata_params(escc=False, alpha_long=True)
  model = int(cp.safetyConfigs[0].safetyModel.raw)
  assert 'hyundaiEscc' not in structs.CarParams.SafetyModel.schema.enumerants
  # Unmarked word, Sunnypilot's ESCC=1 bit (EV_GAS in this namespace) and an unassigned high bit.
  for word in (0x006, 0x007, 0xC07, 0xC06 | 0x1000):
    assert not native_accepts(cp, model, word)
  guarded = sonata_params(escc=True, alpha_long=True)
  for word in (0x000, 0x006, 0x007, 0xC06, 0xC07):
    assert not native_accepts(guarded, int(guarded.safetyConfigs[0].safetyModel.raw), word)
