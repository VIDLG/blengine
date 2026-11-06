# React Native 移动端 RF 与 clean-artifact 门禁 — 2026-10-11

## 范围与 candidate 身份

本报告记录 Android 与 Apple native backend 更新后的当前、未冻结 React Native/Nitro candidate。证据只属于维护中的移动端私有 integration lane，不是 public BLeng API 门禁、public React Native package 发布结果，也不是 ABI freeze/Phase 5 结果。

本轮实际链路为：

```text
RN JavaScript → Nitro HybridObject → 平台 adapter → production Stable UniFFI image
→ Rust BleHub → production AndroidBleBackend / AppleBleBackend → native BLE stack
→ standalone nRF52840 DK
```

执行期间工作区存在未提交改动，因此不声明 commit hash。真机身份如下：

| 平台 | Central | 身份 |
|---|---|---|
| Android | OnePlus 11 / PHB110 | ADB serial `3ac5cc09` |
| iOS | iPhone 14 Plus | UDID `00008110-000C61160C39401E`，team `FWVYQ287W5` |

Android 是 production build，`adb root` 不可用，但项目 runner 使用了配置好的 root-aware 路径（`adb push` 加 `su -c pm install`）。这只是安装方式说明，不表示 `adbd` 本身运行在 root 下。

## Android RN/Nitro RF

Android host 使用 canonical service UUID：

```text
fd000000-0000-4000-8000-000000000000
```

单 DK 运行通过了：

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

覆盖内容包括：

- `PoweredOn` 初始化与 provider-filtered scan/advertisement mapping；
- scan stop 与停止后 2,000 ms 静默；
- connect/disconnect；
- aggregate GATT discovery；
- 初始 read（`00`）、write-with-response/readback（`10`），以及 write-without-response 的本地 submission（`ab`）后独立 readback（`ab`）；
- connected RSSI，最近一次约为 `-49 dBm`；
- PlatformDefault Notification，精确 `ef` value，disable 后 2,000 ms 静默；
- strict Indication，包括不支持请求拒绝、实际 Indication mode、精确 `ef` value 与 disable 后静默；
- `commandId=none` 的 passive disconnect，以及成功的 recovery connection。

Android 双设备与恢复运行也通过了：

```text
run_android_multi_rf --cycles 1
run_android_multi_rf --runtime-mode rf-multi-soak --soak-cycles 10
run_android_multi_fault_rf
```

覆盖两条独立 nRF profile/route、同一 RN process 中的两条 connection、按 route 分离的 GATT/I/O/RSSI/Notification correlation、连续十个 cycle，以及 destructive fault：停止 DK-A 时 DK-B 继续可用，随后 DK-A readiness 和重连恢复成功。最终 fault marker 为：

```text
rf-multi-fault:passed:passiveFault=dk_a:bSurvived=true:aReconnected=true
```

The retained Android runtime logs are under `tests/consumers/react-native-internal/android/build/`, including `rf-runtime.log`, `rf-connect-runtime.log`, `rf-gatt-runtime.log`, `rf-io-runtime.log`, `rf-rssi-runtime.log`, `rf-notification-runtime.log`, `rf-indication-runtime.log`, `rf-passive-disconnect-runtime.log`, `rf-multi-runtime-cycle-1.log`, `rf-multi-soak-runtime.log`, and `rf-multi-fault-runtime.log`.

## RN RF Lab UI entry validation

After adding `runtimeMode=rf-lab`, the Android release APK was launched on the OnePlus 11. UIAutomator confirmed `rf-lab-root`, the Scan/Connect/GATT/I/O/RSSI/Notification/Strict Indication/Passive recovery selectors, step coverage, and projected-event areas. With a dedicated nRF fixture running, the page's `Run Scan` action completed with RF coverage `6/6` and recorded `PoweredOn + Authorized`, `scanStarted`, the matching advertisement, `scanStopped`, post-stop silence, and close steps. The fixture was then stopped through the ownership-aware stop path.

This validates the new page entry and page-driven single-Peripheral Scan flow. It does not expand the automated host recipe/checker Android full-matrix result, and the checker remains the authoritative RF verdict. Multi-DK, destructive fault, reload, and pending-read reload remain host-controlled.

## iOS RN/Nitro RF

iOS host 通过了：

```text
run_ios_rf_scan --connect
run_ios_rf_scan --gatt
run_ios_rf_scan --io
run_ios_rf_scan --rssi
run_ios_rf_scan --notification
run_ios_rf_scan --indication
run_ios_rf_scan --passive-disconnect
```

单设备证据覆盖：

