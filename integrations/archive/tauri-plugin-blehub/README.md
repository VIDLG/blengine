# BleHub Tauri plugin integration

This standalone Cargo workspace owns the repository-only Tauri adapter for the
canonical [BleHub contract](../../.plan/PLAN.md). The package and Cargo `links`
name are both `tauri-plugin-blehub`; the Tauri namespace is `blehub`.
`publish = false` remains intentional. The reference application is
[`examples/tauri-blehub-internal`](../../examples/tauri-blehub-internal/), not part of this workspace.

## Ownership and entry points

The plugin owns a Rust `BleHub`, its sink, event task, and platform assembly.
Core owns no Tauri runtime, queue, or JSON dependency. Production BLE transport
stays under `platforms/`; the plugin defines neither a second backend contract
nor a second native image.

| Entry/configuration | Behavior |
|---|---|
| `tauri_plugin_blehub::init()` with default `native` | Plugin-owned shared Swift `AppleBleBackend` assembly on macOS+iOS, or direct Rust `WindowsBackend` construction on Windows |
| Unsupported target, or `init()` without a native provider | Explicit setup error; never an automatic fake fallback |
| `tauri_plugin_blehub::with_backend(backend)` | Test or optional caller-supplied provider, with exactly the same IPC and permissions |
| Example `--no-default-features --features demo` | Explicit example-owned deterministic backend, not native BLE evidence |

