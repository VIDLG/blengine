# React Native iPhone -> nRF52840 passive disconnect and recovery RF checkpoint

Date: 2026-09-27. Scope: the private, unfrozen RN/Nitro low-level consumer, a physical iPhone 14 Plus (`00008110-000C61160C39401E`), and the standalone nRF52840 provider. This is not a BLeng release gate, Android RN RF evidence, or a Phase 3/5 exit result.

## Candidate and path

The signed RN host was built, installed, and exercised with:

```text
pixi run just react-native host-ios-device-rf-passive-disconnect 00008110-000C61160C39401E FWVYQ287W5
```

The completed production path was:

```text
RN JavaScript -> Nitro HybridObject -> Swift adapter -> production Stable UniFFI BleHub -> production Apple/CoreBluetooth backend -> standalone nRF52840 Peripheral
```

The flow is implemented in [`tests/consumers/react-native-internal/src/App.tsx`](../../../tests/consumers/react-native-internal/src/App.tsx). The iOS launch-mode mapping is in [`tests/consumers/react-native-internal/ios/BleHubNitroHost/AppDelegate.swift`](../../../tests/consumers/react-native-internal/ios/BleHubNitroHost/AppDelegate.swift). The host runner and checker are [`tools/react_native/run_ios_rf_scan.py`](../../../tools/react_native/run_ios_rf_scan.py) and [`tools/react_native/check_runtime_log.py`](../../../tools/react_native/check_runtime_log.py). Fixture coordination is implemented in [`tests/peripherals/nrf52840/direct_fixture.py`](../../../tests/peripherals/nrf52840/direct_fixture.py).

The private Nitro projection reuses the production connection, GATT, and subscription event model. It does not introduce a second BLE contract, native image, or public React Native API.

## Recovery assertions

This gate keeps the notification route active while the fixture owns the fault transition. The bounded coordination protocol is:

```text
notification-enable
  -> runner writes subscription-enabled
  -> fixture writes the exact EF value and value-applied
  -> runner writes connected
  -> fixture writes fault-triggered and disconnects the peer
  -> fixture observes GAP_DEVICE_DISCONNECTED, restarts advertising, writes recovery-ready
  -> RN observes passive disconnect and stale-route silence
  -> RN reconnects the retained peripheral with a new connection ID
  -> RN explicitly disconnects the recovery connection and checks terminal silence
```

CoreBluetooth reports the passive loss as a `connectionStateChanged` error with category `connection`, no command ID, and no connection state in the projected Nitro event. The RN gate accepts only this exact passive shape; other connection errors remain fail-closed. This matches the existing Apple RF `isRfPassiveDisconnect` ledger policy.

The recovery cycle uses disjoint caller-owned IDs:

```text
initial connection:  commandId=9005, connectionId=9007, gattSessionId=9009, subscriptionId=9017
recovery connection: commandId=9023, connectionId=9024, disconnectCommandId=9025
```

## Observed RF ledger

The successful run produced this ordered evidence:

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
rf:step:disconnected:passed:commandId=none:connectionId=9007:peripheralId=0:serviceUuid=fd000000-0000-4000-8000-000000000000
rf:step:connection-silence:passed:connectionId=9007:peripheralId=0:serviceUuid=fd000000-0000-4000-8000-000000000000:observationMs=1000
rf:step:recovery-connecting:passed:commandId=9023:connectionId=9024:peripheralId=0:serviceUuid=fd000000-0000-4000-8000-000000000000
rf:step:recovery-connected:passed:commandId=9023:connectionId=9024:peripheralId=0:serviceUuid=fd000000-0000-4000-8000-000000000000
rf:step:recovery-disconnected:passed:commandId=9025:connectionId=9024:peripheralId=0:serviceUuid=fd000000-0000-4000-8000-000000000000
rf:step:recovery-connection-silence:passed:connectionId=9024:peripheralId=0:serviceUuid=fd000000-0000-4000-8000-000000000000:observationMs=1000
rf:step:close:passed:probe-closed=true:listener-removed=true
rf-passive-disconnect:passed:serviceUuid=fd000000-0000-4000-8000-000000000000:peripheralId=0:connectionId=9007:connectCommandId=9005:gattCommandId=9008:subscriptionId=9017:recoveryConnectCommandId=9023:recoveryConnectionId=9024:recoveryDisconnectCommandId=9025:notificationValue=ef:passiveDisconnectCommandId=none:scanSessionId=9004
```

The independent checker printed `React Native Nitro runtime gate passed`, and the fixture cleanup printed `nrf52840_fixture=stopped`.

The verified ignored log is:

```text
tests/consumers/react-native-internal/ios/build/device-rf-passive-disconnect.log
```

## Validation

Passed during this slice:

```text
pixi run just react-native host-check
pixi run python -m unittest tools.react_native.test_check_runtime_log tools.react_native.test_run_ios_rf_scan
pixi run ruff check tests/peripherals/nrf52840/direct_fixture.py tools/react_native/run_ios_rf_scan.py tools/react_native/check_runtime_log.py tools/react_native/run_ios_device_runtime.py
pixi run just react-native host-ios-device-rf-passive-disconnect 00008110-000C61160C39401E FWVYQ287W5
```

The runner/checker regression suite passed 19 tests. The physical recipe also passed the signed device build, Nitro generation, CocoaPods staging, fixture lifecycle ownership, runtime marker gate, independent log checker, and fixture cleanup.

## Boundaries

This pass proves one private RN iOS production-path passive disconnect and recovery cycle with an active notification subscription, stale-route silence, retained-peripheral recovery connection, explicit recovery disconnect, and cleanup. It does not prove:

- repeated passive-disconnect cycles, pressure, or soak behavior;
- reload/invalidation during an active RF operation;
- Android RN runtime or RF;
- macOS RN, which is archived and not an active host;
- the future BLeng/public React Native API;
- ABI freeze or a Phase 3/5 exit.
