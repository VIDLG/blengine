# RN notification subscription projection — 2026-11-12

## Verdict and scope

The formal RN package line now exports one notification-subscription capability. It is the first
capability in this line that is a **stream** rather than a request/response receipt, so the design
question was not "how do we expose notify?" but "where does a subscription's liveness live?".

Verified today:

- BLeng contract: `EnableNotificationsOperation::wait` returns
  `NotificationSubscription { subscription, actual_mode: Option<NotificationMode> }`;
- 6 new Rust `ffi_scan` notification tests and 137 `bleng-supervisor` tests pass;
- 8 new Jest Workbench tests (87 total) pass;
- `bleng-snapshot-check` verifies 6 reviewed API snapshots;
- both formal native graphs compile (Android App + `androidTest` APK, iOS App + native test target +
  Nitro-free `PlatformContract`);
- the **artifact** consumers run: `apple consumer-bleng-macos` passes
  `supervisor_typed_operations` (which now asserts `actualMode == .indication`), and
  `ffi android-bleng physical` passes **`OK (15 tests)`** on device `3ac5cc09`;
- stream terminal semantics are now explicit and tested in both lanes (see below).

Not verified: a real notification from a real peripheral, the gated RF notification/strict-indication
scenarios, and any real iPhone. See "Unverified boundary".

## Why the contract changed

Before this change the typed enable operation returned only `SubscriptionId` and documented that
"the actual mode remains in the canonical event". That splits one operation's completion across two
channels: a caller that awaits `wait()` learns *that* it subscribed but not *which mode* the backend
chose, and the only way to find out is to also correlate `NotificationStateChanged` on the event
stream. Correlating would have made "subscription is active, mode unknown" a real and silent state,
because the event may be read before or after the caller asks.

`actual_mode` is therefore part of the receipt. It stays `Option<NotificationMode>` because the
canonical contract already models a platform that does not report the selected mode; the projection
keeps that absence instead of guessing from the requested
`NotificationModeRequest::PlatformDefault`.

This is a BLeng package-line change, not a BleHub core change. It is covered by the regenerated
reviewed snapshots and by the artifact consumers (Swift + Kotlin) that consume the public image.

## One owner, one reader, two lanes

`SessionObservationProjection` (formerly the scan-only projection) fans the owner's existing
canonical event stream out to typed lanes:

```text
one owner's Bleng public FIFO
→ the existing single Swift/Kotlin reader
→ event_projection.project(event)
   → scan lane        : optional scan sink envelope (unchanged)
   → notification lane: bounded backlog for the registered subscription
→ the existing raw event sink / Apple recovery inbox keep their own paths
```

The FFI ingest object is renamed `SessionEventProjection` because it is no longer scan-only; the
scan types (`ScanEvent`, `ForeignScanEventEnvelope`, `ScanDelivery`) keep their names and behavior.
`ObservationProjectionLease` revokes **both** lanes while holding both lane locks, so a concurrent
submission either observes the revocation flag or finishes before the drain.

The notification lane is registered *before* the enable is submitted, under the same lock that
performs the bounded BLeng submission, and binds on the `NotificationStateChanged` carrying its own
enable command id. Two consequences worth keeping:

- no value can slip between the enable's state change and the lane becoming observable;
- a subscription left over from an earlier enable for the same characteristic can never be mistaken
  for this one, because the bind key is the command, not the attribute.

## Bounded stream rules

| Concern | Rule |
|---|---|
| Pull | single pending pull; a second concurrent pull is `delivery.unavailable` |
| Backlog | 64 values / 16 KiB, then **fail closed** and discard the unread backlog |
| Caller timeout | caller's deadline on the operation's own runtime; never a native teardown |
| End | explicit `disabled` \| `connectionClosed` \| `ownerClosed`; retirement is never a synthesized disable |
| Disable | addressed through the identity learned from this subscription's own enable |
| Unknown identity | `beginDisable()` before the subscription is observed fails with `notification.subscription_unknown` |

Overflow discarding rather than dropping a single value is deliberate: after a gap, ordering and
completeness can no longer be claimed, so the stream reports failure instead of pretending the
missing value never existed. This mirrors the existing scan delivery policy.

## Test evidence

