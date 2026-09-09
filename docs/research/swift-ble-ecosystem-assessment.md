# Swift BLE ecosystem assessment for BleHub Apple

Date: 2026-09-03

## Question

Can BleHub reduce custom Swift code by adopting an existing CoreBluetooth library while preserving its thin, cross-platform command/event contract?

## Decision

Use one Swift implementation in `platforms/apple` for both macOS and iOS. Keep the production transport on Apple `CoreBluetooth`, Swift standard-library types, `DispatchQueue`, SwiftPM, and generated Stable UniFFI bindings.

Do not add a high-level Swift BLE wrapper to the production backend at this point. The reviewed candidates own concurrency, operation, timeout, retry, connection, or observable state that BleHub deliberately leaves to its caller or must correlate itself at the native callback boundary. Adapting one would retain most BleHub-specific correlation code while adding a second semantic layer.

Do not add Nordic Semiconductor's `IOS-CoreBluetooth-Mock` to the current production or test targets. A pinned local-source audit of tag `1.0.6` at commit `5748c9e8b1750e0d7bc09243c099ff618f211cdf` found useful scripted GATT behavior, but its `CBM...` managers, peripherals, attributes, and delegate protocols are a separate wrapper object model rather than transparent CoreBluetooth aliases. Driving the complete backend through it would require a broad transport abstraction or wholesale production migration, while the existing controller fakes already cover BleHub's important identity, generation, duplicate-instance, invalidation, and late-callback invariants.

The mock also cannot serve as authoritative queue or write-without-response evidence: mock scan callbacks are delivered from a main-run-loop timer instead of the caller-supplied manager queue, native retrieval can replace wrapper identity, and the simulated no-response buffer does not model CoreBluetooth flow control reliably. Keep the pinned checkout as local upstream evidence and reconsider a standalone compatibility fixture only if a future manager/delegate-glue test demonstrably removes more custom infrastructure than it adds. No dependency is added by this assessment.

## Existing project shape

Verified locally:

- [`platforms/apple/Package.swift`](../../platforms/apple/Package.swift) declares one `BleHubApple` library product for both iOS 16+ and macOS 13+.
- [`platforms/apple/Sources/BleHubApple/AppleBleBackend.swift`](../../platforms/apple/Sources/BleHubApple/AppleBleBackend.swift) uses `CBCentralManager` and `CBPeripheral` directly and confines them to one backend-owned private serial queue.
- The public backend methods remain ordinary non-async fire-and-forget commands; accepted operations complete through generated BleHub events.
- Connection and GATT routing must preserve exact native object identity, caller IDs, backend generations, duplicate UUID instances, callback-first teardown, and stale callback silence.
- BleHub core owns no executor, task tree, timer, mailbox, retry policy, or canonical device/GATT state.

