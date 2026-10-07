# React Native iPhone -> nRF52840 repeated recovery RF checkpoint

Date: 2026-09-27. Scope: the private, unfrozen RN/Nitro low-level consumer, a physical iPhone 14 Plus (`00008110-000C61160C39401E`), and the standalone nRF52840 provider. This is not a BLeng release gate, Android RN RF evidence, a macOS RN gate, or a Phase 3/5 exit result.

## Candidate and path

The signed RN host was built, installed, and exercised with:

```text
pixi run just react-native host-ios-device-rf-recovery 00008110-000C61160C39401E FWVYQ287W5
```

The completed production path was:

```text
RN JavaScript -> Nitro HybridObject -> Swift adapter -> production Stable UniFFI BleHub -> production Apple/CoreBluetooth backend -> standalone nRF52840 Peripheral
```

The cycle implementation is in [`tests/consumers/react-native-internal/src/App.tsx`](../../../tests/consumers/react-native-internal/src/App.tsx). The iOS launch-mode mapping is in [`tests/consumers/react-native-internal/ios/BleHubNitroHost/AppDelegate.swift`](../../../tests/consumers/react-native-internal/ios/BleHubNitroHost/AppDelegate.swift). The host runner and checker are [`tools/react_native/run_ios_rf_scan.py`](../../../tools/react_native/run_ios_rf_scan.py) and [`tools/react_native/check_runtime_log.py`](../../../tools/react_native/check_runtime_log.py). Fixture coordination is implemented in [`tests/peripherals/nrf52840/direct_fixture.py`](../../../tests/peripherals/nrf52840/direct_fixture.py).

The private Nitro projection reuses the production connection, GATT, and subscription event model. It does not introduce a second BLE contract, native image, or public React Native API.

## Three-cycle protocol

One RN probe and one event listener remain active for all three cycles. Each cycle uses a disjoint caller-owned ID range:

```text
cycle 0: 10000-10012
cycle 1: 10100-10112
cycle 2: 10200-10212
```

Every cycle completed this ordered flow:

```text
scan/filter/stop/post-stop silence
  -> connect / Connecting / Connected
  -> aggregate GATT discovery
  -> Notification enable / exact EF value
  -> fixture-triggered passive disconnect
  -> exact passive connection-error projection and old-route silence
  -> reconnect retained peripheral with fresh IDs
  -> explicit recovery disconnect / terminal silence
```

The passive disconnect assertion is deliberately narrow: `commandId` is absent, `connectionState` is absent, and the projected error category is `connection`. Other connection errors remain fail-closed. The post-disconnect silence windows begin after the relevant passive or explicit terminal event, so a valid recovery terminal event is not mistaken for a late callback.

The fixture uses per-cycle filesystem ACKs and drains the nRF GAP disconnect event before advancing. It is owned by the RN runner and was confirmed stopped after the run.

## Observed RF ledger

The independent checker accepted exactly 44 lines in the ordered runtime ledger: listener registration and initialization, 3 cycles with 11 markers each, close, and the final `cycles=3` result. The successful run recorded:

```text
cycle 0: scanSessionId=10001 connectionId=10004 gattSessionId=10006 subscriptionId=10008 recoveryConnectionId=10010 peripheralId=0
cycle 1: scanSessionId=10101 connectionId=10104 gattSessionId=10106 subscriptionId=10108 recoveryConnectionId=10110 peripheralId=0
cycle 2: scanSessionId=10201 connectionId=10204 gattSessionId=10206 subscriptionId=10208 recoveryConnectionId=10210 peripheralId=0
```

Each cycle reported the exact Notification value `ef`, one passive disconnect with no command ID, one old-route silence observation of 1,000 ms, and one explicit recovery-disconnect silence observation of 1,000 ms. Scan post-stop silence was 2,000 ms per cycle. The final runtime markers were:

```text
BLEHUB_NITRO_RUNTIME rf-recovery:close:passed:listener-removed=true:probe-closed=true
BLEHUB_NITRO_RUNTIME rf-recovery:passed:cycles=3
```

The independent checker printed `React Native Nitro runtime gate passed`, and provider status after cleanup was:

```text
nrf52840_fixture=stopped
```

The verified ignored log is:

```text
tests/consumers/react-native-internal/ios/build/device-rf-recovery.log
```

## Validation

Passed for this slice:

```text
pixi run python tools/react_native/check_runtime_log.py tests/consumers/react-native-internal/ios/build/device-rf-recovery.log --rf-recovery-service-uuid fd000000-0000-4000-8000-000000000000
pixi run python -m unittest tools.react_native.test_check_runtime_log tools.react_native.test_run_ios_rf_scan
pixi run ruff check tools/react_native tests/peripherals/nrf52840
pixi run just lint
```

The recovery runner and checker tests passed. The runner test suite currently emits an expected mocked cleanup warning from its PID-ownership regression case, but the suite result is `OK`; no fixture is left running. The physical RF run and its independent checker both passed before this report was written.

## Boundaries

This pass proves three sequential private RN iOS production-path passive fault/recovery cycles with one probe/listener, active Notification routes, fresh caller-owned IDs, stale-route silence, retained-peripheral recovery, explicit disconnect, and cleanup. It does not prove:

- reload/invalidation during an active RF operation;
- stress/soak or concurrent multi-peripheral behavior;
- Android RN runtime or RF;
- macOS RN, which is archived and not an active host;
- the future BLeng/public React Native API;
- ABI freeze or a Phase 3/5 exit.
