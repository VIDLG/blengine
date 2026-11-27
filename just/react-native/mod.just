set working-directory := "../.."

bleng_integration := "integrations/react-native-bleng"
formal_host := "tests/consumers/react-native-bleng"
# Expected export path only: no contract is generated or borrowed from v1.
fault_contract := "tests/peripherals/nrf52840/firmware/contracts/multifunction-rf-v2-a.json"
fault_profile := "tests/peripherals/nrf52840/firmware/profiles/multifunction-rf-v2-a.json"

# The formal consumer is intentionally independent from the transitional internal host.
[working-directory("tests/consumers/react-native-bleng")]
formal-host-install:
    npm ci --ignore-scripts --no-audit --no-fund

# Read-only dependency use: bootstrap/lock changes require explicit formal-host-install.
[working-directory("tests/consumers/react-native-bleng")]
formal-host-check:
    npm run typecheck
    node -e 'const {execFileSync}=require("node:child_process"); const config=JSON.parse(execFileSync("./node_modules/.bin/react-native",["config","--platform","android"],{encoding:"utf8"})); const dependency=config.dependencies["@blehub/react-native-bleng"]; if (!dependency?.platforms?.android) throw new Error("formal RN Android dependency was not autolinked"); if (config.dependencies["@blehub/react-native-internal"]) throw new Error("transitional internal dependency leaked into formal host"); console.log("formal RN consumer autolinking passed")'

# Debug includes native contract tests; explicit release packages JS without Metro.
formal-host-android-build variant="debug": formal-host-check
    pixi run python tools/react_native/run_quiet.py --no-tty --progress-seconds 20 {{ quote(".work/logs/formal-host-android-" + variant + "-build.log") }} -- pixi run -e android {{ formal_host }}/android/gradlew -p {{ formal_host }}/android {{ if variant == "debug" { ":app:assembleDebug :app:assembleDebugAndroidTest" } else if variant == "release" { ":app:assembleRelease" } else { error("formal-host-android-build variant must be debug or release") } }} --no-daemon

# Formal App tests mock only the package boundary, not the transitional probe API.
[working-directory("tests/consumers/react-native-bleng")]
formal-host-test:
    npm test

# Ordinary builds verify local Pods evidence; only explicit true permits refresh/network.
formal-host-ios-build refresh_pods="false": stage-apple-bleng bleng-check formal-host-check
    pixi run python tools/react_native/run_quiet.py --no-tty --progress-seconds 20 .work/logs/formal-host-ios-build.log -- pixi run -e apple-ci python -m tools.react_native.bleng_ios_consumer {{ if refresh_pods == "true" { "--refresh-pods" } else if refresh_pods == "false" { "" } else { error("refresh_pods must be true or false") } }}

# Signed App for one physical iPhone. Uses only signing material already on this Mac
# (Team, Apple Development identity, downloaded development profile) and never requests
# a provisioning update, registers a device or touches an Apple account. Pods are keyed by
# the package's own spec/bridge hashes, so a changed spec needs the explicit refresh: a
# device build must never silently compile against an older preparation than it claims.
[macos]
formal-host-ios-device-build device refresh_pods="false":
    pixi run just react-native::stage-apple-bleng
    pixi run just react-native::bleng-check
    pixi run just react-native::formal-host-check
    pixi run python tools/react_native/run_quiet.py --no-tty --progress-seconds 20 .work/logs/formal-host-ios-device-build.log -- pixi run -e apple-ci python -m tools.react_native.bleng_ios_consumer --device {{ quote(device) }} {{ if refresh_pods == "true" { "--refresh-pods" } else if refresh_pods == "false" { "" } else { error("refresh_pods must be true or false") } }}

# Pure JVM retirement mechanics; no device, BLE initialization or tool installation.
bleng-android-unit-test:
    pixi run python tools/react_native/run_quiet.py --no-tty --progress-seconds 20 .work/logs/bleng-android-unit-test.log -- pixi run -e android gradle -p {{ bleng_integration }}/android testDebugUnitTest --offline --no-daemon

