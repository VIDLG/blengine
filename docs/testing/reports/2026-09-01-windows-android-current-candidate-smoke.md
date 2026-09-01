# Windows backend + Android Peripheral current-candidate smoke — 2026-09-01

## Result

**PASS for the executed Windows/Android development RF matrix and for device-level passive disconnect.**

The current production `WindowsBackend` passed scan, connection, public GATT discovery, characteristic I/O, Notification, strict Indication, logical disconnect, capability-false connected RSSI behavior, and a real provider device-level passive disconnect caused by disabling the Android Bluetooth radio.

Android application-level `BluetoothGattServer.cancelConnection()` did not cause WinRT `BluetoothLEDevice.ConnectionStatusChanged::Disconnected`, including when native GATT service objects were retained after public discovery. Those attempts are recorded as failed trigger experiments, not as evidence that the Windows callback path failed: disabling the same phone's Bluetooth radio immediately produced the required passive event through that path.

This is a Phase 2 development report, not a release-candidate HIL, repeated-fault, or soak report.

## Build and environment identity

| Field | Value |
|---|---|
| Execution date | 2026-09-01 |
| Base revision | `fa97ad9be8a44d4bb1226c8830f79086ca9b5236`; the Windows backend, RF runners, fixture controls, recipes, and this report were uncommitted working-tree changes |
| Windows DUT | production `WindowsBackend`, native Windows Cargo `dev` build |
| Windows Central controller | MediaTek Bluetooth Adapter |
| Peripheral device | HONOR `MAG-AN00` (HONOR 90 GT), serial `AQJJUT4308002943` |
| Android fixture | `blehub-android-basic-gatt-v1` |
| Advertised/root service | `fff00000-0000-4000-8000-000000000000` |
| Concurrent QEMU identity | `blehub-qemu-basic-gatt-v1`, service `ffe00000-0000-4000-8000-000000000000`; provider identity remained isolated by service UUID |
| Candidate status | public ABI not frozen; working tree contained pre-existing and current Phase 2 changes |

## Architecture refactor and subsequent non-RF hardening

The production crate was initially reorganized before the final passive-disconnect run and subsequently hardened without changing its public API or caller-owned execution model. The final structure is:

```text
crates/blehub-windows/src/
├── lib.rs             private modules; exports only WindowsBackend
├── backend.rs         shared state, BleBackend, process lease, dispose
├── connection.rs      connect/probe/status callbacks and connection cleanup
├── initialization.rs  adapter/radio initialization and callback-drain barrier
├── scan.rs            watcher/session ownership, filtering, stop, callback cleanup
├── gatt.rs            connection-scoped GATT state and invalidation orchestration
├── gatt/
│   ├── discovery.rs   uncached all-or-error traversal
│   ├── io.rs          FIFO read/write/CCCD and notification lifecycle
│   └── tests.rs
├── mapping.rs
└── error.rs
```

The refactor retained the useful local `btleplug` and SimpleBLE organization principles:

- native resource and callback token ownership are colocated;
- scan ownership is separate from connected-device ownership;
- callbacks capture weak shared state;
- cleanup removes callback routes before closing parent objects;
- GATT native instances remain instance-addressed rather than UUID-deduplicated.

It deliberately did not adopt their async runtime, channels, task queues, blocking operations, polling, retry, timeout, or automatic reconnect policy. BleHub still owns no worker, timer, mailbox, executor, or canonical device/GATT registry.

The six project-specific Windows RF runners now live in the standalone `tests/hil/windows` crate and share `tests/hil/windows/src/support` for the caller-owned channel sink, initialization, service-filtered scan/stop, command/connection waits, public discovery, fixture database validation, bounded fixture subprocesses, and scenario/dispose/recovery failure aggregation. They are test infrastructure, not production examples or artifact consumers. The passive-disconnect runner retains provider-specific fault control because that is test orchestration rather than reusable application behavior. Android radio ADB operations have per-call deadlines, and recovery attempts both radio enable and fixture restart even if one step fails.

