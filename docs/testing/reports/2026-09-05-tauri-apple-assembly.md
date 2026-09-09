# Tauri macOS Apple backend assembly

- Date: 2026-09-06
- Scope: in-process Tauri desktop assembly of the production Swift
  `AppleBleBackend`
- Result: assembly, launch, embedded WebdriverIO initialization/disposal, the
  macOS Tauri-to-P-Android RF workflow, repeated clean UI flows, bounded
  pressure, active-subscription recovery, post-disconnect silence, extended
  repeated-suite soak, and repeated in-flight discovery fault isolation passed;
  longer-duration soak remains outstanding

## Implemented boundary

The Tauri macOS path now uses a one-shot assembly ticket:

```text
Rust Tauri plugin
    -> register assembly ticket and bounded event sink
    -> @_cdecl Swift launcher(ticket)
    -> Swift constructs AppleBleBackend
    -> generated Stable UniFFI attachBackend(ticket, backend)
    -> Rust calls canonical BleHub::new
    -> Rust plugin takes and owns Arc<BleHub>
```

The ticket registry is limited to construction. It is not used for event
routing, device identity, GATT state, or BLE operation IDs. BLE commands and
events continue to use the existing typed `BleHub`/`BleBackend`/`BleEventSink`
contract.

On attach failure, the Swift launcher performs local cleanup and the canonical
Rust constructor retains its existing rollback behavior. On Tauri setup failure,
the ticket is cancelled. `BleHub::dispose` remains the owner of final backend
disposal after a successful transfer.

## Validation

The following commands passed on the project Mac mini:

```text
pixi run -e apple-ci python -m tools.apple.package_uniffi
pixi run -e apple-ci swift test --package-path spikes/tauri-downstream/plugins/blehub/assembly/macos
pixi run cargo check --locked --manifest-path spikes/tauri-downstream/src-tauri/Cargo.toml
pixi run cargo test --locked --manifest-path spikes/tauri-downstream/Cargo.toml --workspace
pixi run cargo clippy --locked --workspace --all-targets -- -D warnings
pixi run just binding snapshot-check
pixi run npm --prefix spikes/tauri-downstream/ui run test:component
pixi run just spike tauri-host-build
```

Swift assembly tests: 2 passed, including rejection of an unknown assembly
ticket. The generated production Swift binding contains `attachBackend`; the
production package symbol and architecture checks passed. The Tauri host check
and build passed. The Rust Tauri workspace tests passed (3 tests), and the frontend component
and headless Playwright suites passed (3 and 3 tests respectively).

The actual macOS Tauri executable was launched with:

```text
pixi run cargo run --manifest-path spikes/tauri-downstream/src-tauri/Cargo.toml
```

It reached the running process state for the bounded 30-second observation
window without a Tauri setup panic, Swift assembly error, CoreBluetooth
construction error, or native loading error. On macOS the host selects
`init_with_assembly`; non-macOS hosts retain the deterministic fake backend.

After adding the host ACL permission for the local `blehub` plugin, the real
macOS window was launched and its nested WebView Accessibility button
`Initialize CoreBluetooth` was clicked. The UI received both
`adapter_snapshot_changed` and `command_completed`, and displayed:

```text
state=PoweredOn;scan_authorization=Authorized;connection_authorization=Authorized
```

This confirms the command crossed the Tauri IPC/plugin route into the attached
production CoreBluetooth backend. The visible macOS UI reports the selected
`apple-corebluetooth` path and no longer presents the fake complete flow as RF
evidence.

## Embedded WebdriverIO UI gate

The macOS E2E suite now uses WebdriverIO's `embedded` provider. The host
registers the test-only `tauri-plugin-wdio-webdriver` server when the explicit
`wdio` Cargo feature is enabled. Basic element operations do not require the
companion `tauri-plugin-wdio`; the spec explicitly selects the current window,
which skips the service's optional focus-recovery IPC path. No WDIO frontend
plugin is included in the ordinary frontend build.

The Tauri plugin now exposes thin typed adapters for scan, scan stop, connect,
disconnect, GATT discovery, characteristic read/write, notification control,
and RSSI. These adapters translate JSON request DTOs into canonical BleHub
commands; event projection includes machine-readable IDs, GATT characteristics,
and values so the React caller can correlate callbacks without duplicating
backend state. The macOS UI exposes a `Run CoreBluetooth flow` action after
initialization and drives those commands from the projected event stream.

