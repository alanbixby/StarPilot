"""One saved profile, frozen before the Sonata LONG startup transaction."""
from dataclasses import replace

from opendbc.car.hyundai.sonata_mads import configured, eligible

PARAM = 'SonataMadsButtons'
SUPERSEDED_BUTTONS = frozenset(('LKASButtonControl', 'MainCruiseButtonControl', 'CancelButtonControl',
                               'LongCancelButtonControl', 'VeryLongCancelButtonControl'))


def prepare(cp, params):
  # Exact bytes: malformed or absent saved data cannot opt a drive in.
  from openpilot.starpilot.saved_source import read_saved
  cp.sonataMadsButtons = bool(eligible(cp) and read_saved(params, PARAM, 8) == (b'1', True))
  return cp


def effective_settings(cp, settings):
  if settings is None or not configured(cp):
    return settings
  # Preserve saved assignments/threshold for the next drive with this profile off.
  return replace(settings, pause_brake_mps=0., lkas_action=9, main_action=0, cancel_actions=(0, 0, 0))


def publish_display_intent(axis, intent, *, now_ns):
  """Copy only the selfdrived owner's accepted, exact carState companion."""
  from openpilot.starpilot.aol.runtime import INTENT_MAX_AGE_NS
  valid = bool(intent is not None and intent.settingsQualified and intent.producerSessionId and
    axis.intentProducerSessionId == intent.producerSessionId and
    axis.sourceCarStateMonoTime == intent.carStateLogMonoTime and
    0 < intent.carStateLogMonoTime <= intent.observedMonoTime <= now_ns <= intent.validUntilMonoTime and
    now_ns - intent.carStateLogMonoTime <= INTENT_MAX_AGE_NS)
  axis.displayIntentValid = valid
  axis.displayIntentEnabled = bool(valid and intent.lateralArmed)


def publish_control_display(cc, sm, *, available, now_ns):
  """Display freshness cannot grant either actuator axis."""
  from openpilot.starpilot.aol.runtime import INTENT_MAX_AGE_NS, current_axis
  cc.lateralDisplay.available = bool(available)
  cc.lateralDisplay.valid = cc.lateralDisplay.enabled = False
  if not available:
    return
  axis = current_axis(sm, now_ns=now_ns)
  valid = bool(axis is not None and axis.displayIntentValid and axis.intentProducerSessionId and
    sm.seen['carState'] and sm.valid['carState'] and sm.alive['carState'] and
    axis.sourceCarStateMonoTime == sm.logMonoTime['carState'] and
    0 < axis.sourceCarStateMonoTime <= now_ns and now_ns - axis.sourceCarStateMonoTime <= INTENT_MAX_AGE_NS)
  cc.lateralDisplay.valid = valid
  cc.lateralDisplay.enabled = bool(valid and axis.displayIntentEnabled)


class _LateralTransportView:
  """Exclude only software MAIN-off from LAT retention's event view."""
  def __init__(self, sm):
    self.sm = sm

  def __getattr__(self, name):
    return getattr(self.sm, name)

  def __getitem__(self, service):
    if service == 'onroadEvents':
      from openpilot.cereal import log
      return tuple(event for event in self.sm[service] if event.name != log.OnroadEvent.EventName.wrongCarMode)
    return self.sm[service]


def lateral_transport_view(sm, cp, cs):
  # Ordinary LONG still sees wrongCarMode and must disengage on MAIN OFF.
  # This view changes no CAN, native status, stamps, leases or output gates.
  return _LateralTransportView(sm) if configured(cp) and not cs.cruiseState.available and not cs.accFaulted else sm
