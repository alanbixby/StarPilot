"""Sunnypilot default MADS buttons over the existing classic LONG owner."""
from opendbc.car.structs import CarState
from openpilot.starpilot.car.hyundai.classic_long_intent import ClassicLongCardIntent
from openpilot.starpilot.car.hyundai.sonata_mads import effective_settings

ButtonType = CarState.ButtonEvent.Type


class SonataMadsCardIntent(ClassicLongCardIntent):
  def __init__(self, cp, settings):
    super().__init__(cp, effective_settings(cp, settings))
    self.main_available = False

  def update(self, CS, **kwargs):
    previous_main = self.main_available
    super().update(CS, **kwargs)
    self.main_available = bool(CS.cruiseState.available)
    if previous_main and not self.main_available:
      self._record_latch_clear('mads_main_off')
      self.allowed_latch = False
      self.pause_lateral = True
      self._withdraw_full_engagement()
    elif (not previous_main and self.main_available and
          any(e.type == ButtonType.mainCruise and e.pressed for e in CS.buttonEvents) and
          not any(e.type in (ButtonType.cancel, ButtonType.lkas) for e in CS.buttonEvents) and
          self.settings.enabled and CS.canValid and not CS.canTimeout and not CS.accFaulted and
          not self._fault_inhibit and not CS.steerFaultPermanent):
      # MAIN ON is an enable edge, even if the lane button already enabled LAT.
      self.allowed_latch = True
      self.pause_lateral = False
      self._last_latch_edge_ns = self.physical_arm_ns = kwargs.get('now_ns', 0)
