# Tauri Downstream Spike

This is a bounded feasibility probe for the approved Tauri integration shape.
It now includes the macOS in-process Stable UniFFI backend attach path, but it
is not a production plugin and remains intentionally excluded from the root
Cargo workspace.

## What it verifies

- a Tauri plugin crate can own the Rust `BleHub` assembly;
- macOS can transfer the production Swift backend through one assembly call;
- a non-macOS host can supply a deterministic fake backend through one assembly call;
- the sink only performs bounded channel handoff;
- a host-owned event pump projects `BleEvent` to a Tauri event;
- the visible frontend can run the basic initialize/scan/connect/GATT/I/O/
  notification/RSSI/disconnect flow and display each projected event;
- command acknowledgement remains synchronous while domain events are async;
- close/dispose is owned by the Rust plugin state;
- the fake backend is host-side test support and is not a plugin dependency.

## Run

The macOS-supported deterministic spike checks have one project-level entry
point:

```text
pixi run just spike tauri
```

This runs the headless React component tests, desktop/mobile Playwright tests,
the macOS Swift assembly test, the isolated Rust workspace tests, and the Tauri
host build. The embedded WebdriverIO suite is intentionally separate because it
launches the desktop application and applies to macOS, Linux, and Windows;
see `e2e/tauri/README.md`.

For an individual step, use the corresponding `pixi run just spike
tauri-ui-test`, `tauri-ui-visual`, `tauri-macos-assembly-test`,
`tauri-rust-test`, or `tauri-host-build` recipe.

Use the project-managed Rust toolchain:

```text
pixi run cargo test --manifest-path spikes/tauri-downstream/plugins/blehub/Cargo.toml
pixi run cargo check --manifest-path spikes/tauri-downstream/src-tauri/Cargo.toml
```

The frontend is a Vite-built React 19.2.8 app with the React Compiler enabled
through the current `@vitejs/plugin-react` preset and uses Astryx
`@astryxdesign/core`/neutral theme components. Build it before launching the
desktop app:

```text
pixi run npm install --no-audit --no-fund   # from spikes/tauri-downstream/ui
pixi run npm run build                      # from spikes/tauri-downstream/ui
```

The desktop app requires a graphical macOS session. On macOS its setup
constructs the production `AppleBleBackend`, attaches it through the generated
Stable UniFFI `attachBackend` entry point, and stores the resulting Rust-owned
`BleHub` in the plugin state. The visible UI detects this path and exposes
**Initialize CoreBluetooth**, with adapter events projected to the React event
log as `blehub://event`. On non-macOS hosts the visible **Run basic flow**
action remains the deterministic fake-backend contract probe.

The macOS attach and launch result is recorded in
[`docs/testing/reports/2026-09-05-tauri-apple-assembly.md`](../../docs/testing/reports/2026-09-05-tauri-apple-assembly.md).
This spike still must not be cited as Tauri-path macOS RF or P-Android RF
evidence.
