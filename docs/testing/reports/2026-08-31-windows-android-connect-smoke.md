# Windows backend + Android Peripheral connect smoke — 2026-08-31

## Result

**PASS.**

This report records a Phase 2 development RF smoke for the production `WindowsBackend`. It extends the earlier [scan smoke](2026-08-31-windows-android-scan-smoke.md) with a real Windows-to-Android BLE connection, an uncached GATT service probe used as connection evidence, and explicit logical disconnect cleanup.

It is not a release-candidate HIL report and does not replace the future QEMU/BLESS fault-injection or soak gates.

## Build and environment identity

| Field | Value |
|---|---|
| Execution time | 2026-08-31 09:52 +08:00 |
| Base revision | `fa97ad9be8a44d4bb1226c8830f79086ca9b5236`; the Windows backend candidate, recipes, and this report were uncommitted working-tree changes during execution |
| Build profile | Cargo `dev`, native Windows host target |
| Windows | Windows 11 家庭版 中文版, version `10.0.26200`, build `26200` |
| Central controller | MediaTek Bluetooth Adapter, USB `VID_13D3&PID_3585`, driver `1.1044.0.556` |
| Windows BLE enumerator | Microsoft Bluetooth LE Enumerator, driver `10.0.26100.8972` |
| Peripheral device | HONOR `MAG-AN00` (HONOR 90 GT), serial `AQJJUT4308002943` |
| Android | Android 16, API 36 |
| Peripheral implementation | `tests/peripherals/android`, shared fixture `blehub-basic-gatt-v1` |
| Topology | Native Windows Central controller ↔ physical Android development Peripheral; no VM or USB passthrough |
| Resource ownership | Windows Bluetooth controller and Android BLE advertiser/GATT server were used exclusively for this smoke |

## Pre-RF source gate

Source review found a race before RF execution: the uncached GATT probe completion handler temporarily removed its connection context while interpreting the result. A passive disconnect in that interval could be discarded, after which the probe could incorrectly report a successful connection.

The implementation was corrected before this smoke so:

- the `ProbingGatt` context remains visible until terminal arbitration;
- probe completion and passive disconnect serialize through the same backend-local delivery gate;
- a passive disconnect while probing terminates the accepted connect as a connection failure;
- the losing callback observes that the correlation was removed and cannot emit duplicate success or a second terminal event.

The corrected candidate passed:

```text
pixi run just windows::check
pixi run just windows::test
pixi run just verify
```

Observed deterministic results were 10 Windows tests and 85 workspace tests, with format, Clippy, UniFFI binding generation, and all three reviewed API snapshots passing.

## Test selection

Peripheral service filter:

```text
fff00000-0000-4000-8000-000000000000
```

Commands:

```text
pixi run just peripheral::android-start
pixi run just windows::connect-smoke 30
pixi run just peripheral::android-stop
```

The timeout was 30 seconds per awaited operation. The smoke executed:

```text
initialize
  -> start scan
  -> matching advertisement
  -> stop scan
  -> connect by discovered address + address type
  -> uncached GATT service probe
  -> explicit disconnect
  -> dispose
```

## Observed ordered events

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
  peripheral_id: 3,
  local_name: None,
  advertised_services: ["fff00000-0000-4000-8000-000000000000"],
  manufacturer_data: [],
  service_data: [],
  rssi: -48,
  connectable: true
}
ScanStopped {
  scan_id: 1,
  reason: Requested { command_id: 3 }
}
CommandCompleted { command_id: 3, error: None }
ConnectionStateChanged {
  command_id: 4,
  connection_id: 1,
  state: Connecting
}
ConnectionStateChanged {
  command_id: 4,
  connection_id: 1,
  state: Connected
}
CommandCompleted { command_id: 4, error: None }
ConnectionStateChanged {
  command_id: 5,
  connection_id: 1,
  state: Disconnected
}
CommandCompleted { command_id: 5, error: None }
```

The Android fixture intentionally omits a local name from its advertisement, so `local_name: None` is expected. `PeripheralId` and RSSI are observations from this process/run and are not stable cross-process identifiers or future thresholds.

## Cleanup

- The connect example called `BleHub::dispose` after explicit disconnect.
- The Android fixture stop command returned `result=ok`.
- No command, WinRT operation, fixture-control, or process exit error was surfaced during the passing run.

## Proven by this run

- The production Windows backend repeated the previously proven initialization, scan predicate, real advertisement, and requested scan-stop path.
- The discovered Windows peripheral reopen key retained both Bluetooth address and address type through connect dispatch.
- `ConnectCommand` produced ordered `Connecting`, `Connected`, and terminal success events with the caller-provided command and connection IDs.
- Windows created a `BluetoothLEDevice` and completed an uncached GATT service query with successful communication status while the device reported `Connected`; this is the backend's current real connection evidence.
- The connection events crossed WinRT completion handling → Windows correlation/delivery gate → `BackendEventIngressHandle` → Rust `BleEventSink`.
- `DisconnectCommand` ended the backend's logical connection lifecycle, released its owned Windows connection context, and produced ordered `Disconnected` and terminal success events.
- Android fixture stop completed successfully after the run.

## Not proven

- Public `DiscoverGattCommand` or unified GATT database mapping. The uncached service query in this smoke is only a private connection probe.
- Characteristic/descriptor discovery, read, write, notification, or indication.
- Android-side telemetry independently confirming the exact disconnect instant.
- Passive RF disconnect and connection-status callback delivery.
- Connect failure, timeout, caller retirement, late/stale callback, duplicate terminal, radio transition, or injected native-error behavior over RF.
- Repeated scan/connect soak, leak freedom, or long-running recovery under hardware stress.
- QEMU/BLESS managed Peripheral behavior.
- Android Central backend behavior.

These remain separate Phase 2 gates.
