# React Native multi-DK static and readiness checkpoint

**Date:** 2026-10-03
**Scope:** private React Native/Nitro multi-device infrastructure
**Status:** static gate, readiness, and two-DK fault RF passed; iOS two-DK subscription isolation passed; Android two-DK subscription isolation remains an explicit failed/blocked boundary for the tested OnePlus controller

## Changes validated

- The iOS device runtime controller accepts `rf-multi` and forwards the two service UUIDs to the RN host.
- A dedicated iOS multi-DK runner now shares `MultiFixtureController`, the JS `rf-multi` flow, and `check_runtime_log` with Android.
- The shared JS flow now keeps both connections active while interleaving, per device:
  - initial characteristic read;
  - write with response;
  - readback.
- Multi-DK subscription coordination now uses one BTP owner/session per DK: the runner triggers each board's `EF` value only after its own enable marker and releases that board after its own disable marker.
- The checker requires six I/O entries with exact Peripheral, connection, GATT session, characteristic, command, operation, and value correlation, plus two Notification enable/value/disable entries with exact subscription-route correlation and post-disable silence.
- Failure runs retain the multi-fixture logs; successful runs remove the temporary fixture directory.
- The private RN README and approved plan now describe the two-DK boundary.

## Static validation

Passed:

- nRF fixture/provider/multi-device and RN multi-ledger tests: 33 tests before the subscription-coordination increment; 14 focused controller/checker tests after it;
- Ruff for the changed Python files;
- RN TypeScript and Nitro code generation;
- RN iOS CocoaPods installation and signed device build;
- Android RN release host build, root-aware install, and CLI autolinking;
- `pixi run just lint`;
- `pixi run just docs-check-mermaid`;
- `git diff --check` except for an existing unrelated CRLF normalization warning in `just/binding/mod.just`.

The iOS Swift language server may report `UIKit` as missing when opened outside the Xcode SDK context. The same AppDelegate compiled successfully in the Xcode Simulator build; this is an editor SDK-resolution diagnostic, not a build failure.

## Hardware readiness and RF evidence

The ignored local configuration maps both boards by full USB serial, CDC port, and distinct profile. CDC/serial mapping passed. Both DKs now run the same BTP/AutoPTS-compatible Tester firmware image; the verified HEX SHA-256 is:

```text
0dd62fc15ab44aed01325d7cfea055db82014df1f94da77844f10c34ec16cefe
```

The bounded two-board readiness command passed for both devices:

```text
nrf52840_multi_readiness=passed devices=2
```

The controller owns one BTP session/process group per DK. `stop_device("dk_a")` now lets the direct fixture disconnect active peers through that same BTP owner before stopping, so a host-triggered DK-A fault produces a real Central passive-disconnect event without opening a second serial owner. Restarting DK-A re-provisions its configured profile and returns it to readiness; DK-B remains owned and running throughout.

The private RN/Nitro multi-device fault/reconnect flow passed on both Centrals:

- **Android:** OnePlus 11 `3ac5cc09`; two advertisements, two connections/GATT sessions, DK-A passive disconnect, DK-B read `00` while A was down, DK-A restart, fresh scan/connect/GATT/readback, and final close/checker passed.
- **iOS:** iPhone 14 Plus `00008110-000C61160C39401E`; the same sequence passed through the production Apple backend and signed RN host. The iOS runner accepts the normal `connected`/`available` devicectl states and the shared flow waits only for cross-platform final `Disconnected` semantics, not an Android-only `Disconnecting` intermediate event.

The follow-up two-DK subscription flow passed on iOS: both routes enabled `Notification`, each received exact `EF` under its own connection/GATT/subscription/characteristic IDs, and each disable was followed by route-specific silence. The same Android flow did not pass on OnePlus 11: both CCCD enables completed and both nRF BTP sessions reported `value-applied`, but only the first Android route produced `NotificationReceived`; the second route produced no native value callback before the bounded timeout. The failure was retained under the Android build fixture directory. A single-DK Android Notification run against DK-A passed earlier; a temporary DK-B single-run attempt was rejected by the existing single-provider profile mismatch and was not classified as RF evidence.

## Interpretation and next action

This is a real two-Peripheral RF pass for the private low-level RN/Nitro integration, not a public BLeng/RN API or ABI-freeze result. It proves shared JS flow, Nitro bridge, platform backend, Rust BleHub contract, event correlation, fixture ownership, and per-device route isolation for the exercised scan/connect/GATT/read path. iOS additionally proves concurrent two-DK Notification route isolation. Android concurrent two-DK Notification delivery remains unresolved for the tested OnePlus controller; it must not be promoted to a cross-platform pass. The report does not yet claim two-DK Indication route isolation, multi-device RSSI, stress/soak, Android reload/invalidation, or public package acceptance.

The next RF increment is to isolate and resolve the Android second-route Notification boundary with a platform/firmware/profile-specific experiment, without changing the shared contract or masking the missing callback. After that, run the same two-DK Indication route flow on both platforms, then multi-device RSSI and soak/reload gates.
