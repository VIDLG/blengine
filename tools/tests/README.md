# Host tooling tests

`react_native/` contains hardware-free regressions for `tools/react_native/`, including bounded local child-process tests. It is not an RF gate and does not require installing mobile dependencies.

```text
pixi run just react-native tools-test
pixi run just react-native tools-fixture-test
```

Use canonical discovery from the repository root:

```text
pixi run python -m unittest discover -s tools/tests/react_native -t . -p 'test_*.py' -q
```

Do not use the old `-s tools/react_native` path (it now contains implementation only), import a `test_*.py` to obtain samples, or put the `tools` directory itself on `sys.path`. Imported tests must have the single module identity `tools.tests.react_native.*`.

## Support ownership

- `react_native/support/`: RN-tooling-only receipts, clocks, scenario samples and scoped harnesses.
- `tests/support/rf_lab/`: marker samples actually shared with Peripheral tests.
- `tests/peripherals/nrf52840/support/`: BTP emulators, synthetic fixture receipts and provider resource mocks.

Support never imports a TestCase or another test module. Live host/provider code must not import synthetic support. Keep single-use helpers near their tests; do not create a universal `helpers.py`, duplicate the real protocol implementation, or weaken different test boundaries into a shared always-successful fixture.

The [architecture note](../../docs/research/react-native/react-native-host-tooling-architecture.md) records completed changes, preserved coverage and the remaining staged work. Other legacy tooling tests and native gate scripts are not implicitly included in the RN-specific commands above.
