set working-directory := "../.."

integration := "integrations/react-native-internal"
host := "tests/consumers/react-native-internal"

# Install the pinned Nitro module dependencies. Ordinary checks never install toolchains.
[working-directory("integrations/react-native-internal")]
install:
    npm ci --ignore-scripts --no-audit --no-fund

# Type-check the module and regenerate ignored Swift/Kotlin/C++ Nitro glue.
[working-directory("integrations/react-native-internal")]
check:
    npm run check

# Stage the existing production UniFFI package and canonical Apple backend sources.
# This never builds or creates another Rust native image.
stage-apple-production:
    test -f .work/staged/apple-production/Sources/BleHubUniFfi/blehub.swift
    test -d .work/staged/apple-production/BleHubUniFfiNative.xcframework
    rm -rf {{integration}}/apple-production \
        {{integration}}/apple-pods/BleHubUniFfi/Sources \
        {{integration}}/apple-pods/BleHubUniFfi/BleHubUniFfiNative.xcframework \
        {{integration}}/apple-pods/BleHubUniFfi/Package.swift \
        {{integration}}/apple-pods/BleHubUniFfi/BleHubUniFfi.flavor \
        {{integration}}/apple-pods/BleHubApple/Sources

    mkdir -p {{integration}}/apple-pods/BleHubUniFfi
    cp -R .work/staged/apple-production/. {{integration}}/apple-pods/BleHubUniFfi/
    mkdir -p {{integration}}/apple-pods/BleHubApple/Sources
    cp -R platforms/apple/Sources/BleHubApple {{integration}}/apple-pods/BleHubApple/Sources/

# Standalone Android module build. Android runtime is intentionally not an extraction blocker.
android-build:
    pixi run python tools/react_native/run_quiet.py {{integration}}/android/build/module-debug.log -- pixi run -e android gradle -p {{integration}}/android :assembleDebug --no-daemon

android-test-build:
    pixi run python tools/react_native/run_quiet.py {{integration}}/android/build/module-android-test.log -- pixi run -e android gradle -p {{integration}}/android :assembleDebugAndroidTest --no-daemon

# Host instrumentation uses the project runner's adb push + root pm install path.
# Do not replace this with Gradle connected tests: those may invoke adb install and
# surface the OEM package-installer confirmation UI on the rooted physical phone.
android-test device test_class="": host-install host-check host-android-config-check
    test -n "{{device}}"
    pixi run python tools/react_native/run_quiet.py {{host}}/android/build/host-instrumentation-build.log -- pixi run -e android {{host}}/android/gradlew -p {{host}}/android :app:assembleDebug :app:assembleDebugAndroidTest --no-daemon
    if test -n "{{test_class}}"; then \
        pixi run -e android python -m tools.android.run_instrumentation --device physical --serial {{device}} --app-apk {{host}}/android/app/build/outputs/apk/debug/app-debug.apk --test-apk {{host}}/android/app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk --test-component com.blehubnitrohost.test/androidx.test.runner.AndroidJUnitRunner --test-class {{quote(test_class)}} --log {{host}}/android/build/host-instrumentation.log --timeout 120; \
    else \
        pixi run -e android python -m tools.android.run_instrumentation --device physical --serial {{device}} --app-apk {{host}}/android/app/build/outputs/apk/debug/app-debug.apk --test-apk {{host}}/android/app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk --test-component com.blehubnitrohost.test/androidx.test.runner.AndroidJUnitRunner --log {{host}}/android/build/host-instrumentation.log --timeout 120; \
    fi

# Install downstream mobile-host dependencies from the private local integration.
host-install: install check
    npm --prefix {{host}} install --ignore-scripts --no-audit --no-fund

host-check:
    {{host}}/node_modules/.bin/tsc -p {{host}}/tsconfig.json --noEmit

# Deterministic native handoff/close tests without CocoaPods, signing or hardware.
host-ios-handoff-test:
    pixi run -e apple-ci swift test --package-path {{integration}}/ios --scratch-path .work/rn-handoff-swift-test

