# Windows → Android/QEMU BLE application throughput matrix — 2026-09-01

## Result

**PASS for the executed release-build read, write-with-response, write-without-response native-terminal, Peripheral callback-accounted write, and lossless Notification throughput selections against both project-managed Peripheral providers.**

The original formal baseline is the median of three 200-operation runs using 20-byte application payloads. A later follow-up on the same date added a best-effort Windows throughput connection policy, negotiated-link evidence, and 20/100/244/512-byte probes; those follow-up values are reported separately and do not rewrite the original medians. Read and write timing excludes initialization, scan, connection, discovery, warm-up, final verification, disconnect, and disposal. Notification timing uses the interval from the first valid sequenced `NotificationReceived` to the last, so 200 notifications contain 199 measured delivery intervals.

The results are end-to-end BleHub application-boundary measurements. They are not raw PHY, controller, or ATT-airtime maxima.

## Candidate and environment

| Field | Value |
|---|---|
| Execution date | 2026-09-01 |
| Base revision | `fa97ad9be8a44d4bb1226c8830f79086ca9b5236`; candidate, fixtures, runners, and report include uncommitted Phase 2 work |
| Windows DUT | production `WindowsBackend`; standalone HIL runners built with Cargo `--release` |
| Windows Central | Windows 11 development host, MediaTek Bluetooth Adapter environment recorded in the preceding current-candidate reports |
| Public contract | version `5`; public ABI not frozen |
| Payload | 20 application bytes; no dependence on an unqueried extended ATT MTU |
| QEMU provider | Debian 13 guest, BlueZ/BLESS, guest-owned TP-Link UB500 `2357:0604`, profile `blehub-qemu-basic-gatt-v1`, service `ffe00000-0000-4000-8000-000000000000` |
| Android provider | OnePlus 11, ADB serial `3ac5cc09`, project Kotlin fixture `blehub-android-basic-gatt-v1`, service `fff00000-0000-4000-8000-000000000000` |
| QEMU GATT fixture run | `881d548d-54ad-477f-870c-c23ee1416e4c` |
| QEMU paced Notification fixture run | `2647b66b-48d0-4e13-b199-967dbee9ae6c` |
| QEMU write-ledger fixture run | `adfec090-ad91-40b6-935b-49132261d222` |

Android and QEMU advertised concurrently but had provider-specific service UUIDs. Every HIL runner selected a fixture with the required service filter, not discovery order or RSSI.

## Summary

### Median application-boundary throughput

| Direction and operation | QEMU/BLESS | Android |
|---|---:|---:|
| Peripheral → Central uncached read | **17.9 ops/s**, **0.350 KiB/s** (~2.87 kbit/s) | **8.3 ops/s**, **0.163 KiB/s** (~1.34 kbit/s) |
| Central → Peripheral write with response, callback-confirmed | **16.7 ops/s**, **0.326 KiB/s** (~2.67 kbit/s) | **8.3 ops/s**, **0.163 KiB/s** (~1.34 kbit/s) |
| Central → Peripheral write without response, Windows native terminals | **1,263.6 ops/s**, **24.680 KiB/s** (~202.2 kbit/s) | **1,262.4 ops/s**, **24.657 KiB/s** (~202.0 kbit/s) |
| Central → Peripheral write without response, Peripheral callback span | **91.8 callbacks/s**, **1.793 KiB/s** (~14.69 kbit/s) | **844.7 callbacks/s**, **16.498 KiB/s** (~135.2 kbit/s) |
| Peripheral → Central lossless Notification | **85.1 notifications/s**, **1.661 KiB/s** (~13.61 kbit/s) | **820.0 notifications/s**, **16.015 KiB/s** (~131.2 kbit/s) |

The low baseline read/write-with-response values are consequences of serialized uncached ATT request/response procedures and the negotiated scheduling observed below. They are not Rust command-dispatch limits. Release-build command admission exceeded one million commands/s in these runs; removing Rust debug checks did not materially change native completion throughput.

## Follow-up: throughput-optimized links and larger payloads

