# Internal React Native consumer

This directory owns an independent downstream iOS/Android React Native New Architecture **transitional host app** used to validate [`@blehub/react-native-internal`](../../../integrations/react-native-internal/README.md). It is an executable consumer and test infrastructure, not a library and not a publishable package. The independent [formal BLeng consumer](../react-native-bleng/README.md) now owns formal native bridge tests and product UI testing; this legacy host remains only for unmigrated probe/RF responsibilities.

The ownership split is:

```text
integrations/react-native-internal
    private Nitro integration library/package
    HybridBleHubProbe and native adapters

integrations/react-native-bleng
    formal BLeng Nitro integration library/package
    lifecycle-only Supervisor bridge

tests/consumers/react-native-internal
    independent RN host application
    JS workbench, iOS app target, Android app target, native/instrumented tests
    consumes only the private Nitro package; owns transitional host evidence and test orchestration

tests/consumers/react-native-bleng
    independent formal RN host; owns formal native bridge tests and shared Appium UI E2E
```

The `internal` suffix describes the private package/test lane consumed by this host; it does not
mean this directory is itself a library.

## Topology

- The mobile host uses React Native `0.87.1` for iOS and Android.
- The independent RN macOS feasibility host (`react-native-macos@0.81.9`) was moved to [`../archive/react-native-macos/`](../archive/react-native-macos/). It is not maintained or part of the active gate.
- The mobile host consumes the private Nitro integration and the production combined Stable UniFFI package, but no longer autolinks the formal Nitro package. The formal Apple owner smoke and Android native Nitro bridge contracts belong to `tests/consumers/react-native-bleng`. Remaining raw probe tests and native adapter reuse are transitional, not a reason to expose raw APIs in the formal package.
- Host app names and runtime markers retain `BleHubNitroHost` / `BLEHUB_NITRO_RUNTIME` because they identify the Nitro test host, not a public API.

The mobile contract-lab UI uses ordinary React Native views and automatic iOS scroll content insets. It does not use deprecated React Native `SafeAreaView`, NativeWind, or GlueStack. XCUITest remains the appropriate framework for the stateful physical-device UI flow.

## RF Lab workbench

Ordinary Android/iOS launches open `runtimeMode=rf-workbench`: no Probe is created and no BLE command is dispatched. The whole test surface uses the same three-level workbench, not separate scenario apps:

| View | Responsibility |
|---|---|
| Tests | Browse category → concrete scenario → setup. Show platform availability, fixture requirement and the maintained host recipe; browsing does not execute it. |
| Current run | Show the one mounted scenario's progress and App checks. Switching views hides inactive content from touch/accessibility without unmounting, cancelling, restarting or clearing that scenario. |
| Evidence | Show mode/session/service identity, coverage limits and the first App failure. The App does **not** receive the host's final physical, cleanup or bundle verdict: App Passed is not host-gate Passed. |

[`rf_lab_catalog.ts`](src/rf_lab_catalog.ts) is static metadata for full validation, diagnostics, lifecycle/fault recovery, multi-Peripheral and host-only ABI runtime categories. It records exact runtime/discriminator, platform, recipe and `none` / `single-DK` / `two-DK` requirements, not passing hardware coverage. In particular:

- `rf-lab-suite` is full-suite only when `rfRecoveryRunId !== undefined`; without it, the legacy flow-only route runs ten flows or the selected `rfLabSuiteFlow`, without RecoveryTen. That route has no standalone maintained recipe and is not offered as a catalog card.
- `rf-adapter-transition` defaults to `rfAdapterMode=startup-off`; explicit `active-loss` selects the different active-read fault scenario. Both are Android-only. `rf-permission-transition` is a separate Android host-selected scenario with denied/active/restored phases and OS process-death evidence; iOS RF permission transitions remain planned; the separate `permission-authorization` entry covers non-RF authorization audits. Unsupported platform entries do not invent commands.
- Subscribed RF reload (`rf-reload`) has only an iOS recipe. Host-only engine reload and physically pending-read reload have separate Android/iOS recipes and evidence boundaries.

[`App.tsx`](src/App.tsx) remains the sole execution router; existing scenario runners retain Probe/Hub, listener, cancellation and cleanup ownership. [`rf_lab_workbench.tsx`](src/rf_lab_workbench.tsx) owns browsing/presentation only. [`rf_lab_session.tsx`](src/rf_lab_session.tsx) keeps a bounded in-memory summary from existing reporters or an existing owner's computed status; it ignores raw `:event:`, `:resources:` and `:native:` records. Failure is sticky, including after a later pass marker; only an explicit manual begin resets the summary. It adds no core runtime, mailbox or backend canonical BLE state. Navigation preserves this session, not a history across engine reload or process restart.

