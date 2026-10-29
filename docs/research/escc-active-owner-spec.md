# ESCC active-owner specification: classic 2023 Sonata Hybrid

Status: **specification and qualification harness only. Active ESCC control is not implemented.**
Baseline: StarPilot `ccdddea522ebc36d7caf4f40f0115c60459783af`. Target: classic CAN
`HYUNDAI_SONATA_HYBRID` (Sonata Hybrid 2020–23) only. CAN-FD, Palisade, non-SCC, legacy and
mixed topologies are out of scope.

This document says what an ESCC owner must do before Dom may send host longitudinal commands
on a car with an inline ESCC interceptor. It also records why that is not yet safe to build. It
builds on the earlier scope report (`scope/escc-integration`,
`docs/research/escc-integration-plan.md`) and the passive decoder contract
(`docs/research/escc-passive-decoding.md`, passive ESCC lane).

## 1. Current behavior on an ESCC installation

Dom has no ESCC path. `opendbc/car/hyundai/tests/test_escc_active_qualification.py` exercises
the current public interfaces and records the following facts. **None of them shows that
unmodified Dom is safe with the interceptor installed.**

| Fact (tested) | Consequence on an ESCC car |
| --- | --- |
| `CarInterface.get_params` returns identical CarParams with or without `0x2AB`/DLC 8 on bus 0. The word stays the ordinary LONG `0x0006`. | Dom cannot tell that the interceptor is installed. |
| With alpha long, `startup_owner` selects `OrdinaryClassicStartup` at `0x7D0` bus 0. `CarInterface.init` sends `28 83 01` (disable RX/TX) to `0x7D0`. | Dom silences the radar. The interceptor forwards car-side traffic to the radar (source inference from firmware `30cdcd9c`; not bench-verified), so **stock FCA/AEB is lost**. That loss is the reason the interceptor was installed. |
| With alpha long, `hyundaican.create_acc_opt` sends a host `FCA12` with `FCA_USM=1` ("AEB disabled") on bus 0. | If the radar were left alive, this frame would compete with the stock FCA12 that the interceptor forwards. |
| With alpha long off (stock ACC), whether a release or development build: no startup owner, and `init` sends no radar disable. | **This is the only current path that sends no radar-disable traffic.** Stock ACC and the forwarded stock FCA stay in charge. |
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
- Adding a naive "withhold LONG when `0x2AB` is present" guard trips the two current-path
  tripwires.

The tripwires exist so that this behavior change has to be deliberately reclassified.

## 2. Interceptor identity (dated evidence, conditional)

Source: `~/.t3-nightly/scratch/2026-10-18-what-is-the-stable-working-9e97075e/escc-review/`
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
  - Three spare H725 units (`2700200…`, `2900200…`, `35001f00…`) were flashed to the same image
    that evening.
  - No later record was found.
- Route 228 (2026-09-27) predates this record. Its behavior cannot be attributed to `30cdcd9c`.
- Because `ESCC_DIAG=1` is set, the build may also emit optional `0x2AD` diagnostics. These are
  not identity, health or emergency-freshness evidence.

**Requalification rule:** an active owner may name `30cdcd9c`/`ESCC_DIAG=1` as its single
qualified interceptor build only after a fresh read-only signature read, with the unit serial
recorded, matches the values above. A missing, different or unreadable identity must select
stock longitudinal (§3, "Unknown"). Nothing in `0x2AB` carries identity, so the host cannot
verify the build at runtime. The installed identity is an installation precondition, not a
runtime proof.

## 3. Active-owner contract (to implement once §5 closes)

**Ownership.** Add a new `EsccStartup` owner, selected in `CarInterface.startup_owner` before
`OrdinaryClassicStartup`, and only for the exact classic Sonata Hybrid with ESCC present.

**Startup state machine:**

| State | Entry condition | Host actions |
| --- | --- | --- |
| Absent | No valid `0x2AB` (standard ID, bus 0, DLC 8) seen during fingerprinting | Existing behavior, unchanged. |
| Present-unqualified | `0x2AB` seen, but the installed identity or another §5 gate is unproven | **Stock longitudinal.** No LONG word, no SCC11–14, no FCA12/FRT_RADAR11, no `0x7D0` disable, keeper or DID write. Report the reason. This is a fail-closed CarParams decision, not a fallback to radar disable. |
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
| `car/hyundai/interface.py` | parent | ESCC presence → Present-unqualified decision; owner dispatch before ordinary classic |
| `car/hyundai/ordinary_classic_startup.py` | parent | `required()` excludes ESCC-present CPs (one predicate) |
| `car/hyundai/carcontroller.py`, `hyundaican.py` | HUD lane | ESCC CP: skip keeper, `create_acc_opt` FCA12 and `create_frt_radar_opt` |
| `car/car.capnp`, `pandad.cc`, `aol_policy.h` | parent / MADS (display schema) | `hyundaiEscc @40` and its native profile |
| `car/hyundai/escc.py`, DBC, parser tests | passive lane | consumed read-only |
| new `escc_startup.py`, `escc_aol.py`, `safety/modes/hyundai_escc.h`, their tests | ESCC active lane | — |

## 4. A smaller, independently safe step (decision needed)

The **Present-unqualified** row can ship before any active control. It needs a few lines in
`interface.py` and is mechanically simple: every radar-disable path (owner, `init`, controller
keeper, FCA12) is already gated on `CP.openpilotLongitudinalControl`.

Effect on this car with alpha long enabled: Dom falls back to stock ACC and stock AEB (forwarded
by the interceptor), with MADS lateral unchanged. Today, the same configuration disables the
radar and loses AEB.

This reduces authority, but it changes what the driver experiences: openpilot longitudinal is
withheld. That is a user/parent decision. It is therefore not implemented in this slice. The
tripwire tests are designed to fail and be reclassified when it lands.

Detection limitation: the check relies on `0x2AB` being present while fingerprinting runs. If
the interceptor boots late, today's behavior results. A second check inside the deferred owner,
before the disable transaction, would close that gap.

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
  opendbc_repo/opendbc/car/hyundai/tests/test_escc_active_qualification.py
```
