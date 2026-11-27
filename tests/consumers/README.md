# Consumer ownership and migration

Consumers validate downstream package boundaries. They do not define a second BLE API or own
production recovery policy. All maintained native consumers use the corresponding published or
staged production image; an RN source-checkout host does not replace an artifact-only consumer.

## Current maintained lanes

| Location | Boundary under test | UI role |
|---|---|---|
| `android/blehub/` | Published BleHub/Android backend artifacts and native conformance | No product UI assertions |
| `android/bleng/` | Published combined BLeng/Supervisor UniFFI image | No product UI assertions |
| `apple/blehub/ios`, `apple/blehub/macos` | Apple BleHub package and production backend consumers | Native test harness, not RN UI |
| `apple/bleng/ios`, `apple/bleng/macos` | Combined Apple BLeng package consumers | Native test harness, not RN UI |
| [`react-native-bleng/`](react-native-bleng/README.md) | Formal JS API and its Nitro/native bridge | **Only RN App** |

The maintained native layout is now platform → package line → OS target where needed. Android
`blehub` and `bleng` remain independent Gradle roots, not modules of a shared consumer project.
An Apple native consumer may need an executable bundle for the OS/runtime without testing a
user interface; “no UI semantics” does not mean “no executable target”.

### Apple artifact package responsibilities

The BleHub native consumers resolve two staged Swift packages, not checkout backend sources:

- `.work/staged/apple-production`: generated UniFFI bindings and the sole native XCFramework.
- `.work/staged/apple-backend-production`: `BleHubApple` Swift backend sources and a dependency
  on that same UniFFI package. It contains no second native image.

`apple consumer-macos`, `apple consumer-ios` and the explicitly authorized `apple consumer-ios-device`
all prepare both packages through the common Just prerequisite. Backend staging is owned by
[`stage_backend.py`](../../tools/apple/stage_backend.py), with the manifest template under
[`platforms/apple/packaging`](../../platforms/apple/packaging/Package.swift.in).
The backend resource contract checks exact bound/disposed snapshots and lease reuse without
initializing CoreBluetooth or scanning. iPhone receipts additionally require this run's unique ID,
`backend_resources=passed` and exactly one successful terminal; old receipts or a lone final
`passed` are not sufficient. macOS execution and unsigned iOS compilation do not prove iPhone
runtime or RF. Hardware-free packaging/receipt regressions use `pixi run just apple test-consumer-tools`.

## Target source layout

```text
consumers/
├── android/
│   ├── blehub/
│   └── bleng/
├── apple/
│   ├── blehub/
│   │   ├── ios/
│   │   └── macos/
│   └── bleng/
│       ├── ios/
│       └── macos/
├── react-native-bleng/          # one formal RN App, native bridge tests and shared UI E2E
│   ├── src/
│   ├── android/
│   └── ios/
└── archive/                    # historical lanes, not active gates
```

The `react-native-internal` package and host were deleted on 2026-11-22 once their last C-class
scene moved to the formal App lane; `pixi run just react-native formal-host-install` asserts that no
internal RN dependency survives in the formal autolinking graph.

