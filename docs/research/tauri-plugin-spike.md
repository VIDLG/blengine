# Tauri Plugin Feasibility Spike

- Date: 2026-09-05
- Status: historical spike evidence plus the current user-authorized integration
  decision. Maintained code lives under `integrations/tauri-plugin-blehub` and
  `examples/tauri-blehub-internal`. Historical passes below are not refactor reruns;
  current release-candidate RF/soak evidence must be recorded separately.
- Scope: Tauri 2 plugin loading, `BleHub` assembly, visible React/Astryx
  basic-flow command coverage, synchronous command acknowledgement, bounded
  event handoff, event projection, and dispose
- Not covered: CoreBluetooth, P-Android RF, Android host packaging, iOS
  signing, production plugin publication, or macOS native-window UI
  interaction assertions. Tauri's `tauri-driver 2.0.6` is Linux/Windows-only;
  the macOS WebView cannot use that driver.

## Fixture

The historical fixture is archived under
[`spikes/archive/tauri-downstream/`](../../spikes/archive/tauri-downstream/).
The maintained Cargo workspace is split between
[`integrations/tauri-plugin-blehub/`](../../integrations/tauri-plugin-blehub/)
and [`examples/tauri-blehub-internal/`](../../examples/tauri-blehub-internal/).

```text
spikes/archive/tauri-downstream/
├── plugins/blehub/       # archived Tauri plugin crate
├── src-tauri/            # archived Tauri host
├── ui/                   # archived React/Astryx Vite frontend
└── Cargo.toml            # historical isolated workspace
```

The archived fixture used a deterministic `FakeBackend` and callback driver.
That is historical topology, not today's default on non-macOS targets.
Maintained `init()` defaults to native macOS Swift assembly or Windows Rust
construction; unsupported targets fail setup explicitly. `with_backend` is the
test/optional-provider seam with the same IPC and permissions. The example uses
its own demo backend only with `--no-default-features --features demo`; no
`CallbackDriver`, probe, or fake-only IPC remains in the maintained plugin.

## Historical evidence — not rerun by this update

All commands, counts, launch observations, old entry-point names, and UI choices
in this section describe the historical spike candidate only. Keep them scoped
to that candidate; they do not describe the current public plugin surface or
prove the authorized refactor, native linkage, Windows execution, or RF.

The following commands passed on the project Mac mini with the Pixi-managed
Rust 1.95 toolchain, including the generated `attachBackend` surface:

```text
pixi run cargo test --manifest-path spikes/tauri-downstream/Cargo.toml --workspace
pixi run cargo check --manifest-path spikes/tauri-downstream/src-tauri/Cargo.toml
pixi run just spike tauri-host-build
```

The frontend build also passed with React 19.2.8, React Compiler 1.0.0,
Vite 8.2.2, `@astryxdesign/core` 0.5.2, and
`@astryxdesign/theme-neutral` 0.5.2:

```text
pixi run npm run build   # from spikes/tauri-downstream/ui
```

The frontend uses Astryx reset/theme CSS, Astryx semantic components, and
StyleX tokens compiled by the Vite and Vitest plugins. No project-owned
`styles.css` remains. Browser verification passed with the bundled headless
Chromium runtime:

```text
pixi run npm run test:component  # 2 passed
pixi run npm run test:visual     # 2 passed, desktop + 390px mobile baseline
pixi run -e apple-ci swift test --package-path spikes/tauri-downstream/plugins/blehub/assembly/macos  # 1 passed
```

These tests validate the React surface and deterministic event projection;
they do not claim that Chromium is the Tauri runtime. The native WebDriver
attempt was made explicitly and terminated with `tauri-driver is not supported
on this platform` on macOS, matching the driver's source-level Linux/Windows
platform guard.

The production Apple package was rebuilt with the new assembly surface. The
Swift assembly test passed two tests, including unknown-ticket rejection. The
Tauri host was built with `init_with_assembly`; the actual macOS executable
reached the running process state for a bounded 30-second observation without a
setup panic, native loading error, Swift attach error, or CoreBluetooth
construction error. This is setup/ownership evidence, not a Tauri RF result.

The workspace test result was 2 passed, 0 failed. The tests cover command-ID
preservation in the projected event and bounded, non-blocking sink handoff.
The host `cargo check` compiled Tauri 2.11.5, `tauri-build` 2.6.3, the
production `blehub` crate, the testkit-backed host, and the plugin crate.
The host build also links the plugin-owned macOS Swift assembly as a static
library and completes successfully.

The desktop host was also launched with:

```text
pixi run cargo run --manifest-path spikes/tauri-downstream/src-tauri/Cargo.toml
```

On macOS the host installs `init_with_assembly`; plugin setup constructs the
production `AppleBleBackend`, calls generated Stable UniFFI `attachBackend`,
and stores the resulting Rust-owned `BleHub`. The UI detects the selected
`apple-corebluetooth` path and presents **Initialize CoreBluetooth**. On
non-macOS hosts the reference app retains the deterministic fake flow. The
plugin's async event pump projects typed `BleEvent` values to
`blehub://event` through the bounded sink.

## Real-backend assembly boundary

The maintained integration keeps plugin and host separate. `init()` owns native
selection and assembly; `with_backend(backend)` accepts a test or optional
provider without changing IPC. macOS creates its Swift backend inside the
plugin and the app supplies no assembly callback. Windows directly constructs
`WindowsBackend`; that wiring is source evidence only, not a validated Windows
Tauri run. The production Apple backend implements the generated foreign
`BleBackend` protocol, not a second plugin backend trait.

