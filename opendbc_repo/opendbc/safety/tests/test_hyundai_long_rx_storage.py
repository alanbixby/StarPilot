"""Selected longitudinal RX windows retain each profile's health and reset contract."""

import pytest

from opendbc.car.structs import CarParams
from opendbc.safety.tests.common import CANPackerSafety
from opendbc.safety.tests.libsafety import libsafety_py
from opendbc.safety.tests.test_hyundai import checksum


C06 = 0xC06
ORDINARY = 0x004
FCEV = 0x104
LEGACY_WORDS = (0xE900, 0xE901, 0xE902, 0xE903, 0xE910, 0xE911, 0xE912, 0xE913)
COMMON_ADDRS = (0x260, 0x386, 0x394, 0x251, 0x4F1)
STATUS_FIELDS = tuple(name for name, _ in libsafety_py.ffi.typeof('RxStatus').fields)
MSG_FIELDS = tuple(name for name, _ in libsafety_py.ffi.typeof('CanMsgCheck').fields)


def status(row):
  return tuple(getattr(row.status, name) for name in STATUS_FIELDS)


def descriptor(msg):
  return tuple(getattr(msg, name) for name in MSG_FIELDS)


def expected_common(legacy):
  def msg(addr, length, frequency, ignore_checksum=True, ignore_counter=True, max_counter=0):
    return (addr, 0, length, frequency, ignore_checksum, ignore_counter, max_counter, True)

  zero = (0,) * len(MSG_FIELDS)
  return (
    (msg(0x260, 8, 100, False, False, 3), msg(0x371, 8, 100), zero),
    (msg(0x386, 8, 100, legacy, legacy, 0 if legacy else 15), zero, zero),
    (msg(0x394, 8, 100, legacy, legacy, 0 if legacy else 7), zero, zero),
    (msg(0x251, 8, 50), zero, zero),
    (msg(0x4F1, 4, 50, True, False, 15), zero, zero),
  )


