# Adapter OFF -> ON 正式 App lane（Android 真机）

本文件记录 C 类 #16（`adapter-transition`）从退役 consumer 迁到正式 App 的垂直切片：语义、
两次失败暴露的产品缺陷、根因修复和真机结果。它只记录本轮实际跑过的事实，不把构建通过当作 RF 通过。

## 主张

| 层 | 谁拥有 | 本轮证据形态 |
|---|---|---|
| 手机无线电 | host（`AndroidRadioController`） | `initial=ON` → `off=OFF`（dumpsys 回执）→ App 报 refused scan 后 `restore=ON` |
| 固件镜像与广播 | host（`DirectFixture` + lease + `jlink:1050252028`） | session `344ab127` 的 fixture receipt + passive close receipt |
| 产品事实 | 正式 App 内的 instrumented scenario | stage 序列与 `adapter_unavailable` terminal |

App 侧只断言三件产品事实：

1. 无线电关着时 `start()` 仍然成功，但必须 **如实报告** `POWEREDOFF`（不许假装可用）。
2. 该状态下的 scan 必须以 `adapter_unavailable` 终止，绝不出现“已开始但永远收不到”的扫描。
3. 主机把无线电打开后，App 自己的重试（新 owner）必须拿到 `POWEREDON`，随后 scan → link →
   GATT → read 返回固件 provision 的值。

主机侧只做状态动作与判定：手机 lease、fixture lease、radio 开关、stage 契约、最终 verdict。

## 入口

```sh
pixi run just react-native formal-host-android-native-adapter-transition \
  --device <serial> \
  --app-apk <app-debug.apk> --app-apk-sha256 <sha> \
  --test-apk <app-debug-androidTest.apk> --test-apk-sha256 <sha> \
  --fixture-port <port> \
  --fixture-contract tests/peripherals/nrf52840/firmware/contracts/multifunction-rf-v2-a.json \
  --fixture-profile tests/peripherals/nrf52840/firmware/profiles/multifunction-rf-v2-a.json \
  --fixture-identity jlink:1050252028 \
  --output .work/android-adapter-transition \
  --authorize-install --authorize-fixture-start --authorize-bluetooth-permissions --use-root
```

`pixi run just react-native formal-host-android-build debug` 是唯一构建入口（App + androidTest）。
runner 冻结两个 APK 的 SHA-256，任何替换都拒绝。

## stage 契约

```
adapter-off → adapter-restored → scan-submitted → scan-started → advertisement
→ scan-stopped → connected → gatt → io → done
```

- `adapter-off` 是 **唯一** 触发主机开无线电的时刻；App 的恢复是对真实状态变化的响应，不是定时器。
- 该 lane 只有一个 peer，所以 marker 不带 per-link 索引（对比 multi-link 的 `subscribed:0`）。
- Python 词表与 Kotlin `marker("...")` 由离线测试做**精确**比较，避免两端漂移。

## 三次失败与根因

### 1. 场景起点假设错误（App 侧）

第一版假设“radio off 时 `start()` 必须失败”。真机结果：`start()` 成功。读
`AndroidBleBackend.adapterSnapshot` 后确认这是产品设计——初始化报告 `AdapterState`，不把
“adapter off”当作初始化失败。旧的 internal consumer 场景也从未这样断言：它断言的是
`off` 阶段 scan 命令终止于 `adapter_unavailable`。

### 2. 测试读错了返回值类型（App 侧）

`waitUntilStarted` 返回 `Promise<BlengCommandResult>`，失败在 `result.failure` 里，不是 Promise
rejection。第一版把 `Result.isFailure` 当作判据，于是永远为 false。

### 3. Android backend 缺少跨平台一致的门禁（产品缺陷，已修）

修好前两条后，真机依然显示 scan **成功开始**。根因：

- Apple backend 在 `startScan` 前用 `AppleEventMapper.scanAvailabilityError` 检查
  `centralManager.state == .poweredOn`，否则发 `commandCompleted(adapterUnavailable)`；
- Android backend 只处理 `bluetoothLeScanner == null` 和 `startScan` 抛
  `IllegalStateException`，而这台设备（PHB110 / Android 15）在 adapter off 时
  `getBluetoothLeScanner()` 非空、`startScan` 不抛，于是后端推送了 `ScanStarted`：
  App 被告知“扫描已开始”，实际永远不会投递。

修复（`platforms/android/.../AndroidBleBackend.kt::startScanOnHandler`）：与 Apple 对齐，先读
`adapter.isEnabled`（`SecurityException` 映射为 permission error），false 时退休该 scan
reservation 并 `emitCommandError(adapterUnavailable("Bluetooth adapter is not enabled"))`。

线上代码变为 `bleng.operation.adapter_unavailable`，真机断言该精确值。

## 真机结果

| 项 | 值 |
|---|---|
| 设备 | `3ac5cc09`（PHB110，Android 15，root） |
| App APK | `aa4af97bddee89e1508366e9e1f6942b1f9048f323efa82f999e882e45a0dc53` |
| androidTest APK | `38401fe7e5a9dc777abd76871c002c3888ddfa52f6a8181d15804ad1493264fd` |
| fixture | `jlink:1050252028` / `fd600000-…` / `BleHubMultiRfV2DKA` / read `fff10000-…` |
| run | `.work/android-adapter-transition-r6/344ab127-7fe8-46fb-9530-7f4313dade9c/result.json` |
| radio | `initial=ON` → `off=OFF` → `restored=ON`（`phone.json` 回执） |
| stages | `adapter-off,adapter-restored,scan-submitted,scan-started,advertisement,scan-stopped,connected,gatt,io,done` |
| 结论 | `status=passed`，`host_gate_status=pending`（host gate 仍按既定流程单独判定） |

`pixi run python -m pytest tools/tests/react_native/test_bleng_android_adapter_transition.py`
→ 16 passed / 32 subtests（无硬件）。

## 对 C 类核对的影响

#16 `adapter-transition` 的 Android 阻塞项关闭：场景、marker、host 判定都在正式产物上。
`react-native-internal` 里对应的 `rf_adapter_transition.tsx` 只剩 iOS 侧的旧入口，不再由 Android lane 依赖。

## 未覆盖

- `active-loss` 模式（连接中掉无线电）不在本切片：旧 consumer 有该模式，本 lane 只做
  `startup-off`。若需要保留，应作为独立语义另开 scenario，而不是复用一个 marker 词表。
- iOS 侧 adapter startup 已有自己的正式产物入口（`host-ios-rf-adapter-startup-candidate`）。
