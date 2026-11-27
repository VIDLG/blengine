# Formal Android UI lane: what was broken and what is left — 2026-11-13

## Scope

This report covers the managed Android **UI** notification lane
(`formal-host-android-managed-notification` → `bleng_android_scan --scene notification` →
`bleng_scan_runner` → `run_bleng_ui.exercise_notifications`). It does not re-open the RF result:
value delivery is already proven by the instrumentation lane
(`docs/testing/reports/2026-11-12-rn-notification-subscription.md`, four runs, `phone/fixture=passed`).

The lane now executes the whole scene except its final teardown: observations reach
`notify:ended` (11 of the 12-entry trace) and the run then fails in the close/evidence tail.
Each defect below was found *because* the lane got one layer further each time.

## Defects found and fixed

### 1. Discovery-order truncation hid the peer's own service

`projectGatt` kept the first `limit` characteristics in discovery order. A real nRF52840 lists
GAP, GATT and Device Information first, so the 21-characteristic topology spent the whole budget
before reaching the fixture service:

```
the discovered topology does not expose exactly one fff20000-0000-4000-8000-000000000000 row:
[] (21 characteristics, 13 not addressable from this screen)
```

The bound is now spent **per service first**: every service with characteristics is represented
before any service receives a second slot (`retained` is always a prefix per service, so a key
index implies its predecessors exist). The bound itself moved from 8 to 64: discovery is already
bounded by the backend (Apple retains at most 1024 characteristics), so the screen bound exists
for the view tree, not to describe the peer, and 8 was below a realistic GATT database.

`tests/consumers/react-native-bleng/src/gattProjection.test.ts` pins both properties;
`Gatt.test.tsx` pins the transport-first topology.

### 2. The GATT panel's height followed the peer's topology

The panel rendered four controls per discovered characteristic. At 21 characteristics that is
~2500 px of controls inside a `ScrollView` whose Android accessibility tree only contains what
is near the viewport, which pushed the notification panel — and the final `close-session`
controls — out of reach.

The panel is now a bounded **action strip**: `gatt-summary`, then the addressable index
(`gatt-rows`, one `<key> <uuid> [properties]` line per row, so the properties remain visible),
then `gatt-key` + Read/Write/Subscribe. The controls act on the named key, so the panel has the
same height for a 3-characteristic peer and a 64-characteristic peer.

### 3. The host probed for rows by position

`_find_characteristic` walked `services × limit` element ids, each miss paying a full reveal
(up to 16 swipes). It now reads the App's own index once (`_gatt_rows`), requires it to agree
with the topology summary, and fails closed when the index does not contain exactly one row for
the target UUID.

### 4. Passive fixture close raced the App's own disconnect

Every failed run recorded

```
fixture.close: RuntimeError: passive close refuses a connected peer
```

The fixture close is deliberately one-shot and refuses to run while a peer is connected, so the
host must let the central finish its own disconnect first. `bleng_scan_runner.close_fixture` now
waits (bounded by the cleanup deadline, pumping its own event queue) for `observed_peers()` and
`peer_present()` to agree, exactly as the instrumentation lane already does.

**Fourth round: that wait could never run.** The condition was `while observed_peers() or
peer_present()`, and `observed_peers()` only knows peers whose events were already drained. A
central that connected and disconnected while the scene was driving the App through another stack
leaves **both** GAP events queued and `_connected_peers` empty, so the condition was false on the
first check, the drain was skipped, and the one-shot close refused the non-empty queue:

```
fixture.close: RuntimeError: passive close refuses a GAP peer event
```

The policy existed three times (`bleng_scan_runner`, `bleng_android_native_notification`,
`bleng_android_scan`) and two copies had the same defect. It is now one method on the fixture —
`DirectFixture.settle_no_peer(deadline=...)` — that drains first and only then reads both facts,
because that is the only order in which `observed_peers()` means anything. All three close sites
call it.

Three regressions pin the policy (`test_settle_drains_a_peer_pair_the_scene_never_read`,
`test_settle_clears_a_cached_peer_whose_disconnect_was_queued`,
`test_settle_refuses_to_wait_out_a_peer_that_stays_connected`). The first one fails on the old
behaviour: an unread connect/disconnect pair makes the close refuse.

### 5. Navigation was assumed, so one lost tap cost the whole budget

`_wait_evidence_receipt` clicked `workbench-evidence` assuming the entry page and clicked back
assuming `Evidence`. When a dispatch returned but the page did not change, the loop could not
satisfy its own precondition and spun until the deadline — the 300 s "stall" that first looked
like a UiA2 hang. Navigation is idempotent and carries no BLE command, so it is the one click
that may be retried: `_WorkbenchUi.navigate(target, state)` reads the page it is actually on,
dispatches, re-reads, and retries at most three times before failing closed.

