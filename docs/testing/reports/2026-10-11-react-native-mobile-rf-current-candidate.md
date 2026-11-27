# React Native mobile RF and clean-artifact gates — 2026-10-11

## Scope and candidate identity

This report records the current, unfrozen private React Native/Nitro candidate after the Android and Apple native backend updates. It is evidence for the maintained mobile integration lane only. It is not a public BLeng API gate, a public React Native package release, or an ABI-freeze/Phase 5 result.

The tested path was:

```text
RN JavaScript → Nitro HybridObject → platform adapter → production Stable UniFFI image
→ Rust BleHub → production AndroidBleBackend / AppleBleBackend → native BLE stack
→ standalone nRF52840 DK
```

The workspace had uncommitted changes during execution; no commit hash is claimed. The physical devices were:

| Platform | Central | Identity |
|---|---|---|
| Android | OnePlus 11 / PHB110 | ADB serial `3ac5cc09` |
| iOS | iPhone 14 Plus | UDID `00008110-000C61160C39401E`, team `FWVYQ287W5` |

The Android device is a production build. `adb root` is not available, but the project runner used the configured root-aware path (`adb push` plus `su -c pm install`). This is an installation detail, not a claim that `adbd` itself runs as root.

## Android RN/Nitro RF

The Android host used the canonical service UUID `fd000000-0000-4000-8000-000000000000`. The single-DK runs passed:

```text
host-android-rf
rf-connect
rf-gatt
rf-io
rf-rssi
rf-notification
rf-indication
rf-passive-disconnect
```

The evidence covers:

- `PoweredOn` initialization and provider-filtered scan/advertisement mapping;
- scan stop and 2,000 ms post-stop silence;
- connect/disconnect;
- aggregate GATT discovery;
- initial read (`00`), write-with-response/readback (`10`), and write-without-response local submission (`ab`) followed by an independent readback (`ab`);
- connected RSSI, most recently approximately `-49 dBm`;
- PlatformDefault Notification with exact `ef` value and 2,000 ms post-disable silence;
- strict Indication, including unsupported-request rejection, actual Indication mode, exact `ef` value, and post-disable silence;
- passive disconnect with `commandId=none` and a successful recovery connection.

The Android multi-device and recovery runs also passed:

```text
run_android_multi_rf --cycles 1
run_android_multi_rf --runtime-mode rf-multi-soak --soak-cycles 10
run_android_multi_fault_rf
```

They covered two independent nRF profiles/routes, two connections in one RN process, per-route GATT/I/O/RSSI/Notification correlation, ten continuous cycles, and a destructive fault in which DK-A was stopped while DK-B survived, followed by DK-A readiness and reconnection recovery. The final fault marker was:

```text
rf-multi-fault:passed:passiveFault=dk_a:bSurvived=true:aReconnected=true
```

Android runtime 日志保存在 `tests/consumers/react-native-internal/android/build/`，包括 `rf-runtime.log`、`rf-connect-runtime.log`、`rf-gatt-runtime.log`、`rf-io-runtime.log`、`rf-rssi-runtime.log`、`rf-notification-runtime.log`、`rf-indication-runtime.log`、`rf-passive-disconnect-runtime.log`、`rf-multi-runtime-cycle-1.log`、`rf-multi-soak-runtime.log` 与 `rf-multi-fault-runtime.log`。

## RN 页面内 RF Lab UI 入口验证

新增 `runtimeMode=rf-lab` 后，Android release APK 在 OnePlus 11 上实际启动了 RF Lab 页面。UIAutomator 确认了 `rf-lab-root`、Scan/Connect/GATT/I/O/RSSI/Notification/Strict Indication/Passive recovery 选择按钮、step coverage 和 projected event 区域。启动本次页面验证专用的 nRF fixture 后，从页面点击 `Run Scan`，页面显示 RF coverage `6/6`，并记录了 `PoweredOn + Authorized`、`scanStarted`、匹配广播、`scanStopped`、停止后静默和 close steps；fixture 随后通过 ownership-aware stop 正常停止。

这证明了新增页面入口和页面驱动的单 Peripheral Scan 流程，不扩大已有 automated host recipe/checker 的 Android 全矩阵结论，也不替代 checker 作为正式 RF 验收权威。multi-DK、destructive fault、reload 和 pending-read reload 仍由 host 控制。

## iOS RN/Nitro RF

The iOS host used the same canonical nRF profile and passed:

```text
run_ios_rf_scan --connect
run_ios_rf_scan --gatt
run_ios_rf_scan --io
run_ios_rf_scan --rssi
run_ios_rf_scan --notification
run_ios_rf_scan --indication
run_ios_rf_scan --passive-disconnect
```

