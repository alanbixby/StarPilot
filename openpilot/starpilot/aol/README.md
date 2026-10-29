# Independent lateral and longitudinal control

AOL connects Card button intent, SelfdriveD axis decisions, Controls output,
driver monitoring and acknowledged Panda permissions. Either control axis can
remain active independently while its permission is valid.

The shared owner handles settings, gestures, transport, freshness and axis
arbitration. Vehicle adapters in `starpilot/car/<brand>/aol.py` own supported
CarParams, safety profiles and button assignments. `policy.py` defines the
contract; `vehicle.py` dispatches to registered adapters. Unknown or malformed
configurations receive no capability. Settings availability, intent admission
and ordinary runtime support are separate: a saved preference cannot grant
steering or longitudinal permission. Current adapters do not cover the entire
vehicle catalog.

Card publishes desired intent; SelfdriveD resolves the two desired axes; Controls
requires matching current native permission before actuating either axis.
Session changes, expired messages, invalid required CAN and the existing
actuator limits still apply. Lateral-only operation uses engaged driver
monitoring independently of the Always On DM preference.

Intent and axis messages have a 30 ms freshness bound; native acknowledgments
have a 200 ms bound. The host and pandad use the same monotonic clock. A new
session first negotiates an acknowledged zero-axis request. These checks are
independent of each vehicle's Panda safety policy.

`aolAxisState` uses custom slot 16. Intent and safety use reserved Data events 125
and 124 with bounded, versioned flat Cap'n Proto payloads. Mapd slots 17–19 remain
separate. Decode stored messages with their writing schema; never reinterpret
an incompatible cache or reuse an occupied event identifier. `CarControl.lateralDisplay`
is an additive display-only vehicle-schema extension; it never grants actuator authority.

`AolBrakePauseSpeedMps` stores meters per second. If absent, `PauseAOLOnBrake`
retains its historical effective meters-per-second value, regardless of the old
UI label. An invalid threshold disables the additional AOL request without
rewriting the preference. Settings convert display units when writing the new
key. Invalid CAN or transfer of button ownership cancels an incomplete hold;
a deliberate axis pause remains latched until changed by the driver.

Common transport and arbitration tests live in `aol/tests`. Vehicle host tests
live in `selfdrive/car/tests`, and native safety tests in
`opendbc_repo/opendbc/safety/tests`. Keep new vehicle admission and its positive
and negative safety tests with the corresponding vehicle port.

## Sonata default MADS profile

`SonataMadsButtons` defaults off. The parked **Sunnypilot MADS Buttons** preference
is shown only for the qualified Sonata Hybrid classic CAN OP-long configuration
with a detected lane button. Card captures it in `CarParams.sonataMadsButtons`
before the existing LONG startup transaction. Other Hyundai profiles and the
existing defaults retain their behavior.

With the profile selected and AOL enabled, a fresh lane-button press toggles
steering independently of cruise MAIN. MAIN OFF→ON enables steering; ON→OFF
withdraws steering and ordinary longitudinal engagement. SET/RES release can
engage longitudinal and steering together, under the ordinary engagement and
previous-speed rules. CANCEL, default braking and accelerator input preserve
already armed steering. They cannot invent a new steering authorization.

The software MAIN latch starts off each process session. Neutral must be seen
before a fresh press; held-at-start or after expired CLU input does not turn it
on. TCS health remains a separate prerequisite. The profile uses the unchanged
native `0xC06`/alternative-experience `32` contract: physical LFA/MAIN/SET proof,
required RX health, host leases, heartbeat and current native acknowledgment
remain required. It changes no actuator limits or driver monitoring rules.
Native may retain an old physical token within a session, including ordinary
SET authority across some TCS faults. Host withdrawal and the ordinary state
machine prevent automatic re-engagement; healthy CAN alone is not a gesture.

The profile supersedes LKAS, MAIN and CANCEL assignments and the brake-pause
threshold in memory. Their saved values are preserved and restored on the next
startup with the profile off. The UI shows those rows as **Set by MADS**.

SelfdriveD transports fresh retained intent as the display-only
`aolAxisState.displayIntentValid/displayIntentEnabled` facts. Controls copies
these to `CarControl.lateralDisplay` only for a current, exact carState source.
`available` captures the qualified profile and AOL master at startup and stays
set on stale input; `valid` and `enabled` clear on invalid or stale input.
Enabled intent may remain true through temporary output gates, so the factory
display can distinguish paused steering from an explicit driver withdrawal.
Neither these facts nor host/native regression tests establish on-car
qualification, factory HUD rendering, or ESCC hardware compatibility.
