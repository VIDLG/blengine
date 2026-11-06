set working-directory := "../.."

integration := "integrations/react-native-internal"
bleng_integration := "integrations/react-native-bleng"
host := "tests/consumers/react-native-internal"
formal_host := "tests/consumers/react-native-bleng"
# Expected export path only: no contract is generated or borrowed from v1.
fault_contract := "tests/peripherals/nrf52840/firmware/contracts/multifunction-rf-v2-a.json"
fault_profile := "tests/peripherals/nrf52840/firmware/profiles/multifunction-rf-v2-a.json"

# The formal consumer is intentionally independent from the transitional internal host.
[working-directory("tests/consumers/react-native-bleng")]
formal-host-install:
    npm ci --ignore-scripts --no-audit --no-fund

[working-directory("tests/consumers/react-native-bleng")]
formal-host-check: formal-host-install
    npm run typecheck
    node -e 'const {execFileSync}=require("node:child_process"); const config=JSON.parse(execFileSync("./node_modules/.bin/react-native",["config","--platform","android"],{encoding:"utf8"})); const dependency=config.dependencies["@blehub/react-native-bleng"]; if (!dependency?.platforms?.android) throw new Error("formal RN Android dependency was not autolinked"); if (config.dependencies["@blehub/react-native-internal"]) throw new Error("transitional internal dependency leaked into formal host"); console.log("formal RN consumer autolinking passed")'

# Debug includes native contract tests; explicit release packages JS without Metro.
formal-host-android-build variant="debug": formal-host-check
    pixi run python tools/react_native/run_quiet.py --no-tty --progress-seconds 20 {{ quote(".work/logs/formal-host-android-" + variant + "-build.log") }} -- pixi run -e android {{ formal_host }}/android/gradlew -p {{ formal_host }}/android {{ if variant == "debug" { ":app:assembleDebug :app:assembleDebugAndroidTest" } else if variant == "release" { ":app:assembleRelease" } else { error("formal-host-android-build variant must be debug or release") } }} --no-daemon

# Formal App tests mock only the package boundary, not the transitional probe API.
[working-directory("tests/consumers/react-native-bleng")]
formal-host-test:
    npm test

# One owner compiles the Nitro-free Swift contract and the formal App/native-test graph.
formal-host-ios-build: stage-apple-bleng bleng-check formal-host-check
    pixi run python tools/react_native/run_quiet.py --no-tty --progress-seconds 20 .work/logs/formal-host-ios-build.log -- pixi run -e apple-ci python -m tools.react_native.bleng_ios_consumer

# Native bridge contracts are distinct from Appium UI E2E. Device execution is opt-in.
formal-host-android-native-test device: formal-host-android-build
    test -n {{ quote(device) }}
    pixi run -e android python -m tools.android.run_instrumentation --device physical --serial {{ quote(device) }} --app-apk {{ formal_host }}/android/app/build/outputs/apk/debug/app-debug.apk --test-apk {{ formal_host }}/android/app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk --test-component com.blehubblenghost.test/androidx.test.runner.AndroidJUnitRunner --test-class com.blehubblenghost.HybridBlengSessionOwnerInstrumentedTest --log {{ formal_host }}/android/build/native-contract-test.log --timeout 120

[working-directory("tests/consumers/react-native-bleng/ios")]
formal-host-ios-native-test simulator: formal-host-ios-build
    test -n {{ quote(simulator) }}
    pixi run python ../../../../tools/react_native/run_quiet.py --no-tty --progress-seconds 20 build/native-test-run.log -- pixi run -e apple-ci xcodebuild -quiet -workspace BleHubBlengHost.xcworkspace -scheme BleHubBlengHost -configuration Debug -destination {{ quote("platform=iOS Simulator,id=" + simulator) }} -derivedDataPath build/simulator-derived CODE_SIGNING_ALLOWED=NO -only-testing:BleHubBlengHostNativeTests -parallel-testing-enabled NO -collect-test-diagnostics never test-without-building

# Opt-in UI-only attach gate. Host preparation owns installation/launch and the
# device lease; this entry never builds, installs, relaunches or mutates radio.
formal-host-ui platform device app_id appium_entry activity="" team="":
    pixi run python -m tools.react_native.run_bleng_ui --platform {{ quote(platform) }} --device {{ quote(device) }} --app-id {{ quote(app_id) }} --appium-entry {{ quote(appium_entry) }} --activity {{ quote(activity) }} --team {{ quote(team) }}

# Explicit managed non-RF run consumes a reviewed prebuilt APK; never builds/grants BLE.
[positional-arguments]
formal-host-android-managed-lifecycle +args:
    pixi run python -m tools.react_native.run_bleng_android_lifecycle "$@"

# Offline integrity replay only; never acquires a device or upgrades the host gate.
formal-host-lifecycle-verify attempt:
    pixi run python -m tools.react_native.bleng_lifecycle_evidence {{ quote(attempt) }}

# Reject missing exports/unmigrated gates before build/install prerequisites.
[private]
fault-inputs-check contract profile mode="":
    python -c 'from pathlib import Path; import sys; from tools.react_native.rf_fixture import preflight_fixture; from tests.peripherals.nrf52840.direct_fixture import require_v2_coordination; preflight_fixture(fixture_contract=Path(sys.argv[1]), fixture_profile=Path(sys.argv[2])); require_v2_coordination(sys.argv[3])' {{ quote(contract) }} {{ quote(profile) }} {{ quote(mode) }}

# Install the pinned Nitro module dependencies. Ordinary checks never install toolchains.
[working-directory("integrations/react-native-internal")]
install:
    npm ci --ignore-scripts --no-audit --no-fund

# Type-check the module and regenerate ignored Swift/Kotlin/C++ Nitro glue.
[working-directory("integrations/react-native-internal")]
check:
    npm run check

# Rebuild the canonical production UniFFI package only when its inputs changed,
# then stage it with the canonical Apple backend sources. This never creates a
# second Rust native image.
[private]
apple-artifacts:
    if pixi run python -m tools.artifact_fingerprint check apple-production; then \
        echo "apple-artifact=fresh"; \
    else \
        pixi run just ffi apple-package; \
    fi

stage-apple-production: apple-artifacts
    test -f .work/staged/apple-production/Sources/BleHubUniFfi/blehub.swift
    test -d .work/staged/apple-production/BleHubUniFfiNative.xcframework
    rm -rf .work/staged/apple-production-react-native {{ integration }}/apple-pods
    pixi run python -m tools.react_native.stage_apple_production
    test -f .work/staged/apple-production-react-native/BleHubUniFfi/Sources/BleHubUniFfi/blehub.swift
    test -f .work/staged/apple-production-react-native/BleHubApple/BleHubApple.podspec
    test -d .work/staged/apple-production-react-native/BleHubApple/Sources/BleHubApple

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


# Standalone Android module build. Android runtime is intentionally not an extraction blocker.
android-build:
    pixi run python tools/react_native/run_quiet.py {{ integration }}/android/build/module-debug.log -- pixi run -e android gradle -p {{ integration }}/android :assembleDebug --no-daemon


