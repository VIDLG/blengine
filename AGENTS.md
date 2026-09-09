# AGENTS.md

## Purpose

This file defines the working rules for coding agents in the `blehub` project.
Read it before researching, planning, or changing the project.

## Current Project State

BleHub's architecture plan is approved. Phase 0 Stable UniFFI mechanism, Phase 1 unified thin-contract/conformance, and Phase 2 Windows-first implementation are complete.

**2026-09-04 maintained Peripheral topology correction:** macOS native BLESS, the `apple-bless` environment, and Mac QEMU + BLESS are retired and must not be used by production or long-lived test paths. Apple Central RF uses the Android Peripheral now and a standalone nRF52840 DK when available. Windows retains QEMU Linux + BlueZ + BLESS; its USB controller is configured only through ignored local configuration or environment variables, with no tracked UB500/Zadig/WinUSB identity. The nRF52840 Dongle will be integrated after arrival, using mutually exclusive HCI firmware for QEMU or standalone Peripheral firmware for direct Windows RF. Historical reports may retain old UB500/macOS-BLESS evidence, but it is not current architecture evidence.

- The single reviewed plan is [`.plan/PLAN.md`](.plan/PLAN.md).
- The plan status is `已批准`.
- Phase 1 public contract and shared conformance are complete. Phase 2 completed the project-managed Android development Peripheral fixture, production Windows backend, managed QEMU/BLESS fixture, artifact-only Windows consumer, lifecycle accounting, fault recovery, and exit review. See [`docs/testing/reports/2026-09-02-windows-phase2-exit-review.md`](docs/testing/reports/2026-09-02-windows-phase2-exit-review.md).
- The user-approved implementation order after Phase 2 is now Phase 3 unified Apple backend (macOS first, then independently validated iOS), Phase 4 Android backend, and Phase 5 Windows + Apple + Android v0.1/production ABI freeze. The current active lane is Phase 3 Apple; Android Central implementation waits for Phase 3 exit, while the Android Peripheral fixture remains available for Apple RF development.
- All maintained Apple Swift runtime, package, backend, and HIL gates run on the project real Mac mini through local Pixi + Just. There is no maintained Windows desktop Swift lane, Windows-managed Swift toolchain/root/bootstrap, or GitHub-hosted macOS workflow. Earlier Windows Swift and GitHub Actions runs remain historical, non-maintained evidence only. Phase 5 requires macOS/iOS Swift coverage from the Mac mini, not a Windows Swift supplement.
- Repository ownership is now explicit: `crates/blehub` is the canonical definition owner and exports the production `BleHub` object, foreign `BleBackend`, foreign `BleEventSink`, direct ingress, commands, events, and errors; thin `crates/blehub-uniffi` owns the only production Stable UniFFI final native image, bindgen entry, and production package staging. Production backends live under `platforms/`; reusable Rust test support lives under `tests/support/blehub-testkit`; `tests/ffi/runtime` contains Swift and Android Kotlin tests that implement test backend/sink objects directly and exercise the production ABI; Apple HIL consumes the production package, uses a direct Swift sink, and shares only a pure-Swift helper with the macOS runtime probe; and staged artifact-only consumers remain under `tests/consumers`. There is no second UniFFI native image or test component. The post-migration current candidate has now passed the macOS single-image Apple package/symbol gate, direct Swift production runtime gate, Apple deterministic and generic-build gates, iPhone HIL project generation/unsigned generic build, Android production AAR assembly/publish and macOS NDK native-slice builds, plus workspace Cargo tests and lint. The current `runtime-macos` invocation completed initialization but its terminal ad-hoc snapshot was adapter `Unavailable` with authorization `NotDetermined`; it was not a `PoweredOn + Authorized` or RF result. Physical Android ART remained unrun after two locked-phone APK installation timeouts, and no migration RF gate ran; see [`docs/testing/reports/2026-09-03-single-image-production-abi-migration.md`](docs/testing/reports/2026-09-03-single-image-production-abi-migration.md). The public ABI remains unfrozen and Phase 5 still requires the complete same-candidate rerun. No production or long-lived test path may depend on `spikes/`; the superseded production-candidate spike is archived under `spikes/archive/`.
- On 2026-09-02 the then-current unfrozen candidate passed the real-macOS Stable UniFFI Swift runtime gate plus a three-slice XCFramework/SPM package gate covering arm64 iOS device, arm64+x86_64 iOS simulator, and arm64+x86_64 macOS, including an isolated macOS package consumer and unsigned generic Xcode builds for iOS simulator/device. See [`docs/testing/reports/2026-09-02-apple-production-candidate-bringup.md`](docs/testing/reports/2026-09-02-apple-production-candidate-bringup.md). The production Swift/CoreBluetooth initialization, scan, connection-lifecycle, aggregate GATT-discovery, characteristic read/write, and Notification/Indication slices now implement single-owner lifecycle, serial-queue confinement, initialization state/authorization mapping, ingress delivery, one active scan, CoreBluetooth local start/stop completion, advertisement/predicate mapping, backend-private peripheral identity reuse, one lifecycle per peripheral, exact native-object plus generation correlation, connect/failure/passive-disconnect mapping, logical disconnect, callback-first route teardown, depth-first service/characteristic/descriptor discovery, duplicate-UUID instance preservation, all-or-error database publication, pending-discovery quarantine after service invalidation, one shared per-connection I/O/subscription FIFO, exact read/write/value/state callback routing, no-response backpressure/local-submission semantics, separate platform write limits, CoreBluetooth mode-selection policy, native unsubscribe-before-rediscovery, and bounded explicit fail-closed late-callback/value-route safety faults, a backend-owned serial queue, local scan filtering before peripheral retention, a bounded GATT FIFO, conservative CoreBluetooth error mapping, and exact RSSI callback correlation across the documented macOS/iOS delegate paths; rediscovery now distinguishes ready, queued native unsubscribe, and unsafe states, fails closed above 1,024 active subscriptions rather than starting discovery, and aggregate discovery enforces private pre-draft FFI/allocation guards of 128 services, 1,024 total characteristics, and 2,048 total descriptors while preserving the previously committed database on failure; wire-valid writes above the current CoreBluetooth maximum terminal as `Gatt`, not capability-level `NotSupported`; 71 deterministic macOS tests and unsigned generic iOS simulator/device builds pass. See [`docs/testing/reports/2026-09-03-apple-gatt-discovery-slice.md`](docs/testing/reports/2026-09-03-apple-gatt-discovery-slice.md), [`docs/testing/reports/2026-09-03-apple-gatt-io-slice.md`](docs/testing/reports/2026-09-03-apple-gatt-io-slice.md), and [`docs/testing/reports/2026-09-03-apple-notification-indication-slice.md`](docs/testing/reports/2026-09-03-apple-notification-indication-slice.md), and [`docs/testing/reports/2026-09-03-apple-architecture-hardening-rssi-slice.md`](docs/testing/reports/2026-09-03-apple-architecture-hardening-rssi-slice.md) for deterministic GATT/RSSI results and the completed characteristic I/O RF evidence. The current candidate now also passes the project-managed real macOS-to-OnePlus 11 `PlatformDefault` Notification RF workflow: actual mode `Notification`, exact `DE AD BE EF` value, enable/disable domain-before-terminal ordering, expected fixture-side rejection plus two-second host silence after disable, logical disconnect, and post-disconnect silence. This is not strict Indication, passive-disconnect recovery, RSSI RF, or iPhone evidence. A bundled macOS app also passed real CoreBluetooth construction, delegate ingress, and initialization event ordering. Its first terminal-launched run reported adapter `Unavailable` and authorization `NotDetermined`; after launching a visible Cocoa app through LaunchServices, the system Bluetooth prompt and TCC registration completed and the app passed a `PoweredOn + Authorized` runtime gate. The source-tree probe is ad-hoc signed, so rebuilding/re-signing may require authorization again; visible Apple Development identities were not usable by the current agent `codesign` process (`errSecInternalComponent`). This is not a development-signing pass. The OnePlus 11 Android fixture (`3ac5cc09`) subsequently passed real macOS CoreBluetooth provider-filtered scan/start/advertisement/stop/post-stop-silence, connect/`Connecting`/`Connected`, aggregate GATT service/characteristic/descriptor discovery with fixture database/CCCD validation, initial characteristic read (`00`), write-with-response (`10 20 30`) and exact readback, write-without-response (`AB CD`) local submission and exact delayed readback, commands 6 through 10 exactly-once/domain-before-terminal ledger validation, logical-disconnect, and post-disconnect-silence RF gates on the built-in `BCM_4388C2` controller (latest I/O run `RSSI=-50`, connectable). The no-response terminal remains local CoreBluetooth submission only; delayed readback is separate fixture-application evidence. A preceding run with `IOConsoleLocked=Yes` timed out before initialization and never entered RF; the passing rerun used an unlocked graphical session and bounded `caffeinate` assertion. See [`docs/testing/reports/2026-09-02-apple-initialization-slice.md`](docs/testing/reports/2026-09-02-apple-initialization-slice.md), [`docs/testing/reports/2026-09-02-apple-scan-slice-macos-runtime.md`](docs/testing/reports/2026-09-02-apple-scan-slice-macos-runtime.md), [`docs/testing/reports/2026-09-02-apple-android-scan-rf-connection-slice.md`](docs/testing/reports/2026-09-02-apple-android-scan-rf-connection-slice.md), and [`docs/testing/reports/2026-09-03-apple-android-gatt-discovery-rf.md`](docs/testing/reports/2026-09-03-apple-android-gatt-discovery-rf.md). The Mac mini exposes UB500 `2357:0604` at the USB layer but not as an `IOBluetoothHCIController`; CoreBluetooth still uses the built-in controller, and the Linux/BlueZ BLESS fixture cannot run directly against that dongle on macOS. A separate Debian arm64/HVF macOS fixture path now passes image provision, guest boot, `linux-aarch64` environment install, and source/profile sync. QEMU 11.1.1/libusb can exclusively claim the UB500 and Linux binds `btusb`, but Realtek HCI command `0xfc61` times out under both xHCI and a temporary OHCI diagnostic, leaving zero BlueZ controllers; BLESS advertising and Apple BLESS scan RF therefore did not run and this remains a fixture-infrastructure limitation, not an Apple backend failure. See [`docs/testing/reports/2026-09-02-apple-bless-qemu-macos-bringup.md`](docs/testing/reports/2026-09-02-apple-bless-qemu-macos-bringup.md). A connected iPhone 14 is visible and its production HIL passed development signing, installation, launch, and CoreBluetooth initialization runtime on 2026-09-04; iPhone RF remains unrun. See [`docs/testing/reports/2026-09-04-apple-ios-production-runtime.md`](docs/testing/reports/2026-09-04-apple-ios-production-runtime.md). Applicable Apple gates must rerun after ABI freeze; macOS evidence never proves iOS. On 2026-09-03 the Apple fixture topology was corrected after inspecting the official BLESS `0.3.0` wheel: upstream BLESS has a native CoreBluetooth server backend even though the existing managed fixture lock was Linux-only. A root `apple-bless` Pixi environment, native process controller, and provider-isolated `blehub-macos-bless-basic-gatt-v1` profile (`FFD0...`) now pass non-RF backend/profile checks. Native macOS BLESS uses the system-selected built-in controller and is intended as a second Peripheral for iPhone Central tests; it never uses UB500 and does not replace Windows-hosted Linux/BlueZ/BLESS + UB500 fault/soak evidence. No native macOS BLESS advertising or iPhone RF pass is yet claimed. The Apple Silicon arm64/HVF QEMU control path is retained for command/readiness parity with Windows. A physical UB500 cold-plug recheck fixed a Unix QEMU child-lifetime bug by starting it in a new session and proved VM/QMP/SSH persistence, but default passthrough again reached `btusb` then failed Realtek `0xfc61` with zero BlueZ controllers. A bounded `guest-resets-all` experiment made guest USB enumeration worse and was removed; attach timeout now performs failure-atomic QMP rollback. See [`docs/testing/reports/2026-09-03-apple-native-macos-bless-design.md`](docs/testing/reports/2026-09-03-apple-native-macos-bless-design.md) and [`docs/testing/reports/2026-09-03-apple-qemu-ub500-cold-plug-recheck.md`](docs/testing/reports/2026-09-03-apple-qemu-ub500-cold-plug-recheck.md).
- The iPhone HIL uses the staged production package, a direct Swift `BleEventSink`, a shared pure-Swift helper also used by the macOS runtime probe, tracked XcodeGen YAML (`tests/hil/apple/ios/project.yml`), and official `xcodebuild`/`xcrun devicectl` JSON interfaces through `tools/apple/test_ios_runtime.py`. Its project generation and unsigned generic iOS build pass. XcodeGen 2.46.0 is a shared Homebrew tool installed only by the explicit `pixi run -e apple-ci just apple install-tools` recipe; ordinary workflows verify it but never install or update it. The connected iPhone 14 is visible, Developer Mode is enabled, the unique non-Personal development team is selected, and Swift/SPM compilation plus automatic provisioning reached final code signing. The device app is now recorded as signed, installed, launched, and runtime-passed for production CoreBluetooth initialization; the explicit human-only login-keychain and Apple signing-tool key-partition workflow was required. Unlocking is an explicit human-only `pixi run -e apple-ci just apple unlock-login-keychain` step; ordinary tests never request or accept a password. Test-layer responsibilities and the real Hub/Stable-UniFFI/Swift/CoreBluetooth HIL path are documented in [`docs/research/testing-layer-responsibilities.md`](docs/research/testing-layer-responsibilities.md).
- The project-managed Kotlin development fixture under `tests/peripherals/android` has verified build/deploy/advertising and ADB control on an Android phone. The authoritative QEMU Debian + BlueZ/BLESS managed peripheral now boots from pinned assets and exclusively owns the TP-Link UB500 through WinUSB; guest doctor, profile advertising/readiness, Windows scan/connect/public GATT discovery, and one stop/detach/rebuild/start/reattach recovery passed on 2026-08-31. Android and QEMU now use semantically aligned but provider-specific JSON profiles with distinct profile IDs, local names, and advertised/root service UUIDs (`FFF0...` versus `FFE0...`) so concurrent fixtures cannot match the same service filter. The production `WindowsBackend` initialization/scan/filter/advertisement/stop, connect/private uncached GATT service probe/logical disconnect, public uncached service/characteristic/descriptor discovery plus unified database mapping, public characteristic read/write-with-response/write-without-response with deterministic readback, and `PlatformDefault` Notification plus strict Indication enable/value/disable with deterministic Android payload and post-disable stale-route silence RF smokes have passed. Concurrent provider-specific service-filter isolation also passed. One earlier notification attempt selected the concurrently advertising QEMU fixture under the old shared service UUID and remains neither pass nor fail evidence. The same candidate then passed QEMU/BLESS scan/connect/public GATT discovery, deterministic read/write readback, `PlatformDefault` Notification, strict Indication, disable stale-route silence, and capability-false connected RSSI terminal behavior. Detaching the guest-owned UB500 produced a passive `command_id=None` disconnect; reattach, guest doctor, fixture restart, and new readiness also passed. Stopping only the BLESS service did not produce a Windows disconnect and is not backend evidence. The Android phone was not ADB-connected for the later comparison rerun, so earlier Android evidence was not relabeled as a current-candidate rerun. At that point, BlueZ/guest/host reboot recovery, repeated USB fault recovery, WHPX-after-reboot, and soak had not yet been recorded as passed. LightBlue is only a manual fallback.
- On 2026-09-01 the RF-tested candidate was reorganized into private initialization, scan, GATT, mapping, and error modules while keeping `WindowsBackend` as the only public Windows export; 28 Windows tests, 103 workspace tests, Clippy, Ruff, rustfmt, and profile isolation passed. The connected Android fixture then reran the full scan/connect/GATT/read/write/Notification/Indication matrix successfully. Android application-level `BluetoothGattServer.cancelConnection()` produced server-side `status=0 state=0` but no WinRT device-level disconnect at 30/60-second observation windows, including after retained public GATT discovery, so it is not a valid device-loss trigger. Disabling the same phone's Bluetooth radio and waiting for `STATE_OFF` immediately produced the required passive `command_id=None` `Disconnected`; radio enable, `STATE_ON`, and fixture restart recovered successfully. See [`docs/testing/reports/2026-09-01-windows-android-current-candidate-smoke.md`](docs/testing/reports/2026-09-01-windows-android-current-candidate-smoke.md). A subsequent non-RF candidate moved potentially reentrant WinRT teardown outside the event-delivery lock with explicit connection/GATT cleanup gates, extracted `connection.rs`, split GATT into parent/discovery/I/O/test modules, removed unbounded historical caller-ID tombstones, redacted raw Bluetooth addresses from default scan tracing, and hardened standalone Windows HIL subprocess/disposal/recovery reporting. It passed 34 Windows tests, 109 workspace tests, strict Clippy, Ruff, rustfmt, and provider-profile isolation. This hardened candidate then reran and passed the QEMU/BLESS non-destructive scan/connect/public GATT/read/write/`PlatformDefault` Notification/strict Indication matrix, including deterministic payloads and post-disable stale-route silence; see [`docs/testing/reports/2026-09-01-windows-hardened-cleanup-qemu-rf.md`](docs/testing/reports/2026-09-01-windows-hardened-cleanup-qemu-rf.md).
- The latest Phase 2 candidate now passes 39 Windows tests and 114 workspace tests. The OnePlus 11 Android fixture (`3ac5cc09`) reran the non-destructive scan/connect/GATT/read/write/Notification matrix; the combined process was interrupted near strict Indication, so strict Indication was rerun independently and passed rather than inferred. The same candidate retains the complete QEMU/BLESS non-destructive matrix evidence. Standard Cargo staged archives, metadata-enforced source isolation, the offline locked `tests/consumers/windows` artifact-only consumer, and the user-facing `examples/windows-central` strict-Clippy gate all pass. See [`docs/testing/reports/2026-09-01-windows-current-candidate-rf-packaging-summary.md`](docs/testing/reports/2026-09-01-windows-current-candidate-rf-packaging-summary.md). A release-build 20-byte throughput matrix ran three 200-operation batches for Android and QEMU. The latest ledger-accounted rerun measured QEMU median uncached read, callback-confirmed write-with-response, no-response WinRT terminal, and no-response Peripheral callback rates of 17.9, 16.7, 1,263.6, and 91.8 ops/s; Android measured 8.3, 8.3, 1,262.4, and 844.7 ops/s. Every write batch reached 200/200 unique fixture callbacks with zero missing, duplicate, out-of-order, out-of-range, foreign-batch, or malformed values. WinRT no-response terminals remain local/native progress and are not relabeled as remote throughput. Earlier lossless Notification medians were 85.1 ops/s for QEMU and 820.0 ops/s for Android. QEMU HCI evidence observed a 30 ms write-stage connection interval and 54.732 ms median ATT Write Request spacing. One unpaced BLESS burst delivered only 183/200; formal 0.5 ms requested guest-pacing and Android callback-backpressure runs had zero sequence gaps, duplicates, or order violations. These are application-boundary values, not raw PHY throughput; see [`docs/testing/reports/2026-09-01-windows-android-qemu-throughput-matrix.md`](docs/testing/reports/2026-09-01-windows-android-qemu-throughput-matrix.md). The final 10-cycle scan/connect and full active-resource accounting smokes now pass with 40 feature-enabled Windows tests and exact per-cycle logical-root and callback-delegate return to zero. The same candidate then passed one OnePlus 11 radio-disable and one QEMU UB500-detach active-subscription passive-disconnect gate, including `command_id=None` disconnect, pre-dispose connection/GATT/subscription resource release, post-dispose exact-zero accounting, and provider recovery; see [`docs/testing/reports/2026-09-02-windows-passive-disconnect-resource-recovery.md`](docs/testing/reports/2026-09-02-windows-passive-disconnect-resource-recovery.md). Those gates were subsequently executed in the 2026-09-02 follow-up described below; this earlier report remains the evidence for the single-cycle passive-disconnect boundary.
- A throughput follow-up added a nonfatal Windows `ThroughputOptimized` connection-parameter request, 20–512-byte HIL payloads, Android PHY/MTU callback evidence, and a QEMU throughput-only active-PHY control. Android confirmed MTU 517 and 2M/2M; 244-byte no-response writes and Notifications reached about 98 KiB/s (~0.8 Mbit/s) at the Peripheral/BleHub application boundaries without sequence loss. QEMU confirmed MTU 517, DLE 251, 15 ms interval, and active 2M/2M, but 244-byte no-response writes remained about 21.7 KiB/s and Notifications about 1.99 KiB/s. Guest HCI evidence identified the UB500's 27-byte × 8 LE ACL host buffer (v2 buffer-size unsupported) as the long-Notification fragmentation limit. Repeated 512-byte QEMU reads disconnected and are not recorded as passed; the stable QEMU matrix is limited to 20/100/244 bytes.
- The consolidated 2026-09-02 follow-up passed four strict 1,000-packet Android/QEMU Notification/Indication soaks with zero sequence loss, three destructive recovery cycles per provider, and four faults synchronized with entered WinRT read/write/CCCD/value callbacks. The provisioned QEMU image was refreshed; BlueZ restart passed, and a TCG same-process guest reboot completed guest shutdown plus SSH/controller/fixture/doctor/Windows-scan recovery under the then-current controller. QEMU 11.1.0 under WHPX instead paused with `Unexpected VP exit code 4` after guest shutdown, while a fresh WHPX process restored UB500, BlueZ/BLESS, doctor, and Windows scan. Same-process WHPX guest reboot is therefore an accepted fixture-infrastructure limitation; fresh-process restart is the supported WHPX recovery path. Elevation scripts, Zadig execution, and USB rollback were hardened against the reviewed TOCTOU/identity risks and passed static checks, but the UAC-changing operations were not executed. The reboot controller was subsequently strengthened to require a changed Linux boot ID, but that TCG RF assertion has not been rerun. A real Windows host reboot also remains unexecuted. See [`docs/testing/reports/2026-09-02-windows-phase2-fault-soak-reboot-hardening.md`](docs/testing/reports/2026-09-02-windows-phase2-fault-soak-reboot-hardening.md). The explicit Phase 2 exit review passed. Neither unexecuted boundary is claimed as passed.
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

