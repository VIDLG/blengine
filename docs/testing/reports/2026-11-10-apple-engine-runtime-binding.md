# Apple runtime-local binding and RN invalidation — 2026-11-10

## Scoped verdict

Apple's formal package now binds session owners to a runtime-local lifecycle module and invokes
the [native retirement barrier](2026-11-10-apple-engine-retirement-seam.md) from RN's actual
`RCTInvalidating` callback. The final native suite passes **39/39 tests**, zero failures/skips,
on the arm64 iPhone 17 Pro Simulator, iOS 26.5. Two tests use real Hermes runtimes, real Nitro
NativeState and real RN TurboModuleManagers over scripted BLE backends.

This is **native runtime/module invalidation evidence**, not a complete RN App reload, mobile
RF or release gate. The host-owned old-retirement-before-new-readiness barrier, full `RCTHost`
reload, old-JS callback behavior during App reload and Android engine migration remain pending.
No phone, shared ADB daemon, DUT permissions, radio or firmware was changed.

## Implementation and ownership

- [BlengEngineLifecycle.mm](../../../integrations/react-native-bleng/ios/BlengEngineLifecycle.mm)
  is a private RN TurboModule with JSI bindings and `RCTInvalidating`. Each module instance owns
  its own `BlengEngineLifetime`; there is no process-global current-engine slot.
- [The JS factory](../../../integrations/react-native-bleng/src/index.ts) materializes that module
  on iOS before creating the Nitro object, requires its private runtime-local binder, and binds
  the actual object before returning it. Android construction is unchanged.
- The binder is nonwritable/nonconfigurable and checks argument count, object/NativeState and
  the exact generated Swift-backed owner type. Its closure captures only that module's lifetime.
  The generated wrapper retain is consumed exactly once even when binding is rejected.
- [BlengEngineLifetime](../../../integrations/react-native-bleng/ios/HybridBlengSessionOwner.swift)
  keeps a bounded native membership set, rejects duplicate/cross-engine/retired binding, and
  permanently claims each accepted owner. Capacity rejection occurs before claiming the object.
  The maximum is 64 bound owners per engine, including explicitly closed owners that can still
  restart before engine retirement. No owner is silently dropped or implicitly adopted.
- RN invalidation synchronously seals each owner's admission and starts its existing shared
  native retirement task. It releases membership after starting those tasks and does not call
  into JS. The native completion record remains observable after VM destruction; synchronous
  module invalidation return alone is not resource-zero.
- The production native constructor rejects `start()` while unbound. Direct Nitro construction
  cannot start an unowned session by bypassing the formal factory. Native-only injected test
  owners keep their existing deterministic construction seam.
- [The Podspec](../../../integrations/react-native-bleng/BleHubReactNativeBleng.podspec) includes
  the lifecycle sources and declares explicit RN hook dependencies. It remains independently
  evaluable by `pod ipc spec`; no new UniFFI image, raw JS API, public engine ID or runner was added.
- [The build owner](../../../tools/react_native/bleng_ios_consumer.py) now requires the installed
  lifecycle header/implementation and exact Podspec entries before project generation, Pod
  preparation or compilation. The Nitro-free platform contract source set is unchanged.

### Compatibility impact

The iOS JS package and native binary must be updated together. An older binary without the
lifecycle module/binder now fails owner construction rather than silently creating an unbound
owner. Missing/invalid binding also fails before BLE initialization. The package remains unfrozen;
this does not establish a released compatibility shim or ABI freeze.

## Runtime evidence and limits

The new [consumer-only harness](../../../tests/consumers/react-native-bleng/ios/BleHubBlengHostNativeTests/BlengEngineRuntimeHarness.mm)
creates two actual Hermes runtimes and two RN TurboModuleManagers, materializes each module,
and binds the supplied real Swift-backed Nitro NativeState through its actual JSI HostFunction.
It uses the same production lifecycle module; it does not add a test image or replacement API.

Verified assertions:

1. Plain objects, duplicate bindings and the same native owner wrapped in another runtime are
   rejected. A rejected cross-engine attempt does not poison the second engine's valid binding.
2. The private binder is immutable. After the first manager invalidates its module, the old
   runtime's still-live binder rejects a fresh owner.
3. Only `[manager invalidate]` invokes the module callback in the runtime tests. Its bound owner
   is sealed/retired; the other module's owner stays active and can start a scan against its fake.
4. The first VM is destroyed while its post-reader native snapshot barrier is held. Releasing
   that barrier later produces successful native completion with exactly one backend disposal.
5. A real Supervisor cleanup failure stays `native_resources_not_zero` across VM destruction
   and repeated native observation; the old owner cannot restart.
6. Separate native tests cover duplicate/retired membership, the 64-owner limit, rejection without
   poisoning an overflow owner, and rejection of an unbound production owner before initialization.