- `PoweredOn` 与 `Authorized` 初始化；
- filtered scan、advertisement mapping、scan stop 与停止后 2,000 ms 静默；
- connect/disconnect；
- aggregate GATT discovery：1 个 service、5 个 characteristic、2 个 descriptor；
- 初始 read（`00`）、write-with-response/readback（`10`），以及 write-without-response 本地 submission（`ab`）后独立 readback（`ab`）；
- connected RSSI，观测值约为 `-57 dBm`；
- Notification，精确 `ef` value，disable 后 2,000 ms 静默；
- strict Indication，包括不支持请求拒绝、实际 Indication mode、精确 `ef` value 与 disable 后静默；
- `commandId=none` 的 passive disconnect、旧 route 静默、recovery connection 与 recovery disconnect。

iOS 双设备运行通过了：

```text
run_ios_multi_rf --cycles 1
run_ios_multi_rf --runtime-mode rf-multi-soak --soak-cycles 10
run_ios_multi_fault_rf
```

双设备运行独立关联两条 service route、两条 connection、GATT discovery、read/write/readback、RSSI 与 Notification。十周期运行最终包含：

```text
rf-multi-soak:close:passed:probe-closed=true
rf-multi-soak:passed:cycles=10
```

destructive fault 运行最终包含：

```text
rf-multi-fault:passed:passiveFault=dk_a:bSurvived=true:aReconnected=true
```

iOS runtime 日志保存在 `tests/consumers/react-native-internal/ios/build/`，包括 `device-rf-scan.log`、`device-rf-connect.log`、`device-rf-gatt.log`、`device-rf-io.log`、`device-rf-rssi.log`、`device-rf-notification.log`、`device-rf-indication.log`、`device-rf-passive-disconnect.log`、`device-rf-multi-cycle-1.log`、`device-rf-multi-soak.log` 与 `device-rf-multi-fault.log`。

## Clean artifact consumers

完成真机功能门禁后，两个 artifact-only consumer 通过：

```text
pixi run just react-native host-android-artifact-consumer
pixi run just react-native host-ios-artifact-consumer
```

Android 输出为：

```text
artifact_fingerprint=android-production:fresh
react_native_android_artifact_consumer=static-passed
```

隔离 Android consumer 从打包的 npm integration 与已发布 Maven artifacts 组装，通过 RN CLI autolinking、clean Gradle build 与 single-image 检查。其 work tree 为：

```text
.work/react-native-android-artifact-consumer/run-1410fa5ead9a4c468fa2dc32bfab3adf
```

iOS 首次报告 production fingerprint 过期，因此 recipe 按设计重新构建 staged Apple package。重新构建后 component-symbol 与 XCFramework slice 检查通过，隔离的 npm-tarball consumer 通过 unsigned simulator/device build 与 single-image 检查：

```text
artifact_fingerprint=apple-production:stale
apple_uniffi_component_symbols=passed
apple_xcframework_slices=passed
react_native_ios_artifact_consumer=static-passed
```

iOS work tree 为：

```text
.work/react-native-ios-artifact-consumer/run-a258ed58f7414997a953d027b0053754
```

之前的 Android stale contract 问题在本轮前已修复：RN Android artifact recipe 现在会发布当前 backend AAR；release publication 会通过 `--rerun-tasks` 做干净重建；Android/Apple backend 从生成 binding 读取 contract version，不再复制硬编码字面量；Android fingerprint 纳入 `platforms/android`。因此当前 Android consumer 验证的是当前 contract，而不是旧的 contract-5 artifact。

## 本 candidate 执行的非 RF 验证

以下检查在本轮 candidate 前后通过：

```text
pixi run just lint
pixi run just --fmt --check
pixi run just docs-check-mermaid
git diff --check
pixi run python -m unittest tools.react_native.test_run_android_rf
pixi run -e android gradle -p platforms/android :library:test --no-daemon
pixi run python -m unittest tools.test_package_bleng_android tools.react_native.test_android_artifact_consumer
```

RN artifact consumer 还执行了 Nitro TypeScript check、Nitrogen generation、RN CLI autolinking、clean dependency installation，以及平台 build/single-image 检查。

## 边界

本报告不宣称：

- public BLeng 或 public React Native API 已建立或通过；
- RN probe 已暴露并通过 native cancellation；当前私有 RN spec 尚未提供完整的 user-facing cancellation matrix；
- public package 发布或 ABI freeze；
- 多小时 soak 或任意 Android 设备上的无限并发；
- Windows 支持仍在维护中；Windows 当前是 archived compatibility material；
- Android RF 可以证明 iOS RF，或任一 RN 结果可以升级为 public BLeng 结果。

本轮结果是当前 private RN/Nitro mobile candidate 在 Android 与 iOS 两个平台上的功能、恢复和 clean-artifact checkpoint。
