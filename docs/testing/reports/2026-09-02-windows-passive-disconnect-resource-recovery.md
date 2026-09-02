# Windows active-subscription passive-disconnect and recovery — 2026-09-02

## Result

**PASS for the executed Android radio-disable and QEMU/UB500 detach active-subscription passive-disconnect gates.**

Both providers reached a connected state with public GATT discovery and an enabled `PlatformDefault` Notification subscription before the destructive fault. Each successful run then observed a passive `ConnectionStateChanged { command_id: None, state: Disconnected }`, required all connection/GATT/subscription logical roots and WinRT delegates to be released before `BleHub::dispose()`, required every backend logical root and all 16 instrumented callback delegate families to be zero after disposal, and restored the provider to an advertising-ready state.

The first Android and QEMU attempts exposed two fixture-control portability/timing defects. They are recorded as infrastructure failures, not product failures. After focused fixes, both complete gates passed. This report closes the latest-candidate destructive passive-disconnect and active-subscription cleanup gates for one Android radio cycle and one QEMU USB cycle. It does not claim repeated-fault, in-flight GATT operation, reboot, WHPX, or long notification/indication soak evidence.

## Candidate and environment identity

| Field | Value |
|---|---|
| Execution date | 2026-09-02 |
| Base revision | `1bf05516f3c99295a26fd57ba5b314a85f2eec23`; the runner, recovery fixes, and this report are working-tree changes on that revision |
| Public contract | version `5`; public ABI not frozen |
| Windows DUT | production `WindowsBackend`, native Windows Cargo `dev` build with HIL-only `resource-accounting` feature |
| Windows unit baseline | 39 tests passed |
| Android Peripheral | OnePlus 11, ADB serial `3ac5cc09`, profile `blehub-android-basic-gatt-v1`, service `fff00000-0000-4000-8000-000000000000` |
| QEMU Peripheral VM | QEMU `11.1.0`; Debian 13; Linux `6.12.105+deb13-amd64` |
| QEMU controller | TP-Link UB500 `2357:0604`, guest-owned through WinUSB passthrough |
| QEMU profile | `blehub-qemu-basic-gatt-v1`, service `ffe00000-0000-4000-8000-000000000000` |
| Restored QEMU run ID | `f7470bae-eab2-408f-93a3-49c1b0278305` |
| Fixture environment fingerprint | `f53cd0aa2bd9ec2396013cc0b3097d21126a259f70d7de1aac0c7b374c641d50` |
| Fixture source fingerprint | `fd22d121300a1b213f75835cbb6900786c2b505acbf0a3bc1ebbdfdc00450db3` |

The QEMU doctor reported `image_fingerprint=stale` both before and after execution. This was not hidden or relabeled: guest SSH, Debian/kernel identity, fixture environment/source fingerprints, BlueZ service, one unblocked UB500 controller, GATT manager, LE advertising manager, and the active ready fixture all passed. The stale image marker remains follow-up evidence for image provenance; it did not prevent the tracked source/environment fixture or the exercised RF path from operating.

## Runner extension and assertions

The standalone `passive-disconnect` HIL runner now accepts three connection anchors:

- `connect-only`;
- `discover`;
- `subscribed`.

The new `subscribed` anchor performs:

1. initialize and provider-specific service-filtered scan;
2. connect;
3. public GATT discovery and fixture database validation;
4. `PlatformDefault` Notification enable with actual mode `Notification`;
5. active-resource assertion, including `ConnectionStatusChanged`, `GattServicesChanged`, and characteristic `ValueChanged` delegates;
6. provider fault;
7. passive disconnect observation with `command_id=None`;
8. pre-dispose assertion that connection/GATT/subscription roots and delegates have been released while initialized adapter/radio ownership remains valid;
9. disposal and exact zero assertion for all backend roots and all 16 callback delegate families;
10. provider recovery.

