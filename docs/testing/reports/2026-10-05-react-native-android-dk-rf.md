# React Native Android 实体 runtime 与双 nRF52840 DK RF — 2026-10-05

## 范围

本轮验证的是私有、`private: true` 的 RN/Nitro low-level integration，不是 public BLeng/RN API，也不构成 ABI freeze。链路为：

```text
RN JS → Nitro HybridObject → Kotlin adapter → Stable UniFFI production image
→ Rust BleHub → production AndroidBleBackend → Android Bluetooth stack
→ standalone nRF52840 DK
```

Android Central 为 OnePlus 11，ADB serial 为 `3ac5cc09`。所有 APK 安装均使用项目的 root-aware `adb push + su -c pm install` runner；没有使用 `adb install`。

## 实体 Android host gates

以下 recipe 均通过，并在本地 device log 中留下结构化 marker：

```text
pixi run just react-native host-android-lifecycle-stress 3ac5cc09
pixi run just react-native host-android-runtime-reload 3ac5cc09
pixi run just react-native host-android-runtime 3ac5cc09
```

结果：

- Nitro typecheck、Nitrogen Kotlin/Swift/C++ generation 和 RN CLI autolinking 通过；
- release host build 通过；
- `android_apk_install_root=true`、`android_apk_install=passed`；
- basic production runtime `close:resolved` 通过；
- lifecycle stress `stress:complete:passed` 通过，覆盖 close/recreate、listener removal、late callback 和 post-close dispatch；
- `ReactDelegate.reload()` generation 1/2 recreate 与 cleanup 通过。

日志：

```text
tests/consumers/react-native-internal/android/build/runtime.log
tests/consumers/react-native-internal/android/build/lifecycle-stress.log
tests/consumers/react-native-internal/android/build/runtime-reload.log
```

## Clean artifact consumer

以下两个 recipe 均通过：

```text
pixi run just react-native host-android-artifact-consumer
pixi run just react-native host-android-artifact-device-lifecycle 3ac5cc09
```

静态 consumer 从 npm tarball 和 staged Maven repository 组装独立 RN host，通过 RN CLI autolinking、clean Gradle build、release APK single-image 检查。实体 artifact consumer 在 OnePlus 11 上使用同一 root-aware 安装路径，通过 lifecycle stress。结果为：

```text
react_native_android_artifact_consumer=static-passed
react_native_android_artifact_consumer=device-lifecycle-passed
```

本轮 artifact work tree 保留在 `.work/react-native-android-artifact-consumer/run-89c16e2f9e1b48d6883cb599a551ccc1`，可由项目的 `work-clean` recipe 回收。

## 单 DK RF

使用 canonical service UUID：

```text
fd000000-0000-4000-8000-000000000000
```

通过的模式：

```text
rf
rf-connect
rf-gatt
rf-io
rf-rssi
rf-notification
rf-indication
rf-passive-disconnect
```

覆盖结果：

- filter scan、scan stop 和 advertisement mapping；
- connect/disconnect；
- aggregate GATT discovery；
- initial read、write-with-response、write-without-response 和 readback；
- connected RSSI，本轮观测为 `-56 dBm`；
- PlatformDefault Notification，精确 `ef` value、disable 后 2 秒静默；
- strict Indication，unsupported combination rejection、实际 Indication、精确 `ef` value、disable 后 2 秒静默；
- passive disconnect 使用 `commandId=none`，并完成 recovery connection。

对应日志：

```text
tests/consumers/react-native-internal/android/build/rf-runtime.log
tests/consumers/react-native-internal/android/build/rf-connect-runtime.log
tests/consumers/react-native-internal/android/build/rf-gatt-runtime.log
tests/consumers/react-native-internal/android/build/rf-io-runtime.log
tests/consumers/react-native-internal/android/build/rf-rssi-runtime.log
tests/consumers/react-native-internal/android/build/rf-notification-runtime.log
tests/consumers/react-native-internal/android/build/rf-indication-runtime.log
tests/consumers/react-native-internal/android/build/rf-passive-disconnect-runtime.log
```

## 双 DK RF

双 DK runner 独立启动并确认：

```text
dk_a → blehub-nrf-indication-v2.json
dk_b → blehub-nrf-indication-v2-b.json
```

两块 DK 使用同一份 BTP/AutoPTS-compatible Tester firmware；profile、service UUID、local name、CDC/J-Link identity 和 BTP session 由 host fixture 配置区分，不需要为第二块 DK 分叉固件。

通过：

```text
pixi run just react-native host-android-rf-multi 3ac5cc09
pixi run just react-native host-android-rf-multi-soak 3ac5cc09
pixi run just react-native host-android-rf-multi-fault 3ac5cc09
```

结果：

- 一轮双设备交错连接/GATT/Notification flow：`rf-multi:passed`；
- 同一 RN process/probe 的 Notification 10-cycle soak：10/10 cycle passed；
- destructive fault：停止 DK-A 后 DK-B 保持可用，DK-A readiness 恢复并重新连接/GATT 路由通过；
- 每个阶段均由 bounded progress marker 结束，fixture 在结束后清理。

日志：

```text
tests/consumers/react-native-internal/android/build/rf-multi-runtime-cycle-1.log
tests/consumers/react-native-internal/android/build/rf-multi-soak-runtime.log
tests/consumers/react-native-internal/android/build/rf-multi-fault-runtime.log
```

## 边界

本报告不宣称：

- public BLeng/RN API 已建立或通过；
- public package clean consumer 已通过；
- 双 component production image 已冻结；
- iOS 本轮重新执行；
- 长时间多小时 soak；
- Android controller 在任意设备上的无限并发 subscription 能力。

本轮证明的是当前 private RN/Nitro Android low-level bridge、production Android backend、单 DK 功能矩阵和双 DK isolation/recovery 流程。Android 与 iOS 的 shared JS/fixture flow 可以共享，但平台 callback 能力仍以各自独立 RF evidence 为准。
