# Internal React Native consumer

This directory owns the downstream iOS/Android React Native New Architecture host used to validate [`@blehub/react-native-internal`](../../../integrations/react-native-internal/README.md). It is test infrastructure, not a public example or package.

## Topology

- The mobile host uses React Native `0.87.1` for iOS and Android.
- The independent RN macOS feasibility host (`react-native-macos@0.81.9`) was moved to [`../archive/react-native-macos/`](../archive/react-native-macos/). It is not maintained or part of the active gate.
- The mobile host consumes the private Nitro integration and production Stable UniFFI package.
- Host app names and runtime markers retain `BleHubNitroHost` / `BLEHUB_NITRO_RUNTIME` because they identify the Nitro test host, not a public API.

The mobile contract-lab UI uses ordinary React Native views and automatic iOS scroll content insets. It does not use deprecated React Native `SafeAreaView`, NativeWind, or GlueStack. XCUITest remains the appropriate framework for the stateful physical-device UI flow.

## Apple gates

```text
pixi run just react-native host-check
pixi run just react-native host-ios-handoff-test
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
pixi run just react-native host-ios-device-rf-scan <device-udid> <development-team>
pixi run just react-native host-ios-device-rf-connect <device-udid> <development-team>
pixi run just react-native host-ios-device-rf-gatt <device-udid> <development-team>
pixi run just react-native host-ios-device-rf-io <device-udid> <development-team>
pixi run just react-native host-ios-device-rf-rssi <device-udid> <development-team>
pixi run just react-native host-ios-device-rf-notification <device-udid> <development-team>
pixi run just react-native host-ios-device-rf-indication <device-udid> <development-team>
pixi run just react-native host-ios-device-rf-passive-disconnect <device-udid> <development-team>
pixi run just react-native host-ios-device-rf-recovery <device-udid> <development-team>
pixi run just react-native host-ios-device-rf-recovery-ten <device-udid> <development-team>
pixi run just react-native host-ios-device-rf-reload <device-udid> <development-team>
pixi run just react-native host-ios-device-rf-multi <device-udid> <development-team>
pixi run just react-native host-ios-device-rf-multi <device-udid> <development-team> notification 3
# Explicit, non-destructive matrix; one signed build, seven separately checked scenarios:
pixi run just react-native host-ios-device-rf-baseline <device-udid> <development-team>
```

`host-ios-artifact-consumer` is an optional clean, unsigned Simulator build: it stages an explicit iOS-only npm tarball containing generated Nitro glue and the prebuilt production Apple/UniFFI package, installs that tarball into a separate ignored `.work/react-native-ios-artifact-consumer/run-<uuid>/host`, verifies the installed XCFramework bytes and both CocoaPods `-force_load` paths, then performs unsigned generic iOS Simulator and device builds with separate final single-image checks. It requires the pre-existing staged production Apple package; prepare it explicitly before this gate, since the gate does not replace existing staging. It never imports the checkout's module source, flashes a Peripheral, signs, or launches an iPhone. This is an internal artifact feasibility gate, not a publishable cross-platform npm package, RF, or public BLeng API pass. The separate `host-ios-artifact-device-runtime` adds explicit signing, install, Nitro contract runtime and lifecycle checking on a real iPhone; it still is not RF evidence. Each run retains its own ignored logs and refuses to overwrite another run. The [iOS artifact-consumer checkpoint](../../../docs/testing/reports/2026-09-27-react-native-ios-artifact-consumer.md) records the unsigned static result and its limits.

The macOS-hosted Swift handoff test checks a real native bounded queue without CocoaPods or hardware; it does not test native Hub disposal, Nitro's JS queue, or RF. `host-ios-device-rf-baseline` is optional and fail-fast: it builds the signed app once, then runs scan, connect, GATT, I/O, RSSI, Notification and Indication using each scenario's existing independent fixture lifecycle and checker. It does not run the destructive recovery/reload gates, install diagnostic firmware, or replace the per-scenario recipes. An interrupted matrix does not count as a seven-scenario pass; inspect the last scenario's fixture lock/cleanup before retrying.

Full CocoaPods/Xcode output is written below the applicable ignored `build/` directory. Successful quiet commands print the log path; failures print a bounded tail and preserve the original exit code. The physical-iPhone UI recipe runs Xcode without terminal input or a controlling terminal; it never reads a password from project configuration. If signing authorization is unavailable, the OS may still require an explicit human keychain unlock or GUI approval, but this recipe cannot request a password from the agent's terminal.