The pre-dispose assertion is intentionally distinct from the final disposal assertion. It prevents `dispose()` from hiding a passive-disconnect cleanup leak.

## Executed commands

Readiness and deterministic validation:

```text
pixi run just peripheral::android-status
pixi run just peripheral::vm-doctor
pixi run just peripheral::vm-fixture-status
pixi run just fmt
pixi run just lint
pixi run just windows::check
pixi run just windows::test
```

Destructive gates:

```text
pixi run just windows::passive-disconnect-smoke 60 fff00000-0000-4000-8000-000000000000 android-radio subscribed
pixi run just windows::passive-disconnect-smoke 45 ffe00000-0000-4000-8000-000000000000 qemu subscribed
```

Post-recovery readiness:

```text
pixi run just peripheral::android-status
pixi run just peripheral::vm-doctor
pixi run just peripheral::vm-fixture-status
```

## Results

| Provider/fault | Active subscription | Passive event | Pre-dispose connection cleanup | Post-dispose exact zero | Recovery | Final result |
|---|---|---|---|---|---|---|
| OnePlus 11 Bluetooth radio disable | PASS | PASS, `command_id=None` | PASS | PASS | radio `STATE_ON`, fixture ready | PASS |
| QEMU guest UB500 detach | PASS | PASS, `command_id=None` | PASS | PASS | USB attached, one BlueZ controller, fixture ready | PASS |

The successful HIL binaries intentionally printed only one `PASS` line. Recent bounded event traces were emitted only during failed infrastructure attempts.

## Infrastructure failures and fixes

### Android optional shell command

The first Android attempt reached an enabled subscription, but fixture control failed because this OnePlus ROM exposes only `enable` and `disable` under `cmd bluetooth_manager`; it does not implement `wait-for-state:STATE_OFF/STATE_ON`.

The project control script now uses `svc bluetooth enable/disable` and polls the stable `dumpsys bluetooth_manager` `state: ON/OFF` observation with a 30-second bound. During verification, Windows Python's default GBK decoding also failed on non-GBK device output. `_adb_output` now decodes ADB output explicitly as UTF-8 with replacement for malformed bytes. The radio was restored, the fixture was restarted, and the full Android gate then passed.

### QEMU hotplug enumeration delay

The first QEMU attempt completed the DUT portion: it reached an active subscription, observed the passive disconnect, and did not report a resource assertion failure. Recovery failed because QMP and guest `lsusb` saw the reattached UB500 before BlueZ had recreated its controller. Immediate doctor observed `usb_2357_0604=present` but `bluetooth_controllers=0`; fixture startup consequently ran too early.

After approximately ten seconds, the same attached device appeared as one functional BlueZ controller and manual fixture recovery passed. `usb_attach()` now waits up to 30 seconds for exactly one `bluetoothctl` controller before running the full doctor. The complete destructive QEMU gate was then rerun and passed, including automatic recovery.

These failures are fixture infrastructure evidence. Neither is recorded as a Windows backend failure.

## Phase 2 impact

Closed by this report:

- latest-candidate Android device-level radio passive disconnect;
- latest-candidate QEMU USB-detach passive disconnect;
- passive disconnect while public GATT and an active Notification subscription are retained;
- pre-dispose connection/GATT/subscription delegate and logical-root release for both providers;
- post-dispose exact-zero accounting for both destructive paths;
- one complete Android radio recovery and one complete QEMU USB/BlueZ/fixture recovery.

Still open and not inferred:

- repeated Android radio and QEMU USB fault/recovery cycles;
- a fault deliberately synchronized with an in-flight GATT read/write/CCCD completion or value callback;
- BlueZ daemon restart, guest reboot, and Windows host reboot recovery;
- WHPX-after-Windows-reboot validation;
- long Notification/Indication soak;
- elevated Windows tooling TOCTOU hardening.

Phase 2 therefore remains active, but its primary single-cycle destructive passive-disconnect and active-subscription cleanup gates are now complete.