[working-directory("tests/consumers/react-native-internal/ios")]
host-ios-native-lifecycle-test simulator="": host-ios-pods
    test -n "{{simulator}}"
    pixi run python ../../../../tools/react_native/run_quiet.py build/native-test-build.log -- pixi run -e apple-ci xcodebuild -quiet -workspace BleHubNitroHost.xcworkspace -scheme BleHubNitroHost -configuration Debug -sdk iphonesimulator -destination 'generic/platform=iOS Simulator' -derivedDataPath build/native-test-derived CODE_SIGNING_ALLOWED=NO build-for-testing
    pixi run python ../../../../tools/react_native/run_quiet.py build/native-test-run.log -- pixi run -e apple-ci xcodebuild -quiet -workspace BleHubNitroHost.xcworkspace -scheme BleHubNitroHost -configuration Debug -destination 'platform=iOS Simulator,id={{simulator}}' -derivedDataPath build/native-test-derived CODE_SIGNING_ALLOWED=NO -only-testing:BleHubNitroHostNativeTests/BleHubNitroHostNativeTests test-without-building

# Confirm RN CLI Android autolinking resolves the Nitro package and loader-only package.
host-android-config-check: host-install
    cd {{host}} && node -e 'const {execFileSync}=require("node:child_process"); const assert=require("node:assert/strict"); const config=JSON.parse(execFileSync("./node_modules/.bin/react-native",["config","--platform","android"],{encoding:"utf8"})); const dependency=config.dependencies["@blehub/react-native-internal"]; assert(dependency?.platforms?.android, "Nitro Android dependency was not autolinked"); assert.match(dependency.platforms.android.packageImportPath,/BleHubReactNativePackage/); assert.match(dependency.platforms.android.packageInstance,/BleHubReactNativePackage/); console.log("RN CLI autolinks @blehub/react-native-internal -> BleHubReactNativePackage")'

host-android-build: host-install host-check host-android-config-check
    pixi run python tools/react_native/run_quiet.py {{host}}/android/build/host-debug.log -- pixi run -e android {{host}}/android/gradlew -p {{host}}/android :app:assembleDebug --no-daemon

host-android-single-image-check: host-android-build
    pixi run python tools/react_native/check_single_image.py android {{host}}/android/app/build/outputs/apk/debug/app-debug.apk

# Build an isolated Android RN consumer from an npm tarball and published fixture Maven artifacts.
host-android-artifact-consumer: install check
    pixi run python -m tools.react_native.android_artifact_consumer

# Optional physical-device lifecycle gate from the clean npm-tarball consumer.
# The default artifact consumer remains static-only and device-free.
host-android-artifact-device-lifecycle device: install check
    test -n "{{device}}"
    pixi run python -m tools.react_native.android_artifact_consumer --device {{device}}

host-android-runtime device="": host-install host-check host-android-config-check
    test -n "{{device}}"
    pixi run python tools/react_native/run_quiet.py {{host}}/android/build/host-release.log -- pixi run -e android {{host}}/android/gradlew -p {{host}}/android :app:assembleRelease --no-daemon
    pixi run -e android python -m tools.android.install_apk --serial {{device}} --device physical --apk {{host}}/android/app/build/outputs/apk/release/app-release.apk --log {{host}}/android/build/apk-install.log
    pixi run python -m tools.react_native.run_android_runtime {{device}} --mode basic --log {{host}}/android/build/runtime.log

# Android RN close/recreate, listener removal, late-callback and post-close dispatch gate.
# This is host-only: it does not start a Peripheral or require RF hardware.
host-android-lifecycle-stress device="": host-install host-check host-android-config-check
    test -n "{{device}}"
    pixi run python tools/react_native/run_quiet.py {{host}}/android/build/host-lifecycle-release.log -- pixi run -e android {{host}}/android/gradlew -p {{host}}/android :app:assembleRelease --no-daemon
    pixi run -e android python -m tools.android.install_apk --serial {{device}} --device physical --apk {{host}}/android/app/build/outputs/apk/release/app-release.apk --log {{host}}/android/build/lifecycle-apk-install.log
    pixi run python -m tools.react_native.run_android_runtime {{device}} --mode stress --log {{host}}/android/build/lifecycle-stress.log --timeout 90