# Host instrumentation uses the project runner's adb push + root pm install path.
# Do not replace this with Gradle connected tests: those may invoke adb install and
# surface the OEM package-installer confirmation UI on the rooted physical phone.
android-test device test_class="": host-install host-check host-android-config-check
    test -n "{{ device }}"
    pixi run python tools/react_native/run_quiet.py {{ host }}/android/build/host-instrumentation-build.log -- pixi run -e android {{ host }}/android/gradlew -p {{ host }}/android :app:assembleDebug :app:assembleDebugAndroidTest --no-daemon
    if test -n "{{ test_class }}"; then \
        pixi run -e android python -m tools.android.run_instrumentation --device physical --serial {{ device }} --app-apk {{ host }}/android/app/build/outputs/apk/debug/app-debug.apk --test-apk {{ host }}/android/app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk --test-component com.blehubnitrohost.test/androidx.test.runner.AndroidJUnitRunner --test-class {{ quote(test_class) }} --log {{ host }}/android/build/host-instrumentation.log --timeout 120; \
    else \
        pixi run -e android python -m tools.android.run_instrumentation --device physical --serial {{ device }} --app-apk {{ host }}/android/app/build/outputs/apk/debug/app-debug.apk --test-apk {{ host }}/android/app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk --test-component com.blehubnitrohost.test/androidx.test.runner.AndroidJUnitRunner --log {{ host }}/android/build/host-instrumentation.log --timeout 120; \
    fi

# Rebuild and publish the canonical Android UniFFI/backend AAR artifacts before
# the RN artifact consumer. The RN module depends on both published components.
[private]
android-artifacts:
    if pixi run python -m tools.artifact_fingerprint check android-production; then \
        echo "android-artifact=fresh"; \
    else \
        pixi run just ffi android-backend-publish; \
    fi

# Install downstream mobile-host dependencies from the private local integration.
host-install: install check
    npm --prefix {{ host }} install --ignore-scripts --no-audit --no-fund

host-check:
    {{ host }}/node_modules/.bin/tsc -p {{ host }}/tsconfig.json --noEmit

# Run all maintained Jest suites once via the host's configured discovery.
host-test:
    npm --prefix {{ host }} test

# Hardware-free host tooling regressions; includes bounded local subprocess tests.
# Canonical package discovery prevents duplicate/ambiguous test-module identities.
tools-test:
    python -m unittest discover -s tools/tests/react_native -t . -p 'test_*.py' -q

# Exercise host, provider and Android OS controllers together; no RF/build/install.
tools-fixture-test:
    python -c 'import unittest; loader = unittest.TestLoader(); suite = unittest.TestSuite([loader.discover("tools/tests/react_native", pattern="test_*.py", top_level_dir="."), loader.discover("tests/peripherals/nrf52840", pattern="test_*.py", top_level_dir="."), loader.loadTestsFromNames(["tools.android.test_state_control", "tools.android.test_permission_control"])]); result = unittest.TextTestRunner(verbosity=1).run(suite); raise SystemExit(not result.wasSuccessful())'

# Offline integrity check only; never reauthorizes RF or turns a failed run into a pass.
host-rf-evidence-check manifest:
    pixi run python -m tools.react_native.rf_evidence_bundle {{ quote(manifest) }}


[working-directory("tests/consumers/react-native-internal/ios")]
host-ios-native-lifecycle-test simulator="": host-ios-pods
    test -n "{{ simulator }}"
    pixi run python ../../../../tools/react_native/run_quiet.py build/native-test-build.log -- pixi run -e apple-ci xcodebuild -quiet -workspace BleHubNitroHost.xcworkspace -scheme BleHubNitroHost -configuration Debug -sdk iphonesimulator -destination 'generic/platform=iOS Simulator' -derivedDataPath build/native-test-derived CODE_SIGNING_ALLOWED=NO build-for-testing
    pixi run python ../../../../tools/react_native/run_quiet.py build/native-test-run.log -- pixi run -e apple-ci xcodebuild -quiet -workspace BleHubNitroHost.xcworkspace -scheme BleHubNitroHost -configuration Debug -destination 'platform=iOS Simulator,id={{ simulator }}' -derivedDataPath build/native-test-derived CODE_SIGNING_ALLOWED=NO -only-testing:BleHubNitroHostNativeTests -parallel-testing-enabled NO -collect-test-diagnostics never test-without-building

# Confirm RN CLI Android autolinking and ensure the canonical Android artifact.
host-android-config-check: host-install android-artifacts
    cd {{ host }} && node -e 'const {execFileSync}=require("node:child_process"); const assert=require("node:assert/strict"); const config=JSON.parse(execFileSync("./node_modules/.bin/react-native",["config","--platform","android"],{encoding:"utf8"})); const dependency=config.dependencies["@blehub/react-native-internal"]; assert(dependency?.platforms?.android, "Nitro Android dependency was not autolinked"); assert.match(dependency.platforms.android.packageImportPath,/BleHubReactNativePackage/); assert.match(dependency.platforms.android.packageInstance,/BleHubReactNativePackage/); console.log("RN CLI autolinks @blehub/react-native-internal -> BleHubReactNativePackage")'

host-android-build: host-install host-check host-android-config-check
    pixi run python tools/react_native/run_quiet.py {{ host }}/android/build/host-debug.log -- pixi run -e android {{ host }}/android/gradlew -p {{ host }}/android :app:assembleDebug --no-daemon

host-android-single-image-check: host-android-build
    pixi run python tools/react_native/check_single_image.py android {{ host }}/android/app/build/outputs/apk/debug/app-debug.apk

# Run the maintained Appium Python-client RN page smoke on one real Android device.
# A release-shaped APK is required so the JS bundle is packaged; Appium owns only
# UI interaction while SDK installation and process/BLE/RF evidence remain separate.
appium-android-smoke device appium_entry: host-install host-check host-android-config-check
    test -n "{{ device }}" && test -n "{{ appium_entry }}"
    pixi run python tools/react_native/run_quiet.py {{ host }}/android/build/host-appium-release.log -- pixi run -e android {{ host }}/android/gradlew -p {{ host }}/android :app:assembleRelease --no-daemon
    pixi run python -m tools.android.run_android_appium_rn_smoke --apk {{ host }}/android/app/build/outputs/apk/release/app-release.apk --device {{ quote(device) }} --appium-entry {{ quote(appium_entry) }}

# Build an isolated Android RN consumer from an npm tarball and published fixture Maven artifacts.
host-android-artifact-consumer: install check android-artifacts
    pixi run python -m tools.react_native.android_artifact_consumer

# Optional physical-device lifecycle gate from the clean npm-tarball consumer.
# The default artifact consumer remains static-only and device-free.
host-android-artifact-device-lifecycle device: install check android-artifacts
    test -n "{{ device }}"
    pixi run python -m tools.react_native.android_artifact_consumer --device {{ device }}

host-android-runtime device="": host-install host-check host-android-config-check
    test -n "{{ device }}"
    pixi run python tools/react_native/run_quiet.py {{ host }}/android/build/host-release.log -- pixi run -e android {{ host }}/android/gradlew -p {{ host }}/android :app:assembleRelease --no-daemon
    pixi run -e android python -m tools.android.install_apk --serial {{ device }} --device physical --apk {{ host }}/android/app/build/outputs/apk/release/app-release.apk --log {{ host }}/android/build/apk-install.log
    pixi run python -m tools.react_native.run_android_runtime {{ device }} --mode basic --log {{ host }}/android/build/runtime.log

# Android RN close/recreate, listener removal, late-callback and post-close dispatch gate.
# This is host-only: it does not start a Peripheral or require RF hardware.
host-android-lifecycle-stress device="": host-install host-check host-android-config-check
    test -n "{{ device }}"
    pixi run python tools/react_native/run_quiet.py {{ host }}/android/build/host-lifecycle-release.log -- pixi run -e android {{ host }}/android/gradlew -p {{ host }}/android :app:assembleRelease --no-daemon
    pixi run -e android python -m tools.android.install_apk --serial {{ device }} --device physical --apk {{ host }}/android/app/build/outputs/apk/release/app-release.apk --log {{ host }}/android/build/lifecycle-apk-install.log
    pixi run python -m tools.react_native.run_android_runtime {{ device }} --mode stress --log {{ host }}/android/build/lifecycle-stress.log --timeout 90

