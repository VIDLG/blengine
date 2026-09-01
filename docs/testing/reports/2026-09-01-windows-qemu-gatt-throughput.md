# Windows → QEMU/BLESS GATT write throughput — 2026-09-01

> Historical initial calibration. The later release-build read/write/Notification matrix for both Android and QEMU providers, including HCI timing evidence and lossless Notification pacing, supersedes the headline numbers in this report: [`2026-09-01-windows-android-qemu-throughput-matrix.md`](2026-09-01-windows-android-qemu-throughput-matrix.md).

## Result

**PASS for the executed end-to-end application GATT write throughput runs.**

The production `WindowsBackend` completed three measured 200-operation batches for each public write mode against the project-managed QEMU/BlueZ/BLESS Peripheral. Every accepted command produced exactly one matching `CharacteristicWritten` event followed by exactly one successful `CommandCompleted` terminal, and each mode's final 20-byte payload was read back exactly.

These values describe this complete Windows → WinRT → controller → RF → guest BlueZ/BLESS application path. They are not Bluetooth PHY, controller, ATT-airtime, or maximum platform throughput measurements.

## Candidate and environment identity

| Field | Value |
|---|---|
| Execution date | 2026-09-01 |
| Base revision | `fa97ad9be8a44d4bb1226c8830f79086ca9b5236`; candidate, runner, and report include uncommitted Phase 2 working-tree changes |
| Windows DUT | production `WindowsBackend`, native Windows Cargo `dev` build |
| Windows Central environment | same Windows 11 development host and MediaTek Bluetooth Adapter environment recorded in [`2026-09-01-windows-hardened-cleanup-qemu-rf.md`](2026-09-01-windows-hardened-cleanup-qemu-rf.md) |
| Peripheral VM | project-managed QEMU Debian 13 guest |
| Peripheral controller | TP-Link UB500, USB ID `2357:0604`, exclusively assigned to the guest |
| Peripheral stack | project-pinned BlueZ/BLESS environment |
| Fixture profile | `blehub-qemu-basic-gatt-v1` |
| Advertised/root service | `ffe00000-0000-4000-8000-000000000000` |
| Fixture readiness | `ready`, run ID `99b8671f-17d1-4dc1-b282-c0283c9decd1` |
| Public contract | version `5`; public ABI not frozen |

Fixture readiness was checked immediately before the benchmark. No destructive radio, USB, guest, or host fault was injected.

## Measurement method

The standalone runner is `tests/hil/windows/src/gatt_throughput.rs`, invoked through:

```text
pixi run just windows::gatt-throughput 30 \
  ffe00000-0000-4000-8000-000000000000 200 20
```

For each write mode, the runner:

1. completed initialization, service-filtered scan, stop, connect, public GATT discovery, and fixture validation before timing;
2. performed one untimed warm-up write;
3. synchronously dispatched 200 unique caller-owned commands with deterministic 20-byte payloads;
4. measured completion until all 200 commands had produced both `CharacteristicWritten` and successful `CommandCompleted` events;
5. rejected duplicate, missing, out-of-order terminal, identity-mismatched, backend-error, and operation-error results;
6. read the characteristic and required exact equality with the final deterministic payload;
7. logically disconnected and disposed the backend.

The 20-byte payload stays within the default ATT value payload and therefore avoids relying on an MTU/PDU size that BleHub does not currently expose. Setup, warm-up, final readback, logical disconnect, and disposal are excluded from the completion duration. For `WithoutResponse`, an additional 500 ms stabilization window before final readback is also excluded.

The synchronous dispatch duration measures command validation and admission into the backend's real private serialized GATT queue. It does **not** measure radio transmission and is retained only to show that caller-side fire-and-forget acceptance is not the throughput bottleneck. The reported BLE-path application throughput uses the all-terminal completion duration.

## Results

### Formal 200-operation runs

| Mode | Run | Completion | Operations/s | Payload KiB/s | Final readback |
|---|---:|---:|---:|---:|---|
| With response | 1 | 10,875.271 ms | 18.4 | 0.359 | verified |
| With response | 2 | 11,189.913 ms | 17.9 | 0.349 | verified |
| With response | 3 | 11,160.128 ms | 17.9 | 0.350 | verified |
| Without response | 1 | 164.391 ms | 1,216.6 | 23.762 | verified |
| Without response | 2 | 162.109 ms | 1,233.7 | 24.096 | verified |
| Without response | 3 | 158.747 ms | 1,259.9 | 24.607 | verified |

Summary:

| Mode | Median operations/s | Observed range | Median payload KiB/s | Observed range |
|---|---:|---:|---:|---:|
| With response | **17.9** | 17.9–18.4 | **0.350** | 0.349–0.359 |
| Without response | **1,233.7** | 1,216.6–1,259.9 | **24.096** | 23.762–24.607 |

A preceding 50-operation calibration also passed:

| Mode | Completion | Operations/s | Payload KiB/s |
|---|---:|---:|---:|
| With response | 2,174.839 ms | 23.0 | 0.449 |
| Without response | 40.609 ms | 1,231.3 | 24.048 |

The 200-operation runs are the reportable measurements because their longer interval reduces fixed timing noise. The lower `WithResponse` rate relative to the short calibration demonstrates that this Python/BLESS end-to-end path has scheduling or connection-timing variability; no PHY-level explanation is inferred from these application-level observations.

## Interpretation and limitations

- `WithResponse` waits for each serialized native operation's response/completion path and reached a median **17.9 writes/s**, or **0.350 KiB/s** of application payload.
- `WithoutResponse` still waits for each serialized WinRT operation's native completion before the backend starts the next queued operation; it reached a median **1,233.7 writes/s**, or **24.096 KiB/s** of application payload.
- The exact final readback proves the final queued payload reached the Peripheral application value. It does not independently prove every intermediate no-response packet at the controller/air level; the one-to-one Windows native completion and terminal-event accounting is the strongest evidence available through the current public contract.
- The BLESS Python write callback updates the characteristic and emits one JSON log record per write. Python callback execution and guest service logging are therefore included in the measured end-to-end path and may cap or perturb throughput.
- ATT MTU, negotiated data length, connection interval, PHY, controller buffers, retries, and RF loss were not queried. The numbers must not be extrapolated to larger payloads or presented as raw BLE speed.
- No Peripheral → Central Notification burst throughput was measured. That requires one guest-side burst control request with sequence/gap accounting; repeated host SSH/process invocations would measure orchestration overhead instead of BLE delivery.

## Remaining work

This benchmark adds attributable Central → Peripheral application throughput evidence but does not close Phase 2. The following relevant gates remain open:

- guest-side Notification burst benchmark and high-frequency FIFO/gap accounting;
- callback/native-handle leak accounting;
- final 10-cycle scan/connect soak;
- latest-candidate destructive passive disconnect and repeated recovery tests;
- BlueZ/guest/host reboot recovery and release-candidate Windows HIL.
