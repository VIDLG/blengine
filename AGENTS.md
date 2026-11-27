# AGENTS.md

## Purpose and source of truth

Read this file before researching or changing BleHub. It contains durable rules and navigation,
not a run log.

When sources disagree, use this order:

1. Current user conversation.
2. This file.
3. [`.plan/PLAN.md`](.plan/PLAN.md), the single approved architecture plan.
4. `docs/research/`, downloaded official evidence, local source, then narrow official online material.

Reports preserve candidate facts and failures; they do not change the plan, freeze ABI, or turn a
pending gate into a pass.

## Current baseline

- Release order: **Apple → Android → Apple + Android ABI freeze**. Windows and old Tauri/BLESS/QEMU/
  Android-Peripheral/RN-macOS lanes are historical unless explicitly re-admitted by the plan.
- Active Peripheral: standalone **nRF52840 Multifunction Fault v2 + BTP**. Resolve live identity,
  capabilities and installed image from receipts and the physical lease; build evidence is not RF evidence.
- RF priority: real Mac controller, real Android/iPhone Central and physical nRF52840 DK. Sniffer is
  passive diagnostics; no silent firmware, radio, permission or Linux/BabbleSim setup.
- RN has one workbench: **Tests → Current run → Evidence**. Browsing issues no BLE commands. Host
  tooling owns installation, physical control, cleanup and final bundle verdict.