# Native bridge contracts are distinct from the UI scene lanes. Device execution is opt-in.
formal-host-android-native-test device: formal-host-android-build
    test -n {{ quote(device) }}
    pixi run -e android python -m tools.android.run_instrumentation --device physical --serial {{ quote(device) }} --app-apk {{ formal_host }}/android/app/build/outputs/apk/debug/app-debug.apk --test-apk {{ formal_host }}/android/app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk --test-component com.blehubblenghost.test/androidx.test.runner.AndroidJUnitRunner --test-class com.blehubblenghost.HybridBlengCentralInstrumentedTest --log {{ formal_host }}/android/build/native-contract-test.log --timeout 120

# Platform-simulated companion presence through the real backend and the library's own
# CompanionDeviceService. CDM creates a synthetic association for this package and simulates
# appear/disappear; no chooser, no physical peer and no RF. The test creates and removes the
# association itself, and asserts the presence lane reports both transitions with one key.
formal-host-android-companion-presence device: formal-host-android-build
    test -n {{ quote(device) }}
    pixi run -e android python -m tools.android.run_instrumentation --device physical --serial {{ quote(device) }} --app-apk {{ formal_host }}/android/app/build/outputs/apk/debug/app-debug.apk --test-apk {{ formal_host }}/android/app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk --test-component com.blehubblenghost.test/androidx.test.runner.AndroidJUnitRunner --test-class com.blehubblenghost.BlengCompanionPresenceInstrumentedTest --log {{ formal_host }}/android/build/companion-presence-test.log --timeout 180

# Real system companion chooser through the library's own `beginAssociateCompanion(..., true)`.
# Requires the advertising nRF52840 fixture on the filtered service; the test first proves the
# phone can see it, selects the single LE-filtered row with key events (the chooser exposes no
# accessibility nodes), then asserts the library armed presence and the fixture appears with one
# owner-scoped key. The disappearance uses CDM's own hook so the lane stays bounded.
formal-host-android-companion-association device: formal-host-android-build
    test -n {{ quote(device) }}
    pixi run -e android python -m tools.android.run_instrumentation --device physical --serial {{ quote(device) }} --app-apk {{ formal_host }}/android/app/build/outputs/apk/debug/app-debug.apk --test-apk {{ formal_host }}/android/app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk --test-component com.blehubblenghost.test/androidx.test.runner.AndroidJUnitRunner --test-class com.blehubblenghost.BlengCompanionAssociationInstrumentedTest#chooserAssociationArmsPresenceAndReportsTheAdvertisingPeer --log {{ formal_host }}/android/build/companion-association-test.log --timeout 240

# The final human-in-the-loop transition. Start the advertising fixture, run this, and when the
# real appear lands (~20 s) carry the DK out of range (or power it off). The test waits up to 180 s
# for the real disappearance and asserts it keeps the same owner-scoped key. The same transition
# has also been verified automatically by stopping the fixture's advertising.
formal-host-android-companion-peer-leaves device: formal-host-android-build
    test -n {{ quote(device) }}
    pixi run -e android python -m tools.android.run_instrumentation --device physical --serial {{ quote(device) }} --app-apk {{ formal_host }}/android/app/build/outputs/apk/debug/app-debug.apk --test-apk {{ formal_host }}/android/app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk --test-component com.blehubblenghost.test/androidx.test.runner.AndroidJUnitRunner --test-class com.blehubblenghost.BlengCompanionAssociationInstrumentedTest#realDisappearanceAfterThePeerLeaves --log {{ formal_host }}/android/build/companion-peer-leaves-test.log --timeout 300

# Real-device engine reload gate: a test-owned ReactHost proves RN delivers invalidation.
formal-host-android-engine-reload-test device: formal-host-android-build
    test -n {{ quote(device) }}
    pixi run -e android python -m tools.android.run_instrumentation --device physical --serial {{ quote(device) }} --app-apk {{ formal_host }}/android/app/build/outputs/apk/debug/app-debug.apk --test-apk {{ formal_host }}/android/app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk --test-component com.blehubblenghost.test/androidx.test.runner.AndroidJUnitRunner --test-class com.blehubblenghost.BlengEngineReloadInstrumentedTest --log {{ formal_host }}/android/build/engine-reload-test.log --timeout 300

[working-directory("tests/consumers/react-native-bleng/ios")]
formal-host-ios-native-test simulator: formal-host-ios-build
    test -n {{ quote(simulator) }}
    pixi run python ../../../../tools/react_native/run_quiet.py --no-tty --progress-seconds 20 build/native-test-run.log -- pixi run -e apple-ci xcodebuild -quiet -workspace BleHubBlengHost.xcworkspace -scheme BleHubBlengHost -configuration Debug -destination {{ quote("platform=iOS Simulator,id=" + simulator) }} -derivedDataPath build/simulator-derived CODE_SIGNING_ALLOWED=NO -only-testing:BleHubBlengHostNativeTests -parallel-testing-enabled NO -collect-test-diagnostics never test-without-building

