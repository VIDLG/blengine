# Android in-process scene lane: first real-device pass

Date: 2026-11-23
Lane: `tools/react_native/run_android_scene_driver.py` + `BlengSceneDriverInstrumentedTest`
Device: OnePlus PHB110 (`3ac5cc09`, rooted), nRF52840 DK (`jlink:1050252028`, profile `multifunction-rf-v2-a`)

This report records candidate facts from the first real-device runs of the Android in-process
lane. It is not a gate verdict, does not freeze any ABI, and every claim below is either the
device's own receipt or an offline bundle verification.

## What changed (architecture)

1. **No Appium on Android.** The scene driver is an instrumented test
   (`androidx.test.uiautomator`) that reads the App's pinned `testID` accessibility surface.
   No UiAutomator2 server, no helper APKs, no HANS exemption, no HTTP hop.
2. **One plan vocabulary, two executors.** `scene_plans.py` owns the steps; `scene_native.py`
   owns phases, host actions, ledgers and all observation derivations. iOS and Android differ
   only in the executor (`scene_ios_native.py`, `scene_android_native.py`).
3. **Android runs one process per scene.** An instrumented test replaces the target package's
   process, so `am instrument` cannot attach to the App the host launched. Host actions (fixture
   emit, peer hold/reject, peer drop, re-advertise) therefore happen *in band*: the driver
   announces a `handoff` step and waits for the host's acknowledgement, while the host keeps its
   own authority guard around the action.
4. **The handshake is files, not logcat.** A long scene can outlive the host's logcat capture;
   the request (`handoff-request-<action>`) and the answer (`handoff-ack-<action>`) are files in
   the App's external files directory. Logcat markers stay diagnostics only.
5. **The receipt is the contract.** A failed scene's receipt is a prefix of the plan and is kept
   (it names the failing step and what the App showed). A passed receipt must report every step.
6. **Android evidence is install-scoped, not pid-scoped.** The instrumentation host runs inside
   the target package's own process, so `AndroidCandidateOwner` grew an explicit
   `driver_runs_in_app_process` mode: binding and cleanup prove the *installation* (uid + installed
   base APK digest) instead of the pid observed at launch, and the lane always stops the package.
7. **The debug APK now embeds its JS bundle** (`debuggableVariants = []`), so a device gate no
   longer depends on a Metro server or `adb reverse`.

## Green (real device, offline-verified bundle)

All eleven managed scenes pass on the OnePlus PHB110 against the nRF52840 DK, each with an
offline-verified bundle (`integrity_status=passed`, `scenario_status=passed`,
`fixture_status=passed`, `phone_status=passed`, `host_gate_status=pending` — an offline replay
cannot upgrade the host gate).

| scene | run | notes |
|---|---|---|
| `scan` | `90c14a3a-0b79-40fd-abfb-0d6aabb4de11` | 24 steps ~23 s; no scroll, no coordinates |
| `io` | `08c183a0-8bef-403f-a10d-f8efa3040055` | read / write / read-back / rssi |
| `notification` | `73a6fb6e-4f47-41ba-af1f-fd0ca50fb15d` | enable, value, disable, `ended` |
| `indication` | `e47bd7e0-7c55-41de-8ccc-7c56e945cdbd` | same, indication mode |
| `cancellation` | `52ae63c1-0749-474d-82a9-b5c5ebac7ac6` | open/hold/queue/settle in one process |
| `write-reject` | `9ac670a2-0045-4302-8bc0-ad66d17cbe64` | arm + reject handoffs, three phases |
| `ccc-enable-reject` | `1325ca0b-9437-4d4d-8c35-c51ba827a9f5` | refused CCC enable, retry, value |
| `ccc-disable-reject` | `d68043dc-3ccb-4f1c-8e91-cdb2a2104701` | refused CCC disable, probe, real disable |
| `passive-disconnect` | `3bd4729d-68ef-43ea-b8f0-52ded04bde7f` | one `drop` handoff |
| `recovery` | `b98d1e14-b301-4259-9309-7a7f58dc48ba` | 3 cycles, `drop,resume,drop,resume,drop` |
| `recovery-ten` | `74027e04-2cbd-4c98-bf5e-8076b95aa4dc` | 10 cycles, 295 steps, 19 handoffs, 116 s |