# Android RN JS runtime invalidation through ReactDelegate.reload().
# This is distinct from Activity/process restart and uses the host's test-only intent hook.
host-android-runtime-reload device="": host-install host-check host-android-config-check
    test -n "{{ device }}"
    pixi run python tools/react_native/run_quiet.py {{ host }}/android/build/host-reload-release.log -- pixi run -e android {{ host }}/android/gradlew -p {{ host }}/android :app:assembleRelease --no-daemon
    pixi run -e android python -m tools.android.install_apk --serial {{ device }} --device physical --apk {{ host }}/android/app/build/outputs/apk/release/app-release.apk --log {{ host }}/android/build/reload-apk-install.log
    pixi run python -m tools.react_native.run_android_runtime {{ device }} --mode reload --log {{ host }}/android/build/runtime-reload.log --timeout 120

# Android RN JS -> Nitro -> production Android scan gate against the standalone nRF DK.
# The runner owns fixture readiness, bounded progress, failure-fast log handling and cleanup.
host-android-rf device mode="rf" service_uuid="fd600000-0000-4000-8000-000000000000": host-install host-check host-android-config-check
    test -n "{{ device }}"
    pixi run python tools/react_native/run_quiet.py {{ host }}/android/build/host-rf-release.log -- pixi run -e android {{ host }}/android/gradlew -p {{ host }}/android :app:assembleRelease --no-daemon
    pixi run -e android adb -s {{ device }} wait-for-device
    pixi run -e android python -m tools.android.install_apk --serial {{ device }} --device physical --apk {{ host }}/android/app/build/outputs/apk/release/app-release.apk --log {{ host }}/android/build/rf-apk-install.log
    pixi run python -m tools.react_native.run_android_rf {{ device }} --mode {{ mode }} --service-uuid {{ service_uuid }}

# Profile-aware single-DK control used for per-device Android RF diagnosis.
# fixture_device and service_uuid must name the same local DK/profile pair.
host-android-rf-single device fixture_device service_uuid mode="rf": host-install host-check host-android-config-check
    test -n "{{ device }}" && test -n "{{ fixture_device }}" && test -n "{{ service_uuid }}"
    pixi run python tools/react_native/run_quiet.py {{ host }}/android/build/host-rf-single-release.log -- pixi run -e android {{ host }}/android/gradlew -p {{ host }}/android :app:assembleRelease --no-daemon
    pixi run -e android adb -s {{ device }} wait-for-device
    pixi run -e android python -m tools.android.install_apk --serial {{ device }} --device physical --apk {{ host }}/android/app/build/outputs/apk/release/app-release.apk --log {{ host }}/android/build/rf-single-apk-install.log
    pixi run python -m tools.react_native.run_android_rf {{ device }} --mode {{ mode }} --service-uuid {{ service_uuid }} --fixture-device {{ fixture_device }}

# Launch the interactive RF Lab page without starting a fixture or waiting for a gate marker.
# The user selects the single-Peripheral flow and closes the probe from the visible RN UI.
host-android-rf-lab device service_uuid="fd600000-0000-4000-8000-000000000000": host-install host-check host-android-config-check
    test -n "{{ device }}" && test -n "{{ service_uuid }}"
    pixi run python tools/react_native/run_quiet.py {{ host }}/android/build/host-rf-lab-release.log -- pixi run -e android {{ host }}/android/gradlew -p {{ host }}/android :app:assembleRelease --no-daemon
    pixi run -e android adb -s {{ device }} wait-for-device
    pixi run -e android python -m tools.android.install_apk --serial {{ device }} --device physical --apk {{ host }}/android/app/build/outputs/apk/release/app-release.apk --log {{ host }}/android/build/rf-lab-apk-install.log
    pixi run -e android adb -s {{ device }} shell am force-stop com.blehubnitrohost
    pixi run -e android adb -s {{ device }} shell am start -n com.blehubnitrohost/.MainActivity --es blehub.runtimeMode rf-lab --es blehub.rfServiceUuid {{ service_uuid }}

# Full single-launch RF Lab: continuous10 (including cancellation/watchdog), then RecoveryTen.
# Build is host-only. The runner owns one live fixture session before snapshot install.
host-android-rf-lab-suite device fixture_contract=fault_contract fixture_profile=fault_profile: (fault-inputs-check fixture_contract fixture_profile "rf-lab-full-attested") host-install host-check host-android-config-check
    test -n {{ quote(device) }} && test -n {{ quote(fixture_contract) }} && test -n {{ quote(fixture_profile) }}
    pixi run python tools/react_native/run_quiet.py {{ host }}/android/build/host-rf-lab-suite-release.log -- pixi run -e android {{ host }}/android/gradlew -p {{ host }}/android :app:assembleRelease --no-daemon
    pixi run python -m tools.react_native.run_rf_lab_full --platform android --device {{ quote(device) }} --artifact {{ host }}/android/app/build/outputs/apk/release/app-release.apk --fixture-contract {{ quote(fixture_contract) }} --fixture-profile {{ quote(fixture_profile) }}

# Ten fresh Probe/Hub lifecycles in one App/runtime, with live RF and native close counts.
# Freeze/install the artifact only after live image proof; no implicit firmware flash.
host-android-rf-recovery-ten device fixture_contract=fault_contract fixture_profile=fault_profile: (fault-inputs-check fixture_contract fixture_profile "") host-install host-check host-android-config-check
    test -n {{ quote(device) }} && test -n {{ quote(fixture_contract) }} && test -n {{ quote(fixture_profile) }}
    pixi run python tools/react_native/run_quiet.py {{ host }}/android/build/host-recovery-ten-release.log -- pixi run -e android {{ host }}/android/gradlew -p {{ host }}/android :app:assembleRelease --no-daemon
    pixi run python -m tools.react_native.run_rf_recovery_ten --platform android --device {{ quote(device) }} --artifact {{ host }}/android/app/build/outputs/apk/release/app-release.apk --fixture-contract {{ quote(fixture_contract) }} --fixture-profile {{ quote(fixture_profile) }}

# Opt-in response-write REJECT in the same workbench; not part of full-suite.
# The runner freezes the artifact and proves the live image; no provisioning/reset.
host-android-rf-write-reject device fixture_contract fixture_profile port: (fault-inputs-check fixture_contract fixture_profile "write-reject-attested") host-install host-check host-android-config-check
    test -n {{ quote(device) }} && test -n {{ quote(fixture_contract) }} && test -n {{ quote(fixture_profile) }} && test -n {{ quote(port) }}
    pixi run python tools/react_native/run_quiet.py {{ host }}/android/build/host-write-reject-release.log -- pixi run -e android {{ host }}/android/gradlew -p {{ host }}/android :app:assembleRelease --no-daemon
    pixi run python -m tools.react_native.run_rf_write_reject --platform android --device {{ quote(device) }} --artifact {{ host }}/android/app/build/outputs/apk/release/app-release.apk --fixture-contract {{ quote(fixture_contract) }} --fixture-profile {{ quote(fixture_profile) }} --port {{ quote(port) }}

