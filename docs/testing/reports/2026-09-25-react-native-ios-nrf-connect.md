# React Native iPhone → nRF52840 connection RF checkpoint

Date: 2026-09-25. Scope: private, unfrozen RN/Nitro low-level consumer, physical iPhone 14 Plus (`00008110-000C61160C39401E`) and standalone nRF52840. This is **not** native Apple HIL, Android RN, BLeng, or a Phase 3 exit result.

## Candidate and path

The signed RN host built with `pixi run just react-native host-ios-device-rf-connect 00008110-000C61160C39401E FWVYQ287W5` passed Nitro generation, TypeScript, Pods and signed iPhone build. The first device launch produced a complete RN RF connection ledger and its checker passed, but the gate command returned failure because the fixture stop controller could not yet verify process-group exit. The nRF marker was then safely cleared after the process exited.

Root-cause investigation found that the RN runner called `provider.direct_start()` **in-process**. We infer that the detached child remained visible to its still-running RN parent while the separate stop process waited for the process group to disappear; the exact transient group members were not captured. The independent short-lived-controller comparison supports this diagnosis. An independent, short-lived provider `direct-start` controller starts the fixture now, retaining the exact PID ownership guard. A direct start→stop control passed. The **same already-signed app** then reran through `pixi run python -m tools.react_native.run_ios_rf_scan 00008110-000C61160C39401E --connect`: the runtime checker passed, the fixture stop returned `nrf52840_fixture=stopped`, and an independent status returned `nrf52840_fixture=stopped`. This second execution is the completed gate; the first is only diagnostic evidence. A subsequent scan-only retry was rejected at app launch with `Locked`, did not enter RF, and still cleaned up the fixture. The connection log remains at ignored `tests/consumers/react-native-internal/ios/build/device-rf-connect.log`; the runner promotes a new attempt to that path **only after checker success**.

The [host RF flow](../../../tests/consumers/react-native-internal/src/App.tsx) calls the [private Nitro spec](../../../integrations/react-native-internal/specs/BleHubProbe.nitro.ts), which forwards through the [Swift adapter](../../../integrations/react-native-internal/ios/HybridBleHubProbe.swift) and production UniFFI `BleHub` to the production Apple/CoreBluetooth backend. The [host checker](../../../tools/react_native/check_runtime_log.py) validates ordered, unique step markers and exact ID/payload correlation. The [runner](../../../tools/react_native/run_ios_rf_scan.py) owns only the fixture it started.

## Observed RN ledger

```text
rf:step:initialize:passed:commandId=9001:adapterState=PoweredOn:scanAuthorization=Authorized
rf:step:scan-start:passed:startCommandId=9002:scanSessionId=9004
rf:step:advertisement:passed:scanSessionId=9004:peripheralId=0:localName=missing:filterServiceUuid=fd000000-0000-4000-8000-000000000000:payloadAdvertisedServices=fd000000-0000-4000-8000-000000000000:payloadContainsFilterUuid=true
rf:step:scan-stop:passed:stopCommandId=9003:scanSessionId=9004:requested=true
rf:step:silence:passed:scanSessionId=9004:observationMs=2000
rf:step:connecting:passed:commandId=9005:connectionId=9007:peripheralId=0:serviceUuid=fd000000-0000-4000-8000-000000000000
rf:step:connected:passed:commandId=9005:connectionId=9007:peripheralId=0:serviceUuid=fd000000-0000-4000-8000-000000000000
rf:step:disconnected:passed:commandId=9006:connectionId=9007:peripheralId=0:serviceUuid=fd000000-0000-4000-8000-000000000000
rf:step:connection-silence:passed:connectionId=9007:peripheralId=0:serviceUuid=fd000000-0000-4000-8000-000000000000:observationMs=1000
rf:listener:removed
rf:close:resolved
rf:step:close:passed:probe-closed=true:listener-removed=true
rf-connect:passed:serviceUuid=fd000000-0000-4000-8000-000000000000:peripheralId=0:connectionId=9007:connectCommandId=9005:disconnectCommandId=9006:scanSessionId=9004
```

`peripheralId=0` is a valid canonical `u64`. `localName=missing` is permitted for this provider. The matched provider filter and the observed advertisement payload both contained FD00 in this run; they are separate evidence. Connect success requires `Connecting`, then `Connected`, then its successful command terminal; requested disconnect requires `Disconnected` with disconnect command ID and the successful terminal. The 2-second scan and 1-second connection silence assertions cover **JS-observed** events in the stated windows, not native queue drain, permanent link loss, or absence of events after close.

## Boundaries

- The passing RF path is scan → connect → logical disconnect only. It does **not** prove GATT discovery, read/write, Notification/Indication, passive loss/recovery, multi-cycle stress, or BLeng.
- The successful signed device build and real RF run are candidate-specific; the public ABI is not frozen. Historical native iPhone→nRF `Connecting` stalls and historical Tauri RF are separate evidence, not this result.
- Android `connectRfPeripheral` / `disconnectRfPeripheral` are intentionally fail-closed without a production Android Central backend. No Android RF is claimed.
- The next RN RF slice can add aggregate GATT discovery and deterministic read/write using the same fixture; it must have a separate ledger and checker, and must not relabel this connection result as a GATT pass.