The separate `apple/bleng-rn-ios` source consumer has been retired. Its useful validation is now
owned by `react-native-bleng/ios/PlatformContract` (isolated staged SwiftPM compilation, no Nitro)
and the formal App/native-test build (full CocoaPods/Nitro graph). The canonical entry is
`pixi run just react-native formal-host-ios-build [false|true]`: omitted or `false` verifies the
local Pods-preparation receipt; `true` is the explicit refresh and passes Python `--refresh-pods`.
Missing/stale preparation fails closed without adoption or fallback, and dependency installation
remains explicit. Podspec dependency validation remains ahead of these builds; the host must not
mask a missing formal package dependency or use a requested prebuilt React Native source fallback.
This is a source/local-package compile gate, not a clean npm-tarball packaging proof. The narrow
CocoaPods snapshot captures local resource drift but is not universal dependency analysis or
complete initial resource-integration proof; see the [preparation boundary](../../tools/react_native/README.md#formal-ios-pods-preparation-boundary)
and [candidate report](../../docs/testing/reports/2026-11-11-ios-pod-preparation-boundary.md).

Migration of native source/build roots:

| Previous root | Maintained root |
|---|---|
| `android/` (old Gradle root) | `android/blehub/` |
| `android-bleng/` | `android/bleng/` |
| `apple/ios/`, `apple/macos/` | `apple/blehub/ios/`, `apple/blehub/macos/` |
| `apple/bleng-ios/`, `apple/bleng-macos/` | `apple/bleng/ios/`, `apple/bleng/macos/` |
| `apple/bleng-rn-ios/` | Formal RN `ios/PlatformContract` + App/native-test compile gate; duplicate static Nitro consumer removed |

Active tools, Just recipes and staged artifact references use the new roots, with no compatibility
shim or source dependency added. Ignored output may remain at an old location; it is not an active
consumer or an alternative source tree. Historical reports retain their original commands and
paths; use this table to locate the maintained source, not to reinterpret their candidate results.

The native BLeng artifact consumers additionally exercise `Supervisor`'s context-fenced typed
operation entry points through the generated Swift/Kotlin bindings. Rust owns initialization;
consumer fixtures verify the ten-operation chain, stale input rejection, existing handle results
and reader/resource retirement. These stay in the existing native consumers, not a new App or a
raw API added to the RN package. The same native readers now also check owner-bound scan
projection envelopes and revocation, without replacing their original raw-event/terminal checks.
Required event observations are awaited before shutdown; reader termination alone is not a FIFO
flush assertion. macOS execution does not prove iPhone or Android ART execution.

## Formal RN consumer: separate responsibilities, one App

- **Component/software tests**: `react-native-bleng/src/**/*.test.[jt]s?(x)` tests page/state behavior at the mocked formal package boundary. `formal-host-check` uses installed dependencies for typecheck/autolinking; only explicit `formal-host-install` runs `npm ci`. Component Jest must not discover device E2E bootstraps.
- **Host/controller tests**: `tools/tests/react_native/` mocks actual SDK/install/transport boundaries and verifies ownership, deadlines and cleanup. Reusable synthetic samples belong to its `support/`, not another `TestCase` module. Peripheral emulators remain under Peripheral-owned support; live providers never import these fixtures.
- **Native bridge contracts**, under its `android/app/src/androidTest` and
  `ios/BleHubBlengHostNativeTests`: directly verify the formal Kotlin/Swift seam, native Nitro
  Promise success/rejection, lifecycle reuse and applicable native cleanup observations. They do
  not prove Hermes/JS execution or UI behavior. These belong to the consumer, not to the
  integration library's own test App or the legacy probe host. Android's production-session
  worker retirement has a separate `AndroidPlatformSessionRetirementInstrumentedTest`: backend
  construction without BLE initialization, then disposal/snapshot/owned-thread exit. A scoped
  [three-test Android runtime pass](../../docs/testing/reports/2026-11-10-android-owned-worker-retirement.md)
  now covers that path, not full Supervisor shutdown, active GATT cleanup or Hermes invalidation.
  Pure worker error/join mechanics
  live beside the integration implementation as JVM tests and run through
  `pixi run just react-native bleng-android-unit-test`, without an App/device or BLE initialization.
  Single-reader observation semantics are tested beside the Android integration and in the formal
  Apple native target: only a `nextEvent` observation timeout continues reading; EOF, cancellation,
  other read failures and delivery failures remain terminal. These deterministic tests do not prove
  real-time idle longevity or Hermes reload; see the
  [observation-timeout candidate](../../docs/testing/reports/2026-11-10-bleng-event-reader-observation-timeout.md).
  Apple retirement requires an explicit zero `peerCacheEntries` count alongside its other native
  resources; missing is not zero. Native teardown observes the cached `closeResult()` receipt after
  a one-shot `close()` rather than invoking raw shutdown twice. The
  [retirement-contract regression](../../docs/testing/reports/2026-11-10-apple-retirement-contract.md)
  covers missing/nonzero peer counts and repeated close observations without changing Rust semantics.
- **Shared UI E2E**: the formal App's rendered surface is driven by a platform-native driver on both
  platforms — the App's own XCTest target (`BlengSceneDriverUITests`) on iOS and its own instrumented
  test (`BlengSceneDriverInstrumentedTest`) on Android, in-process, over the pinned machine surface.
  Device preparation, ownership, installation, signing and failure cleanup remain host
  responsibilities. No UI-automation server is part of either lane anymore.

### The native scene driver (iOS)

A scene is a **host-owned declarative plan**, not a procedure inside the automation server. The host
writes `BLEHUB_SCENE_PLAN` (JSON) and `tools/react_native/run_ios_scene_driver.py` runs it as one
`xcodebuild test-without-building`; the App's XCTest target executes it with `XCUIElement` in the
test process. There is no WebDriverAgent, no Appium server, no tunnel and no element-id round trip.

Steps are `wait`, `waitAny`, `still`, `tap`, `tapWhen`, `tapUntil`, `read`, `setText`, `activate`
and `answerAlert`. A condition is a conjunction on one element: `exists`, `label`, `labelContains`,
`enabled`, and `status` — the last one matches `key=value` pairs of the `run-status` projection
below, so a poll is one element read whose facts cannot disagree. `tapUntil` is the bounded pull
loop the App requires for scan/notification events; it may also declare `advance` (below) so a
step cannot settle on a value an earlier command left behind. `activate` foregrounds the App
already
launched and never restarts it; `answerAlert` answers only a system prompt whose title matches a
declared rule.

Two steps exist because two scenario shapes cannot be expressed by `wait`:

- **`waitAny`** is a bounded disjunction over 2..8 conditions; it reports which one held
  (`observed["waitAny"]` = its index). One question can have two legitimate answers — a refused CCC
  disable can keep delivering or stop locally — and which answers are acceptable is the host's
  decision, not the driver's.
- **`still`** is a temporal assertion: the condition must keep holding for `for` seconds, sampled
  every poll interval. "Nothing may change" is not a wait — a wait for a state that is already true
  returns instantly — so a quiet window needs its own step (`passive-disconnect`).

A scene is a list of phases, because the firmware decides where the host must act: `scan` and `io`
are one run of the App; `notification`/`indication` are `subscribe` → host emits → `collect`; and
`cancellation` is `open` → host arms a held read → `hold` → host confirms the hold → `queue` (submit
and cancel behind it) → host releases → `settle`; `write-reject` and the `ccc-*-reject` pair park one
write or CCC write and refuse it; `passive-disconnect` lets the peer end the link; and
`recovery`/`recovery-ten` repeat one cycle N times on the same owner (their phase list is derived
from `scene.cycles`, not written out by hand). A later phase only re-enters the App
(`activate` + `state=started`); it never restarts the session, re-scans or re-links. A repeated
cycle's first step after the link ends *does* scan again — a scan is a run in the App, so its read
lane and view start fresh. Scenes live in
`scene_ios_native.PHASES`; the producer routes a scene to this driver by name. A scene that acts on
GATT declares its target from the fixture profile (`subscription_target`, `io_target`,
`read_write_target`), never from a position in the topology, and `GATT_TARGET_KEYS` states exactly
which fields each scene needs.

A read the host distinguishes from an earlier one must be told apart by a **settled count**, not by
the phase or the rendered text: the App keeps showing the previous read's value and keeps
`gattRead=done` between reads. `gattReadTotal` and `descriptorReadTotal` are those counts, published
*after* the outcome, so observing one advance also means the new rendering is on screen.

A `tapUntil` whose expected value can equal the value already on screen declares
`"advance": "<count field>"`. The step then records the field's current value when it starts and is
only satisfied once that counter is strictly greater **and** the `until` condition holds — so the tap
actually happens instead of being skipped by a condition that was already true. After each tap with
`advance`, the driver waits for that counter to move before deciding whether to tap again: polling a
pending operation is not a retry, and re-tapping it would record the same command twice and overflow
the App's bounded action window. `descriptor-io` needs this for its final "unchanged" read: the value
it expects (`0000`) is the value the cleared read already left on screen, and only the count proves a
fresh read reached the peer.

A scene whose record must be the **whole session** refuses a window that dropped any action: the
`omitted=<n>` header of `action-ledger` must be `0`. A derivation may compare a suffix only if it says
why (the `recovery` scene does, because it spans one App window per cycle).

A read the host needs later must have a **unique `as` name in its plan**: the receipt is one flat
name→text map and a repeated name silently overwrites the earlier read (`io` reused `initial` and
the host then decoded a characteristic rendering as a `run-status` frame).

Three rules make this a contract rather than a convenience:

- **The plan cannot scroll.** There is no `swipe`/`scrollTo`/coordinate step and a unit test fails if
  one is added. Android omits off-viewport `ScrollView` children from its tree, so a gesture-based
  scene would be platform-specific and only as accurate as input injection. A fact a scene needs
  belongs in the pinned region above, not behind a swipe.
- **The receipt is an attachment.** UI-test process stdout never reaches the `xcodebuild` log, so the
driver adds one `XCTAttachment` named `ios-scene-driver-receipt` and the host reads it from
`xcrun xcresulttool export attachments` → `manifest.json`. Exactly one attachment whose declared
name is `ios-scene-driver-receipt_<n>_<uuid>.json` (xcresulttool adds the suffix) may match;
zero, two, a foreign scope or an unsafe exported file name fail closed.
- **On Android the contract is files, and a scene ends on the last one.** The driver publishes
`scene-receipt.json`, `scene-hierarchy.xml` and then `scene-done` into the App's external files
directory, each written beside and renamed into place. The host removes all of them before a run,
proves the directory holds none, and stops the instrumentation only once `scene-done` exists;
handoffs are files too (`handoff-request-<action>` / `handoff-ack-<action>`). Logcat markers are
diagnostics scoped to the run by a sentinel the host writes first: a replayed marker from an
earlier scene once ended the next one before it had started.
- **`launch` and `activate` are different things.** `launch` starts a fresh App process, for
  the standalone driver entry. A host-owned scene installs, launches and pins one pid itself, so
  it passes `"launch": false` and its plan foregrounds the App with the `activate` step — after
  any system prompt has been answered, because `activate` fails while an alert holds the
  foreground. Measured: a `devicectl`-installed App kept pid 3757, then 3773, across two such
  runs.
- **The bundle names its driver.** The inner result states `driver.kind`; an in-process run
  publishes `driver/record.json` (test-bundle hash, plan, exit status, receipt hashes) and claims
  no automation server, helper APK or WebDriver because it owns none. A failed scene still publishes its receipt, result and text diagnostics.
- **A fresh install's system prompts are answered only as declared.** They sit above the App in
  SpringBoard and swallow taps, so a covered step reports a timeout rather than an error. The
  `answerAlert` step carries one rule per prompt it may answer (title substrings + button labels,
  both localisations) and refuses anything it did not identify. The host composes those steps
  only for a fresh install.

### The `run-status` projection (contract between the App and a UI scene)

A UI scene polls far more often than it acts, so the App publishes every scalar fact a scene needs
as **one pinned text node**, `run-status`, instead of one element per fact. The encoder is
`react-native-bleng/src/runStatus.ts`; the contract itself (key set + strict decode) is
`tools/react_native/run_status.py`, which the host and both native scene drivers use — the
driver's `status` condition matches the same pairs inside the test process (a malformed frame
there simply does not satisfy a condition; the strict rejection happens when the host decodes a
`read` of the same node).
Both sides declare the same key set, and the host fails the run on a missing, unexpected,
unparsable, empty or duplicated field — a drifted projection must not silently stop being checked.

