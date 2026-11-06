# Android managed development peripheral

This page describes the project-managed Kotlin BLE Peripheral under [`tests/peripherals/archive/android/`](../../tests/peripherals/archive/android/). It runs on a physical Android device and provides the Phase 2 day-to-day RF fixture for the Windows Central backend. It is test infrastructure, not an Android Central backend and not a BleHub product capability.

The app is deliberately small:

- Kotlin and Android Framework APIs only;
- no Rust, UniFFI, AndroidX, Compose, foreground service, or production AAR dependency;
- GATT shape loaded directly from [`android-peripheral.json`](../../tests/conformance/scenarios/archive/android/android-peripheral.json), including ordinary `updates` and `indication-updates` characteristics;
- foreground Activity with the screen kept awake;
- explicit notification, indication, disconnect, and status controls;
- Android CLI for deployment and ADB intents for deterministic control.

It does not replace the QEMU/BlueZ/BLESS fault-injection and soak gate.

## Prerequisites

Use the project-managed toolchain and a physical Android device:

```sh
pixi run just android-doctor
pixi run just test android-device physical
```

The fixture requires a device that supports BLE advertising. Android 12 and newer require the Nearby devices runtime permissions `BLUETOOTH_ADVERTISE` and `BLUETOOTH_CONNECT`. The app does not scan and therefore does not request `BLUETOOTH_SCAN` or location permission.

The checked-in build uses compile/target SDK 36, build tools 36.0.0, AGP 8.10.1, Kotlin 2.1.20, JDK 17, and the repository's Aliyun Maven mirrors. Gradle and JDK are supplied by Pixi. The Android CLI uses the SDK root resolved by `tools.android_toolchain`, not an implicit user SDK.

## Build and start

```sh
pixi run just peripheral android-check
pixi run just peripheral android-start
```

`peripheral android-start` builds the debug APK and uses the Android CLI to deploy and launch it on the selected physical device with runtime permissions granted. It exits successfully only after the Activity acknowledges that advertising reached `ADVERTISING`; the host retries the transient asynchronous `STARTING` state for up to five seconds. The UI starts advertising automatically and exposes Start, Stop, Notify, Indicate, Disconnect, and Status buttons.

The Android-specific advertised service is:

```text
FFF00000-0000-4000-8000-000000000000
```

The 128-bit service UUID consumes most legacy advertisement space, so the fixture does not require or advertise the configured local name. Discovery and preflight use the service UUID. The QEMU/BLESS fixture uses the distinct service UUID `FFE00000-0000-4000-8000-000000000000`; both fixtures may therefore advertise concurrently without Central tests selecting the wrong provider.

## Host controls

With the Activity running:

```sh
pixi run just peripheral android-status
pixi run just peripheral android-notify deadbeef
pixi run just peripheral android-indicate deadbeef
pixi run just peripheral android-notification-burst 0123456789abcdef 200
pixi run just peripheral android-write-batch-start 0123456789abcdef 200
pixi run just peripheral android-write-batch-status 0123456789abcdef
pixi run just peripheral android-disconnect
pixi run just peripheral android-radio-disable
pixi run just peripheral android-radio-enable
pixi run just peripheral android-stop
pixi run just peripheral android-log
```

Every post-launch fixture command carries a unique control ID and waits up to five seconds for an Activity `ok/error` acknowledgement. A delivered Android intent is therefore not treated as a successful BLE action. For example, Notify without a matching subscriber exits nonzero with a structured log message.

`android-disconnect` calls the fixture application's `BluetoothGattServer.cancelConnection()` and only proves that this app-owned GATT server observed cancellation; a phone may still expose other system GATT services, so this command is not a deterministic whole-device disconnect. `android-radio-disable`/`android-radio-enable` instead use ADB Bluetooth-manager control and wait for `STATE_OFF`/`STATE_ON`; they are destructive device-level fault controls. The passive-disconnect runner restores the radio and restarts the fixture automatically, but standalone use requires the operator to re-enable the radio and restart advertising.

