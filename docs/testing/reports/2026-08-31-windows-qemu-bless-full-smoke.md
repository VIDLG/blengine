# Windows backend + QEMU/BLESS managed peripheral full smoke — 2026-08-31

## Result

**PASS for the executed QEMU/BLESS development RF matrix.**

This report records production `WindowsBackend` scan, connection, public GATT, characteristic I/O, Notification, strict Indication, unsupported connected RSSI, provider-triggered passive disconnect, USB reattach, guest doctor, and fixture readiness evidence against the project-managed Linux Peripheral VM.

It is not a release-candidate HIL or soak report. The Android comparison run requested in the same session was blocked because no physical Android device was visible to ADB; that infrastructure absence is not a backend failure.

## Build and environment identity

| Field | Value |
|---|---|
| Execution date | 2026-08-31 |
| Base revision | `fa97ad9be8a44d4bb1226c8830f79086ca9b5236`; backend, fixture control, recipes, examples, and this report were uncommitted working-tree changes |
| Windows DUT | production `WindowsBackend`, native Windows Cargo `dev` build |
| Windows Central controller | MediaTek Bluetooth Adapter |
| Peripheral VM | project-managed QEMU Debian 13 guest |
| Peripheral stack | BlueZ 5.82, BLESS 0.3.0 |
| Peripheral controller | TP-Link UB500 owned exclusively by the guest through WinUSB/QEMU USB passthrough |
| Fixture profile | `blehub-qemu-basic-gatt-v1` |
| Advertised/root service | `ffe00000-0000-4000-8000-000000000000` |
| Updates characteristic | `fff20000-0000-4000-8000-000000000000` |
| Deterministic notification payload | `DE AD BE EF` |

## Production completion fixes before RF

The source audit found and corrected two Windows acceptance/rollback issues:

1. `supports_read_rssi=false` was already correct, but `read_rssi` synchronously returned `BackendUnavailable`. It now accepts the command in a ready backend and emits exactly one `CommandCompleted` failure with `BleErrorCategory::NotSupported`; it does not emit `RssiRead` or reuse advertisement RSSI.
2. Initialize reservation and completion-handler registration rejection now best-effort cancel the already-created WinRT adapter operation before returning a synchronous dispatch error.

The Windows backend retained the useful local-source patterns from btleplug and SimpleBLE—uncached connection/discovery/read, explicit WinRT communication-status checks, retained native handles, and callback tokens—without copying their async runtime, channels, backend-owned timeout/retry policy, UUID-keyed canonical GATT cache, or incomplete callback cleanup.

## BLESS runtime control

The guest fixture gained a narrow runtime update control:

```text
host SSH request JSON + control_id + expected run_id
  -> SIGUSR1
  -> fixture rejects a run_id mismatch before mutation
  -> fixture validates value_hex
  -> updates role characteristic.value
  -> BlessServer.update_value(service_uuid, characteristic_uuid)
  -> atomic response JSON and structured log
```

`pixi run just peripheral vm-fixture-update deadbeef` returned a response matching the active fixture run and exact value. The request now carries the expected `run_id`, which the guest validates before changing the characteristic, so a request observed from an older fixture run cannot mutate a replacement run. This control acknowledgement only proves that BLESS accepted the local update. Notification or indication delivery is proven separately by the Windows `NotificationReceived` event.

## Pre-RF gates

Passed before or during this session:

```text
pixi run just windows::check
pixi run just windows::test
pixi run just lint
pixi run just test::all
pixi run python tests/peripherals/bless/bless_fixture.py validate --scenario tests/conformance/scenarios/archive/bless/qemu-basic-gatt-peripheral.json
```

Observed deterministic results:

- 27 Windows tests passed;
- 102 Rust workspace tests passed across 12 suites;
- Clippy, Ruff, rustfmt, fixture scenario validation, and provider-profile isolation passed;
- relevant Rust diagnostics were clean.

## QEMU/BLESS RF commands

```text
pixi run just peripheral vm-fixture-sync
pixi run just peripheral vm-fixture-start
pixi run just peripheral vm-fixture-update deadbeef
pixi run just windows scan-smoke 30 ffe00000-0000-4000-8000-000000000000
pixi run just windows connect-smoke 30 ffe00000-0000-4000-8000-000000000000
pixi run just windows gatt-smoke 30 ffe00000-0000-4000-8000-000000000000
pixi run just windows read-write-smoke 30 ffe00000-0000-4000-8000-000000000000
pixi run just windows notification-smoke 30 ffe00000-0000-4000-8000-000000000000 default qemu
pixi run just windows notification-smoke 30 ffe00000-0000-4000-8000-000000000000 indication qemu
pixi run just windows passive-disconnect-smoke 45 ffe00000-0000-4000-8000-000000000000 qemu
```

## Observed results

### Scan, connect, and unsupported RSSI

- initialization reported `PoweredOn` and terminal success;
- FFE0 service filtering selected the QEMU provider, with observed RSSI varying from approximately `-5` to `-17` dBm;
- scan start and requested stop completed in the required order;
- connection emitted `Connecting`, `Connected`, then terminal success;
- connected `ReadRssiCommand` returned dispatch `Ok(())` and emitted only:

```text
CommandCompleted {
  command_id: 5,
  error: Some(NotSupported: "Windows does not support connected one-shot RSSI reads")
}
```

- no `RssiRead` event was emitted;
- active logical disconnect remained successful.

### Public GATT discovery and characteristic I/O