- `crates/blehub` owns and exports the production `BleHub` object, unified DTOs/errors, foreign `BleBackend`, foreign `BleEventSink`, direct ingress, command API, and event API.
- `crates/blehub-uniffi` is the only final UniFFI native-image, bindgen, and production-package owner; it defines no second API.
- `BleBackend` and `BleEventSink` use ordinary non-async methods.
- Commands are fire-and-forget:
  - synchronous `Result` means accepted/rejected for dispatch;
  - eventual platform/BLE outcomes are events.
- The caller owns command IDs, timeout policy, cancellation policy, stale/late/duplicate event handling, and business state.
- BleHub core owns no runtime, executor, worker thread, mailbox, timer, event loop, or canonical BLE device/GATT state.
- Backends own unavoidable platform handles, callback correlation, and serialized native GATT operations.
- Native backends retain the exported `BackendEventIngressHandle`, whose Rust implementation holds only a weak reference to per-Hub ingress state; there is no token or process-local routing registry.
- Rust, Swift, or Kotlin downstream code supplies the foreign `BleEventSink` and decides whether events go to a channel, actor, UI loop, or runtime.

Current platform direction:

| Baseline order | Platform | Backend direction |
|---:|---|---|
| 1 | Windows | Rust + `windows-rs`; Phase 2 reference implementation complete |
| 2 | macOS/iOS | One shared Swift + CoreBluetooth + Stable UniFFI backend; macOS first, iOS independently validated in Phase 3 |
| 3 | Android | Kotlin + Android Framework BLE + Stable UniFFI in Phase 4 |
| 4 | Linux | Rust + `dbus-rs` + BlueZ D-Bus in Phase 6 |

