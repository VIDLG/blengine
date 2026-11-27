# Android engine retirement parity — 2026-11-11

## Verdict and scope

Android now has the same engine-lifecycle *semantics* as Apple: a session owner belongs to exactly
one RN engine, engine teardown retires that engine's native membership once, and a replacement
engine can only start after the predecessor's retirement is observed and clean.

Verified today:

- new deterministic JVM semantics: **45 tests pass** across
  `BlengEngineLifetimeTest` (10), `BlengEngineLifetimeRegistryTest` (4),
  `BlengEngineHostWiringTest` (7) and the unchanged `OwnedBackendThreadTest` (9),
  `BlengEventReaderTest` (6), `RecoveryEventInboxTest` (6), `RecoveryEventProjectionTest` (3);
- the formal Android App and `androidTest` APKs still compile
  (`pixi run just react-native formal-host-android-build`);
- Jest 68 and Rust Supervisor 124 are unchanged.

Then verified on a physical OnePlus device on 2026-11-12: RN's own teardown reaches the engine
module on a real reload, the retired engine stays sealed and undiscoverable, and the replacement
engine receives a fresh lifetime. See "Real-device reload gate" below.

## What was actually missing

Apple owned three pieces; Android had none of them:

| Concern | Apple (before) | Android (before) |
|---|---|---|
| Engine boundary | `BlengEngineLifecycle` TurboModule + `RCTInvalidating` | none |
| Per-engine membership | `BlengEngineLifetime` with owner set, sticky retirement | none |
| Successor gating | `configurePredecessor` + `requireSuccessfulRetirement` | none |
| Host hook | `FormalReactNativeDelegate` | none |
| Session-level seam | `invalidateForEngine()` on the owner | none |

Android had `close()` only, driven by JS. A reload dropped the Kotlin owner without deterministic
retirement, so a backend `HandlerThread` and any open GATT work depended on GC to disappear.

## Implemented

### 1. `BlengEngineLifetime` (pure Kotlin)

Mirrors the Apple contract so both platforms age the same way:

- admission is sealed permanently at the first `invalidate()`;
- retirement retires every bound owner and aggregates their exact `BlengCloseResult`s in binding
  order, bounded at 64 owners;
- `configurePredecessor(predecessor, timeoutMs)` is one-shot, rejects self/duplicate/late linkage,
  and is serialized by a linkage-only lock that holds no engine registry;
- the successor gate is `predecessor.requireSuccessfulRetirement(timeoutMs)`, which fails on an
  invalid deadline, an unobserved retirement, or any unclean owner receipt;
- engine startup failure (including cancellation of the predecessor gate) is recorded on the
  retirement record, so a middle engine cannot launder a cancelled gate into a clean retirement.

A real race was found and fixed while writing the tests: cancelling a permission job **before its
first dispatch** never runs its body, so a body-level `catch` would have reported a *clean*
startup. The flag is now derived from `Job.invokeOnCompletion`, which also covers
never-dispatched cancellation.

### 2. `BlengEngineLifetimeRegistry`

Exact-key, weak, per-engine lookup:

- requires the caller to pass the concrete engine key;
- an unknown key resolves to `null` rather than to a default;
- duplicate registration for one key fails closed;
- unregistration only removes an entry the caller still owns.

It is generic over the key so the contract is JVM-testable; Android instantiates it with
`ReactApplicationContext`.

### 3. `HybridBlengSessionOwner` engine seam

- implements `EngineOwnedSession`: `claimEngineBinding(permission)` and idempotent
  `invalidateForEngine()`;
- binds at construction to the lifetime registered for its exact `ReactApplicationContext`;
- an unbound owner refuses `start()`, so a missing engine boundary cannot silently start native
  work;
- awaiting the predecessor gate happens **outside** the lifecycle mutex, and awaiting a shared
  engine retirement also happens outside it — either await inside the mutex would deadlock against
  the very retirement it waits for;
- `close()` shares the engine retirement receipt when one exists, so repeated close observes one
  result instead of re-closing native state;
- a bridge-level retirement failure returns `ownerClosed=false` with a typed failure, never a
  clean closure.

### 4. RN engine boundary

`BleHubBlengEngineLifecycleModule` is a spec-less RN `TurboModule` with **no JavaScript method**:

- RN creates one instance per `ReactApplicationContext` and sets `needsEagerInit`, so registration
  precedes any JS-created owner;
- `invalidate()` unregisters and calls `lifetime.invalidate()`; it never blocks;
- registration failure seals admission instead of throwing, because throwing during module creation
  can strand RN's module holder.

RN 0.87 bridgeless `TurboModuleManager.invalidate()` walks its module holders and calls
`invalidate()` on each, which is the Android analog of Apple's `RCTInvalidating`.

### 5. Host-level predecessor wiring

The successor gate now has a production caller. `BleHubBlengEngineHostWiring` is owned by the
host's `ReactPackage` instance — the Android analogue of Apple's host-scoped delegate:

- the package instance lives as long as the host (`MainApplication.reactHost` is a single lazy
  `ReactHost`), so it survives engine reloads;
