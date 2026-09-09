# Tauri Apple Assembly Ecosystem Review

- Date: 2026-09-05
- Scope: Rust Tauri desktop plugin assembly of the existing Swift
  `AppleBleBackend`
- Status: current decision aligned with the user-authorized refactor. macOS
  plugin-owned construction and ownership transfer are implemented; validation
  remains candidate/platform-specific. This update records no new pass.

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
    -> Swift AppleBleBackend (plugin-owned macOS assembly)
    -> CoreBluetooth
```

The plugin and app stay separate. The plugin owns assembly, typed IPC/projection,
bounded event transport, the Rust `BleHub`, and cleanup. The app owns window,
configuration/capability grants, frontend IDs, timeout policy, and flow tracking.
Apple transport stays under `platforms/apple`. The macOS launcher transfers the
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

`init()` with default `native` selects plugin-owned Swift assembly on macOS and
Rust `WindowsBackend` on Windows. Unsupported/no-native targets fail setup,
never silently select a fake. Windows wiring is not a validated Windows Tauri
runtime/RF result. `with_backend(backend)` remains the test/optional-provider seam
with identical IPC and permissions; the example's demo is explicitly selected
with `--no-default-features --features demo`.

## Future iOS shape — not an implemented provider

The intended iOS variant uses the native half of a Tauri mobile plugin. Its
Swift host would construct `AppleBleBackend`; Rust must not duplicate the
CoreBluetooth backend just to obtain a Rust constructor. This is a design
boundary, not a claim of current iOS Tauri setup, signing, lifecycle, or RF.

The intended one-time flow is:

```text
Rust plugin requests `assemble(ticket)` through PluginHandle
    -> Swift Tauri Plugin creates AppleBleBackend
    -> Swift calls generated Stable UniFFI attach_backend with that ticket
    -> Rust completes the canonical BleHub assembly and owns command/event flow
```

Mobile hooks are limited to assembly, permission, lifecycle, and disposal
coordination; they must not become per-BLE-operation JSON commands. The existing
canonical generated `attach_backend(ticket, backend)` already represents the
foreign backend ownership transfer. `swift-rs` launches macOS construction only;
it is not the trait/DTO transfer mechanism and does not prove iOS mobile behavior.

The generic constructor, `BackendAssembly` guard/registry, and exported
`attach_backend` are owned by `crates/blehub`. Tauri-specific launcher and host
coordination remain in the integration; neither a second attach protocol nor a
CoreBluetooth factory belongs in `crates/blehub-uniffi`. Mobile invocation
reentry, cancellation, permission/Activity lifetime, binding initialization,
and final single-image packaging must be independently validated before a
mobile provider is enabled. Android Central sequencing remains Phase 4.

## Options

| Option | Useful capability | Decision |
|---|---|---|
| Stable UniFFI | Existing foreign `BleBackend`, `BleEventSink`, object handles, generated Swift package, and the tested production image | **Keep as the only BLE ABI** |
| `swift-rs` | Links a Swift package and calls narrow `@_cdecl` functions with C-compatible values | Keep the one-function macOS launcher; not BLE DTOs, traits, ownership transfer, or operations |
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
implemented macOS path has only a narrow assembly launcher:

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
`integrations/tauri-plugin-blehub/plugins/blehub/assembly/macos/`, with the
reference host under `examples/tauri-central/`. The previous implementation
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

These decisions do not create new historical pass evidence. The coordinating
agent records final refactor tests; nonnative/demo tests and Apple selection
checks are distinct from native image linkage, Windows runtime, RF, and soak.
Tauri-specific remaining gates delay their provider/showcase only and **do not
block Phase 5 public ABI freeze**. See the
[integration README](../../integrations/tauri-plugin-blehub/README.md) for the
maintained interface and [example README](../../examples/tauri-central/README.md)
for flow policy and execution recipes.