Two regressions pin it (`test_navigation_retries_a_dispatch_that_changed_no_page`,
`test_navigation_fails_closed_when_no_dispatch_takes_effect`).

### 6. Budget constants were magic and mutually inconsistent

`300` appeared as the scene bound in `run_bleng_ui`, `bleng_scan_host` and `bleng_scan_runner`,
`600` as the total bound in `bleng_android_scan`, and the evidence validator read the total.
They are now named: `run_bleng_ui.MAX_SCENE_SECONDS = 600` and
`bleng_android_scan.MAX_TOTAL_SECONDS = 1200`, with the total leaving room for the scene's whole
allowance plus install, automation startup and cleanup.

## Revealing content: which gesture, which direction, and what it costs

Android omits off-viewport `ScrollView` children from the accessibility tree, so the host has to
move the content before it can read or click anything below the fold. Three separate questions
were measured on the physical OnePlus `3ac5cc09`.

### Which gesture actually scrolls this ScrollView

`.work/gesture-probe.py` measured an element's `y` before and after each mechanism:

| mechanism | result |
|---|---|
| `mobile: swipeGesture` (area form) | HTTP 200, element `y` unchanged at 1459 — **silently no-op** |
| `mobile: dragGesture` (explicit `startX/startY/endX/endY`) | element scrolled out of the tree (404) — **works** |

A gesture that returns success and moves nothing is worse than one that fails, because it spends
the same bounded budget. Only `mobile: dragGesture` with explicit coordinates is used.

### Which direction, and why one is not enough

After `scan-samples` was read (it sits at the end of the page), the targeted control
`connect-peripheral` (near the start) failed as
`16 bounded swipes did not bring it into view`. The remembered direction pointed at a content
boundary, where every drag is a no-op that still costs budget, and the target sat the other way.

`_reveal` now sweeps **both** directions within one budget of `_MAX_REVEAL_SWIPES = 8` each,
starting with the direction the run last travelled. Content is finite, so one bounded pass per
direction either finds the node or proves it is not on this page; that proof is recorded in
`_reveal_sweep` and turns a later required read or click into an immediate failure naming the
identifier and the drag count, instead of a poll to the deadline.

The regression models a *moving window* (content coordinates, a viewport offset, off-window nodes
reported as 404) rather than a section switch, because only that makes a gesture's direction
observable: `test_a_control_above_the_window_is_reached_after_the_content_was_read_down`. With
the sweep made one-directional again it fails with the device's own message.

### Can the host stop guessing geometry at all

`.work/reach-probe/` asked UiAutomator2 to do the scrolling inside the App process, where the real
view hierarchy and its own scroll semantics live, via one query:

```
new UiScrollable(new UiSelector().resourceId("workbench-content").scrollable(true))
    .setMaxSearchSwipes(n).scrollIntoView(new UiSelector().resourceId("gatt-subscribe"))
```

It works: a control that was absent from the tree afterwards answered a plain find with
`displayed=true`. Measured cost for `maxSearchSwipes` ∈ {1, 2, 4}: **0.3 s, 6.3 s, 8.0 s** — the
budget, not the distance, drives the time because each internal fling waits for its own settle.
It also returns the scrollable container, not the child, so the caller must re-find by id anyway.

Conclusion: it is the simpler *mechanism* (no host geometry, no direction guess) but not the
cheaper one, and it hides its own retry loop inside a single opaque call. The host-side drag stays
the fallback; the probe result is recorded here so the decision is not re-litigated from memory.

## The fix that removes the gesture entirely

Every round of this lane hit the same shape of failure: a control the scene needed was inside the
scrollable content, so reaching it was a gesture, and the gesture was the unreliable part. The
answer is not a better gesture — it is a layout in which a scenario never has to make one.

The App now has two regions with different owners:

| Region | Contents | Read by |
|---|---|---|
| Pinned (outside every ScrollView) | `run-status` projection, `scan-latest` / `notify-latest`, the startup snapshot, and every command: session start/close, scan start/pull/stop, connect/disconnect/discover/RSSI, GATT target + write inputs + read/write/subscribe, notification pull/disable, scan timeout and filter inputs | a scenario, never scrolls |
| Content (`workbench-content`) | narration, topology index with full rows and descriptors, bounded sample lists, evidence trace, cancel controls | a human only |

Consequences that matter:

- The host no longer reads the topology summary or the addressable index at all. It declares the
  characteristic **UUID** in a pinned input and the App answers `gattMatch` (`none` / `invalid` /
  `ambiguous` / `one`) plus the row's platform-reported `gattProps`. A row key was an artifact of
  discovery order; identity is what the scenario contract already knows.
- The newest observation is pinned (`scan-latest`, `notify-latest`), so the pull loop reads two
  small pinned elements instead of revealing sample content. The bounded list stays on the page and
  on Evidence for a human.
