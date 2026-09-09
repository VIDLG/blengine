# Apple Notification/Indication deterministic slice

Date: 2026-09-03

## Scope

This report records implementation, deterministic tests, generic iOS build validation, focused review, and the macOS-to-Android `PlatformDefault` Notification RF pass for the current unfrozen Swift/CoreBluetooth Apple production candidate. It does not record an Indication RF pass, an iPhone runtime/RF pass, lifecycle/soak evidence, or a release-artifact pass.

## CoreBluetooth mode policy

The backend maps the unified request to the properties CoreBluetooth exposes on the exact committed `CBCharacteristic` instance:

| Characteristic properties | Request | Result |
|---|---|---|
| notify only | `PlatformDefault` or `RequireNotification` | `Notification` |
| indicate only | `PlatformDefault` or `RequireIndication` | `Indication` |
| notify + indicate | `PlatformDefault` or `RequireNotification` | `Notification` |
| notify + indicate | `RequireIndication` | accepted, then terminal `NotSupported` |
| neither | `PlatformDefault` | synchronous `InvalidRequest` |
| required property absent | strict request | accepted, then terminal `NotSupported` |

CoreBluetooth selects Notification when both notify and indicate are present. BleHub therefore does not claim strict Indication on a dual-property characteristic and does not silently substitute Notification for `RequireIndication`.

The Apple capabilities now report:

```text
supportsRequiredNotification=true
supportsRequiredIndication=true
reportsActualNotificationMode=true
```

These capability flags mean the backend can strictly validate and report the selected mode; they do not mean every characteristic supports both modes.

## Serialized operation and callback model

Notification enable and disable share the existing per-connection GATT FIFO with characteristic reads, writes, and internal rediscovery teardown. The backend reserves the complete private route before native enable, including:

- connection generation and exact peripheral object identity;
- GATT session;
- public subscription and characteristic IDs;
- private subscription generation;
- exact native characteristic object identity.

A successful enable starts with `setNotifyValue(true, for:)`, waits for the exact `didUpdateNotificationStateFor` callback, verifies `CBCharacteristic.isNotifying`, and publishes:

```text
NotificationStateChanged(Enabled, actual_mode)
CommandCompleted(success)
```

Disable similarly calls `setNotifyValue(false, for:)`, waits for the exact state callback and `isNotifying == false`, then publishes:

```text
NotificationStateChanged(Disabled)
CommandCompleted(success)
```

An accepted enable or disable receives exactly one terminal on callback failure or teardown. Disconnect and invalidation do not synthesize a successful `Disabled` event.

CoreBluetooth delivers both explicit read results and subscribed values through `didUpdateValueFor`. The controller first resolves an exact active or retained subscription route; only callbacks not owned by a subscription may complete the exact in-flight read. A queued enable cannot consume values before native enable starts. Once `setNotifyValue(true, for:)` has been submitted, values may be correlated while CoreBluetooth is completing the state transition so an early valid delivery is not lost.

## Late-value and route barriers

Because notification-state and value callbacks carry no BleHub IDs or generations, teardown retains bounded exact-object barriers:

- an unresolved retired read callback blocks notification enable on that exact native value route;
- disable, failed enable, operation retirement, and rediscovery preserve the old subscription correlation for late values;
- successful re-enable switches to the new subscription correlation only after the positive CoreBluetooth state callback;
- reads remain fail closed for the remainder of an ambiguous native route/GATT session;
- bounded barrier capacity never evicts an unresolved route to make progress unsafely.

These barriers intentionally prefer a bounded fail-closed degradation over assigning an uncorrelatable callback to a newer caller-owned session or subscription.

## Rediscovery and fail-closed reset

Replacement GATT discovery cannot silently remove a live native subscription. The backend serializes internal `setNotifyValue(false, for:)` operations for all active subscriptions and waits for every exact notification-state callback before starting `discoverServices`.

Internal rediscovery teardown does not publish a synthetic public `Disabled`. If any implicit native disable fails, the backend retires the old GATT/I/O/subscription state and starts fail-closed CoreBluetooth connection cancellation. The connection enters `cancellingAfterBackendFailure` before the native cancel call, so a reentrant command cannot be admitted as if the connection were still usable. The retained route remains only until `didDisconnectPeripheral`, which publishes the passive `command_id=None` disconnect boundary.

## Deterministic validation

The complete Apple gate passed:

```sh
pixi run -e apple-ci just apple check
```

Observed results:

```text
Executed 71 tests, with 0 failures
apple_backend_macos_tests=passed
apple_backend_generic_ios_builds=passed
apple_xcframework_slices=passed
```

The package covered arm64 iOS device, arm64+x86_64 iOS simulator, and arm64+x86_64 macOS slices plus unsigned generic iOS simulator/device builds. Focused tests cover mode selection, synchronous validation, strict unsupported terminals, exact route reservation, shared FIFO ordering, read/value arbitration, enable/disable callback and event ordering, stale correlation, re-enable, bounded barriers, disconnect/invalidation retirement, rediscovery unsubscribe, partial teardown failure, and reentrant command rejection after fail-closed cancellation begins.

A focused independent review after iterative fixes reported:

```text
High: 0
Medium: 0
```

## macOS-to-Android Notification RF

The project-managed workflow passed:

```sh
pixi run -e apple-ci just apple notifications-macos-android
```

The DUT was the production Swift/CoreBluetooth backend on the Mac mini using the system-selected built-in controller. The Peripheral was the OnePlus 11 (`3ac5cc09`) running profile `blehub-android-basic-gatt-v1`. The run established:

```text
adapter_state=poweredOn
authorization=authorized
scan_rssi=-51
scan_connectable=true
gatt_services=5
gatt_characteristics=35
gatt_descriptors=24
actual_notification_mode=notification
notification_value=deadbeef
```

Initialization, provider-filtered scan, advertisement, scan stop and post-stop silence, connection, aggregate GATT discovery/database validation, Notification enable/value/disable event ordering, post-disable stale-value silence, logical disconnect, and post-disconnect silence all passed. The harness applied the strict exactly-once/domain-before-terminal ledger to the accepted Notification enable and disable commands.

After disable, the fixture control command intentionally attempted to send `cafebabe`. Android returned `no connected device subscribed for NOTIFICATION`; the outer workflow required that rejection and then observed the bounded host-side silence interval. The nested Just error is therefore expected negative evidence, while the top-level recipe completed successfully.

## Boundaries and next evidence

- This report claims a macOS `PlatformDefault` Notification RF pass only. It does not claim Indication or iPhone RF.
- The project Android `FFF2...` characteristic is dual notify+indicate. It validates `PlatformDefault` selecting actual Notification, exact value delivery, disable, and post-disable silence; it cannot establish strict Indication success on CoreBluetooth.
- Strict Indication success requires a separately validated indicate-only Peripheral profile.
- macOS evidence never proves iOS. Physical iPhone signing, install, authorization, application lifecycle, and RF remain independent gates.
- RSSI RF, passive disconnect/recovery, lifecycle accounting, repeated cycles, soak, and artifact-only Apple consumers remain open. The logical disconnect in this focused run is not passive-disconnect or destructive-recovery evidence.
- The public ABI remains unfrozen; applicable gates must rerun against the Phase 5 freeze candidate.