Tauri's desktop plugin model runs the desktop implementation in Rust; its
Swift plugin package is the mobile/iOS extension. Consequently, the Swift
`AppleBleBackend` cannot be passed directly as a Rust `Arc<dyn BleBackend>`.
Doing so would require an explicit, audited Rust/Swift assembly bridge that
preserves the existing Stable UniFFI object and callback ownership model. A
second JSON protocol, a child-process proxy, or a duplicate CoreBluetooth Rust
backend would not be equivalent and is not part of this spike.

The macOS assembly now builds the production `AppleBleBackend` from
plugin-owned `assembly/macos`, invokes one narrow `@_cdecl` launcher carrying
only an assembly ticket, and calls generated Stable UniFFI `attachBackend` with
the Swift backend object. Rust then executes the canonical `BleHub::new` and
owns the resulting Hub. The ticket registry is assembly-only and is not an
event-routing or BLE-state registry.

The launcher does not expose scan, connect, GATT, read, write, notification, or
other per-operation C functions. Swift-RS is used only for the C-compatible
launcher; all backend/event values and callbacks remain on the Stable UniFFI
surface. Successful transfer leaves final disposal with `BleHub::dispose`;
failed setup and cancellation are rollback paths.

The ecosystem comparison and rejected alternatives remain recorded in
[`tauri-apple-assembly-ecosystem.md`](tauri-apple-assembly-ecosystem.md).

## Maintained responsibility shape

```text
Tauri Web
    -> Rust plugin command adapter
    -> BleHub::initialize / BleHub::dispose
    -> foreign or Rust BleBackend

BleEventSink
    -> bounded non-blocking channel handoff
    -> plugin-owned Tauri async event pump
    -> AppHandle::emit("blehub://event")
```

The sink performs only nonblocking handoff on the backend callback stack.
Projection/emission happens independently on the plugin-owned Tauri async task,
not inline in the sink, and adds no plugin-owned OS thread. Execution may
interleave with the sink's return; queue handoff is not a global scheduling
barrier or a frontend-delivery guarantee. Core still owns no runtime.

## Decision

The user-authorized refactor keeps the promoted integration and example in
their own workspaces and fixes these current decisions:

- Package and Cargo `links` are `tauri-plugin-blehub`; Tauri namespace remains
  `blehub`. `lib.rs`, `commands.rs`, `events.rs`, and `projection.rs` separate
  assembly/lifecycle, typed dispatch, transport, and JSON responsibilities.
- Official `tauri_plugin::Builder` generates command permissions; defaults live
  in the plugin's `permissions/default.toml`. Example build only invokes
  `tauri_build::build()`. Native selection uses `CARGO_CFG_TARGET_OS`.
- Native `init()` and injected `with_backend()` expose capabilities, official
  BLE commands, and close only. There is no backend-mode/probe protocol or
  default fake fallback. Production dependencies never come from `tests/`;
  testkit remains a test-only dev-dependency.
- Typed JSON preserves structured error/category/platform fields, snake_case
  states, advertisement manufacturer/service data, and GATT descriptors and
  duplicate instances. Every `u64` ID is a decimal string, including request
  IDs; core DTOs and UniFFI ABI are not changed by this serialization policy.
  `detail` is display-only, never control flow.
- The sink uses bounded futures MPSC `channel(128)` plus one reserved slot for
  its sole sender. Overflow closes the source and best-effort projects a
  transport error before cleanup; it cannot guarantee fault delivery and does
  not feed a synthetic BLE error back into core.
- Plugin close quiesces emission, discards buffers, aborts the task, and runs
  Hub cleanup outside the emission gate. Async close awaits the task handle it
  takes. Actual `Exit` and `on_drop` provide fallback, not cancellable
  `ExitRequested`. Synchronous Rust listeners must not tear down the plugin
  from its `emit` stack.
- Installation is one-shot per app, including after removal. Restart requires
  a new app and Hub; clearing a marker to reuse a closed Hub is not supported.
- The reference flow registers fresh string IDs before invoke and waits for
  successful terminal plus required typed domain evidence. It requires a
  powered-on, authorized adapter before proceeding. Single-provider scan/session
  evidence is not fabricated advertisement data; GATT verifies the actual
  profile. No-response delayed readback is a separate command/evidence boundary.

The [Plan dependency diagram](../../.plan/PLAN.md#551-tauri-薄适配器装配) distinguishes
canonical API ownership, the single-image shell, generated binding consumption,
and foreign trait implementation; callback direction does not create a reverse
Cargo dependency. Operational details and commands live in the
[plugin README](../../integrations/tauri-plugin-blehub/README.md) and
[example README](../../examples/tauri-blehub-internal/README.md).

This update records decisions, not new passes. The coordinating agent records
final refactor results. `tauri::tauri-rust-test` explicitly runs the nonnative
plugin workspace and explicit-demo example workspace; those checks do not prove
native assembly, Windows runtime, or RF. Current-candidate native image/package,
RF, fault/recovery, and 30-minute soak must keep their own evidence. They gate
the corresponding Tauri showcase, **not Phase 5 public ABI freeze**; production
core/backend/runtime/package requirements remain independent.