The baseline order records dependency and release sequencing. Phase 3 Apple completes before Phase 4 Android; Phase 5 freezes the shared Windows + Apple + Android v0.1 production ABI. Stable UniFFI direct ingress is the production mechanism. Swift production-candidate runtime/ARC, lifecycle, fault isolation, packaging, and performance gates still require validation before Apple artifacts can ship; before the public ABI is frozen, any Apple result applies only to the tested unfrozen candidate. Apple production transport uses the system CoreBluetooth framework directly. High-level Swift BLE wrappers that own async/reactive execution, timers, retries, operation queues, or canonical BLE state are not used. The pinned local-source audit of Nordic `IOS-CoreBluetooth-Mock` `1.0.6` found that complete-backend use would require a broad second CBM wrapper/delegate type system while its scan queue, native wrapper identity, and no-response flow-control behavior cannot serve as CoreBluetooth authority; it is therefore not added to the current production or test targets. See [`docs/research/swift-ble-ecosystem-assessment.md`](docs/research/swift-ble-ecosystem-assessment.md).

## Dependency And Tooling Rules

- Rust dependencies are managed by Cargo once the Rust workspace is approved and created.
- External development tools are managed by Pixi.
- Project recipes are defined in `justfile`.
- `just` itself is managed by Pixi.
- Run project recipes through Pixi:

```text
pixi run just <recipe>
```

Do not assume globally installed Python, Ruff, Just, RTK, sccache, or other tools. Test recipes route verbose Cargo/Gradle output through Pixi-managed RTK; RTK is an output filter only and must preserve command selection, failure detail, and exit status. Pixi-managed sccache wraps ordinary Rust compilation and eligible C/C++ glue with Cargo incremental disabled to avoid duplicate caches; it does not reduce required final artifacts and does not cache Swift/Kotlin compilation. Stable UniFFI scaffolding is generated by the locked proc macro during ordinary Cargo builds; Kotlin/Swift source, headers, and module maps are generated by the locked bindgen from the same candidate library.

Except for Apple Swift/Xcode, machine-local configurable toolchain roots resolve through `BLEHUB_<TOOL>_ROOT` and ignored `blehub.local.toml` overrides. Android then honors `ANDROID_HOME`/`ANDROID_SDK_ROOT` and otherwise uses the platform-standard shared SDK location (`~/Library/Android/sdk` on macOS, `%LOCALAPPDATA%/Android/Sdk` on Windows, or `~/Android/Sdk` on Linux), so Android Studio and other projects can reuse the pinned packages. Other project-managed tools fall back to `~/.cache/blehub/toolchains/<tool>`. Apple Swift comes only from the Xcode installation selected by `xcode-select` on the project real Mac mini; there is no `BLEHUB_SWIFT_ROOT`, local `swift_root`, or project Swift bootstrap. Copy `blehub.local.example.toml` for stable developer-machine overrides; future non-Apple CI may use environment variables. Keep only machine-local paths and endpoints in local config; pin versions, URLs, hashes, repositories, and license policy in tracked scripts and locks. Use `pixi run just toolchain-paths` to inspect applicable effective values. Bootstrap/install recipes must be explicit; ordinary build and test recipes must not silently install Android SDK/NDK, replace Xcode/Swift, or accept licenses.

