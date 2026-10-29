"""Passive legacy ESCC telemetry for explicitly selected offline analysis only.

No vehicle detection, runtime source selection, CAN output or control authority.
The legacy packet contains no firmware identity, version, application checksum,
counter, or underlying FCA11/SCC12 source ages. Fresh packets cannot establish
emergency-source freshness. See tests/fixtures/escc_legacy.json for pinned wire
provenance and docs/research/escc-passive-decoding.md for the integration boundary.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from opendbc.can import CANParser


ESCC_ADDRESS = 0x2AB
ESCC_BUS = 0


@dataclass(frozen=True)
class EsccCanFrame:
  timestamp_ns: int
  address: int
  data: bytes
  bus: int
  is_extended: bool = False
  is_fd: bool = False


@dataclass(frozen=True)
class EsccPacket:
  # Wire signal names/units, deliberately separate from any outgoing command.
  FCA_CmdAct: int
  CF_VSM_Warn_FCA11: int
  AEB_CmdAct: int
  CF_VSM_Warn_SCC12: int
  CF_VSM_DecCmdAct_SCC12: int
  CF_VSM_DecCmdAct_FCA11: int
  CR_VSM_DecCmd_SCC12: float  # g, not ordinary acceleration in m/s^2
  ObjValid: int
  ACC_ObjStatus: int
  ACC_ObjLatPos: float  # legacy wire axis, not a qualified RadarPoint.yRel
  ACC_ObjDist: float  # m
  ACC_ObjRelSpd: float  # m/s
  CR_VSM_DecCmd_FCA11: float  # g


class EsccDecoder:
  """Decode the deployed classic layout through the real generated DBC parser.

  This validates the frame envelope only; it makes no liveness/identity claim.
  CANParser alone accepts short frames, so length/format checks must precede it.
  The caller must preserve CAN format flags; an address alone cannot distinguish
  an extended frame with numerical ID 0x2AB from its standard counterpart.
  """

  def __init__(self):
    # No frequency/timeout policy here: the offline receiver owns explicit age.
    self._parser = CANParser('hyundai_can_generated', [('ESCC', float('nan'))], ESCC_BUS)

  def decode(self, frame: EsccCanFrame) -> EsccPacket | None:
    if (frame.address != ESCC_ADDRESS or frame.bus != ESCC_BUS or frame.is_extended or frame.is_fd or
        len(frame.data) != 8 or type(frame.timestamp_ns) is not int or frame.timestamp_ns < 0):
      return None
    updated = self._parser.update([(frame.timestamp_ns, [(frame.address, frame.data, frame.bus)])])
    if ESCC_ADDRESS not in updated:
      return None
    values = self._parser.vl['ESCC']
    return EsccPacket(
      FCA_CmdAct=int(values['FCA_CmdAct']), CF_VSM_Warn_FCA11=int(values['CF_VSM_Warn_FCA11']),
      AEB_CmdAct=int(values['AEB_CmdAct']), CF_VSM_Warn_SCC12=int(values['CF_VSM_Warn_SCC12']),
      CF_VSM_DecCmdAct_SCC12=int(values['CF_VSM_DecCmdAct_SCC12']), CF_VSM_DecCmdAct_FCA11=int(values['CF_VSM_DecCmdAct_FCA11']),
      CR_VSM_DecCmd_SCC12=values['CR_VSM_DecCmd_SCC12'], ObjValid=int(values['ObjValid']), ACC_ObjStatus=int(values['ACC_ObjStatus']),
      ACC_ObjLatPos=values['ACC_ObjLatPos'], ACC_ObjDist=values['ACC_ObjDist'], ACC_ObjRelSpd=values['ACC_ObjRelSpd'],
      CR_VSM_DecCmd_FCA11=values['CR_VSM_DecCmd_FCA11'],
    )


@dataclass(frozen=True)
class EsccLead:
  track_id: int
  distance_m: float
  lateral_position_m: float  # unprojected legacy wire axis
  relative_speed_mps: float


@dataclass(frozen=True)
class EsccObservation:
  last_packet_ns: int | None
  packet_age_ns: int | None
  packet: EsccPacket | None
  lead: EsccLead | None
  source: str = 'legacyEscc2ab'
  # Transport age cannot prove how recently the interceptor saw FCA11/SCC12.
  emergency_source_age_ns: None = None

  @property
  def transport_fresh(self) -> bool:
    return self.packet is not None


class EsccOfflineRadar:
  """Opt-in single-lead analysis adapter; never selected by RadarInterface.

  timeout_ns is required and belongs to the caller's explicit analysis policy;
  there is no qualified production default. Poll update(now_ns) even when CAN
  stops. The caller supplies one monotonic capture clock for now and frames.
  The exact timeout boundary is inclusive, with expiry one nanosecond later.

  Valid envelope traffic refreshes packet age even without a lead. A lead needs
  both ObjValid and nonzero ACC_ObjStatus. Nonzero status values are not assigned
  an undocumented enum meaning. No range sentinel or lateral-axis transform is
  inferred from other radar layouts. Equal payloads at newer timestamps remain
  indistinguishable from a frozen source; neither authenticates emergency data.
  """

  def __init__(self, *, timeout_ns: int):
    if type(timeout_ns) is not int or timeout_ns <= 0:
      raise ValueError('timeout_ns must be a positive integer analysis policy')
    self.timeout_ns = timeout_ns
    self._decoder = EsccDecoder()
    self._last_update_ns: int | None = None
    self._last_packet_ns: int | None = None
    self._packet: EsccPacket | None = None
    self._lead: EsccLead | None = None
    self._next_track_id = 0

  def _expire(self, now_ns: int):
    if self._last_packet_ns is not None and now_ns - self._last_packet_ns > self.timeout_ns:
      self._packet = None
      self._lead = None

  def update(self, now_ns: int, frames: Iterable[EsccCanFrame] = ()) -> EsccObservation:
    if type(now_ns) is not int or now_ns < 0:
      raise ValueError('now_ns must be a nonnegative integer capture time')
    if self._last_update_ns is not None and now_ns < self._last_update_ns:
      # A new clock epoch cannot reuse old state or the current ambiguous batch.
      self._decoder = EsccDecoder()
      self._last_packet_ns = None
      self._packet = None
      self._lead = None
    else:
      for frame in frames:
        if (type(frame.timestamp_ns) is not int or not 0 <= frame.timestamp_ns <= now_ns or
            self._last_packet_ns is not None and frame.timestamp_ns <= self._last_packet_ns):
          continue
        packet = self._decoder.decode(frame)
        if packet is None:
          continue
        # Expire at each accepted timestamp too, so a gap inside a batch renews
        # identity even if the caller never polled during the missing interval.
        self._expire(frame.timestamp_ns)
        self._last_packet_ns = frame.timestamp_ns
        self._packet = packet
        if packet.ObjValid and packet.ACC_ObjStatus:
          if self._lead is None:
            track_id = self._next_track_id
            self._next_track_id += 1
          else:
            track_id = self._lead.track_id
          self._lead = EsccLead(track_id, packet.ACC_ObjDist, packet.ACC_ObjLatPos, packet.ACC_ObjRelSpd)
        else:
          self._lead = None
    self._last_update_ns = now_ns
    self._expire(now_ns)
    age = None if self._last_packet_ns is None else now_ns - self._last_packet_ns
    return EsccObservation(self._last_packet_ns, age, self._packet, self._lead)
