# Native UI audit: comma four (compact) and C3 (large)

Audit of StarPilot 7.0 "Dom" at `efe875ffd6fbeda24b44d6b3793686c7deafbf88`, 2026-10-28. The driver uses a
comma four with a 2023 Hyundai Sonata Hybrid Limited, so compact findings come first.

Captures are desktop renders of the native renderer, and the tests are host tests. Nothing here was run on a
device or in a car, and none of it establishes legibility or reach at the windshield mounting position.

## Method

All captures use the native renderer with isolated host Params, as documented in
[the UI README](../../openpilot/starpilot/ui/README.md):

```sh
./dev python -m openpilot.starpilot.ui.preview_shell --profile {large,compact} \
  --font-directory openpilot/starpilot/ui/assets/fonts \
  --asset-directory openpilot/selfdrive/assets --output <new dir>
./dev python -m openpilot.starpilot.ui.preview_{typography,home,settings,device,software,slc_actions} ...
```

Coverage included 32 large and 7 compact shell scenes, plus about 80 narrower captures. The narrower set
covered compact alerts with real event strings and the comma four (`mici`) event table, compact settings
cards, signs, SLC actions, torque bar extremes and lane notices.

Three surfaces can't be captured through the documented previews:

- The compact feature cards.
- Compact alerts with real event text.
- The compact rail with the stock confidence ball.

For these, scratch scripts outside the repository drive the same production classes. The ball is not added to
`preview_shell` because it would import `ui_state`, which constructs a SubMaster, and the preview promises no
device services. A "before" capture either runs the exact `efe875ffd` code or forces the pre-change branch.
Evidence and scripts are kept outside the repository. Before/after hash comparisons accompany each fix.

## Findings

| # | Pri | Profile | Finding | Status |
|---|-----|---------|---------|--------|
| 1 | P1 | Compact | Non-full alert titles shrank to 30 px, then were cut at the road clip ("touch steering wheel: no face dete…"). Subtitles overran the edge. | Fixed, layer 4 |
| 2 | P1 | Compact | The stock confidence ball treated AOL lateral-only as full engagement. A brake prediction for the disengaged axis could turn it red. | Fixed, layer 2 |
| 3 | P1 | Compact | With the stock ball, following distance stayed in the ball's rail path. A low-confidence ball landed on the lanes icon. | Fixed, layer 2 |
| 4 | P1 | Large | The Software "Current Version" value painted over its label for ordinary branch names. | Fixed, layer 5 |
| 5 | P2 | Compact | Card decrement `−` and separator `·` rendered as `?`, because the glyphs aren't in the loaded font set. | Fixed, layer 3 |
| 6 | P2 | Compact | Owner text (`’ “ — … · ²`) on feature cards rendered as `?`. | Fixed, layer 3 |
| 7 | P2 | Large | Stepper values floated in a 40 px band above the −/+ buttons, crowding the separator. | Fixed, layer 1 |
| 8 | P2 | Large | The last Device and Software capsules were cut at y=1030, and the accepted touch area was 10 px short. | Fixed, layer 5 |
| 9 | P2 | Large | PREVIOUS/FAST capsule labels were 30 px while peer capsules were 35 px. | Fixed, layer 5 |
| 10 | P3 | Large | `preview_device` and `preview_software` crashed serializing `frozenset` after export. | Fixed, layer 5 |
| 11 | P2 | Compact | Unloaded ellipses remain in the comma four pairing UI (`galaxy_bluetooth/phone_ui.py:182,262`) and shell notices (`runtime_app.py:729,764`). | Open |
| 12 | P2 | Large | Non-ordinal choices such as Sound Pack (Standard/Classic) use a −/+ stepper. A choice control would read better. | Open: needs an interaction decision |
| 13 | P3 | Large | Current speed uses plain `round()` (`onroad_large_widgets.py:118`), so it can flicker between 62/63 at the boundary. Sunnypilot added 0.2 hysteresis. | Open (C3 only) |
| 14 | P3 | Large | StarPilot feature pages use the purple Aether toggle with an On/Off label. Native panels (Toggles, Software) use the green stock toggle. | Open: design-system choice |
| 15 | P3 | Compact | Home footer icons sit flush with the bottom edge. This matches upstream mici (`mici/layouts/home.py:263`). | Not a regression |

Rejected as non-issues:

- The constellation dots in toggles and headers are an intentional motif.
- Card and carousel peeks are an intentional scroll affordance.
- Preview fixture copy such as "Pip:Enabled" is not product text.
- `maps_state.py:21` contains `…` but isn't reachable from comma four navigation.

### Sunnypilot HUD regression scenarios

These were checked against the uncommitted Sunnypilot `hud_layout.py` work. No reference files were modified.

- **Speed-limit sign yields to alerts.** Compact submits the sign only when there is no alert.
- **No unknown-limit placeholder.** An unknown limit draws nothing in the baseline compact capture.
- **Blind-spot overlap.** StarPilot signals blind spots with an edge border, not an icon in the sign slot, so
  the Sunnypilot overlap cases don't arise.
- **Speed hysteresis.** Open for large only (finding 13). The compact HUD shows no current speed.

## Compact right rail: design decision

The driver asked for the comma confidence ball on the right, as on Sunnypilot, in place of StarPilot's icons.
StarPilot already ships this as Appearance › **Use StarPilot Widgets** set to **Off**, which stores
`StockConfidenceBallWidget=1`. The ball now follows Sunnypilot's semantics
(`sunnypilot/mici/onroad/confidence_ball.py`, 47db84ebfb) using StarPilot's palette, so it agrees with the
border colour from `axis_status_color`:

| State | Position from | Colour |
|---|---|---|
| Lateral and longitudinal | `(1−max brake) × (1−max steer)` | green / yellow / red zones (unchanged) |
| Lateral only (AOL/MADS) | `1−max(steerOverrideProbs)` | AOL teal `(10,186,181)` |
| Longitudinal only | `1−max(brakeDisengageProbs)` | long-only pink `(255,105,180)` |
| Driver/long override | active axes | white→grey (unchanged; takes precedence) |
| Disengaged | sinks out of view | dark grey (unchanged) |

The ball owns the whole rail. Following distance is drawn only if it has been moved off the rail in the layout
editor. The Sonata cluster already shows the gap, and personality changes still raise an alert. The
CEM/driving-mode icon is not shown in this mode, because the border colour already carries experimental and
conditional state.

The confidence score is not invented. It is the model's disengage predictions, as in upstream and Sunnypilot.
It does not replace the factory cluster's separate lateral and longitudinal state or critical warnings. The
native HUD thread found that Dom's lane-visibility bits are set unconditionally, so they are not detection
evidence either.

## Stack

All PRs are drafts on `alanbixby/StarPilot`. Each layer passes the full `openpilot/starpilot/ui/tests`
directory on its own.

| Layer | Branch | PR | Change |
|---|---|---|---|
| 1 | `fix/ui-layout-foundation` | #1 → `Dom` | Large stepper alignment |
| 2 | `fix/compact-confidence-ball` | #2 | Ball semantics and rail ownership |
| 3 | `fix/compact-missing-glyphs` | #6 | Compact glyphs and owner typography |
| 4 | `fix/compact-alert-fit` | #11 | Compact alert wrapping |
| 5 | `fix/large-software-device-panels` | #12 | Large Software/Device panels |
| 6 | `docs/ui-audit` | #15 | This report |

Two failures outside the stack were observed in the shared host runtime. Replay export can't load
`libmedia_mux.so`, which `./dev python` doesn't build. Galaxy pairing tests fail on slug assertions. The stack
touches no export, pairing or Galaxy code.