- UI gates target platform-native drivers (iOS XCTest, Android instrumented). The iOS in-process
  scene driver is live: a host-owned declarative plan over the App's pinned machine surface, one
  `XCTAttachment` receipt read back with `xcresulttool export attachments`, no WDA/Appium/tunnel.
  It cannot scroll by design — a machine fact the tree lacks is pinned in the App instead — and it
  answers only system prompts its plan declares, by declared title and button text. The managed
  envelope is driver-independent (`SceneDriver`): **every** managed iOS scene (`scan`, `notification`,
  `indication`, `io`, `cancellation`, `write-reject`, the two `ccc-*-reject` scenes,
  `passive-disconnect`, `recovery`, `recovery-ten`, `background`, `disconnect`, `connect-failure`)
  now runs in-process
  through the same
  admission/lease/fixture/cleanup path, so iOS keeps no Appium lane at all. Android now has the
  same in-process shape (`scene_android_native` + `BlengSceneDriverInstrumentedTest` over the
  same plans), with one platform fact encoded: an instrumented test runs inside the target
  package's own process, so a scene is one process whose host actions are in-band `handoff`
  steps, and binding/cleanup prove the installation rather than the launch pid. A repeated
  host action is one *occurrence* per request file, never one per action name — a scene that
  drops the peer every cycle must be answered every cycle. Twenty managed scenes exist; on
  the real Android device `scan` … `indication`, `background`, `io`, `disconnect`,
  `connect-failure`, `cancellation`, `write-reject`, the two `ccc-*-reject` scenes,
  `passive-disconnect`, `recovery` and `recovery-ten` passed in one sequence on 2026-11-24, and
  `link-facts` (MTU query/request, connection priority, GATT-cache invalidation) passed on
  2026-11-25 as its own run, then `descriptor-io` (the peer's own CCCD read across a
  subscription's lifecycle) and `devices-query` (the platform's `connected` inventory must
  contain the linked peer) each passed on 2026-11-25 as its own run, `auto-connect` (a
  standing "connect when the peer appears" request, proved against a peer the host had
  withdrawn) passed on 2026-11-26 as its own run, `devices-kinds` (`bonded`/`companion`
  inventories, answered with no peer and no link at all) passed on 2026-11-26 as its own run, and
  `adapter-control` (radio power in both directions, also with no peer: both platforms refuse,
  Android at the operation terminal and Apple at dispatch) passed on both devices the same day.
  `auto-connect` introduced the `Probe` page:
  capabilities outside the observation main line get their own pinned surface instead of
  growing `CommandConsole`, whose fixed height has caused two real regressions. Each of the
  newest five caught a real defect: `disconnect` (App claimed `subscribed` on a dead link),
  `connect-failure` (machine surface reported a failed connect as `failure=none` while the
  panel and ledger carried it), `link-facts` (a longer control strip pushed a later control out
  of the viewport, where Android renders nothing), `descriptor-io` (a `tapUntil` whose expected
  value already equalled the on-screen value was skipped entirely, so the final "unchanged" read
  was a fake witness; fixed with a monotonic `descriptorReadTotal` and a plan-declared `advance`
  guard) and `devices-query` (verification exposed that `tapUntil` with `advance` re-tapped
  before its operation settled, overflowing the App's 32-entry ledger window and dropping the
  scene's first actions; fixed in both drivers by waiting for the counter to advance after each
  tap, and `_require_ledger` now refuses a truncated window explicitly). `auto-connect` caught
  no App defect; it caught a missing page declaration in its own plan, which is now stated as a
  `workbench-page` wait so moving a control between pages fails by name. `devices-kinds` caught
  a structural one: the probe borrowed a `ScanRun` the scene never creates, so the button was
  enabled and the handler returned immediately; inventories other than `connected` are now
  fenced on an owner-scoped probe record instead of a run (and its ledger shape is derived from
  each answer, so "one answered, one refused" is legal where "both the same" is not required).
  **Sequences**: the whole order passed in one Android run on 2026-11-26 (`scene_sequence=passed`,
  all 20, every bundle `integrity=passed`, `cleanup_errors=[]`, 11.9 min; closing run
  `.work/android-native-sequence/2026-11-26-d`, report
  `docs/testing/reports/2026-11-26-closed-loop.md`), including `background`. That scene's earlier
  failure was not a flake but a **fake witness**: the post-return pull's wait was satisfied by the
  value already on screen, so `drained` was read before the away-window value arrived
  (`notifyTotal=1`, then 2 a moment later). The plan now waits on the App's own counter. iOS has
  its own sequence: the whole order also passed in one iPhone run on 2026-11-26
  (`scene_sequence=passed`, all 20, every bundle offline `integrity=passed`, `cleanup_errors=[]`,
  33.4 min; report `docs/testing/reports/2026-11-26-ios-sequence-20.md`). Both platforms' sequences
  in the same batch, with the 4 native lanes, 6 artifact consumers and 9 snapshots, are recorded in
  `docs/testing/reports/2026-11-26-closed-loop.md`. Three iOS scenes have paid an extra 9-23 min
  (`link-facts` 1407 s, `ccc-enable-reject` 1168 s, `devices-kinds` 553 s) while the same plan
  standalone runs in 13-36 s. The shape is measured: one press costs `press≈120.6 s` while
  `hittable≈0.1 s`, and `setText` spends ~61 s inside a single element query — XCTest's 60 s units,
  i.e. the device's automation channel degrading and recovering, with no code-level cause found.
  Mitigations: `tap` records `hittable=`/`press=`, a slow phase keeps XCTest's own console tail,
  the iPhone display is checked before any lease (a sleeping screen accepts every command and
  answers none — measured as a device screenshot with luminance 0 while `lockState` said unlocked),
  and no plan waits on a transient state it can prove another way. The `auto-connect` scene also
  states that an *immediate* adoption of the resumed advertisement is its expected answer; the
  fixture used to refuse it, which made the scene a race against the App's own retry timer.
  Those iOS rounds caught six real
  defects: the software keyboard stayed up after `setText` and covered pinned controls
  (`pull-notification` at y≈622 under a keyboard spanning y 578–926) — the title is now the
  App's tap-to-dismiss surface and the driver retires the keyboard after every text entry;
  `linkEnd` still said `operator` while the package's own vocabulary is `requested`; the Apple
  backend decoded a CCCD's value only as `Data` although CoreBluetooth delivers an `NSNumber`;
  a direct CCCD write made CoreBluetooth *assert and abort* — it is now a typed `NotSupported`
  at the dispatch boundary, published as `descriptorWrite=unsupported`; `failureIdentity` let a
  platform detail without a code (`[Android/not reported …]`) beat `domain/code` and discarded
  the only assertable word; and the App matched one full `not_supported` code although the
  supervisor composes it per boundary. Both are now one rule each — `isNotSupported` in the App
  and `is_not_supported` in the host, on the code's terminal segment — and every ledger
  derivation accepts a refusal at either boundary (submission or operation terminal), because
  that is one answer, not a broken record (reports
  `docs/testing/reports/2026-11-26-ios-seven-scenes.md` and
  `docs/testing/reports/2026-11-26-adapter-control-scene.md`). The Android side of the
  scenes whose plans or App behaviour changed was re-run the same day (`disconnect`,
  `descriptor-io`, `auto-connect`, `adapter-control`). Appium (driver, toolchain,
  helper APKs, HANS policy and every recipe) was deleted on 2026-11-23: nothing is kept as a
  fallback diagnostic. Maestro was removed
  2026-11-22; Detox is source feasibility only. HANS (this ROM's process classifier) **is
  enabled** on the lab phone, and the stall recorded against it belonged to the deleted Appium
  UiA2 helper UID — the in-process lane has no such process and the evidence layer refuses a
  HANS receipt. The one process a scene depends on runs a `connectedDevice` foreground service
  exactly while it is backgrounded (measured), which is outside the AOSP freezer's cached band,
  and `background` passes in that state. If the App is ever observed hung, diagnose in this
  order: HANS props → foreground service present in the away window → trace has `notify:resumed`
  → only then an explicitly authorized classification read. Never whitelist a helper or toggle
  HANS globally (report `docs/testing/reports/2026-11-26-hans-rescope.md`).
- ABI is unfrozen and review readiness is not release/Phase exit. Stress remains deferred until a
  functionally converged, human-reviewed, release-shaped candidate exists.

## Architecture guardrails

- `crates/blehub` owns canonical `BleHub`, commands/DTOs/events/errors, foreign traits and direct
  ingress; it owns no runtime, worker, timer, mailbox or canonical BLE state.
- `crates/blehub-uniffi` and `crates/bleng-uniffi` each own one final Stable UniFFI image for their
  package line. Never add a duplicate API/image for a test.
- Commands are fire-and-forget. BLeng `begin_*` is a separate bounded submission boundary with async
  wait and independent cancel.
- Backends own native handles, callback correlation, bounded serialized I/O and watchdogs. Native
  submission is not remote delivery; platform cancellation is not rollback; process death is not resource-zero.
- Production backends live under `platforms/`; reusable Rust support under
  `tests/support/blehub-testkit`; FFI/artifact consumers stay separate from UI/RF scenarios. Apple
  uses CoreBluetooth directly; Android uses Framework BLE APIs.

### API snapshots are the freeze's mechanical stand-in

The nine reviewed snapshots (`api-snapshots/{rust,kotlin,swift}.txt` for BleHub,
`bleng-*.txt`, `supervisor-*.txt`) are generated from the canonical images and compared in CI by
`binding snapshot-check` / `binding bleng-snapshot-check`. They are not documentation: they are the
thing that fails when a public shape drifts.

Updating them is a **reviewed act**, never a repair:

- allowed: a deliberate public-API change whose DTO/handle/error shape was reviewed, compiled by all
  consumers, and recorded in a report under `docs/testing/reports/`;
- never: running `snapshot-update` to make a red gate green, to land a refactor's incidental output,
  or because a generated file was easier to regenerate than to read;
- the update lands with the change that causes it, so the diff of the snapshots is part of that
  review, and re-adding a removed symbol later is an unfreeze, not a fix.

After the freeze, scenes may still be added (a scene is not ABI); any DTO, handle, error or
snapshot change goes through that review again.

## Research, safety and change discipline

- Inspect local source, tests and indexed codegraph first. Label conclusions as verified, inferred or
  unverified. Keep scenario policy above generic helpers.
- Change code in its owning repository. Before adding a helper/entry, check owners, imports, Just
  recipes and discovery; do not create duplicate codecs, registries or runners for symmetry.
- Preserve identity, lease, deadline, permission, error, watchdog, rollback, cleanup and sealed-failure
  contracts. Do not weaken them to make tests pass.
- Do not adopt unknown sessions, kill/restart foreign ADB daemons, flash/reset firmware, toggle DUT
  radio or close unknown Apps. Destructive mobile/RF actions require explicit scope and a fresh lease.
- Use `pixi run just <recipe>`. Ordinary runs do not install tools, accept licenses, request passwords
  or replace SDK/Xcode. Signing, keychain unlock, `sudo` and developer authorization are human-only.

## Git, validation and documentation

- Inspect the worktree first. Preserve unrelated changes; do not stash/reset/clean, commit, push or
  create branches unless explicitly requested.
- Keep one architecture plan. Put durable responsibilities in `docs/research/`, candidate facts in
  `docs/testing/reports/`, and consumer ownership in `tests/consumers/README.md`.
- Start with targeted checks, then relevant combined checks. Report exact commands and results; do not
  infer current passes from old reports, package builds or synthetic tests.
- For tooling/docs run `pixi run just fmt` and `pixi run just lint`; for Markdown run
  `pixi run just docs-check-mermaid` when applicable. Authored Markdown must be UTF-8, fenced,
  conflict-free and free of trailing whitespace.

## Evidence entry points

- Plan: [`.plan/PLAN.md`](.plan/PLAN.md)
- Responsibilities: [`docs/research/cross-cutting/testing-layer-responsibilities.md`](docs/research/cross-cutting/testing-layer-responsibilities.md)
- ABI freeze readiness (mechanical gates, human review items, what is explicitly not a freeze condition): [`docs/research/cross-cutting/abi-freeze-readiness.md`](docs/research/cross-cutting/abi-freeze-readiness.md)
- Apple gates: [`docs/testing/apple.md`](docs/testing/apple.md)
- RN host ownership: [`docs/research/react-native/react-native-host-tooling-architecture.md`](docs/research/react-native/react-native-host-tooling-architecture.md). Test layering, conventions and guards: [`docs/research/react-native/host-tooling-test-architecture.md`](docs/research/react-native/host-tooling-test-architecture.md). Async/await convenience layer over `begin*`/`wait`/`cancel`: [`docs/research/react-native/pending-operation-layer.md`](docs/research/react-native/pending-operation-layer.md). Background/foreground `background` scene and its declared dwell window: [`docs/research/react-native/background-connection-scene.md`](docs/research/react-native/background-connection-scene.md). Generated signature-level TypeDoc site (derived from specs, never hand-edited): [`docs/research/react-native/typedoc-site.md`](docs/research/react-native/typedoc-site.md). The former RF-lab protocol doc is historical: [`docs/research/archive/react-native-rf-lab-test-architecture.md`](docs/research/archive/react-native-rf-lab-test-architecture.md)
- Functional convergence: [`docs/testing/reports/2026-10-25-core-functional-convergence.md`](docs/testing/reports/2026-10-25-core-functional-convergence.md)
- Historical Appium diagnosis (lane removed 2026-11-23): [`docs/testing/reports/2026-11-10-formal-android-appium-root-cause-diagnosis.md`](docs/testing/reports/2026-11-10-formal-android-appium-root-cause-diagnosis.md)
- Android in-process scene lane (first device pass, root causes): [`docs/testing/reports/2026-11-23-android-native-scene-lane.md`](docs/testing/reports/2026-11-23-android-native-scene-lane.md)
- Historical Appium upstream review (lane removed 2026-11-23): [`docs/testing/reports/2026-11-10-formal-android-appium-upstream-review.md`](docs/testing/reports/2026-11-10-formal-android-appium-upstream-review.md)
- Consumer ownership: [`tests/consumers/README.md`](tests/consumers/README.md)

Detailed candidate reports remain under `docs/testing/reports/`; do not duplicate their timelines here.
