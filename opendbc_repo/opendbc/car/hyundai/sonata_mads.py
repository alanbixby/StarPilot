"""Scoped Sonata MADS MAIN state; physical/native authority remains unchanged."""
from opendbc.car.hyundai.classic_long_aol import qualified as classic_long_qualified
from opendbc.car.hyundai.long_buttons import LongCruiseButtons
from opendbc.car.hyundai.values import CAR, HyundaiFlags


def eligible(cp):
  return bool(cp is not None and cp.carFingerprint == CAR.HYUNDAI_SONATA_HYBRID and
              cp.flags & HyundaiFlags.HAS_LDA_BUTTON and classic_long_qualified(cp))


def configured(cp):
  return bool(eligible(cp) and getattr(cp, 'sonataMadsButtons', False))


class SonataMainButtons(LongCruiseButtons):
  def __init__(self):
    super().__init__(track_main=True)
    self.neutral_seen = False
    self.blocked = True

  def observe_current(self, samples, *, healthy):
    if not healthy:
      self.__init__()
      return
    admitted = []
    for main, normal, cruise in samples:
      if not self.neutral_seen:
        # Held at process start/recovery is not a fresh physical gesture.
        self.neutral_seen = not (main or normal or cruise)
        continue
      admitted.append((main, normal, cruise))
    super().observe(admitted)