Rust (`crates/bleng-supervisor/tests/ffi_scan/notification_view.rs`):

1. ordered values including a zero-length value, plus the actual-mode receipt and its absence;
2. disable ends the stream and no further value is synthesized;
3. `beginDisable()` before the subscription is observed is refused, not guessed;
4. a slow consumer fails closed at the value budget and the unread backlog is discarded;
5. a retired owner ends the stream as `ownerClosed` and new enables fail with
   `supervisor.no_active_session`;
6. a value from another subscription or session is never delivered.

Jest (`tests/consumers/react-native-bleng/src/Notifications.test.tsx`): requested mode follows the
advertised properties, actual mode reported/absent, value counting with bounded samples, an
observation timeout not ending the subscription, stream terminal disabling the pull, overflow
surfaced as a failed stream, disable receipt, and control withdrawal while the engine closes.

## Stream terminal semantics (resolved)

The first device run of the pre-existing ART gate failed one test:

```text
supervisorScanProxyPreservesNativePayloadAndDeliveryAcrossWaitAndPullErrors
  pull ended before pending-state receipt   (PublishedBlengTest.kt:407)
```

The cause was a wrong premise in that test, not an implementation defect: it consumed the
`Stopped` terminal and then expected the next pull to park. `ScanDelivery` deliberately retains its
terminal, and that retention was already asserted elsewhere
(`tests/scan_delivery.rs`: a replaced scan's old endpoint re-reads the same `Stopped`, and retirement
replaces it with `OwnerRetired`).

The rule is now stated and tested explicitly rather than left to implementation accident:

| Fact | Owner | Delivery |
|---|---|---|
| operation result (`wait`) | single-consumer (`OperationResultConsumed`) | exactly once |
| stream terminal (`nextEvent`) | retained | readable more than once until retirement |
| retirement | replaces the retained terminal | definite `OwnerRetired` / `ownerClosed` |

Rationale: a consumer polling on its own schedule must still be able to learn how the stream ended,
and exactly-once delivery belongs to the operation receipt. The two lanes obey the same rule, and
retirement is the only event allowed to replace a retained terminal — after retirement the old owner
can no longer answer for the old stream at all.

Changes made:

- `ScanDelivery` and `NotificationDelivery` document the rule where the behavior lives.
- New Rust test `a_retained_scan_terminal_is_readable_more_than_once_until_retirement` reads the
  terminal three times, checks retention costs no advertisement capacity, then checks retirement
  replaces it twice.
- The notification test now re-reads the `Disabled` end twice, then asserts retirement turns it into
  `OwnerClosed`.
- The ART test proves pending-pull bookkeeping **on a live scan with an empty queue** (the only
  state where a pull can park), proves a concurrent pull is `PullBusy`, then proves the real
  `Stopped` wakes the parked pull, then re-reads the terminal as the retained fact. The
  shutdown-resolves-a-parked-pull case stays in Rust, where it is deterministic.
- The ART harness teardown now uses the idempotent `shutdown_view()` instead of a second strict
  `shutdown()`. A body that already closed the owner was turning cleanup into a false
  `NoActiveSession` failure; bodies that need the strict close still assert its exact
  resource-zero return value.

Result: `pixi run just ffi android-bleng physical` → **`OK (15 tests)`** on device `3ac5cc09`.

## Managed notification scene (wired, not yet run on a phone)

The formal App's subscription capability now has a managed UI scene that shares one producer, one
prepared runner and one evidence publisher with the scan scene. Only a `SceneEvidence` descriptor
differs:

| field | scan | notification | indication |
|---|---|---|---|
| outer scope | `formal-android-managed-scan` | `formal-android-managed-notification` | `formal-android-managed-indication` |
| inner scope | `formal-rn-prepared-scan` | `formal-rn-prepared-notification` | `formal-rn-prepared-indication` |
| `rf` claim | `scan-ui-observation` | `notification-ui-observation` | `indication-ui-observation` |
| characteristic role | — | `updates` | `indications` |

`SceneEvidence` is the only place a scene is named, so a bundle can never carry a scan scope with a
subscription trace. `bleng_scan_runner` keeps its scan-era module name deliberately: every scene
here is a manual-scan workbench run and the scan scene is the shortest one; the runner docstring
says so instead of implying a second runner.