The uncached public discovery returned the standard Windows services plus the QEMU fixture root. The fixture service preserved all four configured characteristic roles and the updates CCCD:

```text
read-write  fff10000... read + write response + write without response
updates     fff20000... read + notify + indicate + CCCD
read-only   fff30000... read
write-only  fff40000... write response + write without response
```

Characteristic I/O passed:

```text
initial read                    -> 00
write with response 10 20 30   -> readback 10 20 30
write without response AB CD   -> readback AB CD
```

### PlatformDefault Notification

```text
NotificationStateChanged Enabled { actual_mode: Some(Notification) }
CommandCompleted success
BLESS update deadbeef -> current-run response updated
NotificationReceived value=[DE, AD, BE, EF]
NotificationStateChanged Disabled
CommandCompleted success
BLESS update cafebabe -> current-run response updated
2-second Windows observation window -> no routed value
explicit disconnect -> success
```

### Strict Indication

```text
NotificationStateChanged Enabled { actual_mode: Some(Indication) }
CommandCompleted success
BLESS update deadbeef -> current-run response updated
NotificationReceived value=[DE, AD, BE, EF]
NotificationStateChanged Disabled
CommandCompleted success
BLESS update cafebabe -> current-run response updated
2-second Windows observation window -> no routed value
explicit disconnect -> success
```

The public `NotificationReceived` event represents payload delivery for both modes. The preceding enabled state proves which exact CCCD mode Windows configured.

### Passive disconnect and USB recovery

The first attempted trigger stopped only the BLESS systemd service. Windows did not report `ConnectionStatusChanged::Disconnected` within 30 seconds, so that attempt failed as a trigger-selection/infrastructure experiment and is not passive-disconnect pass/fail evidence. Removing a GATT application is not equivalent to dropping the BLE controller or RF link.

The corrected test detached the guest-owned UB500 while the production backend was connected. Windows then emitted:

```text
ConnectionStateChanged {
  command_id: None,
  connection_id: 1,
  result: Ok { state: Disconnected }
}
```

No disconnect command was issued. The runner then:

1. reattached the UB500;
2. reran the guest doctor successfully;
3. confirmed one BlueZ controller, GATT manager, and LE advertising manager;
4. restarted the current fixture release;
5. received a new fixture `run_id` and `state=ready`.

This proves one real USB/controller-loss passive disconnect and one immediate detach/reattach/readiness recovery. It does not prove repeated fault recovery or soak.

## Android comparison status

Both `pixi run just peripheral android-status` and `pixi run just test android-device physical` reported no connected physical Android device. Therefore no new Android RF command was run in this session. Earlier Android scan/connect/GATT/read/write/Notification/Indication reports remain valid historical evidence, but they were not re-executed against this exact working-tree candidate.

## Proven by this session

- all Windows v0.1 public command entry points have production implementations or the planned capability-false terminal behavior;
- QEMU-specific FFE0 service selection, scan, connect, public GATT discovery, read, and both write types work through the production backend;
- Windows capability-false connected RSSI is accepted and terminates as `NotSupported` without fabricating a value;
- QEMU/BLESS can drive deterministic Notification and strict Indication values through a run-correlated out-of-band control path and real BLE RF;
- enable/state/terminal ordering, exact payload IDs/value, disable, and two-second post-disable stale-route silence passed for both modes;
- provider controller loss produces a passive `command_id: None` disconnect;
- one UB500 detach/reattach, guest doctor, fixture restart, and new readiness run passed.

## Post-review hardening follow-up

After the RF matrix above, an independent ownership/error-path review produced three focused fixes:

- WinRT initialization setup is serialized against `dispose`, and a callback drain barrier prevents native work from being created after disposal; if completion registration fails and cancellation also fails, the command remains accepted, emits one terminal failure, and retains the adapter/radio operation in backend-private retired cleanup ownership until `dispose` rather than violating dispatch-rejection rollback;
- the passive-disconnect runner now reports both the primary assertion failure and any backend-disposal or fixture-recovery failure, so a failed USB reattach/doctor/restart cannot be hidden;
- the BLESS control request now includes the expected fixture `run_id`, and the guest rejects a mismatch before reading or mutating the updates characteristic.

The follow-up passed `windows::check`, 28 Windows tests, 103 workspace tests, Clippy, Ruff, rustfmt, provider-profile isolation, and the BLESS scenario/control-protocol validator. The updated fixture was synchronized and restarted as run `99b8671f-17d1-4dc1-b282-c0283c9decd1`; a real `vm-fixture-update deadbeef` returned `state=updated` with the same run ID and exact value. Android remained absent from ADB. The complete RF matrix was not repeated after these narrow hardening changes, so this follow-up does not relabel the earlier RF evidence as a new full candidate rerun.

The review also identified a pre-existing Windows tooling TOCTOU risk around non-elevated verification followed by elevated execution of Zadig or a mutable PowerShell script. That issue is recorded separately in the Plan and was not silently folded into this backend slice.

## Not proven

- a new Android comparison run on this exact candidate;
- passive disconnect with active subscriptions or in-flight GATT I/O/value callback;
- `GattServicesChanged`, rediscovery, disconnect, disable, or dispose racing an in-flight callback over RF;
- multiple simultaneous subscriptions, burst/high-rate ordering, downstream backpressure, or long-running notification delivery;
- BlueZ daemon restart, guest reboot, host reboot, repeated USB faults, WHPX-after-reboot, or 10-cycle soak;
- handle/token leak freedom under long stress;
- staged `tests/consumers/windows` package consumption;
- release-candidate HIL and ABI freeze gates.