The real WebDriver click, `state=PoweredOn`, authorized initialization,
`adapter_snapshot_changed`, `command_completed`, and `disposed` passed. The
service's previous 5-second window-state warnings are avoided by the explicit
current-window selection. WDIO 1.3.0 still emits a non-fatal mock-store cleanup
warning after deleting the session; this occurs after the assertions and does
not affect the result.

The headless Playwright suite also passes the complete mocked Apple flow twice,
verifies that initialization is not dispatched again, and can expand an event
row to inspect its structured JSON `data`. The visible UI uses Astryx
`Collapsible` for event payload inspection. After initialization, the UI shows a
non-actionable `CoreBluetooth ready` status instead of a second initialize
button; after a failed flow it fails closed and requires hub disposal/relaunch.
The production Android fixture profile is correctly split: `FFF1` is the
read/write characteristic and `FFF2` is the Notify/Indicate characteristic.

The first opt-in RF implementation incorrectly dispatched `initialize` a second
time and the Apple backend rejected that invalid lifecycle transition. The next
attempt selected the read/write `FFF1` characteristic for notification and was
correctly rejected because it advertises neither Notify nor Indicate. The flow
was changed to select `FFF2`, and the RF runner was changed to issue the managed
fixture command only after the real `notification_state_changed` callback. This
also avoids waiting for `notifications_enable` before the notification value
that is required to mark that step complete.

The corrected RF run passed:

```text
fixture: Android Peripheral 3ac5cc09
command: BLEHUB_TAURI_RF=1 pixi run npm --prefix spikes/tauri-downstream/e2e/tauri test
result: 6 passing (about 1m 25s) per suite run
fault-enabled reruns: three consecutive complete runs, 18 passing tests total
repeated-suite soak: three earlier complete runs, 15 passing tests total
extended soak: ten earlier complete runs, 50 passing tests total, about 12 minutes
notification trigger: android-notify deadbeef
flow: 12/12, including scan, connect, GATT discovery, read, both writes,
       notification enable/value, RSSI, notification disable, disconnect
repeated flows: two additional complete UI flows after clean disconnect
bounded pressure: ten unique scan/connect/GATT-discovery/read/disconnect cycles
recovery: Android radio disable, command-less passive termination, radio restore,
          fixture resume, rescan, reconnect, GATT discovery, read, disconnect
in-flight fault: discovery dispatch followed by asynchronous Android radio loss
                produced an error terminal and connection lifecycle termination
post-disconnect silence: restored fixture notification produced no event for the
                          retired connection ID
```

This is the first valid Tauri-to-P-Android RF workflow and recovery pass. It
proves the real desktop Tauri UI, Tauri plugin typed adapters, canonical Rust
`BleHub`, Stable UniFFI assembly, Swift/CoreBluetooth backend, Android
Peripheral notification control, and the recovery path crossed the tested
boundary. When Android Bluetooth is disabled, CoreBluetooth reports the
command-less connection termination as a native connection error; the recovery
assertion accepts that terminal error as well as a plain `Disconnected` result.
The pass does not prove longer-duration soak through Tauri; the ten-run result
is an extended bounded soak signal, not 30-minute or overnight evidence.

The built Tauri macOS host also passed the scoped native-image audit:
`assembly_entries=1`, `assembly_probes=1`, `attach_backend_symbols=1`, and
`native_dynamic_dependencies=0`. The production Apple package independently
reported valid device/simulator/macOS slices and no obsolete test-harness
symbols. This proves the tested Tauri host assembly boundary, not every
production consumer in the repository.

## Remaining gates

This result does not claim:

- longer-duration soak evidence through Tauri;
- a repository-wide link-map proof that every production consumer has exactly
  one Stable UniFFI native image;
- production plugin publication or promotion out of `spikes/`.

The existing production Apple backend and its standalone macOS/P-Android RF
evidence remain valid, but they are not automatically Tauri-path evidence.
The next gates are longer-duration soak and the repository-wide single-image
artifact/link audit before promoting this fixture to `integrations/` or
`examples/`.