Host-selected launches open Current run and stay pinned to that scenario; selecting another setup does not run it. A new host scenario requires a fresh explicit host launch. `runtimeMode=rf-lab` opens the existing interactive diagnostics; a non-host-selected workbench can also open them when supplied a service UUID. Opening is not BLE execution: the developer must press Run, using a matching fixture already prepared on the host. Android uses `blehub.runtimeMode=rf-lab` / `blehub.rfServiceUuid=<uuid>` activity extras. Explicit `basic` retains contract-smoke behavior; Apple smoke launchers and XCUITest use `--blehub-nitro-basic` instead of the ordinary launch default. Host tools still own installation, fixture/radio control, RF checks and final evidence; no new app or tool framework is introduced.

## Apple gates

Physical iOS UI tests use XCUITest in addition to shared RN Testing Library/Jest software tests. iPhone Developer Mode and macOS developer-tool authorization are different prerequisites. Inspect the latter with the read-only `/usr/sbin/DevToolsSecurity -status`. Changing it with `sudo /usr/sbin/DevToolsSecurity -enable` is an explicit, human-only machine setup decision: it changes developer authorization policies and is never run by ordinary recipes or an agent collecting a password. Enabling it is not, by itself, proof that device UI automation is authorized or passing.

The UI recipe uses `run_quiet.py --no-tty` and `-collect-test-diagnostics never` so automatic failure-diagnostic collection cannot ask for a terminal password. Test failures still propagate; ordinary XCTest results remain available. Do not bypass UI automation authentication or relabel a runner-initialization error as an executed UI assertion.

```text
pixi run just react-native host-check

pixi run just react-native host-ios-pods
pixi run just react-native host-ios-build
pixi run just react-native host-ios-single-image-check
pixi run just react-native host-ios-artifact-consumer
pixi run just react-native host-ios-artifact-device-runtime <device-udid> <development-team>
pixi run just react-native host-ios-device-build <device-udid> <development-team>
pixi run just react-native host-ios-device-single-image-check <device-udid> <development-team>
pixi run just react-native host-ios-device-runtime <device-udid> <development-team>
pixi run just react-native host-ios-device-lifecycle-stress <device-udid> <development-team>
pixi run just react-native host-ios-device-reload <device-udid> <development-team>
pixi run just react-native host-ios-device-ui-test <device-udid> <development-team>
pixi run just react-native host-ios-device-permission-test <device-udid> <development-team> <fresh-xcresult-path> [restore-only=0]
pixi run just react-native host-ios-device-rf-lab <device-udid> <development-team> [service-uuid]
pixi run just react-native host-ios-device-rf-lab-suite <device-udid> <development-team> <fixture-contract> <fixture-profile>
pixi run just react-native host-ios-device-rf-scan <device-udid> <development-team>
pixi run just react-native host-ios-device-rf-connect <device-udid> <development-team>
pixi run just react-native host-ios-device-rf-gatt <device-udid> <development-team>
pixi run just react-native host-ios-device-rf-io <device-udid> <development-team>
pixi run just react-native host-ios-device-rf-rssi <device-udid> <development-team>
pixi run just react-native host-ios-device-rf-notification <device-udid> <development-team>
pixi run just react-native host-ios-device-rf-indication <device-udid> <development-team>
pixi run just react-native host-ios-device-rf-passive-disconnect <device-udid> <development-team>
pixi run just react-native host-ios-device-rf-cancellation <device-udid> <development-team>
pixi run just react-native host-ios-device-rf-recovery <device-udid> <development-team>
pixi run just react-native host-ios-device-rf-recovery-ten <device-udid> <development-team> <fixture-contract> <fixture-profile>
pixi run just react-native host-ios-device-pending-read-reload <device-udid> <development-team> <fixture-contract> <fixture-profile>
pixi run just react-native host-ios-device-rf-reload <device-udid> <development-team>
pixi run just react-native host-ios-device-rf-multi <device-udid> <development-team>
pixi run just react-native host-ios-device-rf-multi <device-udid> <development-team> notification 3
# Explicit, non-destructive matrix; one signed build, seven separately checked scenarios:
pixi run just react-native host-ios-device-rf-baseline <device-udid> <development-team>
```

