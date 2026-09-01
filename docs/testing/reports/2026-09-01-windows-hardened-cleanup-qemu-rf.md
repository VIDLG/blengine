# Windows hardened-cleanup candidate + QEMU/BLESS RF — 2026-09-01

## Result

**PASS for the executed non-destructive Windows/QEMU RF regression matrix.**

The hardened production `WindowsBackend` candidate passed initialization, service-filtered scan, advertisement delivery, requested scan stop, connection, public uncached GATT discovery, characteristic read/write, `PlatformDefault` Notification, strict Indication, notification disable, post-disable stale-route silence, logical disconnect, and capability-false connected RSSI behavior against the project-managed QEMU/BlueZ/BLESS Peripheral.

The Android provider matrix was not executed for this candidate. The physical phone was visible over ADB, but installation of the project fixture blocked after APK transfer while waiting for device-side USB-install approval. This is an Android fixture deployment blocker, not a Windows backend pass or failure.

This is a Phase 2 development RF regression report. It is not a release-candidate HIL, destructive fault, repeated-recovery, leak, or soak report.

## Build and environment identity

| Field | Value |
|---|---|
| Execution date | 2026-09-01 |
| Base revision | `fa97ad9be8a44d4bb1226c8830f79086ca9b5236`; the Windows backend, HIL runners, fixture orchestration, recipes, and this report were uncommitted working-tree changes |
| Candidate identity | post-RF hardened candidate with detached two-phase native cleanup, `connection.rs`, split GATT discovery/I/O/tests, live-only caller-ID collision checks, raw-address trace redaction, and hardened standalone HIL subprocess handling |
| Non-RF baseline | 34 Windows tests and 109 workspace tests; rustfmt, strict Clippy, Ruff, Just formatting, provider-profile isolation, and Mermaid checks passed before this RF run |
| Windows DUT | production `WindowsBackend`, native Windows Cargo `dev` build |
| Windows OS | Microsoft Windows 11 Home China, version `10.0.26200`, build `26200` |
| Windows Central controller | MediaTek Bluetooth Adapter, driver `1.1044.0.556`, provider Mediatek Inc. |
| RF runner | standalone `tests/hil/windows` crate; six cases were run sequentially so the Central controller was not shared concurrently |
| Peripheral VM | QEMU `11.1.0` project-managed runtime; Debian 13 guest, Linux `6.12.105+deb13-amd64` |
| Peripheral controller | TP-Link UB500, USB ID `2357:0604`, assigned exclusively to the guest through the project WinUSB/QEMU path |
| Peripheral stack | project-pinned BlueZ/BLESS environment; fixture profile `blehub-qemu-basic-gatt-v1` |
| Advertised/root service | `ffe00000-0000-4000-8000-000000000000` |
| Fixture readiness | state `ready`, run ID `99b8671f-17d1-4dc1-b282-c0283c9decd1` |
| VM image fingerprint | `e294cad9a15bea954be666aaae9c4cc0017468b93d454ffb244023609eec5908` |
| Fixture environment fingerprint | `f53cd0aa2bd9ec2396013cc0b3097d21126a259f70d7de1aac0c7b374c641d50` |
| Fixture source fingerprint | `7563e7ff2ab0ddbff030b0353da5c8633db421a1fae39dfd03ae41e4cd050550` |
| Candidate status | public ABI not frozen; working tree contained pre-existing and current Phase 2 changes |

The topology was one Windows Central controller plus one QEMU guest-owned Peripheral controller. The QEMU fixture used the provider-specific `FFE0...` service identity and therefore could not be selected by the Android fixture's `FFF0...` filter.

## Commands executed

The RF cases ran sequentially:

```text
pixi run just windows::scan-smoke 30 ffe00000-0000-4000-8000-000000000000
pixi run just windows::connect-smoke 30 ffe00000-0000-4000-8000-000000000000
pixi run just windows::gatt-smoke 30 ffe00000-0000-4000-8000-000000000000
pixi run just windows::read-write-smoke 30 ffe00000-0000-4000-8000-000000000000
pixi run just windows::notification-smoke 30 ffe00000-0000-4000-8000-000000000000 default qemu
pixi run just windows::notification-smoke 30 ffe00000-0000-4000-8000-000000000000 indication qemu
```

Readiness was checked after the matrix with:

```text
pixi run just peripheral::vm-status
pixi run just peripheral::vm-fixture-status
```

## Executed matrix

