# React Native iPhone -> nRF52840 ten-cycle recovery RF checkpoint

Date: 2026-09-27. Scope: private, unfrozen RN/Nitro iPhone host, physical iPhone 14 Plus (`00008110-000C61160C39401E`), and standalone nRF52840 Peripheral. This is not an Android RN, public BLeng, ABI-freeze, or Phase 3/5 exit result.

## Full recipe and path

```text
pixi run just react-native host-ios-device-rf-recovery-ten 00008110-000C61160C39401E FWVYQ287W5
```

The signed host built, installed, launched, passed the independent RF log checker, stopped the fixture, and returned success. The path was RN JavaScript -> Nitro HybridObject -> Swift adapter -> production Stable UniFFI BleHub -> production Apple/CoreBluetooth backend -> standalone nRF52840. See the [RN flow](../../../tests/consumers/react-native-internal/src/App.tsx), [host/fixture controller](../../../tools/react_native/run_ios_rf_scan.py), [console transport](../../../tools/react_native/run_ios_device_runtime.py), [ledger checker](../../../tools/react_native/check_runtime_log.py), and [nRF fixture](../../../tests/peripherals/nrf52840/direct_fixture.py).

## Evidence

One probe and listener stayed active across exactly ten ordered cycles (0–9), with disjoint caller-owned IDs (`100xx` through `109xx`). Each cycle checked scan/filter/stop and 2,000 ms post-stop silence, connection, aggregate GATT discovery, Notification enable in actual `Notification` mode and exact `ef` value, fixture-triggered passive disconnect (`commandId=none`, `error=connection`), 1,000 ms old-route silence, a fresh-ID recovery connection, and explicit recovery disconnect with 1,000 ms silence.

The ignored verified log is `tests/consumers/react-native-internal/ios/build/device-rf-recovery-ten.log`. Its 114 mode markers comprise listener registration, initialization, 110 ordered per-cycle markers, `rf-recovery-ten:close:passed:listener-removed=true:probe-closed=true`, and `rf-recovery-ten:passed:cycles=10`. There are no `:failed:` markers. The independent checker also passed when run separately:

```text
pixi run python tools/react_native/check_runtime_log.py tests/consumers/react-native-internal/ios/build/device-rf-recovery-ten.log --rf-recovery-ten-service-uuid fd000000-0000-4000-8000-000000000000
React Native Nitro runtime gate passed: tests/consumers/react-native-internal/ios/build/device-rf-recovery-ten.log
```

After the recipe, `nrf52840_fixture=stopped`; the RN lock and ten-cycle coordination directory were absent, and no matching detached fixture, runtime controller, or `devicectl device process launch` remained. The actual console capture ended at `close:passed` rather than waiting for application exit or a possible final JS summary; on this run, the summary was also present. The [earlier incomplete attempt](2026-09-27-react-native-ios-nrf-recovery-ten-incomplete.md) remains separate and is not retroactively relabeled as a pass.

## Controller boundaries

`devicectl --console` bridges stdout and waits for application exit; it does not define test completion for an RN app that remains running. The controller therefore stops capture on the observed close/failure marker; a missing close after the final cycle fails after a 15-second grace. The independent checker—not UI text or console process exit—requires the full ordered, ID-correlated ledger and clean close. The nRF provider owns fixture readiness, fault injection, and cleanup. Signed XCUITest remains a complementary iPhone UI/lifecycle gate; it does not replace the external Peripheral or production event-ledger checker.

After this RF run, the console controller was additionally hardened to exit immediately for nested RN `:failed:` markers and to stop its owned console child on an interrupt; the no-device tests cover these behaviors. That follow-up change has unit-test validation, **not a second physical ten-cycle RF run**.

## Validation and limits

```text
pixi run python -m unittest tools.react_native.test_run_ios_device_runtime tools.react_native.test_check_runtime_log tools.react_native.test_run_ios_rf_scan tests.peripherals.nrf52840.test_provider
# 45 tests, OK
pixi run ruff check tools/react_native/run_ios_device_runtime.py tools/react_native/test_run_ios_device_runtime.py
# passed
pixi run python tools/react_native/check_single_image.py ios tests/consumers/react-native-internal/ios/build/device-derived/Build/Products/Debug-iphoneos/BleHubNitroHost.app/BleHubNitroHost.debug.dylib
# ios single-image gate passed for this built device app
pixi run just fmt
pixi run just lint
# passed
```

This is sequential passive fault/recovery pressure with an active Notification route. It does not establish connected RSSI through the private RN projection, pending ATT callback fault/reload synchronization, repeat engine-reload soak, service invalidation RF, Android RN RF, or a public React Native/BLeng API.
