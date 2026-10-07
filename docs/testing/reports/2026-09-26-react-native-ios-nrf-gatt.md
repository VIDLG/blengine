# React Native iPhone → nRF52840 aggregate GATT RF checkpoint

Date: 2026-09-26. Scope: the private, unfrozen RN/Nitro low-level consumer, a physical iPhone 14 Plus (`00008110-000C61160C39401E`), and the standalone nRF52840 provider. This is not a BLeng release gate, Android RN RF evidence, or a Phase 3 exit result.

## Candidate and path

The signed RN host was built and installed through:

```text
pixi run just react-native host-ios-device-rf-gatt 00008110-000C61160C39401E FWVYQ287W5
```

The completed RF path was:

```text
RN JavaScript → Nitro HybridObject → Swift adapter → production Stable UniFFI BleHub → production Apple/CoreBluetooth backend → standalone nRF52840 Peripheral
```

The host flow is implemented in [`tests/consumers/react-native-internal/src/App.tsx`](../../../tests/consumers/react-native-internal/src/App.tsx). The Swift adapter is [`integrations/react-native-internal/ios/HybridBleHubProbe.swift`](../../../integrations/react-native-internal/ios/HybridBleHubProbe.swift), and the independent log checker is [`tools/react_native/check_runtime_log.py`](../../../tools/react_native/check_runtime_log.py). The RF runner starts and stops only the nRF fixture that it owns.

An initial attempt reached discovery but rejected the aggregate database because the RN UI incorrectly required the total service count to equal one. The archived Tauri flow selects the canonical service by UUID instead of requiring the aggregate database to contain no other service. RN now applies the same rule: exactly one canonical FD00 service must be present, and that service must contain the exact expected characteristic and descriptor instances. The total number of discovered services remains recorded as evidence.

## Observed RN ledger

The successful run passed the checker and recorded these markers:

```text
rf:step:initialize:passed:commandId=9001:adapterState=PoweredOn:scanAuthorization=Authorized
rf:step:scan-start:passed:startCommandId=9002:scanSessionId=9004
rf:step:advertisement:passed:scanSessionId=9004:peripheralId=0:localName=missing:filterServiceUuid=fd000000-0000-4000-8000-000000000000:payloadAdvertisedServices=fd000000-0000-4000-8000-000000000000:payloadContainsFilterUuid=true
rf:step:scan-stop:passed:stopCommandId=9003:scanSessionId=9004:requested=true
rf:step:silence:passed:scanSessionId=9004:observationMs=2000
rf:step:connecting:passed:commandId=9005:connectionId=9007:peripheralId=0:serviceUuid=fd000000-0000-4000-8000-000000000000
rf:step:connected:passed:commandId=9005:connectionId=9007:peripheralId=0:serviceUuid=fd000000-0000-4000-8000-000000000000
rf:step:gatt-discovery:passed:commandId=9008:connectionId=9007:peripheralId=0:serviceUuid=fd000000-0000-4000-8000-000000000000:gattSessionId=9009:requestedGattSessionId=9009:serviceCount=1:discoveredServiceCount=2:characteristicCount=5:descriptorCount=2
rf:step:disconnected:passed:commandId=9006:connectionId=9007:peripheralId=0:serviceUuid=fd000000-0000-4000-8000-000000000000
rf:step:connection-silence:passed:connectionId=9007:peripheralId=0:serviceUuid=fd000000-0000-4000-8000-000000000000:observationMs=1000
rf:step:close:passed:probe-closed=true:listener-removed=true
rf-gatt:passed:serviceUuid=fd000000-0000-4000-8000-000000000000:peripheralId=0:connectionId=9007:connectCommandId=9005:gattCommandId=9008:disconnectCommandId=9006:scanSessionId=9004
```

The canonical target service was `fd000000-0000-4000-8000-000000000000`. Its five characteristic instances were validated by UUID and properties:

- `fff10000-0000-4000-8000-000000000000`: read, write with response, write without response;
- `fff20000-0000-4000-8000-000000000000`: read, Notify, Indicate, one CCCD;
- `fff50000-0000-4000-8000-000000000000`: read, Indicate, one CCCD;
- `fff30000-0000-4000-8000-000000000000`: read;
- `fff40000-0000-4000-8000-000000000000`: write with response, write without response.

The aggregate native database exposed two services in total. The current marker intentionally records the extra service count but does not claim the identity or semantics of that additional service; the gate claims only that the advertised canonical service was unique and complete. The target service contained two descriptors, both mapped to the expected CCCD UUID.

`peripheralId=0` is a valid canonical unsigned `u64`, and `localName=missing` is permitted by this provider profile. The 2-second scan silence and 1-second post-disconnect silence are JS-observed windows.

## Validation

Passed during this slice:

```text
pixi run just react-native host-check
pixi run ruff check tools/react_native/run_ios_device_runtime.py tools/react_native/check_runtime_log.py tools/react_native/test_check_runtime_log.py
pixi run python -m unittest tools.react_native.test_check_runtime_log tools.react_native.test_run_ios_rf_scan
pixi run just react-native host-ios-device-rf-gatt 00008110-000C61160C39401E FWVYQ287W5
```

The RF command printed `React Native Nitro runtime gate passed`, and the provider cleanup printed `nrf52840_fixture=stopped`.

The device runner now streams the attempt log, prints only bounded runtime step markers, stops after the final marker or a short close-summary grace window, and preserves the full attempt log for checker diagnosis. It no longer waits silently for the full 210-second console bound after the RN flow has completed.

## Boundaries

This pass proves the private RN low-level aggregate GATT projection and the scan/connect/disconnect path on this candidate. It does not prove:

- characteristic read;
- write with response or write without response;
- Notification or strict Indication;
- RSSI capability behavior;
- Peripheral-initiated passive disconnect and recovery;
- repeated-cycle or pressure/soak behavior;
- Android RN RF;
- the future BLeng/public React Native API;
- ABI freeze or a Phase 3 exit.

Those slices remain separate gates so a later failure can be attributed to I/O, subscription, recovery, or lifecycle behavior rather than to the already-validated aggregate discovery path.
