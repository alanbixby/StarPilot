"""Display-only MADS facts Card hands the Hyundai controller before each apply.

They select factory icons only and never admit actuation. With `available`, the
Sonata Hybrid LKAS car/lanes and LFA wheel follow final CC.latActive: green
while active, a one-second blink after it falls, then white car/lanes plus a
white wheel only while `valid and enabled`. Invalid or unrefreshed facts withhold
just that white wheel; they never show green, and cruise state is never used.
"""
from typing import NamedTuple


class LateralDisplay(NamedTuple):
  available: bool = False  # MADS profile selected and qualified for this drive (startup captured)
  valid: bool = False      # enabled is from a fresh, joined intent source this frame
  enabled: bool = False    # retained/armed lateral intent; false whenever not valid