class LongRx:
  def __init__(self, safety):
    self.safety = safety
    self.packer = CANPackerSafety('hyundai_can_generated')
    self.now = 1_000_000
    self.counter = 0

  def select(self, word):
    legacy = word in LEGACY_WORDS
    experience = 32 if word == C06 or (legacy and word & 2) else 0
    self.safety.set_alternative_experience(experience)
    mode = CarParams.SafetyModel.hyundaiLegacy if legacy else CarParams.SafetyModel.hyundai
    assert self.safety.set_safety_hooks(mode, word) == 0
    self.safety.set_timer(self.now)
    self.safety.set_aol_test_heartbeat(True)
    config = self.safety.get_safety_config()
    return config.rx_checks, config.rx_checks_len

  def extra(self, addr):
    assert self.safety.safety_rx_hook(libsafety_py.make_CANPacket(addr, 0, bytes(8)))

  def feed(self, *, omit=(), button=0):
    self.now += 10_000
    self.safety.set_timer(self.now)
    count = self.counter
    self.counter += 1
    frames = (
      ('EMS16', {'AliveCounter': count % 4}, True),
      ('WHL_SPD11', {'WHL_SPD_AliveCounter_LSB': count % 4,
                     'WHL_SPD_AliveCounter_MSB': (count // 4) % 4}, True),
      ('TCS13', {'AliveCounterTCS': count % 8, 'ACCEnable': 0}, True),
      ('MDPS12', {'CR_Mdps_StrColTq': 0}, False),
      ('CLU11', {'CF_Clu_AliveCnt1': count % 16, 'CF_Clu_CruiseSwState': button}, False),
    )
    for name, values, integrity in frames:
      assert self.safety.safety_rx_hook(self.packer.make_can_msg_safety(
        name, 0, values, fix_checksum=checksum if integrity else None)), name
    for addr in (0x391, 0x91):
      if addr not in omit:
        self.extra(addr)

  def warm(self, **kwargs):
    for _ in range(6):
      self.feed(**kwargs)


@pytest.fixture
def car():
  safety = libsafety_py.libsafety
  safety.init_tests()
  if safety.set_safety_hooks(CarParams.SafetyModel.allOutput, 0) != 0:
    pytest.skip('Existing classic OP-long is DEBUG-only')
  yield LongRx(safety)
  safety.set_alternative_experience(0)
  safety.set_safety_hooks(CarParams.SafetyModel.silent, 0)


@pytest.mark.parametrize('word', (C06, ORDINARY, FCEV, *LEGACY_WORDS))
def test_exact_selected_descriptors_and_initial_status(car, word):
  rows, count = car.select(word)
  legacy = word in LEGACY_WORDS
  common_base = 1 if word == C06 else 0
  assert count == (6 if word in (C06, FCEV) else 5)
  assert tuple(rows[common_base + i].msg[0].addr for i in range(5)) == COMMON_ADDRS
  assert tuple(tuple(descriptor(msg) for msg in rows[common_base + i].msg) for i in range(5)) == expected_common(legacy)
  if word in (C06, FCEV):
    extra_index, addr, frequency = (0, 0x391, 50) if word == C06 else (5, 0x91, 100)
    assert descriptor(rows[extra_index].msg[0]) == (addr, 0, 8, frequency, True, True, 0, True)
    assert all(not any(descriptor(msg)) for msg in rows[extra_index].msg[1:3])
  assert all(not any(status(rows[i])) for i in range(count))


def test_c06_ordinary_fcev_c06_share_windows_and_reset_only_selected_statuses(car):
  c06, _ = car.select(C06)
  car.warm(omit=(0x91,))
  assert car.safety.aol_rx_healthy()
  bcm_status = status(c06[0])
  assert c06[0].status.msg_seen

  ordinary, count = car.select(ORDINARY)
  assert ordinary == c06 + 1 and count == 5
  assert status(c06[0]) == bcm_status  # Outside the selected reset window.
  assert all(not any(status(ordinary[i])) for i in range(count))
  car.now += 1
  car.safety.set_timer(car.now)
  car.extra(0x391)
  assert status(c06[0]) == bcm_status  # An unselected packet cannot stamp this row.
  for _ in range(110):
    car.feed(omit=(0x391, 0x91))
  assert car.safety.aol_rx_healthy()
  car.safety.safety_tick()
  assert car.safety.safety_config_valid()
  assert status(c06[0]) == bcm_status  # Even an expired unselected source is not ticked.

  fcev, count = car.select(FCEV)
  assert fcev == ordinary and count == 6
  assert all(not any(status(fcev[i])) for i in range(count))
  car.warm(omit=(0x391,))
  assert car.safety.aol_rx_healthy()
  fcev_status = status(fcev[5])
  assert fcev[5].status.msg_seen

  returned, count = car.select(C06)
  assert returned == c06 and count == 6
  assert all(not any(status(returned[i])) for i in range(count))
  assert status(fcev[5]) == fcev_status
  car.now += 1
  car.safety.set_timer(car.now)
  car.extra(0x91)
  assert status(fcev[5]) == fcev_status
  car.warm(omit=(0x91,))
  assert car.safety.aol_rx_healthy()


@pytest.mark.parametrize('legacy_word', LEGACY_WORDS)
@pytest.mark.parametrize('modern_word', (ORDINARY, C06, FCEV))
def test_legacy_to_modern_restores_strict_common_integrity(car, legacy_word, modern_word):
  legacy, count = car.select(legacy_word)
  assert count == 5  # E900's 0x100 bit must never add the FCEV source.
  assert tuple(legacy[i].msg[0].max_counter for i in (1, 2)) == (0, 0)
  car.warm(omit=(0x391, 0x91))
  assert car.safety.aol_rx_healthy()

  modern, count = car.select(modern_word)
  common_base = 1 if modern_word == C06 else 0
  assert modern + common_base == legacy
  assert tuple(modern[common_base + i].msg[0].max_counter for i in (1, 2)) == (15, 7)
  for i in (1, 2):
    assert not modern[common_base + i].msg[0].ignore_checksum
    assert not modern[common_base + i].msg[0].ignore_counter
  assert all(not any(status(modern[i])) for i in range(count))
  car.warm()
  assert car.safety.aol_rx_healthy()


@pytest.mark.parametrize('word, required, unselected, lease', ((C06, 0x391, 0x91, 200_000), (FCEV, 0x91, 0x391, 100_000)))
@pytest.mark.parametrize('failure', ('missing', 'lease', 'lagging'))
def test_required_source_missing_or_expired_is_not_replaced_by_other_window(car, word, required, unselected, lease, failure):
  rows, count = car.select(word)
  required_index = 0 if word == C06 else count - 1
  if failure != 'missing':
    car.warm()
    assert car.safety.aol_rx_healthy()
    if word == C06:
      car.feed(button=2)
      car.feed()
      assert car.safety.get_controls_allowed()
    duration = lease + 10_000 if failure == 'lease' else 1_100_000
    for _ in range(duration // 10_000):
      car.feed(omit=(required,))
  else:
    car.warm(omit=(required,), button=2)
    car.feed(omit=(required,))

  car.extra(unselected)
  assert not car.safety.aol_rx_healthy()
  if failure == 'missing':
    assert not rows[required_index].status.msg_seen
  elif failure == 'lagging':
    car.safety.safety_tick()
    assert rows[required_index].status.lagging
    assert all(not rows[i].status.lagging for i in range(count) if i != required_index)
  if word == C06:
    car.safety.aol_set_host_request(3)
    assert car.safety.aol_get_permission_mask() == 0
    assert not car.safety.get_controls_allowed()
    assert not car.safety.safety_tx_hook(car.packer.make_can_msg_safety('LKAS11', 0, {
      'CR_Lkas_StrToqReq': 1, 'CF_Lkas_ActToi': 1}))
    assert not car.safety.safety_tx_hook(car.packer.make_can_msg_safety('SCC12', 0, {
      'aReqRaw': .1, 'aReqValue': .1}))
