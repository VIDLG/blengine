# React Native/Nitro integration boundary

Date: 2026-09-26

Status: integration evidence and naming guidance; the one-public-package/private-probe packaging boundary was approved on 2026-09-28 and is reflected in [the plan](../../../.plan/PLAN.md). This note does not freeze a public ABI.

## Decision

BleHub should formalize the **React Native integration infrastructure** now, but should not publish or present the current Nitro surface as the public BleHub React Native API.

The active package remains a private repository-owned package:

```text
@blehub/react-native-internal
```

Its current `BleHubProbe` HybridObject is intentionally a test-shaped low-level probe. It exists to validate:

```text
RN JavaScript → Nitro → native adapter → production Stable UniFFI BleHub → platform backend
```

It is not a second BLE contract, a TurboModule, a public device manager, or the future `BLeng` API.

**Packaging boundary:** keep `@blehub/react-native-internal` as a separate, never-published diagnostics package and, after the public API review and gates, publish **one** user-facing RN package (`@blehub/react-native`). They coexist in the repository and have separate consumer hosts; simultaneous installation in the same App is not a supported goal. The public package must not ship the probe or a second production UniFFI image. Internal diagnostic tarballs are test artifacts, not another published SDK. Reuse verified bridge mechanics where useful, without coupling product API semantics to probe methods.

## What is already infrastructure-quality

These parts are worth retaining and hardening rather than throwing away:

- Nitro code generation and New Architecture autolinking;
- the iOS Swift and Android Kotlin adapter ownership boundary;
- one production UniFFI native image and staged artifact consumption;
- production Apple/CoreBluetooth assembly on iOS;
- bounded event handoff and asynchronous JS delivery;
- canonical decimal `u64` transport without JavaScript `Number` conversion;
- explicit close, listener removal, late-callback suppression, and overflow fail-closed behavior;
- host recipes, quiet logs, single-image checks, lifecycle/reload tests, and physical RF ledgers.

These are integration and packaging foundations. They can be formalized without claiming that the current low-level API is product-ready.

## What is still intentionally temporary

The following are evidence-oriented probe choices and should not become public API:

- `BleHubProbe` and `createBleHubProbe()` names;
- separate `setCompletionListener` / `setRfScanListener` methods;
- RF-specific methods such as `startRfScan`, `connectRfPeripheral`, and `disconnectRfPeripheral`;
- caller-provided decimal command IDs as the primary JavaScript programming model;
- `roundTrip`, runtime-mode flags, structured console markers, and test-only UI steps;
- Android's current deterministic backend and fail-closed RF methods;
- direct exposure of every low-level `BleEvent` projection to ordinary UI code.

The current names are useful because they prevent accidental promotion. Renaming the probe to `BleHub` now would make the architecture less honest, not more formal.

## Naming recommendation

| Layer | Current/recommended name | Public? | Responsibility |
|---|---|---:|---|
| Private RN package | `@blehub/react-native-internal` | No | Nitro bridge and host-validation surface |
| Private Nitro object | `BleHubProbe` | No | Low-level bridge and lifecycle/RF evidence |
| Native implementation classes | `HybridBleHubProbe`, `BleHubReactNativePackage` | No | Swift/Kotlin implementation and RN autolinking |
| Future product package | `@blehub/react-native` | Future | Typed React Native consumer API, after BLeng/API review |
| Future product object | `BleHub` or a reviewed factory/service name | Future | UI-facing async BLE API backed by the single production image |
| Rust product facade | `BLeng` | Future | Caller-facing async/object semantics owned by Rust; concrete API still requires review |

`@blehub/react-native` is the only planned published RN package; the current `-internal` package remains private. It should only be introduced when its API is backed by a reviewed `BLeng` application contract. Nitro should remain an implementation detail, not part of its naming promise. Native names such as `BleHubReactNative` need not be renamed now just to avoid a hypothetical same-App collision; if simultaneous installation is ever required, Pod/module, native library, namespace and Nitro registry identities need a separate compatibility review.

## Public API shape later

The future public package should not expose the current probe methods verbatim. The API review should decide whether it exposes a typed object/factory or a small typed service, but it should provide:

- initialization and authorization state as typed results/events;
- scan sessions and immutable advertisement records;
- connection handles and explicit connect/disconnect operations;
- aggregate GATT discovery and characteristic read/write operations;
- notification/indication subscriptions with explicit teardown;
- structured errors and capability checks;
- deterministic close/dispose semantics and documented late-event behavior.

It should hide or wrap:

- Nitro `HybridObject` names;
- Swift/Kotlin package classes;
- Stable UniFFI generated names;
- production command-ID allocation and raw decimal-ID bookkeeping, unless a reviewed expert API explicitly requires it;
- RF fixture terminology and test-only markers.

The public surface must still preserve the approved ownership model: the caller owns business state, timeout/cancellation policy, and event consumption; the RN facade must not introduce a second backend, executor, event loop, or canonical BLE state store.

## Formalization gates

Before the public `@blehub/react-native` package is published, all of the following should be independently recorded; design and implementation may begin after BLeng/API review:

1. iOS and Android production backend assembly, not a deterministic Android test backend.
2. iOS and Android artifact-only consumers using one production native image.
3. Generated binding and autolinking checks for the pinned RN/Nitro candidate.
4. Basic runtime, reload/invalidation, close, late-callback, listener-removal, and bounded-overflow gates on both platforms.
5. Scan, connect/disconnect, aggregate GATT discovery, read/write, and notification/indication RF evidence with platform-specific boundaries.
6. Public API review covering ownership, cancellation, errors, capabilities, threading, and versioning.
7. An independently installed public-package artifact consumer and a plan to keep the private probe available for diagnostics without exporting it as the product API or counting private-package tests as public-package validation.

At the time of the original 2026-09-26 review, evidence closed only a subset of these gates. Later private iOS RN GATT and RF checkpoints are recorded in [the plan](../../../.plan/PLAN.md); they do not close Android runtime/RF, BLeng or the future public package's own artifact and runtime gates.

## Recommended next implementation order

1. Keep `@blehub/react-native-internal` and `BleHubProbe` unchanged in name for independent low-level diagnostics; do not convert its existing iOS RF evidence into public-package evidence.
2. Extract reusable bridge mechanics from the probe only when the second operation family demonstrates the need; do not introduce a speculative public facade.
3. Complete the Android production Central backend in its approved Phase 4 lane, then add the same internal surface against it.
4. Design and review `BLeng` independently with Rust contract tests and a fake event source.
5. Create `@blehub/react-native` after BLeng/API review, validate its own staged artifact, lifecycle and platform RF gates, then publish that one user-facing RN package. Retain the separate private probe for repository diagnostics.

This keeps the active integration credible and maintainable without freezing a test harness as a product contract.