`host-ios-artifact-consumer` is an optional clean, unsigned Simulator build: it stages an explicit iOS-only npm tarball containing generated Nitro glue and the prebuilt production Apple/UniFFI package, installs that tarball into a separate ignored `.work/react-native-ios-artifact-consumer/run-<uuid>/host`, verifies the installed XCFramework bytes and both CocoaPods `-force_load` paths, then performs unsigned generic iOS Simulator and device builds with separate final single-image checks. It requires the pre-existing staged production Apple package; prepare it explicitly before this gate, since the gate does not replace existing staging. It never imports the checkout's module source, flashes a Peripheral, signs, or launches an iPhone. This is an internal artifact feasibility gate, not a publishable cross-platform npm package, RF, or public BLeng API pass. The separate `host-ios-artifact-device-runtime` adds explicit signing, install, Nitro contract runtime and lifecycle checking on a real iPhone; it still is not RF evidence. Each run retains its own ignored logs and refuses to overwrite another run. The [iOS artifact-consumer checkpoint](../../../docs/testing/reports/2026-09-27-react-native-ios-artifact-consumer.md) records the unsigned static result and its limits.

`host-ios-device-rf-lab` builds/installs the signed interactive host and returns without preparing a fixture or waiting for a gate verdict. `host-ios-device-rf-lab-suite` instead invokes [`run_rf_lab_full`](../../../tools/react_native/run_rf_lab_full.py): one App/JS boot and live-attested multifunction fixture session for ten RF flows (including cancellation and native watchdog), then ten fresh-Probe/Hub recovery cycles. It supplies the recovery discriminator; the runtime mode alone does not select this full plan. The runner/checkers require the complete combined evidence and owned cleanup, leaving the App visible by default; `--close-app` requests teardown. See the [RF Lab architecture](../../../docs/research/react-native/react-native-rf-lab-test-architecture.md) for the independent pending-read reload and fixture/evidence boundaries.

The macOS-hosted Swift handoff test checks a real native bounded queue without CocoaPods or hardware; it does not test native Hub disposal, Nitro's JS queue, or RF. `host-ios-device-rf-baseline` is optional and fail-fast: it builds the signed app once, then runs scan, connect, GATT, I/O, RSSI, Notification and Indication using each scenario's existing independent fixture lifecycle and checker. It does not run the destructive recovery/reload gates, install diagnostic firmware, or replace the per-scenario recipes. An interrupted matrix does not count as a seven-scenario pass; inspect the last scenario's fixture lock/cleanup before retrying.

Full CocoaPods/Xcode output is written below the applicable ignored `build/` directory. Successful quiet commands print the log path; failures print a bounded tail and preserve the original exit code. The physical-iPhone UI recipe runs Xcode without terminal input or a controlling terminal; it never reads a password from project configuration. If signing authorization is unavailable, the OS may still require an explicit human keychain unlock or GUI approval, but this recipe cannot request a password from the agent's terminal.

