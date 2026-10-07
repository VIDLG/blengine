# React Native iPhone → nRF52840 scan RF checkpoint

Date: 2026-09-25. This is a scan-only checkpoint for the private, unfrozen RN/Nitro integration, not a Phase 3 exit or a BLeng release gate.

## Scope and evidence

The physical iPhone 14 Plus (`00008110-000C61160C39401E`) ran the RN JavaScript → Nitro → production Stable UniFFI `BleHub` → production Swift/CoreBluetooth backend path against the standalone nRF52840 provider. The [host RF recipe](../../../just/react-native/mod.just) starts the standalone provider, installs the signed host app, launches RF mode, checks the JS runtime ledger, and stops its owned fixture. The [JS flow](../../../tests/consumers/react-native-internal/src/App.tsx) and [Swift adapter](../../../integrations/react-native-internal/ios/HybridBleHubProbe.swift) perform the scan; this is not the native Apple HIL runner.

One actual device run reached `rf:passed`. Before the next attempt, the saved `device-rf-scan.log` was rechecked with the corrected `check_runtime_log.py --rf-service-uuid fd000000-0000-4000-8000-000000000000` and printed `React Native Nitro runtime gate passed`. Its observed ordered markers were:

```text
rf:listener:registered-before-initialize
rf:step:initialize:passed:commandId=9001:adapterState=PoweredOn:scanAuthorization=Authorized
rf:step:scan-start:passed:startCommandId=9002:scanSessionId=9004
rf:step:advertisement:passed:scanSessionId=9004:peripheralId=0:localName=missing:filterServiceUuid=fd000000-0000-4000-8000-000000000000:payloadAdvertisedServices=fd000000-0000-4000-8000-000000000000:payloadContainsFilterUuid=true
rf:step:scan-stop:passed:stopCommandId=9003:scanSessionId=9004:requested=true
rf:step:silence:passed:scanSessionId=9004:observationMs=2000
rf:listener:removed
rf:close:resolved
rf:step:close:passed:probe-closed=true:listener-removed=true
rf:passed:filterServiceUuid=fd000000-0000-4000-8000-000000000000:payloadAdvertisedServices=fd000000-0000-4000-8000-000000000000:payloadContainsFilterUuid=true:scanSessionId=9004:peripheralId=0:startCommandId=9002:stopCommandId=9003
```

`peripheralId=0` is valid: [the contract](../../../crates/blehub/src/contract/ids.rs) defines an unsigned `u64` without a nonzero constraint and [AppleScanController](../../../platforms/apple/Sources/BleHubApple/ScanController.swift) starts its allocator at zero. The initial checker rejected this real result; the corrected [checker](../../../tools/react_native/check_runtime_log.py) accepts canonical `u64` IDs including zero, while the [regression tests](../../../tools/react_native/test_check_runtime_log.py) reject leading zeros, negative and overflowing IDs, and mismatched final IDs.

`localName=missing` is permitted by this provider profile. The native service filter matched FD00; in this particular run the observed advertisement payload also contained FD00. The two-second silence assertion covers **JS-observed events from this scan session after stop**, not a guarantee that all native/Nitro queues were drained.

## Recheck and limitations

Subsequent full `host-ios-device-rf-scan` attempts completed Nitro generation, TypeScript checks, Pods, signed device build, fixture readiness, and app installation, but `devicectl` rejected launch with `Locked`. They did **not** enter RN RF and do not constitute additional RF results. That attempt overwrote the ignored `ios/build/device-rf-scan.log` with the failed-launch diagnostic: the earlier successful raw log is no longer retained locally; the ordered markers above were copied from the terminal output before the retry, and the earlier checker pass is recorded in that same terminal session. After the failed attempt, the provider stop encountered a transient unknown group state and preserved its PID marker; the process then exited, and a verified subsequent provider stop removed the marker (`nrf52840_fixture=stopped`). Earlier `Locked`/`Security` launch refusals likewise are not RF evidence.

Host-only regression tests (`26`), Ruff checks, `pixi run just fmt`, and `pixi run just lint` passed. These results do not establish connection/disconnection, GATT, Notification/Indication, Android RN RF, full engine reload under RF, or the eventual BLeng UI-facing API. The next independently recorded RF slice, RN JS-driven connect/disconnect, has subsequently passed as a separate real-device gate; see the [connection RF checkpoint](2026-09-25-react-native-ios-nrf-connect.md). That newer result does not turn this earlier scan-only execution into connection evidence. GATT discovery and I/O still require their own RN gate. Re-run the scan recipe with the iPhone unlocked if a retained current-run RF log is needed. After this failure, the RF runner was updated to write `device-rf-scan.attempt.log` and promote it to `device-rf-scan.log` only after the checker passes; this log-preservation change has host-only tests but has not yet been exercised on an unlocked device.
