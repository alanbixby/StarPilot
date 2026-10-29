"""Parked opt-in for the exact Sonata default MADS button lifecycle."""
from opendbc.car.hyundai.sonata_mads import eligible
from openpilot.starpilot.car.hyundai.sonata_mads import PARAM
from openpilot.starpilot.saved_document import commit_exact
from openpilot.starpilot.saved_source import read_saved
from openpilot.starpilot.ui.feature_settings_state import FeatureRow

LABEL = 'Sunnypilot MADS Buttons'
DESCRIPTION = ('Cruise MAIN turns steering on and off. The lane button toggles steering, SET or RES also starts steering, ' +
               "and CANCEL or braking keeps steering on. Matches sunnypilot's default MADS.")


class SonataMadsFeature:
  def __init__(self, owner):
    self.owner = owner

  def capability(self):
    cp = self.owner.vehicle_params()
    if not eligible(cp):
      return None
    return (str(cp.carFingerprint), str(cp.brand), int(cp.flags), bool(cp.openpilotLongitudinalControl),
            bool(cp.pcmCruise), int(cp.alternativeExperience),
            tuple((str(config.safetyModel), int(config.safetyParam)) for config in cp.safetyConfigs))

  def selected(self):
    return self.capability() is not None and read_saved(self.owner.params, PARAM, 8) == (b'1', True)

  def rows(self, parked):
    capability = self.capability()
    if capability is None:
      return ()
    raw, readable = read_saved(self.owner.params, PARAM, 8)
    valid = readable and raw in (None, b'0', b'1')
    return (FeatureRow(PARAM, LABEL, ('On' if raw == b'1' else 'Off') if valid else 'Invalid saved choice',
                       source=raw, choices=('Off', 'On') if valid else (), default_value='Off',
                       available=parked and self.owner.authority('parked_preferences') and valid,
                       reason=DESCRIPTION if valid else 'Invalid saved choice', capability=capability),)

  def apply(self, request):
    if (request.key != PARAM or request.value not in ('Off', 'On') or not request.confirmation or
        request.expected not in (None, b'0', b'1') or request.dependencies or request.related_source is not None or
        request.display_unit or request.direction):
      return False

    def authorized():
      return (self.owner.authority('parked_preferences') and bool(request.vehicle_fingerprint) and
              self.owner.vehicle_fingerprint() == request.vehicle_fingerprint and request.capability is not None and
              self.capability() == request.capability)

    result = commit_exact(self.owner.params, key=PARAM, max_bytes=8, expected=request.expected,
                          raw=b'1' if request.value == 'On' else b'0', authorized=authorized, temp_prefix='.sonata-mads-')
    return result.committed and result.verified
