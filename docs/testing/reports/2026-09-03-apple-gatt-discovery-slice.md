# Apple aggregate GATT discovery slice

Date: 2026-09-03

## Scope

This report records deterministic implementation and build validation for aggregate CoreBluetooth GATT discovery in the current unfrozen Apple production candidate. It does not record a real-RF GATT pass, an iPhone runtime pass, or a release-artifact pass.

## Implementation

The production Swift backend now maps the CoreBluetooth discovery sequence:

```text
discoverServices(nil)
→ didDiscoverServices
→ discoverCharacteristics(nil, for:)
→ didDiscoverCharacteristicsFor
→ discoverDescriptors(for:)
→ didDiscoverDescriptorsFor
```

The controller traverses services, characteristics, and descriptors depth first. It stages the complete result before publishing either of these success events, in order:

```text
GattDiscovered
CommandCompleted(error: nil)
```

A discovery failure emits only terminal `CommandCompleted(error: Some(...))`; no partial `GattDiscovered` is published.

The resulting database and backend-private native map preserve:

- every service, characteristic, and descriptor instance, including duplicate UUID instances;
- monotonically allocated opaque IDs for each native attribute instance;
- canonical Bluetooth UUIDs;
- `CBService.isPrimary`;
- CoreBluetooth read, write-with-response, write-without-response, notify, and indicate properties;
- caller-provided connection and GATT session IDs.

## Correlation and invalidation hardening

Connection routes now pin the exact `CBPeripheral` object accepted for the lifecycle. Native callbacks must match both that object identity and the active connection generation; another object carrying the same CoreBluetooth identifier cannot complete or retire the route. Disconnect, adapter loss, and disposal use this pinned object instead of consulting the mutable scan cache.

CoreBluetooth aggregate discovery callbacks do not carry a request ID. A discovery generation alone therefore cannot distinguish a late callback from an invalidated request from a callback for a replacement request on the same connection.

The backend uses a conservative policy when `didModifyServices` invalidates a pending discovery:

1. remove and terminal-fail the pending discovery;
2. remove the committed GATT session for that connection generation;
3. quarantine further discovery on that generation;
4. synchronously reject a replacement discovery before native work;
5. clear the quarantine only when that connection lifecycle is retired, or when all backend state is drained.

Normal callback-delivered success or failure consumes the native operation and does not quarantine the connection. This policy adds no backend timer and preserves caller ownership of command timeout and retry policy.

## Deterministic validation

The focused package test command passed:

```sh
pixi run -e apple-ci swift test --package-path platforms/apple
```

Observed result:

```text
Executed 24 tests, with 0 failures
```

The seven GATT-controller tests cover:

- depth-first aggregate discovery and publication only after every level completes;
- duplicate UUID instance preservation and opaque native-object maps;
- failure rollback without corrupting a previously committed session;
- connection-generation and native attribute-object rejection of stale callbacks;
- pending-discovery quarantine after service invalidation;
- quarantine removal on connection retirement and full drain;
- pending and active state removal before late callbacks after retirement;
- GATT session collision and CoreBluetooth property mapping boundaries.

The six connection-controller tests additionally cover exact peripheral-object identity. A replacement object with the same UUID cannot satisfy connect, connected lookup, or disconnect callbacks for the pinned route.

The complete Apple check also passed:

```sh
pixi run -e apple-ci just apple check
```

Observed results:

- the same 24 macOS tests passed;
- the Stable UniFFI production-candidate Apple package was regenerated;
- arm64 iOS device, arm64+x86_64 iOS simulator, and arm64+x86_64 macOS XCFramework slices passed;
- unsigned generic iOS Simulator and iOS Device builds passed.

## Boundaries

- No GATT RF command was run for this report.
- A fresh user-confirmed RF preflight is required before testing GATT against the Android Peripheral.
- Deterministic callback routing does not establish that CoreBluetooth delivers every native discovery callback exactly once; runtime fault and recovery evidence remains required.
- macOS results do not prove iOS behavior.
- The public ABI remains unfrozen, so applicable Apple gates must rerun against the frozen candidate.
- Characteristic read/write, Notification, Indication, RSSI, passive disconnect, recovery, lifecycle accounting, and soak remain open for the Apple backend.