The non-RF physical-iPhone gates validate signed install/launch, Nitro object creation, bytes/errors, canonical `u64`, completion ordering, listener removal, repeated close/recreate, late-callback silence, bridgeless engine reload/invalidation, and the visible XCUITest recreate flow. The separate RF scan recipe exclusively starts the standalone nRF52840 provider, then requires the iPhone RN JS caller to observe `PoweredOn + Authorized`, dispatch scan through Nitro/UniFFI/production Apple backend, receive a matching FD00 advertisement, request scan stop, observe terminal completion and post-stop silence, and close. One real iPhone scan-only RF flow and a corrected checker pass are documented in the [RN iPhone-to-nRF checkpoint](../../../docs/testing/reports/2026-09-25-react-native-ios-nrf-scan.md); a later retry was blocked by the device lock and overwrote the ignored runtime log. This scan-only result does not validate connection, GATT, subscription, or the future BLeng surface; the full engine reload gate does not claim Fast Refresh semantics. The separate `host-ios-device-rf-connect` gate uses the same production stack to require ordered, ID-correlated `Connecting` → `Connected` → connect terminal and requested `Disconnected` → disconnect terminal, followed by one-second JS-observed silence, explicit listener removal, and close. Its signed physical-device run has now passed with exact connection/disconnection ID correlation and fixture cleanup; see the [connection RF checkpoint](../../../docs/testing/reports/2026-09-25-react-native-ios-nrf-connect.md). A later scan-only retry was blocked by an iPhone auto-lock, so keep the device unlocked through app launch. The separate `host-ios-device-rf-gatt` gate extends the same JS-driven flow through aggregate service/characteristic/descriptor discovery, validates the nRF profile's five characteristic instances and two CCCD descriptors, then performs requested disconnect and post-disconnect silence. The completed physical-device result is documented in the [RN iPhone-to-nRF GATT checkpoint](../../../docs/testing/reports/2026-09-26-react-native-ios-nrf-gatt.md). It proves the private RN low-level GATT projection only; it does not prove characteristic I/O, subscriptions, strict Indication, or a BLeng API. The separate `host-ios-device-rf-io` gate extends the same flow through initial read (`00`), write with response (`10`) plus readback, write without response (`AB`) with an explicitly local-submission terminal, and a separate readback of `AB`, followed by requested disconnect and silence. Its completed physical-device result is documented in the [RN iPhone-to-nRF GATT I/O checkpoint](../../../docs/testing/reports/2026-09-26-react-native-ios-nrf-gatt-io.md). It proves the private RN low-level characteristic I/O projection only; it does not prove subscriptions, strict Indication, recovery, or a BLeng API. The separate `host-ios-device-rf-notification` gate uses the validated `fff20000` updates characteristic and requires `PlatformDefault` to resolve to actual `Notification`, exact `EF` value delivery, disable terminal ordering, two seconds of post-disable value silence, requested disconnect, and post-disconnect silence. The separate `host-ios-device-rf-indication` gate first requires strict `RequireIndication` on dual-property `fff20000` to terminate with `NotSupported`, then uses pure-Indication `fff50000` and requires actual `Indication`, exact `EF` value delivery, disable terminal ordering, two seconds of stale-value silence, requested disconnect, and post-disconnect silence. Both are private RN subscription gates; passive recovery and the future BLeng API remain separate.

The optional 10-cycle gate borrows the repeated connect/discover/disconnect scenario shape from BLE library examples, but uses one production RN probe/listener, disjoint per-cycle IDs, fixture-driven passive faults, and an independently checked event ledger. `devicectl --console` is only the log transport: the bounded console controller stops at the observed `close:passed` marker, while the checker still requires all ten ordered cycles, exact correlation, and close with no failure. The final JS summary may be dropped at the console boundary and is not the sole completion authority. A final-cycle marker without close has a 15-second grace window, then fails rather than waiting the full device timeout. UI `passed` text alone is not RF gate evidence; the full recipe must exit with a checker pass and `nrf52840_fixture=stopped`. Interrupted terminal commands can bypass process-local `finally`; inspect fixture PID/PGID and console ownership before clearing a stale RN lock, never blindly stop an unrelated provider. The post-fix full recipe passed on the physical iPhone; see the [ten-cycle RF checkpoint](../../../docs/testing/reports/2026-09-27-react-native-ios-nrf-recovery-ten.md). The earlier [interrupted attempt](../../../docs/testing/reports/2026-09-27-react-native-ios-nrf-recovery-ten-incomplete.md) remains incomplete evidence. Keep UI assertions and app-lifecycle waits in the existing signed XCUITest host (`ios/BleHubNitroHostUITests`); the nRF provider owns physical faults, and the independent checker owns BLE semantics. `devicectl --console` remains a bounded transport, not the test oracle; nested RN `:failed:` markers now stop capture immediately, and interrupted capture stops its owned child. Moving RF orchestration wholesale to Detox or another RN E2E tool would not remove the nRF BTP owner, fixture ACKs, or native event-ledger checker. A future XCTest-owned RF UI gate would complement these layers, not silently replace their checker.

The separate `host-ios-device-rf-rssi` gate reuses provider-filtered scan, connection and requested disconnect without requiring GATT discovery. The private Nitro projection dispatches `ReadRssiCommand` and expects a connection- and command-correlated `RssiRead` domain event before the successful terminal, with an exact Int16 reading; the checker compares its value with the final summary. The nRF fixture does not supply a synthetic RSSI value, and the Android deterministic probe rejects RF dispatch. iOS/Android static builds and single-image checks pass. The [unlocked-device RSSI run](../../../docs/testing/reports/2026-09-27-react-native-ios-nrf-rssi.md) completed with a real `-40 dBm` reading, independent checker pass and fixture cleanup; the earlier [locked-device attempt](../../../docs/testing/reports/2026-09-27-react-native-ios-nrf-rssi-blocked.md) remains incomplete evidence.

