# Local generated-artifact retention

BleHub keeps build output and hardware evidence out of Git, but ignored does not mean automatically deleted. The project therefore uses explicit cleanup recipes rather than deleting work directories during ordinary builds.

## Ownership and policy

| Location | Contents | Default policy |
|---|---|---|
| `.work/react-native-ios-artifact-consumer/run-*` | Isolated npm tarball consumers, Pods, DerivedData and logs | Keep one newest run; older runs are disposable |
| `tests/consumers/react-native-internal/ios/{Pods,build}` | Source-linked RN host CocoaPods and Xcode output | Disposable and rebuilt by the next recipe |
| `tests/consumers/react-native-internal/android/app/{build,.cxx}` | Android Gradle/CMake output | Disposable and rebuilt by the next recipe |
| `tests/consumers/react-native-internal/node_modules` | Local RN host dependencies | Disposable; `npm install` recreates it |
| `.work/archive-local` | Local copies made while archiving retired hosts | Disposable, but requires explicit all-cache cleanup |
| `.work/cargo-target` | Rust target/build cache | Disposable, but deleting it causes a large rebuild |
| `.pixi` | Pixi environments and package caches | Not owned by the BleHub cleanup recipe; use Pixi's own cache management |
| `.work/staged` and current platform evidence | Staged production inputs or active reports | Not removed by generic cleanup |
| `.work/blehub-uniffi-apple-*` and matching `.lock` files | Rebuildable Apple package work/staging and advisory package-builder locks | Disposable; package recipes serialize access and recreate work output |

A successful report is stored under `docs/testing/reports/`; the ignored run directory is supporting local evidence, not a permanent release artifact. Reports must not claim that a local run directory is retained after it has been pruned.

## Commands

Report without changing anything:

```text
pixi run just work-report
```

Remove stale RN runs and generated RN host output while retaining one newest artifact-consumer run:

```text
pixi run just work-clean
```

Also remove local retired-host copies and the rebuildable Cargo target cache:

```text
pixi run just work-clean-all
```

The cleanup tool only acts below the explicitly listed generated roots, refuses negative retention, does not follow symlinks, and never reads or changes credentials. It does not run automatically as part of `build`, `test`, RF, or package recipes so that a failed hardware run cannot silently lose its logs.

After cleanup, `work-report` should be rerun. A large `.pixi` directory is a separate tool-environment cache and should only be pruned through Pixi-aware commands after confirming which environments will need to be recreated.
