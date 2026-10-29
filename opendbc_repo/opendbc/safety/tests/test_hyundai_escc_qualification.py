"""Native ESCC qualification gate for the classic Sonata Hybrid.

Legacy ESCC 0x2AB carries cached FCA11/SCC12 emergency fields with no
per-source age, so receiving a fresh packet proves nothing about the emergency
source. Until a native ESCC owner is qualified, the ordinary classic profiles
must keep ignoring 0x2AB and keep rejecting every emergency command field, and
no dedicated ESCC safety model may be admitted.
"""
import unittest

from opendbc.car.structs import CarParams
from opendbc.safety.tests.libsafety import libsafety_py
from opendbc.safety.tests import test_hyundai_classic_long_aol as fixture

ESCC_ADDR = 0x2AB
SONATA_HYBRID_LONG_AOL = 0xC06  # LONG | HYBRID_GAS | AOL marker | LDA source
SONATA_HYBRID_LONG = 0x006  # unmarked LONG | HYBRID_GAS, alternative experience 0
CANDIDATE_ESCC_MODEL = 40  # first unassigned SafetyModel at ccdddea52


def escc_frame(*, fca_act=False, scc12_act=False, fca_dec_act=False, scc12_dec_act=False, fca_decel=0, scc12_decel=0,
               obj_valid=True, obj_status=1, distance_raw=222):
  """Serialize the deployed legacy 0x2AB layout (Intel bit numbering)."""
  value = int(fca_act) | (1 << 1 if fca_act else 0) | (int(scc12_act) << 3) | (1 << 4 if scc12_act else 0)
  value |= (int(scc12_dec_act) << 6) | (int(fca_dec_act) << 7) | (scc12_decel << 8)
  value |= (int(obj_valid) << 16) | (obj_status << 17) | (200 << 24) | (distance_raw << 33) | (1700 << 44) | (fca_decel << 56)
  return value.to_bytes(8, 'little')


EMERGENCY_FRAMES = (
  escc_frame(fca_act=True, fca_dec_act=True, fca_decel=50),
  escc_frame(scc12_act=True, scc12_dec_act=True, scc12_decel=50),
  escc_frame(fca_act=True, scc12_act=True, fca_dec_act=True, scc12_dec_act=True, fca_decel=255, scc12_decel=255),
)