Windows provider wiring exists in source but has not been validated in this
refactor's Windows runtime/RF lane. Android remains a future provider. macOS and
iOS use one shared Swift package under
[`assembly/apple-assembly`](plugins/blehub/assembly/apple-assembly/)
(macOS 13 / iOS 16), with one narrow launcher
carrying a ticket, generated `attach_backend`, canonical `BleHub::new`, and
`BackendAssembly::finish()`. The iOS build uses Tauri's official `ios_path` mobile plugin setup as its
single Swift linker owner; macOS uses the plugin build script's `SwiftLinker`.
The package/product name is the Cargo package name required by Tauri's linker.
No target uses both paths, and no second Tauri command/event bridge or native
image is added. Same-candidate source-package staging supplies generated bindings
and headers, not new API definitions or a second native image. The host supplies
no assembly factory/callback.
See the [assembly decision](../../docs/research/tauri-apple-assembly-ecosystem.md)
and the [dependency/responsibility diagram](../../.plan/PLAN.md#551-tauri-薄适配器装配).

## Module and permission boundaries

Under [`plugins/blehub`](plugins/blehub/):

- [`src/lib.rs`](plugins/blehub/src/lib.rs): setup, native/provider selection,
  managed ownership, and lifecycle.
- [`src/commands.rs`](plugins/blehub/src/commands.rs): typed dispatch requests
  and errors; synchronous acceptance is not BLE completion.
- [`src/events.rs`](plugins/blehub/src/events.rs): bounded nonblocking sink,
  event task, transport failure, and emission shutdown.
- [`src/projection.rs`](plugins/blehub/src/projection.rs): typed JSON projection
  and lossless decimal-string IDs.
- [`build.rs`](plugins/blehub/build.rs): official `tauri_plugin::Builder`
  command-permission generation; native selection uses `CARGO_CFG_TARGET_OS`,
  not the build host alone. Native macOS assembly requires the maintained Mac host.
- [`permissions/default.toml`](plugins/blehub/permissions/default.toml): the
  plugin-owned default permission set, consumed as `blehub:default` by the app.

The example's build script only calls `tauri_build::build()`; it does not
recreate plugin permissions. Production dependencies do not come from `tests/`;
`blehub-testkit` is permitted only as a dev-dependency for deterministic tests.

## IPC and JSON

The registered surface is `capabilities`, `initialize`, `start_scan`,
`stop_scan`, `connect`, `disconnect`, `discover_gatt`, `read_characteristic`,
`write_characteristic`, `set_notifications`, `read_rssi`, and `close`, invoked
as `plugin:blehub|<command>`. There is no `backend_mode`, `CallbackDriver`,
probe, or fake-only command surface. Behavior choices use capabilities.

Events arrive on `blehub://event`. Their projection retains:

- typed `kind`, correlation IDs, variant-specific `data`, and structured
  `error` with `category`, `message`, and optional `platform_details`;
- snake_case adapter, authorization, connection, and notification states;
- actual notification mode separately from subscription state;
- actual advertisement service UUIDs, manufacturer/service-data entries and
  bytes, local name, RSSI, and optional connectability;
- GATT service/characteristic/descriptor instances, IDs, UUIDs, and properties,
  without merging duplicate UUID instances.

Dispatch errors retain category, message, optional original detail and native
platform fields. `close_failed` is an integration lifecycle category; native
classification is not guessed from its text. `detail` is display-only: never
parse it to drive state, matching, completion, or retry policy.

**Every `u64` ID in requests and projections is a decimal JSON string**, including
zero and `18446744073709551615`. Input must be a nonempty ASCII digit string in
range; leading zeroes are accepted, output is canonical decimal. Numeric JSON,
signs, fractions, exponents, and overflow are rejected. Frontends must not round
IDs through JavaScript `Number`. Byte arrays and smaller numeric fields retain
their typed numeric representation. This is integration-only serialization;
core DTOs and the Stable UniFFI ABI are unchanged.

## Event transport and shutdown

The sink only calls `try_send` into `futures_channel::mpsc::channel(128)`.
Futures adds **one reserved slot for the single sender**; it is not a dedicated
terminal slot, byte budget, or reliable-delivery guarantee. Projection and
`AppHandle::emit` run on a plugin-owned task using Tauri's async runtime, not
on the native callback stack and not on a new plugin OS thread.

Queue overflow closes the source and wakes the task. The task best-effort emits
an integration transport fault using the `backend_error` projection and triggers
Hub cleanup. It does not silently lose a terminal and continue a successful
flow, retry the event, or send the transport fault back through core ingress.
Emission failure also retires the transport and triggers cleanup. Neither
transport-fault delivery nor frontend receipt is guaranteed.

`close` first makes emission quiescent: it waits for entered emission, prevents
later emission, and aborts the task, intentionally discarding buffered events.
It then disposes the Hub without holding the emission gate across core drain
or native cleanup. Async `close` also awaits destruction of the task handle it
takes. Actual `RunEvent::Exit` and plugin `on_drop` perform synchronous stop and
cleanup fallback; cancellable `ExitRequested` and closing one window are not
app-exit boundaries. Already emitted WebView work is not recalled by close.

**Rust Tauri listeners must not synchronously tear down this plugin from inside
its `emit` stack.** Queue such work onto caller-owned execution. Separately,
`BleEventSink::on_event` must never synchronously reenter the same Hub.

One installation is allowed per Tauri app. Duplicate installation and
remove-then-reinstall are rejected: managed state is intentionally not recycled
into a closed Hub. Restart requires a new app and new Hub. Listener/projection
recreation alone does not reset Hub lifetime or caller-owned IDs.

## Validation boundary

Run the nonnative plugin and explicit-demo example workspace tests with:

```text
pixi run just tauri::tauri-rust-test
```

The recipe explicitly tests both independent workspaces: plugin with
`--no-default-features`, example with `--no-default-features --features demo`.
It does not validate default native linkage, Windows execution, or RF. Native
assembly, app builds, browser/native UI checks, RF, and release-candidate soak
remain separate; see the [example README](../../examples/tauri-blehub-internal/README.md).
The coordinating session reports two real macOS Swift assembly XCTest tests passed
with the package's `testTarget` now registered. Both complete unsigned Tauri iOS
app builds (`aarch64-sim` / `aarch64`) passed: final plist and `LC_BUILD_VERSION`
minimum OS are 16.0, and each final slice contains the 123 BleHub UniFFI
definitions exactly once plus one assembly entry, with zero old harness symbols
and zero additional BleHub dylibs. These are build/symbol gates, not iOS runtime,
development-signing, or RF results; those Tauri iOS gates remain unrun.

macOS WDIO now uses a test-layer `NSWorkspace`/LaunchServices owner for the actual
debug/instrumented bundle, retaining `NSRunningApplication` for exact-instance
cleanup. With `services: []`, ordinary WebDriver Classic connects to the same
Rust embedded server only after temporary loopback port/PID ownership and
`/status` readiness checks. This replaces npm-service process ownership, not the
protocol or TCC policy; Windows/Linux retain their service. See the
[E2E ownership details](../../examples/tauri-blehub-internal/e2e/tauri/README.md).

System logs identified the old direct-spawn root cause: responsible process
`sshd-keygen-wrapper`, with Bluetooth `Policy disallows prompt`. At
2026-09-09 15:38:30 the new path logged the app as responsible and
`AUTHREQ_PROMPTING` / `display_prompt`; that attempt timed out at 30 seconds.
After the user authorized Bluetooth, native E2E passed **2 tests / 4 skipped**
without RF, closing the macOS Tauri TCC blocker. Earlier demo (3 passed /
3 skipped) and owner lifecycle passes remain separate evidence.

The first scan then produced start and terminal events without advertisements:
E2E lacked `android-ensure` before its first scan, while native HIL exit stops
the Peripheral. RF-only setup now ensures readiness once, teardown restores
radio/advertising, and visible-flow waits propagate early UI errors without
altering fault `allowError`. With no backend changes or relaxed assertions,
the first complete Tauri RF run passed **6 tests / 0 skipped** in about 1m35s,
covering visible flow, two fresh-ID repeats, 10 cycles, discovery radio loss,
active-subscription passive loss/recovery, and dispose. App PID cleanup and
Android `STATE_ON` / advertising readiness were confirmed.

A later UI-only change adds missing evidence, command ID, and scope to timeout
diagnostics. All 19 component tests passed, including rejection of scan
start/terminal without a valid advertisement (stale-session/nonconnectable
observations do not permit connect). The final diagnostic candidate then
actually reran the RF command: **6 passing / 0 skipped in 1m34.6s** (WDIO total
1m36), with owned app exit and Peripheral `STATE_ON` / ready confirmed. This is
new same-candidate RF evidence, not an inherited earlier pass or long-soak proof.

Apple prepare/build workflows share staging and must run serially. A parallel
prepare caused a missing-directory failure; after serial staging recovery the
native iOS HIL signing retry still failed with `errSecInternalComponent` before
RF, including the latest serial `runtime-ios-android` retry. Native
macOS-to-Android Peripheral `all` RF reran and passed independently before the
Tauri RF pass. Historical iOS I/O RF does not prove a current full pass; Tauri
iOS remains unrun and Windows was not tested. Candidate-specific records are in
the [dated report](../../docs/testing/reports/2026-09-09-tauri-apple-dual-target-workflow.md).

Tauri is an optional downstream validation surface. Unfinished Tauri platform,
RF, or soak work delays that showcase, **not Phase 5's public ABI freeze**.
Production core/backend/runtime/package gates remain independently required.