The native BLE operations run through Rust but use scripted backends, not CoreBluetooth RF.
The harness deliberately uses a no-op JS CallInvoker: no JS Promise settlement or complete App
bundle execution is claimed. Module/native retirement can finish independently of a destroyed VM;
this does not yet show that a new RN App engine waits for all prior cleanup before becoming ready.

## Exact validation

```sh
ENTERPRISE_REPOSITORY=https://repo.reactnative.dev/maven2 pixi run just react-native formal-host-ios-build
pixi run just --no-deps react-native formal-host-ios-native-test 633606C2-89B2-4707-9CF7-2FEF326D7100
pixi run just react-native formal-host-test
pixi run just react-native formal-host-check
pixi run just react-native formal-host-android-build debug
pixi run just test supervisor
pixi run just --command pixi run python -m unittest tools.tests.react_native.test_bleng_ios_consumer tools.tests.react_native.test_recipe_wiring
```

| Gate | Observed result |
|---|---|
| Normal iOS owner build | Passed: Podspec admission, isolated platform contract, Pod graph refresh and App/native-test compile |
| Apple native | Final **39 passed**, zero failed/skipped; earlier same-slice run before unbound-start assertion passed 38 |
| Formal Jest | **68 passed**, 3 suites; 11 new factory cases exercise real package-source JS with mocked RN/Nitro transport |
| Formal typecheck/autolinking | Passed |
| Android debug App + AndroidTest | Compile passed; no device installation |
| Rust Supervisor | **124 passed**, 6 suites |
| iOS build owner + recipe wiring | **23 passed**; synthetic subprocess boundaries only |

## Pod preparation diagnosis and refreshed graph

Bounded read-only HEAD probes observed both Maven Central base URLs returning a 301 redirect to
`repo.reactnative.dev`; one redirect-following request timed out. Direct requests to that target
subsequently returned 200. Installed RN 0.87.1 Core/dependencies/Hermes helpers support the
`ENTERPRISE_REPOSITORY` base-URL override, so this run supplied the observed redirect target
explicitly. This is per-command configuration, not a third-party patch, persistent machine
change, checksum bypass or source-build fallback. The default redirect path's reliability is
not claimed fixed.

The normal maintained build refreshed the changed Pod source/dependency graph. Logs retain
prebuilt mode (`Building from source: false`) and pinned RN/Core/dependencies 0.87.1 and
Hermes 250829098.0.17. The new lock and installed Manifest are byte-identical, SHA-256:

```text
1633fa09c2d12fb1f68826594a67cf13db548bebb8c1566c109898dda3d05b9b
```

Pre-build lock digests, refreshed locks, Podspec/Pod/build logs, initial and final native result
bundles/test trees, and source hashes are under `.work/apple-engine-runtime-binding-2026-11-10/`.
No old sealed failure was edited.

## Local failures retained and corrected

These were implementation/setup failures, not RF failures or deliberately red behavioral tests:

- The new Jest suite initially mocked only the consumer dependency tree, leaving the integration's
  real Nitro module to load. Exact-path mocks now cover both trees while exercising the real factory.
- Typecheck then rejected Node-only resolver globals in the RN test environment. Narrow local
  resolver types and relative package resolution fixed this without changing dependencies/tsconfig.
- Initial standalone Podspec evaluation rejected the Podfile-only `install_modules_dependencies`
  helper. Explicit RN hook dependencies replaced that call; the error log is preserved separately.
- Initial C++ harness compilation found an ambiguous `hermes` namespace. Fully qualifying
  `facebook::hermes::makeHermesRuntime` fixed it; the failed compile log remains separate.

All final gates above passed after correction. Final checks also passed:
`pixi run just fmt`, `pixi run just lint`, `pixi run just docs-check-mermaid`
(27 diagrams in 238 Markdown files), `git --no-pager diff --check`, and authored Markdown
UTF-8/trailing-whitespace/conflict/fence/local-link checks. Formatting changed only the new
build-owner test within the source receipt; its combined 23-test regression was rerun and passed.
Existing Git CRLF-normalization warnings were not whitespace errors.

Final read-only `simctl list devices booted` showed no booted simulator, matching the initial
shutdown state. No foreign cleanup or simulator shutdown was needed. No physical mobile or RF
cleanup claim follows from these tests.

## Next work

Use the existing formal consumer and host ownership to prove a full RN App reload, with the
same process/run identity, actual old-engine callback receipt, native retirement completion and
new-engine readiness in order. Keep cleanup failures sealed and prevent a new host engine from
claiming readiness before that barrier. Do not implement this as a process-global current engine,
copy the legacy pending-RF API into JS, or use Appium/Maestro success as lifecycle/resource proof.