`host-ios-device-rf-multi` and `host-android-rf-multi` use the same JS `rf-multi` flow and structured checker. The flow requires two explicit DK mappings, scans two distinct service UUIDs, keeps both native connections alive while interleaving per-device read/write/readback operations, then disconnects each connection with exact identity correlation. The controller provisions profiles over independent BTP sessions but never flashes firmware; both boards must first pass transport/BTP/profile readiness. A second board that is only USB/J-Link visible or returns BTP EOF is not RF evidence. One-DK RF results remain valid single-device evidence and must not be relabeled multi-device evidence. Both recipes accept an optional `cycles=N` argument (bounded to 20); each cycle creates a fresh RN probe and independent two-DK fixture lifecycle, preserves a separate log, and stops on the first failure. This is a repeated-cycle pressure gate, not evidence of one uninterrupted long-lived JS process.

For physical iPhone signing, unlock the Mac login keychain through the explicit human-only `pixi run -e apple-ci just apple unlock-login-keychain` recipe; never pass its password into automation. The production macOS CoreBluetooth backend and native Apple consumers remain independently maintained outside this RN host.

### iOS app-specific authorization (non-RF)

`host-ios-device-permission-test` uses the existing UI target and the same workbench, with no Peripheral, private API or global radio change. The complete flow requires initial Authorized + PoweredOn, publicly resets only this App's Bluetooth authorization, uses a consent-staging process and then a fresh audit process to check denied initialize/scan Permission rejection and 17-zero close, then restores authorization and checks fresh initialize/close. Staging process termination has no claimed close/zero result. Authorized phases do not scan, connect or run GATT. Ordinary all-tests skip the destructive path unless the host explicitly opts in.

The result path is relative to `ios/` unless absolute and must be fresh; prior result/log/summary are not overwritten. The optional final `1` selects **restoration-only**, first reading native authorization and avoiding reset if already authorized. Its successful XCTest/summary is not a complete deny/restore pass. The [checkpoint](../../../docs/testing/reports/2026-10-17-rn-ios-permission-authorization.md) records a complete new authorization-only pass after the UI-control fixes, preserves the earlier failures and separates restoration-only evidence. This is not a live-revoke, radio or RF pass. Leave the restored result visible. UI runner errors and cleanup failures remain distinct, and no terminal password is requested.

### iOS Settings radio-control feasibility (non-RF)

`host-ios-device-radio-control-test <udid> <team> <fresh-xcresult-path> [inspect-only=0]` uses the same UI target and the existing workbench authorized audit. It requires exclusive use of the phone, initial authorization/PoweredOn, a unique Settings Bluetooth switch and ON readback. The intended control sequence is OFF/readback → ON/readback with failure restoration, then return to the original native App identity. It is not an RF gate, does not keep a live Probe through the toggle, and is skipped without the explicit recipe. No private setter, Settings URL scheme, Control Center disconnect or coordinate tapping is used.

The [first attempt](../../../docs/testing/reports/2026-10-17-rn-ios-radio-control-feasibility.md) failed before switching the radio: Settings exposed an empty hierarchy/black screenshot despite an unlocked-device readback. The optional final `1` performs only read-only Settings inspection; successful snapshot collection is not control readiness. Full logs/attachments and failed attempts are preserved. This control entry is not offered as a new RF catalog card, does not change Android-only adapter availability and must not run alongside RF/permission gates. Live-attested iOS startup-off and RF recovery remain unverified.

## Android gates

Android now has both clean artifact/static gates and a production-backend RN runtime/RF lane. The RF lane remains private internal evidence and does not freeze the public BLeng/RN API; the current package includes single-DK and two-DK flows.