Encoding: `key=value` pairs joined by `;`, in this order.

| Key | Meaning |
|---|---|
| `state` | Session lifecycle state (`idle` … `closedWithError`). |
| `page` | Selected workbench page (`Tests`, `Current run`, `Evidence`). |
| `scan`, `link`, `gatt`, `notify` | The run's four phases, as the panels show them. `notify` is `unverified` after a refused *disable*: the peer still has the CCC enabled, while whether this client still receives is a platform fact the App may not claim either way. |
| `linkEnd` | What ended the link, once something did: `none` before an end, then `operator` (a disconnect this App submitted), `unrequested` (the link ended and nobody here asked) or `owner` (the engine retired, which ends the observation rather than proving the link is down). A link that ended stays ended: the projection is the single writer of `link`, so a late command receipt cannot resurrect it. |
| `scanTotal`, `notifyTotal` | Advertisement and value counts, not unique devices or deliveries. |
| `notifyMode` | `none` with no subscription, `unknown` when one exists but reports no mode, otherwise the reported mode. |
| `gattRead` | The read lane's terminal: `none` before a read, `waiting` while its wait is outstanding, then `done`, `cancelled` (the operator cancelled it) or `failed`. |
| `scanTimeout` | The observation-timeout input, or `unknown` if it is not a usable number. |
| `scanFilter` | The declared service filter in canonical comma-separated form, `none`, or `unknown`. |
| `gattTarget` | The characteristic UUID the action strip acts on, as declared; `none` when undeclared. |
| `gattMatch` | `none` / `invalid` / `ambiguous` / `one`: how that declaration resolved in the discovered topology. |
| `gattProps` | The resolved row's platform-reported capabilities (`read,write,notify…`), or `none`. |
| `descriptor` | The CCCD read lane's terminal: `none`, `done`, `absent` (the characteristic has no CCCD) or `failed`. |
| `descriptorValue` | The CCCD bytes the peer reported, lowercase hex without separators, or `none` before a read. |
| `descriptorWrite` | The CCCD write's terminal: `none`, `done` or `failed`. |
| `descriptorReadTotal` | Settled descriptor reads, monotonic. A scene advances it (via `advance`) to prove a fresh read even when the expected bytes equal the value already on screen. |
| `devices` | The `connected` inventory query's terminal: `none`, `pending`, `done`, `unsupported` or `failed`. |
| `devicesCount` | Entries the completed inventory returned, or `none` before one. |
| `devicesPeer` | Whether that inventory contained the peer this run is linked to (`yes`/`no`), or `none` before a query. A list that omits the peer is a divergence, not a success. |
| `auto` | The standing "connect when the peer appears" lane: `none`, `pending`, `done`, `unsupported` or `failed`. `unsupported` is a platform answer (CoreBluetooth has no auto-connect), never a timeout. |
| `failure` | The failure the run is showing: `<platform>/<platform code>` when the platform reported one, else `<domain>/<code>`, else `none`. The sentence itself stays in the panel a human reads, so an unbounded string can never push a command out of the screen. |