## Red, with root causes (all four are now fixed)

Each of these was found by the lane, not hidden by it: the bundle names the plan step that
stopped and carries the App's own record of what it did.

1. **`notification`, `indication`, `ccc-enable-reject`: a settled disable never published the
   stream's terminal.** After `disable-notifications` succeeded the App kept `notify` at
   `subscribed` and the pull control disabled for the whole window. `canPullNotification` is
   false only for `notifyPending` or `notifyTerminal`, and the only writer of `notifyTerminal`
   was a pull — so the terminal could only arrive from a pull that the same state made
   impossible. → **Product fix** in `tests/consumers/react-native-bleng/src/useManualScan.ts`:
   a successful disable publishes `notifyTerminal` + `notifyPhase: ended` itself; a failed
   disable still reports `unverified`. One defect, three scenes, and it was never reachable from
   the old Appium lane because that lane drove notifications from host code.
2. **`ccc-disable-reject`: the phase slicer assumed every non-first phase has a handoff row.**
   `_merged_steps` emits a `handoff` step only for a phase that declares a host action, while
   `_split_phases` advanced the cursor for every phase after the first. `ccc-disable-reject` is
   the only scene with a host-less *middle* phase (`subscribe`), so its one-process receipt was
   always one row short of the plan and the envelope failed with "the merged scene receipt is
   shorter than the plan" — an arithmetic mismatch, not a device fact. → **Host fix**: the row
   → phase map is now built once (`_merged_origin`) by the same function that emits the plan, so
   the two can no longer disagree; a failed receipt is sliced as far as it goes, and only a
   receipt that claims to have *passed* must be whole. The run document is also published whole
   (`scene-<name>.json`) instead of being the first phase's slice.
3. **`ccc-disable-reject`: the plan poked a control that a successful disable withdraws.** The
   probe ended with `tapUntil pull-notification until notify=ended`, which asks a closed lane for
   a value; measured on the device, the App had already published `ended`
   (`15|notify-disable-wait|succeeded`) and stepped 55/56 recorded real failures from the retry.
   → **Plan fix**: one tap, a declared settle window (`still`, 8 s, inside the App's own 5 s
   observation budget) and two reads; the derivation — not a step timeout — decides what
   settled. Same shape as the earlier `_pull_value` fix.
4. **`recovery`, `recovery-ten`: the host answered a repeated handoff only once.**
   `_drive_scene` deduplicated handoffs by action *name* for the whole run, so a repeating
   scene's second `drop` (and every `resume` after the first) was never acted on, and the
   driver sat in its 180 s handoff timeout. The file protocol was already per-occurrence (the
   driver deletes both files), so → **Host fix**: the request file *is* the occurrence; the host
   acts, acknowledges, and waits for the driver to consume it before considering that action
   again. Two further fixture changes came with it: `drop_peer` takes the peer address from the
   tester's own GAP connect event when the fault engine holds no live ATT channel, and its
   confirmation is now bounded (15 s) and names the observed state instead of waiting forever.

## Driver facts learned on the device

* `am instrument` force-stops the target package: the App the host launched does not survive into
  the scene, so the plan's `activate` step is what starts it (inside the instrumentation process).
* `UiObject2.setText` dispatches `ACTION_SET_TEXT`; tapping a field first raises the keyboard,
  which reflows the App's own `ScrollView` and can move a pinned field out of the Android
  accessibility tree mid-step. Fields are written without a tap and focused only if refused.
* A React control renders `disabled` while its own submission is in flight, so `tap` waits for
  `isEnabled` instead of sampling it once.
* `UiObject2` property reads can throw `StaleObjectException` between resolve and read; that is a
  re-render, not a failure, and is re-observed on the next poll.
