# ESCC active-owner specification: classic 2023 Sonata Hybrid

Status: **specification, qualification harness and a present/unqualified dashcam guard. Active
ESCC control is not implemented.**
Baseline: StarPilot `ccdddea522ebc36d7caf4f40f0115c60459783af`. Target: classic CAN
`HYUNDAI_SONATA_HYBRID` (Sonata Hybrid 2020–23) only. CAN-FD, Palisade, non-SCC, legacy and
mixed topologies are out of scope.

This document says what an ESCC owner must do before Dom may send host longitudinal commands
on a car with an inline ESCC interceptor. It also records why active control remains
unqualified. The passive decoder contract in `docs/research/escc-passive-decoding.md` supplies
offline packet handling; it grants no startup or actuation authority.

## 1. Current behavior on an ESCC installation

Dom has no ESCC control path. It has one present/unqualified guard (§4).
`opendbc/car/hyundai/tests/test_escc_active_qualification.py` exercises the public interfaces
and `openpilot/starpilot/tests/test_escc_present_guard.py` drives the actual `Car` constructor.
Together they record the following facts. **None of them shows that Dom is safe to drive
with the interceptor installed.**

| Fact (tested) | Consequence on an ESCC car |
| --- | --- |
| Guard: for exactly `HYUNDAI_SONATA_HYBRID`, when alpha long would be selected and `0x2AB` (any DLC) is in the bus 0 fingerprint, `CarInterface.get_params` returns `dashcamOnly`, `openpilotLongitudinalControl=False`, `pcmCruise=True` and a single `noOutput` safety config. `alphaLongitudinalAvailable` stays set so the unsupported request remains visible. | No LONG word, startup owner or controller output is authorized. The car is dashcam/no-output; this is not an automatic stock-ACC fallback. |
| Guarded CP: `startup_required` is false and `startup_owner(..., requested=True)` returns `None`, including the final blended fallthrough. `CarInterface.init` sends no radar disable. Through the actual `Car` constructor, the factory sees only the guarded CP, no `10 03`/`28 83 01`/keeper/DID request or frame is sent, the published CP is passive `noOutput`, `Card.step` never reaches the controller, and a saved MADS choice qualifies no AOL profile. | The radar-disable path is not reachable when `0x2AB` is seen while fingerprinting. Existing startup NoControl/dashcam reporting applies; no new event or toggle exists. |
| Detection limit: without `0x2AB` the ordinary LONG path is unchanged. `startup_owner` selects `OrdinaryClassicStartup` at `0x7D0` bus 0 and `CarInterface.init` sends `28 83 01` (disable RX/TX) to `0x7D0`. | A missed or late interceptor still gets the radar-disable attempt. Firmware `30cdcd9c` suggests that the interceptor forwards this traffic, but acceptance on the installed ECU and resulting FCA/AEB behavior are not bench-verified. This remains a stock FCA/AEB risk, not a measured physical outcome. |
| The ordinary LONG `hyundaican.create_acc_opt` builder still emits a host `FCA12` with `FCA_USM=1` ("AEB disabled") on bus 0. The guarded CP never reaches it through `Card`. | Only reachable on the unguarded (missed-detection) path, where it would compete with the stock FCA12 that the interceptor forwards. |
| With alpha long off (stock ACC), release or development: CarParams are identical with or without `0x2AB` (also with MADS requested); no startup owner, and `init` sends no radar disable. | The guard leaves this path unchanged. It does not establish that the interceptor's physical fallback, stock ACC, stock FCA or MADS behavior is qualified. |
| `0x2AB` on bus 1 or 2, other candidates (`HYUNDAI_SONATA`, `HYUNDAI_ELANTRA_HEV_2021`, `HYUNDAI_PALISADE`) and alpha long off: CarParams unchanged. | The guard is scoped to the exact classic Sonata Hybrid alpha-long selection. |
| No `SafetyModel` can carry ESCC (`@40` is unassigned). The donor `ESCC=1` bit is `EV_GAS` in Dom and is rejected by `native_accepts`. | Activation cannot be negotiated by accident. |

