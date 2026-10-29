# Legacy ESCC passive decoding

This change adds an explicitly constructed offline telemetry receiver and the
classic Hyundai ESCC wire declaration. It changes no vehicle detection, runtime
radar selection, startup, CAN output, safety profile, authorization or limits.
`EsccOfflineRadar` is not imported by any runtime owner. The runtime Sonata Hybrid
factory continues to select raw Mando tracks on bus 1 when present; adding only
bus-0 `0x2AB` does not make radar available or alter CarParams.

## Pinned protocol and corrections

The functional wire layout is pinned to Sunnypilot
`0b2c431d3abab630df0a32f53236944855a91aa3`. Sanitized synthetic fixture bytes,
expected physical values, exact source paths, URLs and SHA256 hashes are in
`opendbc_repo/opendbc/car/hyundai/tests/fixtures/escc_legacy.json`. These are not
captured route frames. The downloaded reference files were hash-checked against
their manifest before implementation. The decoder and offline lifecycle are newly
written; donor runtime implementation is not copied. The pinned donor repository
also carries a Custom MIT License in `opendbc_repo/LICENSE.md`; this work makes no
claim to relicense donor extensions or authorize future copying of control code.

Only standard 11-bit CAN `0x2AB`, bus 0, DLC 8 is decoded. Extended and CAN-FD
frames are rejected even when their numeric address and payload length match.
The caller must preserve those transport format flags. Integers in the wire
layout are unsigned little-endian:

| Signal | Start:length | Scale, offset |
| --- | --- | --- |
| FCA_CmdAct | 0:1 | 1, 0 |
| CF_VSM_Warn_FCA11 | 1:2 | 1, 0 |
| AEB_CmdAct | 3:1 | 1, 0 |
| CF_VSM_Warn_SCC12 | 4:2 | 1, 0 |
| CF_VSM_DecCmdAct_SCC12 | 6:1 | 1, 0 |
| CF_VSM_DecCmdAct_FCA11 | 7:1 | 1, 0 |
| CR_VSM_DecCmd_SCC12 | 8:8 | 0.01 g, 0 |
| ObjValid | 16:1 | 1, 0 |
| ACC_ObjStatus | 17:2 | 1, 0 |
| ACC_ObjLatPos | 24:9 | 0.1 m, -20 m |
| ACC_ObjDist | 33:11 | 0.1 m, 0 |
| ACC_ObjRelSpd | 44:12 | 0.1 m/s, -170 m/s |
| CR_VSM_DecCmd_FCA11 | 56:8 | 0.01 g, 0 |

Bits 19–23 are unspecified and ignored. They are not a protocol version, counter
or checksum. The Palisade declaration is incompatible and is unchanged: the
fixture `7d2503c8bca16753` represents 22.2 m and -4.2 m/s here, but Palisade decodes
122.4 m and +25.3 m/s. The declaration belongs in the source template
`opendbc_repo/opendbc/dbc/generator/hyundai/hyundai_can.dbc`. The current parser
generates `hyundai_can_generated` in memory through `generate_all()`; generated
output is not committed or edited manually.

The exact deployed donor first constructs its raw-radar parser and initializes
the extension only when that parser is absent. An ESCC flag therefore does not
prove the deployed planner used an ESCC lead. Its generic raw Mando parser uses
bus 1 and the `0x500`–`0x51F` bank. This offline receiver deliberately does not
implement source preference, fallback or fusion.

## Offline interface contract

Construct `EsccOfflineRadar(timeout_ns=analysis_timeout_ns)` explicitly. The
positive integer timeout is a caller-owned analysis policy. There is no production
default: the evidence does not qualify one. Regression tests use the arbitrary
value 123456789 ns to avoid implying a production policy or borrowing the
interceptor's takeover timer. A packet remains fresh at exactly the supplied
timeout and expires one nanosecond later.