```text
pixi run just react-native host-android-config-check
pixi run just react-native host-android-build
pixi run just react-native host-android-bundle
pixi run just react-native host-android-single-image-check
pixi run just react-native host-android-artifact-consumer
pixi run just react-native host-android-artifact-device-lifecycle <adb-serial>
pixi run just react-native host-android-runtime <adb-serial>
pixi run just react-native host-android-lifecycle-stress <adb-serial>
pixi run just react-native host-android-rf <adb-serial> rf
pixi run just react-native host-android-rf-lab-suite <adb-serial> <fixture-contract> <fixture-profile>
pixi run just react-native host-android-rf-recovery-ten <adb-serial> <fixture-contract> <fixture-profile>
pixi run just react-native host-android-pending-read-reload <adb-serial> <fixture-contract> <fixture-profile>
pixi run just react-native host-android-rf-adapter-transition <adb-serial> <fixture-contract> <fixture-profile> [startup-off|active-loss]
pixi run just react-native host-android-rf-permission-transition <adb-serial> <fixture-contract> <fixture-profile>
pixi run just react-native host-android-rf <adb-serial> rf-connect
pixi run just react-native host-android-rf <adb-serial> rf-gatt
pixi run just react-native host-android-rf <adb-serial> rf-io
pixi run just react-native host-android-rf <adb-serial> rf-rssi
pixi run just react-native host-android-rf <adb-serial> rf-notification
pixi run just react-native host-android-rf <adb-serial> rf-indication
pixi run just react-native host-android-rf <adb-serial> rf-passive-disconnect
pixi run just react-native host-android-rf <adb-serial> rf-cancellation
pixi run just react-native host-android-rf-multi <adb-serial>
pixi run just react-native host-android-rf-multi <adb-serial> both notification 3
```

`host-android-artifact-consumer` creates a fresh ignored run below `.work/react-native-android-artifact-consumer`, packs only the Android integration whitelist into an npm tarball, installs that tarball into a clean RN host, verifies RN CLI resolves the installed package, and builds against the pre-published fixture Maven repository. It then runs the same Android single-image APK gate. It never uses the integration checkout as a Gradle module dependency and never installs or starts a device. `host-android-artifact-device-lifecycle <adb-serial>` is the explicit physical-device extension: after the static gate it builds the clean host's release JS bundle, checks the release APK single image, installs it through the root-aware flow, and runs the shared lifecycle stress controller. Its generated Metro config resolves the installed package and never points at the checkout. Run the production Android publish recipe first when `.work/staged/android-maven` is absent.

`host-android-runtime <adb-serial>` and RF recipes require a connected phone; only RF needs the standalone nRF52840 fixture. `host-android-rf-lab <adb-serial> [service-uuid]` launches interactive diagnostics without starting a fixture or waiting for a marker. The automated full-suite recipe uses the same `run_rf_lab_full` plan as iOS, with explicit contract/profile, ten RF flows plus RecoveryTen and host-owned evidence. `host-android-lifecycle-stress` and `host-android-runtime-reload` are separate non-RF gates. Pending-read reload uses the actual native engine teardown hook, same PID/two generations and a live-attested held read; Activity/process restart is not equivalent. Historical RF/reload results are indexed in the architecture document, not inherited by the current worktree. Two-DK outer `cycles=N` repetitions are also distinct from the continuous `rf-multi-soak` route.

The adapter recipe's final mode argument is optional: `startup-off` by default, or explicit `active-loss`. It is opt-in and excluded from full-suite. Startup-off has [separate RF evidence](../../../docs/testing/reports/2026-10-16-rn-android-adapter-transition.md). After the explicit bounded `active-gatt-canonical-v1` audit fix and an APK rebuild, **active-loss passed a fresh real RF run** with host/physical/state/bundle checks and offline integrity verification; the first logcat-truncated attempt remains failed. The [active-read radio-loss report](../../../docs/testing/reports/2026-10-16-rn-android-active-read-radio-loss.md) binds the exact APK/session and records `disconnect-first`: each read has one `connection` terminal before the passive disconnect, pre-close route retirement, two closed-zero snapshots and fresh-Probe `00/10/AB` readbacks. This is not same-Hub recovery, in-flight write/CCCD loss, permission transitions, iOS or a final same-candidate full-suite/ABI result.

`host-android-rf-permission-transition` uses the same workbench with three explicit fresh processes: denied startup (Denied snapshot, successful initialize and permission scan terminal), normal RF baseline followed by held + queued reads and CONNECT revocation, then restored-permission fresh-App RF I/O. The host requires exact old PID/UID/user/process/time correlation and OS `ApplicationExitInfo` reason 8. It does not expect old-process terminals, a close Promise or dead-Probe zero accounting. Grants are restored before the phone lease is released; the final radio check is read-only ON. The restored result stays visible on success. This opt-in destructive gate is excluded from full-suite and does not prove same-Hub recovery, active write/CCCD loss, iOS permissions or ABI freeze. See the [host tooling](../../../tools/react_native/README.md) for evidence ownership.

## Local output

Do not commit `node_modules/`, Pods, workspaces generated by CocoaPods, `.xcode.env.local`, Gradle caches, `.cxx`, build output, runtime logs, APKs, or staged native artifacts.
