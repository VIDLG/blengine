# React Native iPhone -> nRF52840 Notification RF checkpoint

Date: 2026-09-26. Scope: the private, unfrozen RN/Nitro low-level consumer, a physical iPhone 14 Plus (`00008110-000C61160C39401E`), and the standalone nRF52840 provider. This is not a BLeng release gate, Android RN RF evidence, strict Indication evidence, or a Phase 3 exit result.

## Candidate and path

The signed RN host was built, installed, and exercised through:

```text
pixi run just react-native host-ios-device-rf-notification 00008110-000C61160C39401E FWVYQ287W5
```

The completed path was:

```text
RN JavaScript -> Nitro HybridObject -> Swift adapter -> production Stable UniFFI BleHub -> production Apple/CoreBluetooth backend -> standalone nRF52840 Peripheral
```

The flow is implemented in [`tests/consumers/react-native-internal/src/App.tsx`](../../../tests/consumers/react-native-internal/src/App.tsx). The Swift adapter is [`integrations/react-native-internal/ios/HybridBleHubProbe.swift`](../../../integrations/react-native-internal/ios/HybridBleHubProbe.swift), and the independent checker is [`tools/react_native/check_runtime_log.py`](../../../tools/react_native/check_runtime_log.py). The RN runner owns the nRF fixture and coordinates its deterministic value trigger through a private host marker directory.

The private Nitro projection reuses the production `SetNotificationsCommand`, `NotificationStateChanged`, and `NotificationReceived` contract. It does not introduce a second subscription model or public React Native API. Android remains deterministic and fail-closed for RF.

## Coordination and recovery hardening

The first attempt reached `notification-enable` with `actualMode=Notification` but timed out waiting for a value. Investigation found an infrastructure omission: the RN runner started the nRF fixture without passing its existing coordination directory, so the fixture correctly waited for `notification-enabled` and never applied its `EF` value. The runner now:

1. starts `direct-start --coordination-path ... --coordination-mode notification`;
2. writes `notification-enabled` after the RN enable state and terminal are observed;
3. waits for the fixture's `value-applied` marker before continuing;
4. writes `notification-disabled` after the RN disable state and terminal;
5. waits for the fixture's `post-disable-triggered` acknowledgement; and
6. removes the coordination directory during cleanup.

The device runner also recognizes final RF `failed` markers and exits immediately instead of waiting for the full console timeout. This preserves the attempt log while making a real runtime failure actionable.

## Observed RF ledger

The final successful run passed the runtime marker gate and the independent checker:

```text
rf:step:initialize:passed:commandId=9001:adapterState=PoweredOn:scanAuthorization=Authorized
rf:step:scan-start:passed:startCommandId=9002:scanSessionId=9004
rf:step:advertisement:passed:scanSessionId=9004:peripheralId=0:localName=missing:filterServiceUuid=fd000000-0000-4000-8000-000000000000:payloadAdvertisedServices=fd000000-0000-4000-8000-000000000000:payloadContainsFilterUuid=true
rf:step:scan-stop:passed:stopCommandId=9003:scanSessionId=9004:requested=true
rf:step:silence:passed:scanSessionId=9004:observationMs=2000
rf:step:connecting:passed:commandId=9005:connectionId=9007:peripheralId=0:serviceUuid=fd000000-0000-4000-8000-000000000000
rf:step:connected:passed:commandId=9005:connectionId=9007:peripheralId=0:serviceUuid=fd000000-0000-4000-8000-000000000000
rf:step:gatt-discovery:passed:commandId=9008:connectionId=9007:peripheralId=0:serviceUuid=fd000000-0000-4000-8000-000000000000:gattSessionId=9009:requestedGattSessionId=9009:serviceCount=1:discoveredServiceCount=2:characteristicCount=5:descriptorCount=2
rf:step:notification-enable:passed:commandId=9015:connectionId=9007:gattSessionId=9009:subscriptionId=9017:characteristicId=14:actualMode=Notification
rf:step:notification-value:passed:connectionId=9007:gattSessionId=9009:subscriptionId=9017:characteristicId=14:value=ef
rf:step:notification-disable:passed:commandId=9016:connectionId=9007:gattSessionId=9009:subscriptionId=9017:characteristicId=14:state=Disabled:postDisableSilenceMs=2000
rf:step:disconnected:passed:commandId=9006:connectionId=9007:peripheralId=0:serviceUuid=fd000000-0000-4000-8000-000000000000
rf:step:connection-silence:passed:connectionId=9007:peripheralId=0:serviceUuid=fd000000-0000-4000-8000-000000000000:observationMs=1000
rf:step:close:passed:probe-closed=true:listener-removed=true
rf-notification:passed:serviceUuid=fd000000-0000-4000-8000-000000000000:peripheralId=0:connectionId=9007:connectCommandId=9005:gattCommandId=9008:enableCommandId=9015:disableCommandId=9016:subscriptionId=9017:notificationValue=ef:postDisableSilenceMs=2000:disconnectCommandId=9006:scanSessionId=9004
```

The notification characteristic was the validated `fff20000-0000-4000-8000-000000000000` updates instance in the canonical FD00 service. Its backend-private characteristic identifier was `14` in this run. `PlatformDefault` was reported as actual `Notification`; no Indication claim is made.

The fixture applied exactly one `EF` value after the host observed the enable terminal. The app received that value with the same connection, GATT session, subscription, and characteristic identifiers. After disable completion, the app observed two seconds without a stale value for the retired subscription before allowing the fixture's post-disable acknowledgement. Requested disconnect and one second of post-disconnect silence then completed normally.

`peripheralId=0` is a valid canonical unsigned `u64`, and `localName=missing` is permitted by this provider profile. The scan and connection silence windows are JS-observed windows. The fixture was stopped by the runner after the successful check.

## Validation

Passed during this slice:

```text
pixi run just react-native host-ios-build
pixi run python -m unittest tools.react_native.test_check_runtime_log tools.react_native.test_run_ios_rf_scan
pixi run ruff check tools/react_native/run_ios_device_runtime.py tools/react_native/run_ios_rf_scan.py tools/react_native/check_runtime_log.py tools/react_native/test_check_runtime_log.py
pixi run just react-native host-ios-device-rf-notification 00008110-000C61160C39401E FWVYQ287W5
```

The checker/runner regression suite passed 17 tests. The final physical run printed `React Native Nitro runtime gate passed`, the checker passed, and cleanup printed `nrf52840_fixture=stopped`. The verified ignored attempt log was promoted to:

```text
tests/consumers/react-native-internal/ios/build/device-rf-notification.log
```

## Boundaries

This pass proves the private RN PlatformDefault Notification projection on one physical iPhone-to-nRF path, including exact value delivery, disable ordering, stale-value silence, requested disconnect, and post-disconnect silence. It does not prove:

- strict Indication or the `fff50000` indication-only characteristic;
- passive disconnect and recovery;
- repeated-cycle or pressure/soak behavior;
- Android RN runtime or RF;
- the future BLeng/public React Native API;
- ABI freeze or a Phase 3 exit.

The next RF slice is strict Indication, followed by passive-disconnect/recovery and bounded repeat/pressure gates.
