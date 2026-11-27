# React Native internal consumer: physical retirement

Date: 2026-11-22
Status: executed; hardware-free suites green. This report records deletion evidence, not RF or
UI results.

## Why this was possible now

The `react-native-internal` consumer existed as the transitional workbench for scenarios that had
no product UI and no formal App lane. Every remaining scenario now has a formal owner:

| Scenario | Formal owner |
|---|---|
| adapter transition (C#16) | `formal-host-android-native-adapter-transition` (device run passed) |
| permission transition (C#17) | `formal-host-android-native-permission-transition` (device run passed) |
| permission authorization (C#5) | `formal-host-ios-permission-authorization` (two device runs passed) |
| watchdog (C#21) | retired as native-only, HIL recipes deleted |

With no scenario left without an owner, keeping a second RN package, a second App and a second
runner tree was pure duplication: it could only drift from the formal lane.

## Deleted

- `tests/consumers/react-native-internal/` (App, Jest suites, iOS/Android projects).
- `integrations/react-native-internal/` (Nitro module, packaging shells, podspecs).
- 74 Just recipes in `just/react-native/mod.just`: every `host-*` entry, `install`, `check`,
  `android-test*`, `android-build`, `apple-artifacts`, `stage-apple-production`,
  `android-artifacts`, `fault-inputs-check`, `appium-android-smoke`, plus the `integration` and
  `host` variables that pointed at the removed trees.
- 20 `tools/react_native` modules that only served those entries:
  `adapter_transition_capture`, `android_artifact_consumer`, `check_single_image`,
  `ios_artifact_consumer`, `run_android_adapter_transition`, `run_android_multi_fault_rf`,
  `run_android_multi_rf`, `run_android_pending_read_reload`,
  `run_android_permission_transition`, `run_android_rf`, `run_android_runtime`,
  `run_ios_device_runtime`, `run_ios_multi_fault_rf`, `run_ios_multi_rf`,
  `run_ios_pending_read_reload_gate`, `run_ios_radio_restore`, `run_rf_ccc_disable_reject`,
  `run_rf_ccc_enable_reject`, `run_rf_lab_full`, `run_rf_write_reject`.
- `tools/react_native/stage_apple_production.py`: its `PACKAGING_ROOT` was the deleted
  integration tree, so the entry could not have run.
- 15 test modules in `tools/tests/react_native/` that existed only for the modules above, plus
  `test_ios_artifact_consumer`, `test_run_ios_device_runtime`, `test_full_suite_ledger`,
  `test_check_runtime_log`, `test_rf_watchdog`, `test_rf_watchdog_v2` from the earlier pass.
- Seven `GeneratedRoot` entries in `tools/clean_work.py` whose only target was the deleted host.
- Five `file_scan_exclusions` entries in `.zed/settings.json`.

`tools/react_native/` went from 123 to 103 modules. Deletions were preceded by a full backup under
`.work/retired-rn-tools-backup/` (`tools/` is untracked, so removal is otherwise unrecoverable).

## Kept, and why

An import-graph audit from live roots (Just recipes, `tools/` outside `tools/react_native`,
`tests/`) reached all 103 remaining modules, so no second deletion wave was needed:

- `bleng_*` + `run_bleng_*`: the formal scan/lifecycle/multi-link/managed scene/notification/UI
  automation surface.
- `rf_*`, `capture_io`, `capture_transport`, `ios_radio_lease`, `ios_adapter_startup_*`: shared
  mechanics still used by live lanes and by the peripheral simulators.
- `run_rf_recovery_ten`, `run_ios_adapter_startup`, `run_ios_rf_scan`, `run_rf_cancellation`:
  reachable from `tests/peripherals/nrf52840/` simulators or test support.

## Root causes fixed, not just symptoms

1. **A recipe named a module that does not exist.** `tools-fixture-test` listed
   `tools.android.test_permission_control`, but the file lives in `tests/android/`. The combined
   suite therefore failed on import while the real 47-case permission-control suite never ran at
   all. Fix: the file moved to `tools/tests/test_permission_control.py`, where canonical pytest
   discovery covers it, and the stale name was dropped from the recipe.
2. **Nothing checked for tests outside a discovery root.** The existing reachability guard only
   scanned `tools/**/test_*.py`, which is exactly how case 1 hid. New guard:
   `test_no_test_file_outside_a_declared_discovery_root` requires every `tests/**/test_*.py`
   (excluding `archive/`, `consumers/`, `ffi/`) to sit under a `discover -s` root from a recipe or
   be named by dotted module or path. It immediately found `tests/hil/apple/support/test_rf_scenario.py`,
   which is legitimately invoked by path from `just/apple/mod.just`.
3. **`just/peripheral/nrf.just` named three deleted test modules.** The `nrf52840-test` entry now
   names only surviving modules.
4. **A synthetic sample raised `NameError`.** `tests/support/rf_lab/markers.py` still referenced
   `ledger.SERVICE` after the module-level import was replaced by inline constants; it now uses
   `WATCHDOG_SERVICE`.
5. **Retired-lane assertions were rewritten rather than deleted where they still had meaning.**
   `test_consumer_topology` now asserts that the only surviving `react-native-internal` mention is
   the formal autolinking leak guard; `test_apple_staging` covers the single live
   `stage_bleng_apple` lane (canonical backend Podspec, lane-owned image, preserve-output-on-failure)
   instead of two lanes.

## Validation (exact commands)

| Command | Result |
|---|---|
| `pixi run just react-native tools-test` | 2145 passed, 21 skipped, 9745 subtests (2:22) |
| `pixi run just react-native tools-fixture-test` | 2720 tests, OK (skipped=1) (9:38) |
| `pixi run just docs-check-mermaid` | 27 diagrams in 260 Markdown files |
| `pixi run just --fmt --check` | pass |
| `pixi run ruff check tools/` | pass |
| `pixi run ruff format --check tools/` | pass |

No device, RF, firmware or signing action was part of this change, so none of the above is RF or
UI evidence.

## Documented state

- `tests/consumers/README.md`: the internal row is gone; it records the deletion date and the
  autolinking guard that replaces it.
- `integrations/react-native-bleng/README.md`, `tools/react_native/README.md`: boundary text and
  entry points updated to the formal `formal-host-*` surface.
- `.plan/PLAN.md`: a dated retirement record plus corrections to the package/consumer topology,
  the source tree and the ownership bullet that still placed iOS RN ledgers in the deleted host.

## Architecture debt found and fixed after the first pass

The deletion exposed an inversion that the first pass had left in place: live modules were
importing private helpers out of the retired RF evidence bundle, which kept a dead scenario
subsystem "production-reachable". Two modules now own those concerns instead:

- `tools/react_native/evidence_io.py` — bounded read-only file/stream mechanics (`open_regular`,
  `read_small`, `hash_stream`, `recovery_json`, `walk`, `safe_name`, `require`, `EvidenceError`).
- `tools/react_native/ios_radio_control.py` — the iOS radio-control step vocabulary and its
  exact-schema audit (`CONTROL_STEPS`, `identity`, `verify_control`), which the iOS lease had been
  borrowing from a retired adapter-startup ledger.

`ios_radio_lease` and `ios_radio_restoration` now import from those two modules; the earlier
`rf_evidence_bundle` import list (ten private names) is gone, and `EvidenceError` has exactly one
definition again.

## Second pass: the RF-lab device protocol

After the host-side cut, the remaining duplication was the retired RF-lab device protocol. It was
kept "production" only by the live device provider, so it was removed in a second verified pass.

Deleted:

- `provider.py` coordination branches and `--coordination-mode` choices for
  `permission-transition`, `adapter-transition-attested`, `android/ios-pending-reload`,
  `recovery-ten-attested`, `write-reject-attested`, `ccc-enable-reject-attested`,
  `rf-lab-suite` and `rf-lab-full-attested` (no recipe ever passed them).
- The matching `direct_fixture.serve` branches; the live modes
  (`notification`, `indication`, `soak-*`, `passive-disconnect`, `host-close`, `recovery`,
  `recovery-ios`, `recovery-ten`, `system-loss-attested`, `ccc-disable-reject-attested`) are
  unchanged.
- Device simulators: `adapter_transition`, `pending_reload`, `recovery_ten`, `full_suite`,
  `write_reject`, `ccc_enable_reject`, `permission_transition`, `ios_adapter_startup`,
  `continuous_watchdog`, `suite_cancellation`, `notification_observation`, plus the
  `ccc_enable_reject` emulator and the retired emulator/sample support modules.
- 35 `tools/react_native` modules that only they and their tests reached: `rf_evidence_bundle`
  (3.7 k lines), `rf_scenario_runner`, `check_runtime_log`, the pending-read gate, the iOS
  adapter-startup candidate and its journal/ledger/evidence, and every `*_capture` / `*_ledger`
  whose only producer was a retired CLI.
- 45 test modules and 21 test-support modules; `tools/react_native` went 105 → 70 modules and
  `tools/tests/react_native` 118 → 87 test modules.

Two inversions were fixed rather than papered over:

1. `ios_radio_lease` / `ios_radio_restoration` borrowed ten private helpers from the evidence
   bundle. They now use `evidence_io`; `ios_radio_lease` also uses `ios_radio_control` instead of
   a retired adapter-startup ledger.
2. The live `ccc_disable_reject` fixture borrowed its GATT/CCC carrier from the retired
   CCC-enable-reject emulator. The carrier is now `tests/peripherals/nrf52840/support/ccc_wire.py`.

`tools/android/state_control.py` gained `verify_state_control`, the journal contract check that
`tools/android/test_state_control.py` used to import from the deleted RF-lab ledger.

Validation: `tools-fixture-test` 2175 tests OK (skipped=1); `tools-test` 1796 passed / 21 skipped /
5722 subtests; peripheral discover 356 tests OK; `ruff check`, `ruff format --check`,
`just --fmt --check`, `docs-check-mermaid` all pass.

## Notification-observation capability: removed instead of reconstructed

Two untracked modules (`tests/peripherals/nrf52840/notification_observation.py` and
`notification_observation_capture.py`) were deleted in the first cut with no backup; only their
bytecode survived. Rather than reconstruct them, the dependency was audited — and the capability
turned out to be inert:

- `ccc_disable_reject` only started a capture when a host published
  `notification-observation-request.json` in the coordination root.
- No maintained tool, recipe or test writes that request file.
- The wire codec's own module docstring marks the firmware capability as "NOT RELEASED".

So the capture could never be triggered by a maintained flow. Removed:

- the conditional hook and the `observation` import in `ccc_disable_reject.py` (the CCC-disable-reject
  gate itself is unchanged and still passes its 6 tests);
- the two bytecode-only modules and their `support/notification_observation_*` samples;
- the orphan fixture `tests/support/rf_lab/notification_observation_c_wire.json`;
- dead helpers and the golden journal sample in `support/ios_adapter_startup_samples.py`
  (`journal_document`, `session_identity`, `summary_document`, `fake_app`, `put` and the
  `ios_adapter_startup_journal.json` attachment had no remaining caller).

No `.pyc` remains in the package tree. If the firmware capability is ever released, its Python
codec must be written fresh against the Zephyr wire — there is nothing to revive.

## Residual

- `docs/research/react-native/react-native-internal-integration-review.md` and
  `rn-internal-rf-scenario-migration.md` still describe the deleted consumer and are linked from
  historical reports; archiving them needs a link-audit pass.
- Historical reports under `docs/testing/reports/` record what was true at the time and are
  intentionally left alone.