class TestHyundaiEsccQualification(unittest.TestCase):
  setUp = fixture.TestHyundaiClassicLongAol.setUp
  tearDown = fixture.TestHyundaiClassicLongAol.tearDown
  reset = fixture.TestHyundaiClassicLongAol.reset
  rx = fixture.TestHyundaiClassicLongAol.rx
  request = fixture.TestHyundaiClassicLongAol.request
  feed = fixture.TestHyundaiClassicLongAol.feed

  def select(self, word):
    self.reset(word, 0 if word == SONATA_HYBRID_LONG else 32)

  def tx(self, name, values, bus=0):
    return self.safety.safety_tx_hook(self.packer.make_can_msg_safety(name, bus, values))

  def rx_escc(self, data, bus=0):
    return self.safety.safety_rx_hook(libsafety_py.make_CANPacket(ESCC_ADDR, bus, data))

  def engage(self, word):
    """Genuine physical SET engagement; controls_allowed is never forced."""
    self.select(word)
    for _ in range(6):
      self.feed(word)
    self.feed(word, button=2)
    self.feed(word)
    self.assertTrue(self.safety.get_controls_allowed())
    self.assertEqual(self.request(3), 0 if word == SONATA_HYBRID_LONG else 3)

  def assert_emergency_commands_rejected(self):
    for values in ({'AEB_CmdAct': 1}, {'CR_VSM_DecCmd': 50}, {'AEB_StopReq': 1}, {'CR_VSM_DecCmd': 255, 'AEB_CmdAct': 1}):
      self.assertFalse(self.tx('SCC12', {'aReqRaw': 0, 'aReqValue': 0, **values}), values)
    for values in ({'FCA_CmdAct': 1}, {'CF_VSM_DecCmdAct': 1}, {'CR_VSM_DecCmd': 50}):
      self.assertFalse(self.tx('FCA11', {'FCA_Status': 2, **values}), values)

  def test_fresh_escc_emergency_packets_grant_no_native_emergency_exception(self):
    if self.release:
      self.skipTest('Current classic OP-long is DEBUG-only')
    for word in (SONATA_HYBRID_LONG, SONATA_HYBRID_LONG_AOL):
      self.engage(word)
      permission = self.request(3)
      # Ordinary longitudinal output stays admitted, so the rejection below is specific to emergency fields.
      self.assertTrue(self.tx('SCC12', {'aReqRaw': 0, 'aReqValue': 0}))
      for data in EMERGENCY_FRAMES:
        for _ in range(5):
          self.rx_escc(data)
        self.assertTrue(self.safety.get_controls_allowed())
        self.assertEqual(self.request(3), permission)
        self.assert_emergency_commands_rejected()

  def test_escc_packets_never_engage_or_rearm(self):
    for word in (SONATA_HYBRID_LONG, SONATA_HYBRID_LONG_AOL):
      self.select(word)
      for _ in range(6):
        self.feed(word)
        for data in EMERGENCY_FRAMES + (escc_frame(),):
          self.rx_escc(data)
      self.assertFalse(self.safety.get_controls_allowed())
      self.assertEqual(self.request(3) & 2, 0)
    if self.release:
      return
    self.engage(SONATA_HYBRID_LONG_AOL)
    self.feed(SONATA_HYBRID_LONG_AOL, button=4)
    self.assertFalse(self.safety.get_controls_allowed())
    for _ in range(10):
      self.feed(SONATA_HYBRID_LONG_AOL)
      self.rx_escc(escc_frame())
    self.assertFalse(self.safety.get_controls_allowed())
    self.assertEqual(self.request(3) & 2, 0)

  def test_host_cannot_spoof_escc_packet(self):
    for word in (SONATA_HYBRID_LONG, SONATA_HYBRID_LONG_AOL):
      self.select(word)
      for bus in (0, 1, 2):
        self.assertFalse(self.safety.safety_tx_hook(libsafety_py.make_CANPacket(ESCC_ADDR, bus, escc_frame())))

  def test_hyundai_profile_rejects_radar_disable_requests(self):
    # The radar-disable transaction is admitted only before the Hyundai profile
    # (diagnostic elm327 mode); once published, only tester present may reach 0x7D0.
    self.reset(SONATA_HYBRID_LONG_AOL)
    for request in (b'\x02\x10\x03', b'\x03\x28\x83\x01', b'\x02\x10\x07', b'\x04\x2E\x01\x42\x00'):
      self.assertFalse(self.safety.safety_tx_hook(libsafety_py.make_CANPacket(0x7D0, 0, request.ljust(8, b'\x00'))), request)

  def test_no_dedicated_escc_safety_model_is_admitted(self):
    self.assertNotIn(CANDIDATE_ESCC_MODEL, CarParams.SafetyModel.schema.enumerants.values())
    for word in (0, SONATA_HYBRID_LONG, SONATA_HYBRID_LONG_AOL):
      if not self.release:
        self.engage(SONATA_HYBRID_LONG_AOL)
      # Firmware answers an unknown mode by failing here and falling back to SILENT (panda/board/main.c).
      self.safety.set_alternative_experience(32)
      self.assertNotEqual(self.safety.set_safety_hooks(CANDIDATE_ESCC_MODEL, word), 0)
      self.assertFalse(self.safety.safety_config_valid())
      self.assertFalse(self.safety.get_controls_allowed())
      self.assertEqual(self.request(3), 0)

  def test_donor_escc_parameter_bit_is_not_an_escc_profile(self):
    # Sunnypilot's ESCC=1 safety parameter is EV_GAS here. On the marked Sonata word it
    # selects no AOL policy (host never negotiates it) and opens no emergency exception.
    word = SONATA_HYBRID_LONG_AOL | 1
    self.reset(word)
    for _ in range(6):
      self.feed(word)
    self.feed(word, button=2)
    self.feed(word)
    self.assertEqual(self.request(3), 0)
    for data in EMERGENCY_FRAMES:
      self.rx_escc(data)
      self.assert_emergency_commands_rejected()

if __name__ == '__main__':
  unittest.main()
