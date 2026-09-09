# BleHub Android test peripheral

This is a physical-device Kotlin/Android Framework GATT Server fixture for Phase 2 Windows Central development. See [`docs/testing/android-peripheral.md`](../../../docs/testing/android-peripheral.md) for setup, controls, and validation.

The Gradle project packages the complete Android Peripheral profile from [`tests/conformance/scenarios/android-peripheral.json`](../../conformance/scenarios/android-peripheral.json) as an APK asset. It contains both the ordinary `updates` characteristic and the dedicated `indication-updates` characteristic, so Notification and strict Indication are tested on one GATT database. Its roles and properties stay aligned with the QEMU profile, while its advertised service UUID remains Android-specific so concurrent fixtures cannot be confused.

Build through the repository entry point:

```sh
pixi run just peripheral android-check
```

The callback-serialized notification burst control is:

```sh
pixi run just peripheral android-notification-burst 0123456789abcdef 200
```

It requires exactly one connected Notification subscriber. One ADB intent starts the whole burst; the fixture waits for each successful Android `onNotificationSent` callback before sending the next value. Payloads may be 20–512 bytes: ASCII `BHN1`, the eight `burst_id` bytes, a zero-based `u64` little-endian sequence, and deterministic filler. The final control acknowledgement is emitted only after all callbacks succeed, or immediately when dispatch, disconnect, or fixture teardown terminates the burst. The fixture also requests LE 2M and logs actual PHY/MTU callbacks; the current OnePlus 11 evidence is MTU 517 and 2M/2M.

Strict Apple Indication uses the same fixture and service; the single Indicate command follows the characteristic whose Indication CCCD is currently enabled:

```sh
pixi run just peripheral android-indicate deadbeef
```

Windows-to-Peripheral throughput tests use the callback-side write ledger:

```sh
pixi run just peripheral android-write-batch-start 0123456789abcdef 200
pixi run just peripheral android-write-batch-status 0123456789abcdef
```

Matching 20–512-byte writes contain ASCII `BHW1`, the eight batch-ID bytes, a zero-based little-endian `u64` sequence, and deterministic filler. The status command reports exact callback accounting and the provider-local callback span; benchmark writes do not generate one UI/Logcat record per value.