# Android RN JS runtime invalidation through ReactDelegate.reload().
# This is distinct from Activity/process restart and uses the host's test-only intent hook.
host-android-runtime-reload device="": host-install host-check host-android-config-check
    test -n "{{device}}"
    pixi run python tools/react_native/run_quiet.py {{host}}/android/build/host-reload-release.log -- pixi run -e android {{host}}/android/gradlew -p {{host}}/android :app:assembleRelease --no-daemon
    pixi run -e android python -m tools.android.install_apk --serial {{device}} --device physical --apk {{host}}/android/app/build/outputs/apk/release/app-release.apk --log {{host}}/android/build/reload-apk-install.log
    pixi run python -m tools.react_native.run_android_runtime {{device}} --mode reload --log {{host}}/android/build/runtime-reload.log --timeout 120

# Android RN JS -> Nitro -> production Android scan gate against the standalone nRF DK.
# The runner owns fixture readiness, bounded progress, failure-fast log handling and cleanup.
host-android-rf device mode="rf" service_uuid="fd000000-0000-4000-8000-000000000000": host-install host-check host-android-config-check
    test -n "{{device}}"
    pixi run python tools/react_native/run_quiet.py {{host}}/android/build/host-rf-release.log -- pixi run -e android {{host}}/android/gradlew -p {{host}}/android :app:assembleRelease --no-daemon
    pixi run -e android adb -s {{device}} wait-for-device
    pixi run -e android python -m tools.android.install_apk --serial {{device}} --device physical --apk {{host}}/android/app/build/outputs/apk/release/app-release.apk --log {{host}}/android/build/rf-apk-install.log
    pixi run python -m tools.react_native.run_android_rf {{device}} --mode {{mode}} --service-uuid {{service_uuid}}

# Profile-aware single-DK control used for per-device Android RF diagnosis.
# fixture_device and service_uuid must name the same local DK/profile pair.
host-android-rf-single device fixture_device service_uuid mode="rf": host-install host-check host-android-config-check
    test -n "{{device}}" && test -n "{{fixture_device}}" && test -n "{{service_uuid}}"
    pixi run python tools/react_native/run_quiet.py {{host}}/android/build/host-rf-single-release.log -- pixi run -e android {{host}}/android/gradlew -p {{host}}/android :app:assembleRelease --no-daemon
    pixi run -e android adb -s {{device}} wait-for-device
    pixi run -e android python -m tools.android.install_apk --serial {{device}} --device physical --apk {{host}}/android/app/build/outputs/apk/release/app-release.apk --log {{host}}/android/build/rf-single-apk-install.log
    pixi run python -m tools.react_native.run_android_rf {{device}} --mode {{mode}} --service-uuid {{service_uuid}} --fixture-device {{fixture_device}}

# Private two-DK gate. The runner derives both service UUIDs from the explicit
# local device/profile mapping and fails before the RN host if either DK is not ready.
host-android-rf-multi device notification_mode="both" subscription_mode="notification" cycles="1": host-install host-check host-android-config-check
    test -n "{{device}}"
    pixi run python tools/react_native/run_quiet.py {{host}}/android/build/host-rf-multi-release.log -- pixi run -e android {{host}}/android/gradlew -p {{host}}/android :app:assembleRelease --no-daemon
    pixi run -e android adb -s {{device}} wait-for-device
    pixi run -e android python -m tools.android.install_apk --serial {{device}} --device physical --apk {{host}}/android/app/build/outputs/apk/release/app-release.apk --log {{host}}/android/build/rf-multi-apk-install.log
    pixi run python -m tools.react_native.run_android_multi_rf {{device}} --notification-mode {{notification_mode}} --subscription-mode {{subscription_mode}} --cycles {{cycles}}

