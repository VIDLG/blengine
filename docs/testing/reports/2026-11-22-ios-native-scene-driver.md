# iOS native scene driver (in-process XCTest)

2026-11-22. Scope: the UI *driver* layer only. This is not a gate result and not RF
evidence — no fixture session was started for the runs below, and every scene still ends
`host_gate=pending`.

## What landed

| Piece | Path | Role |
|---|---|---|
| Scene driver | `tests/consumers/react-native-bleng/ios/BleHubBlengHostUITests/BlengSceneDriverUITests.swift` | Executes a declarative plan with `XCUIElement` in the test process |
| Host runner | `tools/react_native/run_ios_scene_driver.py` | Builds the plan, runs `test-without-building`, exports the receipt |
| Entry | `just react-native formal-host-ios-scene-driver +args` | Manual/host use |
| Guards | `tools/tests/react_native/test_ios_scene_driver.py` (22 tests), `test_workbench_ids.py` (plan identifiers) | Plan shape, receipt contract, one ID vocabulary |
| Projection contract | `tools/react_native/run_status.py` | One key set and one strict decoder, shared with the Appium lane |

Nothing was added to the App. The driver reads what the App already exposes: the single
`run-status` projection (`src/runStatus.ts`) plus the elements deliberately rendered
outside every scroll container.

## Why it is not a WebDriver wrapper

The Appium lane spent its budget on XCUITest's accessibility snapshot, once per element
lookup, over an HTTP hop through WebDriverAgent: 8.5 s per click, 5 660 commands and
1 132 s of pure round-trip time for one scan scene. The native lane issues the same
identifiers directly:

| Operation | Appium (measured) | Native driver (measured) |
|---|---|---|
| one click | 8.5 s | 0.45–0.80 s |
| read one element | 0.05–0.5 s (plus snapshot cost per lookup) | 0.13–0.29 s |
| poll/wait | 8.5 s per probe | 0.07–0.24 s |
| a full 18-step scene | not measured (same steps ≈ 2+ min of clicks) | **6.62 s of step time** |

`smoke` (18 steps, session start → three page switches → close): **6.62 s**. `scan`
(13 steps, start scan → pull one advertisement → stop → close): **5.65 s**. Both plans
passed on `00008110-000C61160C39401E` (iPhone 14 Plus, iOS 27.2.1) against App tree
`215b300e8f9190aa450965d847439b8f7f337bfc01769a19c86997b0ae0f4a53`.

The scan run drained a real advertisement from the RF environment
(`name="MX 8.3 - BT3"; rssi=-69; connectable=true`). That proves the scan path, **not**
the nRF52840 fixture: the provider requires an explicit immutable `--fixture-request`
session, which only a host-owned lane can create.

## Contracts this lane added

1. **The plan cannot scroll.** There is no `swipe`/`scrollTo`/coordinate step, and a unit
   test fails if one is added. Android's accessibility tree omits off-viewport
   `ScrollView` children, so a gesture plan would behave differently per platform and only
   as accurately as input injection. If a scene needs a fact the tree lacks, that fact is
   pinned in the App (`src/App.tsx`), which keeps `iOS and Android identical` and keeps the
   scene honest. `scan-samples` and `lifecycle-evidence` are therefore *not* scene surface;
   counts come from `run-status`, the newest observation from `scan-latest`/`notify-latest`.
2. **The receipt is an attachment, never stdout.** UI-test process output does not reach
   the `xcodebuild` log. The driver adds one `XCTAttachment` named
   `ios-scene-driver-receipt`; the host reads `attachments/manifest.json` from
   `xcrun xcresulttool export attachments` and accepts exactly one attachment whose
   declared name matches `ios-scene-driver-receipt_<n>_<uuid>.json` (the suffix is
   added by xcresulttool). Zero, two, a foreign scope or an unsafe exported name all fail.

3. **The projection has one decoder.** `tools/react_native/run_status.py` now owns the key set
   and the strict decode, and both lanes use it: the Appium lane wraps its error as
   `AppiumError` (so existing scene assertions are unchanged), the native lane decodes every
   `run-status` read and publishes the result as `projection`. A drifted frame therefore fails
   the scene instead of matching nothing.

## Attach-only mode is proven, not assumed

A host-owned scene must not relaunch the App: the host installs, launches and pins one
pid, and `XCUIApplication().launch()` would silently invalidate that identity.

Measured, twice, with the App installed by `devicectl` from the device product rather
than by xcodebuild:

| Step | pid |
|---|---|
| `devicectl device process launch` | 3757 |
| `test-without-building` + 18-step plan | 3757 |
| `devicectl device process launch` (second run) | 3773 |
| `test-without-building` + 22-step plan | 3773 |

So `xcodebuild` does not reinstall an already-installed target App, and the installation
container is unchanged. Attach mode is a real capability, not a hope.

Foregrounding is a **plan step**, not an entry mode. `activate()` fails when a system
prompt holds the foreground, so the plan answers the prompt first and activates second.
That is also why it cannot be a flag: the order is the point.

## Three device facts the first fresh-install run exposed

1. **A fresh install puts system prompts above the App, and they swallow taps.**
   `devicectl` uninstall + install clears the TCC decisions, so the next launch raises
   `允许“BleHubBlengHost”查找本地网络中的设备？` and the first session raises
   `允许“BleHubBlengHost”查找蓝牙设备？`. A tap on the App then lands on the prompt: the
   step reports a timeout while `run-status` still says `state=idle`. The trees are what
   proved it — SpringBoard's window list showed an `Alert` the App's own tree cannot.
   `answerAlert` answers only alerts whose title matches a rule the plan declared, using
   only a declared button label, and refuses everything else. Measured on a clean install:
   step 0 answered the local-network prompt in 1.2 s and step 5 answered the Bluetooth
   prompt in 1.6 s; the whole 22-step scene passed in 13.7 s.
2. **A prompt's controls appear twice in SpringBoard's tree.**
   `kTCCServiceBluetoothAlways-allow-button` exists on a wrapper *and* on its child, so
   `alert.buttons["允许"].tap()` fails hard with "Multiple matching elements found" — a
   snapshot failure, not a throw, so it aborted the test before any receipt existed.
   `firstMatch` is the fix, and it is the same primitive the rest of the driver uses.
3. **Resolving an element that has just vanished aborts the test.**
   `alerts.element` needs the query to match exactly one element; when the alert
   disappears between a `count` read and the element read, XCTest raises
   "Failed to get matching snapshot" instead of returning. The step now only ever uses
   `firstMatch` + `exists`, and reads a label/identifier *before* tapping, because a read
   after the tap can hit the vanished alert.

Those failures also exposed a hole in the host's verdict rule: XCTest can fail without
throwing, so a receipt that says `passed` is not sufficient. `run_scene` now requires
`xcodebuild` to exit 0 as well, and records the failure with the log tail.

## The managed lane now runs this driver (scan)

`bleng_ios_scan` no longer refuses a scene for having no WebDriverAgent. A scene with a
plan in `scene_ios_native.PLANNED_SCENES` — today `scan` — builds
`NativeIosSceneDriver` and goes through the shared envelope (`run_prepared_scene`): frozen
candidate → phone lease → fixture session → managed install/launch → **in-process scene**
→ App finalize → passive fixture close → phone finalize → bundle. The Appium scenes keep
their WDA/Appium inputs and the `automation/` evidence shape until each one gets a plan.

That refactor made the driver a seam instead of an assumption:

| Piece | Module |
|---|---|
| Envelope (driver-independent) | `bleng_scan_runner.run_prepared_scene` + `SceneDriver` |
| Appium implementation (to be deleted scene by scene) | `bleng_scan_runner.AppiumSceneDriver` |
| In-process XCTest implementation | `scene_ios_native.NativeIosSceneDriver` |
| Candidate binding without a WebDriver | `IosCandidateOwner.bind_native` |
| Evidence | `driver/record.json` + `driver/plan.json` + `driver/result.json` + `driver/receipt.json` |

A bundle states which driver ran (`inner.driver.kind`) instead of assuming a server. The
in-process record has no WDA/server/helper claim, because this lane owns none of them;
what it must prove is the exact xctestrun, the plan, a zero exit status, a passed receipt
and that the receipt's hash is the record's. The raw `xcresult` and `xcodebuild` log stay
outside the bundle — they are tool storage — while the receipt, the result and the
text diagnostics are copied in.

Two host-side changes came with it: the iOS request for this lane records
`driver_kind` + `xctestrun_sha256` instead of three unused ports, and `--xctestrun` is
required for planned scenes while `--authorize-wda-build` is rejected for them.

### Managed run status

**Passed** on `00008110-000C61160C39401E`, App tree
`4a69d66a87c7aa7bf79ea0ce10442d02870ddd34ee4677932f71cef49a989be8`, run
`b622ac84-facd-4558-9f3c-391b969c0c87` (`.work/ios-native-scan-r5`):

