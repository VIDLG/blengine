# Tauri production-directory migration and single-image audit — 2026-09-07

## Result

The maintained Tauri implementation was migrated out of the active spike path:

```text
integrations/tauri-plugin-blehub/
examples/tauri-blehub-internal/
```

The former workspace is archived at `spikes/archive/tauri-downstream/`. No
active Just, integration, or example path refers to `spikes/tauri-downstream`.

The migrated deterministic Rust, Swift, host-build, UI, and native WebDriver
checks passed. The migrated RF/fault/pressure suite was subsequently rerun from
the formal path and passed. The 30-minute continuous RF soak remains an
explicit release-candidate gate and was intentionally not run during this
development session.

## Ownership boundary

- `integrations/tauri-plugin-blehub` owns the Tauri Rust plugin and the narrow
  macOS Swift assembly launcher.
- `examples/tauri-blehub-internal` owns the reference window, frontend, permissions,
  and lifecycle wiring.
- The plugin and example continue to use the canonical `BleHub` /
  `BleBackend` / `BleEventSink` contract and do not define a second BLE
  protocol.
- Only `crates/blehub-uniffi` owns the production Stable UniFFI native image.
- The archived spike is not referenced by active recipes or production paths.

## Static single-image audit

The source/build graph was checked across `crates`, `platforms`,
`integrations`, `examples`, `tests/ffi`, `tests/hil`, and `tests/consumers`.
The only production native-image owner is `crates/blehub-uniffi`; the migrated
Tauri plugin depends on the canonical `crates/blehub` contract and the macOS
assembly consumes the staged production Apple package.

The migrated Tauri host was built as:

```text
.work/cargo-target/debug/blehub-tauri-internal
```

Observed results:

| Check | Result |
|---|---:|
| Host architecture | arm64 Mach-O |
| `blehub_tauri_macos_assemble` symbols | 1 |
| `blehub_tauri_macos_assembly_probe` symbols | 1 |
| `libblehub_uniffi` dynamic dependency in `otool -L` | 0 |
| Production Apple `libblehub_uniffi.a` slices | 3 |

The three Apple slices are the expected macOS, iOS simulator, and iOS device
artifacts. The Tauri host therefore has one tested static assembly boundary and
no second `libblehub_uniffi` dynamic library. This is a repository/source and
migrated-host audit; it does not claim a Tauri Android artifact, because no
Tauri Android integration is part of this migration.

## Zed and rust-analyzer workspace loading

Zed was also configured at `.zed/settings.json` to load only the active Cargo
workspaces and to launch rust-analyzer through the project-managed Pixi
environment:

```json
"binary": {
  "path": "pixi",
  "arguments": ["run", "rust-analyzer"]
}
```

The initial `failed to load workspaces` log had two separate causes: the
archived Tauri workspace has stale paths, and the system environment used by
Zed did not contain `cargo` or `rustc`. Active manifests load successfully with
Pixi; the archive remains intentionally excluded from editor workspace loading.
After changing this setting, reload the project or restart rust-analyzer so the
initialization options and binary environment are applied. The Pixi `rust` feature
also includes the matching `rust-src` package, and `pixi install` has provisioned
it under `.pixi/envs/default/lib/rustlib/src/rust/library` so rust-analyzer can
load the standard library without relying on a separate rustup toolchain.

## Validation

The active native WebDriver spec was restored to
`examples/tauri-blehub-internal/e2e/tauri/specs/basic-flow.e2e.js`; the archived copy was
not used as an active test path. The E2E Just recipe now installs its locked Node
dependencies before invoking WebdriverIO.

Passed:

```text
pixi run cargo test --locked --manifest-path integrations/tauri-plugin-blehub/Cargo.toml --workspace
pixi run cargo test --locked --manifest-path examples/tauri-blehub-internal/Cargo.toml --workspace
pixi run -e apple-ci swift test --package-path integrations/tauri-plugin-blehub/plugins/blehub/assembly/macos
pixi run just tauri::tauri-ui-build
pixi run cargo check --locked --features wdio --manifest-path examples/tauri-blehub-internal/src-tauri/Cargo.toml
pixi run npm --prefix examples/tauri-blehub-internal/ui run test:component
pixi run just tauri::tauri-host-build
pixi run just tauri::tauri-native-e2e
pixi run just tauri::tauri-native-rf
pixi run just --list --list-submodules
```

The formal native E2E suite passed 6/6 without RF. The migrated RF run also
passed 6/6 in approximately 1 minute 23 seconds against Android Peripheral
`3ac5cc09`, covering the 12-step CoreBluetooth flow, repeated clean flows,
10-cycle scan/connect/GATT/read/disconnect pressure, in-flight radio-loss fault
handling, active-subscription passive-disconnect recovery, and final disposal.
The fixture log contains the expected stale-notification negative check and a
transient `STARTING` readiness response during radio recovery; the control
sequence recovered and all assertions passed.

The migrated plugin tests executed 3 tests, the macOS assembly tests executed
2 tests, and the migrated UI component suite executed 3 tests.

## Remaining release gates

- Run one 30-minute continuous Tauri RF soak on the frozen release candidate.
- Do not treat the existing approximately 12-minute spike soak as the migrated
  release soak.
- Overnight soak remains optional evidence unless the release requirement
  explicitly promises overnight/background operation.
