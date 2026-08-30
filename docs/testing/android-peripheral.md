# Android managed development peripheral

This page describes the project-managed Kotlin BLE Peripheral under [`tests/peripherals/android/`](../../tests/peripherals/android/). It runs on a physical Android device and provides the Phase 2 day-to-day RF fixture for the Windows Central backend. It is test infrastructure, not an Android Central backend and not a BleHub product capability.

The app is deliberately small:

- Kotlin and Android Framework APIs only;
- no Rust, UniFFI, AndroidX, Compose, foreground service, or production AAR dependency;
- GATT shape loaded directly from [`basic-gatt-peripheral.json`](../../tests/conformance/scenarios/basic-gatt-peripheral.json);
- foreground Activity with the screen kept awake;
- explicit notification, indication, disconnect, and status controls;
- Android CLI for deployment and ADB intents for deterministic control.

It does not replace the QEMU/BlueZ/BLESS fault-injection and soak gate.

## Prerequisites

Use the project-managed toolchain and a physical Android device:

```sh
pixi run just android-doctor
pixi run just android-test-device physical
```

The fixture requires a device that supports BLE advertising. Android 12 and newer require the Nearby devices runtime permissions `BLUETOOTH_ADVERTISE` and `BLUETOOTH_CONNECT`. The app does not scan and therefore does not request `BLUETOOTH_SCAN` or location permission.

The checked-in build uses compile/target SDK 36, build tools 36.0.0, AGP 8.10.1, Kotlin 2.1.20, JDK 17, and the repository's Aliyun Maven mirrors. Gradle and JDK are supplied by Pixi. The Android CLI uses the SDK root resolved by `tools.android_toolchain`, not an implicit user SDK.

## Build and start

```sh
pixi run just peripheral-android-check
pixi run just peripheral-android-start
```

`peripheral-android-start` builds the debug APK and uses the Android CLI to deploy and launch it on the selected physical device with runtime permissions granted. It exits successfully only after the Activity acknowledges that advertising reached `ADVERTISING`; the host retries the transient asynchronous `STARTING` state for up to five seconds. The UI starts advertising automatically and exposes Start, Stop, Notify, Indicate, Disconnect, and Status buttons.

The advertised service is:

```text
FFF00000-0000-4000-8000-000000000000
```

The 128-bit service UUID consumes most legacy advertisement space, so the fixture does not require or advertise the configured local name. Discovery and preflight use the service UUID.

## Host controls

With the Activity running:

```sh
pixi run just peripheral-android-status
pixi run just peripheral-android-notify deadbeef
pixi run just peripheral-android-indicate deadbeef
pixi run just peripheral-android-disconnect
pixi run just peripheral-android-stop
pixi run just peripheral-android-log
```

Every post-launch host command carries a unique control ID and waits up to five seconds for an Activity `ok/error` acknowledgement. A delivered Android intent is therefore not treated as a successful BLE action. For example, Notify without a matching subscriber exits nonzero with a structured log message.

The Notify and Indicate commands update `FFF20000-0000-4000-8000-000000000000` and send only to connected devices whose CCCD currently selects the requested mode. Dispatch acceptance is logged immediately; Android's later `onNotificationSent` status is logged separately. Each connected device permits only one in-flight notification or indication until that completion callback arrives, preventing a later command from overwriting or overtaking the previous send.

CCCD values are interpreted as little-endian bytes:

```text
00 00 = disabled
01 00 = notification
02 00 = indication
```

## Windows/Bleak smoke

Start the Android fixture first. In a second terminal, run:

```sh
pixi run just ble-fixture-check --notification-timeout 60
```

Wait until the Central prints:

```text
notification_subscription=ready
```

Then trigger the matching update from another terminal:

```sh
pixi run just peripheral-android-notify deadbeef
```

The expected evidence is:

1. the Android log records the CCCD write;
2. the dispatch call reports accepted;
3. `onNotificationSent` reports the platform completion status;
4. the Windows Central receives exactly `DE AD BE EF` and reports `notification_delivery=passed`.

This is a real-RF test. Before running it, identify the DUT, confirm the Android fixture is advertising, and reserve the physical devices/controller. A missing or misconfigured fixture is infrastructure unavailable, not a BleHub backend failure.

## Limits

- The Activity owns the GATT server; closing it stops advertising and releases the server.
- The first version stays foreground and keeps the screen awake. Add a foreground service only if a later soak requirement proves it necessary.
- It does not yet provide delayed responses, malformed ATT behavior, callback reordering, scripted failures, or long-running recovery. Those remain BLESS responsibilities.
- One phone cannot simultaneously be the tested Android Central and this Peripheral. Android Central HIL needs BLESS, nRF52840, or a second phone.
