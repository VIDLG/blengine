# Windows backend + Android Peripheral notification/indication smoke — 2026-08-31

## Result

**PASS.**

This report records Phase 2 development RF evidence for public Windows notification and indication configuration, passive value delivery, explicit disable, callback-route retirement, and normal disconnect cleanup. It also records the provider-profile isolation introduced after an earlier attempt selected the concurrently advertising QEMU fixture under the old shared service UUID.

It is not a release-candidate HIL report and does not replace passive-disconnect, fault-injection, recovery, or soak gates.

## Build and environment identity

| Field | Value |
|---|---|
| Execution date | 2026-08-31, after provider-profile isolation deployment |
| Base revision | `fa97ad9be8a44d4bb1226c8830f79086ca9b5236`; the Windows backend candidate, fixture profiles, recipes, and this report were uncommitted working-tree changes during execution |
| Build profile | Cargo `dev`, native Windows host target |
| Windows | Windows 11 家庭版 中文版, version `10.0.26200`, build `26200` |
| Central controller | MediaTek Bluetooth Adapter, USB `VID_13D3&PID_3585`, driver `1.1044.0.556` |
| Peripheral device | HONOR `MAG-AN00` (HONOR 90 GT), serial `AQJJUT4308002943` |
| Android | Android 16, API 36 |
| Android fixture | `blehub-android-basic-gatt-v1`, service `fff00000-0000-4000-8000-000000000000` |
| Concurrent QEMU fixture | `blehub-qemu-basic-gatt-v1`, service `ffe00000-0000-4000-8000-000000000000`, state `ready` |
| Resource ownership | Windows MediaTek controller, Android BLE advertiser/GATT server, and the QEMU-owned TP-Link UB500 were isolated by controller and provider-specific service UUID |

## Provider-profile isolation

The first notification attempt was not pass/fail evidence: Android and QEMU were both advertising the old `fff00000-0000-4000-8000-000000000000` service, and Windows selected the local QEMU fixture at RSSI `-10`. Android received no connection or CCCD callback and correctly rejected the attempted update because it had no subscriber.

The project then split the provider profiles while preserving their characteristic roles, properties, UUIDs, and initial values:

```text
Android: blehub-android-basic-gatt-v1
         fff00000-0000-4000-8000-000000000000

QEMU:    blehub-qemu-basic-gatt-v1
         ffe00000-0000-4000-8000-000000000000
```

With both fixtures advertising concurrently, production Windows scans observed:

```text
filter FFF0... -> advertisement FFF0..., RSSI -46 / -39, Android control closed the loop
filter FFE0... -> advertisement FFE0..., RSSI -6, QEMU remained ready
```

The passing notification and indication runs therefore selected Android by service identity rather than RSSI, discovery order, or local-name inference.

## Pre-RF source gate

The Windows implementation:

- registers `GattCharacteristic::ValueChanged` before starting the CCCD write;
- copies `GattValueChangedEventArgs::CharacteristicValue` into an owned `Vec<u8>` before delivery;
- writes CCCD through the existing per-connection FIFO with `WriteClientCharacteristicConfigurationDescriptorWithResultAsync`;
- reports the exact Windows-selected mode;
- suppresses stale callbacks using connection, GATT-session, subscription, characteristic, and private generation correlation;
- retains read-conflict routes until callback removal returns;
- removes callback handlers before publishing successful disable;
- performs no untracked cleanup CCCD writes outside the FIFO;
- adds no runtime, worker, timer, retry, blocking wait, or backend-owned event queue.

Independent review found three medium-severity cleanup linearization issues before RF: untracked cleanup CCCD writes could overlap later work, successful disable could release the read blocker before callback removal, and failed enable rollback had the same premature release window. All three were corrected. Final review reported no remaining High/Medium notification issue.

The corrected candidate passed:

```text
pixi run just windows::check
pixi run just windows::test
pixi run rtk cargo clippy -p blehub-windows --all-targets -- -D warnings
pixi run just lint
pixi run just test::all
pixi run just peripheral profiles-check
pixi run just peripheral android-check
```

Observed deterministic results were 26 Windows tests and 101 Rust workspace tests across 12 suites. Android lint/build, BLESS scenario validation, Ruff, Clippy, profile isolation checks, and relevant editor diagnostics also passed.

## Test selection and commands

Fixture service and updates characteristic:

```text
service:        fff00000-0000-4000-8000-000000000000
characteristic: fff20000-0000-4000-8000-000000000000
CCCD:           00002902-0000-1000-8000-00805f9b34fb
payload:        DE AD BE EF
```

Commands:

```text
pixi run just peripheral android-start
pixi run just windows notification-smoke 30 fff00000-0000-4000-8000-000000000000 default
pixi run just windows notification-smoke 30 fff00000-0000-4000-8000-000000000000 indication
pixi run just windows scan-smoke 20 ffe00000-0000-4000-8000-000000000000
pixi run just peripheral android-stop
pixi run just peripheral vm-fixture-status
```

