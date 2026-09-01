# Windows current candidate RF and packaging summary — 2026-09-01

## Result

**PASS for the executed Windows non-RF, staged packaging, artifact-only consumer, Android RF, and QEMU/BLESS non-destructive RF selections.**

This report records one current Phase 2 candidate after the Windows cleanup/module hardening, HIL runner hardening, staged Cargo packaging, source-isolation assertion, and user-facing Rust example work. It does not declare Phase 2 complete and does not relabel any unexecuted destructive, recovery, soak, or leak gate as passed.

## Build and candidate identity

| Field | Value |
|---|---|
| Execution date | 2026-09-01 |
| Base revision | `fa97ad9be8a44d4bb1226c8830f79086ca9b5236`; the candidate and this report include uncommitted Phase 2 working-tree changes |
| Public contract | version `5`; public ABI not frozen |
| Windows DUT | production `WindowsBackend`, native Windows Cargo `dev` build |
| Windows public surface | `WindowsBackend` remains the only public Windows backend export |
| Windows unit baseline | 39 tests passed |
| Workspace baseline | 114 tests passed |
| Recipe layout | Rust-style Just module tree under `just/windows`, `just/peripheral`, `just/test`, `just/binding`, and `just/spike`; existing public recipe names are unchanged |

The production backend remains synchronous and runtime-free. The caller owns channels, command/session IDs, deadlines, timeout policy, event processing, and stale/duplicate/orphan handling.

## Non-RF and packaging gates

The following commands passed for this candidate:

```text
pixi run just windows::test
pixi run just test::all
pixi run just windows::package
pixi run just windows::consumer
pixi run just windows::example
```

Results:

| Gate | Result | Evidence |
|---|---|---|
| Windows unit tests | PASS | 39 tests across two suites |
| Workspace tests | PASS | 114 tests across twelve suites |
| Standard Cargo package | PASS | Cargo produced `blehub-0.0.0.crate` and `blehub-windows-0.0.0.crate`; the packaged Windows manifest pins `blehub = "=0.0.0"` and contains no path fallback |
| Cargo directory source | PASS | local and third-party packaged sources were staged under `.work/staged/windows/vendor` with Cargo checksum documents |
| Metadata source isolation | PASS | the standalone consumer workspace root was `tests/consumers/windows`; every dependency manifest resolved under the staged vendor directory; only the consumer itself was a path/workspace package |
| Artifact-only consumer | PASS | offline locked Clippy and execution loaded both BleHub crates from the staged directory source, exercised ingress delivery/close, idempotent disposal, `WindowsBackend` assembly/capabilities, and backend process-lease release |
| User-facing Windows example | PASS | `examples/windows-central` passed strict Clippy; it demonstrates a caller-owned `std::sync::mpsc` channel, service-filtered scan, caller deadlines/event loop, requested stop, and explicit idempotent disposal without fixture orchestration |

Staged archive identities:

| Package | SHA-256 |
|---|---|
| `blehub-0.0.0.crate` | `3777003edc29020cd1ffc049b2c97b72cc660fbae6f7b66269c761648844e677` |
| `blehub-windows-0.0.0.crate` | `f28edbfc59278c330a4015df87c2fcceb7aa1b4309c1af64f4450aa383b336ad` |

The clean consumer is deliberately separate from the user example and the RF HIL runners:

- `tests/consumers/windows` proves packaged artifact assembly without workspace/path source fallback;
- `examples/windows-central` teaches the Rust downstream integration model and may use repository path dependencies;
- `tests/hil/windows` owns project fixture assertions and RF orchestration.

## QEMU/BLESS non-destructive RF matrix

The current candidate passed the provider-specific `blehub-qemu-basic-gatt-v1` matrix using service `ffe00000-0000-4000-8000-000000000000`:

| Case | Result |
|---|---|
| initialize, service-filtered scan, advertisement, requested stop | PASS |
| connect and logical disconnect | PASS |
| public uncached service/characteristic/descriptor discovery | PASS |
| deterministic characteristic read | PASS |
| write with response plus exact readback | PASS |
| write without response plus exact readback | PASS |
| `PlatformDefault` Notification enable and exact `DE AD BE EF` value | PASS |
| strict Indication enable and exact `DE AD BE EF` value | PASS |
| disable and two-second post-disable stale-route silence | PASS |
| capability-false connected RSSI terminal `NotSupported` behavior | PASS |

The detailed QEMU environment, controller, VM, fixture fingerprints, and case evidence are recorded in [`2026-09-01-windows-hardened-cleanup-qemu-rf.md`](2026-09-01-windows-hardened-cleanup-qemu-rf.md). HIL success output is intentionally concise; recent event traces are retained and printed on failure rather than logging every successful callback.

## Android non-destructive RF matrix

The connected physical fixture was a OnePlus 11 selected as serial `3ac5cc09`. Its status was:

```text
Status: ADVERTISING
profile=blehub-android-basic-gatt-v1
service=fff00000-0000-4000-8000-000000000000
```

The candidate passed:

| Case | Result |
|---|---|
| initialize, service-filtered scan, advertisement, requested stop | PASS |
| connect and logical disconnect | PASS |
| public uncached GATT discovery and fixture mapping | PASS |
| deterministic read | PASS |
| both write modes with deterministic readback | PASS |
| `PlatformDefault` Notification, exact payload, disable, stale-route silence | PASS |
| strict Indication, exact payload, disable, stale-route silence | PASS |

The combined Android command sequence was interrupted by the host orchestration near the strict Indication case. No strict-Indication pass was inferred from that interruption. Strict Indication was rerun independently against the same advertising fixture and completed successfully, including actual-mode verification, exact payload delivery, disable, and post-disable stale-route silence.

This Android run was non-destructive. It did not disable the phone Bluetooth radio and does not replace the earlier, separately recorded device-level passive-disconnect evidence.

## Provider isolation

Android and QEMU use semantically aligned GATT roles but distinct RF identities:

| Provider | Profile | Advertised/root service |
|---|---|---|
| Android | `blehub-android-basic-gatt-v1` | `fff00000-0000-4000-8000-000000000000` |
| QEMU/BLESS | `blehub-qemu-basic-gatt-v1` | `ffe00000-0000-4000-8000-000000000000` |

`pixi run just peripheral::profiles-check` passed. The providers cannot satisfy the same required-service filter, so discovery order or RSSI is not used to select a fixture.

## Not executed or not claimed

The following Phase 2 gates remain open:

- callback-token and native-handle resource accounting with no-known-leak evidence;
- 10-cycle QEMU/BLESS scan/connect soak;
- long notification/indication soak and high-frequency FIFO RF behavior;
- QEMU USB detach passive disconnect for this latest candidate;
- Android Bluetooth radio-disable passive disconnect for this latest candidate;
- passive disconnect during an active subscription or in-flight GATT/value callback;
- repeated USB and Android radio fault/recovery cycles;
- BlueZ daemon, guest reboot, and Windows host reboot recovery;
- WHPX-after-reboot verification;
- forced HIL fixture-subprocess timeout/termination runtime validation;
- elevated Windows tooling TOCTOU hardening;
- release-candidate Windows HIL report.

Destructive Android radio or QEMU USB fault tests require fresh explicit authorization before execution. Until the remaining exit gates pass, Phase 2 remains active and the production ABI remains unfrozen.