# Opt-in CCC enable REJECT; recovery and silence in the existing workbench.
host-android-rf-ccc-enable-reject device fixture_contract fixture_profile port: (fault-inputs-check fixture_contract fixture_profile "ccc-enable-reject-attested") host-install host-check host-android-config-check
    test -n {{ quote(device) }} && test -n {{ quote(fixture_contract) }} && test -n {{ quote(fixture_profile) }} && test -n {{ quote(port) }}
    pixi run python tools/react_native/run_quiet.py {{ host }}/android/build/host-ccc-enable-reject-release.log -- pixi run -e android {{ host }}/android/gradlew -p {{ host }}/android :app:assembleRelease --no-daemon
    pixi run python -m tools.react_native.run_rf_ccc_enable_reject --platform android --device {{ quote(device) }} --artifact {{ host }}/android/app/build/outputs/apk/release/app-release.apk --fixture-contract {{ quote(fixture_contract) }} --fixture-profile {{ quote(fixture_profile) }} --port {{ quote(port) }}

# Opt-in CCC disable REJECT; keeps the original subscription through rejection.
host-android-rf-ccc-disable-reject device fixture_contract fixture_profile port: (fault-inputs-check fixture_contract fixture_profile "ccc-disable-reject-attested") host-install host-check host-android-config-check
    test -n {{ quote(device) }} && test -n {{ quote(fixture_contract) }} && test -n {{ quote(fixture_profile) }} && test -n {{ quote(port) }}
    pixi run python tools/react_native/run_quiet.py {{ host }}/android/build/host-ccc-disable-reject-release.log -- pixi run -e android {{ host }}/android/gradlew -p {{ host }}/android :app:assembleRelease --no-daemon
    pixi run python -m tools.react_native.run_rf_ccc_disable_reject --platform android --device {{ quote(device) }} --artifact {{ host }}/android/app/build/outputs/apk/release/app-release.apk --fixture-contract {{ quote(fixture_contract) }} --fixture-profile {{ quote(fixture_profile) }} --port {{ quote(port) }}

# Same Android App/PID, two RN engines: native teardown of a physically pending read.
# Uses the multifunction image and unified bundle; ordinary execution never flashes.
host-android-pending-read-reload device fixture_contract=fault_contract fixture_profile=fault_profile: (fault-inputs-check fixture_contract fixture_profile "") host-install host-check host-android-config-check
    test -n {{ quote(device) }} && test -n {{ quote(fixture_contract) }} && test -n {{ quote(fixture_profile) }}
    pixi run python tools/react_native/run_quiet.py {{ host }}/android/build/host-pending-reload-release.log -- pixi run -e android {{ host }}/android/gradlew -p {{ host }}/android :app:assembleRelease --no-daemon
    pixi run python -m tools.react_native.run_android_pending_read_reload {{ quote(device) }} --fixture-contract {{ quote(fixture_contract) }} --fixture-profile {{ quote(fixture_profile) }} --apk {{ host }}/android/app/build/outputs/apk/release/app-release.apk

# Opt-in phone radio gate: startup-off (default) or held-read active-loss.
# Neither mode is implicitly included in the non-destructive full suite.
# The runner freezes the APK, proves the live fixture, and restores/final-reads ON.
host-android-rf-adapter-transition device fixture_contract=fault_contract fixture_profile=fault_profile mode="startup-off": (fault-inputs-check fixture_contract fixture_profile "adapter-transition-attested") host-install host-check host-android-config-check
    test -n {{ quote(device) }} && test -n {{ quote(fixture_contract) }} && test -n {{ quote(fixture_profile) }}
    pixi run python tools/react_native/run_quiet.py {{ host }}/android/build/host-adapter-transition-release.log -- pixi run -e android {{ host }}/android/gradlew -p {{ host }}/android :app:assembleRelease --no-daemon
    pixi run python -m tools.react_native.run_android_adapter_transition --device {{ quote(device) }} --apk {{ host }}/android/app/build/outputs/apk/release/app-release.apk --fixture-contract {{ quote(fixture_contract) }} --fixture-profile {{ quote(fixture_profile) }} --mode {{ quote(mode) }}

# Opt-in permission gate: denied startup, active-read process death, fresh-App restore.
# Permission changes/OS proof are host-owned; this is not implicit in full-suite.
host-android-rf-permission-transition device fixture_contract=fault_contract fixture_profile=fault_profile: (fault-inputs-check fixture_contract fixture_profile "permission-transition") host-install host-check host-android-config-check
    test -n {{ quote(device) }} && test -n {{ quote(fixture_contract) }} && test -n {{ quote(fixture_profile) }}
    pixi run python tools/react_native/run_quiet.py {{ host }}/android/build/host-permission-transition-release.log -- pixi run -e android {{ host }}/android/gradlew -p {{ host }}/android :app:assembleRelease --no-daemon
    pixi run python -m tools.react_native.run_android_permission_transition --device {{ quote(device) }} --apk {{ host }}/android/app/build/outputs/apk/release/app-release.apk --fixture-contract {{ quote(fixture_contract) }} --fixture-profile {{ quote(fixture_profile) }}

# Standalone attested watchdog diagnostic; continuous10 already includes this gate.
# Installation and RF start occur only after the runner proves the live DK identity.
host-android-rf-watchdog device fixture_contract=fault_contract fixture_profile=fault_profile port="": (fault-inputs-check fixture_contract fixture_profile "") host-install host-check host-android-config-check
    pixi run python tools/react_native/run_quiet.py {{ host }}/android/build/host-rf-watchdog-release.log -- pixi run -e android {{ host }}/android/gradlew -p {{ host }}/android :app:assembleRelease --no-daemon
    if test -n {{ quote(port) }}; then \
        pixi run python -m tools.react_native.run_rf_watchdog --platform android --device {{ quote(device) }} --fixture-contract {{ quote(fixture_contract) }} --fixture-profile {{ quote(fixture_profile) }} --port {{ quote(port) }} --apk {{ host }}/android/app/build/outputs/apk/release/app-release.apk --replace-app; \
    else \
        pixi run python -m tools.react_native.run_rf_watchdog --platform android --device {{ quote(device) }} --fixture-contract {{ quote(fixture_contract) }} --fixture-profile {{ quote(fixture_profile) }} --apk {{ host }}/android/app/build/outputs/apk/release/app-release.apk --replace-app; \
    fi

# RN JS-driven cancellation gate after the aggregate GATT baseline.
host-android-rf-cancellation device fixture_contract=fault_contract fixture_profile=fault_profile: (fault-inputs-check fixture_contract fixture_profile "") host-install host-check host-android-config-check
    pixi run python tools/react_native/run_quiet.py {{ host }}/android/build/host-rf-cancellation-release.log -- pixi run -e android {{ host }}/android/gradlew -p {{ host }}/android :app:assembleRelease --no-daemon
    pixi run python -m tools.react_native.run_rf_cancellation --platform android --device {{ quote(device) }} --fixture-contract {{ quote(fixture_contract) }} --fixture-profile {{ quote(fixture_profile) }} --apk {{ host }}/android/app/build/outputs/apk/release/app-release.apk --replace-app

# Private two-DK gate. The runner derives both service UUIDs from the explicit
# local device/profile mapping and fails before the RN host if either DK is not ready.
host-android-rf-multi device notification_mode="both" subscription_mode="notification" cycles="1": host-install host-check host-android-config-check
    test -n "{{ device }}"
    pixi run python tools/react_native/run_quiet.py {{ host }}/android/build/host-rf-multi-release.log -- pixi run -e android {{ host }}/android/gradlew -p {{ host }}/android :app:assembleRelease --no-daemon
    pixi run -e android adb -s {{ device }} wait-for-device
    pixi run -e android python -m tools.android.install_apk --serial {{ device }} --device physical --apk {{ host }}/android/app/build/outputs/apk/release/app-release.apk --log {{ host }}/android/build/rf-multi-apk-install.log
    pixi run python -m tools.react_native.run_android_multi_rf {{ device }} --notification-mode {{ notification_mode }} --subscription-mode {{ subscription_mode }} --cycles {{ cycles }}