# One RN process/probe continuous multi-DK pressure gate. Unlike --cycles above,
# this keeps the fixture and probe alive across all inner ledgers.
host-android-rf-multi-soak device subscription_mode="notification" cycles="10": host-install host-check host-android-config-check
    test -n "{{device}}"
    pixi run python tools/react_native/run_quiet.py {{host}}/android/build/host-rf-multi-soak-release.log -- pixi run -e android {{host}}/android/gradlew -p {{host}}/android :app:assembleRelease --no-daemon
    pixi run -e android adb -s {{device}} wait-for-device
    pixi run -e android python -m tools.android.install_apk --serial {{device}} --device physical --apk {{host}}/android/app/build/outputs/apk/release/app-release.apk --log {{host}}/android/build/rf-multi-soak-apk-install.log
    pixi run python -m tools.react_native.run_android_multi_rf {{device}} --runtime-mode rf-multi-soak --subscription-mode {{subscription_mode}} --soak-cycles {{cycles}}

# Destructive private two-DK fault gate: stop only DK-A, require DK-B survival,
# then restart DK-A and verify a fresh connection/GATT route.
host-android-rf-multi-fault device: host-install host-check host-android-config-check
    test -n "{{device}}"
    pixi run python tools/react_native/run_quiet.py {{host}}/android/build/host-rf-multi-fault-release.log -- pixi run -e android {{host}}/android/gradlew -p {{host}}/android :app:assembleRelease --no-daemon
    pixi run -e android adb -s {{device}} wait-for-device
    pixi run -e android python -m tools.android.install_apk --serial {{device}} --device physical --apk {{host}}/android/app/build/outputs/apk/release/app-release.apk --log {{host}}/android/build/rf-multi-fault-apk-install.log
    pixi run python -m tools.react_native.run_android_multi_fault_rf {{device}}

host-android-bundle: host-install host-check
    cd {{host}} && ./node_modules/.bin/react-native bundle --platform android --dev false --entry-file index.js --bundle-output /tmp/blehub-nitro-host.android.bundle --assets-dest /tmp/blehub-nitro-host-assets

[working-directory("tests/consumers/react-native-internal/ios")]
host-ios-pods: stage-apple-production host-install host-check
    pixi run python ../../../../tools/react_native/run_quiet.py build/pod-install.log -- pixi run -e apple-ci pod install

[working-directory("tests/consumers/react-native-internal/ios")]
host-ios-build: host-ios-pods
    pixi run python ../../../../tools/react_native/run_quiet.py build/simulator-build.log -- pixi run -e apple-ci xcodebuild -quiet -workspace BleHubNitroHost.xcworkspace -scheme BleHubNitroHost -configuration Debug -sdk iphonesimulator -destination 'generic/platform=iOS Simulator' -derivedDataPath build/simulator-derived CODE_SIGNING_ALLOWED=NO build

host-ios-single-image-check: host-ios-build
    pixi run -e apple-ci python tools/react_native/check_single_image.py ios {{host}}/ios/build/simulator-derived/Build/Products/Debug-iphonesimulator/BleHubNitroHost.app/BleHubNitroHost.debug.dylib

# Build an isolated, unsigned iOS RN consumer from an npm tarball, never the module source.
host-ios-artifact-consumer: install check
    pixi run python -m tools.react_native.ios_artifact_consumer

# Signed clean-artifact iPhone runtime smoke; no RF or Peripheral is involved.
host-ios-artifact-device-runtime udid team: install check
    pixi run python -m tools.react_native.ios_artifact_consumer --device-udid {{quote(udid)}} --development-team {{quote(team)}}

# Signed clean-artifact iPhone XCUITest launched directly after artifact install.
# This remains a bridge/UI lifecycle gate, not an RF test.
host-ios-artifact-ui-test udid team: install check
    pixi run python -m tools.react_native.ios_artifact_consumer --device-udid {{quote(udid)}} --development-team {{quote(team)}} --ui-test

