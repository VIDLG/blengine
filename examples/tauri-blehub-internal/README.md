# BleHub Tauri internal example

This standalone Cargo workspace is the repository-owned internal low-level BleHub test app for
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
integration work. iOS uses the standard Tauri mobile entry point in
[`src-tauri/src/lib.rs`](src-tauri/src/lib.rs). macOS 13 and iOS 16 share the
plugin's `assembly/apple-assembly` Swift package; platform-specific build owners
do not create a second backend or BLE ABI.

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

On macOS, [`macos-owner.js`](e2e/tauri/macos-owner.js) and
[`launch-app.swift`](e2e/tauri/launch-app.swift) launch the actual app bundle via
standard `NSWorkspace`/LaunchServices and retain its `NSRunningApplication`.
WDIO uses `services: []` and ordinary WebDriver Classic against the same Rust
embedded server. Before connecting, the owner verifies the temporary IPv4
loopback port belongs to the returned app PID with `lsof` and checks `/status`
readiness. Stdin EOF, including after the Node parent receives `SIGKILL`, releases
the lease and triggers termination of that exact instance, with bounded
`forceTerminate` fallback. There is no `osascript` or `pkill` scripting.

Windows/Linux retain `@wdio/tauri-service`. Its missing official attach option
does not prevent macOS automation: the new owner replaces npm-service process
ownership, not the WebDriver protocol, and does not bypass TCC. See the
[native E2E README](e2e/tauri/README.md) for lifecycle and readiness boundaries.
The dedicated `tauri::macos-owner-test` entry builds the bundle and owner before lifecycle tests, without requiring a
successful native E2E run or issuing BLE commands.

For human Bluetooth authorization, build and launch the same debug bundle with
WDIO instrumentation:

```text
pixi run just tauri::macos-bundle
pixi run just tauri::macos-authorize
```

`macos-authorize` also builds the bundle. In the visible app, manually click
**Initialize**, allow Bluetooth when prompted, then quit before running WDIO.
Launching is not an authorization/runtime test or a TCC bypass. If signing needs
the login keychain, the human-only step is
`pixi run just apple unlock-login-keychain`; do not give a password to an agent.
The old direct-spawn path was not merely waiting for permission: system logs
attributed responsibility to `sshd-keygen-wrapper` and recorded
`Policy disallows prompt` for Bluetooth. With the LaunchServices owner, the
2026-09-09 15:38:30 TCC log attributes responsibility to the app and records
`AUTHREQ_PROMPTING` / `display_prompt`. That run still timed out at 30 seconds
while awaiting the user. After the user allowed Bluetooth, native WDIO E2E
passed **2 tests / 4 skipped**, without RF. The macOS Tauri TCC blocker is now
closed; the earlier timeout remains historical evidence, not current state.

## Official iOS project and unsigned builds

The official Tauri CLI `2.11.4` is pinned by this example root's `package.json`
and `package-lock.json`, with `scripts.tauri`; it no longer belongs to `ui/`.
Just runs the standard `npm run tauri -- ...` from the example root so the
generated Xcode build callbacks can rediscover the CLI. There is no custom
Tauri `project.yml` template or replacement Xcode callback script.

```text
pixi run just tauri::ios-init
pixi run just tauri::ios-build aarch64-sim
pixi run just tauri::ios-build aarch64
```

`ios-init` explicitly uses `--skip-targets-install`: Rust targets remain
Pixi-managed. `ios-build` uses `--no-sign --archive-only` for the complete debug
app, not just a Rust or Swift compilation check. SDK/toolchain provisioning and
development signing remain explicit, separate concerns.

**Serialize Apple preparation and its consumers.** Recipes using `apple-prepare`
share the same staging directory, including `.work/staged/apple-production`;
do not run concurrent prepare/rebuild operations while another build consumes
it. A coordinating-session parallel run caused a missing-directory failure.
Serial recovery restored staging; the iOS signing retry then still failed with
`errSecInternalComponent`, a distinct signing blocker rather than an RF result.

Do not confuse the two Apple project files:

- `tests/hil/apple/ios/project.yml` is the tracked, manually maintained XcodeGen
  input for the **independent native Swift HIL**, not this Tauri app.
- `src-tauri/gen/apple/project.yml` is official Tauri-generated, ignored output.
  Do not hand-edit or check it in. `ios init` reuses an existing template; after
  relevant configuration changes, preserve the old `gen/apple` directory outside
  that generation path, then regenerate with `ios-init` rather than patching it.

## Current evidence boundary

The coordinating session reports the following results; this documentation-only
update does not rerun them or replace its detailed report:

| Lane | Current result |
|---|---|
| Shared Apple assembly XCTest on real macOS | Both registered tests passed; the missing manifest `testTarget` was fixed, so this is not a zero-test pass |
| Tauri iOS `aarch64-sim` and `aarch64` | Both complete unsigned app builds passed; final plist minimum OS and `LC_BUILD_VERSION` are 16.0; each slice has all 123 BleHub UniFFI definitions exactly once, one assembly entry, zero old harness symbols, and zero additional BleHub dylibs |
| Tauri macOS WDIO demo / owner | New LaunchServices path: demo 3 passed / 3 skipped; owner lifecycle scenarios passed. Neither is native BLE/RF evidence |
| Tauri macOS native E2E | After user authorization: 2 passed / 4 skipped, no RF; TCC blocker closed |
| Historical Tauri macOS → Android Peripheral RF | Pre-nRF evidence only: 6 passed / 0 skipped before the provider archive migration; it does not prove the current nRF lane |
| Current Tauri macOS → nRF52840 RF | Passed: 6 / 0 skipped in about 1m10s; see the dated nRF report. This is internal low-level BleHub evidence, not BLeng/RN evidence |
| Native Apple macOS → Android Peripheral HIL | Historical pre-archive evidence only; the active Apple RF provider is nRF52840 |
| Native Apple iOS HIL, this round | Serial retry after staging recovery still failed at `codesign` with `errSecInternalComponent` before RF; historical iOS I/O RF results are not a current full-matrix pass |
| Tauri iOS | Runtime, development signing, and RF not run |
| Tauri Windows | Not tested this round |

## Separate Tauri RF gates

The standalone nRF52840 RF workflow and explicit release-candidate soak
are separate hardware gates:

```text
pixi run just tauri::tauri-native-rf
pixi run just tauri::tauri-native-rf-soak 5
```

The current RF setup starts the nRF52840 direct provider before the first scan.
The provider owns BTP/serial access, consumes notification and passive-disconnect
requests from the coordination directory, and is stopped during teardown.
No Android radio control or archive provider is part of this lane. The
post-migration nRF RF command passed **6 / 0 skipped in about 1m10s** after fixing
nRF-compatible one-byte write payloads and restoring advertising after Central
logical disconnect. All 19 UI component tests passed, and the provider was
ready/stopped under explicit single-serial ownership. See the [dated nRF report](../../docs/testing/reports/2026-09-13-tauri-macos-nrf-rf.md).

The five-minute soak is not an ordinary development check; it is a short
continuous regression, not release-duration soak evidence. It has not been
completed in this follow-up because the longer attempt was stopped manually.
No Windows run is claimed. Tauri validation never substitutes for production
runtime/package/backend validation, and incomplete Tauri gates **do not block
Phase 5 public ABI freeze**.
