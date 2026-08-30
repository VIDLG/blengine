# BleHub Android test peripheral

This is a physical-device Kotlin/Android Framework GATT Server fixture for Phase 2 Windows Central development. See [`docs/testing/android-peripheral.md`](../../../docs/testing/android-peripheral.md) for setup, controls, and validation.

The Gradle project packages the shared profile from [`tests/conformance/scenarios/basic-gatt-peripheral.json`](../../conformance/scenarios/basic-gatt-peripheral.json) as an APK asset. Do not duplicate its UUID or property definitions in this project.

Build through the repository entry point:

```sh
pixi run just peripheral-android-check
```