* `exists: false` needs a boolean predicate: a node object cannot stand in for "matched", because
  a matched absence has no node.
* Logcat replays its buffer at subscription time, so a marker baseline must settle before the run
  or the previous phase's `result:` looks like the current one's.
* An interrupted run leaves both a phone-lease lock and a running App, and the next run refuses
  both on purpose. `tools/android/release_stale_lease.py` is the audited way to release a lease
  whose owning host pid is provably gone.
* A scene that repeats an action (`drop` per cycle) must be answered per occurrence. The request
  file is the occurrence; a host that remembers action names silently skips the second one and
  the failure surfaces 180 s later as a driver-side handoff timeout, not as a host error.
* `recovery-ten` declares a 1500 s scenario budget. A shorter `--timeout` is refused by the
  runner before any device action, so a suite loop must give it its own budget.

## The Appium chain is gone

Once all eleven scenes passed on both platforms, the Appium chain was deleted rather than kept as
a fallback diagnostic: the driver and its owners, the helper/APK preparation, the UiAutomator2 and
WDA toolchains, the two Appium-only lanes (`run_bleng_ui`, `run_bleng_android_lifecycle`), the
3000-line Appium bundle verifier, eight recipes and the `pixi` Appium client.

Three things had to be *moved* rather than deleted, because live code still needed them:

* the generic evidence mechanics (`evidence_io`) — deadline, file identity, stable reads, strict
  JSON, bounded inventory and exclusive freeze — now live in their declared owner, and the
  release-shaped APK inventory moved to `bleng_scan_evidence.require_release_apk`, so a frozen
  candidate is still inspected before any install;
* `bleng_scan_host` held only profile derivation once the scene functions went, so it is now
  `scene_targets`;
* the host-switch probes that shared a file with WDA (automation-mode marker, DevToolsSecurity,
  keychain) moved to their only consumer, `xctest_unattended`.

Two real defects surfaced while rewiring, both caught by running the lane rather than by tests:

1. **A failed post-bind identity check stopped poisoning the candidate.** Both in-process bindings
   re-prove the App between actions; when that proof failed they raised but left the candidate
   usable, so cleanup could stop a process the run could no longer prove was its own. Both now
   record the failure, and cleanup refuses (iOS additionally proves its own liveness first).
2. **The Android producer did not pass `release_retained_app=True` to the envelope.** A successful
   device run then failed in cleanup with "the in-process lane cannot retain an App across its own
   test process". This is exactly the wiring an Appium-era mock harness used to cover, so it is
   worth stating plainly: the real-device run is what caught it.

Re-proved on the device through the new CLI after the deletion: `scan` (`dcd1698b`) and
`passive-disconnect` (`7f0f114a`, one `drop` handoff) both `status=passed`, `host_gate=pending`.

## Verification commands

```sh
pixi run ruff check tools && pixi run ruff format --check tools
pixi run python -m pytest tools/tests/react_native -q
cd tests/consumers/react-native-bleng && npx jest --silent
pixi run python -m tools.react_native.bleng_scan_evidence .work/android-native-scenes/<run>
```

One scene per invocation (add `--release-retained-app` when running a sequence, so the next
scene starts from a device with nothing of ours running):

```sh
pixi run python -m tools.react_native.run_bleng_android_scan --scene recovery-ten \
  --device 3ac5cc09 --app-id com.blehubblenghost --activity com.blehubblenghost.MainActivity \
  --apk <app-debug.apk> --apk-sha256 <sha> --test-apk <app-debug-androidTest.apk> \
  --test-apk-sha256 <sha> \
  --fixture-contract tests/peripherals/nrf52840/firmware/contracts/multifunction-rf-v2-a.json \
  --fixture-profile tests/peripherals/nrf52840/firmware/profiles/multifunction-rf-v2-a.json \
  --fixture-port /dev/cu.usbmodem0010502520281 --fixture-resource-identity jlink:1050252028 \
  --output .work/android-native-scenes --timeout 1800 \
  --authorize-install --authorize-fixture-start --authorize-bluetooth-permissions --use-root
```