The production Windows backend now best-effort calls `BluetoothLEDevice.RequestPreferredConnectionParameters` with `BluetoothLEPreferredConnectionParameters::ThroughputOptimized()` after the uncached GATT connection probe succeeds. Unsupported APIs, drivers, or rejected status remain nonfatal and are debug-traced. This keeps the public ABI unchanged while asking Windows for a lower-latency, higher-power connection policy.

This materially changed response-based throughput. The repeated 20-byte read/write-with-response rate rose from roughly **17/17 ops/s to 33/33 ops/s on QEMU**, and from roughly **8/8 ops/s to 32/33 ops/s on Android**. The gain is native connection scheduling, not Rust dispatch concurrency.

### Android negotiated link and payload matrix

The OnePlus 11 fixture reported ATT MTU **517**, then `onPhyUpdate` confirmed **LE 2M TX / LE 2M RX**. The table below is one 100-operation follow-up matrix on one negotiated link; all writes reached the fixture callback ledger without missing, duplicate, out-of-order, out-of-range, foreign-batch, or malformed values.

| Payload | Uncached read | Write with response | No-response Peripheral callback throughput |
|---:|---:|---:|---:|
| 20 bytes | 0.632 KiB/s | 0.647 KiB/s | ~22.6 KiB/s |
| 100 bytes | 3.131 KiB/s | 3.245 KiB/s | ~68.8 KiB/s |
| 244 bytes | 7.785 KiB/s | 7.972 KiB/s | **~96.6 KiB/s (~791 kbit/s)** |
| 512 bytes | **16.103 KiB/s** | **16.409 KiB/s** | ~79.6 KiB/s |

For Android no-response writes, **244 bytes** was the measured application-callback sweet spot: a separate 200-operation run reached about **98.0 KiB/s (~803 kbit/s)**. A 244-byte, 200-value Notification burst also passed losslessly at **98.276 KiB/s (~805 kbit/s)**. Larger is not automatically faster: 512 bytes improved serialized read/with-response byte throughput but reduced no-response callback throughput.

### QEMU active PHY and controller-buffer limit

The QEMU throughput HIL can now ask the guest-owned controller to use LE 2M after Windows reports `Connected`, and verifies the result with `LE Read PHY` before timed GATT or Notification traffic. One integrated 244-byte × 200 run reported `active_phy=2m/2m` and passed all callback accounting:

| Operation | QEMU LE 2M result |
|---|---:|
| Uncached read | 1.753 KiB/s |
| Write with response | 7.828 KiB/s |
| No-response Peripheral callbacks | 91.0 callbacks/s, ~21.7 KiB/s (~178 kbit/s) |

Changing the active QEMU link from 1M to 2M did not materially increase write callback throughput. A bounded guest `btmon` capture proved that the rest of the radio link was already configured appropriately:

- ATT MTU exchange: **517** effective fixture request/response evidence;
- LE Data Length Change: **251 TX / 251 RX octets**;
- connection interval: **15.00 ms** during throughput traffic;
- PHY Update Complete and explicit Read PHY: **LE 2M / LE 2M**;
- 244-byte application Notification: one reassembled ATT Handle Value Notification with ATT length 246 and `Data[244]`.

The limiting evidence is the TP-Link UB500/Realtek host-controller interface. `LE Read Buffer Size` returned **27-byte LE ACL packets with 8 buffers** (`1B 00 08`), while LE Read Buffer Size v2 returned unsupported status `0x01`. A 244-byte Notification occupies 251 bytes including L2CAP and ATT overhead and is therefore split into about ten 27-byte host-controller fragments. The capture showed the final fragment as `dlen 8` and roughly 115–120 ms between completed 244-byte notifications. This controller boundary explains why PHY 2M and MTU 517 cannot make the QEMU Peripheral Notification path resemble the phone.

A lossless 244-byte QEMU Notification run on a verified 2M link reached **1.988 KiB/s (~16.3 kbit/s)**. That is better than the earlier 1M 244-byte probe (~1.151 KiB/s), but remains controller-buffer limited. For this UB500, 20-byte Notifications fit exactly in one 27-byte HCI ACL packet after 4-byte L2CAP and 3-byte ATT overhead; larger values improve byte throughput only modestly while increasing latency and fragmentation.

