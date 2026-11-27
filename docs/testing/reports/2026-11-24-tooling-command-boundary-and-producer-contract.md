# Host tooling: one command boundary, one producer contract

Date: 2026-11-24
Scope: `tools/host_command.py`, `tools/android/**`, `tools/react_native/android_sdk.py`,
`tools/react_native/bleng_android_candidate.py`, `tools/tests/**`

This report records an offline refactor of the host tooling after the Appium removal. It is not
a device gate, does not freeze ABI, and claims nothing about RF. Every result below is a
hardware-free test run.

## Problem

Deleting the Appium chain left two structural problems behind:

1. **Several "one place" claims, none of them true.** `adb.py` was created as the single adb
   boundary, but `run_instrumentation.has_root`, `native_hil._command`, `state_control` and
   `bleng_android_candidate._command` each still reached `subprocess` directly, and the aapt2
   calls of the Android candidate ran through the *adb* runner. A test therefore had to patch
   four different seams to keep a device command off the wire.
2. **No producer-level test for the Android managed scan.** With the Appium-era
   `test_bleng_android_scan.py` gone, wiring mistakes (the `release_retained_app` flag, a
   profile-derived target, a stage ordered before the fixture was live) could only surface on a
   real device, where they present as a hang.

## What changed

### One bounded host command boundary

`tools/host_command.py` now owns process mechanics for every project tool: a finite budget,
stderr merged into stdout, `HostCommandError` for a process that cannot start, and
`subprocess.TimeoutExpired` (with its partial output) when the budget expires.
`tools/android/adb.py` is a thin typed layer over it that owns the adb argv shape
(`-s <serial> …`), the streaming `start_command` used by instrumentation, and `AdbError`.

Consequences:

- `run_instrumentation.has_root(serial)` and `install_apk(serial, …)` no longer take a `Path`.
  The tool path is resolved once, through `adb.adb_command`. This removed two latent bugs where
  the *module* was passed where a path was expected (`android_sdk.install_apk`,
  `run_instrumentation.run`).
- `native_hil._command` routes through `adb.run_command`; `native_hil` has no subprocess site.
- `bleng_android_candidate._command` takes an explicit `runner`: `adb.run_command` for device
  commands, `host_command.run` for the aapt2 dump. The module no longer implies that aapt2 is adb.
- Android lanes seal transport failures into `AndroidSdkError` (a subclass of `AdbError`) with
  the transport error preserved as `__cause__` and its notes carried over, so the failure names
  the lane that was reading while `except adb.AdbError` still catches everything.

### Deleted Appium-era residue

`tools/android/automation_observation.py` (dumpsys `activity processes` parser) and its 16-test
suite plus fixture, and `tools/tests/react_native/support/bleng_ui.py` (imported the deleted
`tools.mobile_appium`). Nothing imported them: the in-process lanes read the instrumentation's
own receipts. Stale CLI names were dropped from the `test_layout` protected set, and the
`runStatus.ts` comment now points at `run_status.py`.

### Producer-level contract for the Android managed scan

`tools/tests/react_native/test_bleng_android_scan.py` drives `run_android_scan` end to end with
one seam per collaborator (phone lease, fixture session, fixture, permission scope, host,
driver, prepared envelope, bundle publication) and a real release-shaped APK, so
`require_release_apk` really runs. It pins:

- the fixture is leased, prepared, started and attested **before** the App is installed,
  launched, or the permission scope mutated;
- the prepared envelope receives `release_retained_app=True` (the in-process App is the
  instrumentation host and cannot outlive the run) and one scene-sized window inside the run
  budget, never an extension of it;
- subscription and io scenes pass the target their profile role derives, to the driver;
- an admitted scene reports the inner result and does **not** repeat candidate finalization,
  automation close, fixture close or phone finish;
- a pre-admission scene failure still releases every owned stage in the pinned order
  (`permissions.restore` → `automation.close` → `candidate.finalize` → `fixture.close` → lease);
- a fixture start failure never constructs the host, so nothing is installed.

## Verification (offline)

| Command | Result |
|---|---|
| `pixi run python -m pytest tools/tests -q -n auto --dist loadfile` | 879 passed, 21 skipped, 1639 subtests, 19.5 s |
| `pixi run python -m unittest tools.android.test_run_instrumentation tools.android.test_state_control -q` | 29 passed |
| `pixi run python -m unittest discover -s tests/peripherals/nrf52840 -t . -q` | 356 passed |
| `cd tests/consumers/react-native-bleng && npx jest` | 122 passed / 9 suites |
| `pixi run just lint` | format, clippy `-D warnings`, ruff, peripheral profiles |
| `pixi run just docs-check-mermaid` | 267 Markdown files |

The test count fell from 895 to 879 only because the deleted Appium observation suite left.

## Not claimed

- This report claimed no device rerun. The same day's follow-up ran all eleven Android scenes in
  one command against the refactored boundary and re-earned the pass; it also found one latent
  race. See `2026-11-24-android-scene-sequence-and-run-scoped-completion.md`.
- The iOS lane was not re-run on a device in this round; its host-tooling tests are green.
  See `2026-11-23-android-native-scene-lane.md` for the standing device facts.
