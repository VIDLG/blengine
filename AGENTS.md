# AGENTS.md

## Purpose

This file defines the working rules for coding agents in the `blehub` project.
Read it before researching, planning, or changing the project.

## Current Project State

BleHub's architecture plan is approved. Phase 0 Stable UniFFI mechanism and Phase 1 unified thin-contract/conformance are complete; Phase 2 Windows-first implementation is active, and the public ABI is not frozen.

- The single reviewed plan is [`.plan/PLAN.md`](.plan/PLAN.md).
- The plan status is `已批准`.
- Phase 1 public contract and shared conformance are complete. Phase 2 starts with the project-managed Android development Peripheral fixture, then proceeds to the production Windows backend.
- Real macOS/iOS production-candidate runtime/package and RF gates are explicitly deferred until before Apple Phase 5 and are not recorded as passed.
- The project-managed Kotlin development fixture under `tests/peripherals/android` has verified build/deploy/advertising and ADB control on an Android phone. The authoritative QEMU Debian + BlueZ/BLESS managed peripheral now boots from pinned assets and exclusively owns the TP-Link UB500 through WinUSB; guest doctor, profile advertising/readiness, Windows scan/connect/public GATT discovery, and one stop/detach/rebuild/start/reattach recovery passed on 2026-08-31. Android and QEMU now use semantically aligned but provider-specific JSON profiles with distinct profile IDs, local names, and advertised/root service UUIDs (`FFF0...` versus `FFE0...`) so concurrent fixtures cannot match the same service filter. The production `WindowsBackend` initialization/scan/filter/advertisement/stop, connect/private uncached GATT service probe/logical disconnect, public uncached service/characteristic/descriptor discovery plus unified database mapping, public characteristic read/write-with-response/write-without-response with deterministic readback, and `PlatformDefault` Notification plus strict Indication enable/value/disable with deterministic Android payload and post-disable stale-route silence RF smokes have passed. Concurrent provider-specific service-filter isolation also passed. One earlier notification attempt selected the concurrently advertising QEMU fixture under the old shared service UUID and remains neither pass nor fail evidence. The same candidate then passed QEMU/BLESS scan/connect/public GATT discovery, deterministic read/write readback, `PlatformDefault` Notification, strict Indication, disable stale-route silence, and capability-false connected RSSI terminal behavior. Detaching the guest-owned UB500 produced a passive `command_id=None` disconnect; reattach, guest doctor, fixture restart, and new readiness also passed. Stopping only the BLESS service did not produce a Windows disconnect and is not backend evidence. The Android phone was not ADB-connected for the later comparison rerun, so earlier Android evidence was not relabeled as a current-candidate rerun. At that point, BlueZ/guest/host reboot recovery, repeated USB fault recovery, WHPX-after-reboot, and soak had not yet been recorded as passed. LightBlue is only a manual fallback.
- On 2026-09-01 the RF-tested candidate was reorganized into private initialization, scan, GATT, mapping, and error modules while keeping `WindowsBackend` as the only public Windows export; 28 Windows tests, 103 workspace tests, Clippy, Ruff, rustfmt, and profile isolation passed. The connected Android fixture then reran the full scan/connect/GATT/read/write/Notification/Indication matrix successfully. Android application-level `BluetoothGattServer.cancelConnection()` produced server-side `status=0 state=0` but no WinRT device-level disconnect at 30/60-second observation windows, including after retained public GATT discovery, so it is not a valid device-loss trigger. Disabling the same phone's Bluetooth radio and waiting for `STATE_OFF` immediately produced the required passive `command_id=None` `Disconnected`; radio enable, `STATE_ON`, and fixture restart recovered successfully. See [`docs/testing/reports/2026-09-01-windows-android-current-candidate-smoke.md`](docs/testing/reports/2026-09-01-windows-android-current-candidate-smoke.md). A subsequent non-RF candidate moved potentially reentrant WinRT teardown outside the event-delivery lock with explicit connection/GATT cleanup gates, extracted `connection.rs`, split GATT into parent/discovery/I/O/test modules, removed unbounded historical caller-ID tombstones, redacted raw Bluetooth addresses from default scan tracing, and hardened standalone Windows HIL subprocess/disposal/recovery reporting. It passed 34 Windows tests, 109 workspace tests, strict Clippy, Ruff, rustfmt, and provider-profile isolation. This hardened candidate then reran and passed the QEMU/BLESS non-destructive scan/connect/public GATT/read/write/`PlatformDefault` Notification/strict Indication matrix, including deterministic payloads and post-disable stale-route silence; see [`docs/testing/reports/2026-09-01-windows-hardened-cleanup-qemu-rf.md`](docs/testing/reports/2026-09-01-windows-hardened-cleanup-qemu-rf.md).
- The latest Phase 2 candidate now passes 39 Windows tests and 114 workspace tests. The OnePlus 11 Android fixture (`3ac5cc09`) reran the non-destructive scan/connect/GATT/read/write/Notification matrix; the combined process was interrupted near strict Indication, so strict Indication was rerun independently and passed rather than inferred. The same candidate retains the complete QEMU/BLESS non-destructive matrix evidence. Standard Cargo staged archives, metadata-enforced source isolation, the offline locked `tests/consumers/windows` artifact-only consumer, and the user-facing `examples/windows-central` strict-Clippy gate all pass. See [`docs/testing/reports/2026-09-01-windows-current-candidate-rf-packaging-summary.md`](docs/testing/reports/2026-09-01-windows-current-candidate-rf-packaging-summary.md). A release-build 20-byte throughput matrix ran three 200-operation batches for Android and QEMU. The latest ledger-accounted rerun measured QEMU median uncached read, callback-confirmed write-with-response, no-response WinRT terminal, and no-response Peripheral callback rates of 17.9, 16.7, 1,263.6, and 91.8 ops/s; Android measured 8.3, 8.3, 1,262.4, and 844.7 ops/s. Every write batch reached 200/200 unique fixture callbacks with zero missing, duplicate, out-of-order, out-of-range, foreign-batch, or malformed values. WinRT no-response terminals remain local/native progress and are not relabeled as remote throughput. Earlier lossless Notification medians were 85.1 ops/s for QEMU and 820.0 ops/s for Android. QEMU HCI evidence observed a 30 ms write-stage connection interval and 54.732 ms median ATT Write Request spacing. One unpaced BLESS burst delivered only 183/200; formal 0.5 ms requested guest-pacing and Android callback-backpressure runs had zero sequence gaps, duplicates, or order violations. These are application-boundary values, not raw PHY throughput; see [`docs/testing/reports/2026-09-01-windows-android-qemu-throughput-matrix.md`](docs/testing/reports/2026-09-01-windows-android-qemu-throughput-matrix.md). The final 10-cycle scan/connect and full active-resource accounting smokes now pass with 40 feature-enabled Windows tests and exact per-cycle logical-root and callback-delegate return to zero. The same candidate then passed one OnePlus 11 radio-disable and one QEMU UB500-detach active-subscription passive-disconnect gate, including `command_id=None` disconnect, pre-dispose connection/GATT/subscription resource release, post-dispose exact-zero accounting, and provider recovery; see [`docs/testing/reports/2026-09-02-windows-passive-disconnect-resource-recovery.md`](docs/testing/reports/2026-09-02-windows-passive-disconnect-resource-recovery.md). Those gates were subsequently executed in the 2026-09-02 follow-up described below; this earlier report remains the evidence for the single-cycle passive-disconnect boundary.
- A throughput follow-up added a nonfatal Windows `ThroughputOptimized` connection-parameter request, 20–512-byte HIL payloads, Android PHY/MTU callback evidence, and a QEMU throughput-only active-PHY control. Android confirmed MTU 517 and 2M/2M; 244-byte no-response writes and Notifications reached about 98 KiB/s (~0.8 Mbit/s) at the Peripheral/BleHub application boundaries without sequence loss. QEMU confirmed MTU 517, DLE 251, 15 ms interval, and active 2M/2M, but 244-byte no-response writes remained about 21.7 KiB/s and Notifications about 1.99 KiB/s. Guest HCI evidence identified the UB500's 27-byte × 8 LE ACL host buffer (v2 buffer-size unsupported) as the long-Notification fragmentation limit. Repeated 512-byte QEMU reads disconnected and are not recorded as passed; the stable QEMU matrix is limited to 20/100/244 bytes.
- The consolidated 2026-09-02 follow-up passed four strict 1,000-packet Android/QEMU Notification/Indication soaks with zero sequence loss, three destructive recovery cycles per provider, and four faults synchronized with entered WinRT read/write/CCCD/value callbacks. The provisioned QEMU image was refreshed; BlueZ restart passed, and a TCG same-process guest reboot completed guest shutdown plus SSH/controller/fixture/doctor/Windows-scan recovery under the then-current controller. QEMU 11.1.0 under WHPX instead paused with `Unexpected VP exit code 4` after guest shutdown, while a fresh WHPX process restored UB500, BlueZ/BLESS, doctor, and Windows scan. Same-process WHPX guest reboot is therefore an accepted fixture-infrastructure limitation; fresh-process restart is the supported WHPX recovery path. Elevation scripts, Zadig execution, and USB rollback were hardened against the reviewed TOCTOU/identity risks and passed static checks, but the UAC-changing operations were not executed. The reboot controller was subsequently strengthened to require a changed Linux boot ID, but that TCG RF assertion has not been rerun. A real Windows host reboot also remains unexecuted. See [`docs/testing/reports/2026-09-02-windows-phase2-fault-soak-reboot-hardening.md`](docs/testing/reports/2026-09-02-windows-phase2-fault-soak-reboot-hardening.md). Phase 2 remains active pending explicit exit review; neither unexecuted boundary is claimed as passed.
- Project-authored research belongs under [`docs/research/`](docs/research/).
- Downloaded platform API evidence belongs under [`docs/platform-api-reference/`](docs/platform-api-reference/).