Derivation is profile-only. `exercise_owned_notifications` loads the session profile through the
canonical `load_session_profile`, takes the characteristic whose `role` matches the scene, and
derives all three expected facts from it:

- the characteristic UUID the App must be told to subscribe to;
- the mode the App will request, because the App asks for exactly what the characteristic
  advertises (`notifiable` → `requireNotification`, else `requireIndication`);
- the payload, from `notification_value`.

Nothing in the host or the UI scene hardcodes a UUID, a mode or a value, and the scene still
asserts the App's own `notify-mode` projection matches the derived mode — an unreported or
different mode fails the subscription instead of being read as the requested one.

The emission is a host action between two ownership guards, and it happens only after the App has
reported `subscribed`: the firmware's `set_value` emits nothing while the peer's CCC is disabled, so
the ordering is enforced by the peer, not by the host's optimism. The host returns the exact bytes
it applied and the UI scene refuses any pulled projection that is not exactly that value, so an
enabled subscription alone is never evidence of delivery.

Disable is a receipt, not an end. The scene waits for the App's `notify-disable-wait` receipt, then
pulls once more and requires the stream's own terminal. `useManualScan` settles that pull with
`ended: disabled`, and the trace assertion requires exactly that detail, so a connection close or an
owner close cannot be mistaken for a completed disable. After the terminal the pull control is
withdrawn and the received count must be unchanged; after close the retained terminal stays `ended`
while the scan phase becomes `ownerClosed`.

Evidence is asserted exactly. `_notification_actions` lists the ordered `(action, detail)` pairs for
the scene and the verifier parses the App's bounded trace, requires contiguous sequences, requires
every result to be `succeeded`, and refuses a trace that reported `evidence-omitted`.

Hardware-free regressions:

- `tools/tests/react_native/test_bleng_notification_ui.py` (10): declared observation trace, parked
  pull settled in place, host/App value mismatch rejected, unreported and wrong modes rejected,
  indication scene, UUID-not-position row selection, unaddressable target reported rather than
  skipped, disagreeing topology summary, receipt-without-terminal never passing, and a projection
  longer than 16 bytes matched in full.
- `tools/tests/react_native/test_bleng_scan_host.py::OwnedNotificationTests` (5): profile-derived
  target/mode/value through the real fixture, emission after the observed enable, unknown role
  rejected before any UI call, strict indication (and its refusal when the App reports
  notification), and the single scene entry point receiving the profile facts.
- `tools/tests/react_native/test_bleng_scan_evidence.py` (+3): a notification bundle publishes and
  replays offline under its own scope/`rf`, a scan scope cannot carry a subscription trace, and a
  subscription scope cannot keep the scan trace.
- `tools/tests/react_native/test_bleng_android_prepared_scan.py` (+1): the prepared runner drives
  the subscription scene end to end through the concrete Android owners and leaves
  `scenario_status=passed`, `host_gate_status=pending`, the scene trace and a clean cleanup order.

Whole-tree results for this change: `tools/tests/react_native` **2003 tests OK (1 skipped)**,
`tests/peripherals/nrf52840` **513 tests OK**, `just fmt` and `just lint` clean.

Entry points: `just react-native::formal-host-android-managed-notification` and
`formal-host-android-managed-indication` forward to the same CLI with `--scene`; the flags,
admission, lease and cleanup are unchanged and still require explicit install/permission/fixture
authorization.

## Real-device attempt (2026-11-12, blocked by automation stability)

Six managed `notification` scenes ran against `3ac5cc09` (`PHB110`, rooted) and DK A
(`BleHubMultiRfV2DKA`, profile `multifunction-rf-v2-a`, `jlink:1050252028`). None produced a
notification value. The runs are recorded rather than retried, and every one released its phone lease
except where cleanup itself could not complete.