After the RF run recorded below, the production backend moved potentially reentrant WinRT `Cancel`, callback removal, and `Close` calls outside its event-delivery lock. Live correlation is detached under `delivery -> state`, a minimal cleanup gate prevents command overtaking, and prepared events are attempted after native teardown. Historical caller-owned GATT/subscription ID tombstones were removed in favor of live-collision checks and private generations. Notification callback-revocation failure now retains disabled cleanup ownership and supports a revoke-only retry rather than restoring a route whose CCCD is already disabled. The resulting candidate passed 34 Windows non-RF tests, 109 workspace tests, and the complete strict lint suite, but it has not rerun the Android/QEMU RF matrix; this report therefore remains evidence for the earlier RF-tested candidate and must not be relabeled as a current-candidate RF pass.

## Non-RF validation

The refactored candidate passed:

```text
pixi run just fmt
pixi run just windows::check
pixi run just windows::test
pixi run just lint
pixi run just test::all
```

Observed deterministic results:

- 28 Windows tests passed;
- 103 Rust workspace tests passed across 12 suites;
- rustfmt, Just formatting, Clippy, Ruff, and provider-profile isolation passed;
- no public Windows crate API was added beyond `WindowsBackend`.

## Android fixture deployment

The physical device was selected deterministically:

```text
android_test_device=AQJJUT4308002943
```

The fixture build, deployment, launch, and advertising-readiness control succeeded. An earlier first-install/start attempt in the same current-candidate session had returned exit 127 after successful install and launch; a direct retry succeeded and Android logs did not show an application crash. The later build/start used for the final passive-disconnect run succeeded normally.

## Current-candidate functional RF matrix

After the private module split and example refactor, the current candidate reran and passed the complete Android-specific matrix:

```text
pixi run just windows scan-smoke 30 fff00000-0000-4000-8000-000000000000
pixi run just windows connect-smoke 30 fff00000-0000-4000-8000-000000000000
pixi run just windows gatt-smoke 30 fff00000-0000-4000-8000-000000000000
pixi run just windows read-write-smoke 30 fff00000-0000-4000-8000-000000000000
pixi run just windows notification-smoke 30 fff00000-0000-4000-8000-000000000000 default android
pixi run just windows notification-smoke 30 fff00000-0000-4000-8000-000000000000 indication android
```

Observed behavior:

1. initialization, service-filtered scan, advertisement delivery, and requested stop passed;
2. connect emitted `Connecting`, `Connected`, and terminal success;
3. connected `ReadRssiCommand` was accepted and completed with `NotSupported`, without fabricating `RssiRead`;
4. logical disconnect passed;
5. public uncached discovery returned the Android fixture service plus Android system services and accurately mapped all four fixture characteristics and the updates CCCD;
6. characteristic read passed;
7. write with response and deterministic readback passed;
8. write without response and deterministic readback passed;
9. `PlatformDefault` selected actual `Notification`, delivered exact `DE AD BE EF`, disabled successfully, and routed no stale value after disable;
10. strict Indication selected actual `Indication`, delivered exact `DE AD BE EF`, disabled successfully, and routed no stale value after disable.

Android correctly rejected both post-disable update attempts because no device remained subscribed. That provider-side rejection is expected; the Windows assertion was the following two-second stale-route silence.

## Passive-disconnect trigger characterization

### Application-level GATT server cancellation

The original Android provider control calls `BluetoothGattServer.cancelConnection()` for devices connected to the fixture GATT server while leaving the phone Bluetooth radio and other Android GATT services active.

It was tested against the current candidate in four observations:

| Connection ownership before trigger | Timeout | Windows passive event | Android evidence |
|---|---:|---|---|
| connect-only, before module refactor | 30 s | none | control success; `status=0 state=0` |
| connect-only, before module refactor | 60 s | none | control success; `status=0 state=0` |
| connect-only, after module refactor | 60 s | none | control success; `status=0 state=0` |
| public GATT discovery retained native services | 60 s | none | control success; `status=0 state=0` |

The two post-refactor commands were:

```text
pixi run just windows passive-disconnect-smoke 60 fff00000-0000-4000-8000-000000000000 android connect-only
pixi run just windows passive-disconnect-smoke 60 fff00000-0000-4000-8000-000000000000 android discover
```