## Source Of Truth Order

When sources disagree, use this order:

1. Explicit instructions in the current user conversation.
2. This `AGENTS.md`.
3. The reviewed decisions in [`.plan/PLAN.md`](.plan/PLAN.md).
4. Project-authored research in [`docs/research/`](docs/research/).
5. Locally downloaded official platform API references in [`docs/platform-api-reference/`](docs/platform-api-reference/).
6. Relevant source repositories already present in the workspace.
7. External online material only when the required information is not available locally.

A research report records evidence and recommendations; it does not silently override the plan. Proposed architectural changes must be made explicit in the plan and reviewed by the user.

## Local-First Research Policy

### Always inspect local source first

When investigating a library, API, behavior, or architecture:

1. Search the current workspace first.
2. Use `codegraph` against the relevant local repository before using broad text searches.
3. Read the actual implementation and tests rather than relying on remembered behavior, package summaries, or third-party articles.
4. Record whether a conclusion is:
   - verified by local source or official API evidence;
   - inferred from that evidence;
   - still unverified and requires a spike.

Do not browse the web merely to get a newer or more convenient copy of material that is already available locally.

### Relevant local repositories

The workspace currently provides these source trees:

| Project | Workspace path | Use |
|---|---|---|
| BleHub | `D:\projects\blehub` | Current plan, research, tools, and future implementation |
| BoltFFI | `D:\projects\contrib\boltffi` | FFI generator/runtime implementation, generated bindings, packaging code, examples, and tests |
| espkit | `D:\projects\users\hyc\espkit` | Optional BLE domain reference, especially `crates/std/btleplus`; it is not BleHub's architecture baseline |