| run | reached | primary failure |
|---|---|---|
| `d3da62e1` | preparation | expected helper SHA256 differed from the installed set; nothing installed, no UI |
| `e8aac233` | `scan:running` | `delivery.advertisement_overflow` (the App's bounded scan lane filled before a match) |
| `f9593147` | `started` | `adapter-state` and `workbench-content` were both absent from the accessibility tree |
| `d0cedf7f` | `gatt:ready` | `mobile: scroll` could not reach `gatt-panel` |
| `9ad28ebd` | `gatt:ready` | 14 s HTTP read timeout: the same stage as the documented UiA2 stall |
| `751a2c00` | `started` | one `mobile: scroll` took 15.3 s, returned 404, and the next scroll never answered |

What this established, and what changed because of it:

1. **The Appium 4 line is the installed one.** The device's three helpers are byte-identical to the
   pinned `android-4.0.0-beta.7-uia2-9.0.1` toolchain receipts in its own `installed.json`, so the
   3.9.0 baseline hashes are stale for this phone. Device admission now passes with the 4.x set;
   nothing about it was guessed from a readback.
2. **A bounded manual scan cannot stay unfiltered.** The scan lane's 64-advertisement budget filled
   before the scene's first pull in this RF environment. The Workbench now exposes the scan request's
   own `serviceUuids` (`scan-service-filter`), the host declares the attested contract's service
   there, and the scene verifies the App's `scan-filter` projection before it starts anything. An
   unfiltered run is still expressible (empty filter) and still fails closed.
3. **Configuration is not a live control.** Adding that input to the pinned header pushed
   `workbench-content` off-screen, and UiA2 pruned it from the tree — which is why `adapter-state`
   disappeared. Timeout and filter inputs moved into the scrolled content, and the operational panels
   now precede narration and bounded sample lists.
4. **`mobile: scroll` is the poison, not the delay.** The UiScrollable scroll-to handler spent 15.3 s
   and then wedged the automation server. The reveal path is now one bounded `mobile: swipeGesture`
   per clipped read, driven by the caller's own deadline, and a driver-side gesture failure is
   reported as a miss rather than an action.
5. **The run budget has to survive preparation.** Uninstall/install of the candidate plus the
   automation and fixture startup consumed a 240 s budget, leaving 0.03 s for the scene (which then
   failed on its first HTTP call). The single finite run budget is now 480 s, the prepared runner is
   handed a scene-sized window cut from it, and a run refuses to start a scene with less than a
   quarter of its budget left instead of starting one that cannot finish.

Residual blocker: after a fresh session the App's views intermittently never enter the accessibility
tree (`element` 404 with `getCurrentPackage` still answering), and this persists until the run
budget expires. That is the same unresolved UiA2/ADB instability already recorded in
[`2026-11-10-formal-android-appium-root-cause-diagnosis.md`](2026-11-10-formal-android-appium-root-cause-diagnosis.md),
now with a second, independent trigger (scrolling and element lookup rather than App close). No
notification value has crossed the air, and no bundle from this section may be read as an RF pass.

Offline validation after these changes: `tools/tests/react_native` **2014 tests OK (1 skipped)**,
`tests/peripherals/nrf52840` **513 tests OK**, Jest **88 passed**, `just fmt`/`just lint`/
`docs-check-mermaid`/`git diff --check` clean.

## First real RF notification (2026-11-12, Android 3ac5cc09 + nRF52840 DK A)

A real value crossed the air and was verified end to end. The lane used is Android
instrumentation (`com.blehubblenghost.test`), not a UI automation server: the App's own
process opens the subscription through the same Nitro surface the JS API calls, and the
host owns the fixture, the timing and every device action. The reason is the unresolved
UiA2 stall documented earlier in this file — the accessibility-tree driver is the unstable
component, and *delivery* is what this gate has to prove.

| run | scene | role | mode | outcome |
|---|---|---|---|---|
| `31d5abb8` | `single` | `updates` (`fff20000`) | `notification` | value `ef` matched, `status=passed` |
| `04588dd4` | `single` | `indications` (`fff50000`) | `indication` | value `ef` matched, `status=passed` |
| `a85a1669` | `sequence` | `updates` | `notification` | 8 values, none lost/duplicated/reordered |
| `519d0df2` | `overflow` | `updates` | `notification` | 96 sent, slow consumer got `delivery.value_overflow` |
| `06cff58a` | `single` | `updates` | `notification` | re-run on the final test APK |

The three scenes are one binary and one host lane; `--scene` only selects the test method and
the stage contract. `single` and `sequence` require the stream to end on its own disable;
`overflow` deliberately never disables, because the lane has already failed closed and only the
peer teardown (disconnect) applies.

### What each scene rules out

- `single`: a subscription that is enabled but delivers nothing, and a delivery whose payload
  differs from what the peripheral applied.
- `sequence`: silent loss and duplication. The firmware fixes one value length per
  characteristic, so the scene keeps the profile's payload and varies the count; a dropped or
  duplicated admission shows up as a count mismatch, and a reordered backlog as an index
  mismatch.
- `overflow`: a hidden gap. The host admits 96 values (more than the 64-value budget) while the
  consumer reads nothing; the first read must report `delivery.value_overflow`, never a value.
  Receiving a value there would be the exact defect the bounded lane exists to prevent.

Both bundles report `scenario_status=passed`, `phone_status=passed`,
`fixture_status=passed`, empty cleanup errors, the fixture's passive close with
`advertising_inactive=true` and `resources_closed=true`, and the phone's Bluetooth
permissions restored to their captured `denied` baseline. `host_gate_status` stays
`pending`: this is a scene observation, not the release gate.

What the scene proves, in order (each stage is a logcat marker from the App's own process):

1. `started` — the owner started with `state=poweredon`, `scan=authorized`;
2. `scan-submitted` / `scan-started` — a filtered scan (`serviceUuids=[contract service]`);
3. `advertisement-matched` — the only advertisement carrying `fd600000…` was
   `BleHubMultiRfV2DKA`, i.e. the attested fixture;
4. `connected`, `gatt-ready` — capability-derived link and discovery;
5. `subscribed:notification` / `subscribed:indication` — the platform **reported** the
   mode, and the test asserts it equals the mode the profile's properties imply;
6. `value-matched:ef` — after that report the host wrote the profile's
   `notification_value` through the fixture's live BTP session, and the value the
   subscription pulled is byte-identical (`ef`);
7. `disabled` — the disable receipt;
8. `terminal` — the stream's own terminal, asserted to be `disabled` rather than a
   connection or owner end;
9. `disconnected` — the central released the peer, so the passive close could verify
   `no-peer` instead of refusing.

Host boundaries that made this work, and are now part of the lane:

- the scene's target facts (characteristic UUID, mode, payload) come from the session
  profile only — no UUID, mode or value is written in the runner or the test;
- `DirectFixture.drain_events` lets an in-process host pump the tester's own event queue
  (queue **and** wire) as the long-running fixture loop does, which is what makes the GAP
  connect/disconnect observations and therefore the passive close correct;
- `DirectFixture.peer_present` / `observed_peers` are read-only controller facts used to
  wait for the central's own disconnect before the one-shot close;
- a scene that starts with less than a quarter of its budget left is refused rather than
  started, and a stuck instrumentation run is bounded by force-stopping **only** the
  installation whose bytes match the authorized APK.

Regression coverage added offline (`tools/tests/react_native/test_bleng_android_native_notification.py`, 8
tests): marker parsing with an exact tag boundary, per-scene stage contracts (ordered,
duplicate-free, one `subscribed`, prefix shared), the property that a value scene can never
claim an overflow and vice versa, that the overflow run exceeds the supervisor budget,
one-shot advancement (a replayed marker list advances nothing), and profile-derived targets
for both roles including the unknown-role refusal.

Still not proven here: the *UI* path to a real notification (that remains blocked by the
UiA2 stall), the iOS lane, and the release gate itself. This section is evidence that the
capability delivers on real hardware, not that the candidate is release-ready.

## Unverified boundary

- No real notification has been received. The nRF52840 multifunction fixture's `updates` and
  `indications` roles remain the evidence path: both scenes are wired and offline-tested, but the
  managed run itself has not been executed against a phone and a DK.
- The Workbench uses `requireNotification`/`requireIndication` from the discovered properties, so
  the platform never has to choose under `platformDefault`; the `platformDefault` branch is covered
  by the Rust tests, not by the App.
- iOS has never run this path; only the compiled graph and the macOS artifact consumer exist.
- The nRF52840 multifunction fixture and the sniffer are the next evidence step; nothing in this
  report uses either.
