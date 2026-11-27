# Apple native engine-retirement seam — 2026-11-10

## Scoped verdict

The formal Apple RN native target passes **34/34 tests, zero failures/skips** on the arm64
iPhone 17 Pro Simulator, iOS 26.5. Seven new deterministic tests exercise the prerequisite
native engine-retirement seam using the real Rust Supervisor and a no-radio scripted backend.
The previous 27 native tests, including reader timeouts and peer-cache accounting, still pass.

**This is not an actual Hermes invalidation/reload pass.** The formal package does not yet bind
its HybridObjects to RN engine identity or call this seam from an RN destruction callback.
It is a native-only prerequisite, currently invoked by native tests, not a completed migration.
No public JS/UniFFI API, second runtime, lifecycle registry, App or runner was added.

This follows the [Apple retirement-contract correction](2026-11-10-apple-retirement-contract.md).
Earlier failed and passed candidate evidence remains unchanged.

## Local-source findings

Pinned local sources are RN 0.87.1 and Nitro 0.37.1. Nitro's registered HybridObject constructor
has no runtime/context parameter, so the generated constructor alone cannot establish which
engine owns a session. The current formal JS factory only constructs the HybridObject, and the
formal package has no engine invalidation module yet. Object destruction and explicit JS close
are not substitutes for RN engine invalidation.

The installed RN sources provide per-instance TurboModule invalidation on Apple and a
`ReactHost` before-destroy listener on Android. The existing legacy host demonstrates those
mechanisms, but its RF-specific pending-read protocol is not copied into the formal package.
Actual hook installation and runtime-local binding remain next work. No external dependency
version or third-party source was changed during this slice.

## Implemented native prerequisite

- `HybridBlengSessionOwner.invalidateForEngine()` synchronously seals the old admission gate,
  then returns one shared native `Task<BlengCloseResult, Never>`, not an old-runtime JS Promise.
- Sealing is permanent for that Hybrid owner. Delayed and future starts cannot create a new
  native owner or publish a late initialization receipt. Ordinary explicit close still supports
  an explicit fresh start while the engine is live.
- The lifecycle actor drains already accepted start/close calls before invoking its existing
  close path. An in-flight initialization or explicit close is not mistaken for a terminal
  `supervisor_busy` retirement result.
- Root scan and child stop admission share a lock-only `AppleEngineAdmission` fence. Invalidation
  takes that same lock, so a previously admitted bounded synchronous Rust submit completes
  before retirement starts. The lock is not held through async waits or BLE callbacks.
- Scan children retain only this handle-free fence and their existing Rust proxy. They do not
  retain the root receipt or Supervisor through the fence. Stop waits/cancels and scan pulls keep
  their original operation/delivery ownership semantics.
- Native retirement still uses Rust shutdown → reader drain → native snapshot/resource checks.
  Repeated invalidation and subsequent close observe the same outcome; cleanup failure stays
  failed and the old Hybrid owner cannot restart.
- No BLE command, event delivery or native cleanup is retried to obtain a pass. No timer or
  polling loop was introduced.

Owning files:

- [HybridBlengSessionOwner.swift](../../../integrations/react-native-bleng/ios/HybridBlengSessionOwner.swift)
- [AppleRustSupervisorOwner.swift](../../../integrations/react-native-bleng/ios/AppleRustSupervisorOwner.swift)
- [HybridBlengScan.swift](../../../integrations/react-native-bleng/ios/HybridBlengScan.swift)
- [BlengScanMapping.swift](../../../integrations/react-native-bleng/ios/BlengScanMapping.swift)
- [Formal native tests](../../../tests/consumers/react-native-bleng/ios/BleHubBlengHostNativeTests/BleHubBlengHostNativeTests.swift)

## Seven new native regressions

1. Invalidate during delayed actor admission: reject the delayed start and all future starts,
   with zero native owner construction/initialization.
2. Invalidate an already accepted, held initialization: wait for the existing call, reject late
   scan receipt publication, then retire exactly once. A close requested after invalidation
   joins the native retirement rather than returning a busy pseudo-result.
3. Invalidate an active scan with a held post-reader resource snapshot: reject new root scans
   and stale child stops immediately, preserve the existing Rust owner-closed scan end, and
   complete through the snapshot barrier. A separate native owner remains usable; this does
   not yet prove RN cross-engine identity routing.