# Release bundled UI gate. The UI-only scheme builds App + UI tests without the
# native @testable target, so no testability override enters the production App.
[working-directory("tests/consumers/react-native-bleng/ios")]
formal-host-ios-ui-test simulator: formal-host-ios-build
    test -n {{ quote(simulator) }}
    pixi run python ../../../../tools/react_native/run_quiet.py --no-tty --progress-seconds 20 build/ui-test-build.log -- pixi run -e apple-ci xcodebuild -quiet -workspace BleHubBlengHost.xcworkspace -scheme BleHubBlengHostUISimulator -configuration Release -sdk iphonesimulator -destination 'generic/platform=iOS Simulator' -derivedDataPath build/simulator-ui-derived CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO build-for-testing
    pixi run python ../../../../tools/react_native/run_quiet.py --no-tty --progress-seconds 20 build/ui-test-run.log -- pixi run -e apple-ci xcodebuild -quiet -workspace BleHubBlengHost.xcworkspace -scheme BleHubBlengHostUISimulator -configuration Release -destination {{ quote("platform=iOS Simulator,id=" + simulator) }} -derivedDataPath build/simulator-ui-derived CODE_SIGNING_ALLOWED=NO -only-testing:BleHubBlengHostUITests -parallel-testing-enabled NO -collect-test-diagnostics never test-without-building

# Explicit managed scan: prebuilt candidate, test APK and exact fixture identity/contract.
# The scene runs inside the App's own instrumentation; no build, firmware provisioning or
# reset is a prerequisite, and every managed scene shares this one entry point.
[positional-arguments]
formal-host-android-managed-scan +args:
    pixi run python -m tools.react_native.run_bleng_android_scan --scene scan "$@"

# Every managed Android scene in one command, in dependency order: the normal path, then
# the fault-armed scenes, then one and ten recovery cycles. Each scene keeps its own run id,
# phone lease and evidence root (<output>/<scene>); the sequence stops at the first scene
# that does not pass, and hands a repeating scene the budget that scene itself declares.
[positional-arguments]
formal-host-android-managed-sequence +args:
    pixi run python -m tools.react_native.run_bleng_android_scenes "$@"

# Explicit managed notification scene: same producer/admission as the scan scene; the
# scene fixes the evidence scope and the expected trace, the profile fixes the target.
[positional-arguments]
formal-host-android-managed-notification +args:
    pixi run python -m tools.react_native.run_bleng_android_scan --scene notification "$@"

# Native notification scene: the App process itself opens the subscription and the host
# proves what crossed the air. No UI automation server is involved; requires the frozen
# App and test APKs plus explicit install/fixture-start authorization.
[positional-arguments]
formal-host-android-native-notification +args:
    pixi run python -m tools.react_native.run_bleng_android_native_notification "$@"

# Multi-link isolation on one owner: the host ends the first declared peer's link and every
# other declared peer must still deliver a value applied *after* that end. Each `--peer`
# carries its own port, contract, profile and canonical identity, so the topology is the
# declaration, not this recipe. No UI automation server is involved.
[positional-arguments]
formal-host-android-native-multi-link +args:
    pixi run python -m tools.react_native.run_bleng_android_multi_link "$@"

# Adapter OFF -> ON recovery on the formal App's own native surface: the host owns the phone
# radio and the fixture, the App owns the reported adapter state, the refused scan and its own
# retry once the radio is back. No UI automation server and no retired consumer App in the path.
[positional-arguments]
formal-host-android-native-adapter-transition +args:
    pixi run python -m tools.react_native.run_bleng_android_adapter_transition "$@"

# Permission transition on the formal App's own native surface: three processes (denied /
# active read parked in the fixture then ended by the OS / restored). The host owns every
# permission mutation and the reason-8 death proof; the App owns only the product facts.
[positional-arguments]
formal-host-android-native-permission-transition +args:
    pixi run python -m tools.react_native.run_bleng_android_permission_transition "$@"