The local architecture research already recommends one shared Swift/CoreBluetooth backend for iOS and macOS; see [`boltffi-vs-rust-native-ble-backends.md`](boltffi-vs-rust-native-ble-backends.md#54-ios-与-macos-应优先共用一个-swift-backend).

## Platform reuse before third-party reuse

The primary ecosystem dependency is Apple's own `CoreBluetooth` framework. Direct use is not equivalent to reimplementing Bluetooth:

- CoreBluetooth owns controller access, scanning, connection establishment, ATT/GATT transport, notification delivery, authorization, and platform lifecycle callbacks.
- Swift/ARC owns Objective-C object retention.
- `DispatchQueue` provides the required serial execution boundary.
- SwiftPM and Xcode provide the shared macOS/iOS package and build graph.
- Stable UniFFI generates the cross-language contract bindings.

BleHub-specific code is still necessary where no generic library can infer the public contract: caller command IDs, terminal event ordering, FFI DTO mapping, opaque attribute IDs, exact native-object correlation, generation retirement, and conservative handling of callbacks that have no native request ID.

## Candidate assessment

| Candidate | Upstream shape verified 2026-09-03 | Useful reuse | Conflict or risk | Decision |
|---|---|---|---|---|
| Apple CoreBluetooth | System framework; common manager/peripheral/delegate model on iOS and macOS | All native BLE transport and callbacks | Platform differences still require availability, permission, lifecycle, background, and signing tests | Production transport |
| Nordic `IOS-CoreBluetooth-Mock` | Pinned local audit of `1.0.6`; SwiftPM package, BSD-3-Clause, iOS 12+/macOS 10.14+; separate `CBM...` wrapper API forwards to CoreBluetooth or emulates it | Scripted discovery, duplicate UUID instances, invalidation, basic I/O, value updates, and disconnect scenarios | Full-backend use requires broad CB/CBM adaptation; scan callbacks escape the supplied queue, native retrieval can replace wrapper identity, and no-response flow control is not faithful | Do not add to current targets; retain only as local reference/possible standalone compatibility fixture |
| Nordic `IOS-BLE-Library` | Swift/Combine wrapper with real and mock products from one source tree; depends on `IOS-CoreBluetooth-Mock` for the mock product | Modern API and existing mock/product split | Owns a Combine-based async abstraction and wrapper object model above CoreBluetooth; BleHub would still need its own event/correlation layer | Do not use as production transport; source may be consulted after local audit |
| `AsyncCoreBluetooth` | Swift 6 async/observable wrapper; depends on a fork of CoreBluetooth Mock, AsyncObservable, and Swift Collections | AsyncStream and SwiftUI-facing convenience | Early-stage upstream documents missing descriptor, service-invalidation, notification-state, and restoration callbacks; adds task/stream/observable state that contradicts BleHub's backend boundary | Reject for production |
| `RxBluetoothKit` | RxSwift-based CoreBluetooth wrapper; package manifest depends on RxSwift 5.x | Mature reactive API | Adds Rx runtime and observable semantics; upstream package/readme target an older Swift/Rx generation and pushed metadata was last observed in 2024 | Reject |
| `Bluejay` | Callback/FIFO framework focused on one peripheral; iOS-only SwiftPM platform declaration | Operation serialization and restoration helpers | Owns FIFO, timers, auto-reconnect, canonical connection state, single-peripheral policy, and application-level serialization; package does not declare macOS | Reject |

## Pinned `IOS-CoreBluetooth-Mock` source audit

Audited local source:

```text
repository: https://github.com/NordicSemiconductor/IOS-CoreBluetooth-Mock
tag: 1.0.6
commit: 5748c9e8b1750e0d7bc09243c099ff618f211cdf
local path: /Users/vision/projects/contrib/IOS-CoreBluetooth-Mock
```

Verified from the pinned implementation:

- `Package.swift` uses Swift tools 5.9 and Swift language mode 5, exposes one `CoreBluetoothMock` product, and resolves `swift-docc-plugin` 1.5.0 plus `swift-docc-symbolkit` 1.0.0 even though the library target declares no target dependency.
- `CBMCentralManagerFactory` always selects mock on a simulator; macOS and physical devices use native forwarding unless `forceMock` is set.
- Native forwarding wraps `CBCentralManager`, `CBPeripheral`, and GATT attributes in distinct `CBM...` objects. Only selected value types such as UUIDs, states, properties, and write types are aliases.
- Mock callbacks usually use the supplied manager queue, but advertisement discovery is driven by a main-run-loop timer and calls the manager delegate directly. Restoration and one idempotent notification-state path also call delegates synchronously.
- Mock peripherals are generally reused by UUID within one manager. Native delegate callbacks reuse a wrapper only while both identifier and exact native peripheral match, while native retrieve methods unconditionally replace cached wrappers.
- Services, characteristics, and descriptors carry separate instance identifiers and preserve duplicate Bluetooth UUID instances. Discovery remains CoreBluetooth-shaped and must be driven service by service and characteristic by characteristic.
- Basic characteristic/descriptor read, write-with-response, notification state/value, service invalidation, RSSI, and disconnect paths are simulated. Notification and Indication share one `isNotifying` model.
- The no-response buffer is not suitable for correctness or throughput claims: its ready state does not consistently represent remaining capacity, the zero-capacity guard does not suppress the later write, and payloads are truncated to one simulated `MTU - 3` segment.
- `didModifyServices` can invalidate old service instances by internal identity, but already queued discovery callbacks are not generation-aware and may still arrive while connected. BleHub would still need its own quarantine logic.
- The mock manager and peripheral can form a strong-reference cycle until power-off/teardown; the native peripheral and delegate wrapper can also retain each other until the delegate is cleared.
- The package has no SwiftPM test target; its XCTest coverage lives in the example Xcode project.

A local `swift build` with Swift 6.2.3 passed. It emitted the same unreachable-branch warning twice in `CBMCentralManagerNative.swift` because the current deployment context makes the legacy pre-iOS-17 wrapper branch constant-false. This is build compatibility evidence in Swift 5 language mode, not Swift 6 strict-concurrency readiness.

The useful simulated scenarios remain non-authoritative. They do not prove TCC, controller state, RF, ATT timing, notification-versus-indication reliability, CoreBluetooth no-response backpressure, background restoration, native object lifetime, signing, installation, or physical iPhone behavior.

## Why a high-level wrapper would not remove the hard code

The shortest-looking wrapper APIs generally combine scan, connect, discovery, read, and subscription into `async`, reactive, promise, or queued operations. BleHub cannot expose those semantics directly because:

1. synchronous return means dispatch acceptance only;
2. native/BLE outcomes must be emitted as `BleEvent` values carrying caller IDs;
3. callers own deadlines, cancellation, retries, and stale/duplicate handling;
4. duplicate service/characteristic/descriptor UUID instances must remain distinct;
5. CoreBluetooth callbacks such as value updates and aggregate discovery steps lack BleHub request IDs;
6. teardown must invalidate routes before delivering potentially reentrant events.

A wrapper that resolves a future or observable still needs an adapter recreating all six rules. Its internal timeout, retry, caching, queue, or identity choices can also hide precisely the callbacks and native instances BleHub must validate.

## Reuse policy

Production dependencies may be adopted when all of the following are true:

- they remove substantial code rather than one or two direct CoreBluetooth calls;
- they support both declared Apple targets or are isolated to one platform-specific packaging layer;
- they do not own caller policy, timers, retries, canonical device/GATT state, or a conflicting executor model;
- exact `CBPeripheral`/attribute identity and delegate callbacks remain observable;
- duplicate UUID instances and BleHub caller/generation correlation remain intact;
- source, license, maintenance, transitive dependencies, Swift version, and package behavior are audited locally at a pinned revision;
- deterministic, package, macOS RF, and iOS RF gates pass after adoption.

Small standard ecosystem utilities remain acceptable when justified. Current controllers are array/dictionary state machines on one serial queue, so `swift-collections`, `swift-atomics`, a dependency-injection framework, or a logging package would add dependencies without removing a project-specific invariant. Prefer `Array`, `Dictionary`, `Set`, `DispatchQueue`, `os.Logger` when logging is added, and XCTest/Swift Testing as appropriate.

## Test-mock conclusion

Do not create a CB/CBM transport hierarchy solely to consume this mock. That seam would touch central construction, both delegate protocols, peripheral routing, every GATT attribute type, and the exact-object checks that the test suite is intended to protect. The dependency would therefore add more integration machinery than it removes.

Continue using BleHub's small controller fakes for deterministic lifecycle and correlation invariants, followed by generic Apple builds and real macOS/iPhone HIL. A future standalone compatibility fixture may depend on the pinned mock without changing `BleHubApple`, but only when it targets manager/delegate glue not already covered and explicitly treats the mock's queue, wrapper identity, no-response, notification/indication, and teardown behavior as library-specific rather than CoreBluetooth guarantees.

## What BleHub borrowed from the mock audit

No `IOS-CoreBluetooth-Mock` source code, API surface, wrapper hierarchy, or package dependency was copied into BleHub. The production backend was not redesigned around `CBM...` protocols, and the deterministic tests do not claim that mock behavior proves CoreBluetooth behavior.

The audit was still useful as a design and test review input:

- its separate service, characteristic, and descriptor instance identifiers reinforced the requirement to preserve duplicate UUID instances rather than key GATT work by UUID;
- its queued discovery and service-invalidation behavior reinforced testing stale discovery callbacks and invalidation quarantine;
- its manager/peripheral and delegate retention paths reinforced explicit delegate teardown and child-before-parent ownership release;
- its synchronous callback exceptions and scan timer behavior reinforced treating callback execution context as library-specific rather than authoritative for CoreBluetooth;
- its no-response buffer defects reinforced keeping write-without-response backpressure and completion semantics tied to real CoreBluetooth evidence;
- its shared notification/indication state reinforced BleHub's rule that strict Indication success requires platform evidence rather than inference from a dual-property characteristic.

These are borrowed lessons and adversarial scenarios, not borrowed implementation. BleHub's focused fakes model only the exact project invariants under test and are followed by generic Apple builds and real macOS/iPhone HIL.

## Evidence classification

Verified by local source:

- BleHub's shared SwiftPM product and current CoreBluetooth/direct-event architecture;
- the project constraints requiring caller-owned execution and exact native correlation;
- current deterministic and generic Apple build gates;
- pinned `IOS-CoreBluetooth-Mock` package graph, CBM/native object model, callback dispatch, identity/cache, GATT, invalidation, I/O, teardown, and documented unsupported paths.

Verified from upstream repository README, package manifests, and GitHub repository metadata on 2026-09-03:

- candidate platform declarations, direct dependencies, advertised concurrency model, feature scope, license, and documented limitations;
- `IOS-CoreBluetooth-Mock` and `IOS-BLE-Library` were active, non-archived Nordic repositories at inspection time;
- `RxBluetoothKit` and `Bluejay` exposed older package/runtime models and were last pushed in 2024 according to repository metadata;
- `AsyncCoreBluetooth` documented missing callbacks that BleHub already requires.

Inferred from those sources and BleHub's approved contract:

- adopting the high-level wrappers would add more semantic adaptation than code removal;
- adapting the complete backend to `IOS-CoreBluetooth-Mock` would add a broad second transport type system without improving the strongest existing deterministic invariants.

Verified by local execution:

- the pinned mock package builds with Swift 6.2.3 in its declared Swift 5 language mode, with an unreachable legacy-wrapper branch warning.

Still unverified and intentionally not claimed:

- Swift 6 language mode and strict-concurrency readiness;
- real-device parity of the mock package's native forwarding;
- any RF, TCC, ATT, background-restoration, signing, installation, or iPhone behavior from mock results.

## Upstream sources

- [Apple CoreBluetooth local snapshots](../platform-api-reference/apple/)
- [Nordic IOS-CoreBluetooth-Mock](https://github.com/NordicSemiconductor/IOS-CoreBluetooth-Mock)
- [Nordic IOS-CoreBluetooth-Mock Package.swift](https://raw.githubusercontent.com/NordicSemiconductor/IOS-CoreBluetooth-Mock/main/Package.swift)
- [Nordic IOS-BLE-Library](https://github.com/NordicSemiconductor/IOS-BLE-Library)
- [Nordic IOS-BLE-Library Package.swift](https://raw.githubusercontent.com/NordicSemiconductor/IOS-BLE-Library/main/Package.swift)
- [AsyncCoreBluetooth](https://github.com/meech-ward/AsyncCoreBluetooth)
- [AsyncCoreBluetooth Package.swift](https://raw.githubusercontent.com/meech-ward/AsyncCoreBluetooth/main/Package.swift)
- [RxBluetoothKit](https://github.com/Polidea/RxBluetoothKit)
- [RxBluetoothKit Package.swift](https://raw.githubusercontent.com/Polidea/RxBluetoothKit/master/Package.swift)
- [Bluejay](https://github.com/steamclock/bluejay)
- [Bluejay Package.swift](https://raw.githubusercontent.com/steamclock/bluejay/master/Package.swift)
