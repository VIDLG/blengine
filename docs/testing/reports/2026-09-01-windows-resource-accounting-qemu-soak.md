# Windows callback/native-resource accounting + QEMU/BLESS 10-cycle soak — 2026-09-01

## Result

**PASS for the executed 10-cycle scan/connect soak and 10-cycle full active-resource lifecycle soak.**

Every cycle established a non-vacuous Windows resource graph before disposal and then required all backend logical roots and all instrumented WinRT callback delegates to return exactly to zero after `BleHub::dispose()`. The same zero-state assertion passed again after idempotent disposal. No callback delegate or backend-owned logical native root remained live at any cycle boundary.

This closes the Phase 2 10-cycle scan/connect and callback/native-resource accounting gates for the executed normal-disposal paths. It does not claim destructive passive-disconnect, repeated USB/radio fault recovery, BlueZ/guest/host reboot recovery, or long notification/indication soak evidence.

## Candidate and environment identity

| Field | Value |
|---|---|
| Execution date | 2026-09-01 |
| Base revision | `fa97ad9be8a44d4bb1226c8830f79086ca9b5236`; the candidate and this report include uncommitted Phase 2 working-tree changes |
| Public contract | version `5`; public ABI not frozen |
| Windows DUT | production `WindowsBackend`, native Windows Cargo `dev` build |
| Windows unit baseline | 39 default-feature tests; 40 tests with `resource-accounting` enabled |
| Workspace baseline | 114 tests |
| Central environment | same exclusive Windows 11 runner and MediaTek Central controller recorded in the hardened-cleanup QEMU report |
| Peripheral VM | QEMU `11.1.0`; Debian 13 guest; Linux `6.12.105+deb13-amd64` |
| Peripheral controller | TP-Link UB500 `2357:0604`, owned exclusively by the QEMU guest through WinUSB passthrough |
| Peripheral profile | `blehub-qemu-basic-gatt-v1`; service `ffe00000-0000-4000-8000-000000000000` |
| Fixture run ID | `bd69b485-39f3-4871-981e-c50f6eebc7ef` |
| VM image fingerprint | `e294cad9a15bea954be666aaae9c4cc0017468b93d454ffb244023609eec5908` |
| Fixture environment fingerprint | `f53cd0aa2bd9ec2396013cc0b3097d21126a259f70d7de1aac0c7b374c641d50` |
| Fixture source fingerprint | `fd22d121300a1b213f75835cbb6900786c2b505acbf0a3bc1ebbdfdc00450db3` |

Before RF execution, `peripheral::vm-doctor`, `peripheral::vm-status`, and `peripheral::vm-fixture-status` passed. The guest reported one unblocked Bluetooth controller, active BlueZ, GATT/LE advertising managers, and fixture state `ready`.

## Accounting design

The production crate now has a non-default `resource-accounting` feature used only by the standalone Windows HIL crate. It does not change BleHub DTOs, `BleBackend`, `BleEvent`, UniFFI bindings, the public command model, or production threading/runtime behavior.

The accounting model has two independent views:

1. **Delegate lifetime ledger** — each generated WinRT callback closure retains an RAII guard. The guard decrements only when the callback delegate is actually dropped; calling `Cancel()` or `Remove*()` alone does not decrement it.
2. **Backend logical-resource snapshot** — the HIL retains the concrete `Arc<WindowsBackend>` and observes ingress ownership, pending initialization operations, adapter/radio roots, radio event token, watcher root, connection roots, and process-adapter lease ownership.

The probe owns only an `Arc` of atomic counters. It does not retain backend state, WinRT objects, the event ingress, or the downstream sink and therefore cannot create a cleanup cycle.

The delegate ledger covers all 16 production callback families:

- ten async completion delegates: adapter lookup, radio lookup, device lookup, connection GATT probe, service discovery, characteristic discovery, descriptor discovery, characteristic read, characteristic write, and CCCD write;
- six event delegates: `Radio.StateChanged`, watcher `Received`, watcher `Stopped`, device `ConnectionStatusChanged`, device `GattServicesChanged`, and characteristic `ValueChanged`.

Normal builds compile the same guard calls to a zero-sized no-op path. The hidden `WindowsResourceProbe`, callback snapshot, logical snapshot, and `WindowsBackend::new_accounted()` APIs exist only when the HIL feature is enabled.

## Executed commands

Non-RF validation:

```text
pixi run just windows::check
pixi run just windows::test
pixi run cargo test -p blehub-windows --features resource-accounting --offline
pixi run just lint
pixi run just verify
pixi run just windows::consumer
pixi run just windows::example
```

RF readiness and soak:

```text
pixi run just peripheral::vm-doctor
pixi run just peripheral::vm-status
pixi run just peripheral::vm-fixture-status
pixi run just windows::resource-lifecycle-smoke 30 ffe00000-0000-4000-8000-000000000000 10
pixi run just windows::scan-connect-soak 30 ffe00000-0000-4000-8000-000000000000 10
```

The project gate and both slow HIL defaults are 10 cycles, matching the explicit project decision to keep slow RF tests bounded.

## Active graph assertions

### Full resource lifecycle

Before each disposal, the runner required all six long-lived event delegate classes to be nonzero:

- radio state changed;
- advertisement received;
- watcher stopped;
- connection status changed;
- GATT services changed;
- characteristic value changed.

It also required one ingress root, adapter root, radio root, radio token, scan root, connection root, and adapter lease. The scenario therefore exercised initialization, an active watcher, a connected device, public GATT discovery, an enabled subscription, and disposal from that active state rather than a vacuous inert backend.

### Scan/connect soak

Before each disposal, the runner required the initialized/connected logical graph plus radio-state, connection-status, and GATT-services-changed delegates. The requested scan had already stopped, so watcher delegate counts were expected to be zero before connected-state disposal.

### Disposal boundary

For both runners, every cycle required:

- first `dispose()` succeeds;
- all logical roots become exactly zero;
- all 16 delegate counters become exactly zero;
- a second idempotent `dispose()` succeeds;
- the zero snapshots remain zero after the second call;
- the next cycle reacquires the process adapter lease and reconstructs a fresh graph.

A bounded five-second wait allows WinRT to release callback wrappers after successful revocation. No cycle reached that deadline.

## Quantitative results

| Runner | Planned | Completed | First failure | Exact accounting result |
|---|---:|---:|---|---|
| Full active-resource lifecycle | 10 | 10 | none | logical roots and all callback delegates returned to zero after every first and second dispose |
| Scan/connect soak | 10 | 10 | none | logical roots and all callback delegates returned to zero after every first and second dispose |

Process telemetry is secondary evidence only; process handle count alone does not prove COM/WinRT object release, and allocator/WinRT caches prevent exact memory-return assertions.

| Runner | Process handles | Private bytes | Working-set bytes |
|---|---|---|---|
| Full active-resource lifecycle | cold `97` → cycle 1 `213` → cycle 3 settled `225` → final `224`; settled max `225` | `843,776` → `3,133,440` → `3,698,688` → `3,747,840` | `5,296,128` → `15,810,560` → `16,789,504` → `16,900,096` |
| Scan/connect soak | cold `97` → cycle 1 `218` → cycle 3 settled `223` → final `228`; settled max `229` | `839,680` → `2,899,968` → `3,305,472` → `3,510,272` | `5,292,032` → `15,634,432` → `16,044,032` → `16,261,120` |

The full active-resource run plateaued after WinRT/Bluetooth cold initialization: final handle count was one below its settled value and settled maximum. The scan/connect process metric increased by five handles between cycle 3 and cycle 10, while the backend-owned logical roots and every authored delegate counter still returned exactly to zero on every cycle. This small process-wide trend is recorded rather than relabeled as backend ownership; future longer stress may use it as a comparison baseline.

## Packaging lock workflow

A changed same-version `blehub-windows 0.0.0` staged archive legitimately changes its Cargo checksum. Cargo refuses to update that checksum in place while reading the old lock. The project now provides an explicit workflow:

```text
pixi run just windows::consumer-refresh-lock
```

It stages the final crates, removes and regenerates only the standalone consumer lock from the offline staged directory source, and reruns metadata source-isolation. Normal `windows::consumer` verification remains locked and does not silently rewrite the lock.

## Remaining Phase 2 work

The following are still open and are not inferred from this report:

- latest-candidate destructive Android radio and QEMU USB passive-disconnect tests;
- active-subscription or in-flight GATT disconnect races;
- repeated USB/radio fault recovery;
- BlueZ daemon, guest, Windows host, and WHPX-after-reboot recovery;
- long notification/indication soak;
- elevated Windows tooling TOCTOU hardening listed separately in the Plan.