# The same scene on the iPhone lane: a hosted XCTest bundle inside the signed formal App drives
# the real Nitro surface, and stage markers come from the device syslog. The App and the
# `build-for-testing` manifest are inputs, so this entry never builds, signs or installs on its
# own authority.
[positional-arguments]
formal-host-ios-native-multi-link +args:
    pixi run python -m tools.react_native.run_bleng_ios_multi_link "$@"

# App-specific Bluetooth authorization on the formal App's own UI test target: allowed -> denied
# -> allowed, read from the App's initialization snapshot. Consumes the signed App + manifest from
# `formal-host-ios-device-test-build`; never builds, signs or touches radio/fixture. The only
# destructive action is this App's authorization reset/restore, hence the explicit flag.
[positional-arguments]
formal-host-ios-permission-authorization +args:
    pixi run python -m tools.react_native.run_bleng_ios_permission_ui "$@"

# Drive one UI scene in-process with XCTest: no WebDriverAgent, Appium server or tunnel.
# The host still owns fixture/lease/evidence; this entry only drives identifiers and
# returns per-step timings. Plans are host-owned (`smoke`, `scan`).
[positional-arguments]
formal-host-ios-scene-driver +args:
    pixi run python -m tools.react_native.run_ios_scene_driver "$@"

# Produce the two inputs that entry needs (signed App + manifest) from the formal host project,
# using only signing material already on this Mac. Never installs, launches or registers.
#
# The signed product links the *staged* Apple backend, not `platforms/apple`, so the same
# prerequisites as the host build apply: a Swift fix that was never staged would otherwise ship
# silently and look like a device bug.
[macos]
[positional-arguments]
formal-host-ios-device-test-build +args: stage-apple-bleng bleng-check formal-host-check
    pixi run python -m tools.react_native.run_bleng_ios_device_test_build "$@"

# Explicit managed indication scene; a strict indication CCC, not a notification fallback.
[positional-arguments]
formal-host-android-managed-indication +args:
    pixi run python -m tools.react_native.run_bleng_android_scan --scene indication "$@"

# Explicit managed read/write scene: read the profile's read-write characteristic, write
# a declared payload, read it back (a write receipt is not a stored value) and read RSSI.
[positional-arguments]
formal-host-android-managed-io +args:
    pixi run python -m tools.react_native.run_bleng_android_scan --scene io "$@"

# Explicit managed cancellation scene: the fixture holds the first read, the App queues a
# second, the UI cancels the queued one, the held read is released and completes, then a
# later read proves the lane recovered. Fault-armed, so it stays a separate entry.
[positional-arguments]
formal-host-android-managed-cancellation +args:
    pixi run python -m tools.react_native.run_bleng_android_scan --scene cancellation "$@"

# Managed iOS scan: the same evidence vocabulary as the Android scenes with the iOS
# preparation steps instead — frozen .app tree, a host-built WebDriverAgent runner,
# no helper APKs and no permission scope. Pass --app-tree-sha256 from the reviewed
# signed device build; the producer refuses a different tree.
[positional-arguments]
formal-host-ios-managed-scan +args:
    pixi run python -m tools.react_native.run_bleng_ios_scan --scene scan "$@"

[positional-arguments]
formal-host-ios-managed-notification +args:
    pixi run python -m tools.react_native.run_bleng_ios_scan --scene notification "$@"

[positional-arguments]
formal-host-ios-managed-indication +args:
    pixi run python -m tools.react_native.run_bleng_ios_scan --scene indication "$@"

# The same read/write scene on the iPhone lane.
[positional-arguments]
formal-host-ios-managed-io +args:
    pixi run python -m tools.react_native.run_bleng_ios_scan --scene io "$@"

# The fault-armed cancellation scene on the iPhone lane, on its own: one held read, one
# queued and cancelled read, then a recovery read. The sequence runs it too; this entry is
# for a targeted rerun.
[positional-arguments]
formal-host-ios-managed-cancellation +args:
    pixi run python -m tools.react_native.run_bleng_ios_scan --scene cancellation "$@"

# Explicit managed write-reject scene: the firmware parks the exact write and the host
# refuses it, so the App must report a failed write with the peer's ATT status, change
# nothing, and then write and read back normally.
[positional-arguments]
formal-host-android-managed-write-reject +args:
    pixi run python -m tools.react_native.run_bleng_android_scan --scene write-reject "$@"

