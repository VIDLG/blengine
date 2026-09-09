# Single-image production ABI migration — 2026-09-03

## Result

**PASS for the completed current-candidate desktop runtime, production-package, deterministic Apple, generic Apple build, and Android package-assembly gates.**

**BLOCKED for physical iPhone signing/install/runtime and physical Android ART execution. No RF gate was run for this migration.**

This result applies only to the current unfrozen candidate. It is not a Windows runtime rerun, an Android ART pass, an iPhone runtime pass, an RF pass, or the Phase 5 production ABI freeze. Phase 5 still requires the complete Windows + Apple + Android candidate and all applicable runtime, artifact-only consumer, lifecycle, performance, and RF gates to be rerun from that same candidate.

## Migration boundary

The maintained production and test graph now has one Stable UniFFI production image:

- `crates/blehub` defines and exports the production `BleHub`, foreign `BleBackend`, foreign `BleEventSink`, direct ingress, commands, events, and errors;
- `crates/blehub-uniffi` owns the only final native image, bindgen entry, and production package staging;
- `tests/ffi/harness` was deleted;
- Swift and Kotlin code under `tests/ffi/runtime` implements test backend and sink objects directly and exercises the exported production API;
- Apple HIL consumes the staged production package and uses a direct Swift sink;
- the Apple HIL and macOS runtime probe share only a pure-Swift helper, not a Rust test component or second native image.

The completed gates below therefore replace the prior statement that all post-migration production runtime/package gates were still unrun. Historical pre-migration reports remain historical and were not modified.

> Later maintenance decision: the project no longer maintains a Windows desktop Swift lane, Windows-managed Swift root/bootstrap, or GitHub-hosted macOS workflow. References in this report to missing Windows Swift evidence describe this report's historical boundary, not future required work. Maintained Apple runtime, package, backend, and HIL gates run on the project real Mac mini through local Pixi + Just, and Phase 5 requires macOS/iOS Swift evidence rather than a Windows Swift supplement.

## Completed validation

| Area | Invocation | Result | Boundary |
|---|---|---|---|
| Apple production package | `pixi run -e apple-ci just ffi apple-package` | Passed the iOS device, iOS simulator, and macOS slices plus production symbol checks | Does not prove signing, installation, launch, CoreBluetooth, RF, or independent final release consumers |
| Swift production runtime | `just ffi swift-macos` | Passed direct execution of the exported production `BleHub` with Swift test backend/sink implementations, including the event catalog and concurrency, error, and lifecycle cases | macOS host evidence only; not iOS or Windows runtime evidence |
| Kotlin/JVM production runtime | `pixi run just ffi kotlin-jvm` | Passed direct execution of the exported production `BleHub` with Kotlin test backend/sink implementations, including 29 checksum checks and one contract check | Desktop JVM evidence only; not Android ART, Android BLE, or Windows runtime evidence |
| Apple deterministic tests | `just apple test` | 71 tests passed | Deterministic Swift/CoreBluetooth mapping and lifecycle evidence; not RF |
| Generic iOS builds | `just apple build-ios` | Unsigned generic iOS simulator and device builds passed | Compile/link evidence only; not signing, installation, launch, or device runtime |
| macOS initialization | `just apple runtime-macos` | The production-package initialization path completed; the terminal ad-hoc run observed adapter `Unavailable` and authorization `NotDetermined` | This is not a `PoweredOn + Authorized` result and did not enter RF |
| Rust workspace and lint | Workspace Cargo tests and lint | Passed | Static/unit evidence only |
| iPhone HIL project/build | Tracked Xcode project generation and unsigned generic iOS build | Passed | Does not prove device signing, installation, launch, runtime, or RF |
| Android production packaging | Production AAR assembly/publish and macOS-hosted NDK native-slice builds | Passed after replacing the Windows-only NDK linker assumption | Package/build evidence only; no ART or BLE runtime |

The Apple package contains these expected native slices:

- iOS device: `arm64`;
- iOS simulator: `arm64`, `x86_64`;
- macOS: `arm64`, `x86_64`.

## Physical-device blockers

### iPhone

The iPhone doctor remains blocked because the login keychain is unavailable to noninteractive `codesign`. The current migration therefore has no recorded iPhone signing, installation, launch, runtime, or RF pass. Xcode project generation and unsigned generic builds do not close those gates.

### Android ART

The production AAR was assembled and published, and the required Android native slices built on macOS after removing the Windows-only NDK linker assumption. Physical ART tests did not run: APK installation timed out twice with `ShellCommandUnresponsiveException` while the phone was locked.

Those installation failures are infrastructure/device-state blockers, not ART runtime passes or failures. No Android production BLE backend or RF claim is made by this report.

## Evidence boundaries

This migration verifies that the current macOS candidate can:

- build the single production Apple image/package for all required Apple slices and pass symbol checks;
- execute direct Swift and desktop Kotlin/JVM runtime consumers against the exported production `BleHub`, foreign backend, foreign sink, and direct ingress graph;
- retain the 71-test deterministic Apple result and unsigned generic iOS builds;
- generate and compile the unsigned iPhone HIL project against the production package;
- assemble and publish the production Android AAR and build its native slices from macOS.

It does **not** verify:

- a current-candidate Windows Swift or Kotlin runtime rerun;
- Android ART loading, callback execution, lifecycle, release/minified behavior, or physical-device execution;
- iPhone signing, installation, launch, CoreBluetooth runtime, or RF;
- any RF behavior for this migration;
- final artifact-only consumers for every platform;
- the complete Phase 5 performance/package-size matrix or production ABI freeze.

## Remaining required work

1. Unlock the login keychain through the explicit human-only workflow, then rerun iPhone doctor, signing, installation, launch, device runtime, and RF gates.
2. Rerun physical Android ART after the phone is unlocked and APK installation is responsive; record whether the production AAR loads and the direct Kotlin backend/sink runtime catalog completes.
3. Complete the independent production artifact-only Apple and Android consumers and remaining lifecycle/performance/package-size cases required by the Plan.
4. Do not infer a current Windows runtime pass from the macOS Swift or Kotlin/JVM results.
5. Rerun every applicable gate from the final unified Windows + Apple + Android candidate before the Phase 5 ABI freeze.