The `discover` variant completed public uncached discovery, validated the fixture database, and retained its native GATT map before triggering cancellation. It still timed out. Therefore retaining `GattDeviceService` objects is not sufficient to make this application-level cancellation become a WinRT device-level disconnect.

The final Android log contained two successful server-side disconnect callbacks and no subsequent reconnect callback:

```text
connection device=9C:C7:D3:5E:7B:89 status=0 state=0
connection device=9C:C7:D3:5E:7B:89 status=0 state=0
```

This proves that the Android fixture server observed its own connection cancellation. It does not prove that the entire phone/controller link became disconnected from Windows. The same phone exposes many Android system GATT services, while WinRT reports `BluetoothLEDevice` connection status for the device rather than for one application-owned GATT server.

### Device-level Android Bluetooth radio fault

A new deterministic fixture control uses ADB shell Bluetooth management and waits for the native adapter state rather than sleeping:

```text
svc bluetooth disable
cmd bluetooth_manager wait-for-state:STATE_OFF
```

The passing command was:

```text
pixi run just windows passive-disconnect-smoke 60 fff00000-0000-4000-8000-000000000000 android-radio connect-only
```

After the same scan and successful connection, Android reported:

```text
disable: Success
wait-for-state:STATE_OFF: Success
android_bluetooth_radio=STATE_OFF
```

Windows immediately emitted the real native passive lifecycle event:

```text
ConnectionStateChanged {
  command_id: None,
  connection_id: ConnectionId { value: 1 },
  result: Ok { state: Disconnected }
}
```

No `DisconnectCommand` was issued. The runner then restored the physical device deterministically:

```text
svc bluetooth enable
cmd bluetooth_manager wait-for-state:STATE_ON
android_bluetooth_radio=STATE_ON
```

It rebuilt/relaunched the project fixture and received `control ... result=ok`. The same radio fault was run a second time after adding the outer process deadlines and best-effort recovery aggregation; it again produced the native passive event, restored `STATE_ON`, rebuilt/relaunched the fixture, and received `result=ok`. This proves two immediate radio/advertising recovery cycles, but not a systematic repeated-fault or soak gate. After the final matrix and recovery check, `pixi run just peripheral android-stop` returned `result=ok`, leaving the project fixture stopped.

## Interpretation

Verified:

- the current production `ConnectionStatusChanged` registration, correlation, teardown, and ingress delivery path observes a real Android device-level disconnect;
- `command_id=None` passive disconnect semantics survived the production module refactor;
- Android app-level GATT server cancellation and Android device radio loss are different fault classes;
- retaining public-discovery native service objects does not make the app-level cancellation sufficient;
- two Android radio disable/enable and fixture restart recovery cycles passed, including one after timeout/recovery hardening.

Inferred:

- the app-level cancellation ends the fixture server's connection ownership, while Windows or Android still has device-level/shared connection ownership through the phone's remaining GATT surface;
- WinRT therefore has no required `BluetoothLEDevice.ConnectionStatus` transition for that trigger.

Not claimed:

- that `BluetoothGattServer.cancelConnection()` never tears down a physical link on any Android device;
- that WinRT coalescing, controller behavior, or shared ownership has been isolated with HCI/ETW traces;
- that `GattSession.MaintainConnection` is needed. It remains intentionally unused because it would introduce system connection/reconnection policy contrary to the approved thin-backend plan;
- systematic repeated radio fault recovery beyond the two development cycles, active-subscription passive disconnect, in-flight GATT callback races, long notification soak, or native handle/token leak freedom.

## Remaining Phase 2 gates

- staged Windows package plus `tests/consumers/windows` artifact-only consumer;
- broader deterministic native seam/race coverage where real call sites justify it;
- combined name/service/manufacturer filter RF coverage;
- passive disconnect while a subscription or GATT operation/value callback is active;
- BlueZ daemon, guest, and host reboot recovery;
- systematic repeated USB and Android radio fault recovery;
- WHPX verification after reboot;
- 10-cycle scan/connect soak and long notification soak;
- callback token/native handle leak evidence;
- separate elevated Windows tooling TOCTOU hardening.
