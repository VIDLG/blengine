# React Native iPhone -> nRF52840 GATT characteristic I/O RF checkpoint

Date: 2026-09-26. Scope: the private, unfrozen RN/Nitro low-level consumer, a physical iPhone 14 Plus (`00008110-000C61160C39401E`), and the standalone nRF52840 provider. This is not a BLeng release gate, Android RN RF evidence, or a Phase 3 exit result.

## Candidate and path

The signed RN host was built, installed, and exercised through:

```text
pixi run just react-native host-ios-device-rf-io 00008110-000C61160C39401E FWVYQ287W5
```

The completed path was:

```text
RN JavaScript -> Nitro HybridObject -> Swift adapter -> production Stable UniFFI BleHub -> production Apple/CoreBluetooth backend -> standalone nRF52840 Peripheral
```

The flow is implemented in [`tests/consumers/react-native-internal/src/App.tsx`](../../../tests/consumers/react-native-internal/src/App.tsx). The Swift adapter is [`integrations/react-native-internal/ios/HybridBleHubProbe.swift`](../../../integrations/react-native-internal/ios/HybridBleHubProbe.swift), and the independent checker is [`tools/react_native/check_runtime_log.py`](../../../tools/react_native/check_runtime_log.py). The RF runner starts and stops only the nRF fixture that it owns.

The static host was also checked before the device run:

```text
pixi run just react-native check
pixi run just react-native host-ios-build
pixi run just react-native host-ios-single-image-check
```

The iOS single-image gate passed for the simulator host. The device build used the same staged production UniFFI package and Apple backend sources.

## Observed RF ledger

The successful run passed both the runtime marker gate and the independent checker. The relevant ledger was:

```text
rf:step:initialize:passed:commandId=9001:adapterState=PoweredOn:scanAuthorization=Authorized
rf:step:scan-start:passed:startCommandId=9002:scanSessionId=9004
rf:step:advertisement:passed:scanSessionId=9004:peripheralId=0:localName=missing:filterServiceUuid=fd000000-0000-4000-8000-000000000000:payloadAdvertisedServices=fd000000-0000-4000-8000-000000000000:payloadContainsFilterUuid=true
rf:step:scan-stop:passed:stopCommandId=9003:scanSessionId=9004:requested=true
rf:step:silence:passed:scanSessionId=9004:observationMs=2000
rf:step:connecting:passed:commandId=9005:connectionId=9007:peripheralId=0:serviceUuid=fd000000-0000-4000-8000-000000000000
rf:step:connected:passed:commandId=9005:connectionId=9007:peripheralId=0:serviceUuid=fd000000-0000-4000-8000-000000000000
rf:step:gatt-discovery:passed:commandId=9008:connectionId=9007:peripheralId=0:serviceUuid=fd000000-0000-4000-8000-000000000000:gattSessionId=9009:requestedGattSessionId=9009:serviceCount=1:discoveredServiceCount=2:characteristicCount=5:descriptorCount=2
rf:step:read-initial:passed:commandId=9010:connectionId=9007:gattSessionId=9009:peripheralId=0:serviceUuid=fd000000-0000-4000-8000-000000000000:characteristicId=13:value=00
rf:step:write-response:passed:commandId=9011:connectionId=9007:gattSessionId=9009:peripheralId=0:serviceUuid=fd000000-0000-4000-8000-000000000000:characteristicId=13:value=10
rf:step:read-response:passed:commandId=9012:connectionId=9007:gattSessionId=9009:peripheralId=0:serviceUuid=fd000000-0000-4000-8000-000000000000:characteristicId=13:value=10
rf:step:write-without-response:passed:commandId=9013:connectionId=9007:gattSessionId=9009:peripheralId=0:serviceUuid=fd000000-0000-4000-8000-000000000000:characteristicId=13:value=ab:terminal=local-submission
rf:step:read-without-response:passed:commandId=9014:connectionId=9007:gattSessionId=9009:peripheralId=0:serviceUuid=fd000000-0000-4000-8000-000000000000:characteristicId=13:value=ab
rf:step:disconnected:passed:commandId=9006:connectionId=9007:peripheralId=0:serviceUuid=fd000000-0000-4000-8000-000000000000
rf:step:connection-silence:passed:connectionId=9007:peripheralId=0:serviceUuid=fd000000-0000-4000-8000-000000000000:observationMs=1000
rf:step:close:passed:probe-closed=true:listener-removed=true
rf-io:passed:serviceUuid=fd000000-0000-4000-8000-000000000000:peripheralId=0:connectionId=9007:connectCommandId=9005:gattCommandId=9008:readCommandId=9010:writeResponseCommandId=9011:writeWithoutResponseCommandId=9013:disconnectCommandId=9006:scanSessionId=9004
```

The canonical target service was `fd000000-0000-4000-8000-000000000000`. Aggregate discovery exposed two services, five characteristic instances, and two descriptors; the selected read/write characteristic was the validated `fff10000-0000-4000-8000-000000000000` instance. Its native backend-private characteristic identifier was `13` in this run.

The observed values were:

| Stage | Command | Observed value | Evidence meaning |
| --- | ---: | --- | --- |
| Initial read | `9010` | `00` | Remote fixture initial state |
| Write with response | `9011` | `10` | Characteristic-written event and successful command completion |
| Readback after write with response | `9012` | `10` | Separate remote fixture state/read evidence |
| Write without response | `9013` | `ab` | Local CoreBluetooth submission terminal only |
| Readback after write without response | `9014` | `ab` | Separate remote fixture state/read evidence |

The write-without-response terminal is deliberately labeled `terminal=local-submission`. It does not claim that CoreBluetooth provides a remote write completion callback. The later `9014` readback is independent Peripheral application evidence that the fixture state contained `AB` when read.

`peripheralId=0` is a valid canonical unsigned `u64`, and `localName=missing` is permitted by this provider profile. The 2-second scan silence and 1-second post-disconnect silence are JS-observed windows. The fixture was stopped by the runner after the successful check.

## Validation

Passed during this slice:

```text
pixi run just react-native check
pixi run just react-native host-check
pixi run just react-native host-ios-build
pixi run just react-native host-ios-single-image-check
pixi run ruff check tools/react_native/run_ios_device_runtime.py tools/react_native/run_ios_rf_scan.py tools/react_native/check_runtime_log.py tools/react_native/test_check_runtime_log.py
pixi run python -m unittest tools.react_native.test_check_runtime_log tools.react_native.test_run_ios_rf_scan
pixi run just react-native host-ios-device-rf-io 00008110-000C61160C39401E FWVYQ287W5
```

The checker regression suite passed 16 tests, including synthetic ordering/correlation checks for the I/O ledger and the local no-response terminal. The final physical run printed `React Native Nitro runtime gate passed` and `nrf52840_fixture=stopped`. The final ignored attempt log is retained at:

```text
tests/consumers/react-native-internal/ios/build/device-rf-io.log
```

## Boundaries

This pass proves the private RN low-level characteristic read/write projection on one physical iPhone-to-nRF path, including scan, connection, aggregate GATT discovery, requested disconnect, and post-disconnect silence. It does not prove:

- Notification enable/value/disable or stale-value silence;
- strict Indication behavior;
- RSSI capability behavior;
- Peripheral-initiated passive disconnect and recovery;
- repeated-cycle or pressure/soak behavior;
- Android RN runtime or RF;
- the future BLeng/public React Native API;
- ABI freeze or a Phase 3 exit.

Those remain separate gates so failures can be attributed to subscription, indication, recovery, lifecycle, or public API design rather than to this completed characteristic I/O slice.