Rules the two sides rely on:

- The projection is *derived in the same render pass* as the labelled elements, so it is a second
  presentation of one view state, not a second source of truth.
- It carries no errors, result lines, topology rows or sample text. Those are free-form
  observations and stay in their own elements, so a diagnostic can never masquerade as a result.
- `none` means the App has no such value; `unknown` means it has a value it cannot honour. A scene
  that needs the value fails either way instead of guessing.
- The host derives what it can (for example the omitted-sample count comes from the total and the
  retained samples) rather than trusting a second number that could disagree.

#### The action ledger (`action-ledger`)

A scene has to establish that the App *recorded* the actions it dispatched, but
`lifecycle-evidence` is prose inside the scroll container, which a scene may not read. The
same bounded list is therefore also published as one pinned node, `action-ledger`:

```text
omitted=0
1|start|succeeded|
2|notify-pull|succeeded|ended: disabled
```

Line 0 is `omitted=<n>` (entries the bounded window dropped); each further line is
`sequence|action|result|detail`, with an empty detail when there is none, and `|` /
newlines stripped from every value so one entry can never become two. The encoder is
`src/actionLedger.ts`, the strict decoder is `tools/react_native/action_ledger.py`. An
unparsable line, a gap in the sequence or a foreign action/result is drift and fails the
scene. The node is the last one in the pinned block on purpose: it grows with the run, and
everything a scene taps must stay above it.