# One RN process/probe continuous multi-DK pressure gate. Unlike --cycles above,
# this keeps the fixture and probe alive across all inner ledgers.
host-android-rf-multi-soak device subscription_mode="notification" cycles="10": host-install host-check host-android-config-check
    test -n "{{ device }}"
    pixi run python tools/react_native/run_quiet.py {{ host }}/android/build/host-rf-multi-soak-release.log -- pixi run -e android {{ host }}/android/gradlew -p {{ host }}/android :app:assembleRelease --no-daemon
    pixi run -e android adb -s {{ device }} wait-for-device
    pixi run -e android python -m tools.android.install_apk --serial {{ device }} --device physical --apk {{ host }}/android/app/build/outputs/apk/release/app-release.apk --log {{ host }}/android/build/rf-multi-soak-apk-install.log
    pixi run python -m tools.react_native.run_android_multi_rf {{ device }} --runtime-mode rf-multi-soak --subscription-mode {{ subscription_mode }} --soak-cycles {{ cycles }}

# Destructive private two-DK fault gate: stop only DK-A, require DK-B survival,
# then restart DK-A and verify a fresh connection/GATT route.
host-android-rf-multi-fault device: host-install host-check host-android-config-check
    test -n "{{ device }}"
    pixi run python tools/react_native/run_quiet.py {{ host }}/android/build/host-rf-multi-fault-release.log -- pixi run -e android {{ host }}/android/gradlew -p {{ host }}/android :app:assembleRelease --no-daemon
    pixi run -e android adb -s {{ device }} wait-for-device
    pixi run -e android python -m tools.android.install_apk --serial {{ device }} --device physical --apk {{ host }}/android/app/build/outputs/apk/release/app-release.apk --log {{ host }}/android/build/rf-multi-fault-apk-install.log
    pixi run python -m tools.react_native.run_android_multi_fault_rf {{ device }}

host-android-bundle: host-install host-check
    cd {{ host }} && ./node_modules/.bin/react-native bundle --platform android --dev false --entry-file index.js --bundle-output /tmp/blehub-nitro-host.android.bundle --assets-dest /tmp/blehub-nitro-host-assets

[working-directory("tests/consumers/react-native-internal/ios")]
host-ios-pods: stage-apple-bleng host-install host-check
    pixi run python ../../../../tools/react_native/run_quiet.py build/pod-install.log -- pixi run -e apple-ci pod install

[working-directory("tests/consumers/react-native-internal/ios")]
host-ios-build: host-ios-pods
    pixi run python ../../../../tools/react_native/run_quiet.py build/simulator-build.log -- pixi run -e apple-ci xcodebuild -quiet -workspace BleHubNitroHost.xcworkspace -scheme BleHubNitroHost -configuration Debug -sdk iphonesimulator -destination 'generic/platform=iOS Simulator' -derivedDataPath build/simulator-derived CODE_SIGNING_ALLOWED=NO build

host-ios-single-image-check: host-ios-build
    pixi run -e apple-ci python tools/react_native/check_single_image.py ios {{ host }}/ios/build/simulator-derived/Build/Products/Debug-iphonesimulator/BleHubNitroHost.app/BleHubNitroHost.debug.dylib

# Build an isolated, unsigned iOS RN consumer from an npm tarball, never the module source.
host-ios-artifact-consumer: install check stage-apple-production
    pixi run python -m tools.react_native.ios_artifact_consumer

# Signed clean-artifact iPhone runtime smoke; no RF or Peripheral is involved.
host-ios-artifact-device-runtime udid team: install check stage-apple-production
    pixi run python -m tools.react_native.ios_artifact_consumer --device-udid {{ quote(udid) }} --development-team {{ quote(team) }}

# Signed clean-artifact iPhone XCUITest launched directly after artifact install.
# This remains a bridge/UI lifecycle gate, not an RF test.
host-ios-artifact-ui-test udid team: install check stage-apple-production
    pixi run python -m tools.react_native.ios_artifact_consumer --device-udid {{ quote(udid) }} --development-team {{ quote(team) }} --ui-test

[working-directory("tests/consumers/react-native-internal/ios")]
host-ios-runtime udid="": host-ios-pods
    test -n "{{ udid }}"
    xcrun simctl boot {{ udid }} >/dev/null 2>&1 || true
    xcrun simctl bootstatus {{ udid }} -b
    pixi run python ../../../../tools/react_native/run_quiet.py build/simulator-runtime-build.log -- pixi run -e apple-ci xcodebuild -quiet -workspace BleHubNitroHost.xcworkspace -scheme BleHubNitroHost -configuration Release -sdk iphonesimulator -destination 'platform=iOS Simulator,id={{ udid }}' -derivedDataPath build/runtime-derived CODE_SIGNING_ALLOWED=NO build
    xcrun simctl install {{ udid }} build/runtime-derived/Build/Products/Release-iphonesimulator/BleHubNitroHost.app
    xcrun simctl launch {{ udid }} org.reactjs.native.example.BleHubNitroHost --blehub-nitro-basic
    sleep 5
    xcrun simctl spawn {{ udid }} log show --last 30s --info --debug --style compact --predicate 'eventMessage CONTAINS "BLEHUB_NITRO_RUNTIME"' > build/runtime.log
    pixi run python ../../../../tools/react_native/check_runtime_log.py build/runtime.log

[working-directory("tests/consumers/react-native-internal/ios")]
host-ios-device-build udid="" team="": host-ios-pods
    test -n "{{ udid }}"
    test -n "{{ team }}"
    pixi run python ../../../../tools/react_native/run_quiet.py --no-tty build/device-build.log -- pixi run -e apple-ci xcodebuild -quiet -workspace BleHubNitroHost.xcworkspace -scheme BleHubNitroHost -configuration Debug -sdk iphoneos -destination 'id={{ udid }}' -derivedDataPath build/device-derived DEVELOPMENT_TEAM={{ team }} PRODUCT_BUNDLE_IDENTIFIER=org.reactjs.native.example.BleHubNitroHost CODE_SIGN_STYLE=Automatic CODE_SIGN_IDENTITY='Apple Development' ONLY_ACTIVE_ARCH=YES build
    test -d build/device-derived/Build/Products/Debug-iphoneos/BleHubNitroHost.app

host-ios-device-single-image-check udid="" team="":
    pixi run just react-native host-ios-device-build {{ udid }} {{ team }}
    pixi run python tools/react_native/check_single_image.py ios {{ host }}/ios/build/device-derived/Build/Products/Debug-iphoneos/BleHubNitroHost.app/BleHubNitroHost.debug.dylib

host-ios-device-runtime udid="" team="":
    pixi run just react-native host-ios-device-build {{ udid }} {{ team }}
    pixi run -e apple-ci xcrun devicectl device install app --device {{ udid }} --json-output {{ host }}/ios/build/device-install.json {{ host }}/ios/build/device-derived/Build/Products/Debug-iphoneos/BleHubNitroHost.app
    pixi run python -m tools.react_native.run_ios_device_runtime {{ udid }} {{ host }}/ios/build/device-runtime.log --seconds 20 --keep-app
    pixi run python tools/react_native/check_runtime_log.py {{ host }}/ios/build/device-runtime.log