- The scenario asserts its own acceptance criterion: `_require_no_gesture` fails a run that had to
  scroll, naming the count and the identifiers. A layout regression fails by name instead of
  producing a run that is merely slower and occasionally wrong.
- The reveal machinery remains for content a failure path still reads (`scan-error`, the evidence
  trace) and is covered by reader-level tests, not by a scenario.

Machine-read text is deliberately small (10–11 pt projection, compact command buttons): it is a
projection, not a headline, and every row it takes is a row of pinned space.

## Content inventory: what one page actually exposes

`GET /source` on `Current run` is 35 KB / 454 ms and lists only nodes near the viewport:
`connect-peripheral` present, `gatt-subscribe` and `scan-samples` absent. So page source is not an
alternative to revealing content, and `allowInvisibleElements` would trade the drag cost for
rebuilding the page source per lookup.

## Measured cost, and why the tail still fails

A scene is ~25 logical reads but thousands of Appium requests. Server-side time for three runs
that reached the notification value:

| run | requests | server time | `find` | `displayed` | `swipe` | `getCurrentPackage` |
|---|---|---|---|---|---|---|
| `17b88053` | 1366 | 244 s | 128 s | 17 s | 54 s | 11 s |
| `d56dfaed` | 3472 | 234 s | 128 s | 16 s | 60 s | 13 s |
| `b66109b8` | 3059 | 475 s | — | — | — | 25 s |

Two facts matter:

1. **Each element read is a request**: `find` + `displayed` + `text`. Per-request latency on this
   device ranges from 40 ms (early) to 500 ms (later), and single `displayed` calls intermittently
   stall for ~11 s inside the automation server (five such calls cost 57 s in one run). This is
   the automation server, not the App: the same reads answer in tens of milliseconds elsewhere in
   the same session.
2. **Android only exposes what is near the viewport.** Off-screen `ScrollView` children are absent
   from the accessibility tree, so every move between the GATT panel and the notification panel
   pays swipes (each ~600 ms), and the run's cost follows the number of panel switches.

The tail (Evidence → Current run → `close-session`) is where the last runs died. In
`593f2adc` the loop held `page=Current run`, `state=started`, and `close-session` never became
reachable: gestures stopped moving the viewport, and after the declared budget the driver reported
`ReadTimeoutError`. That is consistent with the documented UiA2 pathology on this device
(`$HOME/.cache/blehub/toolchains/...` is pinned; the server also logged one `socket hang up`
during session creation in `f2daf48c`).

## Atomic observations: one request, one snapshot

`_WorkbenchUi` used to observe an element as three separate calls (`find`, `displayed`, `text`).
That is both the dominant cost in a wait loop and a correctness problem: the three answers come
from three different instants, so a caller that checks two of them can act on a state that never
existed.

The observation layer is now `_Observation`: one value carrying identity plus the attributes read
with it, produced by `_observe()`. On Android that is a **single** `POST /element` with
`shouldUseCompactResponses=false` + `elementResponseAttributes=text,displayed,enabled`; the
session applies and verifies that contract at admission (`apply_compact_reads`), so a server that
silently keeps the old profile fails the run instead of tripling every read. On iOS there is no
equivalent projection, so its reading stays a bounded sequence and the value says
`atomic=False` — the difference is recorded rather than hidden.

Attributes are requested only when used (`text=`/`enabled=`), so a presence check does not pay for
text it discards.

### Measured cost

Alternating rounds, 3 rounds × 30 samples per path, physical OnePlus `3ac5cc09`, same element,
same session, warm and cold both represented (`.work/compact-response-probe/bench.py`):

| path | requests/observation | median | p90 | max |
|---|---|---|---|---|
| find + displayed + text | 3 | 90.8 ms | 122.2 ms | 142.3 ms |
| compact find | 1 | **70.4 ms** | **94.9 ms** | 105.8 ms |

**The win is ~1.3×, not 3×.** Round-trip count drops by 3× but per-call server work does not: a
compact find makes the server materialise the attributes it would otherwise serve in separate
calls, so the saving is the HTTP/JSON overhead of two round trips, not their whole cost. Reporting
this honestly matters more than the headline: the earlier table's 200–475 s scenes are dominated by
*number of observations × per-observation latency*, so this change buys roughly a quarter of the
time back, not two thirds.

What it buys unconditionally is the semantic property: a pre-dispatch recheck now compares one
snapshot (identity, displayed, enabled) instead of trusting a handle and then reading three times.

## Not done (explicitly deferred, not silently dropped)

- **Consolidate the live status into one pinned element.** The hot poll paths read `session-state`,
  `workbench-page` and one value per iteration. A single pinned machine-readable status projection
  would make a poll one read instead of three. It changes the same trace assertions the atomic
  observation change already touched, so it is the natural next step in that area.

