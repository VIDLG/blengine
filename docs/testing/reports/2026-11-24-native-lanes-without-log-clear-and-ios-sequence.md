# Native lanes without the log clear, and the iOS sequence

Date: 2026-11-24
Lanes: `bleng_android_{multi_link,permission_transition,native_notification,adapter_transition}.py`
through `bleng_android_instrumentation.py`; `run_bleng_ios_scenes.py`, `ios_app_cleanup.py`,
`run_ios_scene_driver.py`
Devices: OnePlus PHB110 (`3ac5cc09`, rooted); iPhone 14 Plus (`00008110-000C61160C39401E`,
iOS 27.2.1); nRF52840 DK-A (`jlink:1050252028`, v2-a) and DK-B (`jlink:1050251479`, v2-b)
Artifacts: App APK `fb914530573210670c60825f1fff6cbeed9f06233755e893857be58f18339274`, test APK
`feea61906c4cf6aa51386bda470b82cd57b0c859c019b201494688221fa8bd4a`; iOS App tree
`b67288e104d6ebcc7e22378adc581dce4bce6cae54a78921488e3bb7360267fe`, xctestrun
`4bca358da3b431f54a388398eefe4902496789e225447530355321c7234ff3c9`

This report records candidate facts. It is not a gate verdict, freezes no ABI, and claims
nothing beyond what the devices' own receipts and an offline replay state. The iOS sequence
stopped during UI runner initialization.

## Android: four native-contract lanes stopped clearing the device log

The four lanes avoided logcat's replay by running `adb logcat -c` before every capture, which
erases every app's diagnostics on the phone. The scene lane had refused to do that and paid
for a heuristic instead (see `2026-11-24-android-scene-sequence-and-run-scoped-completion.md`).
All four now create a run-scoped capture (`MarkerCapture(..., host_tag=HOST_MARKER_TAG)`), so
each round or phase begins at its own sentinel and nothing is cleared. The shared drivers
`run_instrumentation` and `run_instrumentation_until_process_exit` require the replay to have
been delivered before the test process starts. No module under `tools/react_native` or
`tools/android` issues `logcat -c` any more, and a test fails if one is reintroduced.

The device's log buffers are 256 KiB each, so the failure-time `logcat -d` dump these lanes
keep stays bounded without the clear.

### Device runs (same artifacts as the eleven-scene pass)

| Lane | Scene | Result |
|---|---|---|
| native notification | single | passed |
| native notification | sequence | passed |
| native notification | overflow | passed |
| adapter transition | adapter OFF → ON | passed, `radio=restored` |
| permission transition | denied → active → restored | passed, `hold=held`, `permission_death` recorded |
| multi-link | 2 peers (DK-A v2-a, DK-B v2-b), 1 round | passed, 24 stages |
| multi-link | 2 peers, 2 rounds | passed, each round reports exactly its own 24 stages |

Every run: `status=passed`, `host_gate_status=pending`, `cleanup_errors=[]`. Evidence root:
`.work/android-native-contract/`. DK-B's installed image was read back first
(`nrf-multifunction-rf-v2-b`, `f101a5c4b13b2c869848edd6f0bd563543ade85728cd9f3fa6ffce71430e96f5`);
nothing was flashed.

The two-round multi-link run is the case the old comment named: the previous round's markers
would have driven this round's fixtures against links that did not exist yet.

## iOS: one declared order for both platforms, and two real defects

**Shared order.** The managed scenes' dependency order is declared once
(`scene_sequence.ORDER`) and both platforms run exactly it; a scene added without a place
there fails a test instead of being forgotten on one platform. The iOS sequence was the four
fault-free scenes; it is now all eleven. The iOS App was rebuilt from the current source
(`useManualScan.ts` and `BlengSceneDriverUITests.swift` had changed since the previous build).

**Defect 1 — a clean iPhone could not start a sequence.** `release_app`, run before every
scene, required exactly one installation of the bundle. Two different situations were both
"0 installations":

- the device **has not answered**: on the first query after a new CoreDevice tunnel,
  `devicectl device info apps` returned `apps: []` with `outcome: success` inside its 8 s
  budget, while this very App was installed and running (PID 4647, confirmed by a second
  query). Reading that as "nothing is installed" would have released nothing;
- the device **answered and the App is absent**: nothing to release.

Now an empty inventory is asked again (three times, bounded) and then refused; an answered
inventory without this bundle is `not-installed`, passed, and no process inventory is taken;
several installations are still refused. On the device the fixed step then attributed the
left-over process, terminated it once and confirmed absence across two inventories.

**Defect 2 — a failed scene discarded XCTest's own words.** A managed scene runs the iOS lane
in a directory it discards, so `xcodebuild.log` was lost and the result said only that the
receipt attachment was missing. A failed result now keeps the last 16 KiB of that log
(`xcodebuild_output`), including XCTest's reason when automation-mode initialization times out.

### The iOS sequence stopped at UI runner initialization

`scan` failed after 64 s with `expected exactly one 'ios-scene-driver-receipt' attachment,
found 0`. Reproduced with the standalone driver, whose attempt directory keeps the log:

```text
Testing failed:
	BleHubBlengHostUITests-Runner (5077) encountered an error
	(The test runner failed to initialize for UI testing.
	 (Underlying Error: Timed out while enabling automation mode.))
** TEST EXECUTE FAILED **
```

The Mac reported `unattended` for its automation-mode authentication policy, developer mode
and keychain. The iPhone was connected, paired and unlocked. The log places the timeout
before the test body. The phone's **Settings › Developer › UI Automation** value at that time
is unknown. With the log retained, the diagnostic at the time read `the iPhone did not enable UI automation for XCTest (xcodebuild exit=65): iPhone Settings
> Developer > UI Automation …`.

No iOS scene is claimed. Evidence: `.work/ios-native-sequence/2026-11-24-b` (sequence),
`.work/ios-scene-driver/e42a99ac-f793-4851-bc18-d5e854187f19` (standalone reproduction).

## Verification (offline)

| Command | Result |
|---|---|
| `pixi run python -m pytest tools/tests -q -n auto --dist loadfile` | 936 passed, 21 skipped, 1723 subtests, 19.1 s |
| `pixi run just lint` | format, clippy `-D warnings`, ruff, peripheral profiles |
| `pixi run just react-native formal-host-ios-device-test-build --device 00008110-000C61160C39401E` | `TEST BUILD SUCCEEDED` |

New coverage: `test_bleng_android_instrumentation.py` (both drivers require the baseline
before launch; an unsettled replay starts no process; no native lane clears the device log),
`test_ios_app_cleanup.py` (the three answers, each with a scripted device whose receipts pass
the real `official_result` validator), `test_ios_scene_driver.py` (the device-half refusal,
the kept console tail), `test_scene_sequence.py` (one order for both platforms).

## An earlier run of this turn was not real

The first attempt to start the iOS sequence was issued inside a tool loop whose output was
not a tool result; no log file and no output root were created and no process ran. The only
trusted statements about that attempt come from files re-read afterwards. This is noted
because a report that trusted it would have described a run that never happened.

## Not claimed

- Any iOS scene on the current App tree. The eleven-scene pass of 2026-11-23 was on the
  previous tree (`abf0377a…`).
- `host_gate_status` stays `pending` everywhere: an offline replay is not the host gate.
- The iPhone's UI Automation switch has no read-only probe from this Mac; the lane can only
  name the refusal after the fact.