| Case | Result | Evidence |
|---|---|---|
| Initialize and scan | PASS | adapter reported `PoweredOn`; scan started; the `FFE0...` advertisement matched; requested stop emitted `ScanStopped` and terminal success |
| Connect and logical disconnect | PASS | emitted `Connecting`, `Connected`, and terminal success; logical disconnect emitted `Disconnected` and terminal success |
| Connected RSSI | PASS | command was accepted and completed with `NotSupported`, consistent with the Windows capability; no fabricated `RssiRead` event |
| Public GATT discovery | PASS | uncached all-or-error traversal returned services, characteristics, descriptors, properties, and the fixture CCCD |
| Characteristic read | PASS | the read/write characteristic returned its current value through `CharacteristicRead` followed by terminal success |
| Write with response | PASS | wrote `10 20 30`; deterministic readback returned exactly `10 20 30` |
| Write without response | PASS | wrote `AB CD`; deterministic readback returned exactly `AB CD` |
| `PlatformDefault` Notification | PASS | enable reported actual mode `Notification`; fixture update delivered exactly `DE AD BE EF` |
| Strict Indication | PASS | enable reported actual mode `Indication`, not Notification; fixture update delivered exactly `DE AD BE EF` |
| Disable and stale-route silence | PASS | both modes emitted `Disabled` and terminal success; a later `CA FE BA BE` fixture update produced no routed value during the two-second caller-owned observation window |
| Per-case disposal/recovery aggregation | PASS for normal path | all cases completed and logically disconnected without a scenario, dispose, or recovery error |

## GATT profile observed

The public database included the QEMU fixture root service and preserved its instance-addressed characteristic records:

| UUID | Properties | Role |
|---|---|---|
| `fff10000-0000-4000-8000-000000000000` | read, write with response, write without response | deterministic I/O |
| `fff20000-0000-4000-8000-000000000000` | read, notify, indicate; CCCD `0x2902` | controlled updates |
| `fff30000-0000-4000-8000-000000000000` | read | read-only boundary |
| `fff40000-0000-4000-8000-000000000000` | write with response, write without response | write-only boundary |

Windows/BlueZ system services were also present. The runner selected and validated the provider root service rather than assuming it was the only service in the database.

## Hardened-path interpretation

This run provides real RF regression evidence after the following non-RF hardening changes:

- connection and GATT correlation is invalidated and ownership is detached before potentially reentrant WinRT teardown;
- native `Cancel`, callback removal, and `Close` execute outside the delivery/state critical section;
- minimal connection/GATT cleanup gates prevent command overtaking;
- notification disable does not restore an active route after CCCD disable when callback revocation needs retry;
- historical caller-owned GATT/subscription tombstones are absent; live collision checks and backend-private generations isolate stale callbacks;
- fixture subprocess execution is bounded and attached to the Windows Job Object support path.

The successful notification updates exercised the normal bounded QEMU fixture subprocess path used by the hardened HIL support. This run did **not** inject a hung subprocess and therefore does not prove the forced Job Object termination, `taskkill` fallback, or reader-timeout paths at runtime; those paths were compiled and linted only.

The changing numeric `PeripheralId` values across separate runner processes are expected. They are backend-opaque identities scoped to each backend lifetime and are not persisted hardware addresses.

## Android fixture blocker

The connected physical device selected by the Android tooling was:

```text
serial=3ac5cc09
kind=physical
```

The fixture package was absent. Both status and install attempts confirmed the blocker:

```text
Activity class dev.blehub.fixture.peripheral/.MainActivity does not exist
Standard Install: Sent 2.42 MB (1 APK)
```

The install then waited until the host-side deadline without a device-side completion. The phone must allow install-via-USB or the vendor-specific USB debugging security option before the Android matrix can be rerun. No Android RF result is claimed for this candidate.

## Not executed or not claimed

The following were intentionally not run in this non-destructive session:

- Android scan/connect/GATT/I/O/Notification/Indication matrix;
- Android Bluetooth radio disable;
- QEMU USB detach or other passive-disconnect fault injection;
- active-subscription or in-flight GATT disconnect races;
- BlueZ daemon, guest, or Windows host reboot recovery;
- repeated USB faults or systematic recovery cycles;
- WHPX-after-reboot validation;
- 10-cycle scan/connect soak or long notification soak;
- callback-token/native-handle leak measurement;
- forced fixture-subprocess timeout/kill fault injection.

The earlier Android and QEMU reports remain evidence for their recorded candidates and cases. This report is the evidence that the current hardened candidate reran and passed the listed QEMU non-destructive RF matrix; it does not retroactively relabel any unexecuted gate.

## Remaining Phase 2 gates

- approve/install the Android development fixture and rerun the provider-specific `FFF0...` matrix against this candidate;
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