When investigating BoltFFI behavior, query `D:\projects\contrib\boltffi` directly. Do not infer capabilities only from generated API names or old discussion.

When investigating `btleplus`, follow `espkit/AGENT.md` and its `.planning/` source-of-truth rules. Reuse only domain structures or behavior that fit BleHub's deliberately thin architecture.

### Native platform API evidence

Official platform BLE Central references have already been downloaded to:

```text
docs/platform-api-reference/
├── windows/
├── android/
├── apple/
├── bluez/
├── README.md
└── sources.lock.json
```

Use these local snapshots before looking online:

- Windows: WinRT Bluetooth API documentation.
- Android: Android 36 Bluetooth Framework source and Javadocs. The API is Java-defined even though the backend will be written in Kotlin.
- Apple: CoreBluetooth documentation snapshots for iOS and macOS.
- Linux: BlueZ D-Bus API documentation.

The platform subdirectories are generated evidence. Do not hand-edit files under `windows/`, `android/`, `apple/`, or `bluez/`.

Refresh them only through:

```text
pixi run just docs-update
```

### Missing external source

If required information is absent locally:

- A small, narrowly scoped official page may be queried online only when it is genuinely needed and the local snapshots do not contain the answer.
- Prefer official specifications, vendor documentation, and upstream source over blogs or summaries.
- If a large library or repository is important enough that its implementation must be audited, do not clone or download the large repository automatically. Tell the user exactly which repository, revision/tag, and reason are needed, and ask the user to add it to the workspace.
- After the user provides the repository, initialize/query it with `codegraph` and inspect its source locally.
- Do not introduce a dependency based only on online documentation when its ownership, threading, runtime, FFI, or lifecycle behavior is architecture-critical.