Call `update(now_ns, frames=())` on every analysis tick, including ticks without
CAN. `EsccCanFrame(timestamp_ns, address, data, bus, is_extended=False,
is_fd=False)` uses the same monotonic capture clock as `now_ns`. Wrong format,
bus, ID or length never reaches CANParser and never refreshes packet liveness.
Future, negative, repeated or reordered frame timestamps are rejected. A capture
clock rewind clears the cache and discards that call's ambiguous batch; a
subsequent call can reacquire in the new clock epoch. A newly constructed receiver
starts empty. Identical payloads with newer timestamps remain acceptable traffic
because this protocol has no sequence/source-integrity evidence.

Each immutable observation contains:

- `source == 'legacyEscc2ab'`: explicit source identity for analysis, not an
  authenticated producer or a runtime source selection.
- `last_packet_ns` and `packet_age_ns`: last accepted transport timestamp and
  elapsed capture-clock time, retained after expiry for diagnostics.
- `packet` and `transport_fresh`: decoded wire telemetry only while fresh.
- `lead`: present only while fresh and both `ObjValid` and `ACC_ObjStatus` are
  nonzero. A valid packet with either lead flag clear refreshes transport age but
  immediately clears the old lead. Nonzero status values are preserved without
  inventing an enum interpretation. Distance sentinels from other protocols are
  not applied to classic ESCC.
- `lead.track_id`: stable during continuous observed presence; renewed after an
  invalid lead, timeout or clock reset. This is host lifecycle identity, since
  the packet carries no physical object ID. Changes of physical object without
  an observed invalid interval cannot be distinguished.
- `lead.lateral_position_m`: unprojected wire value. A vehicle-frame sign or
  projection into `RadarPoint.yRel` is not qualified here. The deployed donor
  emits NaN lateral position; this adapter does not silently substitute an axis.
- `emergency_source_age_ns is None`: always unknown, including fresh packets
  containing nonzero emergency requests.

`EsccDecoder.decode(frame)` is also available for envelope-checked interpretation
of individual packets through the real generated CANParser. It
does not enforce ordering or freshness; the offline receiver adds that policy.

## Separate active-owner boundary

No packet field is an outgoing command or permission to steer/accelerate. Legacy
`0x2AB` alone cannot prove underlying FCA11/SCC12 freshness: the interceptor can
continue sending fresh SCC11-triggered packets with cached emergency fields.
Transport liveness, lead validity, stock warning evidence and emergency-source
health are separate claims. Nonzero AEB copying remains unproven. Emergency
deceleration uses g, not the ordinary SCC12 acceleration units.

A later active proposal must independently declare the runtime lead source and
failure policy, verify installed firmware/protocol identity, preserve native
fail-closed authorization with no automatic rearm or expanded limits, and close
startup/output ownership and emergency-source provenance. This passive receiver
does not solve those gates. Source and host tests establish offline behavior only;
they do not establish replay, bench, firmware or on-car qualification.

## Regression evidence

On baseline `ccdddea522ebc36d7caf4f40f0115c60459783af`, constructing
`CANParser('hyundai_can_generated', [('ESCC', 50)], 0)` fails with
`RuntimeError: could not find message 'ESCC' in DBC hyundai_can_generated`.
The new decoder tests also initially fail collection because the module is absent.

The fixtures were produced without CANPacker or CANParser. Independent literal
single-field wire vectors additionally check every signal boundary without
adjacent-field contamination. Tests exercise actual generator output, parser and
packer, positive/negative scaling, extremes, reserved bits, incompatible Palisade
decoding, malformed envelopes, timestamp faults, missing-trigger expiry, lead
clearing/reacquisition, immutable snapshots and unchanged runtime source choice.

From the repository root, with a Python environment containing opendbc's test
dependencies, the focused checks are:

```sh
PYTHONPATH="$PWD:$PWD/opendbc_repo" python -B -m pytest -p no:cacheprovider -o addopts= -q \
  opendbc_repo/opendbc/car/hyundai/tests/test_escc_protocol.py \
  opendbc_repo/opendbc/car/hyundai/tests/test_escc_radar.py \
  opendbc_repo/opendbc/can/tests
```

Additional existing Hyundai factory/controller and raw-radar checks are recorded
in the implementation handoff. Card/IPC-dependent suites require native host
build artifacts; their availability is separate from these offline parser tests.