The non-RF physical-iPhone gates validate signed install/launch, Nitro object creation, bytes/errors, canonical `u64`, completion ordering, listener removal, repeated close/recreate, late-callback silence, bridgeless engine reload/invalidation, and the visible XCUITest recreate flow. The separate RF scan recipe exclusively starts the standalone nRF52840 provider, then requires the iPhone RN JS caller to observe `PoweredOn + Authorized`, dispatch scan through Nitro/UniFFI/production Apple backend, receive a matching FD00 advertisement, request scan stop, observe terminal completion and post-stop silence, and close. One real iPhone scan-only RF flow and a corrected checker pass are documented in the [RN iPhone-to-nRF checkpoint](../../../docs/testing/reports/2026-09-25-react-native-ios-nrf-scan.md); a later retry was blocked by the device lock and overwrote the ignored runtime log. This scan-only result does not validate connection, GATT, subscription, or the future BLeng surface; the full engine reload gate does not claim Fast Refresh semantics. The separate `host-ios-device-rf-connect` gate uses the same production stack to require ordered, ID-correlated `Connecting` → `Connected` → connect terminal and requested `Disconnected` → disconnect terminal, followed by one-second JS-observed silence, explicit listener removal, and close. Its signed physical-device run has now passed with exact connection/disconnection ID correlation and fixture cleanup; see the [connection RF checkpoint](../../../docs/testing/reports/2026-09-25-react-native-ios-nrf-connect.md). A later scan-only retry was blocked by an iPhone auto-lock, so keep the device unlocked through app launch. The separate `host-ios-device-rf-gatt` gate extends the same JS-driven flow through aggregate service/characteristic/descriptor discovery, validates the nRF profile's five characteristic instances and two CCCD descriptors, then performs requested disconnect and post-disconnect silence. The completed physical-device result is documented in the [RN iPhone-to-nRF GATT checkpoint](../../../docs/testing/reports/2026-09-26-react-native-ios-nrf-gatt.md). It proves the private RN low-level GATT projection only; it does not prove characteristic I/O, subscriptions, strict Indication, or a BLeng API. The separate `host-ios-device-rf-io` gate extends the same flow through initial read (`00`), write with response (`10`) plus readback, write without response (`AB`) with an explicitly local-submission terminal, and a separate readback of `AB`, followed by requested disconnect and silence. Its completed physical-device result is documented in the [RN iPhone-to-nRF GATT I/O checkpoint](../../../docs/testing/reports/2026-09-26-react-native-ios-nrf-gatt-io.md). It proves the private RN low-level characteristic I/O projection only; it does not prove subscriptions, strict Indication, recovery, or a BLeng API. The separate `host-ios-device-rf-notification` gate uses the validated `fff20000` updates characteristic and requires `PlatformDefault` to resolve to actual `Notification`, exact `EF` value delivery, disable terminal ordering, two seconds of post-disable value silence, requested disconnect, and post-disconnect silence. The separate `host-ios-device-rf-indication` gate first requires strict `RequireIndication` on dual-property `fff20000` to terminate with `NotSupported`, then uses pure-Indication `fff50000` and requires actual `Indication`, exact `EF` value delivery, disable terminal ordering, two seconds of stale-value silence, requested disconnect, and post-disconnect silence. Both are private RN subscription gates; passive recovery and the future BLeng API remain separate.

The optional 10-cycle gate borrows the repeated connect/discover/disconnect scenario shape from BLE library examples, but uses one production RN probe/listener, disjoint per-cycle IDs, fixture-driven passive faults, and an independently checked event ledger. `devicectl --console` is only the log transport: the bounded console controller stops at the observed `close:passed` marker, while the checker still requires all ten ordered cycles, exact correlation, and close with no failure. The final JS summary may be dropped at the console boundary and is not the sole completion authority. A final-cycle marker without close has a 15-second grace window, then fails rather than waiting the full device timeout. UI `passed` text alone is not RF gate evidence; the full recipe must exit with a checker pass and `nrf52840_fixture=stopped`. Interrupted terminal commands can bypass process-local `finally`; inspect fixture PID/PGID and console ownership before clearing a stale RN lock, never blindly stop an unrelated provider. The post-fix full recipe passed on the physical iPhone; see the [ten-cycle RF checkpoint](../../../docs/testing/reports/2026-09-27-react-native-ios-nrf-recovery-ten.md). The earlier [interrupted attempt](../../../docs/testing/reports/2026-09-27-react-native-ios-nrf-recovery-ten-incomplete.md) remains incomplete evidence. Keep UI assertions and app-lifecycle waits in the existing signed XCUITest host (`ios/BleHubNitroHostUITests`); the nRF provider owns physical faults, and the independent checker owns BLE semantics. `devicectl --console` remains a bounded transport, not the test oracle; nested RN `:failed:` markers now stop capture immediately, and interrupted capture stops its owned child. Moving RF orchestration wholesale to Detox or another RN E2E tool would not remove the nRF BTP owner, fixture ACKs, or native event-ledger checker. A future XCTest-owned RF UI gate would complement these layers, not silently replace their checker.

