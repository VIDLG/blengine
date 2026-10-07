# Native iPhone to nRF52840 RF completion — 2026-09-28

## Result

The current unfrozen Apple candidate passed the maintained **native Swift/CoreBluetooth iPhone Central to standalone nRF52840 DK Peripheral** RF matrix on the real iPhone 14 Plus. This is native Apple HIL evidence; it is not React Native/Nitro RF evidence and does not relabel macOS RF or the earlier iPhone-to-Android result.

The nRF BTP application serial was verified before the run:

```text
/dev/cu.usbmodem0010502520281
VID:PID=1366:1061
J-Link serial=001050252028
```

The other J-Link CDC port was not selected. The machine-local `blehub.local.toml` now selects the verified application port; no USB identity was added to tracked configuration.

## Environment

| Field | Value |
|---|---|
| Central | Native `BleHubApple` Swift/CoreBluetooth iOS probe |
| iPhone | iPhone 14 Plus, iOS 26.6.1 |
| iPhone UDID | `00008110-000C61160C39401E` |
| Apple team | `FWVYQ287W5` |
| Peripheral | nRF52840 DK standalone Peripheral fixture over USB CDC/BTP |
| Active profile | `blehub-nrf52840-indication-v2` |
| Service | `fd000000-0000-4000-8000-000000000000` |
| RF write vectors | with response `10`; without response `ab` |
| Notification/Indication value | `ef` |

The provider was owned by `tests/peripherals/nrf52840/` and stopped by the recipe trap after each run. The iOS runner used the production Apple backend and production UniFFI package; no RN host or second native image participated.

## Passed scenarios

| Scenario | Run ID | Evidence |
|---|---|---|
| I/O | `dd074a7925544968b73aa6c214b84e56` | service-filtered scan/advertisement/stop silence; connect; aggregate GATT; initial read `00`; write/readback with response; no-response local submission/readback; logical disconnect; ledger and post-dispose silence |
| Notification | `28178fa155de476ebde6bcb460e90498` | actual mode `notification`; exact value `ef`; enable/disable ordering; post-disable silence; logical disconnect; ledger/post-dispose silence |
| Strict Indication | `2903479d3718411aabbac19b9c74975b` | strict Indication mode; indication-only characteristic; dual-property rejection contract; exact value `ef`; disable/post-disable silence; ledger/post-dispose silence |
| Connected RSSI | `58466ee07b344e68b2429381bccd2773` | connected RSSI read `-66`; correlation/terminal; logical disconnect; ledger/post-dispose silence |
| Passive disconnect | `57efe0690dbc413a977847c145416553` | active subscription; nRF Peripheral-initiated disconnect; iOS passive disconnect; no late connection/GATT/value event; recovery-ready and silence |
| Recovery, 3 cycles | `dd4dae06e7b847e58201fd32b6c68b3e` | three independent scan/connect/GATT/Notification/passive-disconnect/re-advertise cycles; cycle ledger; final dispose/post-dispose silence |

Every scenario reported:

```text
apple_ios_corebluetooth_rf=passed
apple_ios_rf_ledger=passed
apple_ios_rf_post_dispose_silence=passed
```

The recovery run additionally reported `apple_ios_rf_recovery_cycle_1=passed`, `..._2=passed`, `..._3=passed`, and `apple_ios_rf_recovery=passed`.

## Infrastructure changes made during this run

- Added verified DK serial selection to the ignored local configuration.
- Added iOS external nRF recipes for Notification, strict Indication, RSSI, passive disconnect, and three-cycle recovery.
- Added bounded iOS app-container ↔ nRF host coordination marker bridging. Probe failure results are checked while waiting, so a failed UI/app result does not wait for the full coordination timeout.
- Added an explicit `recovery-ios` provider coordination mode without changing the existing RN `recovery`/`recovery-ten` modes.
- Corrected shared Apple RF notification ledger assertions to use the profile's configured notification value instead of hardcoded `DE AD BE EF`; the nRF profile value is `ef`.

## Boundaries

This report does not claim:

- React Native/Nitro iOS RF; RN remains a separate internal integration gate.
- macOS native RF, Android Peripheral RF, Windows RF, or iPhone background/restoration behavior.
- ABI freeze or production release readiness.
- Android hardware rerun; no Android device was attached in this session.
- nRF qualification coverage beyond the maintained BTP/profile contract and the scenarios listed above.

The earlier blocked nRF reports remain historical blocked attempts; this report is the current successful iPhone-to-DK candidate evidence.