Repeated 512-byte QEMU reads are not a passing selection: one 100-operation probe disconnected around operation 78. Stable QEMU throughput matrices therefore remain limited to 20/100/244 bytes until that provider/link behavior is investigated separately.

## GATT read/write method

The runner `tests/hil/windows/src/gatt_throughput.rs` performed, for each provider:

1. initialize, service-filtered scan/stop, connect, and public GATT discovery outside timing;
2. write one deterministic 20-byte read value outside timing;
3. warm up one uncached read, then dispatch 200 read commands and require for every command exactly one matching `CharacteristicRead`, exact value, and one later successful terminal;
4. warm up each write mode, then reset a fixture-local ledger with a fresh 8-byte batch ID and expected count;
5. dispatch 200 fixed 20-byte writes encoded as ASCII `BHW1`, batch ID, and zero-based little-endian `u64` sequence;
6. require exactly one matching `CharacteristicWritten` followed by exactly one successful terminal per write command;
7. require exact final readback and query the fixture ledger exactly once after each write mode;
8. require 200 received and unique Peripheral callbacks with zero missing, duplicate, out-of-order, out-of-range, foreign-batch, or malformed values;
9. disconnect and dispose.

The Windows backend intentionally uses one private FIFO for read, write, and CCCD operations per connection and has at most one native GATT I/O operation in flight. The caller dispatches the whole batch without waiting, so dispatch duration is reported only as command admission evidence and is not counted as BLE throughput.

### QEMU raw release runs

| Run | Read ops/s | With-response terminal ops/s | With-response callback ops/s | No-response terminal ops/s | No-response callback ops/s | No-response callback span |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 17.9 | 16.7 | 16.7 | 1,263.6 | 92.0 | 2,164.192 ms |
| 2 | 17.8 | 16.7 | 16.7 | 1,233.2 | 91.8 | 2,167.988 ms |
| 3 | 18.4 | 16.7 | 16.7 | 1,299.5 | 91.5 | 2,175.651 ms |

Median durations were approximately 11.160 seconds for 200 reads, 11.988 seconds for 200 writes with response, 158.279 ms for 200 Windows no-response native terminals, and 2.168 seconds from the first to last no-response Peripheral callback. Every run reported 200 received/unique callbacks and zero sequence violations for both write modes.

### Android raw release runs

| Run | Read ops/s | With-response terminal ops/s | With-response callback ops/s | No-response terminal ops/s | No-response callback ops/s | No-response callback span |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 8.3 | 8.3 | 8.3 | 1,287.4 | 844.7 | 235.588 ms |
| 2 | 8.3 | 8.3 | 8.3 | 1,262.4 | 647.2 | 307.489 ms |
| 3 | 8.3 | 8.3 | 8.3 | 1,246.5 | 896.5 | 221.965 ms |

Median durations were approximately 24.000 seconds for 200 reads, 24.009 seconds for 200 writes with response, 158.425 ms for 200 Windows no-response native terminals, and 235.588 ms from the first to last no-response Peripheral callback. Every run reported 200 received/unique callbacks and zero sequence violations for both write modes. The Android no-response callback rate varied materially across the three short batches and is retained rather than treated as a stable controller maximum.

### No-response interpretation

`WriteWithoutResponse` has no remote ATT Write Response. The backend still waits for each WinRT async operation to complete before starting the next queued operation, but WinRT completion can precede Peripheral application processing because Windows/controller buffers may drain later.

The follow-up fixture ledger closes the earlier application-callback accounting gap: all three 200-write runs on each provider reported all 200 unique sequences with zero missing, duplicate, out-of-order, out-of-range, foreign-batch, or malformed values. This proves one-for-one delivery to the Android `BluetoothGattServerCallback` and BLESS write callback for the executed batches.