# Explicit managed CCC-enable-reject scene: the firmware refuses the subscription's own
# CCC write, the App must end that attempt as a failed subscription, and a second enable
# must subscribe and deliver a value the host emitted.
[positional-arguments]
formal-host-android-managed-ccc-enable-reject +args:
    pixi run python -m tools.react_native.run_bleng_android_scan --scene ccc-enable-reject "$@"

# Explicit managed CCC-disable-reject scene: the refused disable must leave the
# subscription subscribed and still delivering; a second disable retires it.
[positional-arguments]
formal-host-android-managed-ccc-disable-reject +args:
    pixi run python -m tools.react_native.run_bleng_android_scan --scene ccc-disable-reject "$@"

# A peer-initiated link end: the host ends the link from the peripheral side, and the App
# must announce it by itself — from the connection's own end lane, attributed to nobody
# here — then hold that reason over a stated quiet window with no command submitted.
[positional-arguments]
formal-host-android-managed-passive-disconnect +args:
    pixi run python -m tools.react_native.run_bleng_android_scan --scene passive-disconnect "$@"

# The same link scene on the iPhone lane.
[positional-arguments]
formal-host-ios-managed-passive-disconnect +args:
    pixi run python -m tools.react_native.run_bleng_ios_scan --scene passive-disconnect "$@"

# The operator's own end of a live link: read once, subscribe, receive one emitted value,
# then submit the App's own disconnect. The App must attribute the end to the operator (not
# to a peer), retire the subscription with the link, count no further value, and withdraw
# every control that needs a link.
[positional-arguments]
formal-host-android-managed-disconnect +args:
    pixi run python -m tools.react_native.run_bleng_android_scan --scene disconnect "$@"

[positional-arguments]
formal-host-ios-managed-disconnect +args:
    pixi run python -m tools.react_native.run_bleng_ios_scan --scene disconnect "$@"

# The peer's own CCCD across a subscription's whole life: read it disabled, subscribe, read it
# enabled, write back the same bytes, read it unchanged, disable, read it cleared. The descriptor
# is the second witness a subscription claim otherwise never has.
[positional-arguments]
formal-host-android-managed-descriptor-io +args:
    pixi run python -m tools.react_native.run_bleng_android_scan --scene descriptor-io "$@"

[positional-arguments]
formal-host-ios-managed-descriptor-io +args:
    pixi run python -m tools.react_native.run_bleng_ios_scan --scene descriptor-io "$@"

# The platform's own `connected` inventory, checked against the link this run holds: the peer
# the App is linked to must appear in the list, matched by the owner-scoped key. A list that
# merely came back is not the claim.
[positional-arguments]
formal-host-android-managed-devices-query +args:
    pixi run python -m tools.react_native.run_bleng_android_scan --scene devices-query "$@"

[positional-arguments]
formal-host-ios-managed-devices-query +args:
    pixi run python -m tools.react_native.run_bleng_ios_scan --scene devices-query "$@"

# The two inventories the observation main line never needs: `bonded` and `companion`. No peer
# and no link — each platform either reports its own inventory or refuses the capability.
[positional-arguments]
formal-host-android-managed-devices-kinds +args:
    pixi run python -m tools.react_native.run_bleng_android_scan --scene devices-kinds "$@"

[positional-arguments]
formal-host-ios-managed-devices-kinds +args:
    pixi run python -m tools.react_native.run_bleng_ios_scan --scene devices-kinds "$@"

# Radio power, both directions. An adapter fact with no peer and no link: each answer is either
# the platform changing the radio or refusing with its own code, and both platforms we test
# refuse (API 33+ excludes ordinary apps; CoreBluetooth exposes no radio control).
[positional-arguments]
formal-host-android-managed-adapter-control +args:
    pixi run python -m tools.react_native.run_bleng_android_scan --scene adapter-control "$@"

[positional-arguments]
formal-host-ios-managed-adapter-control +args:
    pixi run python -m tools.react_native.run_bleng_ios_scan --scene adapter-control "$@"

# A standing "connect when the peer appears" request: the host withdraws the peer, the App
# asks for a link while it is gone, the host restores it, and the platform either links or
# refuses the capability as itself.
[positional-arguments]
formal-host-android-managed-auto-connect +args:
    pixi run python -m tools.react_native.run_bleng_android_scan --scene auto-connect "$@"