The separate `host-ios-device-rf-rssi` gate reuses provider-filtered scan, connection and requested disconnect without requiring GATT discovery. The private Nitro projection dispatches `ReadRssiCommand` and expects a connection- and command-correlated `RssiRead` domain event before the successful terminal, with an exact Int16 reading; the checker compares its value with the final summary. The nRF fixture does not supply a synthetic RSSI value, and the Android deterministic probe rejects RF dispatch. iOS/Android static builds and single-image checks pass. The [unlocked-device RSSI run](../../../docs/testing/reports/2026-09-27-react-native-ios-nrf-rssi.md) completed with a real `-40 dBm` reading, independent checker pass and fixture cleanup; the earlier [locked-device attempt](../../../docs/testing/reports/2026-09-27-react-native-ios-nrf-rssi-blocked.md) remains incomplete evidence.

`host-ios-device-rf-multi` and `host-android-rf-multi` use the same JS `rf-multi` flow and structured checker. The flow requires two explicit DK mappings, scans two distinct service UUIDs, keeps both native connections alive while interleaving per-device read/write/readback operations, then disconnects each connection with exact identity correlation. The controller provisions profiles over independent BTP sessions but never flashes firmware; both boards must first pass transport/BTP/profile readiness. A second board that is only USB/J-Link visible or returns BTP EOF is not RF evidence. One-DK RF results remain valid single-device evidence and must not be relabeled multi-device evidence. Both recipes accept an optional `cycles=N` argument (bounded to 20); each cycle creates a fresh RN probe and independent two-DK fixture lifecycle, preserves a separate log, and stops on the first failure. This is a repeated-cycle pressure gate, not evidence of one uninterrupted long-lived JS process.

For physical iPhone signing, unlock the Mac login keychain through the explicit human-only `pixi run -e apple-ci just apple unlock-login-keychain` recipe; never pass its password into automation. The production macOS CoreBluetooth backend and native Apple consumers remain independently maintained outside this RN host.

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
pixi run just react-native host-android-rf <adb-serial> rf-connect
pixi run just react-native host-android-rf <adb-serial> rf-gatt
pixi run just react-native host-android-rf <adb-serial> rf-io
pixi run just react-native host-android-rf <adb-serial> rf-rssi
pixi run just react-native host-android-rf <adb-serial> rf-notification
pixi run just react-native host-android-rf <adb-serial> rf-indication
pixi run just react-native host-android-rf <adb-serial> rf-passive-disconnect
pixi run just react-native host-android-rf-multi <adb-serial>
pixi run just react-native host-android-rf-multi <adb-serial> both notification 3
```

`host-android-artifact-consumer` creates a fresh ignored run below `.work/react-native-android-artifact-consumer`, packs only the Android integration whitelist into an npm tarball, installs that tarball into a clean RN host, verifies RN CLI resolves the installed package, and builds against the pre-published fixture Maven repository. It then runs the same Android single-image APK gate. It never uses the integration checkout as a Gradle module dependency and never installs or starts a device. `host-android-artifact-device-lifecycle <adb-serial>` is the explicit physical-device extension: after the static gate it builds the clean host's release JS bundle, checks the release APK single image, installs it through the root-aware flow, and runs the shared lifecycle stress controller. Its generated Metro config resolves the installed package and never points at the checkout. Run the production Android publish recipe first when `.work/staged/android-maven` is absent.

`host-android-runtime <adb-serial>` and the RF recipes require a connected device; only the RF recipes require the standalone nRF52840 DK. The runtime controller fails fast on structured JS failure markers, waits for the terminal marker instead of sleeping a fixed duration, writes a bounded log, and force-stops only the owned host package. `host-android-lifecycle-stress <adb-serial>` is the host-only close/recreate, listener-removal, late-callback and post-close-dispatch gate, and passed on the OnePlus 11 in the 2026-10-03 run. Android engine reload/invalidation is intentionally not claimed yet: the active host still lacks a verified RN 0.87 Android runtime-reload hook, and an Activity/process restart would not prove JS runtime invalidation. Android and iOS two-DK `cycles=2` repeated-cycle gates now pass; they preserve per-cycle logs but are not uninterrupted long-process soak evidence. Pending Promise/queue-overflow behavior remains unverified.

## Local output

Do not commit `node_modules/`, Pods, workspaces generated by CocoaPods, `.xcode.env.local`, Gradle caches, `.cxx`, build output, runtime logs, APKs, or staged native artifacts.