It also shows why the two rates must remain separate. Windows produced about 1,262–1,264 no-response native terminals/s, while the callback arrival spans yielded a median **91.8 callbacks/s on QEMU/BLESS** and **844.7 callbacks/s on Android**. WinRT terminal completion therefore represents local/native send acceptance and queue progress, not synchronized Peripheral application completion. The callback span uses the provider-local monotonic interval from first to last callback; because host and provider clocks are not synchronized, it is not presented as trigger-to-final end-to-end latency or raw PHY throughput.

## Why response-based operations are slow

A passive QEMU guest `btmon` HCI/ATT capture was taken around a 20-operation diagnostic run. It observed:

- LE Enhanced Connection Complete at a **30.00 ms** connection interval;
- an update to **15.00 ms** during setup/discovery;
- a later update back to **30.00 ms** during the write-with-response sequence;
- 20-byte application writes as `ATT: Write Request` with ATT PDU length 22, each followed by `ATT: Write Response`;
- 22 relevant write requests including setup/warm-up, with a measured inter-request median of **54.732 ms**; after the setup transition, repeated intervals were predominantly about **53.9–66.6 ms**.

That observed request spacing predicts roughly 15–19 completed request/response operations per second and directly matches the formal QEMU result. The path waits for approximately two 30 ms connection-event opportunities for many steady-state operations, plus host/VM/BlueZ/BLESS scheduling.

Source review independently confirms:

```text
caller batch dispatch
→ WindowsBackend per-connection private GATT FIFO
→ one ReadValueWithCacheModeAsync(Uncached) or WriteValueWithResultAndOptionAsync
→ WinRT completion and communication status
→ public value/write event and successful terminal
→ next queued native operation
```

The BLESS fixture emits a JSON log for each ordinary read/write callback, and the Android fixture publishes an app snapshot/log per ordinary callback. The `BHW1` benchmark path instead performs only in-memory ledger updates and suppresses per-write logs on both providers, so journald, Logcat, and UI rendering are not multiplied by 200 inside the measured callback batches. Android response/read operations remain slower while both providers produce almost identical no-response native-terminal rates. The dominant evidenced factors are request/response serialization, negotiated connection scheduling, native platform completion, and provider/device scheduling.

## Notification method

The runner `tests/hil/windows/src/notification_throughput.rs` uses payloads:

```text
4 bytes   ASCII BHN1
8 bytes   per-run burst ID
8 bytes   little-endian sequence, 0..N-1
```

For both providers it:

1. enables strict Notification and waits for the public enabled state plus successful terminal;
2. performs one untimed burst warm-up;
3. triggers one provider-local burst control operation in a worker thread;
4. receives BLE events concurrently rather than waiting for ADB/SSH to finish;
5. requires exact correlation IDs, 20-byte length, magic, burst ID, strict in-order sequence, no duplicate, and all 200 values;
6. observes an additional 250 ms duplicate/late-event drain window;
7. disables Notification, disconnects, and disposes.

The reported delivery throughput uses the 199 intervals from first to last valid event. Trigger-to-first includes ADB/SSH/process control overhead and is reported separately by the runner, not counted as BLE delivery speed.

### QEMU/BLESS lossless Notification

An initial zero-pacing 200-notification run passed once at 84.0 notifications/s, while the next identical run delivered only **183/200** before timeout. In both cases the guest called all 200 `update_value` operations in approximately 11 ms. This proves BLESS local acceptance is not delivery completion and that an unbounded producer can overflow or coalesce somewhere in the BLESS/BlueZ/controller/RF/WinRT path.

Pacing probes at 10,000, 5,000, 2,000, 1,000, and 500 µs all reached the same Central delivery ceiling of about 83–86 notifications/s without gaps. The formal QEMU runs therefore use explicit guest-side **500 µs requested pacing**, the fastest tested stable setting. Guest asyncio scheduling made the observed local generation span about 258–260 ms for 200 values rather than the theoretical 99.5 ms. Pacing is test-provider backpressure, not hidden in BleHub or the Windows backend.