## Project Architecture Guardrails

The current intended architecture is a thin cross-platform BLE Central contract:

- Rust owns the unified DTOs, command API, event API, and thin facade.
- `BleBackend` uses ordinary non-async methods.
- Commands are fire-and-forget:
  - synchronous `Result` means accepted/rejected for dispatch;
  - eventual platform/BLE outcomes are events.
- The caller owns command IDs, timeout policy, cancellation policy, stale/late/duplicate event handling, and business state.
- BleHub core owns no runtime, executor, worker thread, mailbox, timer, event loop, or canonical BLE device/GATT state.
- Backends own unavoidable platform handles, callback correlation, and serialized native GATT operations.
- In v0.1, native backends push events through exported `push_backend_event(BackendEventIngressToken, BleEvent)` and a process-local weak registry; migrate to a direct Rust-provided ingress handle when BoltFFI supports that foreign-trait parameter shape.
- Downstream supplies `BleEventSink` and decides whether events go to a channel, actor, UI loop, or runtime.

Current platform direction:

| Priority | Platform | Backend direction |
|---:|---|---|
| 1 | Windows | Rust + `windows-rs` |
| 2 | Android | Kotlin + Android Framework BLE + BoltFFI |
| 3 | iOS/macOS | Swift + CoreBluetooth + BoltFFI |
| 4 | Linux | Rust + `dbus-rs` + BlueZ D-Bus |

The Kotlin/JVM foreign trait object plus token ingress workaround has partial end-to-end Phase 0 evidence. Swift runtime/ARC, remaining lifecycle counters, fault isolation, packaging, and performance gates still require validation before the public ABI is frozen; the ideal direct ingress-handle shape remains blocked by BoltFFI #869.

## Dependency And Tooling Rules

- Rust dependencies are managed by Cargo once the Rust workspace is approved and created.
- External development tools are managed by Pixi.
- Project recipes are defined in `justfile`.
- `just` itself is managed by Pixi.
- Run project recipes through Pixi:

```text
pixi run just <recipe>
```

