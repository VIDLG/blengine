# Android scene sequence: run-scoped completion, and eleven scenes in one command

Date: 2026-11-24
Lane: `tools/react_native/run_bleng_android_scenes.py` → `run_bleng_android_scan` →
`run_android_scene_driver.py` + `BlengSceneDriverInstrumentedTest`
Device: OnePlus PHB110 (`3ac5cc09`, rooted), nRF52840 DK (`jlink:1050252028`, profile
`multifunction-rf-v2-a`)
App APK: `fb914530573210670c60825f1fff6cbeed9f06233755e893857be58f18339274` (unchanged from the
2026-11-23 pass)
Test APK: `feea61906c4cf6aa51386bda470b82cd57b0c859c019b201494688221fa8bd4a` (rebuilt; the driver
changed — see below)

This report records candidate facts from two real-device sequence runs. It is not a gate verdict,
does not freeze any ABI, and claims nothing beyond what the device's own receipts and an offline
bundle replay state.

## What the first sequence found

Running the eleven managed scenes as one sequence (`.work/android-native-sequence/2026-11-24-a`)
passed `scan`, `notification`, `indication`, `io` and `cancellation`, then failed `write-reject`
and stopped, as a sequence must:

```text
phase=scene status=failed phase_elapsed=4s error=JSONDecodeError:Expecting value: line 1 column 1 (char 0)
```

The failed attempt's own record (`driver/scene-write-reject.json`) shows the mechanism:

| Fact | Value |
|---|---|
| `instrumentation_returncode` | `-15` (the host stopped it) |
| `instrumentation_seconds` | `3.197` |
| `instrumentation_lingered` | `true` |
| `instrumentation_output` | only the test-class header line |
| markers after the host's baseline | `step:59:wait`, `step:60:read`, `step:61:read`, `receipt:…`, `hierarchy:…`, `result:passed`, `scene:write-reject` |
| `handoffs` | `[]` |

Those first six markers are the **tail of the previous scene** (`cancellation` ends with
`step:61:read`). For the five passing scenes the markers after the baseline each begin with the
scene's own `scene:<name>`; for `write-reject` the baseline had been taken in the middle of
logcat's replay.

Root cause, in two layers:

1. **A heuristic stood in for a run boundary.** `_settled_stage_count` declared logcat's replay
   finished when two counts 0.1 s apart were equal. The replay arrives in bursts, so a pause
   inside it was mistaken for its end, and the previous scene's `result:passed` was then read as
   this scene's completion signal.
2. **A marker was a result source.** The lane's own documentation says the receipt is the
   contract and markers are diagnostics, yet `_drive_scene` ended the scene on the `result:`
   marker. A logcat line cannot say which run logged it.

The misleading error text has its own small cause, confirmed on the device: `adb exec-out cat
<missing>` answers **exit 0** with the shell's complaint on stdout
(`cat: …/scene-receipt.json: No such file or directory`), so the lane decoded a sentence as JSON.

This is a latent race in the lane, not a regression of the same day's adb-boundary refactor: the
five scenes before it and the earlier eleven-scene pass used the same heuristic and happened not
to hit the pause.

## What changed

**Completion is a device file.** The driver's last act, on a failed step too, is to publish
`scene-done` (passed as the `-e done` argument, like `receipt` and `hierarchy`). The receipt, the
hierarchy and the done file are each written under a temporary name and renamed into place, so a
file that exists is this run's and is complete. The host stops the instrumentation only once the
done file appears in the driver's directory — the same listing it already polls for handoff
requests.

**Stale files are removed and proved gone.** Before a run, one command removes the run files and
every handoff exchange, and the listing is read back; a file that survives is a failure before
anything is driven. A leftover done file would be obeyed at once as this run's completion.

**Markers are scoped to the run.** `MarkerCapture` accepts a `host_tag`; with one it writes a
unique sentinel from the host (`adb shell log -t BLEHUB_RN_HOST run-<uuid>`) before returning, and
`stages()` reports only what the stream delivered after that line. The settle heuristic is gone,
and the lane still does not clear the device's log: `logcat -c` destroys diagnostics the lane does
not own.

**A failed run explains itself.**

- A missing or unreadable receipt is named (`the scene driver published no readable
  scene-receipt.json: …`) instead of surfacing as `JSONDecodeError`.
- A non-passing instrumentation exit no longer erases the returncode, markers and answered
  handoffs: they are recorded before the exit is judged.
- When the receipt reports a failed step, that text is the primary error and the executor's
  verdict is a note on it.
- A cleanup failure that arises after a passing scene marks the published result `failed`.