- `getModule(name, context)` runs once per new engine, where the package installs the exact
  predecessor before returning the module;
- a refused link seals the successor's admission instead of letting it start ungated;
- the wiring exposes only `attach` and an observation-only `latest()`; there is no lookup by name
  and no process-global current engine.

Confirmed statically against installed RN 0.87.1: `ReactInstance.initializeEagerTurboModules()`
creates eager modules on the native-modules queue thread before JS runs, and
`ReactInstance.destroy()` calls `turboModuleManager.invalidate()`.

## Real-device reload gate (2026-11-12)

```sh
pixi run just react-native formal-host-android-engine-reload-test 3ac5cc09
```

Device: `3ac5cc09` (OnePlus `PHB110`, Oplus ROM), rooted ADB install path, **no radio lease taken**
and no BLE capability exercised — this gate is about engine lifetime, not RF.

`BlengEngineReloadInstrumentedTest` runs inside the formal host App's own instrumentation process
and builds a **real** `ReactHost` through `DefaultReactHost.getDefaultReactHost` with the app's
autolinked `PackageList` and an empty bundle. It then:

1. starts the host and asserts the engine module registered a lifetime for the live
   `ReactApplicationContext`, without ever calling `getNativeModule`;
2. calls `host.reload(...)` and asserts `invalidate()` was delivered, that the retirement receipt is
   empty (that engine had no bound owner), and that the old context no longer resolves a lifetime;
3. asserts the replacement context has a distinct, non-invalidated lifetime.

Result: `OK (1 test)` in 0.29 s. Device logcat shows the real `ReactHost{0}` start →
`getOrCreateReloadTask()` → `Destroying ReactInstance` → new `ReactInstance` sequence for
`com.blehubblenghost`, so the assertions are backed by a genuine engine teardown, not by a shallow
success. Production still has no reload trigger; the host is constructed only by the test.

### Two false signals found while closing this gate

- **`context.getNativeModule(NAME)` creates the module.** Using it as an "is the module there?"
  probe both forced eager creation and hid whether eager init had run at all. The gate now polls the
  engine registry only. Any future existence check must observe state, never obtain the module.
- **A stale `androidTest` APK looks like a native failure.** Gradle reported the task up-to-date
  across source edits, so an earlier device run executed an old test class and reported zero
  registrations. The recipe now always rebuilds both APKs; verify APK mtime before trusting a
  device result.

### Still unverified

- Two concurrently live `ReactHost` instances in one process. The `NitroModules.applicationContext`
  limitation below still makes that configuration unsupported rather than tested.
- Nothing in this gate exercises a bound owner: the reload happened with no session, so a *non-empty*
  retirement receipt across a real reload remains covered by JVM semantics only.

## Verified contract additions

Two invariants were tightened after reviewing the first implementation, both because a
process-global shortcut or a test-only bypass had leaked into production code:

1. **No test-only bypass.** The session owner previously exposed
   `allowUnboundStartForTesting()` so the direct owner instrumentation could start without an
   engine. That was a production API existing only for tests, and it hid the real binding path.
   It is removed; the instrumented test now registers the real engine lifecycle module for its
   test context and binds through the production path.
2. **Retirement is permanent.** An owner bound to a retired engine could previously call `start()`
   again, because only the close epoch moved. `start()` now refuses whenever the engine has been
   invalidated, so a torn-down engine's runtime cannot resurrect native work. A plain `close()`
   still permits an explicit later `start()`, which is the intended JS-level restart.

## Library constraint, recorded honestly

On Apple the JS factory binds the created owner through an explicit runtime-local JSI host
function. Nitro's Android side exposes no equivalent hook to Kotlin: `NitroModules` only offers
`install()` with a runtime pointer and a call invoker, and hand-written C++/JNI business glue is
forbidden by the architecture guardrails.

Android therefore has exactly one engine-context source, `NitroModules.applicationContext`, which is
a process-global written once per engine by that engine's `NitroModules` module. The owner reads it
once at construction and performs an **exact-context** lookup, so:

- a miss leaves the owner unbound, and `start()` refuses with an actionable message;
- no owner ever binds by name or by "whichever engine is newest".

The residual limitation is that **one live ReactHost per process** is required. With two
concurrently live engines, the global could point at the other engine and the lookup would find
that engine's lifetime. That configuration is outside this consumer's model and is recorded as a
known limitation rather than papered over; removing it needs a verified explicit binding path, not
another global.

## Test-boundary debt

The direct owner instrumentation still injects native owners by mutating private fields
(`owner`, `ownerFactory`, `terminalClose`) through reflection. Replacing that with a real seam is
the right direction, but the replacement owner would drive real platform BLE, so it cannot be
verified without a fresh radio lease and an nRF52840 fixture. The reload gate above is deliberately
engine-only and is not a substitute. It is left unchanged and recorded as debt.

## Commands

```sh
pixi run just react-native bleng-android-unit-test
pixi run just react-native formal-host-android-build
pixi run just react-native formal-host-android-engine-reload-test 3ac5cc09
pixi run just react-native formal-host-test
pixi run just test supervisor
pixi run just fmt
pixi run just lint
```