[working-directory("tests/consumers/react-native-internal/ios")]
host-ios-runtime udid="": host-ios-pods
    test -n "{{udid}}"
    xcrun simctl boot {{udid}} >/dev/null 2>&1 || true
    xcrun simctl bootstatus {{udid}} -b
    pixi run python ../../../../tools/react_native/run_quiet.py build/simulator-runtime-build.log -- pixi run -e apple-ci xcodebuild -quiet -workspace BleHubNitroHost.xcworkspace -scheme BleHubNitroHost -configuration Release -sdk iphonesimulator -destination 'platform=iOS Simulator,id={{udid}}' -derivedDataPath build/runtime-derived CODE_SIGNING_ALLOWED=NO build
    xcrun simctl install {{udid}} build/runtime-derived/Build/Products/Release-iphonesimulator/BleHubNitroHost.app
    xcrun simctl launch {{udid}} org.reactjs.native.example.BleHubNitroHost
    sleep 5
    xcrun simctl spawn {{udid}} log show --last 30s --info --debug --style compact --predicate 'eventMessage CONTAINS "BLEHUB_NITRO_RUNTIME"' > build/runtime.log
    pixi run python ../../../../tools/react_native/check_runtime_log.py build/runtime.log

[working-directory("tests/consumers/react-native-internal/ios")]
host-ios-device-build udid="" team="": host-ios-pods
    test -n "{{udid}}"
    test -n "{{team}}"
    pixi run python ../../../../tools/react_native/run_quiet.py --no-tty build/device-build.log -- pixi run -e apple-ci xcodebuild -quiet -workspace BleHubNitroHost.xcworkspace -scheme BleHubNitroHost -configuration Debug -sdk iphoneos -destination 'id={{udid}}' -derivedDataPath build/device-derived DEVELOPMENT_TEAM={{team}} PRODUCT_BUNDLE_IDENTIFIER=org.reactjs.native.example.BleHubNitroHost CODE_SIGN_STYLE=Automatic CODE_SIGN_IDENTITY='Apple Development' ONLY_ACTIVE_ARCH=YES build
    test -d build/device-derived/Build/Products/Debug-iphoneos/BleHubNitroHost.app

host-ios-device-single-image-check udid="" team="":
    pixi run just react-native host-ios-device-build {{udid}} {{team}}
    pixi run python tools/react_native/check_single_image.py ios {{host}}/ios/build/device-derived/Build/Products/Debug-iphoneos/BleHubNitroHost.app/BleHubNitroHost.debug.dylib

host-ios-device-runtime udid="" team="":
    pixi run just react-native host-ios-device-build {{udid}} {{team}}
    pixi run -e apple-ci xcrun devicectl device install app --device {{udid}} --json-output {{host}}/ios/build/device-install.json {{host}}/ios/build/device-derived/Build/Products/Debug-iphoneos/BleHubNitroHost.app
    pixi run python tools/react_native/run_ios_device_runtime.py {{udid}} {{host}}/ios/build/device-runtime.log --seconds 20
    pixi run python tools/react_native/check_runtime_log.py {{host}}/ios/build/device-runtime.log

host-ios-device-lifecycle-stress udid="" team="":
    pixi run just react-native host-ios-device-build {{udid}} {{team}}
    pixi run -e apple-ci xcrun devicectl device install app --device {{udid}} --json-output {{host}}/ios/build/device-stress-install.json {{host}}/ios/build/device-derived/Build/Products/Debug-iphoneos/BleHubNitroHost.app
    pixi run python tools/react_native/run_ios_device_runtime.py {{udid}} {{host}}/ios/build/device-lifecycle-stress.log --seconds 35 --stress
    pixi run python tools/react_native/check_runtime_log.py {{host}}/ios/build/device-lifecycle-stress.log --stress

host-ios-device-reload udid="" team="":
    pixi run just react-native host-ios-device-build {{udid}} {{team}}
    pixi run -e apple-ci xcrun devicectl device install app --device {{udid}} --json-output {{host}}/ios/build/device-reload-install.json {{host}}/ios/build/device-derived/Build/Products/Debug-iphoneos/BleHubNitroHost.app
    pixi run python tools/react_native/run_ios_device_runtime.py {{udid}} {{host}}/ios/build/device-reload.log --seconds 40 --reload
    pixi run python tools/react_native/check_runtime_log.py {{host}}/ios/build/device-reload.log --reload

# iPhone RN JS -> Nitro -> production Apple scan gate against the standalone nRF.
# RN gates share an atomic lock; other Apple native gates do not use it.
host-ios-device-rf-scan udid="" team="":
    test -n "{{udid}}" && test -n "{{team}}"
    pixi run just react-native host-ios-device-build {{udid}} {{team}}
    pixi run python -m tools.react_native.run_ios_rf_scan {{quote(udid)}}

