"""ESCC active-control qualification tripwires for the classic Sonata Hybrid.

These tests record the *current* incompatibility, not a safe ESCC path. Today the
ordinary Sonata LONG path cannot see an inline ESCC interceptor: with 0x2AB on
the powertrain bus it still selects the radar-disable startup owner, the generic
init disable, the tester-present keeper and synthetic FCA12 "AEB disabled"
output. On an ESCC installation that disables the stock radar AEB the
interceptor exists to preserve. Only stock longitudinal (alpha long off) sends
no radar-disable traffic.

When an ESCC owner is implemented, these assertions are expected to fail and
must be reclassified against docs/research/escc-active-owner-spec.md, never
deleted to make a demonstration pass.
"""
import pytest

from opendbc.can import CANPacker
from opendbc.car import gen_empty_fingerprint, structs, uds
from opendbc.car.hyundai import hyundaican, interface as hyundai_interface
from opendbc.car.hyundai.classic_long_aol import native_accepts, ordinary_word, qualified
from opendbc.car.hyundai.interface import CarInterface
from opendbc.car.hyundai.ordinary_classic_startup import OrdinaryClassicStartup
from opendbc.car.hyundai.values import CAR, HyundaiFlags

ESCC_ADDR = 0x2AB
RADAR_DISABLE = bytes([uds.SERVICE_TYPE.COMMUNICATION_CONTROL, 0x80 | uds.CONTROL_TYPE.DISABLE_RX_DISABLE_TX, uds.MESSAGE_TYPE.NORMAL])


def sonata_params(*, escc, alpha_long, release=False):
  fingerprint = gen_empty_fingerprint()
  fingerprint[0][0x391] = 8  # LDA button source
  fingerprint[0][0x38D] = 8  # FCA11, forwarded by the interceptor from the stock radar
  if escc:
    fingerprint[0][ESCC_ADDR] = 8
  return CarInterface.get_params(CAR.HYUNDAI_SONATA_HYBRID, fingerprint, [], alpha_long, release, False)


def radar_disables(cp, monkeypatch):
  calls = []
  monkeypatch.setattr(hyundai_interface, 'disable_ecu', lambda recv, send, **kwargs: calls.append(kwargs))
  CarInterface.init(cp, lambda *args, **kwargs: [], lambda *args, **kwargs: None)
  return calls


def test_current_ordinary_long_profile_is_blind_to_escc():
  plain, escc = sonata_params(escc=False, alpha_long=True), sonata_params(escc=True, alpha_long=True)
  assert escc.to_dict() == plain.to_dict(), 'CarParams now depend on ESCC presence; reclassify against the active-owner spec'
  assert escc.openpilotLongitudinalControl and not escc.pcmCruise
  assert escc.flags & HyundaiFlags.USE_FCA
  assert qualified(escc) and escc.safetyConfigs[0].safetyParam == ordinary_word(escc) == 0x006


def test_current_ordinary_long_with_escc_still_disables_the_radar(monkeypatch):
  cp = sonata_params(escc=True, alpha_long=True)
  assert CarInterface.startup_required(cp)
  owner = CarInterface.startup_owner(cp, (lambda *a, **k: [], lambda frames: None), requested=True)
  assert isinstance(owner, OrdinaryClassicStartup)
  assert (owner.address, owner.bus) == (0x7D0, 0)
  calls = radar_disables(cp, monkeypatch)
  assert calls == [{'bus': 0, 'addr': 0x7D0, 'com_cont_req': RADAR_DISABLE}]


def test_current_ordinary_long_synthesizes_fca12_with_aeb_disabled():
  cp = sonata_params(escc=True, alpha_long=True)
  packer = CANPacker('hyundai_can_generated')
  opts = {address: (data, bus) for address, data, bus in hyundaican.create_acc_opt(packer, cp)}
  # With ESCC the interceptor forwards the stock radar's FCA12; this host frame would compete with it.
  assert 0x483 in opts and opts[0x483][1] == 0
  assert packer.dbc.name_to_msg['FCA12'].address == 0x483
  fca_usm = opts[0x483][0][0] & 0x7  # FCA_USM 0|3, 1 = AEB disabled
  assert fca_usm == 1, 'FCA12 builder changed; reclassify the ESCC FCA pass-through gate'


@pytest.mark.parametrize('release', [False, True])
def test_stock_longitudinal_with_escc_sends_no_radar_disable(release, monkeypatch):
  cp = sonata_params(escc=True, alpha_long=False, release=release)
  assert not cp.openpilotLongitudinalControl and cp.pcmCruise
  assert not CarInterface.startup_required(cp)
  assert CarInterface.startup_owner(cp, (lambda *a, **k: [], lambda frames: None), requested=False) is None
  assert radar_disables(cp, monkeypatch) == []
  assert not qualified(cp)


def test_no_escc_profile_is_negotiable():
  cp = sonata_params(escc=True, alpha_long=True)
  model = int(cp.safetyConfigs[0].safetyModel.raw)
  assert 'hyundaiEscc' not in structs.CarParams.SafetyModel.schema.enumerants
  # Unmarked word, Sunnypilot's ESCC=1 bit (EV_GAS in this namespace) and an unassigned high bit.
  for word in (0x006, 0x007, 0xC07, 0xC06 | 0x1000):
    assert not native_accepts(cp, model, word)