Do not assume globally installed Python, Ruff, Just, RTK, sccache, or other tools. Test recipes route verbose Cargo/Gradle output through Pixi-managed RTK; RTK is an output filter only and must preserve command selection, failure detail, and exit status. Pixi-managed sccache wraps ordinary Rust compilation and eligible C/C++ glue with Cargo incremental disabled to avoid duplicate caches; it does not reduce required final artifacts and does not cache Swift/Kotlin compilation. BoltFFI metadata generation must use a dedicated Cargo target and bypass sccache because its cfg/environment-driven macro output is not safely distinguished from ordinary Rust artifacts by the cache key; the CLI itself may remain in the ordinary cached target.

Machine-local toolchain roots resolve in this order: `BLEHUB_<TOOL>_ROOT`, ignored `blehub.local.toml`, then `~/.cache/blehub/toolchains/<tool>`. Copy `blehub.local.example.toml` for stable developer-machine paths and use environment variables in CI. Keep only paths in local config; pin versions, URLs, hashes, repositories, and license policy in tracked scripts and locks. Use `pixi run just toolchain-paths` to inspect effective values. Bootstrap/install recipes must be explicit; ordinary build and test recipes must not silently install Android SDK/NDK, Swift, or accept licenses.

Current recipes include:

```text
pixi run just docs-update
pixi run just docs-check-mermaid
pixi run just fmt
pixi run just lint
```

Prefer mature ecosystem libraries where they clearly reduce risk or maintenance. “Thin core” means limited responsibility, not a ban on dependencies. Lightweight boilerplate-reduction crates such as `derive_more`, `delegate`, or `getset` are allowed when they remove meaningful repeated code without obscuring ownership, FFI, validation, or lifecycle behavior. Do not add accessor/delegation macros merely to replace one or two obvious lines, and verify any attribute macro used in a BoltFFI-scanned crate against binding generation. Do not add a runtime or heavyweight framework that changes the caller-owned execution model.

## Documentation Rules

- Write and edit all project-authored documentation as UTF-8.
- Keep one reviewable architecture plan in [`.plan/PLAN.md`](.plan/PLAN.md).
- Put durable investigations and decision evidence in [`docs/research/`](docs/research/).
- Keep generated official snapshots separate from authored interpretation.
- When making factual claims, link to the relevant local source file or snapshot where practical.
- Distinguish platform guarantees from BleHub policy and from conclusions that still need runtime validation.
- Do not claim that a callback, completion, disconnect, write, shutdown, or cancellation has stronger semantics than the native platform provides.
- Future user-facing docs should be derived from the approved plan and verified implementation, not copied blindly from an unapproved draft.

## Change Discipline

- Inspect relevant files and existing user work before editing.
- Make minimal, focused changes.
- Do not modify vendored/generated code unless the task explicitly requires it.
- Do not broaden planning into implementation without approval.
- Do not commit or create branches unless explicitly requested.
- This directory may not yet be a Git repository; do not assume `git status` or `git diff` is available.

## Validation

Do not add tests for trivial field assignment, obvious constructors/getters, derived trait behavior, or direct delegation that has no project-specific invariant. Prefer tests that protect FFI/wire compatibility, validation boundaries, lifecycle and rollback semantics, concurrency linearization, platform mapping, or previously observed regressions. Combine closely related boundary cases when that improves signal and readability; test count is not a goal.

For documentation/tooling changes, run the narrowest applicable checks through Pixi. At minimum:

```text
pixi run just fmt
pixi run just lint
```

For authored Markdown, also verify:

- Mermaid fences with `pixi run just docs-check-mermaid` when diagrams are present;
- UTF-8 decoding;
- balanced fenced code blocks;
- no trailing whitespace;
- no merge-conflict markers.

For future Rust/platform implementation, add platform-specific build, unit-test, generated-binding, packaging-consumer, and lifecycle stress recipes to `justfile` as each phase begins.