Native counterpart: `opendbc/safety/tests/test_hyundai_escc_qualification.py` (real libsafety).
The test engages through a genuine physical SET for both the unmarked word `0x006` and the AOL
word `0xC06`; it never forces `controls_allowed`. It then shows:

- Fresh `0x2AB` packets with nonzero FCA11/SCC12 emergency fields change nothing. Every SCC12
  `AEB_CmdAct`/`CR_VSM_DecCmd`/`AEB_StopReq` and every FCA11 actuation field is still rejected,
  while an ordinary SCC12 frame is still admitted.
- `0x2AB` traffic never engages longitudinal control and never re-arms it after a cancel.
- The host cannot transmit `0x2AB` on any bus.
- Once the Hyundai profile is published, only tester present may reach `0x7D0`. The disable
  transaction is admitted only in the pre-publication `elm327` diagnostic mode.
- Selecting mode 40 fails. That leaves no config, no controls and no AOL permission (firmware then
  falls back to SILENT in `panda/board/main.c`).
- Marking the word with the donor bit (`0xC07`) selects no AOL policy and opens no emergency
  exception.

Mutation checks were run once and the changes reverted:
- Weakening the native `aeb_req` rejection makes the emergency and donor tests fail.
- Removing the present/unqualified guard fails the guarded-profile, owner, any-DLC and actual
  `Car` tests, while the scope and unchanged-path tests still pass.

The tripwires exist so that any later change to this behavior has to be deliberately
reclassified.

## 2. Interceptor identity (dated evidence, conditional)

Local-only development record, not included in this repository:
`~/.t3-nightly/scratch/2026-10-18-what-is-the-stable-working-9e97075e/escc-review/`
(`manifest.json`, `branch-comparison.html`, `flashes/*/after.json`). The record was made on
**2026-10-18**. It is not a current read, and no device was contacted for this work.

- On that date, the in-car interceptor's 128-byte firmware signature matched a clean,
  reproducible build. The non-diagnostic build does not match.
  - Board: red panda, STM32H725.
  - Source: `alanbixby/escc-panda` at `30cdcd9cc97ed6b49d2824723d08266c26539d1f` (branch
    `escc-fca-diag-g-radar-link-gate`).
  - Build: `scons --escc --minimal`, `ESCC_DIAG=1`, arm-none-eabi-gcc 13.2.1.
  - Image SHA-256: `c2b27b0f37df5d43c82ace5aadf6698aaf8f60e278576fe78fb6ca391092da29`.
  - Signature SHA-256: `68298338bb9a492180c495441b8551cd3e3530fb1d87cf121dfcfd36bbfc4504`.
  - Reported version: `DEV-30cdcd9c-DEBU`.
- Limits of this evidence:
  - The match is a signature comparison only; the MCU flash was not dumped.
  - The serial number of the in-car unit was not recorded.
  - Three spare H725 units were flashed to the same image that evening.
  - No later record was found.
- Route 228 (2026-09-27) predates this record. Its behavior cannot be attributed to `30cdcd9c`.
- Because `ESCC_DIAG=1` is set, the build may also emit optional `0x2AD` diagnostics. These are
  not identity, health or emergency-freshness evidence.

**Requalification rule:** an active owner may name `30cdcd9c`/`ESCC_DIAG=1` as its single
qualified interceptor build only after a fresh read-only signature read, with the unit serial
recorded, matches the values above. A missing, different or unreadable identity must withhold
ESCC longitudinal takeover and report why (§3, "Present-unqualified"). It does not authorize an
automatic stock-ACC fallback. Nothing in `0x2AB` carries identity, so the host cannot
verify the build at runtime. The installed identity is an installation precondition, not a
runtime proof.

## 3. Active-owner contract (to implement once §5 closes)

**Ownership.** Add a new `EsccStartup` owner, selected in `CarInterface.startup_owner` before
`OrdinaryClassicStartup`, and only for the exact classic Sonata Hybrid with ESCC present.

**Startup state machine:**

