# Consumer ownership and migration

Consumers validate downstream package boundaries. They do not define a second BLE API or own
production recovery policy. All maintained native consumers use the corresponding published or
staged production image; an RN source-checkout host does not replace an artifact-only consumer.

## Current maintained lanes

| Location | Boundary under test | UI role |
|---|---|---|
| `android/blehub/` | Published BleHub/Android backend artifacts and native conformance | No product UI assertions |
| `android/bleng/` | Published combined BLeng/Supervisor UniFFI image | No product UI assertions |
| `apple/blehub/ios`, `apple/blehub/macos` | Apple BleHub package and production backend consumers | Native test harness, not RN UI |
| `apple/bleng/ios`, `apple/bleng/macos` | Combined Apple BLeng package consumers | Native test harness, not RN UI |
| [`react-native-bleng/`](react-native-bleng/README.md) | Formal JS API and its Nitro/native bridge | **Only formal RN UI App** |
| [`react-native-internal/`](react-native-internal/README.md) | Unmigrated private probe, lifecycle diagnostics and RF ledgers | Transitional legacy workbench; scheduled for removal |

The maintained native layout is now platform → package line → OS target where needed. Android
`blehub` and `bleng` remain independent Gradle roots, not modules of a shared consumer project.
An Apple native consumer may need an executable bundle for the OS/runtime without testing a
user interface; “no UI semantics” does not mean “no executable target”.

### Apple artifact package responsibilities

The BleHub native consumers resolve two staged Swift packages, not checkout backend sources:

- `.work/staged/apple-production`: generated UniFFI bindings and the sole native XCFramework.
- `.work/staged/apple-backend-production`: `BleHubApple` Swift backend sources and a dependency
  on that same UniFFI package. It contains no second native image.

`apple consumer-macos`, `apple consumer-ios` and the explicitly authorized `apple consumer-ios-device`
all prepare both packages through the common Just prerequisite. Backend staging is owned by
[`stage_backend.py`](../../tools/apple/stage_backend.py), with the manifest template under
[`platforms/apple/packaging`](../../platforms/apple/packaging/Package.swift.in).
The backend resource contract checks exact bound/disposed snapshots and lease reuse without
initializing CoreBluetooth or scanning. iPhone receipts additionally require this run's unique ID,
`backend_resources=passed` and exactly one successful terminal; old receipts or a lone final
`passed` are not sufficient. macOS execution and unsigned iOS compilation do not prove iPhone
runtime or RF. Hardware-free packaging/receipt regressions use `pixi run just apple test-consumer-tools`.

## Target source layout

```text
consumers/
├── android/
│   ├── blehub/
│   └── bleng/
├── apple/
│   ├── blehub/
│   │   ├── ios/
│   │   └── macos/
│   └── bleng/
│       ├── ios/
│       └── macos/
├── react-native-bleng/          # one formal RN App, native bridge tests and shared UI E2E
│   ├── src/
│   ├── android/
│   └── ios/
└── archive/                    # historical lanes, not active gates
```

The remaining transitional source root outside this final tree is `react-native-internal`; it
must lose its remaining probe/RF responsibilities before deletion.

The separate `apple/bleng-rn-ios` source consumer has been retired. Its useful validation is now
owned by `react-native-bleng/ios/PlatformContract` (isolated staged SwiftPM compilation, no Nitro)
and the formal App/native-test build (full CocoaPods/Nitro graph). The canonical entry is
`pixi run just react-native formal-host-ios-build`. Podspec dependency validation remains ahead
of these builds; the host must not mask a missing formal package dependency. This is a
source/local-package compile gate, not a clean npm-tarball packaging proof.

Migration of native source/build roots:

| Previous root | Maintained root |
|---|---|
| `android/` (old Gradle root) | `android/blehub/` |
| `android-bleng/` | `android/bleng/` |
| `apple/ios/`, `apple/macos/` | `apple/blehub/ios/`, `apple/blehub/macos/` |
| `apple/bleng-ios/`, `apple/bleng-macos/` | `apple/bleng/ios/`, `apple/bleng/macos/` |
| `apple/bleng-rn-ios/` | Formal RN `ios/PlatformContract` + App/native-test compile gate; duplicate static Nitro consumer removed |

