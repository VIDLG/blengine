# Tauri Apple Assembly Ecosystem Review

- Date: 2026-09-05
- Scope: unified macOS+iOS Rust Tauri plugin assembly of the existing Swift
  `AppleBleBackend`
- Status: shared Apple assembly and official iOS build integration are implemented.
  Current results below are supplied by the coordinating session, not rerun by
  this documentation update; runtime, signing, and RF remain platform-specific.

## Boundary

Tauri desktop plugins are Rust implementations. Tauri's Swift plugin package
is the mobile/iOS extension, not a macOS desktop implementation. The Tauri
plugin therefore remains Rust-owned and holds the production `BleHub`; Swift
is only needed because the existing Apple transport is implemented in Swift.

```text
Tauri Web UI
    -> Rust Tauri plugin
    -> Rust-owned BleHub
    -> canonical Stable UniFFI foreign BleBackend proxy
    -> Swift AppleBleBackend (plugin-owned shared macOS+iOS assembly)
    -> CoreBluetooth
```

The plugin and app stay separate. The plugin owns assembly, typed IPC/projection,
bounded event transport, the Rust `BleHub`, and cleanup. The app owns window,
configuration/capability grants, frontend IDs, timeout policy, and flow tracking.
Apple transport stays under `platforms/apple`. The shared Apple launcher transfers the
Swift backend through canonical Stable UniFFI and Rust retains the resulting Hub.