| Run | Delivery span | Notifications/s | Payload KiB/s | Guest local elapsed | Missing/duplicate/order |
|---:|---:|---:|---:|---:|---|
| 1 | 2,310.095 ms | 86.1 | 1.682 | 260.305 ms | 0 / 0 / 0 |
| 2 | 2,341.067 ms | 85.0 | 1.660 | 260.427 ms | 0 / 0 / 0 |
| 3 | 2,339.533 ms | 85.1 | 1.661 | 257.619 ms | 0 / 0 / 0 |

Median lossless throughput: **85.1 notifications/s**, **1.661 KiB/s**.

### Android lossless Notification

Android uses no artificial pacing. Its fixture follows the Android platform requirement to wait for each successful `onNotificationSent` callback before dispatching the next notification. It does not log or update the UI per burst payload.

| Run | Delivery span | Notifications/s | Payload KiB/s | Android local elapsed | Missing/duplicate/order |
|---:|---:|---:|---:|---:|---|
| 1 | 245.910 ms | 809.2 | 15.805 | 268.723 ms | 0 / 0 / 0 |
| 2 | 242.696 ms | 820.0 | 16.015 | 234.716 ms | 0 / 0 / 0 |
| 3 | 187.536 ms | 1,061.1 | 20.725 | 176.659 ms | 0 / 0 / 0 |

Median lossless throughput: **820.0 notifications/s**, **16.015 KiB/s**. The observed variability is retained rather than collapsed into a universal device capability claim.

After adding the write callback ledger and redeploying both fixtures, one non-destructive Notification regression run per provider also passed with zero missing/duplicate/order violations: QEMU delivered 82.9 notifications/s and Android delivered 1,016.1 notifications/s. These single follow-up values verify control-path non-regression and do not replace the three-run medians above.

## Fixture recovery observation

After the unpaced QEMU burst experiments, stopping the old BLESS service blocked in `server.stop()` until the systemd 90-second stop timeout killed it. Source synchronization had completed, but that interrupted restart did not form a Windows backend pass or failure. The service was then recovered and a fresh ready run started without USB detach.

The guest fixture now bounds `server.stop()` with a 10-second asyncio timeout so a BLESS shutdown stall cannot make future fixture updates appear indefinitely hung. After activating that fix, one explicit BLESS service stop/start completed normally and produced fresh readiness with run ID `2647b66b-48d0-4e13-b199-967dbee9ae6c`; no USB detach was used. This is fixture hardening and recovery evidence, not evidence about Windows native cleanup.

## Commands

```text
# Read + both write modes, QEMU
pixi run just windows::gatt-throughput 35 \
  ffe00000-0000-4000-8000-000000000000 200 20

# Read + both write modes, Android
pixi run just windows::gatt-throughput 35 \
  fff00000-0000-4000-8000-000000000000 200 20

# Lossless Notification, QEMU; auto selects 500 us guest pacing
pixi run just windows::notification-throughput 30 \
  ffe00000-0000-4000-8000-000000000000 qemu 200 auto

# Lossless Notification, Android; callback backpressure, no added pacing
pixi run just windows::notification-throughput 30 \
  fff00000-0000-4000-8000-000000000000 android 200 auto
```

## Remaining limits

- The original three-run medians use 20-byte payloads; follow-up matrices measured negotiated-MTU payloads up to 512 bytes on Android and 244 bytes on the stable QEMU selection.
- Connection interval, PHY, data length, controller buffering, retries, and RF conditions remain provider/session-specific. Android callback evidence confirmed MTU 517 and 2M/2M; QEMU HCI evidence confirmed MTU 517, DLE 251, a 15 ms interval, active 2M/2M when requested, and the UB500's 27-byte × 8 LE ACL host buffer.
- Notification success is proven at the BleHub application event boundary, not at raw PHY.
- No-response writes are now accounted one-for-one at each fixture's application callback for the executed 200-write batches. This does not turn the faster WinRT terminal rate into remote throughput; terminal completion and callback arrival remain separately reported semantics.
- Indication throughput was not measured; strict single-value Indication correctness remains covered by the existing RF smoke.
- These throughput runs do not close callback/native-handle leak, destructive recovery, or final 10-cycle soak gates.