Active tools, Just recipes and staged artifact references use the new roots, with no compatibility
shim or source dependency added. Ignored output may remain at an old location; it is not an active
consumer or an alternative source tree. Historical reports retain their original commands and
paths; use this table to locate the maintained source, not to reinterpret their candidate results.

The native BLeng artifact consumers additionally exercise `Supervisor`'s context-fenced typed
operation entry points through the generated Swift/Kotlin bindings. Rust owns initialization;
consumer fixtures verify the ten-operation chain, stale input rejection, existing handle results
and reader/resource retirement. These stay in the existing native consumers, not a new App or a
raw API added to the RN package. The same native readers now also check owner-bound scan
projection envelopes and revocation, without replacing their original raw-event/terminal checks.
Required event observations are awaited before shutdown; reader termination alone is not a FIFO
flush assertion. macOS execution does not prove iPhone or Android ART execution.

## Formal RN consumer has two complementary test boundaries

- **Native bridge contracts**, under its `android/app/src/androidTest` and
  `ios/BleHubBlengHostNativeTests`: directly verify the formal Kotlin/Swift seam, native Nitro
  Promise success/rejection, lifecycle reuse and applicable native cleanup observations. They do
  not prove Hermes/JS execution or UI behavior. These belong to the consumer, not to the
  integration library's own test App or the legacy probe host.
- **Shared Appium UI E2E**: one button/state scenario for both platforms, exercising the formal
  JS API through the rendered App. Device preparation, ownership, installation, signing and
  failure cleanup remain host responsibilities. See the
  [attach-only gate](react-native-bleng/README.md#shared-appium-lifecycle-e2e).

Rust and direct UniFFI tests remain under `crates/` and `tests/ffi/runtime`. Their deterministic,
FFI and artifact results complement these RN tests; none substitutes for a real RF gate.

## What must leave the legacy host before it can be deleted

| Existing responsibility | Destination / removal condition |
|---|---|
| Formal Apple owner smoke and Android formal Nitro bridge contracts | Moved to `react-native-bleng` native targets; no formal Nitro package dependency remains in the legacy host |
| Raw `u64`/bytes/error/ingress contract checks | Native UniFFI/runtime or artifact consumer where applicable; preserve any genuinely Nitro-specific transport regression separately |
| Probe-specific queue, close and resource fixtures | Keep unique regressions until equivalent tests exist at their actual owner; do not migrate the raw probe into the formal JS API |
| Actual Hermes invalidation/pending reload | Needs a formal engine-lifetime seam and real old-owner retirement evidence; Rust unit tests alone cannot replace it |
| Workbench browsing and run/evidence persistence | Formal lifecycle workbench now owns Tests/Current run/Evidence navigation, pending-action continuity and bounded in-memory observations; legacy RF scenario/provider/host-ledger wiring and real UI evidence still need migration. No reload/process persistence is claimed. |
| RF full-suite and separate REJECT/radio/permission/multi-DK gates | Retarget host scenarios/ledgers to the formal API and retain live fixture/cleanup evidence; lifecycle smoke is not replacement coverage |

The formal host now renders the typed initialization snapshot from `start()` and tests its
non-ready/independent-authorization semantics. Its workbench keeps the owner/action state above
view switching, retains initialization/failure observations after close and leaves the Evidence
page visible; only a new explicit session replaces that UI summary. These software-tested
presentation responsibilities do not migrate the legacy RF/reload protocols. Formal scan now has
an approved typed API, bounded Rust delivery and an explicit manual App workflow; both native
App/test graphs compile, but mobile runtime/Appium/RF remain unverified. Typed close separately
reports logical owner closure and cleanup failure. See the [scan integration candidate](../../docs/testing/reports/2026-11-04-formal-rn-scan.md).
Connect/GATT/I/O/notifications, actual reload and host-ledger migration remain incomplete; JS
recovery projection is separate contract work, not completed by moving tests. Preserve full-suite
versus opt-in fault gate identities and known platform failures.
Delete the legacy integration and host only after the remaining requirements have a meaningful
owner/test mapping, live references are removed, and applicable new-candidate evidence exists.
Historical reports remain unchanged; their results never transfer to a new package/candidate.