host-ios-device-lifecycle-stress udid="" team="":
    pixi run just react-native host-ios-device-build {{ udid }} {{ team }}
    pixi run -e apple-ci xcrun devicectl device install app --device {{ udid }} --json-output {{ host }}/ios/build/device-stress-install.json {{ host }}/ios/build/device-derived/Build/Products/Debug-iphoneos/BleHubNitroHost.app
    pixi run python tools/react_native/run_ios_device_runtime.py {{ udid }} {{ host }}/ios/build/device-lifecycle-stress.log --seconds 35 --stress --keep-app
    pixi run python tools/react_native/check_runtime_log.py {{ host }}/ios/build/device-lifecycle-stress.log --stress

host-ios-device-reload udid="" team="":
    pixi run just react-native host-ios-device-build {{ udid }} {{ team }}
    pixi run -e apple-ci xcrun devicectl device install app --device {{ udid }} --json-output {{ host }}/ios/build/device-reload-install.json {{ host }}/ios/build/device-derived/Build/Products/Debug-iphoneos/BleHubNitroHost.app
    pixi run python tools/react_native/run_ios_device_runtime.py {{ udid }} {{ host }}/ios/build/device-reload.log --seconds 40 --reload --keep-app
    pixi run python tools/react_native/check_runtime_log.py {{ host }}/ios/build/device-reload.log --reload

# iPhone RN JS -> Nitro -> production Apple scan gate against the standalone nRF.
# RN gates share an atomic lock; other Apple native gates do not use it.
host-ios-device-rf-scan udid="" team="":
    test -n "{{ udid }}" && test -n "{{ team }}"
    pixi run just react-native host-ios-device-build {{ udid }} {{ team }}
    pixi run python -m tools.react_native.run_ios_rf_scan {{ quote(udid) }}

# Launch the interactive RF Lab page without starting a fixture or waiting for a gate marker.
# The user selects the single-Peripheral flow and closes the probe from the visible RN UI.
[working-directory("tests/consumers/react-native-internal/ios")]
host-ios-device-rf-lab udid="" team="" service_uuid="fd600000-0000-4000-8000-000000000000":
    test -n "{{ udid }}" && test -n "{{ team }}" && test -n "{{ service_uuid }}"
    pixi run just react-native host-ios-device-build {{ udid }} {{ team }}
    pixi run -e apple-ci xcrun devicectl device install app --device {{ udid }} --json-output {{ host }}/ios/build/device-rf-lab-install.json {{ host }}/ios/build/device-derived/Build/Products/Debug-iphoneos/BleHubNitroHost.app
    pixi run -e apple-ci xcrun devicectl device process launch --device {{ udid }} --terminate-existing --activate org.reactjs.native.example.BleHubNitroHost --blehub-nitro-rf-lab-service {{ service_uuid }}

# Full single-launch RF Lab: continuous10, physical-clean handoff, then RecoveryTen.
# One App/runtime/fixture session; retain the result page after all gates and cleanup.
host-ios-device-rf-lab-suite udid team fixture_contract=fault_contract fixture_profile=fault_profile: (fault-inputs-check fixture_contract fixture_profile "rf-lab-full-attested")
    test -n {{ quote(udid) }} && test -n {{ quote(team) }} && test -n {{ quote(fixture_contract) }} && test -n {{ quote(fixture_profile) }}
    pixi run just react-native host-ios-device-build {{ quote(udid) }} {{ quote(team) }}
    pixi run python -m tools.react_native.run_rf_lab_full --platform ios --device {{ quote(udid) }} --artifact {{ host }}/ios/build/device-derived/Build/Products/Debug-iphoneos/BleHubNitroHost.app --fixture-contract {{ quote(fixture_contract) }} --fixture-profile {{ quote(fixture_profile) }}

# Same iOS App/PID, two RN engines; actual module invalidation closes the pending owner.
# Live multifunction image proof and cleanup barrier are required; ordinary runs never flash.
host-ios-device-pending-read-reload udid team fixture_contract=fault_contract fixture_profile=fault_profile: (fault-inputs-check fixture_contract fixture_profile "")
    test -n {{ quote(udid) }} && test -n {{ quote(team) }} && test -n {{ quote(fixture_contract) }} && test -n {{ quote(fixture_profile) }}
    pixi run just react-native host-ios-device-build {{ quote(udid) }} {{ quote(team) }}
    pixi run python -m tools.react_native.run_ios_pending_read_reload_gate {{ quote(udid) }} --app {{ host }}/ios/build/device-derived/Build/Products/Debug-iphoneos/BleHubNitroHost.app --fixture-contract {{ quote(fixture_contract) }} --fixture-profile {{ quote(fixture_profile) }}

# Standalone attested watchdog diagnostic; retains the visible result on success.
host-ios-device-rf-watchdog udid team fixture_contract=fault_contract fixture_profile=fault_profile port="": (fault-inputs-check fixture_contract fixture_profile "ios-standalone-watchdog")
    pixi run just react-native host-ios-device-build {{ quote(udid) }} {{ quote(team) }}
    if test -n {{ quote(port) }}; then \
        pixi run python -m tools.react_native.run_rf_watchdog --platform ios --device {{ quote(udid) }} --fixture-contract {{ quote(fixture_contract) }} --fixture-profile {{ quote(fixture_profile) }} --port {{ quote(port) }}; \
    else \
        pixi run python -m tools.react_native.run_rf_watchdog --platform ios --device {{ quote(udid) }} --fixture-contract {{ quote(fixture_contract) }} --fixture-profile {{ quote(fixture_profile) }}; \
    fi

# Separate RN JS-driven nRF connect/disconnect gate; scan-only pass is not connection evidence.
host-ios-device-rf-connect udid="" team="":
    test -n "{{ udid }}" && test -n "{{ team }}"
    pixi run just react-native host-ios-device-build {{ udid }} {{ team }}
    pixi run python -m tools.react_native.run_ios_rf_scan {{ quote(udid) }} --connect

# RN JS-driven aggregate GATT discovery gate against the connected nRF profile.
host-ios-device-rf-gatt udid="" team="":
    test -n "{{ udid }}" && test -n "{{ team }}"
    pixi run just react-native host-ios-device-build {{ udid }} {{ team }}
    pixi run python -m tools.react_native.run_ios_rf_scan {{ quote(udid) }} --gatt

# RN JS-driven cancellation gate after the aggregate GATT baseline.
host-ios-device-rf-cancellation udid team fixture_contract=fault_contract fixture_profile=fault_profile: (fault-inputs-check fixture_contract fixture_profile "")
    pixi run just react-native host-ios-device-build {{ quote(udid) }} {{ quote(team) }}
    pixi run python -m tools.react_native.run_rf_cancellation --platform ios --device {{ quote(udid) }} --fixture-contract {{ quote(fixture_contract) }} --fixture-profile {{ quote(fixture_profile) }}

# RN JS-driven canonical read/write gate after aggregate GATT discovery.
host-ios-device-rf-io udid="" team="":
    test -n "{{ udid }}" && test -n "{{ team }}"
    pixi run just react-native host-ios-device-build {{ udid }} {{ team }}
    pixi run python -m tools.react_native.run_ios_rf_scan {{ quote(udid) }} --io

# Connected RSSI read through the production Apple backend; no synthetic fixture value.
host-ios-device-rf-rssi udid="" team="":
    test -n "{{ udid }}" && test -n "{{ team }}"
    pixi run just react-native host-ios-device-build {{ udid }} {{ team }}
    pixi run python -m tools.react_native.run_ios_rf_scan {{ quote(udid) }} --rssi