The Android start recipe briefly observed the expected asynchronous `STARTING` state, retried readiness, and returned `result=ok` before RF began.

Each notification-mode run executed:

```text
initialize
  -> scan by Android-specific service UUID
  -> stop scan
  -> connect
  -> public GATT discovery
  -> enable notifications with caller-owned SubscriptionId
  -> wait for NotificationStateChanged::Enabled and terminal success
  -> trigger Android payload through ADB control
  -> verify NotificationReceived IDs and exact value
  -> disable the same subscription
  -> wait for NotificationStateChanged::Disabled and terminal success
  -> attempt another provider update and require Android to report no subscriber
  -> require a caller-owned two-second observation window with no routed value
  -> explicit logical disconnect
  -> dispose
```

## Observed ordered result

### PlatformDefault selected Notification

```text
AdvertisementObserved {
  advertised_services: [fff00000-0000-4000-8000-000000000000],
  rssi: -46,
  connectable: true
}

NotificationStateChanged {
  command_id: 6,
  connection_id: 1,
  gatt_session_id: 1,
  subscription_id: 1,
  characteristic_id: 90,
  state: Enabled { actual_mode: Some(Notification) }
}
CommandCompleted { command_id: 6, error: None }

Android control: notify deadbeef -> result=ok
NotificationReceived {
  connection_id: 1,
  gatt_session_id: 1,
  subscription_id: 1,
  characteristic_id: 90,
  value: [DE, AD, BE, EF]
}

NotificationStateChanged { command_id: 7, state: Disabled, ... }
CommandCompleted { command_id: 7, error: None }
Android control: notify cafebabe -> error: no connected device subscribed for NOTIFICATION
Windows routed values during the following two seconds: none
```

### Strict Indication remained Indication

```text
AdvertisementObserved {
  advertised_services: [fff00000-0000-4000-8000-000000000000],
  rssi: -39,
  connectable: true
}

NotificationStateChanged {
  command_id: 6,
  connection_id: 1,
  gatt_session_id: 1,
  subscription_id: 1,
  characteristic_id: 90,
  state: Enabled { actual_mode: Some(Indication) }
}
CommandCompleted { command_id: 6, error: None }

Android control: indicate deadbeef -> result=ok
NotificationReceived {
  connection_id: 1,
  gatt_session_id: 1,
  subscription_id: 1,
  characteristic_id: 90,
  value: [DE, AD, BE, EF]
}

NotificationStateChanged { command_id: 7, state: Disabled, ... }
CommandCompleted { command_id: 7, error: None }
Android control: indicate cafebabe -> error: no connected device subscribed for INDICATION
Windows routed values during the following two seconds: none
```

Both runs ended with ordered `Disconnected` state and terminal success for the explicit disconnect command.

## Cleanup

- The Android fixture stop control returned `result=ok`.
- The QEMU fixture remained active and `ready` with profile `blehub-qemu-basic-gatt-v1` and service `FFE0...`.
- No connection, subscription, scan, disconnect, disposal, or process failure occurred in either passing run.
- The expected nonzero Android controls after disable are positive evidence that the provider no longer had a matching CCCD subscriber; the RF example required that exact reason and treated any other control failure as a test failure.

## Proven by these runs

- Concurrent Android and QEMU fixtures cannot match the same provider-specific service filter.
- Windows `PlatformDefault` selected Notification when both Notify and Indicate were available and reported that exact mode.
- Windows strict Indication did not silently substitute Notification and reported `actual_mode: Indication`.
- Successful enable published `NotificationStateChanged::Enabled` before terminal success.
- Passive values preserved the connection, GATT-session, subscription, and characteristic IDs and copied the exact `DE AD BE EF` payload.
- Successful disable published `NotificationStateChanged::Disabled` before terminal success.
- Android observed the CCCD mode during enable and no longer had a matching subscriber after disable.
- No stale value was routed during the caller-owned two-second post-disable observation window.
- Explicit disconnect and disposal completed after both notification modes.

## Not proven

- Passive disconnect while a subscription is active, or notification behavior across reconnect.
- Disable, disconnect, `GattServicesChanged`, rediscovery, or dispose racing an in-flight value callback over RF.
- Callback removal failure, CCCD protocol/access failure, enable rollback, or stale native callback delivery under injected faults.
- Multiple simultaneous subscriptions, multiple characteristics, duplicate characteristic UUID instances, or high-rate/burst notification ordering over RF.
- Long-running delivery, leak freedom, repeated subscribe/unsubscribe soak, or backpressure behavior in a real downstream sink.
- Android `onNotificationSent` completion status as a separately asserted fixture result; Central receipt proves these payloads arrived, but the fixture-control acknowledgement only records dispatch acceptance.
- QEMU/BLESS notification or indication delivery; only its isolated `FFE0...` advertising/readiness and concurrent scan selection were checked in this session.
- Passive RF disconnect, USB/BlueZ fault injection, host/guest reboot recovery, WHPX-after-reboot, or the planned 10-cycle soak.
