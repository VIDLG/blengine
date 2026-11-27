# iOS system-loss fresh-owner platform-timeout

Date: 2026-10-31

Status: **system-loss gate not passed; platform-timeout recorded.** The phone was restored ON, but iOS/CoreBluetooth did not deliver a fresh advertisement callback after the fresh-owner attempt.

## Candidate

```text
run_id=1caa0473-c387-495c-adf6-3099fbcdf82e
case=response-write-queued-ccc-enable
peripheral=multifunction-rf-v2-b
image_sha256=f101a5c4b13b2c869848edd6f0bd563543ade85728cd9f3fa6ffce71430e96f5
iPhone=00008110-000C61160C39401E
```

The maintained existing `BlengAppleIOSProbe` App and signed UI-test target were used. The run used the real DK-B provider, device relay, iOS Settings UI and independent nRF52840 sniffer. No synthetic advertisement, connection, GATT, subscription or notification was accepted.

## Observed evidence

The run completed the pre-recovery portions:

- signed App installation and UI automation preflight;
- one Bluetooth OFF action and verified OFF readback;
- retained provider `disconnected-uncommitted` receipt;
- two real old-operation terminal records (`watchdog-timeout`);
- one Bluetooth ON restoration and verified ON readback;
- old-owner native resource snapshot at zero;
- fresh owner/backend creation with changed owner/backend identity;
- fresh scan using a fresh scan session, so the old `Peripheral` identity was not reused;
- independent sniffer capture showing the Peripheral advertisement after restoration.

The App diagnostic recorded:

```text
central_recreation_attempted=true
central_recreation.recreated=true
central_recreation.state=CBManagerState(rawValue: 5)
fresh_owner_attempted=true
recovery_scan_id=6
matching_advertisements=0
recovery_failure_reason=no-advertisement-callback-after-fresh-owner
resources.activeScans=0
resources.connections=0
resources.gattAttributes=0
resources.subscriptions=0
resources.pendingIo=0
resources.watchdogs=0
resources.callbackRoutes=0
resources.ingressRoots=0
resources.adapterDelegates=0
resources.backendLeases=0
```

Raw evidence remains under the ignored attempt directory:

```text
.work/apple-bleng-ios-system-loss/1caa0473-c387-495c-adf6-3099fbcdf82e/
```

The corresponding sniffer PCAP is:

```text
.work/apple-bleng-ios-system-loss/20261031-response-write-queued-ccc-enable-fresh-evidence.pcap
```

## Interpretation

This is not a recovery pass. The sniffer proves that the Peripheral was advertising; it does not prove that CoreBluetooth delivered an advertisement callback to the fresh native owner. The current boundary is therefore iOS/CoreBluetooth advertisement delivery after a real radio OFF/ON transition, not the nRF52840 image, provider binding, host relay, stale peripheral identity, or native resource retirement.

The candidate must not be repeated merely to reconfirm the same missing callback. A reconnect/resubscribe workaround, if investigated later, requires a separately authorized candidate with fresh evidence and must not be inferred from this run.

## Restoration-only cleanup

After the candidate, the original native lease was not deleted or adopted. A fresh RN Settings inspection-only run verified the Bluetooth switch ON on the same physical device, and the original quarantined owner/journal bytes were archived through the restoration-only lease protocol:

```text
.work/ios-radio-restoration/native-system-loss-1caa0473-20261031-lease-archive/restoration-release.json
scope=ios-native-system-loss-restoration-only
original_journal_status=quarantined
inspected_switch_value=1
rf_gate_passed=false
```

The phone lock is released. This archive is cleanup evidence only; it does not alter the candidate verdict or prove BLE recovery.

## Implementation follow-up

The recovery protocol now preserves this distinction end to end:

- the App diagnostic exports `recovery_outcome=platform-timeout` with the failure reason;
- the host validates session, control, witness and owner identity before accepting the diagnostic;
- the host records `platform-timeout` as a terminal non-pass state after verified ON restoration;
- the iOS phone lease can be archived as safely restored instead of being misclassified as an unresolved quarantine;
- `finish` remains unavailable, so the system-loss BLE gate cannot be reported as passed.

The shared Rust `bleng-recovery` policy already exposes `PlatformTimeout` and `ManualReconnectRequired` as distinct outcomes. No Stable UniFFI recovery ABI or `crates/bleng` recovery path is added by this change.