[positional-arguments]
formal-host-ios-managed-auto-connect +args:
    pixi run python -m tools.react_native.run_bleng_ios_scan --scene auto-connect "$@"

# Facts about the live link itself: query the MTU, request one, ask for high connection
# priority, and invalidate the GATT cache. Each answer is either the platform doing it or the
# platform saying it has no such capability; the App must not turn either into a timeout.
[positional-arguments]
formal-host-android-managed-link-facts +args:
    pixi run python -m tools.react_native.run_bleng_android_scan --scene link-facts "$@"

[positional-arguments]
formal-host-ios-managed-link-facts +args:
    pixi run python -m tools.react_native.run_bleng_ios_scan --scene link-facts "$@"

# A subscription that must outlive a background/foreground cycle: the App is sent away for a
# declared dwell, the host emits while it is away, and the value is only observable after it
# returns. Its own entry because the dwell makes it the slowest scene to debug in a sequence.
[positional-arguments]
formal-host-android-managed-background +args:
    pixi run python -m tools.react_native.run_bleng_android_scan --scene background "$@"

[positional-arguments]
formal-host-ios-managed-background +args:
    pixi run python -m tools.react_native.run_bleng_ios_scan --scene background "$@"

# A connect the peer cannot answer: the scan matches the fixture, the host then withdraws its
# advertising (proved on the controller's own readback), and the App submits a connect it can
# never complete. The App must report the failure as a failure — no link, no invented end, the
# identity its own record carries — and withdraw every control that needs a link.
[positional-arguments]
formal-host-android-managed-connect-failure +args:
    pixi run python -m tools.react_native.run_bleng_android_scan --scene connect-failure "$@"

[positional-arguments]
formal-host-ios-managed-connect-failure +args:
    pixi run python -m tools.react_native.run_bleng_ios_scan --scene connect-failure "$@"

# N peer-initiated ends on one owner: every cycle repeats scan/link/discover/read on the
# *same* owner, so a stale capability or a needed owner replacement shows up as a cycle
# that cannot reach its own read.
[positional-arguments]
formal-host-android-managed-recovery +args:
    pixi run python -m tools.react_native.run_bleng_android_scan --scene recovery "$@"

[positional-arguments]
formal-host-ios-managed-recovery +args:
    pixi run python -m tools.react_native.run_bleng_ios_scan --scene recovery "$@"

# The same cycle ten times on one owner. The scene declares the budget it needs (~9 s per
# iOS click and six clicks per cycle), so the run must declare at least that: `--timeout`
# below the scene's own minimum is refused before the App is driven.
[positional-arguments]
formal-host-android-managed-recovery-ten +args:
    pixi run python -m tools.react_native.run_bleng_android_scan --scene recovery-ten "$@"

[positional-arguments]
formal-host-ios-managed-recovery-ten +args:
    pixi run python -m tools.react_native.run_bleng_ios_scan --scene recovery-ten "$@"

# The same three fault-armed scenes on the iPhone lane.
[positional-arguments]
formal-host-ios-managed-write-reject +args:
    pixi run python -m tools.react_native.run_bleng_ios_scan --scene write-reject "$@"

[positional-arguments]
formal-host-ios-managed-ccc-enable-reject +args:
    pixi run python -m tools.react_native.run_bleng_ios_scan --scene ccc-enable-reject "$@"

[positional-arguments]
formal-host-ios-managed-ccc-disable-reject +args:
    pixi run python -m tools.react_native.run_bleng_ios_scan --scene ccc-disable-reject "$@"

# Release this repository's own formal iOS App if it is still running. A passing scene
# keeps it as evidence and the platform can restore it later, so the next run would refuse
# takeover. Attribution is the device's own installation for this bundle, it terminates at
# most one process, and it is not a resource-zero, radio or gate claim.
[positional-arguments]
formal-host-ios-release-app +args:
    pixi run python -m tools.react_native.ios_app_cleanup "$@"

# Every managed iOS scene in one command, in the same dependency order as the Android
# sequence (scene_sequence.ORDER). Each scene keeps its own run id, phone lease and evidence
# root (<output>/<scene>), the sequence stops at the first scene that does not pass, and a
# repeating scene is handed the budget it declares. --release-retained-app is always applied,
# because a retained App would block the next scene's takeover.
[positional-arguments]
formal-host-ios-managed-sequence +args:
    pixi run python -m tools.react_native.run_bleng_ios_scenes "$@"

