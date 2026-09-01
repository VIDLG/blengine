# Windows backend + Android Peripheral characteristic read/write smoke — 2026-08-31

## Result

**PASS.**

This report records a Phase 2 development RF smoke for public characteristic read and write operations on the production `WindowsBackend`. It extends the earlier [GATT discovery smoke](2026-08-31-windows-android-gatt-discovery-smoke.md) by routing an opaque characteristic ID back through public commands, validating owned read payloads, both write modes, deterministic readback, ordered domain/terminal events, and normal disconnect cleanup.

It is not a release-candidate HIL report and does not replace the future QEMU/BLESS fault-injection or soak gates.

## Build and environment identity

| Field | Value |
|---|---|
| Execution time | 2026-08-31 12:49 +08:00 |
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

The Windows implementation uses a backend-private FIFO per connection to serialize public characteristic I/O. It retains native characteristics behind session-scoped opaque IDs and does not add a runtime, worker, blocking wait, timeout, retry, cached fallback, implicit chunking, or application protocol.

The implementation uses:

- `ReadValueWithCacheModeAsync(BluetoothCacheMode::Uncached)` and `GattReadResult` for reads;
- `DataWriter::WriteBytes` and `WriteValueWithResultAndOptionAsync` for writes;
- exact `GattWriteOption::WriteWithResponse` / `WriteWithoutResponse` mapping;
- `GattReadResult::ProtocolError` / `GattWriteResult::ProtocolError` when WinRT reports protocol failure;
- generation-scoped callback correlation and callback-first cleanup.

Independent review found and the implementation corrected a deferred-invalidation deadlock before RF: a `GattServicesChanged` callback could previously leave a reserved FIFO head waiting for invalidation while invalidation waited for that same head to leave `Reserved`. Reservation and the actual native Start/`SetCompleted` transition are now distinct states; only the narrow native transition defers invalidation. Final review reported no remaining High/Medium issue in the reviewed read/write paths.

The corrected candidate passed:

```text
pixi run just windows::check
pixi run just windows::test
pixi run rtk cargo clippy -p blehub-windows --all-targets -- -D warnings
```

Observed deterministic results were 21 Windows tests. A separate Rust workspace check, format check, Clippy, and test run passed with 96 tests across 12 suites. Editor diagnostics reported no errors or warnings in the backend, GATT implementation, read/write RF example, or Windows recipe.

The repository-wide `pixi run just verify` was not recorded as passing for this exact candidate because concurrent work in `tools/peripheral_fixture/bless.py` contained undefined `SEED_ROOT` / `SEED_IMAGE` names during Ruff. That unrelated Python diagnostic was not changed or suppressed as part of this slice.

## Test selection

Fixture service and characteristic:

```text
service:        fff00000-0000-4000-8000-000000000000
characteristic: fff10000-0000-4000-8000-000000000000
```

The characteristic advertises `read`, `write`, and `write_without_response` and starts with value `00` when the fixture lifecycle is initialized.

Commands:

```text
pixi run just peripheral::android-start
pixi run just windows::read-write-smoke 30
pixi run just peripheral::android-stop
```

The Android start recipe first observed the fixture's transient `STARTING` phase, then repeated readiness control and returned `result=ok`; RF execution began only after that successful readiness result. The timeout was 30 seconds per awaited operation.

The smoke executed:

```text
initialize
  -> scan and match fixture service
  -> stop scan
  -> connect
  -> public GATT discovery
  -> initial public characteristic read
  -> write with response
  -> public readback and exact payload comparison
  -> write without response
  -> caller-side 500 ms observation delay
  -> public readback and exact payload comparison
  -> explicit logical disconnect
  -> dispose
```

## Observed ordered result

The relevant public I/O sequence was:

```text
GattDiscovered {
  command_id: 5,
  connection_id: 1,
  gatt_session_id: 1,
  read_write_characteristic_id: 89
}
CommandCompleted { command_id: 5, error: None }

CharacteristicRead {
  command_id: 6,
  connection_id: 1,
  gatt_session_id: 1,
  characteristic_id: 89,
  value: [00]
}
CommandCompleted { command_id: 6, error: None }

CharacteristicWritten {
  command_id: 7,
  connection_id: 1,
  gatt_session_id: 1,
  characteristic_id: 89
}
CommandCompleted { command_id: 7, error: None }
CharacteristicRead {
  command_id: 8,
  connection_id: 1,
  gatt_session_id: 1,
  characteristic_id: 89,
  value: [10, 20, 30]
}
CommandCompleted { command_id: 8, error: None }

CharacteristicWritten {
  command_id: 9,
  connection_id: 1,
  gatt_session_id: 1,
  characteristic_id: 89
}
CommandCompleted { command_id: 9, error: None }
CharacteristicRead {
  command_id: 10,
  connection_id: 1,
  gatt_session_id: 1,
  characteristic_id: 89,
  value: [AB, CD]
}
CommandCompleted { command_id: 10, error: None }

ConnectionStateChanged { command_id: 11, connection_id: 1, state: Disconnected }
CommandCompleted { command_id: 11, error: None }
```

Every successful read emitted `CharacteristicRead` before its terminal success. Both write modes emitted `CharacteristicWritten` before their terminal success. All events preserved the caller-owned command, connection, and GATT session IDs and the backend-generated characteristic ID from the active database.

## Cleanup

- The example explicitly disconnected and then called `BleHub::dispose`.
- The Android fixture stop command returned `result=ok` after RF completion.
- No command, WinRT operation, fixture-control, cleanup, or process exit error was surfaced during the passing RF run.

## Proven by this run

- A public `ReadCharacteristicCommand` resolved the opaque characteristic ID in the active connection/GATT session and returned an owned byte payload.
- The fixture's initial value was read as `00`.
- Public write with response produced the required domain event and terminal ordering, and a subsequent public read observed the exact remote fixture value `10 20 30`.
- Public write without response produced the required domain event and terminal ordering, and a later public read observed the exact remote fixture value `AB CD`.
- Both write intents mapped to working Windows/Android interoperability paths without silently substituting a different public command shape.
- Normal explicit disconnect and disposal completed after retained characteristic I/O.
- Android fixture stop completed successfully after the run.

The without-response terminal still means only that Windows accepted/completed the local send attempt. The later readback independently proves that the Android fixture eventually applied this particular payload; it does not retroactively turn the write terminal into remote acknowledgement.

## Not proven

- Concurrent submission/FIFO ordering of multiple accepted read/write commands over RF; this run awaited each terminal before submitting the next command.
- An accepted read/write interrupted by explicit disconnect, passive disconnect, `GattServicesChanged`, rediscovery, or dispose over RF.
- Read/write failure mapping for permission, unreachable, ATT protocol error, malformed/stale IDs, unsupported characteristic properties, or platform exceptions over RF.
- Empty, 512-byte, platform/MTU-limit, or oversized values over RF; deterministic validation covers the public 512-byte hard boundary.
- Write-only characteristic behavior or public descriptor read/write.
- Notification/indication enable, value delivery, disable, or callback cleanup.
- Passive RF disconnect and connection-status callback delivery.
- Repeated scan/connect/discovery/read/write soak, leak freedom, or long-running recovery under hardware stress.
- QEMU/BLESS managed Peripheral behavior.
- Android Central backend behavior.

These remain separate Phase 2 gates.