The fourth segment is the action's own outcome sentence. The App stores it in `Observation.error`
and the encoder emits it as `detail`; the two names disagreeing once meant every failed action
reached the machine ledger with an empty outcome. `actionLedger.test.ts` pins that mapping, and a
scene that needs a failure's identity asserts it from this segment.

#### The connection-end lane

A link has exactly one terminal, and the App observes it on the connection capability that owns the
link (`BlengConnection.awaitEnd`). The lane is registered by Rust with the connect submission, so it
cannot miss the end it is about, and the end is *retained*: a caller that starts observing late — or
that was busy when the link ended — still learns what happened. No command has to fail first, which
is why the passive-disconnect scene no longer submits a read to discover an end.

Attribution is a fact about this capability, never a guess about the peer. `operator` means a
disconnect this App submitted is the command that produced the terminal; every other terminal is
`unrequested`, because "the link ended and we did not ask" is all a central can know. A platform
error is carried as reported, and its absence is not evidence of a clean peer request. `owner` is the
end of observation, not evidence that the link is down.

A caller deadline is its own bound, not a link fact: timing out reports an observation failure and
the end stays readable. At most one pull is pending per connection, so two consumers cannot report
one end twice.

### Pinned commands vs. readable content

The App is split into two regions, and the split is load-bearing:

- **Pinned (never inside a scroll container):** the `run-status` projection, the newest observation
  (`scan-latest`, `notify-latest`), the startup snapshot, the last outcome of the commands
  (`gatt-result`, `gatt-cancel`, `gatt-error`, `notify-error`, `rssi-value`, `rssi-error`), and every
  command the scenario can issue — session start/close (on **every** page), scan start/pull/stop,
  connect/disconnect/discover/RSSI, the GATT target and write inputs with
  read/write/subscribe/cancel-read, notification pull/disable, the two scan configuration inputs,
  and the `probe-console` families (auto-connect today) on their own page.
- **Content (the `workbench-content` ScrollView):** narration, the topology index with full rows and
  descriptors, bounded sample lists, the evidence trace, and the scan start/stop cancel controls.
  A human reads it; a scenario never needs it. The GATT read's cancellation is the exception that
  proves the rule: a scene has to drive it, so `cancel-gatt-read` is pinned while the scan cancel
  pair stays human-only until the cancellation scene drives those too.

A result line is machine surface for the same reason a command is: a scene reads the outcome of the
command it just issued. The subscription's own failure detail is the same kind of fact — it is the
outcome of *Enable* or *Disable*, so it lives in the pinned console with the write's, not in the
notification panel. Measured on the managed iPhone lane (`ios-managed-io-r2`), a `gatt-result`
sitting in the content was found by accessibility id and had text, while `is_displayed` was `false`
for every observation — the value existed and a scene could never read it. iOS reports off-screen
children as not displayed instead of omitting them from the tree, so "present" and "observable" are
different facts, and only a pinned element is both.

