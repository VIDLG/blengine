# Windows backend + Android Peripheral GATT discovery smoke — 2026-08-31

## Result

**PASS.**

This report records a Phase 2 development RF smoke for public `DiscoverGattCommand` on the production `WindowsBackend`. It extends the earlier [scan smoke](2026-08-31-windows-android-scan-smoke.md) and [connect smoke](2026-08-31-windows-android-connect-smoke.md) with complete uncached Windows traversal and unified mapping of the public `service -> characteristic -> descriptor` scope.

It is not a release-candidate HIL report and does not replace the future QEMU/BLESS fault-injection or soak gates.

## Build and environment identity

| Field | Value |
|---|---|
| Execution time | 2026-08-31 11:21 +08:00 |
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

Before RF execution, source review compared the implementation with the approved Plan and selectively audited the local btleplug and SimpleBLE Windows implementations. BleHub retained native GATT objects and explicit callback-first/child-to-parent cleanup, but did not adopt their blocking/runtime, UUID-keyed map, retry, cache-fallback, or partial-success policies.

Independent review found and the implementation corrected these lifecycle races before RF:

- native discovery work is started only after reserving its correlation and cleanup ownership;
- discovery start/continuation is linearized against disconnect and native invalidation;
- `GattServicesChanged` arriving during a reserved native transition or cleanup is deferred and drained rather than discarded;
- discovery terminal events are published only after retiring state and cleaning native ownership;
- disconnect can end a pending `CreatingDevice` or `ProbingGatt` logical lifecycle without falsely reporting Windows cancellation evidence;
- a retired connect correlation still receives one late terminal attempt and cannot become the current connection again.

The corrected candidate passed:

```text
pixi run just windows::check
pixi run just windows::test
pixi run just verify
```

Observed deterministic results were 17 Windows tests and 92 workspace tests, with format, Clippy, UniFFI Kotlin/Swift binding generation, Ruff, and all three reviewed API snapshots passing. Editor diagnostics reported no errors or warnings in the Windows backend, GATT implementation, or RF example. A final independent source review reported no remaining High/Medium issue in the reviewed discovery and pending-connect disconnect paths.

## Test selection

Peripheral service filter and validation target:

```text
fff00000-0000-4000-8000-000000000000
```

Commands:

```text
pixi run just peripheral::android-start
pixi run just windows::gatt-smoke 30
pixi run just peripheral::android-stop
```

The timeout was 30 seconds per awaited operation. The smoke executed:

```text
initialize
  -> start scan with fixture service predicate
  -> matching advertisement
  -> stop scan
  -> connect by discovered address + address type
  -> private uncached service probe for connection evidence
  -> public DiscoverGattCommand
  -> uncached service/characteristic/descriptor traversal
  -> fixture database validation
  -> explicit logical disconnect
  -> dispose
```

## Observed ordered result

The relevant public sequence was:

```text
AdapterSnapshotChanged { command_id: 1, state: PoweredOn, ... }
CommandCompleted { command_id: 1, error: None }
ScanStarted { command_id: 2, scan_id: 1 }
CommandCompleted { command_id: 2, error: None }
AdvertisementObserved {
  scan_session_id: 1,
  peripheral_id: 2,
  advertised_services: ["fff00000-0000-4000-8000-000000000000"],
  rssi: -51,
  connectable: true
}
ScanStopped { scan_id: 1, reason: Requested { command_id: 3 } }
CommandCompleted { command_id: 3, error: None }
ConnectionStateChanged { command_id: 4, connection_id: 1, state: Connecting }
ConnectionStateChanged { command_id: 4, connection_id: 1, state: Connected }
CommandCompleted { command_id: 4, error: None }
GattDiscovered {
  command_id: 5,
  connection_id: 1,
  gatt_session_id: 1,
  root_service_count: 13
}
CommandCompleted { command_id: 5, error: None }
ConnectionStateChanged { command_id: 6, connection_id: 1, state: Disconnected }
CommandCompleted { command_id: 6, error: None }
```

`GattDiscovered` preceded the discovery terminal success as required. The complete database contained 13 Windows root services. The fixture service was present as opaque `GattServiceId { value: 13 }` and mapped as `primary: Some(true)`.

The fixture subtree validated as:

| UUID | Opaque ID in this run | Properties / descriptor |
|---|---:|---|
| `fff10000-0000-4000-8000-000000000000` | characteristic `89` | read, write with response, write without response |
| `fff20000-0000-4000-8000-000000000000` | characteristic `90` | read, notify, indicate; CCCD `00002902-0000-1000-8000-00805f9b34fb` as descriptor `93` |
| `fff30000-0000-4000-8000-000000000000` | characteristic `91` | read |
| `fff40000-0000-4000-8000-000000000000` | characteristic `92` | write with response, write without response |

These numeric opaque IDs are valid only for this connection/GATT session and are not stable values or UUID substitutes.

## Cleanup

- The GATT example explicitly disconnected and then called `BleHub::dispose`.
- The Android fixture stop command returned `result=ok` after RF completion.
- No command, WinRT operation, fixture-control, or process exit error was surfaced during the passing run.

## Proven by this run

- Production `WindowsBackend` accepted public `DiscoverGattCommand` after a real Windows-to-Android connection.
- Windows completed uncached service, characteristic, and descriptor enumeration across the complete v0.1 flat traversal scope.
- The backend emitted one complete unified `GattDatabase`; no partial database path was observed.
- Caller-provided command, connection, and GATT session IDs were preserved in the public result and terminal event.
- Backend-generated service, characteristic, and descriptor IDs remained distinct from UUIDs and routed the discovered native instances.
- Windows root services mapped to `primary: Some(true)` under the approved Windows policy.
- The managed fixture service, all four expected characteristics, their exact public properties, and the expected CCCD descriptor were mapped correctly.
- `GattDiscovered` was delivered before the matching successful `CommandCompleted`.
- Explicit logical disconnect completed after discovery, and retained GATT resources were exercised through the normal cleanup path without a surfaced cleanup error.
- Android fixture stop completed successfully after the run.

## Not proven

- Public characteristic or descriptor read.
- Public write with response or write without response. Discovered properties only describe capability; they do not prove operation behavior.
- Notification/indication enable, value delivery, disable, or callback cleanup over the production Windows backend.
- Public descriptor read/write behavior.
- Duplicate UUID instances under the same parent, rediscovery replacement, `GattServicesChanged`, or failure-atomic all-or-error behavior over RF; these remain deterministic/source-review gates until a controlled fixture drives them.
- Passive RF disconnect and connection-status callback delivery.
- Connect/discovery failure, timeout, caller retirement, late/stale callback, duplicate terminal, radio transition, or injected native-error behavior over RF.
- Repeated scan/connect/discovery soak, leak freedom, or long-running recovery under hardware stress.
- QEMU/BLESS managed Peripheral behavior.
- Android Central backend behavior.

These remain separate Phase 2 gates.