| Fact | Value |
|---|---|
| Outer result | `status=passed`, `host_gate=pending`, `rf=scan-ui-observation` |
| Statuses | scenario/session/phone/fixture all `passed`; `cleanup_errors=[]` |
| Cleanup order | scan.run-prepared → scan.result → fixture.close-record → lease-release → lease-record → bundle.prepare → cleanup.complete |
| Fixture close | `resource_identity=jlink:1050252028`, `session_id` = the run id |
| Inner driver | `kind=ios-xctest-in-process`, `plan=scan`, `steps=22`, session `ios-xctest-in-process:<run>` |
| Observations | `idle, started, scan:running, scan:advertisement-matched, scan:stopped, closed` |
| Scene | 22 steps in 27.9 s, `xcodebuild` rc 0 |
| Peer observed | `name="BleHubMultiRfV2DKA"; rssi=-53; connectable=true; services=fd600000-4000-…` |
| Filter | `setText` on `scan-service-filter` verified `value=…`; projection `scanFilter=fd600000-…` |

The bundle passed `verify_scan_bundle`. `host_gate` stays `pending` by design: a scene pass is
not a release verdict.

### What the first runs got wrong (all fixed)

1. **A scene that matched any radio is not evidence.** The first passing scene matched a
   neighbour (`rssi=-86`, `name=not reported`). The plan now declares the peer: the host
   passes the contract's `local_name` and `service_uuid`, the scene sets the service filter
   (verified through the projection) and the pull loop only succeeds on
   `name="<fixture>"`. Without a local name the driver refuses to start at all.
2. **`scan-service-filter` is not on the Tests page.** The pinned console only exists on
   Current run, so the filter step has to follow session start, not precede it — measured as
   `setText` timing out on an absent element.
3. **A missing read is a silent plan gap.** The trace derivation wants `started`; the scan
   plan never read it, so a scene that ran to completion (27 s) failed on the receipt. The
   plan now reads every fact the trace asserts, and `test_scene_ios_native` pins that link.

### Earlier failures, for the record

First attempt: refused at install (`App already running; refusing takeover` — manual runs
had left it up) and the lease was quarantined; archived with
`ios_radio_lease archive-observation` under the documented hash gate.

Second attempt (`--release-before-install`): candidate freeze, phone lease, fixture start,
managed install/launch and the whole cleanup order passed; the scene failed because the
device could not enable automation mode (`Timed out while enabling automation mode`, 64 s,
reproduced twice more at 108 s) while the host reported all three switches `unattended`.
That half is the phone's `Settings > Developer > UI Automation`, which this Mac cannot read
or set; after the operator rebooted and re-armed it, the probe passed in 0.25 s and the run
above followed.

One real defect surfaced by that failed bundle: the driver's raw run directory was inside
the attempt, and `attachments/manifest.json` is a *list*, so bundle verification failed
with `JSON object required`. Fixed by moving the raw run out of the bundle and publishing
derived witnesses only (above).

## The action ledger is machine surface now

The remaining scenes (notification/indication/io/cancellation/write-reject/ccc-*) each
have to establish that the App *recorded* the actions the scene dispatched. The Appium
lane reads that from `lifecycle-evidence` — prose inside the scroll container — which the
in-process driver cannot and must not read.

So the same bounded list is now also published as one pinned node, `action-ledger`:

```text
omitted=0
1|start|succeeded|
2|notify-pull|succeeded|ended: disabled
```

`src/actionLedger.ts` encodes it, `tools/react_native/action_ledger.py` decodes it strictly
(increasing sequence, enum-shaped action/result, bounded detail, bounded entry count), and
`lifecycle-evidence` stays exactly as it was for a human. The node is the **last** entry in
the pinned block: it grows with the run, and everything a scene taps must stay above it.

A guard derives the scrollable-content vocabulary from the source instead of trusting a
hand-maintained list: it reads the identifier groups from `ids.ts`, collects the ones
`ManualScan.tsx` renders (the panels `App.tsx` puts inside `workbench-content`), and fails
if any native plan drives one of them. That check caught the real distinction — the old
`_SCROLL_TARGETS` list is "identifiers Appium may scroll to reveal", not "content", and
using it as content would have been wrong.

## Two more scenes: notification and indication (phased)

Both passed on the same run shape, App tree
`5cfabfac16fa188bea05eedf1be70814cfcc4817d94734fad4f97aeaa6e415fa`:

| Scene | Run | Phases | Steps | Trace |
|---|---|---|---|---|
| `notification` | `301c47e8` (`.work/ios-native-notification-r8`) | subscribe 30 → host emit → collect 9 | 39 | 12/12 |
| `indication` | `79d4d320` (`.work/ios-native-indication`) | subscribe 30 → host emit → collect 9 | 39 | 12/12 |

`notifyMode` came back as `notification` and `indication` respectively — the mode is read from
the App's enable receipt and compared with the profile, not assumed. The App's own ledger
for the notification run:

```text
1|start 2|begin-scan 3|scan-start-wait 4|scan-pull 5|scan-pull 6|scan-stop
7|connect 8|connect-wait 9|gatt-discover 10|gatt-discover-wait
11|notify-enable 12|notify-enable-wait 13|notify-pull
14|notify-disable 15|notify-disable-wait 16|notify-pull 17|close      (all succeeded)
```

Timings: subscribe 35.2 s, collect 6.0 s. The pulled value was `1 bytes [ef]` — the value the
fixture emitted, matched through the plan's own condition and again through the derivation.

## Six device facts the phased lane exposed

1. **An App Switcher left in front swallows taps.** `activate()` brings the *process* forward,
   but a presented switcher keeps receiving the taps, so `tap start-session` does nothing and
   the scene reports `state=idle` for 90 s. The switcher's window exists in SpringBoard's tree
   whether or not it is presented, so detection is unreliable: the step now presses Home
   unconditionally first.
2. **`activate()` returns before the App is frontmost.** Pressing Home then activating
   immediately lost the next tap in the transition. The step now waits for
   `.runningForeground` (15 s bound) and fails with the actual state instead of pressing into
   a moving screen.
3. **An Apple ID notice re-posts until acknowledged.** `你的账户中有设备加入` covers the App,
   has one acknowledgement button and grants nothing; it came back between runs. It is now a
   declared rule, and the receipt records `answered[1] id= label=… button=…`, so the lane can
   answer it auditable rather than silently.
4. **An undeclared alert is left alone, not fatal** (`required: false`): it may belong to
   another app and not cover ours at all. `required: true` makes a stranger in place of the
   expected prompt a failure.
5. **The target must be declared after discovery.** `gattMatch` resolves against the discovered
   topology, so waiting for `gattMatch=one` before setting the target can never succeed.
6. **A subscription's terminal is delivered through the event stream.** After
   `disable-notifications` the phase stays `subscribed` until a pull consumes the end; the
   collect plan therefore pulls once more and waits for `notify=ended`. That is exactly the
   trailing `notify-pull → ended: disabled` the Appium lane recorded.

## Not proven here

- Android: needs an instrumented-driver equivalent of the same plan vocabulary; the
  driver seam (`SceneDriver`) is where it plugs in.
- `host_gate` stays `pending` on every passing run: a scene pass is not a release verdict.
- The remaining iOS scenes still run through Appium (see below).

## Next: the remaining scenes

Nothing is left on Appium for iOS: all eleven managed scenes moved over on 2026-11-23
([io](2026-11-23-ios-native-io-scene.md),
[cancellation](2026-11-23-ios-native-cancellation-scene.md) and the rest in
[all scenes](2026-11-23-ios-native-all-scenes.md)). Appium now serves Android only.
They need the same seam the subscription pair used (`PHASES` + a host action) plus the
fault-armed host actions: hold a read (`cancellation`), park a write and arm a CCC refusal
(`write-reject`, `ccc-*-reject`), and drive cycles (`recovery-ten`). The `setText` step and
the `value` condition are already there for the GATT target and write payload.

## Reproduce

```sh
pixi run just react-native formal-host-ios-device-test-build --device 00008110-000C61160C39401E
pixi run just react-native formal-host-ios-scene-driver \
  --device 00008110-000C61160C39401E \
  --xctestrun tests/consumers/react-native-bleng/ios/build/device-derived/Build/Products/BleHubBlengHost_iphoneos27.2-arm64.xctestrun \
  --plan smoke --timeout 300
```

`--launch false` is attach mode: the plan activates the App the host already installed and
launched, and leaves its pid untouched. The managed lane is
`pixi run just react-native formal-host-ios-managed-scan --device <udid> --app-tree-sha256 <sha>
--xctestrun <xctestrun> --fixture-contract … --fixture-profile … --fixture-port … --fixture-resource-identity …
--output <dir> --authorize-install --authorize-fixture-start --release-before-install`.
