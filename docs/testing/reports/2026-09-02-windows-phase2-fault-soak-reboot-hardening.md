# Windows Phase 2 fault, soak, reboot, and elevated-tooling follow-up — 2026-09-02

## Result

**PASS for every completed Windows backend RF gate in this follow-up; one QEMU/WHPX same-process guest-reboot path failed as infrastructure, and the Windows host-reboot and hardened UAC execution boundaries remain unexecuted.**

Completed evidence:

- four strict 1,000-packet Notification/Indication delivery soaks across Android and QEMU/BLESS;
- three repeated destructive fault/recovery cycles for each provider;
- four faults synchronized with entered WinRT read, write, CCCD, and notification-value callbacks;
- a refreshed provisioned QEMU image and current fixture fingerprints;
- BlueZ daemon restart recovery;
- pre-hardening guest reboot recovery under TCG, with serial/systemd shutdown and SSH/controller/fixture recovery evidence;
- recovery by starting a fresh WHPX QEMU process after the WHPX same-process reboot failure;
- source-level hardening of all current Windows elevation paths.

Not passed or not executed:

- QEMU `11.1.0` under WHPX failed to reboot the guest in the same QEMU process with `WHPX: Unexpected VP exit code 4`; QMP reported `status=paused`;
- the strengthened TCG guest-reboot controller now requires a changed Linux boot ID, but that assertion was added after the recorded TCG run and was not rerun over RF;
- a real Windows host reboot was not performed and is not inferred from guest or process recovery;
- hardened UAC operations were not executed because USB bind/rollback would change the current controller ownership.

The WHPX failure occurred after the Debian guest completed systemd shutdown. The earlier TCG run observed guest shutdown plus SSH/controller/fixture recovery, and a new WHPX QEMU process subsequently restored UB500 ownership, BlueZ/BLESS readiness, doctor, and Windows scan. The supported Windows fixture procedure is therefore fresh-process recovery under WHPX rather than same-process guest reboot. This is recorded as a QEMU/WHPX infrastructure limitation, not as a `WindowsBackend` product failure or pass.

## Candidate and environment identity

| Field | Value |
|---|---|
| Execution date | 2026-09-02 |
| Base revision | `1bf05516f3c99295a26fd57ba5b314a85f2eec23`; implementation, fixture, runner, tooling, and report changes are working-tree changes on that revision |
| Public contract | version `5`; public ABI not frozen |
| Windows DUT | production `WindowsBackend`; standalone `tests/hil/windows` runners |
| Static/unit baseline before this report | 39 default Windows tests, 43 Windows tests with HIL callback gates, and 114 workspace tests passed |
| Windows OS | Microsoft Windows 11 Home China, version/build `10.0.26200` |
| Windows Central controller | MediaTek Bluetooth Adapter, driver `1.1044.0.556`, provider Mediatek Inc. |
| Android Peripheral | OnePlus 11, ADB serial `3ac5cc09`; profile `blehub-android-basic-gatt-v1`; service `fff00000-0000-4000-8000-000000000000` |
| QEMU Peripheral VM | QEMU `11.1.0`; Debian 13; Linux `6.12.105+deb13-amd64` |
| QEMU controller | TP-Link UB500 `2357:0604`, guest-owned through WinUSB passthrough |
| QEMU profile | `blehub-qemu-basic-gatt-v1`; service `ffe00000-0000-4000-8000-000000000000` |
| Refreshed VM image fingerprint | `4b6f09da25824886e2290c99f4ed46a6b053419a75c3064391a0dddd6b666f15` |
| Fixture environment fingerprint | `f53cd0aa2bd9ec2396013cc0b3097d21126a259f70d7de1aac0c7b374c641d50` |
| Fixture source fingerprint | `fd22d121300a1b213f75835cbb6900786c2b505acbf0a3bc1ebbdfdc00450db3` |

The refreshed QEMU image was provisioned in 3 minutes 18 seconds. The later recovery ended with a fresh WHPX process running, the guest-owned UB500 attached, the BLESS fixture advertising-ready, guest doctor passing, and Windows service-filtered scan passing.

## Strict Notification/Indication soak

The release `notification-throughput` runner was extended to execute either strict Notification or strict Indication, validate the requested mode against the actual enabled mode, receive a sequenced burst, disable the subscription, observe a bounded stale-route-silence window, and assert final native-resource zero state. Android serializes sends through `onNotificationSent`; QEMU uses the stable requested guest pacing already established by the throughput matrix.

Each provider/mode combination delivered 1,000 packets:

| Provider | Requested/actual mode | Packets | Rate | Payload throughput | Sequence result | Disable/stale route | Cleanup |
|---|---:|---:|---:|---:|---|---|---|
| Android | Notification | 1,000 | 482.0 ops/s | 9.413 KiB/s | `missing=0`, `duplicates=0`, `out_of_order=0` | PASS | exact zero |
| Android | Indication | 1,000 | 32.3 ops/s | 0.631 KiB/s | `missing=0`, `duplicates=0`, `out_of_order=0` | PASS | exact zero |
| QEMU/BLESS | Notification | 1,000 | 83.8 ops/s | 1.636 KiB/s | `missing=0`, `duplicates=0`, `out_of_order=0` | PASS | exact zero |
| QEMU/BLESS | Indication | 1,000 | 32.9 ops/s | 0.643 KiB/s | `missing=0`, `duplicates=0`, `out_of_order=0` | PASS | exact zero |