The [Plan responsibility diagram](../../.plan/PLAN.md#551-tauri-薄适配器装配)
separates three different relationships:

1. `crates/blehub` owns the API/traits/ingress and generic assembly; the thin
   `crates/blehub-uniffi` shell depends on it for final native-image production,
   bindgen, and package staging. The shell adds no backend factory or second API.
2. Apple Swift consumes the same-candidate generated package and implements
   `BleBackend`. That implementation and runtime callback direction are not a
   reverse Cargo dependency from core to Apple or to Tauri.
3. The Tauri plugin depends on core and its selected provider/launcher. The final
   process must have a single BleHub/UniFFI symbol owner, not an independent
   Rust-host registry and another copy hidden in a loaded native library.
   Validate the linked artifact; a source graph alone does not prove this gate.

`init()` with default `native` selects plugin-owned Swift assembly on macOS+iOS and
Rust `WindowsBackend` on Windows. Unsupported/no-native targets fail setup,
never silently select a fake. Windows wiring is not a validated Windows Tauri
runtime/RF result. `with_backend(backend)` remains the test/optional-provider seam
with identical IPC and permissions; the example's demo is explicitly selected
with `--no-default-features --features demo`.

## Unified Apple assembly and official iOS build ownership

macOS 13 and iOS 16 share
`integrations/tauri-plugin-blehub/plugins/blehub/assembly/apple-assembly`.
The one-time ticket launcher and generated `attach_backend` flow below are
shared; iOS does not need a second Swift `PluginHandle` assembly command or a
second CoreBluetooth implementation. Mobile glue must not introduce per-BLE-
operation JSON commands.

The plugin's `build.rs` selects exactly one Swift build/link owner per target:

- iOS: official `tauri_plugin::Builder::ios_path("assembly/apple-assembly")`,
  which owns the Swift link step; no additional direct `SwiftLinker` invocation.
- macOS: the plugin build script's `SwiftLinker`, without the mobile builder path.

The Swift package/product is named `tauri-plugin-blehub` as required by the
Tauri linker. `swift-rs` is narrow launch/link glue, not the trait/DTO ownership
transfer mechanism. The generic constructor, `BackendAssembly` guard/registry,
and exported `attach_backend` remain in `crates/blehub`. Source-package staging
provides same-candidate generated bindings/headers; it neither defines another
API nor introduces a second native image.

The example has the standard mobile entry point in `src-tauri/src/lib.rs`.
Official CLI `2.11.4`, its npm lock, and `scripts.tauri` live at the example root,
not `ui/`; standard `npm run tauri` lets Xcode callbacks rediscover that CLI.
No custom Tauri XcodeGen template or callback wrapper is maintained.
`tauri::ios-init` explicitly passes `--skip-targets-install`, and
`tauri::ios-build` supports complete unsigned `aarch64-sim` / `aarch64` app builds.

The tracked, manually maintained `tests/hil/apple/ios/project.yml` belongs to
the independent native Swift HIL. In contrast, the example's
`src-tauri/gen/apple/project.yml` is official ignored generated output and must
not be hand-edited. `ios init` reuses an existing template: preserve the old
`gen/apple` directory outside the generation path before regenerating after
relevant configuration changes. iOS Tauri runtime, development signing, RF,
and mobile lifecycle still require independent validation. Android Central
sequencing remains Phase 4; this change adds no Android implementation.

## Options

| Option | Useful capability | Decision |
|---|---|---|
| Stable UniFFI | Existing foreign `BleBackend`, `BleEventSink`, object handles, generated Swift package, and the tested production image | **Keep as the only BLE ABI** |
| `swift-rs` | Links a Swift package and calls narrow `@_cdecl` functions with C-compatible values | Keep the narrow shared Apple launcher with one linker owner per target; not BLE DTOs, traits, ownership transfer, or operations |
| `swift-bridge` | Rich Rust/Swift structs, classes, results, and async bridge | Do not add: it would create a second Rust/Swift ABI beside UniFFI |
| Rust `objc2-core-bluetooth`/`corebluetooth` | Could make a Tauri macOS path pure Rust | Do not duplicate the already-tested Swift Apple backend |
| Child process or JSON proxy | Easy process boundary | Reject: it changes ownership, timing, and event semantics |

`swift-rs` documents that its direct C entry points are limited to
Objective-C-compatible values and that creating Rust-side objects in Swift is
not its goal. `swift-bridge` is more expressive, but its generated bridge
would still be a second contract. Rust CoreBluetooth crates are useful
ecosystem references, but adopting one would mean another backend state
machine and another RF/lifecycle validation matrix.

## Decision

Use Stable UniFFI for every `BleHub`/backend/event value and callback. The
implemented macOS+iOS path has only a narrow assembly launcher:

```text
let assembly = BackendAssembly::new(sink)
    -> launcher(assembly.ticket()) asks Swift to create AppleBleBackend
    -> generated attachBackend(ticket, backend) calls canonical BleHub::new
    -> let hub = assembly.finish() transfers ownership to the Rust plugin
```

`BackendAssembly` is a project-owned RAII guard: dropping an unfinished guard
cancels a pending/attaching handoff and disposes an attached-but-unclaimed Hub.
Pending sink release, unexpected/failed-finish entry destruction, and unclaimed
Hub dispose all occur outside the registry mutex: remove/take under lock, then
destroy after unlock. This rule includes implicit final `Arc` drops and foreign
destructors, not just explicit cleanup methods. The registry is assembly-only,
never event routing, backend lookup, or canonical BLE state. A generic
scope-guard crate would replace only the `Drop` syntax, while `slotmap` or an
arena would replace only key storage; none models the one-shot state machine,
cancellation race, ownership transfer, or lock boundary. The small explicit
implementation is therefore lower-risk than adding another dependency.

The launcher must not expose scan/connect/GATT/read/write/notification
functions. `swift-rs` cannot by itself turn a Swift `AppleBleBackend` instance
into Rust's `Arc<dyn BleBackend>`; that is an ownership boundary, not a
launcher concern. Duplicate/unknown/late attach, cancellation during construction,
unclaimed-Hub cleanup, ARC release, and final single-image ownership require
candidate-specific checks. Any host timeout policy is caller-owned, not a core
timer. If a provider needs a second operation protocol or duplicate image, that
provider is a No-Go; it is not enabled by relabeling setup as ownership proof.

The maintained implementation is under
`integrations/tauri-plugin-blehub/plugins/blehub/assembly/apple-assembly/`, with the
reference host under `examples/tauri-blehub-internal/`. The previous implementation
slice is retained at `spikes/archive/tauri-downstream/` as historical evidence.
The migrated path constructs the production Swift backend inside the plugin and
transfers it into the plugin-owned Tauri `BleHub` through the canonical Stable
UniFFI surface. The host does not provide a backend factory or assembly
callback.

## Maintained integration and lifecycle consequences

- Package and `links` are `tauri-plugin-blehub`, still `publish = false`.
  `lib.rs` owns assembly/lifecycle while `commands.rs`, `events.rs`, and
  `projection.rs` own dispatch, transport, and JSON. Official plugin Builder
  permissions live in the plugin's `permissions/default.toml`; the example build
  only calls `tauri_build::build()`. Target selection uses `CARGO_CFG_TARGET_OS`.
- Capabilities and official commands are shared by every provider; there is no
  `backend_mode`, `CallbackDriver`, or probe IPC. Production dependencies do not
  use `tests/`; testkit is allowed as a dev-dependency only.
- Structured errors, snake_case states, advertisement auxiliary data, and GATT
  descriptors survive the typed projection. All `u64` IDs use decimal JSON
  strings, solely in the integration; core ABI stays unchanged. Display text
  never substitutes for typed error/state/correlation decisions.
- Bounded futures MPSC `channel(128)` has one additional reserved slot for its
  only sender. Overflow closes the source, best-effort reports transport failure,
  and triggers cleanup; it does not guarantee terminal delivery or create a
  core BLE fault. The task uses Tauri's runtime, not a core queue/runtime.
- Before Hub cleanup, plugin stop quiesces emission, discards queued events, and
  aborts the task; async close awaits the handle it takes. No emission lock
  crosses ingress drain/native cleanup. Actual `Exit` and `on_drop` retire the
  plugin; cancellable `ExitRequested` must not. Rust listeners cannot tear it
  down synchronously from the `emit` stack.
- Hub cleanup itself uses `Open -> Closing -> Closed(result)` with a Condvar:
  one disposer drains/cleans while concurrent disposers wait for the same final
  result, including errors. No command gate crosses cleanup, and an empty
  backend slot alone never proves completion. Foreign failure logs use fixed
  classes rather than arbitrary text; public error data is preserved.
- Each Tauri app permits one installation only, including after removal.
  Restart needs a new app and Hub, not reuse of retained closed state.

Current coordinating-session evidence is deliberately bounded:

- Two real macOS Swift assembly XCTest tests are registered and passed after correcting
  the missing manifest `testTarget`; this is not a zero-test success.
- Complete unsigned Tauri iOS simulator/device app builds both passed. Final
  plist minimum OS and `LC_BUILD_VERSION` are 16.0; each slice has all 123 BleHub
  UniFFI definitions exactly once, one assembly entry, zero old harness symbols,
  and zero additional BleHub dylibs. iOS Tauri runtime, development signing,
  and RF have not run.
- System logs established why the old WDIO direct spawn could not prompt:
  responsibility belonged to `sshd-keygen-wrapper`, and Bluetooth policy logged
  `Policy disallows prompt`. This was a launch-attribution defect, not simply
  an unanswered authorization request.
- The test-only `macos-owner.js` + `launch-app.swift` now launch the actual bundle
  through standard `NSWorkspace`/LaunchServices and retain `NSRunningApplication`.
  macOS WDIO uses `services: []` and ordinary WebDriver Classic against the same
  Rust embedded server. A temporary IPv4 loopback port, `lsof` match to the
  returned app PID, and `/status` readiness check precede session creation.
  Stdin EOF, including after Node-parent `SIGKILL`, triggers exact-instance
  `terminate` with bounded `forceTerminate` fallback. No `osascript` or `pkill`
  scripting is involved. Windows/Linux retain the npm service. Its lack of an
  official attach option does not prohibit this automation: only process
  ownership changes, not the protocol, BLE ABI, or OS security policy.
- The new demo run passed 3 tests with 3 skipped; owner lifecycle scenarios
  passed independently. The coordinating thread's dedicated
  `tauri::macos-owner-test` entry builds the bundle and owner and runs lifecycle
  tests without BLE commands or a successful native E2E prerequisite.
- Native Initialize initially timed out after 30 seconds despite the
  2026-09-09 15:38:30 TCC log confirming the app as responsible and
  `AUTHREQ_PROMPTING` / `display_prompt`. After user authorization, native E2E
  passed 2 tests / 4 skipped without RF. The macOS Tauri TCC blocker is closed;
  the preceding prompt/timeout is historical, not the current authorization state.
- `tauri::macos-bundle` builds the same debug/instrumented bundle;
  `tauri::macos-authorize` is human launch → click Initialize → allow → quit,
  not validation. Login-keychain access is the human-only
  `pixi run just apple unlock-login-keychain` step, never an agent password input.
- Apple prepare and builds consuming its shared staging must be serialized.
  Concurrent preparation caused a missing-directory failure; serial recovery
  restored staging, after which native iOS HIL signing still failed with
  `errSecInternalComponent` before RF. Staging races and signing failures are
  distinct, and neither is RF evidence.
- The first authorized Tauri scan showed start and terminal events but no
  advertisement. E2E had not ensured the Android Peripheral before its first
  scan, whereas native HIL exit stops that fixture. RF-only setup now calls
  `android-ensure` once before scanning; teardown restores radio/advertising.
  Visible-flow waits surface early UI errors without changing fault `allowError`.
- Native macOS → Android Peripheral `all` RF reran and passed independently.
  Then `pixi run just tauri tauri-native-rf` passed 6 tests / 0 skipped in about
  1m35s: visible flow, two fresh-ID repeats, 10 cycles, discovery radio loss,
  active-subscription passive loss/recovery, and dispose. App PID cleanup and
  Android `STATE_ON` / advertising readiness were confirmed. No backend changed
  and no assertion was weakened.
- The subsequent UI-only timeout diagnostics name missing evidence, command ID,
  and scope. All 19 component tests passed; a regression requires timeout and
  no connect for scan start/terminal without a matching valid advertisement,
  including stale-session or explicitly nonconnectable observations. The final
  diagnostic candidate subsequently reran the same RF command and passed
  6 tests / 0 skipped in 1m34.6s (WDIO total 1m36). Owned app exit and Peripheral
  `STATE_ON` / ready were confirmed. This independently validates that candidate;
  neither this rerun nor the first pass establishes long-soak coverage.
- The latest serial native iOS `runtime-ios-android` retry still failed at
  `codesign` with `errSecInternalComponent` before RF. Historical iOS I/O RF is
  not a current full-matrix result; Tauri iOS remains unrun and Windows untested.

The coordinating agent owns the detailed run report; this document records the
architecture and evidence boundary rather than duplicating the maintenance log.
See the [candidate-specific report](../testing/reports/2026-09-09-tauri-apple-dual-target-workflow.md).
Tauri-specific remaining gates delay their provider/showcase only and **do not
block Phase 5 public ABI freeze**. See the
[integration README](../../integrations/tauri-plugin-blehub/README.md) for the
maintained interface and [example README](../../examples/tauri-blehub-internal/README.md)
for flow policy and execution recipes.