4. Drop the Hybrid root while that snapshot is held: the native retirement task survives and
   completes without an old JS Promise or strong reference to the Hybrid root.
5. Join an explicit close already blocked at its snapshot barrier, without a second disposal.
6. Preserve a real Supervisor `native_resources_not_zero` result across invalidation/close,
   without allowing the old Hybrid owner to restart.
7. Invalidate after explicit close, for both successful and failed cleanup: preserve the first
   result and permanently prevent old-owner reuse.

These use deterministic admission/initialization/snapshot gates, not an RF scan or OS engine
destruction. They do not validate native module materialization, JSI binding, Hermes teardown,
old-JS callback suppression, cross-reload readiness or long-running stress.

## Exact validation and evidence

Native compile used the unchanged installed Pods graph, following the prior report's bounded
continuation. The Pod preparation network issue was not reclassified as fixed. No Pod install,
codegen regeneration, lock update, tool install or dependency upgrade was needed in this slice.

```sh
pixi run just --command pixi run python tools/react_native/run_quiet.py --no-tty --progress-seconds 20 .work/apple-engine-retirement-seam-2026-11-10/final-native-build.log -- pixi run -e apple-ci xcodebuild -quiet -workspace tests/consumers/react-native-bleng/ios/BleHubBlengHost.xcworkspace -scheme BleHubBlengHost -derivedDataPath tests/consumers/react-native-bleng/ios/build/simulator-derived -configuration Debug -sdk iphonesimulator -destination 'generic/platform=iOS Simulator' CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO -only-testing:BleHubBlengHostNativeTests build-for-testing
pixi run just --no-deps react-native formal-host-ios-native-test 633606C2-89B2-4707-9CF7-2FEF326D7100
```

Both passed. Initial execution before adding the root-release case passed 33/33; the final
execution passed 34/34. There was no intentionally red assertion run in this slice. Results,
full `.xcresult`, summaries/test tree, build/runtime logs and source SHA-256 receipt are under
`.work/apple-engine-retirement-seam-2026-11-10/`.

The Nitro-free platform contract was independently rebuilt, also passing:

```sh
pixi run just --command pixi run python tools/react_native/run_quiet.py --no-tty --progress-seconds 20 .work/apple-engine-retirement-seam-2026-11-10/platform-contract-build.log -- pixi run -e apple-ci xcodebuild -quiet -project .work/react-native-bleng-ios-consumer/PlatformContract/BlengPlatformContract.xcodeproj -scheme BlengPlatformContract -derivedDataPath .work/react-native-bleng-ios-consumer/PlatformContract/DerivedData -configuration Debug -sdk iphonesimulator -destination 'generic/platform=iOS Simulator' CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO build
```

Other results:

- `pixi run just test supervisor`: **124 passed**, 6 suites.
- `pixi run just react-native formal-host-test`: **57 passed**, 2 suites.

Podfile.lock and Pods/Manifest.lock remained byte-identical with SHA-256
`bf6c8b171ee905f4c262773897420f28f5e0e98051a535e51bcace916c5b2694`.
Final `simctl list devices booted` showed no booted simulator. No physical phone, shared ADB
daemon, DUT permission, radio or fixture firmware was changed.

Final checks passed: `pixi run just fmt`, `pixi run just lint`,
`pixi run just docs-check-mermaid` (27 diagrams in 237 Markdown files), and
`git --no-pager diff --check`. Authored Markdown passed UTF-8, trailing-whitespace,
conflict-marker, fence-balance and local-link checks. Existing Git CRLF-normalization warnings
were not whitespace errors.

## Next boundary

Bind each formal Hybrid owner to its actual RN engine using a private runtime-local mechanism,
and invoke this native seam from the real old-instance invalidation callback. Then verify real
Hermes/native-module destruction and host-owned old-retirement/new-readiness evidence. Keep
per-object retirement separate from the still-unimplemented cross-engine readiness barrier.

Android engine integration, formal scan/RF, mobile release provenance and stress remain
independent gates. No legacy RF/reload implementation is deleted, Appium remains the default UI
driver, and Maestro remains an explicit opt-in comparison.