The user-approved development workflow uses two independent Git clones sharing one GitHub remote and commit history, with both Windows and the real Mac mini developing on `main`. There are no maintained per-host branches. Both hosts may commit and push `main` when explicitly authorized; Windows is no longer the sole Git owner. Before starting work, use `git pull --ff-only` with a clean worktree. If both hosts have committed and the histories diverge, fetch, review and merge `origin/main`, resolve conflicts and validate before pushing; never force-push to overwrite the other host. Exchange changes through Git, never live filesystem synchronization between the two worktrees. Migration starts by publishing the current Windows checkout as the common baseline, then cloning that history on the Mac while preserving its old working directory. A Mac repository with no commits is not yet a migrated clone. See [`docs/git-workflow.md`](docs/git-workflow.md) for setup and daily use.

The BleHub Mutagen project has been terminated and its daemon stopped for this migration. Do not restart it against the independent Windows/Mac Git worktrees. The existing `tools/mutagen_sync.py` and Just recipes remain available only for an explicitly requested temporary non-Git copy; they are not the maintained collaboration workflow or part of build/runtime/package. They retain `two-way-safe`, VCS ignores, and ignored local endpoint configuration. Never copy `.git` between hosts. Machine configuration, tool environments, work directories, and generated/build output remain local. A Git push/merge or historical Mutagen transfer does not transfer build, runtime, package, or RF evidence between platforms; validate the relevant gates on each host against the identified candidate.

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