# RN JS-driven PlatformDefault Notification enable/value/disable gate.
host-ios-device-rf-notification udid="" team="":
    test -n "{{ udid }}" && test -n "{{ team }}"
    pixi run just react-native host-ios-device-build {{ udid }} {{ team }}
    pixi run python -m tools.react_native.run_ios_rf_scan {{ quote(udid) }} --notification

# RN JS-driven strict Indication gate: dual-property rejection plus pure-Indication value flow.
host-ios-device-rf-indication udid="" team="":
    test -n "{{ udid }}" && test -n "{{ team }}"
    pixi run just react-native host-ios-device-build {{ udid }} {{ team }}
    pixi run python -m tools.react_native.run_ios_rf_scan {{ quote(udid) }} --indication

# iPhone RN JS -> Nitro -> production Apple multi-DK gate. The runner shares
# the explicit two-DK controller and checker with the Android lane.
host-ios-device-rf-multi udid="" team="" subscription_mode="notification" cycles="1":
    test -n "{{ udid }}" && test -n "{{ team }}"
    pixi run just react-native host-ios-device-build {{ udid }} {{ team }}
    pixi run python -m tools.react_native.run_ios_multi_rf {{ quote(udid) }} --subscription-mode {{ subscription_mode }} --cycles {{ cycles }}

# One RN process/probe continuous multi-DK pressure gate. Unlike --cycles above,
# this keeps the fixture and probe alive across all inner ledgers.
host-ios-device-rf-multi-soak udid="" team="" subscription_mode="notification" cycles="10":
    test -n "{{ udid }}" && test -n "{{ team }}"
    pixi run just react-native host-ios-device-build {{ udid }} {{ team }}
    pixi run python -m tools.react_native.run_ios_multi_rf {{ quote(udid) }} --runtime-mode rf-multi-soak --subscription-mode {{ subscription_mode }} --soak-cycles {{ cycles }}

# Destructive private two-DK fault gate: stop only DK-A, require DK-B survival,
# then restart DK-A and verify a fresh connection/GATT route.
host-ios-device-rf-multi-fault udid="" team="":
    test -n "{{ udid }}" && test -n "{{ team }}"
    pixi run just react-native host-ios-device-build {{ udid }} {{ team }}
    pixi run python -m tools.react_native.run_ios_multi_fault_rf {{ quote(udid) }}

# Opt-in non-destructive RF matrix: build once, then retain each independent
# checker/fixture lifecycle and its own log; stop immediately on first failure.
host-ios-device-rf-baseline udid="" team="":
    test -n "{{ udid }}" && test -n "{{ team }}"
    pixi run just react-native host-ios-device-build {{ udid }} {{ team }}
    pixi run python tools/react_native/check_single_image.py ios {{ host }}/ios/build/device-derived/Build/Products/Debug-iphoneos/BleHubNitroHost.app/BleHubNitroHost.debug.dylib
    pixi run python -m tools.react_native.run_ios_rf_scan {{ quote(udid) }}
    pixi run python -m tools.react_native.run_ios_rf_scan {{ quote(udid) }} --connect
    pixi run python -m tools.react_native.run_ios_rf_scan {{ quote(udid) }} --gatt
    pixi run python -m tools.react_native.run_ios_rf_scan {{ quote(udid) }} --io
    pixi run python -m tools.react_native.run_ios_rf_scan {{ quote(udid) }} --rssi
    pixi run python -m tools.react_native.run_ios_rf_scan {{ quote(udid) }} --notification
    pixi run python -m tools.react_native.run_ios_rf_scan {{ quote(udid) }} --indication

# RN JS-driven passive-disconnect/recovery gate with an active notification route.
host-ios-device-rf-passive-disconnect udid="" team="":
    test -n "{{ udid }}" && test -n "{{ team }}"
    pixi run just react-native host-ios-device-build {{ udid }} {{ team }}
    pixi run python -m tools.react_native.run_ios_rf_scan {{ quote(udid) }} --passive-disconnect

# Real engine reload with an active RF subscription; the fixture observes host disconnect.
host-ios-device-rf-reload udid="" team="":
    test -n "{{ udid }}" && test -n "{{ team }}"
    pixi run just react-native host-ios-device-build {{ udid }} {{ team }}
    pixi run python -m tools.react_native.run_ios_rf_scan {{ quote(udid) }} --rf-reload

# Ten fresh Probe/Hub lifecycles in one App/runtime; replaces the historical delay-based gate.
host-ios-device-rf-recovery-ten udid team fixture_contract=fault_contract fixture_profile=fault_profile: (fault-inputs-check fixture_contract fixture_profile "")
    test -n {{ quote(udid) }} && test -n {{ quote(team) }} && test -n {{ quote(fixture_contract) }} && test -n {{ quote(fixture_profile) }}
    pixi run just react-native host-ios-device-build {{ quote(udid) }} {{ quote(team) }}
    pixi run python -m tools.react_native.run_rf_recovery_ten --platform ios --device {{ quote(udid) }} --artifact {{ host }}/ios/build/device-derived/Build/Products/Debug-iphoneos/BleHubNitroHost.app --fixture-contract {{ quote(fixture_contract) }} --fixture-profile {{ quote(fixture_profile) }}

# Opt-in response-write REJECT in the same workbench; not part of full-suite.
# The runner freezes the artifact and proves the live image; no provisioning/reset.
host-ios-device-rf-write-reject udid team fixture_contract fixture_profile port: (fault-inputs-check fixture_contract fixture_profile "write-reject-attested")
    test -n {{ quote(udid) }} && test -n {{ quote(team) }} && test -n {{ quote(fixture_contract) }} && test -n {{ quote(fixture_profile) }} && test -n {{ quote(port) }}
    pixi run just react-native host-ios-device-build {{ quote(udid) }} {{ quote(team) }}
    pixi run python -m tools.react_native.run_rf_write_reject --platform ios --device {{ quote(udid) }} --artifact {{ host }}/ios/build/device-derived/Build/Products/Debug-iphoneos/BleHubNitroHost.app --fixture-contract {{ quote(fixture_contract) }} --fixture-profile {{ quote(fixture_profile) }} --port {{ quote(port) }}

# Opt-in CCC enable REJECT; no firmware change or full-suite membership.
host-ios-device-rf-ccc-enable-reject udid team fixture_contract fixture_profile port: (fault-inputs-check fixture_contract fixture_profile "ccc-enable-reject-attested")
    test -n {{ quote(udid) }} && test -n {{ quote(team) }} && test -n {{ quote(fixture_contract) }} && test -n {{ quote(fixture_profile) }} && test -n {{ quote(port) }}
    pixi run just react-native host-ios-device-build {{ quote(udid) }} {{ quote(team) }}
    pixi run python -m tools.react_native.run_rf_ccc_enable_reject --platform ios --device {{ quote(udid) }} --artifact {{ host }}/ios/build/device-derived/Build/Products/Debug-iphoneos/BleHubNitroHost.app --fixture-contract {{ quote(fixture_contract) }} --fixture-profile {{ quote(fixture_profile) }} --port {{ quote(port) }}

# Opt-in CCC disable REJECT; enabled readback requires the corrected getter candidate.
host-ios-device-rf-ccc-disable-reject udid team fixture_contract fixture_profile port: (fault-inputs-check fixture_contract fixture_profile "ccc-disable-reject-attested")
    test -n {{ quote(udid) }} && test -n {{ quote(team) }} && test -n {{ quote(fixture_contract) }} && test -n {{ quote(fixture_profile) }} && test -n {{ quote(port) }}
    pixi run just react-native host-ios-device-build {{ quote(udid) }} {{ quote(team) }}
    pixi run python -m tools.react_native.run_rf_ccc_disable_reject --platform ios --device {{ quote(udid) }} --artifact {{ host }}/ios/build/device-derived/Build/Products/Debug-iphoneos/BleHubNitroHost.app --fixture-contract {{ quote(fixture_contract) }} --fixture-profile {{ quote(fixture_profile) }} --port {{ quote(port) }}

