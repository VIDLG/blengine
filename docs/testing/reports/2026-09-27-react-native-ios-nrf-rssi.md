# React Native iPhone -> nRF52840 connected RSSI RF checkpoint

Date: 2026-09-27. Scope: private, unfrozen RN/Nitro consumer, physical iPhone 14 Plus (`00008110-000C61160C39401E`) and standalone nRF52840 Peripheral. This is not Android RN, a public BLeng API, RSSI calibration, or a Phase 3/5 exit result.

## Full recipe

```text
pixi run just react-native host-ios-device-rf-rssi 00008110-000C61160C39401E FWVYQ287W5
```

The signed device host built, installed, launched while unlocked, passed its independent RSSI log checker, stopped the owned fixture, and returned success. The production path was RN JavaScript -> Nitro HybridObject -> Swift adapter -> production Stable UniFFI BleHub -> production Apple/CoreBluetooth backend -> standalone nRF52840 Peripheral. See the [private Nitro projection](../../../integrations/react-native-internal/specs/BleHubProbe.nitro.ts), [Swift adapter](../../../integrations/react-native-internal/ios/HybridBleHubProbe.swift), [JS scenario](../../../tests/consumers/react-native-internal/src/App.tsx), [fixture runner](../../../tools/react_native/run_ios_rf_scan.py), and [independent checker](../../../tools/react_native/check_runtime_log.py).

## Observed ledger

The ignored verified log is `tests/consumers/react-native-internal/ios/build/device-rf-rssi.log`. The checker accepted the ordered, ID-correlated sequence: `PoweredOn + Authorized` initialization, FD00-filtered scan and matching nRF advertisement (`peripheralId=0`), requested scan stop, 2,000 ms post-stop silence, `Connecting` and `Connected` with connect terminal, connected RSSI domain event followed by successful terminal, requested disconnect with terminal, 1,000 ms post-disconnect silence, listener removal, and probe close. The RSSI step and final summary agreed on the value:

```text
rf:step:rssi:passed:commandId=9026:connectionId=9007:peripheralId=0:serviceUuid=fd000000-0000-4000-8000-000000000000:rssiDbm=-40
rf-rssi:passed:serviceUuid=fd000000-0000-4000-8000-000000000000:peripheralId=0:connectionId=9007:connectCommandId=9005:rssiCommandId=9026:rssiDbm=-40:disconnectCommandId=9006:scanSessionId=9004
```

The final log contained 16 runtime markers and no `:failed:` markers. Independent rerun:

```text
pixi run python tools/react_native/check_runtime_log.py tests/consumers/react-native-internal/ios/build/device-rf-rssi.log --rf-rssi-service-uuid fd000000-0000-4000-8000-000000000000
React Native Nitro runtime gate passed: tests/consumers/react-native-internal/ios/build/device-rf-rssi.log
```

The same built iPhone app passed `check_single_image.py ios`; after the recipe `nrf52840_fixture=stopped`, the RN lock and coordination directory were absent, and no matching fixture, runner or `devicectl device process launch` remained. The preceding [locked-device attempt](2026-09-27-react-native-ios-nrf-rssi-blocked.md) was rejected before app launch and remains separate incomplete evidence.

## Limits

The value is CoreBluetooth's connected RSSI result projected through the production contract, not a calibrated distance or a Peripheral-provided synthetic value. This one successful read does not establish repeated RSSI reads, stale callback behavior across passive disconnect/reload, concurrent reads, Android production BLE, or the future public React Native/BLeng API. The Android deterministic probe still fails closed on RF commands; the Android host build and single-image checks are static integration evidence only.