These are Peripheral-to-BleHub application-boundary rates for 20-byte payloads, not raw PHY or controller throughput.

A shorter strict 200-packet matrix also passed before the long run:

| Provider | Mode | Rate | Payload throughput |
|---|---:|---:|---:|
| Android | Notification | 474.1 ops/s | 9.259 KiB/s |
| Android | Indication | 33.2 ops/s | 0.648 KiB/s |
| QEMU/BLESS | Notification | 42.1 ops/s | 0.822 KiB/s |
| QEMU/BLESS | Indication | 29.6 ops/s | 0.578 KiB/s |

The persisted entry point is:

```text
pixi run just windows::subscription-soak <timeout> <service-uuid> <android|qemu> 1000 <pacing> 20 1
```

It runs strict Notification and strict Indication sequentially. It requires exclusive RF ownership and provider readiness.

## Repeated destructive fault/recovery

`passive-disconnect` now accepts a bounded cycle count from 1 through 10. The formal destructive default is three cycles because each iteration performs real scan, connection, public GATT discovery, active subscription, provider fault, passive-disconnect observation, pre-dispose resource release, final disposal, provider recovery, and fresh readiness validation.

| Provider/fault | Planned | Completed | Duration | Passive disconnect and cleanup | Recovery | Result |
|---|---:|---:|---:|---|---|---|
| OnePlus 11 Bluetooth radio disable | 3 | 3 | 46.23 s | `command_id=None`; pre-dispose connection/GATT/subscription release; post-dispose exact zero | radio `STATE_ON`, force-stop/activity restart, fresh advertising readiness every cycle | PASS |
| QEMU guest UB500/BlueZ teardown | 3 | 3 | 82.17 s | `command_id=None`; pre-dispose connection/GATT/subscription release; post-dispose exact zero | USB/controller, BlueZ, fixture, and doctor readiness every cycle | PASS |

The Android recovery path now uses `android-resume`: it force-stops the installed package, starts the existing Activity, and waits for a newly initialized advertising-ready fixture. Merely resending an intent to the existing Activity was rejected after it exposed stale internal `ready` state without a restarted advertiser. The optimized path avoids rebuilding and reinstalling the APK on every cycle and reduced the formal cycle time to approximately 15–16 seconds.

An exploratory Android 10-cycle request was manually interrupted after four completed cycles and is not recorded as a completed 10-cycle gate. The existing non-destructive scan/connect and active-resource lifecycle gates remain the formal 10-cycle soaks.

The persisted entry point is:

```text
pixi run just windows::fault-recovery-soak <timeout> <service-uuid> <android-radio|qemu> 3
```

## In-flight callback fault races

A Windows-only `hil-callback-gates` feature adds one-shot, generation-scoped barriers for four callback families:

- characteristic read completion;
- characteristic write completion;
- CCCD write completion;
- characteristic value notification.

The gates do not alter the public BleHub or UniFFI ABI. They do not wait while holding the backend state or event-delivery lock, stale gate release cannot open a later generation, and disposal releases a blocked callback.

| Provider/fault | Entered callback | Required terminal/event behavior | Stale post-release behavior | Cleanup/recovery | Result |
|---|---|---|---|---|---|
| Android radio disable | characteristic read completion | accepted read produced exactly one failure terminal; Windows may classify radio-off as `Connection` or `Gatt` such as services changed | no stale success | passive `command_id=None` disconnect, exact-zero disposal, provider recovery | PASS |
| QEMU controller teardown | characteristic write completion | accepted write produced exactly one failure terminal | no stale success | passive `command_id=None` disconnect, exact-zero disposal, provider recovery | PASS |
| Android radio disable | CCCD write completion | accepted enable produced exactly one failure terminal | no stale enabled state or success | passive `command_id=None` disconnect, exact-zero disposal, provider recovery | PASS |
| QEMU controller teardown | notification value callback | no command terminal is associated with the value callback | no stale notification delivery | passive `command_id=None` disconnect, exact-zero disposal, provider recovery | PASS |

Every run proved callback entry before applying the fault and kept the blocked delegate observable during the race. A raw QMP `device_del` alone was rejected as a deterministic fault primitive because it did not reliably terminate the active BLE link and allowed the blocked write to complete after release. QEMU in-flight tests use the guest Bluetooth/controller teardown path already validated by passive-disconnect testing.

The persisted entry point is:

```text
pixi run just windows::inflight-fault-smoke <timeout> <service-uuid> <android-radio|qemu> <read|write|cccd|notification-value>
```

## QEMU image, BlueZ, and reboot recovery

### Refreshed image

The stale provisioned image was rebuilt successfully. The current tracked identities are:

```text
image=4b6f09da25824886e2290c99f4ed46a6b053419a75c3064391a0dddd6b666f15
environment=f53cd0aa2bd9ec2396013cc0b3097d21126a259f70d7de1aac0c7b374c641d50
source=fd22d121300a1b213f75835cbb6900786c2b505acbf0a3bc1ebbdfdc00450db3
```

Automatic accelerator selection chose WHPX when available. VM startup, UB500 attachment, a unique BlueZ controller, fixture installation/synchronization, BLESS readiness, doctor, and Windows scan passed.

### BlueZ restart

`pixi run just peripheral::vm-bluez-restart` stopped the fixture, restarted `bluetooth.service`, waited for controller re-enumeration, restarted the fixture, ran doctor, and returned `bluez_restart_recovery=pass`. A subsequent Windows scan passed.

### Guest reboot under TCG

A TCG-launched VM passed the then-current `peripheral::vm-guest-reboot` path:

1. guest serial/systemd evidence reached shutdown;
2. SSH became unavailable after the reboot request;
3. the same QEMU process remained valid;
4. SSH returned;
5. UB500 and BlueZ returned;
6. the fixture restarted;
7. doctor passed;
8. Windows scan passed.

A final review identified that SSH disappearance/recovery alone is weaker than proving a new kernel boot. The persisted controller now captures `/proc/sys/kernel/random/boot_id` before reboot and requires a different non-empty value after SSH returns. Python compilation, Ruff, and formatting passed after this hardening, but the TCG RF path was not rerun. The report therefore preserves the executed pre-hardening recovery evidence without claiming that the new boot-ID assertion has passed.

### Guest reboot under WHPX

The same-process WHPX attempt failed with:

```text
WHPX: Unexpected VP exit code 4
```

QMP remained reachable but reported:

```text
status=paused
```

The guest serial/systemd evidence reached shutdown, so this is isolated to the QEMU `11.1.0`/WHPX reboot transition rather than Debian, systemd, the USB fixture, or readiness polling. The reboot controller now checks QMP for `paused` and reports the accelerator failure immediately instead of waiting the full SSH recovery timeout.

A fresh WHPX QEMU process then started successfully. UB500 attach, BlueZ/BLESS, doctor, fixture readiness, and Windows scan all recovered. Same-process WHPX guest reboot is therefore not a supported gate for this fixture topology; fresh-process restart is the supported WHPX recovery procedure.

## Elevated tooling hardening

A read-only review found three privilege-boundary risks:

1. a predictable writable PowerShell path crossed the UAC boundary;
2. Zadig was hashed before elevation but the original writable file was executed later;
3. USB rollback trusted pre-elevation INF identity and weakly validated its form.

The tooling now:

- creates a random elevated script with exclusive creation;
- hashes the exact UTF-8 bytes and embeds the expected SHA-256 in the encoded elevated bootstrap;
- reads the staged script once under elevation, verifies it, decodes and executes the same captured bytes, and cleans the file on success or failure;
- copies Zadig under elevation into a restricted random staging directory, verifies the protected copy against the pinned digest, executes that copy, checks its exit status, and removes the directory;
- requires `oem[0-9]+.inf` before rollback;
- rechecks under elevation the pinned device instance, unique VID/PID match, `WinUSB` service, unchanged INF identity, and absence of another present device sharing that package;
- checks both `pnputil /delete-driver` and device-scan exit codes.

Python compilation, Ruff, formatting, and command parsing passed for these changes. The UAC-changing operations were deliberately not executed, so this report records source/static hardening rather than a successful elevated bind or rollback.

## Phase 2 impact and remaining boundary

Closed by this follow-up:

- strict long Notification and Indication delivery/lifecycle soak for both managed providers;
- formal repeated three-cycle destructive fault/recovery for Android and QEMU;
- deterministic in-flight read/write/CCCD/value callback fault races;
- current QEMU image provenance;
- BlueZ daemon restart recovery;
- pre-hardening TCG same-process guest shutdown/recovery evidence;
- WHPX fresh-process recovery;
- source/static elevated-tooling TOCTOU hardening.

Accepted infrastructure limitation:

- QEMU `11.1.0` same-process guest reboot under WHPX. It is not used as a `WindowsBackend` correctness gate because the same guest reboots under TCG and a fresh WHPX process fully restores the fixture. The supported WHPX operation is process restart and reattachment, not guest reboot in place.

Still open and not inferred:

- a TCG RF rerun of the strengthened guest-reboot controller's boot-ID-change assertion;
- a real Windows host reboot and post-reboot backend/fixture recovery run;
- runtime execution of the hardened UAC bind/rollback paths when changing USB ownership is authorized;
- broader repeated fault counts, deliberately faulting native operations before callback entry, and longer-duration soak remain optional confidence extensions rather than results claimed here.

The normative Phase 2 exit conditions in `.plan/PLAN.md` do not require QEMU same-process reboot under every accelerator. The functional Windows, packaging, managed-fixture, leak-accounting, destructive recovery, in-flight race, and long-delivery gates are now covered. Phase 2 remains marked active until the exit boundary is explicitly reviewed; this report does not silently mark the phase complete or claim the unexecuted Windows host reboot.