- **`allowInvisibleElements` + XPath reads.** UiAutomator2 documents that this setting exposes
  off-screen nodes to XPath lookups and page source (not to id lookups). It would remove the swipe
  cost, at the cost of rebuilding the page source per lookup.

- **A lease-recovery recipe.** A failed run that leaves cleanup uncertainty keeps the phone lock by
  design. Releasing it needs the three conditions (owner dead, no device residue, failed bundle on
  record) plus a `lease-recovery.json`, or an `interrupted-recovery.json` when the operator stopped
  the run before any verdict existed. Today that is a manual step.

## Fixed in this round

| Defect | Why it was a defect |
|---|---|
| A click whose page/state precondition never holds polled silently to the run deadline | It then surfaced as a transport timeout that named nothing. It now fails after `_PRECONDITION_SETTLE_SECONDS` (20 s) and reports the observed state/page. |
| `mobile: swipeGesture` sent `elementId` | On this device the element-handle form was a silent no-op: a read-only `adb shell input swipe` probe revealed the target in 2 gestures while the harness saw 16 misses. The gesture now carries the container's screen area (`left/top/width/height`) and no handle can go stale. |
| `mobile: swipeGesture` with the area form | Also a silent no-op (HTTP 200, element `y` unchanged). Replaced with `mobile: dragGesture` and explicit coordinates. |
| Reveal tried directions blindly | A node that is present but clipped already reports its own rect, so the direction comes from geometry; and the sweep now reverses instead of trusting one remembered direction. |
| A target could be reported unreachable after the swipe allowance was spent, without saying so | It now fails closed with the number of bounded drags and the phase `unrevealed`. |
| Session controls lived inside the scrolled content | `close-session` could be pushed out of the Android accessibility tree by a long topology. Start/Close are now pinned with the live panel: run ownership is not content. |
| The peer-settle condition could never become true for an unread queue | Both the connect and the disconnect event stayed queued and the passive close refused them. One `settle_no_peer` on the fixture now drains before it reads. |

## Result

All three managed UI scenes pass on the physical OnePlus against the physical nRF52840 with the
same APK, **with zero injected gestures, run back to back**:

| scene | run | trace |
|---|---|---|
| `scan` | `d003e8cb` | `idle … scan:stopped` |
| `notification` | `a2ba72d8` | `idle … notify:ended` |
| `indication` | `00be16d1` | `idle … notify:ended` |

Every run: `status=passed`, `scenario_status=passed`, `cleanup_errors=[]`,
`phone_cleanup_errors=[]`, fixture close receipt `advertising_inactive=true` /
`resources_closed=true` from `btp-gap-controller-info-advertising-bit-clear`.
`host_gate_status` stays `pending` by design: a UI lane passing does not upgrade to a release
gate, which the instrumentation lane supports.

Earlier runs of the same scene injected 1 gesture twice (`evidence-scan-authorization`, then a
clipped command) and the run failed naming it — which is exactly the behaviour the assertion
exists for: the layout was corrected, not the gesture.

### Back-to-back scenes need one authorization, not a manual stop

A passing run keeps the App it launched running, because the App is also the evidence a human
inspects. The next scene then refused (`Android App/UID already running; refusing takeover or
incomplete cleanup`) — safe, but it made the natural sequence *run scan, run notification* need a
human `am force-stop` in between. `--release-retained-app` states that intent instead: the run
stops exactly the installation its own receipts prove it launched, by the same verified path a
failed run takes, and the bundle records `action=force-stop` **and**
`released_retained_app=true`. The evidence verifier accepts only those two shapes — a stop without
the flag, or the flag without a stop, fails verification, so an unexplained termination can never
ride a passing bundle. The three runs above are the sequence, with no manual step.

## Commands

```sh
pixi run just react-native::formal-host-android-build release

# One scene. Add --release-retained-app when the next scene follows.
for scene in scan notification indication; do
  pixi run just react-native::formal-host-android-managed-$scene \
    --device <serial> ... --timeout 900 --cleanup-seconds 90 --release-retained-app
done

pixi run python -m unittest tools.tests.react_native.test_bleng_ui \
  tools.tests.react_native.test_bleng_scan_ui tools.tests.react_native.test_bleng_notification_ui \
  tools.tests.react_native.test_bleng_scan_host \
  tools.tests.react_native.test_bleng_android_prepared_scan \
  tools.tests.react_native.test_bleng_android_lifecycle tools.tests.react_native.test_bleng_android_scan \
  tools.tests.react_native.test_bleng_android_candidate tools.tests.react_native.test_bleng_scan_evidence
cd tests/consumers/react-native-bleng && npx jest --runInBand && npx tsc --noEmit
```