Android omits off-viewport `ScrollView` children from the accessibility tree, so anything a scenario
must touch would have to be dragged to — and a drag is only as accurate as the automation server's
gesture injection. Measured on the physical device: `mobile: swipeGesture` (area form) returns HTTP
200 and moves the content **0 px**, `mobile: scrollGesture` with an element handle also moves
**0 px**, and `mobile: dragGesture` with explicit coordinates moves a fixed ~216 px regardless of
its `speed` argument. That is not a tuning problem; it is why the layout is the design.

So the scenario asserts its own acceptance criterion: `_require_no_gesture` fails a run that had to
scroll, naming the count and the identifiers. A regression that puts a command back into the
content fails by name instead of leaving a run that is merely slower and sometimes wrong. The
reveal machinery remains for content a failure path still reads (an error detail, the evidence
trace), and is covered by reader-level tests rather than by a scenario.

Characteristics are addressed by **declared UUID**, never by a row key: a row key is an artifact of
discovery order and means nothing on another peer. The App resolves the declaration against its own
projection and reports `gattMatch`; the host acts only on `one`.

- **Physical RF and verdict**: named host scenarios, the live provider and scenario ledgers/bundles own real BLE evidence and cleanup. Neither a UI driver nor its idle/synchronization result owns BLE policy, fixture provisioning or resource-zero.

Rust and direct UniFFI tests remain under `crates/` and `tests/ffi/runtime`. Their deterministic,
FFI and artifact results complement these RN tests; none substitutes for a real RF gate.
The [layer responsibility guide](../../docs/research/cross-cutting/testing-layer-responsibilities.md#21-正式-rn-consumer-的具体责任落点)
maps these checks to the maintained owners. Detox evaluation is a bounded UI-only
comparison, not an adopted second framework, a replacement for native/RF gates, or permission
to delete legacy coverage; see the [evaluation report](../../docs/testing/reports/2026-11-08-rn-test-responsibilities-and-ui-tool-evaluation.md).

## What must leave the legacy host before it can be deleted

The scenario-level accounting for those rows lives in
[RN-internal RF 场景迁移对照](../../docs/research/react-native/rn-internal-rf-scenario-migration.md):
it maps each of the legacy consumer's 29 runtime modes to the layer that must prove it (managed
UI scene, host-driven with the UI as witness, or native/HIL), and states which of them have an
equivalent new-lane receipt today. Retirement is gated on that table, not on the rows below
being plausibly covered.

