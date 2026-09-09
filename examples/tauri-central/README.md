# BleHub Tauri Central example

This standalone Cargo workspace is the maintained desktop and iOS reference app for
[`integrations/tauri-plugin-blehub`](../../integrations/tauri-plugin-blehub/).
The app owns the window, frontend, capability grants, and example flow policy;
the plugin owns native assembly, the Rust `BleHub`, typed IPC, event transport,
and cleanup. The app build script only calls `tauri_build::build()`; official
command permissions belong to the plugin, not to the example.

## Native by default; demo only by choice

Default features install `tauri_plugin_blehub::init()`: plugin-owned shared
Swift `AppleBleBackend` on macOS and iOS, Rust `WindowsBackend` on Windows.
Unsupported targets or disabled native providers fail setup explicitly. Windows
wiring is present but this refactor has no Windows runtime/RF validation; do not
infer a pass from source selection or old backend reports. Android remains future
integration work. iOS uses the standard Tauri mobile entry point in `src/lib.rs`.

For a deterministic demonstration, explicitly select
`--no-default-features --features demo`. This uses the example's own
[`DemoBackend`](src-tauri/src/demo_backend.rs) via `with_backend`, exposing the
same capabilities and official IPC as native mode. There is no automatic
non-macOS fake fallback, `backend_mode`, `CallbackDriver`, or probe command.
Neither example nor plugin production dependencies use `tests/`; plugin tests
may use testkit as a dev-dependency.

## What the flow actually waits for

[`ui/src/flow.js`](ui/src/flow.js) owns orchestration, not BleHub core:

1. Register a waiter and fresh caller-owned command ID before `invoke`, since a
   provider may emit synchronously. Allocate fresh scan/connection/GATT/
   subscription IDs as well. All `u64` IDs cross JSON as decimal strings; the
   frontend allocator uses `BigInt`, never rounded JavaScript `Number` IDs.
2. Require successful dispatch **and** successful terminal plus each step's
   required domain evidence. An acknowledgement alone never marks a BLE step
   complete. Match typed kind, command/session IDs, state, and error fields;
   `detail` is only for display. Timeout and abandonment are app policy.
3. Before scanning, require initialization terminal and an adapter snapshot with
   `powered_on` and both authorizations `authorized | not_required`. Core
   initialization itself can complete with a powered-off or unauthorized
   snapshot; this readiness requirement belongs to the example.
4. Scan using the actual single-provider service filter and accept observations
   only from that session. CoreBluetooth may hide service UUID fields despite a
   native filter match. Do not invent `advertised_services`; verify the real
   fixture service, characteristic properties, and CCCD during GATT discovery.
5. Read, write-with-response, and verify exact readback. For no-response writes,
   terminal means local submission only; the caller-owned delay and subsequent
   read are a separate command and independent fixture-application evidence,
   not a stronger interpretation of the write terminal.
6. Require notification state and value evidence, use capabilities for RSSI,
   then wait for disable/disconnect domain and terminal outcomes. Unsupported
   RSSI is visibly skipped rather than fabricated as success.

The typed projection includes structured errors, snake_case states, advertisement
manufacturer/service data, and GATT descriptors/duplicate-UUID instances. See
[the integration JSON contract](../../integrations/tauri-plugin-blehub/README.md#ipc-and-json).
The flow is a reference application, not a replacement for shared conformance
or native backend HIL.

## Lifecycle constraints

Close disables and drains entered Tauri emission, discards buffered events,
aborts the event task, and performs Hub cleanup outside the emission lock;
async close also awaits its taken task handle. Overflow fails the transport
closed, best-effort reports a transport error, and triggers cleanup; it is not
reliable delivery or a BLE success. App exit cleanup uses actual `Exit`, not
cancellable `ExitRequested`, and plugin removal has an `on_drop` fallback.

Only one plugin installation is allowed per app. Closing/removing it does not
allow reinstallation into the same app; restart requires a new app and Hub.
A WebView listener/projection rebuild is not a new Hub and must not reuse IDs
from its lifetime. Rust event listeners must not synchronously tear down the
plugin from its `emit` stack; schedule follow-up work instead.

## Checks and execution boundaries

The narrow Rust recipe needs no native package or BLE hardware and explicitly
selects the two separate workspaces:

```text
pixi run just tauri::tauri-rust-test
```

It tests the plugin with `--no-default-features` and the example with
`--no-default-features --features demo`. It is not a default-native build or RF
gate. Final refactor counts/results belong to the coordinating agent's record;
this documentation update does not rerun or promote historical passes.

The broader deterministic macOS lane includes UI checks, Swift assembly, and
native host build, with their native package/toolchain prerequisites:

```text
pixi run just tauri::tauri
```

Native WebView automation launches the desktop application and is separate:

```text
pixi run just tauri::tauri-native-e2e
```

The maintained suite uses the embedded WebDriver provider. The historical
standalone `tauri-driver` macOS limitation is not evidence that this suite ran.

The managed Android Peripheral RF workflow and explicit release-candidate soak
are separate hardware gates:

```text
pixi run just tauri::tauri-native-rf
pixi run just tauri::tauri-native-rf-soak 30
```

The 30-minute soak is not an ordinary development check. No RF or Windows run
is claimed by this documentation change. Tauri validation never substitutes
for production runtime/package/backend validation, and incomplete Tauri gates
**do not block Phase 5 public ABI freeze**.
