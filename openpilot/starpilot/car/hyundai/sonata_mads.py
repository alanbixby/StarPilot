"""One saved profile, captured once by each Sonata drive owner."""
from collections import deque
from dataclasses import replace

from opendbc.car.hyundai.sonata_mads import eligible
from opendbc.car.hyundai.lateral_display import LateralDisplay

PARAM = 'SonataMadsButtons'
SUPERSEDED_BUTTONS = frozenset(('LKASButtonControl', 'MainCruiseButtonControl', 'CancelButtonControl',
                               'LongCancelButtonControl', 'VeryLongCancelButtonControl'))


def capture(cp, params):
  # Called once by each process owner. Saved changes cannot alter this session.
  from openpilot.starpilot.saved_source import read_saved
  return bool(eligible(cp) and read_saved(params, PARAM, 8) == (b'1', True))


def effective_settings(cp, settings, *, mads_buttons=False):
  if settings is None:
    return None
  selected = bool(eligible(cp) and mads_buttons)
  if not selected:
    return replace(settings, mads_buttons=False) if settings.mads_buttons else settings
  # Preserve saved assignments/threshold for the next drive with this profile off.
  return replace(settings, pause_brake_mps=0., lkas_action=9, main_action=0, cancel_actions=(0, 0, 0), mads_buttons=True)


class MadsProfileAgreement:
  """A startup choice must agree with Card for the entire producer session."""
  def __init__(self, selected):
    self._selected = bool(selected)
    self.producer = ''
    self.rejected = False
    self.rejected_producers = set()
    self.rejection_overflow = False

  @property
  def selected(self):
    return self._selected

  def accept(self, intent):
    if intent is None:
      return None
    self.producer = intent.producerSessionId
    if not intent.madsButtonsConsistent or bool(intent.madsButtons) != self.selected:
      if self.producer not in self.rejected_producers and len(self.rejected_producers) >= 16:
        self.rejection_overflow = True  # Bound memory; never evict a rejected session into eligibility.
      else:
        self.rejected_producers.add(self.producer)
    self.rejected = bool(self.rejection_overflow or self.producer in self.rejected_producers)
    return None if self.rejected else intent


def publish_display_intent(axis, intent, *, now_ns):
  """Copy only the selfdrived owner's accepted, exact carState companion."""
  from openpilot.starpilot.aol.runtime import INTENT_MAX_AGE_NS
  valid = bool(intent is not None and intent.madsButtonsConsistent and intent.settingsQualified and intent.producerSessionId and
    axis.intentProducerSessionId == intent.producerSessionId and
    axis.sourceCarStateMonoTime == intent.carStateLogMonoTime and
    0 < intent.carStateLogMonoTime <= intent.observedMonoTime <= now_ns <= intent.validUntilMonoTime and
    now_ns - intent.carStateLogMonoTime <= INTENT_MAX_AGE_NS)
  axis.displayIntentValid = valid
  axis.displayIntentEnabled = bool(valid and intent.lateralArmed)


class MadsDisplayState:
  """Fresh display facts join only this Card's valid source publications."""
  def __init__(self, *, available):
    self._available = bool(available)
    self.sources = deque(maxlen=4)
    self.identity = None

  @property
  def available(self):
    return self._available

  def _bind(self, producer, session, context):
    identity = (producer, session, context)
    if identity != self.identity:
      self.sources.clear()
      self.identity = identity

  def record(self, stamp, *, valid, producer, session, context):
    self._bind(producer, session, context)
    if not valid or stamp <= 0:
      self.sources.clear()
    elif self.sources and stamp <= self.sources[-1]:
      self.sources.clear()
    else:
      self.sources.append(stamp)

  def snapshot(self, sm, *, now_ns, producer, session, context, source_healthy):
    from openpilot.starpilot.aol.runtime import INTENT_MAX_AGE_NS, current_axis
    self._bind(producer, session, context)
    if not self.available:
      return LateralDisplay()
    axis = current_axis(sm, now_ns=now_ns)
    valid = bool(source_healthy and axis is not None and axis.displayIntentValid and producer and session and
      axis.sessionId == session and axis.intentProducerSessionId == producer and
      axis.sourceCarStateMonoTime in self.sources and
      0 < axis.sourceCarStateMonoTime <= axis.observedMonoTime <= now_ns and
      now_ns - axis.sourceCarStateMonoTime <= INTENT_MAX_AGE_NS)
    return LateralDisplay(available=True, valid=valid, enabled=bool(valid and axis.displayIntentEnabled))


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


def lateral_transport_view(sm, cp, cs, *, mads_buttons=False):
  # Ordinary LONG still sees wrongCarMode and must disengage on MAIN OFF.
  # This view changes no CAN, native status, stamps, leases or output gates.
  return _LateralTransportView(sm) if mads_buttons and eligible(cp) and not cs.cruiseState.available and not cs.accFaulted else sm