| Existing responsibility | Destination | Current boundary / removal condition |
|---|---|---|
| Formal Apple owner smoke and Android formal Nitro bridge contracts | Formal consumer native targets | Source ownership moved; no formal Nitro dependency remains in the legacy host. Actual runtime passes remain candidate/platform-specific, not inferred from the move. |
| Raw `u64`/bytes/error/ingress contract checks | Native UniFFI/runtime and artifact consumers | Preserve meaningful low-level coverage there and audit any Nitro-specific transport regressions separately; do not expose raw handles in formal JS merely to move a test. |
| Probe-specific queue, close and resource fixtures | The production owner and its neighboring tests, plus the native bridge where relevant | Equivalence audit remains required; preserve unique failure assertions until the receiving layer covers them. Process death and logical owner closure are not resource-zero. |
| Workbench browsing and run/evidence display | The unique formal App and its in-process scene drivers | Formal App owns navigation and bounded in-memory observations; Android has a scoped [lifecycle device candidate](../../docs/testing/reports/2026-11-07-formal-android-hans-lifecycle.md). This is not iOS, scan/RF, or reload/process-persistence evidence. |
| Typed scan and its host ownership | Formal package/App, `run_prepared_scene`, platform owners and the live fixture provider | API and UI implementation exist. The [named Android producer and sealed scan bundle](../../docs/testing/reports/2026-11-08-formal-android-managed-scan.md) now have concrete-owner software coverage, including preparation failures and independent outer lease cleanup. They have not acquired a new mobile scan/RF pass; iOS producer/runtime evidence remains separate. |
| Connect/disconnect | Reviewed formal operation surface and the same App | Implemented as owner-scoped Rust capabilities (`BlengPeripheral.beginConnect()` → `BlengConnection` → `beginDisconnect()`), with one shared failure projection. Jest covers the JS path; Rust `ffi_scan` tests cover success, single-consumer waits and retired-owner refusal. No device link has been exercised, so this is not RF evidence. |
| GATT discovery/read/write/RSSI | Reviewed formal operation surface and the same App | Implemented as an owner-scoped capability tree (`beginDiscoverGatt()` → session → service → characteristic → `beginRead`/`beginWrite`), with `kind()` tri-state and data-only descriptors. No handle crosses into JS; a replaced topology fails closed at admission. Rust `ffi_scan` tests cover projection, single-consumer waits, replaced-topology rejection and zero/negative RSSI; the Workbench keeps at most 8 addressable characteristics. No real device read/write has been exercised. |
| Notification subscriptions | Reviewed formal operation surface and the same App | Implemented as a bounded stream (`beginSubscription` → `waitUntilEnabled` → `nextEvent` / `beginDisable`) on a lane inside the owner's one observation projection. The enable receipt carries the actual mode; overflow fails the stream closed; retirement is an explicit `ownerClosed` terminal. Rust `ffi_scan` and Jest cover it; no real notification has been received. |
| Strict indication / RF notification suites | Formal capability plus the live nRF52840 provider and a HANS-reviewed host | Not retargeted: they still need a real fixture lease, strict-mode evidence and the RF ledger migration. The typed capability is a prerequisite, not replacement coverage. |
| Actual Hermes invalidation/pending reload | Formal engine-lifetime seam and host/runtime gate | Apple has [runtime binding](../../docs/testing/reports/2026-11-10-apple-engine-runtime-binding.md) and [same-process RCTHost reload](../../docs/testing/reports/2026-11-11-apple-rcthost-reload.md) evidence: actual package JS, RN callbacks and old-retirement-before-new-start admission over scripted backends. The [AppDelegate/shared delegate and Fabric subtree regression](../../docs/testing/reports/2026-11-11-apple-formal-delegate-rendered-reload.md) now use the same predecessor wiring and check a small native-mounted status page. Full workbench/window/device App reload, RF and Android migration remain incomplete; these tests are not a full App/UI gate. |
| RF full-suite and separate REJECT/radio/permission/multi-DK gates | Named formal host scenarios/ledgers and the existing live provider | Retarget only after their formal capabilities exist; retain distinct protocols, live fixture/cleanup evidence and known platform limitations. Lifecycle or scan smoke is not replacement coverage. |

The formal host now renders the typed initialization snapshot from `start()` and tests its
non-ready/independent-authorization semantics. Its workbench keeps the owner/action state above
view switching, retains initialization/failure observations after close and leaves the Evidence
page visible; only a new explicit session replaces that UI summary. These software-tested
presentation responsibilities do not migrate the legacy RF/reload protocols. Formal scan has
an approved typed API, bounded Rust delivery and an explicit manual App workflow. Its earlier
[scan integration candidate](../../docs/testing/reports/2026-11-04-formal-rn-scan.md) compiled both native
App/test graphs; current concrete Android prepared-scan tests exercise the real owners behind
isolated SDK/serial/UI boundaries, not actual Nitro or RF. Those software passes and the separate
Android lifecycle device pass do not establish mobile scan/runtime/RF. Typed close separately
reports logical owner closure and cleanup failure.
Actual reload and host-ledger migration remain incomplete; connect, disconnect, GATT read/write
and notification subscriptions are exported but only software-tested. JS
recovery projection is separate contract work, not completed by moving tests. Preserve full-suite
versus opt-in fault gate identities and known platform failures.
Delete the legacy integration and host only after the remaining requirements have a meaningful
owner/test mapping, live references are removed, and applicable new-candidate evidence exists.
Historical reports remain unchanged; their results never transfer to a new package/candidate.