The single-device evidence covers:

- `PoweredOn` and `Authorized` initialization;
- filtered scan, advertisement mapping, scan stop, and 2,000 ms post-stop silence;
- connect/disconnect;
- aggregate GATT discovery with one service, five characteristics, and two descriptors;
- initial read (`00`), write-with-response/readback (`10`), and write-without-response local submission (`ab`) followed by independent readback (`ab`);
- connected RSSI, observed as approximately `-57 dBm`;
- Notification with exact `ef` value and 2,000 ms post-disable silence;
- strict Indication with unsupported-request rejection, actual Indication mode, exact `ef` value, and post-disable silence;
- passive disconnect with `commandId=none`, old-route silence, recovery connection, and recovery disconnect.

The iOS multi-device runs passed:

```text
run_ios_multi_rf --cycles 1
run_ios_multi_rf --runtime-mode rf-multi-soak --soak-cycles 10
run_ios_multi_fault_rf
```

The multi-device run correlated two service routes, two connections, GATT discovery, read/write/readback, RSSI, and Notification independently. The ten-cycle run ended with:

```text
rf-multi-soak:close:passed:probe-closed=true
rf-multi-soak:passed:cycles=10
```

The destructive run ended with:

```text
rf-multi-fault:passed:passiveFault=dk_a:bSurvived=true:aReconnected=true
```

The retained iOS runtime logs are under `tests/consumers/react-native-internal/ios/build/`, including `device-rf-scan.log`, `device-rf-connect.log`, `device-rf-gatt.log`, `device-rf-io.log`, `device-rf-rssi.log`, `device-rf-notification.log`, `device-rf-indication.log`, `device-rf-passive-disconnect.log`, `device-rf-multi-cycle-1.log`, `device-rf-multi-soak.log`, and `device-rf-multi-fault.log`.

## Clean artifact consumers

After the physical-device functional gates, the two artifact-only consumers passed:

```text
pixi run just react-native host-android-artifact-consumer
pixi run just react-native host-ios-artifact-consumer
```

Android reported:

```text
artifact_fingerprint=android-production:fresh
react_native_android_artifact_consumer=static-passed
```

The isolated Android consumer was assembled from the packed npm integration and the published Maven artifacts, then passed the single-image check. Its work tree was:

```text
.work/react-native-android-artifact-consumer/run-1410fa5ead9a4c468fa2dc32bfab3adf
```

iOS initially reported a stale production fingerprint, so the recipe rebuilt the staged Apple package before consuming it. The rebuilt package passed component-symbol and XCFramework-slice checks, and the isolated npm-tarball consumer passed unsigned simulator/device builds and single-image checks:

```text
artifact_fingerprint=apple-production:stale
apple_uniffi_component_symbols=passed
apple_xcframework_slices=passed
react_native_ios_artifact_consumer=static-passed
```

The iOS work tree was:

```text
.work/react-native-ios-artifact-consumer/run-a258ed58f7414997a953d027b0053754
```

The stale Android contract incident was fixed before this run: the RN Android artifact recipe now publishes the current backend AAR, the release publication is rebuilt cleanly with `--rerun-tasks`, the Android/Apple backends read the generated contract version instead of duplicating a literal, and the Android fingerprint includes `platforms/android`. The current Android consumer therefore verified the current contract rather than a stale contract-5 artifact.

## Non-RF validation executed with this candidate

The following checks passed before or during this candidate run:

```text
pixi run just lint
pixi run just --fmt --check
pixi run just docs-check-mermaid
git diff --check
pixi run python -m unittest tools.react_native.test_run_android_rf
pixi run -e android gradle -p platforms/android :library:test --no-daemon
pixi run python -m unittest tools.test_package_bleng_android tools.react_native.test_android_artifact_consumer
```

The RN artifact consumers also ran Nitro TypeScript checking, Nitrogen generation, RN CLI autolinking, clean dependency installation, and their platform-specific build/single-image checks.

## Limits

This report does not claim:

- a public BLeng or public React Native API;
- native cancellation exposed through the RN probe; the current private RN spec does not provide a complete user-facing cancellation matrix;
- public package publication or ABI freeze;
- multi-hour soak or arbitrary-device Android concurrency;
- Windows support, which is archived compatibility material;
- that Android RF proves iOS RF, or that either RN result proves a public BLeng result.

The result is a current private RN/Nitro mobile functional, recovery, and clean-artifact checkpoint for both maintained mobile platforms.