# Offline scan replay cannot authorize hardware or upgrade the RF verdict.
formal-host-scan-verify attempt:
    pixi run python -m tools.react_native.bleng_scan_evidence {{ quote(attempt) }}

# Stage the combined Apple BLeng UniFFI image and the canonical Apple backend for
# the formal React Native BLeng package. This never mutates apple-production.
[private]
stage-apple-bleng:
    pixi run just ffi apple-bleng-package
    test -f .work/staged/apple-bleng/Sources/BleHubUniFfi/bleng.swift
    test -f .work/staged/apple-bleng/Sources/BleHubUniFfi/blehub.swift
    test -d .work/staged/apple-bleng/BleHubUniFfiNative.xcframework
    rm -rf .work/staged/apple-bleng-react-native {{ bleng_integration }}/apple-pods
    pixi run python -m tools.react_native.stage_bleng_apple
    test -f .work/staged/apple-bleng-react-native/BleHubUniFfi/Sources/BleHubUniFfi/bleng.swift
    test -f .work/staged/apple-bleng-react-native/BleHubApple/BleHubApple.podspec
    test -d .work/staged/apple-bleng-react-native/BleHubApple/Sources/BleHubApple

# Validate the formal BLeng Nitro spec and refresh its ignored generated glue.
[private]
bleng-check:
    npm --prefix {{ bleng_integration }} run check

# The runnable example's type layer is its contract: it compiles against the same package source a
# consumer resolves. Reuses the formal host's installed toolchain (no second install, nothing
# written into the example) and fails closed when that toolchain is absent.
quickstart-typecheck:
    pixi run python -m tools.react_native.quickstart_typecheck

# The example's Babel config is executed, not assumed: every source must compile with it and every
# component must actually be transformed by React Compiler. Same host toolchain, example read-only.
quickstart-compile:
    pixi run python -m tools.react_native.quickstart_compile

# Signature-level reference generated from the package's public entry point (`src/index.ts`). The
# site is derived from the specs, never hand-edited, and behaviour contracts stay in API.md /
# API.zh.md — see docs/research/react-native/typedoc-site.md.
api-site:
    npm run docs:api

# Same entry point validated without writing a site; any TypeDoc warning fails the check.
api-site-check:
    npm run docs:api:check

# Hardware-free tooling regressions. One discovery root: `tools/tests` holds the tool
# suites, `tools/tests/react_native` the RN lanes, so a new test file is found by adding it
# to that tree — never by naming a runner.
#
# Parallel by default: these cases are hardware-free and file-independent, and `loadfile`
# keeps one module on one worker, so counts are identical and the wall clock is a third
# (597 s -> 161 s on ten cores). `-k` narrows a targeted regression.
# `tools-fixture-test` deliberately stays on `unittest discover`: it also loads the
# peripheral tree, which must not be collected by pytest.
tools-test filter="":
    python -m pytest tools/tests -q -n auto --dist loadfile --durations=10 {{ if filter != "" { "-k " + quote(filter) } else { "" } }}

# Stable hardware-free responsibility slices; each remains a filter over the same discovery.
tools-test-android-safety:
    python -m pytest tools/tests -q -n auto --dist loadfile --durations=10 -k android_
tools-test-lifecycle:
    python -m pytest tools/tests -q -n auto --dist loadfile --durations=10 -k lifecycle
# The device-observed population: real device answers, not synthesized payloads. Same
# discovery, one filter, so `-k regression_` and this recipe can never disagree.
tools-test-observed:
    python -m pytest tools/tests -q -n auto --dist loadfile --durations=10 -k regression_

# Exercise host, provider and Android OS controllers together; no RF/build/install.
# The three named modules live beside their subject (`tools/android`) because the runner is
# not RN-specific; naming them here is what makes them run at all.
tools-fixture-test:
    python -c 'import unittest; loader = unittest.TestLoader(); suite = unittest.TestSuite([loader.discover("tools/tests", pattern="test_*.py", top_level_dir="."), loader.discover("tests/peripherals/nrf52840", pattern="test_*.py", top_level_dir="."), loader.loadTestsFromNames(["tools.android.test_run_instrumentation", "tools.android.test_state_control"])]); result = unittest.TextTestRunner(verbosity=2, durations=10).run(suite); raise SystemExit(not result.wasSuccessful())'