**One sequence engine for both platforms.** `scene_sequence.py` owns the argument contract
(the sequence owns `--output` and rejects `--scene`), order validation, exclusive per-scene roots,
stop at the first failure, scene budgets and the summary line. A repeating scene declares the
budget it needs (`SceneEvidence.min_seconds`); the sequence raises that scene's `--timeout` to its
floor and leaves every other scene's exactly as the caller stated it. `run_bleng_ios_scenes.py` is
now a thin binding of it (release the retained App before every scene); `run_bleng_android_scenes.py`
binds it to the Android lane with all eleven scenes in dependency order and no pre-admission
action, because that producer refuses to take over a running App, replaces the installation
itself and always stops the App it launched.

```sh
pixi run just react-native formal-host-android-managed-sequence \
  --output .work/android-native-sequence/<run> \
  --device <serial> --app-id com.blehubblenghost \
  --activity com.blehubblenghost.MainActivity \
  --apk <app-debug.apk> --apk-sha256 <sha> \
  --test-apk <app-debug-androidTest.apk> --test-apk-sha256 <sha> \
  --fixture-contract <contract.json> --fixture-profile <profile.json> \
  --fixture-port <port> --fixture-resource-identity jlink:<serial> \
  --authorize-install --authorize-fixture-start \
  --authorize-bluetooth-permissions --use-root
```

## Device pass (second sequence, final driver)

`.work/android-native-sequence/2026-11-24-b`, log `.work/android-native-sequence-2026-11-24-b.log`,
`scene_sequence=passed`. Every row was re-verified offline with
`python -m tools.react_native.bleng_scan_evidence <attempt>` (`integrity_status=passed`, exit 0).

| Scene | Scene time | Markers | Handoffs | Exit | Status |
|---|---:|---:|---:|---|---|
| scan | 26.9 s | 29 | 0 | 0 | passed |
| notification | 31.8 s | 49 | 1 | 0 | passed |
| indication | 31.7 s | 49 | 1 | 0 | passed |
| io | 33.6 s | 50 | 0 | 0 | passed |
| cancellation | 38.6 s | 70 | 3 | 0 | passed |
| write-reject | 39.0 s | 67 | 2 | 0 | passed |
| ccc-enable-reject | 38.4 s | 64 | 3 | 0 | passed |
| ccc-disable-reject | 51.8 s | 69 | 3 | 0 | passed |
| passive-disconnect | 37.2 s | 57 | 1 | 0 | passed |
| recovery | 50.6 s | 109 | 5 | 0 | passed |
| recovery-ten | 118.2 s | 200 (bounded) | 19 | 0 | passed |

For every scene: `status=passed`, `scenario_status=passed`, `session_status=passed`,
`phone_status=passed`, `fixture_status=passed`, `host_gate_status=pending`. No instrumentation had
to be stopped (`instrumentation_lingered=false`); each one exited by itself with `OK (1 test)`
inside the grace window. Each scene's first reported marker is its own `scene:<name>`. Scene time
totals 497.9 s.

After that run the host tree changed once more in ways the scene lane does not execute (the host
tag moved to the shared instrumentation module; the shared drivers gained a baseline call), so one
scene was re-run on the final tree: `passive-disconnect`, passed, `host_gate=pending`.

## Verification (offline)

| Command | Result |
|---|---|
| `pixi run python -m pytest tools/tests -q -n auto --dist loadfile` | 920 passed, 21 skipped, 1651 subtests, 19.2 s |
| `pixi run just lint` | format, clippy `-D warnings`, ruff, peripheral profiles |
| `pixi run just react-native formal-host-android-build debug` | app + androidTest APKs built |

New hardware-free coverage:

- `test_android_scene_driver.py` (10 tests) — the lane's device protocol against one fake
  device: the replayed-marker regression, the done file as the only completion signal, a resident
  runner, a failed step's receipt, a stale done file that survives removal, a missing receipt,
  a handoff exchange, cleanup.
- `test_bleng_android_instrumentation.py` — `MarkerBaselineTests`: history before the sentinel is
  never reported, nothing is reported until the sentinel arrives, only the exact sentinel bounds
  the stream, a long replay cannot starve the run's marker budget.
- `test_scene_sequence.py` (24 tests) — the engine and the Android binding.

Before this round the Android driver's host logic had no offline test at all.

## Not claimed

- **The four native-contract lanes** cleared the device log with `adb logcat -c` when this
  report was written. They were migrated to the sentinel the same day and re-run on the
  device; see `2026-11-24-native-lanes-without-log-clear-and-ios-sequence.md`.
- **iOS.** No iOS scene is claimed by this report; the follow-up above records the iOS state.
- `host_gate_status` stays `pending` for every bundle: an offline replay is not the host gate.
