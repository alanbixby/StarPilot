"""Original carState companions from Card's nonconflating intent transport."""
from collections import deque
from dataclasses import replace

from openpilot.cereal import messaging
from openpilot.starpilot.aol.runtime import INTENT_MAX_AGE_NS, SAFETY_MAX_AGE_NS
from openpilot.starpilot.aol.wire import INTENT_SERVICE, decode_intent

MAX_COMPANIONS = 16
MAX_DRAIN = 32


def restrict_intent(intent, restriction):
  """Fold vetoes without renewing the selected source or hiding profile conflicts."""
  return replace(intent, allowedLatch=intent.allowedLatch and restriction.allowedLatch,
    pauseLateral=intent.pauseLateral or restriction.pauseLateral,
    pauseLongitudinal=intent.pauseLongitudinal or restriction.pauseLongitudinal,
    lateralArmed=intent.lateralArmed and restriction.lateralArmed,
    optionalSetRelease=intent.optionalSetRelease and restriction.optionalSetRelease,
    madsButtonsConsistent=bool(intent.madsButtonsConsistent and restriction.madsButtonsConsistent and
                              intent.madsButtons == restriction.madsButtons))


class IntentCompanion:
  def __init__(self, sock):
    self.sock = sock
    self.samples = {}
    self.latest = None
    self.session = None
    self.sequence = -1
    self.sequence_stamp = 0
    self.integrity_epoch = 0
    self.verified_sm_projection = None
    self.raw_projections = {}
    self.last_rejection = None
    self.last_drain_count = 0
    self.last_drain_ok = True
    self.mads_profiles = {}
    self.mads_profile_pending = deque()
    self.mads_profile_overflow = False

  def _observe(self, stamp, valid, raw):
    stamp = int(stamp)
    intent = decode_intent(raw)
    valid = bool(valid and intent is not None and intent.carStateLogMonoTime == stamp)
    previous = self.samples.get(stamp)
    if self.latest is not None and stamp < self.latest[0] and previous is None:
      valid = False
    if intent is not None and (self.latest is None or stamp >= self.latest[0]):
      if self.session is not None and intent.producerSessionId != self.session:
        self.integrity_epoch += 1
        self.verified_sm_projection = None
        self.raw_projections.clear()
        self.samples.clear()
        self.sequence = -1
        self.sequence_stamp = 0
      self.session = intent.producerSessionId
    if previous is not None:
      valid = bool(valid and previous[1] and previous[2] == intent)
    if valid and intent is not None:
      if (stamp > self.sequence_stamp and intent.producerSessionId == self.session and
          intent.sequence <= self.sequence):
        valid = False
      for other_stamp, other_valid, other in self.samples.values():
        if other_valid and other is not None and other.producerSessionId == intent.producerSessionId and (
            other_stamp < stamp and other.sequence >= intent.sequence or
            other_stamp > stamp and other.sequence <= intent.sequence):
          valid = False
    if valid and intent is not None and (self.latest is None or stamp >= self.latest[0]):
      self.sequence = max(self.sequence, intent.sequence)
      self.sequence_stamp = max(self.sequence_stamp, stamp)
    if not valid:
      self.integrity_epoch += 1
      self.verified_sm_projection = None
      self.raw_projections.clear()
    sample = (stamp, valid, intent)
    if valid and intent is not None:
      # Keep intake provenance outside the replaceable source caches. Freshness
      # is checked on the decision clock, before pruning or consulting SM.
      if len(self.mads_profile_pending) >= MAX_DRAIN:
        self.mads_profile_overflow = True
      else:
        self.mads_profile_pending.append(sample)
    self.samples[stamp] = sample
    if self.latest is None or stamp >= self.latest[0]:
      self.latest = sample
    while len(self.samples) > MAX_COMPANIONS:
      del self.samples[min(self.samples)]

  @staticmethod
  def _fresh(sample, now_ns):
    stamp, valid, intent = sample
    return bool(valid and intent is not None and intent.settingsQualified and intent.producerSessionId and
                0 < stamp <= now_ns and now_ns - stamp <= INTENT_MAX_AGE_NS and
                intent.observedMonoTime <= now_ns <= intent.validUntilMonoTime and
                now_ns - intent.observedMonoTime <= INTENT_MAX_AGE_NS)

  @property
  def mads_profile_consistent(self):
    profile = self.mads_profiles.get(self.session)
    return not self.mads_profile_overflow and (profile is None or profile[1])

  def _remember_profiles(self, now_ns):
    while self.mads_profile_pending:
      sample = self.mads_profile_pending.popleft()
      if not self._fresh(sample, now_ns):
        continue
      intent = sample[2]
      producer = intent.producerSessionId
      previous = self.mads_profiles.get(producer)
      if previous is None:
        if len(self.mads_profiles) >= MAX_COMPANIONS:
          # Never evict a rejected/previous producer back into eligibility.
          # A consumer restart is required after exhausting this bounded journal.
          self.mads_profile_overflow = True
          continue
        previous = (intent.madsButtons, True)
      self.mads_profiles[producer] = (previous[0], bool(previous[1] and intent.madsButtonsConsistent and
                                                      previous[0] == intent.madsButtons))

  def drain(self, *, max_messages=MAX_DRAIN):
    """Read bounded transport before sampling the decision clock."""
    self.last_drain_count = 0
    self.last_drain_ok = False
    for _ in range(max(0, min(MAX_DRAIN, max_messages))):
      message = messaging.recv_one_or_none(self.sock)
      if message is None:
        break
      self.last_drain_count += 1
      self._observe(message.logMonoTime, message.valid, message.aolIntentWire)
      sample = self.samples.get(int(message.logMonoTime))
      if sample is not None and sample[1]:
        self.raw_projections[sample[0]] = (sample[2], self.integrity_epoch, self.session)
      while len(self.raw_projections) > MAX_COMPANIONS:
        del self.raw_projections[min(self.raw_projections)]
    else:
      self.integrity_epoch += 1
      self.verified_sm_projection = None
      self.raw_projections.clear()
      self.samples.clear()
      if self.latest is not None:
        self.latest = (self.latest[0], False, self.latest[2])
        self.samples[self.latest[0]] = self.latest
      self.last_rejection = 'drain_overrun'
      return False
    self.last_drain_ok = True
    return True

  def _reject(self, reason):
    self.last_rejection = reason
    return None

  def current(self, sm, *, source_ns, now_ns, drain=True):
    self.last_rejection = None
    if drain:
      self.drain()
    self._remember_profiles(now_ns)
    if not self.last_drain_ok:
      return self._reject('drain_overrun')
    projection = None
    if sm.seen.get(INTENT_SERVICE, False):
      stamp = int(sm.logMonoTime[INTENT_SERVICE])
      raw_verified = self.raw_projections.get(stamp)
      raw_current = bool(raw_verified is not None and raw_verified[0].settingsQualified and
        raw_verified[0].producerSessionId and 0 < raw_verified[0].observedMonoTime <= now_ns <=
        raw_verified[0].validUntilMonoTime)
      verified = ((stamp, *raw_verified) if raw_current else self.verified_sm_projection)
      if (verified is not None and verified[0] == stamp and verified[2] == self.integrity_epoch and
          verified[3] == self.session and sm.valid.get(INTENT_SERVICE, False) and
          sm.alive.get(INTENT_SERVICE, False) and 0 < stamp <= now_ns <= stamp + SAFETY_MAX_AGE_NS and
          (stamp < source_ns or (source_ns == 0 or source_ns == stamp) and
           0 < stamp <= now_ns <= stamp + SAFETY_MAX_AGE_NS) and
          now_ns - stamp > INTENT_MAX_AGE_NS and decode_intent(sm[INTENT_SERVICE]) == verified[1]):
        # This exact previously accepted SM identity contributes restrictions
        # only; it never replaces the fresh raw source pair or latest sample.
        projection = (stamp, True, verified[1])
      else:
        self._observe(stamp, sm.valid.get(INTENT_SERVICE, False), sm[INTENT_SERVICE])
    self._remember_profiles(now_ns)
    for stamp in tuple(self.samples):
      if not 0 < stamp <= now_ns or now_ns - stamp > INTENT_MAX_AGE_NS:
        del self.samples[stamp]
    for stamp in tuple(self.raw_projections):
      if not 0 < stamp <= now_ns or now_ns - stamp > SAFETY_MAX_AGE_NS:
        del self.raw_projections[stamp]
    if not (sm.seen.get(INTENT_SERVICE, False) and sm.valid.get(INTENT_SERVICE, False) and
            sm.alive.get(INTENT_SERVICE, False)):
      return self._reject('sm_unavailable')
    pair = self.samples.get(source_ns)
    sm_sample = projection if projection is not None else self.samples.get(int(sm.logMonoTime[INTENT_SERVICE]))
    if pair is None or self.latest is None or sm_sample is None:
      return self._reject('missing_pair_latest_or_sm')
    fresh_samples = (pair, self.latest) if projection is not None else (pair, self.latest, sm_sample)
    for label, sample in zip(('pair', 'latest', 'sm'), fresh_samples, strict=False):
      if not self._fresh(sample, now_ns):
        stamp, valid, value = sample
        reason = ('invalid' if not valid or value is None else
                  'settings_or_session' if not value.settingsQualified or not value.producerSessionId else
                  'future' if stamp > now_ns or value.observedMonoTime > now_ns else
                  'nonpositive_stamp' if stamp <= 0 else
                  'expired')
        return self._reject(label + '_' + reason)
    if self.latest[0] < source_ns:
      return self._reject('latest_older_than_source')
    intent = pair[2]
    for sample in (self.latest, sm_sample):
      _, _, latest = sample
      if latest.producerSessionId != intent.producerSessionId:
        return self._reject('producer_session_mismatch')
      intent = restrict_intent(intent, latest)
    intent = replace(intent, madsButtonsConsistent=intent.madsButtonsConsistent and self.mads_profile_consistent)
    if projection is None:
      # Save only after every ordinary fresh pair/latest/SM guard succeeded.
      self.verified_sm_projection = (sm_sample[0], sm_sample[2], self.integrity_epoch, self.session)
    return intent
