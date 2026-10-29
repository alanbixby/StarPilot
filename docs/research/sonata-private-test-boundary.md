# Private Sonata parity test boundary

This branch combines the fork's Sonata MADS buttons, factory cluster display,
offline ESCC decoder and qualification tests. It is a source-build testing
branch. It is not an ESCC control implementation or a vehicle qualification.

## Build and identity

The inherited `prebuilt` marker and its stale receipt are removed deliberately.
The normal launcher must finish `build.py` before starting manager. In
particular, the native Params registry must include `SonataMadsButtons`, the
custom schema outputs must match the source, and panda firmware must match the
native safety source. A host test build does not establish any of these on the
comma. Do not copy host `.so`/`.a` outputs onto it or add a marker to skip the build.

Use the exact reviewed commit on the private fork, record the resulting device
build and panda firmware identities, and preserve the existing checkout and
configuration backup. This branch does not require a userdata reset. Verify
Wi-Fi and hotspot reconnection after the operator's installation; this source
work has not inspected or changed device networking.

## Feature selection

The saved **Sunnypilot MADS Buttons** setting defaults off. Its effective profile
requires **Enable Always On Lateral**, the exact classic Sonata Hybrid with LDA,
and the qualified openpilot-longitudinal owner. Turning the AOL master off
restores ordinary button behavior and saved wheel/brake assignments; it preserves
the stored profile choice. Restart the vehicle processes after changing startup
settings.

In the qualified profile, LFA toggles steering independently. MAIN controls its
software master; SET/RES requires MAIN on and successful cruise engagement.
CANCEL and default braking withdraw longitudinal control while retaining armed
steering. Final lateral activity controls the active factory icons; fresh joined
intent can show a paused white wheel. Display facts grant no actuation authority.

## Installed ESCC and ignition-on tests

For the exact Sonata Hybrid, a bus-0 `0x2AB` address in the fingerprint vetoes a
requested alpha-long profile. The result is dashcam/passive mode with `noOutput`
safety, no startup owner, and no host control output. A guard check should reach
that result, rather than MADS or factory HUD control. Existing cold-cache,
read-only firmware queries are separate from the blocked startup writes.

**Absence of `0x2AB` does not prove absence of the interceptor.** If its message
is late or missing during fingerprinting, the ordinary alpha-long path remains
available and attempts radar-disable traffic and synthetic FCA configuration.
Start observation with the openpilot toggle off and alpha long off. A positive
guard check can establish detection timing and no-output behavior; it cannot
establish active-control readiness. Record first `0x2AB`, fingerprint completion,
and published CarParams across ignition cycles before relying on detection.

The requested active-control test with the installed interceptor remains outside
this branch's established boundary. The exact MADS profile requires openpilot
longitudinal; no stock-long MADS profile or automatic stock fallback is supplied.
The five gates in [the active-owner specification](escc-active-owner-spec.md)
remain open: current installed identity, emergency-source freshness and FCA
authority, physical relay/topology fallback, handback timing, and nonzero AEB
evidence. Removing the interceptor would create a different hardware
configuration, which these results also do not qualify.

## Safety comparison and evidence

The comparative Sunnypilot source is
`0b2c431d3abab630df0a32f53236944855a91aa3`. The actual pre-migration device
source `a440c4242644fc614b485191a27be9b7884f79ca` was unavailable; comparison
to that installed tree is not established.

The audit requires preserving driver monitoring and excessive-actuation checks,
actuator limits, driver override and the complete native safety suite. The
existing `0xC06` profile is tightened to require bus-0 BCM `0x391`, length 8,
at 50 Hz. A gap longer than 200 ms withdraws authority on native evaluation;
returning traffic cannot restore old SET proof. Recovery requires healthy
neutral and a fresh gesture. This requirement applies to every platform sharing
that word. No new safety word or emergency-command exception is supplied.

Qualification records must identify the exact source, native artifact hashes,
collection accounting and skips for the canonical coverage, opendbc MISRA,
panda MISRA and panda host gates, plus the separate RELEASE plan. Keep earlier
failed and interrupted logs; do not combine rerun counts into a claimed single
full run. Source review and host tests do not establish physical behavior.

The branch must retain truthful StarPilot/fork identity and the obligations in
[SAFETY.md](../SAFETY.md). Sunnypilot parity is not an exemption from those
requirements, and a local review cannot guarantee comma server-policy clearance.