| State | Entry condition | Host actions |
| --- | --- | --- |
| Absent | No `0x2AB` on bus 0 seen during fingerprinting (the §4 guard treats any DLC as present) | Existing behavior, unchanged. |
| Present-unqualified | `0x2AB` seen, but the installed identity or another §5 gate is unproven | Withhold ESCC longitudinal takeover and report the reason. No ESCC LONG profile, host SCC11–14/FCA12/FRT_RADAR11, or radar-disable/keeper/DID traffic may be authorized from this unqualified state. Stock-ACC fallback requires a separately validated mode-selection and handback policy. This row does not implement such a fallback. |
| Qualified-ready | All gates pass and the passive decoder shows a fresh link (packet age only) | Dedicated ESCC profile (below). SCC takeover begins only when controls are ready. |
| Active | Native acknowledgment of the ESCC profile, plus a physical engagement | SCC11/12/13/14 with ordinary limits. Stock FCA11/FCA12 pass through untouched. |
| Lost | Packet age exceeds the caller timeout, or the frame is malformed, out of order, or affected by a clock reset | Withdraw longitudinal through native safety and alert. Stop host SCC only under the validated handback policy (§5 gate 4). No automatic re-entry; a new physical engagement is required. |

**Invariants on every ESCC path:**
- Zero `10 03`, `28 83 01` and `10 07` diagnostic requests, zero `0x2E` writes, and no
  tester-present keeper.
- No host `FCA11`, `FCA12`, `FRT_RADAR11` or `0x2AB` frames.
- SCC12 emergency fields are always zero. This means:
  - no OR or max combination of FCA11 and SCC12 demand;
  - no forced `AEB_Status`;
  - no native emergency exception justified by `0x2AB` liveness. The packet caches emergency
    fields with no per-source age; an actual-C reproduction on 2026-10-18 showed cached FCA
    state persisting 60 s and across safety re-initialization.
- Lead source: exactly one declared source per drive. Either qualified raw Mando tracks, or the
  passive `EsccOfflineRadar` single lead promoted by a reviewed selection. Never a silent mix.
- Unchanged: ordinary `HYUNDAI_LONG_LIMITS`, steering limits, driver monitoring, brake/gas/cancel
  behavior, physical re-arm, `ALLOW_DEBUG`/development admission, and the default MADS axis
  contract owned by the MADS lane. No bypass flag and no firmware-identity spoofing.

**Transport.** Revalidated at `ccdddea`:
- `SafetyModel` ends at `teslaPreap @39` and `declarations.h` ends at `SAFETY_TESLA_PREAP 39U`.
  `@40` is still the first free value; this is a fork-local proposal, not an upstream reservation.
- `CarParams.flags` (UInt32) has no free Hyundai bits.
- `safetyParam` is UInt16. Adding bits there collides with the exact AOL word predicates in
  `classic_long_aol.py` and `aol_policy.h`.

Recommendation: a dedicated `SafetyModel.hyundaiEscc @40`.
- Python side: `car.capnp`, plus `classic_long_aol`-style exact qualification in a new
  `escc_aol.py`.
- Native side: `declarations.h`, `safety.h` registry under the existing debug gate, and a new
  `safety/modes/hyundai_escc.h`. That mode wraps the shared classic hooks with an ESCC TX
  allowlist: no `0x7D0` and no `0x38D`/`0x483`.
- Negotiation: a new pandad `AolSafetyProfile` in `openpilot/starpilot/car/hyundai/aol_policy.h`
  and in the `pandad.cc` profile list.

Old firmware that does not know model 40 already fails closed (see the native test above).

**Proposed change contract for shared files.** The parent assigns these; nothing is edited here.