# Separate RN JS-driven nRF connect/disconnect gate; scan-only pass is not connection evidence.
host-ios-device-rf-connect udid="" team="":
    test -n "{{udid}}" && test -n "{{team}}"
    pixi run just react-native host-ios-device-build {{udid}} {{team}}
    pixi run python -m tools.react_native.run_ios_rf_scan {{quote(udid)}} --connect

# RN JS-driven aggregate GATT discovery gate against the connected nRF profile.
host-ios-device-rf-gatt udid="" team="":
    test -n "{{udid}}" && test -n "{{team}}"
    pixi run just react-native host-ios-device-build {{udid}} {{team}}
    pixi run python -m tools.react_native.run_ios_rf_scan {{quote(udid)}} --gatt

# RN JS-driven canonical read/write gate after aggregate GATT discovery.
host-ios-device-rf-io udid="" team="":
    test -n "{{udid}}" && test -n "{{team}}"
    pixi run just react-native host-ios-device-build {{udid}} {{team}}
    pixi run python -m tools.react_native.run_ios_rf_scan {{quote(udid)}} --io

# Connected RSSI read through the production Apple backend; no synthetic fixture value.
host-ios-device-rf-rssi udid="" team="":
    test -n "{{udid}}" && test -n "{{team}}"
    pixi run just react-native host-ios-device-build {{udid}} {{team}}
    pixi run python -m tools.react_native.run_ios_rf_scan {{quote(udid)}} --rssi

# RN JS-driven PlatformDefault Notification enable/value/disable gate.
host-ios-device-rf-notification udid="" team="":
    test -n "{{udid}}" && test -n "{{team}}"
    pixi run just react-native host-ios-device-build {{udid}} {{team}}
    pixi run python -m tools.react_native.run_ios_rf_scan {{quote(udid)}} --notification

# RN JS-driven strict Indication gate: dual-property rejection plus pure-Indication value flow.
host-ios-device-rf-indication udid="" team="":
    test -n "{{udid}}" && test -n "{{team}}"
    pixi run just react-native host-ios-device-build {{udid}} {{team}}
    pixi run python -m tools.react_native.run_ios_rf_scan {{quote(udid)}} --indication

# iPhone RN JS -> Nitro -> production Apple multi-DK gate. The runner shares
# the explicit two-DK controller and checker with the Android lane.
host-ios-device-rf-multi udid="" team="" subscription_mode="notification" cycles="1":
    test -n "{{udid}}" && test -n "{{team}}"
    pixi run just react-native host-ios-device-build {{udid}} {{team}}
    pixi run python -m tools.react_native.run_ios_multi_rf {{quote(udid)}} --subscription-mode {{subscription_mode}} --cycles {{cycles}}

# One RN process/probe continuous multi-DK pressure gate. Unlike --cycles above,
# this keeps the fixture and probe alive across all inner ledgers.
host-ios-device-rf-multi-soak udid="" team="" subscription_mode="notification" cycles="10":
    test -n "{{udid}}" && test -n "{{team}}"
    pixi run just react-native host-ios-device-build {{udid}} {{team}}
    pixi run python -m tools.react_native.run_ios_multi_rf {{quote(udid)}} --runtime-mode rf-multi-soak --subscription-mode {{subscription_mode}} --soak-cycles {{cycles}}

# Destructive private two-DK fault gate: stop only DK-A, require DK-B survival,
# then restart DK-A and verify a fresh connection/GATT route.
host-ios-device-rf-multi-fault udid="" team="":
    test -n "{{udid}}" && test -n "{{team}}"
    pixi run just react-native host-ios-device-build {{udid}} {{team}}
    pixi run python -m tools.react_native.run_ios_multi_fault_rf {{quote(udid)}}

