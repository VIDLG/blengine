# React Native iPhone -> nRF52840 strict Indication RF checkpoint

Date: 2026-09-26. Scope: the private, unfrozen RN/Nitro low-level consumer, a physical iPhone 14 Plus (`00008110-000C61160C39401E`), and the standalone nRF52840 provider. This is not a BLeng release gate, Android RN RF evidence, or a Phase 3/5 exit result.

## Candidate and path

The signed RN host was built, installed, and exercised through:

```text
pixi run just react-native host-ios-device-rf-indication 00008110-000C61160C39401E FWVYQ287W5
```

The completed path was:

```text
RN JavaScript -> Nitro HybridObject -> Swift adapter -> production Stable UniFFI BleHub -> production Apple/CoreBluetooth backend -> standalone nRF52840 Peripheral
```

The flow is implemented in [`tests/consumers/react-native-internal/src/App.tsx`](../../../tests/consumers/react-native-internal/src/App.tsx). The Swift adapter is [`integrations/react-native-internal/ios/HybridBleHubProbe.swift`](../../../integrations/react-native-internal/ios/HybridBleHubProbe.swift), the host runner is [`tools/react_native/run_ios_rf_scan.py`](../../../tools/react_native/run_ios_rf_scan.py), and the independent checker is [`tools/react_native/check_runtime_log.py`](../../../tools/react_native/check_runtime_log.py).

The private Nitro projection reuses the production `SetNotificationsCommand`, `NotificationStateChanged`, and `NotificationReceived` contract. It does not introduce a second subscription model or public React Native API. Android remains deterministic and fail-closed for RF.

## Strict Indication assertions

The RN flow first uses the `fff20000-0000-4000-8000-000000000000` updates characteristic, which has both `notify` and `indicate` properties. A strict `RequireIndication` request is required to terminate with `NotSupported`; accepting this request would be a backend policy failure because `PlatformDefault` is the appropriate mode for a dual-property characteristic.

The flow then uses the `fff50000-0000-4000-8000-000000000000` indications characteristic, which has `indicate` but not `notify`. A strict `RequireIndication` request must report actual mode `Indication`, receive the exact fixture value `EF`, disable in state-before-terminal order, and observe two seconds of stale-value silence.

The runner starts the fixture with `--coordination-mode indication`. After the enable state and terminal are observed, it signals the fixture through a private coordination directory. The fixture applies the profile's `EF` value to the indications role and acknowledges `value-applied`; after the disable terminal, the host signals `subscription-disabled` and waits for `post-disable-triggered`. Cleanup removes the coordination directory and stops only the fixture process owned by this run.

## Observed RF ledger

The final successful run printed the following ordered evidence:

```text
rf:step:initialize:passed:commandId=9001:adapterState=PoweredOn:scanAuthorization=Authorized
rf:step:scan-start:passed:startCommandId=9002:scanSessionId=9004
rf:step:advertisement:passed:scanSessionId=9004:peripheralId=0:localName=missing:filterServiceUuid=fd000000-0000-4000-8000-000000000000:payloadAdvertisedServices=fd000000-0000-4000-8000-000000000000:payloadContainsFilterUuid=true
rf:step:scan-stop:passed:stopCommandId=9003:scanSessionId=9004:requested=true
rf:step:silence:passed:scanSessionId=9004:observationMs=2000
rf:step:connecting:passed:commandId=9005:connectionId=9007:peripheralId=0:serviceUuid=fd000000-0000-4000-8000-000000000000
rf:step:connected:passed:commandId=9005:connectionId=9007:peripheralId=0:serviceUuid=fd000000-0000-4000-8000-000000000000
rf:step:gatt-discovery:passed:commandId=9008:connectionId=9007:peripheralId=0:serviceUuid=fd000000-0000-4000-8000-000000000000:gattSessionId=9009:requestedGattSessionId=9009:serviceCount=1:discoveredServiceCount=2:characteristicCount=5:descriptorCount=2
rf:step:indication-rejection:passed:commandId=9018:connectionId=9007:gattSessionId=9009:subscriptionId=9022:characteristicId=14:error=not_supported
rf:step:indication-enable:passed:commandId=9019:connectionId=9007:gattSessionId=9009:subscriptionId=9021:characteristicId=15:actualMode=Indication
rf:step:indication-value:passed:connectionId=9007:gattSessionId=9009:subscriptionId=9021:characteristicId=15:value=ef
rf:step:indication-disable:passed:commandId=9020:connectionId=9007:gattSessionId=9009:subscriptionId=9021:characteristicId=15:state=Disabled:postDisableSilenceMs=2000
rf:step:disconnected:passed:commandId=9006:connectionId=9007:peripheralId=0:serviceUuid=fd000000-0000-4000-8000-000000000000
rf:step:connection-silence:passed:connectionId=9007:peripheralId=0:serviceUuid=fd000000-0000-4000-8000-000000000000:observationMs=1000
rf:step:close:passed:probe-closed=true:listener-removed=true
rf-indication:passed:serviceUuid=fd000000-0000-4000-8000-000000000000:peripheralId=0:connectionId=9007:connectCommandId=9005:gattCommandId=9008:rejectionCommandId=9018:enableCommandId=9019:disableCommandId=9020:subscriptionId=9021:indicationValue=ef:postDisableSilenceMs=2000:disconnectCommandId=9006:scanSessionId=9004
```

The dual-property rejection used independent rejected subscription `9022` and backend-private characteristic identifier `14`; the successful pure-Indication subscription used `9021` and characteristic identifier `15`. The app observed the same connection, GATT session, subscription, and characteristic correlation through the complete enable/value/disable sequence. The runner printed `React Native Nitro runtime gate passed` and the fixture cleanup printed `nrf52840_fixture=stopped`.

The verified ignored log was promoted to:

```text
tests/consumers/react-native-internal/ios/build/device-rf-indication.log
```

## Validation

Passed during this slice:

```text
pixi run just react-native host-check
pixi run just react-native host-ios-build
pixi run just react-native host-ios-single-image-check
pixi run python -m unittest tools.react_native.test_check_runtime_log tools.react_native.test_run_ios_rf_scan
pixi run ruff check tools/react_native/run_ios_device_runtime.py tools/react_native/run_ios_rf_scan.py tools/react_native/check_runtime_log.py tools/react_native/test_check_runtime_log.py tools/react_native/test_run_ios_rf_scan.py tests/peripherals/nrf52840/direct_fixture.py
pixi run just docs-check-mermaid
pixi run just react-native host-ios-device-rf-indication 00008110-000C61160C39401E FWVYQ287W5
```

The checker/runner regression suite passed 18 tests. The iOS simulator host build and single-image symbol gate passed before the physical run. The physical run passed the independent checker and cleaned up the nRF fixture.

## Boundaries

This pass proves the private RN strict Indication projection on one physical iPhone-to-nRF path, including strict capability rejection, pure-Indication selection, exact value delivery, disable ordering, stale-value silence, requested disconnect, post-disconnect silence, and fixture cleanup. It does not prove:

- passive disconnect and recovery;
- repeated-cycle or pressure/soak behavior;
- reload/invalidation during an active RF operation;
- Android RN runtime or RF;
- macOS RN, which is archived and not an active host;
- the future BLeng/public React Native API;
- ABI freeze or a Phase 3/5 exit.
