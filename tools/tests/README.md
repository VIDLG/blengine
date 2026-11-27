# Host tooling tests

`tools/tests/` is the one discovery root for tooling regressions, all hardware-free:
`react_native/` covers `tools/react_native/` (including bounded local child-process tests),
`observed/` holds regressions whose input came from a real device, and the files directly under
it cover the tools that are not RN-specific (`clean_work`, `check_peripheral_profiles`, the
sniffer, Android permission control, and the one host command boundary in `test_adb.py`).
It is not an RF gate and does not require installing mobile dependencies.

```text
pixi run just react-native tools-test
pixi run just react-native tools-fixture-test
```

A test file that lives outside `tools/tests` and is not named by a recipe never runs, so it
cannot be a contract; one such file drifted for months and asserted a `clean_work` policy row
that no longer existed. `test_recipe_wiring.py::test_every_tool_test_file_is_reachable` fails
on that shape.

Both maintained entries report each test as it runs and the ten slowest cases at completion.
`tools-test` runs pytest with `-n auto --dist loadfile`: these cases are hardware-free and
file-independent, counts are identical to a serial run (597 s → 159 s on ten cores), and one
module stays on one worker. `tools-test` accepts an optional `-k` name filter without changing
discovery roots or installing dependencies. An omitted/empty filter runs the full tooling suite;
a filtered pass is only a targeted regression, not a full `tools-test` pass.

Stable responsibility slices are also available; they use the same canonical discovery and do
not install dependencies or touch a device:

```sh
pixi run just react-native tools-test-android-safety
pixi run just react-native tools-test-lifecycle
pixi run just react-native tools-test-observed
```

These slices intentionally overlap at shared evidence/lease boundaries. Their counts must not
be added as a coverage claim. For one-off narrowing, the raw filter remains available:

```sh
pixi run just react-native tools-test '*candidate*deadline*'
```

Use canonical discovery from the repository root. Its direct equivalent for the full tooling suite is:

```sh
pixi run python -m pytest tools/tests -q -n auto --dist loadfile --durations=10
```

`tools-fixture-test` deliberately stays on `unittest discover`: it also loads the peripheral
tree, which pytest must not collect.

Save full output to an ignored log and give the invoking process a bounded deadline. A timeout
is not a test failure or a suite pass: inspect the last named case and attempt-owned processes
before choosing a targeted diagnostic. Do not infer a hang from quiet output or automatically
extend the budget.

Do not use the old `-s tools/react_native` path (it now contains implementation only), import a `test_*.py` to obtain samples, or put the `tools` directory itself on `sys.path`. Imported tests must have the single module identity `tools.tests.*`.

Some tool tests live beside their subject rather than under `tools/tests` (`tools/android/test_run_instrumentation.py` is not RN-specific). Those are reachable only because `tools-fixture-test` names them; anywhere else, move the file into `tools/tests` or name it in a recipe.

## Observed regressions

`observed/` is a separate population, not a subdirectory of `react_native/`: its input is what a
device once answered, and that is a different kind of confidence from a payload written to reach
a branch. Keeping them apart is what lets a reader see at a glance which failures deserve a
hunt for a code regression and which may just be a brittle fixture.

- **Name**: `test_regression_<where>_<symptom>`. `test_` is what any runner collects; a bare
  `regression_` name is invisible to both `unittest` and `pytest` (five tests once never ran).
- **Comment above the test**: `Trigger:`, `Symptom:`, `Provenance:`; the provenance carries the
  day and a path that can be re-read (a `.work` run, a report, a log). No prompts, tokens or
  private identifiers.
- **Fixture**: inline when short; a real payload over ~30 lines lives beside the test or under
  `react_native/support/`, and is what the device actually wrote.
- **Contract**: a failing regression means the code is probably broken. Only replace one when the
  original behaviour was genuinely wrong, and keep the provenance trail in the new test.

`observed/test_population.py` enforces the population: every test in the directory carries that
name, `regression_` appears nowhere else, each one states the three facts with a dated
provenance, and each module says what its population is. It walks module-level functions as well
as class methods, so neither shape can slip past.

Observed fixtures live under `react_native/support/` (`android_scene_device.py`,
`coredevice_fake.py`) because synthetic contract tests use the same phone stand-ins. A fake
still answers through the lane's own validator, so a shape change breaks both populations
instead of drifting quietly in one.

## Progress journals

Every producer accepts `--progress-journal <path>` (default `<output>/progress.jsonl`) and
reports through `rf_runner.ProgressReporter`. A sequence passes the same journal to each scene,
so one file shows `scene` / `finished` lines in order, including scenes a crash interrupted.
This is an observation for a human watching a long run: it is not admitted to the evidence
bundle and never participates in a verdict.

## Support ownership

- `react_native/support/`: RN-tooling-only receipts, clocks, scenario samples and scoped harnesses.
- `tests/support/rf_lab/`: marker samples actually shared with Peripheral tests.
- `tests/peripherals/nrf52840/support/`: BTP emulators, synthetic fixture receipts and provider resource mocks.

Support never imports a TestCase or another test module. Live host/provider code must not import synthetic support. Keep single-use helpers near their tests; do not create a universal `helpers.py`, duplicate the real protocol implementation, or weaken different test boundaries into a shared always-successful fixture.

Two conventions hold across both populations. A test replaces exactly one seam: `stop()` in
`host_command.py` owns the terminate→wait→kill sequence that nine modules used to spell out, and
`sha256_file` owns the one digest that twelve modules computed three different ways. And a
fixture must go through the production parser it stands in for (`ios_sdk.official_result`,
`scene_receipt.require_receipt`), never a test-only decoder.

The [test architecture note](../../docs/research/react-native/host-tooling-test-architecture.md)
records the layering, conventions C1–C7 and the guards; the
[host tooling note](../../docs/research/react-native/react-native-host-tooling-architecture.md)
records production responsibilities. Other legacy tooling tests and native gate scripts are not
implicitly included in the RN-specific commands above.
