# React Native iPhone -> nRF52840 active RF reload checkpoint

Date: 2026-09-27. Scope: private, unfrozen RN/Nitro integration; physical iPhone (`00008110-000C61160C39401E`); standalone nRF52840 Peripheral. This is not a public BLeng/RN API, Android RF, CoreBluetooth service-invalidation, or Phase 3/5 exit gate.

## Gate and architecture

```text
pixi run just react-native host-ios-device-rf-reload 00008110-000C61160C39401E FWVYQ287W5
```

The candidate was built and installed on device; each RN generation exercised RN JS -> Nitro HybridObject -> Swift adapter -> production Stable UniFFI BleHub -> Apple/CoreBluetooth backend -> nRF52840. The test host is [`tests/consumers/react-native-internal/src/App.tsx`](../../../tests/consumers/react-native-internal/src/App.tsx), with native reload trigger in [`AppDelegate.swift`](../../../tests/consumers/react-native-internal/ios/BleHubNitroHost/AppDelegate.swift), host coordination in [`run_ios_rf_scan.py`](../../../tools/react_native/run_ios_rf_scan.py), independent checker in [`check_runtime_log.py`](../../../tools/react_native/check_runtime_log.py), and host-close observation in [`direct_fixture.py`](../../../tests/peripherals/nrf52840/direct_fixture.py).

The first generation initialized, scanned by the provider-specific service UUID, connected, discovered the GATT database, enabled PlatformDefault Notification (actual mode `Notification`), and received the exact value `ef`. It **kept the connection and subscription active** while RN's native reload command ran and the old React effect entered cleanup. The fixture did not initiate disconnect; it observed the host-side GAP disconnect, resumed advertising, and sent the same value to the new generation after its separate subscription ACK. The second generation repeated the RF scan/connect/GATT/Notification value sequence on a newly created probe/backend.

## Observed evidence

The successful log is ignored build output at:

```text
tests/consumers/react-native-internal/ios/build/device-rf-reload.log
```

It contains exactly two ordered groups of:

```text
rf-reload:listener:registered-before-initialize
rf-reload:initialize:passed:commandId=11000
rf-reload:scan:passed:serviceUuid=fd000000-0000-4000-8000-000000000000:peripheralId=0:scanSessionId=11001
rf-reload:connected:passed:commandId=11005:connectionId=11004:peripheralId=0
rf-reload:gatt:passed:commandId=11007:connectionId=11004:gattSessionId=11006
rf-reload:notification-enable:passed:commandId=11009:connectionId=11004:gattSessionId=11006:subscriptionId=11008:characteristicId=14:actualMode=Notification
rf-reload:notification-value:passed:connectionId=11004:gattSessionId=11006:subscriptionId=11008:value=ef
rf-reload:active:peripheralId=0:connectionId=11004:subscriptionId=11008
```

The first group is followed by `rf-reload:native-triggered` and `rf-reload:cleanup-started`. The runner waited for the independently controlled fixture's `generation-0/host-disconnected` file before enabling generation 1. After the console writer exited, it merged its observed `rf-reload:fixture-host-disconnected` evidence into the log **before** the second-generation enable marker and appended `rf-reload:host-complete:passed` after the second active marker. Those two markers are host-runner evidence, **not** JS or CoreBluetooth callbacks. The independent checker validated exactly two complete generation ledgers, correlations, fixture disconnect ordering, cleanup ordering, and no failed markers; it printed `React Native Nitro runtime gate passed`. Provider cleanup reported `nrf52840_fixture=stopped`.

An initial development run incorrectly waited for an old-JS `close()` Promise callback across engine reload, and a later run incorrectly expected a native log observer from the old generation to report second-generation completion. Another attempt detected a host/fixture arming race. A subsequent attempt completed both RF generations but was rejected because the console writer overwrote a concurrently inserted fixture marker. All were rejected as RF evidence; the final run uses bounded host-side ACKs and single-writer log merging.

## Validation and limits

The final candidate also passed:

```text
pixi run just react-native host-check
pixi run python -m unittest tools.react_native.test_check_runtime_log tools.react_native.test_run_ios_rf_scan tests.peripherals.nrf52840.test_provider
pixi run ruff check tools/react_native tests/peripherals/nrf52840
pixi run just lint
```

The runner/checker/provider suite passed 35 tests. This gate proves an active RN Notification connection survives teardown and new-generation reconstruction without leaving the fixture's old connection active. It does **not** prove the old JavaScript `close()` Promise callback runs after engine destruction, that Nitro's already-enqueued JS callbacks drain, a CoreBluetooth unsubscribe/disconnect callback completion, pending ATT request cancellation, repeat reload soak, iOS service invalidation (`didModifyServices`), or Android RF. Both generations use the same caller-owned numeric IDs in distinct newly created Hub/backend lifecycles; they are not reused within one Hub lifecycle.
