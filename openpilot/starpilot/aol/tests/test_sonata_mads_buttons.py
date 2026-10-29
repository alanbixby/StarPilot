"""Sonata profile at the real Hyundai parser/interface and Card intent boundary."""

from pathlib import Path

from opendbc.car.hyundai.interface import CarInterface
from opendbc.car.hyundai.values import CAR
from openpilot.starpilot.aol.intent import AolSettings
from openpilot.starpilot.aol.tests.test_classic_long_profiles import params
from opendbc.car.hyundai.tests.test_sonata_alpha_implicit import classic_frames
from openpilot.starpilot.car.hyundai.aol import create_intent
from openpilot.starpilot.car.hyundai.sonata_mads import effective_settings


def profile_cp():
  cp = params(CAR.HYUNDAI_SONATA_HYBRID, lda=True)
  cp.safetyConfigs[0].safetyParam = 0xC06
  cp.alternativeExperience = 32
  return cp


def test_parser_main_lifecycle_is_independent_of_tcs_health(monkeypatch):
  cp = profile_cp()
  ci = CarInterface(cp)
  ci.CS.sonata_mads_buttons = True
  intent = create_intent(cp, effective_settings(cp, AolSettings(True, 0., 0, 0, (0, 0, 0), (0, 0, 0)), mads_buttons=True))
  tick = 0

  def step(main=0, lkas=0, button=0):
    nonlocal tick
    tick += 1
    now = 10_000_000_000 + tick * 10_000_000
    monkeypatch.setattr('time.clock_gettime_ns', lambda clock: now)
    cs = ci.update([(now, classic_frames(ci, tick, main=main, lkas=lkas, button=button))])
    intent.update(cs, now_ns=now, fault_active=False)
    return cs

  for _ in range(120):
    cs = step()
  assert cs.canValid and not cs.accFaulted
  assert not cs.cruiseState.available, 'TCS health is not the software cruise MAIN state'
  assert not intent.allowed_latch
  for _ in range(10):
    cs = step(main=1)
  for _ in range(10):
    cs = step()
  assert cs.cruiseState.available and intent.allowed_latch
  for _ in range(10):
    cs = step(main=1)
  for _ in range(10):
    cs = step()
  assert not cs.cruiseState.available and not intent.allowed_latch
  for _ in range(10):
    cs = step(lkas=1)
  for _ in range(10):
    cs = step()
  assert not cs.cruiseState.available and intent.allowed_latch


def test_opt_in_is_exact_scoped_and_preserves_saved_choices(tmp_path):
  from openpilot.common.params import Params
  from openpilot.starpilot.car.hyundai.sonata_mads import capture

  saved = Params(str(tmp_path))
  saved.put_bool('AlwaysOnLateral', True, block=True)
  original = AolSettings(True, 10., 8, 9, (1, 5, 6), (8, 8, 8))
  cp = params(CAR.HYUNDAI_SONATA_HYBRID, lda=True)
  for raw in (None, b'0', b'garbage', b'1\n'):
    if raw is not None:
      Path(saved.get_param_path('SonataMadsButtons')).write_bytes(raw)
    assert not capture(cp, saved)
    assert effective_settings(cp, original) is original
  saved.put_bool('SonataMadsButtons', True, block=True)
  assert capture(cp, saved)
  settings = effective_settings(cp, original, mads_buttons=capture(cp, saved))
  assert settings.lkas_action == 9 and settings.main_action == 0 and settings.cancel_actions == (0, 0, 0)
  assert settings.pause_brake_mps == 0. and settings.distance_actions == original.distance_actions
  assert original.lkas_action == 8 and original.pause_brake_mps == 10.
  for other in (params(CAR.HYUNDAI_SONATA, lda=True), params(CAR.HYUNDAI_SONATA_HYBRID, lda=False),
                params(CAR.HYUNDAI_SONATA_HYBRID, lda=True, release=True)):
    assert not capture(other, saved)


def test_held_main_at_startup_and_recovery_requires_neutral_then_new_press(monkeypatch):
  cp = profile_cp()
  ci = CarInterface(cp)
  ci.CS.sonata_mads_buttons = True
  tick = 0

  def step(main=0, *, omit=False):
    nonlocal tick
    tick += 1
    now = 10_000_000_000 + tick * 10_000_000
    monkeypatch.setattr('time.clock_gettime_ns', lambda clock: now)
    frames = classic_frames(ci, tick, main=main)
    if omit:
      frames = [frame for frame in frames if frame[0] != 0x4F1]
    return ci.update([(now, frames)])

  for _ in range(120):
    cs = step(1)
  assert cs.canValid and not cs.cruiseState.available
  for _ in range(10):
    step()
  for _ in range(10):
    step(1)
  assert step().cruiseState.available
  for _ in range(120):
    cs = step(1, omit=True)
  assert not cs.canValid and not cs.cruiseState.available
  for _ in range(120):
    cs = step(1)
  assert cs.canValid and not cs.cruiseState.available
  for _ in range(10):
    step()
  for _ in range(10):
    step(1)
  assert step().cruiseState.available