# One RN probe/listener and production Apple backend across three passive fault/recovery cycles.
host-ios-device-rf-recovery udid="" team="":
    test -n "{{ udid }}" && test -n "{{ team }}"
    pixi run just react-native host-ios-device-build {{ udid }} {{ team }}
    pixi run python -m tools.react_native.run_ios_rf_scan {{ quote(udid) }} --recovery

# App-specific Bluetooth authorization only: no DK, RF, radio toggle or private API.
# An explicit fresh xcresult path preserves each attempt and rejects skipped execution.
[working-directory("tests/consumers/react-native-internal/ios")]
host-ios-device-permission-test udid team result restore_only="0": host-ios-pods
    test -n {{ quote(udid) }} && test -n {{ quote(team) }} && test -n {{ quote(result) }} && test ! -e {{ quote(result) }} && test ! -e {{ quote(result) }}.log && test ! -e {{ quote(result) }}.summary.json && test {{ quote(restore_only) }} = 0 -o {{ quote(restore_only) }} = 1
    pixi run python -c 'import sys; from pathlib import Path; root=Path("../../../..").resolve(); sys.path.insert(0,str(root)); from tools.react_native.ios_device import require_ready_unlocked_device; require_ready_unlocked_device(sys.argv[1], cwd=root)' {{ quote(udid) }}
    TEST_RUNNER_BLEHUB_IOS_PERMISSION_GATE=1 TEST_RUNNER_BLEHUB_IOS_PERMISSION_RESTORE_ONLY={{ quote(restore_only) }} pixi run python ../../../../tools/react_native/run_quiet.py --no-tty {{ quote(result) }}.log -- pixi run -e apple-ci xcodebuild -quiet -workspace BleHubNitroHost.xcworkspace -scheme BleHubNitroHostUIOnly -configuration Debug -destination 'id={{ udid }}' -derivedDataPath build/device-ui-derived -resultBundlePath {{ quote(result) }} DEVELOPMENT_TEAM={{ team }} CODE_SIGN_STYLE=Automatic CODE_SIGN_IDENTITY='Apple Development' ONLY_ACTIVE_ARCH=YES -parallel-testing-enabled NO -collect-test-diagnostics never -only-testing:BleHubNitroHostUITests/BleHubNitroHostUITests/testBluetoothAuthorizationDenyAndRestore test
    xcrun xcresulttool get test-results summary --path {{ quote(result) }} --compact > {{ quote(result) }}.summary.json
    pixi run python -c 'import json,sys; value=json.load(open(sys.argv[1])); assert value["passedTests"] == value["totalTestCount"] == 1 and value["failedTests"] == value["skippedTests"] == 0, value; print("ios-permission-authorization=" + ("restoration-only" if sys.argv[2] == "1" else "passed") + " scope=non-rf app=kept-open")' {{ quote(result) }}.summary.json {{ quote(restore_only) }}

# Non-RF Settings control feasibility, distinct from the candidate RF gate below.
# No active Probe, Peripheral or RF verdict.
# Requires exclusive use of the phone and restores/readbacks ON before returning.
[working-directory("tests/consumers/react-native-internal/ios")]
host-ios-device-radio-control-test udid team result inspect_only="0": host-ios-pods
    test -n {{ quote(udid) }} && test -n {{ quote(team) }} && test -n {{ quote(result) }} && test ! -e {{ quote(result) }} && test ! -e {{ quote(result) }}.log && test ! -e {{ quote(result) }}.summary.json && (test {{ quote(inspect_only) }} = 0 || test {{ quote(inspect_only) }} = 1)
    pixi run python -c 'import sys; from pathlib import Path; root=Path("../../../..").resolve(); sys.path.insert(0,str(root)); from tools.react_native.ios_device import require_ready_unlocked_device; require_ready_unlocked_device(sys.argv[1], cwd=root)' {{ quote(udid) }}
    TEST_RUNNER_BLEHUB_IOS_RADIO_CONTROL_INSPECT_ONLY={{ quote(inspect_only) }} TEST_RUNNER_BLEHUB_IOS_RADIO_CONTROL_GATE=1 pixi run python ../../../../tools/react_native/run_quiet.py --no-tty {{ quote(result) }}.log -- pixi run -e apple-ci xcodebuild -quiet -workspace BleHubNitroHost.xcworkspace -scheme BleHubNitroHostUIOnly -configuration Debug -destination 'id={{ udid }}' -derivedDataPath build/device-ui-derived -resultBundlePath {{ quote(result) }} DEVELOPMENT_TEAM={{ team }} CODE_SIGN_STYLE=Automatic CODE_SIGN_IDENTITY='Apple Development' ONLY_ACTIVE_ARCH=YES -parallel-testing-enabled NO -collect-test-diagnostics never -only-testing:BleHubNitroHostUITests/BleHubNitroHostUITests/testBluetoothSettingsOffAndRestore test
    xcrun xcresulttool get test-results summary --path {{ quote(result) }} --compact > {{ quote(result) }}.summary.json
    pixi run python -c 'import json,sys; value=json.load(open(sys.argv[1])); assert value["passedTests"] == value["totalTestCount"] == 1 and value["failedTests"] == value["skippedTests"] == 0, value; print("ios-radio-control=" + ("inspection-only" if sys.argv[2] == "1" else "passed") + " scope=settings-control-feasibility-non-rf")' {{ quote(result) }}.summary.json {{ quote(inspect_only) }}

# Candidate iOS startup-off RF gate. Requires prebuilt signed App + xctestrun;
# no build/provision fallback. Explicit authorization permits one radio OFF/ON window.
# Unknown final ON proof quarantines the phone lease; no automatic retry or unlock.
# Not a recorded RF pass merely because this recipe exists.
host-ios-rf-adapter-startup-candidate udid app xctestrun fixture_contract fixture_profile authorization:
    test {{ quote(authorization) }} = authorize-radio
    pixi run python -m tools.react_native.run_ios_adapter_startup {{ quote(udid) }} --app {{ quote(app) }} --xctestrun {{ quote(xctestrun) }} --fixture-contract {{ quote(fixture_contract) }} --fixture-profile {{ quote(fixture_profile) }} --authorize-radio

# Keep UI execution non-interactive; opt-in privileged failure diagnostics are not part of this gate.
[working-directory("tests/consumers/react-native-internal/ios")]
host-ios-device-ui-test udid="" team="": host-ios-pods
    test -n "{{ udid }}"
    test -n "{{ team }}"
    pixi run python ../../../../tools/react_native/run_quiet.py --no-tty build/device-ui-test.log -- pixi run -e apple-ci xcodebuild -quiet -workspace BleHubNitroHost.xcworkspace -scheme BleHubNitroHostUIOnly -configuration Debug -destination 'id={{ udid }}' -derivedDataPath build/device-ui-derived DEVELOPMENT_TEAM={{ team }} CODE_SIGN_STYLE=Automatic CODE_SIGN_IDENTITY='Apple Development' ONLY_ACTIVE_ARCH=YES -collect-test-diagnostics never -only-testing:BleHubNitroHostUITests/BleHubNitroHostUITests/testContractLabCompletesAndRecreatesProbe test
