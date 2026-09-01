# Windows backend + Android Peripheral scan smoke — 2026-08-31

## Result

**PASS after one implementation-corrected development attempt.**

This report records a Phase 2 development RF smoke. It proves the current Windows backend candidate can initialize the real Windows adapter, submit and stop a WinRT advertisement watcher, receive the project Android Peripheral advertisement, apply the shared service filter, and deliver the corresponding unified BleHub events.

It is not a release-candidate HIL report and does not replace the future QEMU/BLESS fault-injection or soak gates.

## Build and environment identity

| Field | Value |
|---|---|
| Execution time | 2026-08-31 09:36 +08:00 |
| Base revision | `fa97ad9be8a44d4bb1226c8830f79086ca9b5236`; Windows backend candidate and this report were uncommitted working-tree changes during execution |
| Build profile | Cargo `dev`, native Windows host target |
| Windows | Windows 11 家庭版 中文版, version `10.0.26200`, build `26200` |
| Central controller | MediaTek Bluetooth Adapter, USB `VID_13D3&PID_3585`, driver `1.1044.0.556` |
| Windows BLE enumerator | Microsoft Bluetooth LE Enumerator, driver `10.0.26100.8972` |
| Peripheral device | HONOR `MAG-AN00` (HONOR 90 GT), serial `AQJJUT4308002943` |
| Android | Android 16, API 36 |
| Peripheral implementation | `tests/peripherals/android`, shared fixture `blehub-basic-gatt-v1` |
| Topology | Native Windows Central controller ↔ physical Android development Peripheral; no VM or USB passthrough |
| Resource ownership | Windows Bluetooth controller and Android BLE advertiser were used exclusively for this smoke |

## Test selection

Peripheral service filter:

```text
fff00000-0000-4000-8000-000000000000
```

Commands:

```text
pixi run just peripheral::android-start
pixi run just windows::scan-smoke 30
pixi run just peripheral::android-stop
```

The scan timeout was 30 seconds. The smoke executed:

```text
initialize -> start scan -> matching advertisement -> stop scan -> dispose
```

## Attempt 1: failed and localized

The first run successfully initialized the Windows adapter:

```text
AdapterSnapshotChanged {
  state: PoweredOn,
  scan_authorization: NotRequired,
  connection_authorization: NotRequired,
  name: Some("蓝牙")
}
CommandCompleted { command_id: 1, error: None }
```

The scan command then failed because the implementation read `BluetoothLEAdvertisementWatcher.Status` immediately after `Start()` and observed `Created`:

```text
CommandCompleted {
  command_id: 2,
  error: "advertisement watcher entered unexpected status BluetoothLEAdvertisementWatcherStatus(0)"
}
```

Classification: **product implementation failure**, not fixture or RF failure.

The local official API snapshot says `Start()` transitions immediately to `Started` or `Aborted`, but this Windows/driver combination returned successfully while an immediate `Status()` read still reported `Created`. Local `btleplug` source also treats successful `Start()` return as the start result and does not synchronously poll `Status()`.

The implementation was corrected so successful `Start()` return is Windows local start evidence. An immediate/synchronous `Stopped` callback can still win the delivery gate and complete start as failure; a later abort is reported by the normal `ScanStopped` error path. BleHub does not claim that RF packets have already been received when it emits `ScanStarted`.

Peripheral cleanup after the failed attempt completed successfully.

## Attempt 2: passed

Observed ordered events:

```text
AdapterSnapshotChanged {
  command_id: 1,
  state: PoweredOn,
  scan_authorization: NotRequired,
  connection_authorization: NotRequired,
  name: Some("蓝牙")
}
CommandCompleted { command_id: 1, error: None }
ScanStarted { command_id: 2, scan_id: 1 }
CommandCompleted { command_id: 2, error: None }
AdvertisementObserved {
  scan_session_id: 1,
  peripheral_id: 2,
  local_name: None,
  advertised_services: ["fff00000-0000-4000-8000-000000000000"],
  manufacturer_data: [],
  service_data: [],
  rssi: -47,
  connectable: true
}
ScanStopped {
  scan_id: 1,
  reason: Requested { command_id: 3 }
}
CommandCompleted { command_id: 3, error: None }
```

The Android fixture intentionally does not include a local name in its advertisement, so `local_name: None` is expected. RSSI is an observation from this run, not a stable threshold or future assertion.

## Proven by this run

- Native Windows adapter/radio initialization reached `PoweredOn`.
- Windows authorization mapping was `NotRequired` for scan and connection.
- `StartScanCommand` produced ordered `ScanStarted` and terminal success.
- The real Android advertisement crossed WinRT callback → Windows mapping → `BackendEventIngressHandle` → Rust `BleEventSink`.
- The shared 128-bit service UUID was mapped canonically and passed the backend-owned scan predicate.
- `PeripheralId`, RSSI, and connectable data were present in the unified observation.
- Explicit stop produced one requested `ScanStopped` followed by terminal success.
- Android fixture stop completed successfully after the run.

## Not proven

- Windows connect/disconnect behavior.
- GATT discovery, read, write, descriptor access, notification, or indication.
- Manufacturer-data or service-data RF mapping on this fixture advertisement.
- Duplicate, stale, late, abort, suspend, radio transition, or fault-injection behavior.
- Repeated scan/connect soak or leak freedom under hardware stress.
- QEMU/BLESS managed Peripheral behavior.
- Android Central backend behavior.

These remain separate Phase 2 gates.