| File | Owner today | ESCC change |
| --- | --- | --- |
| `car/hyundai/interface.py` | parent | ESCC presence → Present-unqualified decision (dashcam veto implemented, §4); owner dispatch before ordinary classic |
| `car/hyundai/ordinary_classic_startup.py` | parent | `required()` excludes ESCC-present CPs (one predicate) |
| `car/hyundai/carcontroller.py`, `hyundaican.py` | HUD lane | ESCC CP: skip keeper, `create_acc_opt` FCA12 and `create_frt_radar_opt` |
| `car/car.capnp`, `pandad.cc`, `aol_policy.h` | parent / MADS (display schema) | `hyundaiEscc @40` and its native profile |
| `car/hyundai/escc.py`, DBC, parser tests | passive lane | consumed read-only |
| new `escc_startup.py`, `escc_aol.py`, `safety/modes/hyundai_escc.h`, their tests | ESCC active lane | — |

## 4. Present/unqualified guard

`CarInterface._get_params` vetoes the exact classic `HYUNDAI_SONATA_HYBRID` alpha-long selection
when `0x2AB` is in the bus 0 fingerprint at any DLC. The result is `dashcamOnly`, no openpilot
longitudinal, `pcmCruise` and a single `noOutput` safety config. `alphaLongitudinalAvailable` is
left as computed so the unsupported request stays visible. This is the §3 "Present-unqualified"
row reduced to a dashcam veto.

Placement matters. `Card`'s `pre_create_hook` calls the startup factory before the car interface
is constructed, and `OrdinaryClassicStartup` would send `10 03` then `28 83 01` to `0x7D0` from
there. Withholding after controls are ready would be too late. `_get_params` sees the
fingerprint before any startup owner is chosen. `vehicle_preferences` prepare/finalize and the
startup factory do not restore the guarded LONG profile or an owner; the actual `Car` test checks
this.

What it does not do:
- It implements no ESCC owner, no emergency forwarding, no native safety exception and no
  automatic stock-ACC or hardware fallback. Nothing here verifies stock ACC with the
  interceptor.
- It is a positive-presence veto only. A missing or late `0x2AB` does not prove absence and does
  not qualify the ordinary LONG path. If the interceptor boots late, the ordinary path still
  attempts the radar disable (§1, detection limit). A second check inside a future owner, before
  any disable transaction, would narrow that gap.
- It does not qualify driving with the interceptor. Two distinct test modes are not active
  control: mode A, passive logging with the openpilot toggle off; mode B, a negative check that
  the guard engages (dashcam, no output) with alpha long requested. Neither qualifies driving nor
  active MADS with ESCC.
- It adds no event, toggle or parameter. The existing startup NoControl/dashcam reporting is the
  user-visible signal.

The alpha-long-off stock path is unchanged. The interceptor's physical fallback, stock ACC and
stock FCA behavior on that path remain unqualified. §5 gates are untouched.

## 5. Gates that block activation

1. **Emergency freshness.** Legacy `0x2AB` carries no per-source age, counter or integrity check,
   so the host cannot detect stale cached FCA11/SCC12 data. To close this gate, either:
   - prove that stock FCA pass-through alone is authoritative on this FCA vehicle, so SCC12
     reconstruction is never needed (bench/HIL with nonzero stock requests); or
   - ship a reviewed firmware/protocol revision that carries per-source freshness and validity.
2. **Installed identity.** A fresh signature read with the unit serial (§2).
3. **Physical fallback.** Relay, power-loss and hung-MCU behavior of the Sonata harness, and
   which stock FCA source sits on the intercepted spur. The published pinout is for the Ioniq.
4. **Handback.** Measured 150 ms takeover/release behavior on the installed build, and stopping
   SCC without returning ACC to a radar in an unknown state.
5. **Nonzero AEB evidence.** A controlled bench/HIL trace with a nonzero stock emergency request.
   Route 228 showed an FCW only.

Host and source tests, including this harness, cannot close gates 1–5 and do not qualify the
vehicle.

## 6. Checks

Run from the repository root with the task-local runner (`.host_verification/run`, a copy of the
host-verification lane's runner):

```
.host_verification/run opendbc_repo/opendbc/safety/tests/test_hyundai_escc_qualification.py \
  opendbc_repo/opendbc/car/hyundai/tests/test_escc_active_qualification.py \
  openpilot/starpilot/tests/test_escc_present_guard.py
```

The actual-`Car` test needs the host native Params/IPC libraries (`.host_verification/run
--build`).