# Opt-in non-destructive RF matrix: build once, then retain each independent
# checker/fixture lifecycle and its own log; stop immediately on first failure.
host-ios-device-rf-baseline udid="" team="":
    test -n "{{udid}}" && test -n "{{team}}"
    pixi run just react-native host-ios-device-build {{udid}} {{team}}
    pixi run python tools/react_native/check_single_image.py ios {{host}}/ios/build/device-derived/Build/Products/Debug-iphoneos/BleHubNitroHost.app/BleHubNitroHost.debug.dylib
    pixi run python -m tools.react_native.run_ios_rf_scan {{quote(udid)}}
    pixi run python -m tools.react_native.run_ios_rf_scan {{quote(udid)}} --connect
    pixi run python -m tools.react_native.run_ios_rf_scan {{quote(udid)}} --gatt
    pixi run python -m tools.react_native.run_ios_rf_scan {{quote(udid)}} --io
    pixi run python -m tools.react_native.run_ios_rf_scan {{quote(udid)}} --rssi
    pixi run python -m tools.react_native.run_ios_rf_scan {{quote(udid)}} --notification
    pixi run python -m tools.react_native.run_ios_rf_scan {{quote(udid)}} --indication

# Opt-in diagnostic pending-ATT reload; never builds/flashes/resets DK firmware.
# The confirmed SHA validates only a previously built local HEX, not DK flash.
host-ios-device-rf-pending-read-reload udid team probe_serial confirmed_sha:
    pixi run just react-native host-ios-device-build {{quote(udid)}} {{quote(team)}}
    pixi run python tools/react_native/check_single_image.py ios {{host}}/ios/build/device-derived/Build/Products/Debug-iphoneos/BleHubNitroHost.app/BleHubNitroHost.debug.dylib
    pixi run python -m tools.react_native.run_ios_pending_read_reload --udid {{quote(udid)}} --probe-serial {{quote(probe_serial)}} --confirm-hex-sha256 {{quote(confirmed_sha)}}

# RN JS-driven passive-disconnect/recovery gate with an active notification route.
host-ios-device-rf-passive-disconnect udid="" team="":
    test -n "{{udid}}" && test -n "{{team}}"
    pixi run just react-native host-ios-device-build {{udid}} {{team}}
    pixi run python -m tools.react_native.run_ios_rf_scan {{quote(udid)}} --passive-disconnect

# Real engine reload with an active RF subscription; the fixture observes host disconnect.
host-ios-device-rf-reload udid="" team="":
    test -n "{{udid}}" && test -n "{{team}}"
    pixi run just react-native host-ios-device-build {{udid}} {{team}}
    pixi run python -m tools.react_native.run_ios_rf_scan {{quote(udid)}} --rf-reload

# Repeated RF fault/recovery pressure using one RN probe/listener across ten cycles.
host-ios-device-rf-recovery-ten udid="" team="":
    test -n "{{udid}}" && test -n "{{team}}"
    pixi run just react-native host-ios-device-build {{udid}} {{team}}
    pixi run python -m tools.react_native.run_ios_rf_scan {{quote(udid)}} --recovery-ten

# One RN probe/listener and production Apple backend across three passive fault/recovery cycles.
host-ios-device-rf-recovery udid="" team="":
    test -n "{{udid}}" && test -n "{{team}}"
    pixi run just react-native host-ios-device-build {{udid}} {{team}}
    pixi run python -m tools.react_native.run_ios_rf_scan {{quote(udid)}} --recovery

[working-directory("tests/consumers/react-native-internal/ios")]
host-ios-device-ui-test udid="" team="": host-ios-pods
    test -n "{{udid}}"
    test -n "{{team}}"
    pixi run python ../../../../tools/react_native/run_quiet.py --no-tty build/device-ui-test.log -- pixi run -e apple-ci xcodebuild -quiet -workspace BleHubNitroHost.xcworkspace -scheme BleHubNitroHost -configuration Debug -destination 'id={{udid}}' -derivedDataPath build/device-ui-derived DEVELOPMENT_TEAM={{team}} CODE_SIGN_STYLE=Automatic CODE_SIGN_IDENTITY='Apple Development' ONLY_ACTIVE_ARCH=YES -only-testing:BleHubNitroHostUITests/BleHubNitroHostUITests/testContractLabCompletesAndRecreatesProbe test