The Notify command updates `FFF20000-0000-4000-8000-000000000000`. The single Indicate command follows the characteristic whose Indication CCCD is currently enabled: the ordinary Windows path uses `FFF20000...`, while the Apple strict-Indication gate uses the indication-only `FFF50000-0000-4000-8000-000000000000`. Each command sends only to connected devices whose CCCD currently selects the requested mode. Dispatch acceptance is logged immediately; Android's later `onNotificationSent` status is logged separately. Each connected device permits only one in-flight notification or indication until that completion callback arrives, preventing a later command from overwriting or overtaking the previous send. On connection the fixture requests LE 2M in both directions and records `onPhyRead`, `onPhyUpdate`, and `onMtuChanged`; the OnePlus 11 throughput run confirmed MTU 517 and 2M/2M.

`android-notification-burst` requires exactly one Notification subscriber and emits a 20–512-byte sequence at a time: ASCII `BHN1`, the supplied 8-byte burst ID, a little-endian `u64` sequence, and deterministic filler when the payload is larger than 20 bytes. The fixture waits for each successful `onNotificationSent` before dispatching the next value, does not log every payload, and acknowledges the host command only after the full burst completes or fails. Windows throughput HIL independently verifies the burst ID, every sequence, exact length, ordering, duplicates, and missing values. The current OnePlus 11 sweet spot is approximately 244 bytes, reaching about 98 KiB/s (~0.8 Mbit/s) losslessly.

The write-throughput ledger is fixture instrumentation, not a product protocol. `android-write-batch-start` resets one expected batch; matching 20–512-byte Central writes use ASCII `BHW1`, the 8-byte batch ID, a zero-based little-endian `u64` sequence, and deterministic filler. `android-write-batch-status` reports received/unique/missing/duplicate/order/range/foreign/malformed counters and the provider-local first-to-last callback span. Benchmark writes update only this in-memory ledger and do not emit one Logcat/UI record per payload.

CCCD values are interpreted as little-endian bytes:

```text
00 00 = disabled
01 00 = notification
02 00 = indication
```

## Windows/Bleak smoke

Start the Android fixture first. In a second terminal, run:

```sh
pixi run just test fixture --notification-timeout 60
```

Wait until the Central prints:

```text
notification_subscription=ready
```

Then trigger the matching update from another terminal:

```sh
pixi run just peripheral android-notify deadbeef
```

The expected evidence is:

1. the Android log records the CCCD write;
2. the dispatch call reports accepted;
3. `onNotificationSent` reports the platform completion status;
4. the Windows Central receives exactly `DE AD BE EF` and reports `notification_delivery=passed`.

This is a real-RF test. Before running it, identify the DUT, confirm the Android fixture is advertising, and reserve the physical devices/controller. A missing or misconfigured fixture is infrastructure unavailable, not a BleHub backend failure.

## Historical Windows backend notification smoke

The archived `WindowsBackend` path historically used the Android-specific service UUID so it remained isolated while the QEMU fixture advertised its `FFE0...` profile. This is historical compatibility evidence, not an active gate:

```sh
pixi run just peripheral android-start
pixi run just windows notification-smoke 30 fff00000-0000-4000-8000-000000000000 default
pixi run just windows notification-smoke 30 fff00000-0000-4000-8000-000000000000 indication
pixi run just peripheral android-stop
```

The 2026-08-31 Notification and strict Indication runs passed enable, exact `DE AD BE EF` value delivery, disable, and a caller-owned post-disable observation window with no stale routed value. See the [notification/indication RF report](reports/2026-08-31-windows-android-notification-smoke.md) for the environment, event sequence, and limits. The report does not separately assert Android `onNotificationSent`; fixture control confirms dispatch acceptance, while Central receipt proves delivery.

The 2026-09-01 current candidate also proved one device-level passive disconnect by disabling the Android Bluetooth radio, waiting for `STATE_OFF`, observing Windows `command_id=None` `Disconnected`, restoring `STATE_ON`, and restarting the fixture. The same report records why app-level GATT-server cancellation is not equivalent: [`2026-09-01 current-candidate Android smoke`](reports/2026-09-01-windows-android-current-candidate-smoke.md).

## Limits

- The Activity owns the GATT server; closing it stops advertising and releases the server.
- The first version stays foreground and keeps the screen awake. Add a foreground service only if a later soak requirement proves it necessary.
- It does not yet provide delayed responses, malformed ATT behavior, callback reordering, scripted failures, or long-running recovery. Those remain BLESS responsibilities.
- One phone cannot simultaneously be the tested Android Central and this Peripheral. Android Central HIL needs BLESS, nRF52840, or a second phone.
