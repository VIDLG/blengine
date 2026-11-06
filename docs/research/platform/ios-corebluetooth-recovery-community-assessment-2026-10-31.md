# iOS CoreBluetooth recovery 与社区方案评估

日期：2026-10-31

状态：研究结论；不改变当前 iOS `platform-timeout` 证据，也不宣称 system-loss gate 通过。

## 结论摘要

社区和 Apple 官方能力支持的是有界的 session reconstruction，而不是 Bluetooth radio OFF/ON 后的自动恢复保证：

```text
poweredOn observation
→ retrieve connected/known peripheral
→ scan fallback
→ connect
→ rediscover GATT
→ resubscribe
→ verify real value
```

如果 CoreBluetooth 不向 App 交付新的 advertisement callback，任何库都无法仅靠 App 代码制造这个事实。理想产品策略因此是：

```text
best-effort automatic recovery
→ recovered
或
→ platform-timeout / manual-reconnect-required
```

## Apple 官方能力

项目已下载的 CoreBluetooth 文档确认：

- `CBCentralManager` 的 BLE 操作只能在 `poweredOn` 状态进行；
- `centralManagerDidUpdateState` 是必须处理的状态边界；
- `retrieveConnectedPeripherals(withServices:)` 可以查询系统当前已连接、符合 service 条件的 Peripheral；
- `retrievePeripherals(withIdentifiers:)` 可以查询系统已知的 Peripheral；
- 如果找不到已知对象，应用仍需要使用 `scanForPeripherals` 并等待真实 `didDiscover` callback；
- `CBCentralManagerOptionRestoreIdentifierKey` 与 `willRestoreState` 支持 App 被系统停止/重新启动后的 central state restoration。

官方 state restoration 不是 radio OFF/ON 的保证：它要求 stable restore identifier，且应用仍需处理恢复字典缺少部分状态的情况。它不能保证系统在同一个 App 进程中完成 Bluetooth OFF/ON 后重新发送 advertisement callback。

本地官方快照：

- [`CBCentralManager`](../../../platform-api-reference/apple/cbcentralmanager.md)
- [`CBCentralManagerDelegate`](../../../platform-api-reference/apple/cbcentralmanagerdelegate.md)
- [`CBPeer.identifier`](../../../platform-api-reference/apple/cbpeer-identifier.md)

## 社区实现观察

### react-native-ble-manager

本地 checkout `contrib/react-native-ble-manager` 的 `ios/SwiftBleManager.swift`：

- 支持 `CBCentralManagerOptionRestoreIdentifierKey`；
- 实现 `centralManager(_:willRestoreState:)` 并向 RN 发出 restoration event；
- 连接时如果本地 Peripheral map 没有对象，会调用 `retrievePeripherals(withIdentifiers:)`；
- 暴露 `retrieveConnectedPeripherals`；
- 断开事件交给上层 callback，社区文档建议调用方自行决定是否 reconnect 或提示用户。

这是一套合理的 reconnect 基础设施，但本地源码和文档没有证明它能解决本项目观察到的“真实 radio OFF/ON 后没有 `didDiscover` callback”。它把恢复 policy 留给调用方，而不是声称 CoreBluetooth 一定恢复。

### react-native-ble-nitro

本地 checkout `contrib/react-native-ble-nitro`：

- 初始化 `CBCentralManager` 时支持 restore identifier；
- 支持 `retrieveConnectedPeripherals`；
- 连接查找失败时使用 `retrievePeripherals(withIdentifiers:)`；
- 转发 `willRestoreState`、connect、disconnect 和 scan callback；
- 明确把 iOS `requestBluetoothEnable` 标记为不支持，不能由 App 程序化打开系统 Bluetooth。

它的实现再次证明社区通用做法是恢复/重建路径，而不是强制修复系统 radio。该 checkout 没有提供本项目所需的旧 operation terminal barrier、exact native identity、resource-zero 或 system-loss verdict 证据，因此不直接替换 BleHub/BLeng 的 native layer。

### react-native-ble-plx

本地研究和 checkout 显示它暴露 restore identifier，并把 iOS BLE engine 放在独立 native adapter 中。底层 adapter 的完整恢复实现不在本次本地审计范围内，因此不能把它的 API 形状当作 CoreBluetooth system-loss 通过证据。

### 高级 Swift wrapper / Bluejay / mock

项目已有的 Swift BLE ecosystem assessment 结论仍成立：

- Bluejay 等库可以自带 auto-reconnect、FIFO 和 retry policy；
- Nordic mock、AsyncCoreBluetooth、RxBluetoothKit 等可以帮助测试或包装 API；
- 这些库不能改变 CoreBluetooth 的系统 callback 交付边界；
- 引入它们还会增加第二套 operation、identity、queue 和 retry semantics，不会移除 BleHub 必须保留的 correlation/retirement 代码。

参考：[`swift-ble-ecosystem-assessment.md`](swift-ble-ecosystem-assessment.md)。

## 与本项目真实证据的对照

### 健康进程中的 Central recreation

[`2026-10-30-ios-central-recovery-spike.md`](../../testing/reports/2026-10-30-ios-central-recovery-spike.md) 记录：

- 健康进程中替换 `CBCentralManager` 后可以再次收到 advertisement；
- 该结果只是 diagnostic，不证明 radio system-loss recovery；
- 当前 policy 允许最多一次 native Central recreation，不允许 retry loop。

### 真实 radio OFF/ON

[`2026-10-31-ios-system-loss-fresh-owner-platform-timeout.md`](../../testing/reports/2026-10-31-ios-system-loss-fresh-owner-platform-timeout.md) 记录：

- Bluetooth OFF/ON、old operation terminals、old resource-zero 和 fresh owner/backend 创建均成立；
- 独立 sniffer 能看到 Peripheral advertisement；
- fresh owner 的 scan 没有收到 CoreBluetooth advertisement callback；
- 没有新的 connection、GATT、subscription 或 EF value；
- 结果必须是 `platform-timeout`，不能改写为 pass。

因此，社区的 retrieve/scan/reconnect 方案值得接入作为 best-effort 路径，但不能被解释为已解决当前平台限制。

## BleHub/BLeng 应采用的方案

### 自动尝试

iOS native Supervisor 应执行一次有界恢复：

1. 观察 `poweredOn`；
2. 让旧 operation 逐一 terminal；
3. 退休旧 scan/connection/GATT/subscription route；
4. 等待 resource-zero；
5. 尝试 `retrieveConnectedPeripherals`；
6. 尝试 `retrievePeripherals(withIdentifiers:)`；
7. 找不到时按 service UUID scan；
8. 使用真实 advertisement callback 建立 fresh connection；
9. fresh GATT discovery、fresh subscription、真实 expected value verification；
10. 如果 bounded window 内没有 callback，最多执行一次 Central/backend recreation；
11. 仍失败则发布 `platform-timeout`。

### Process relaunch restoration

正式 iOS package 可以使用 stable restore identifier，并处理 `willRestoreState`。这覆盖的是系统停止/重新启动 App 的恢复路径，必须单独记录 identity 和 restoration evidence，不能与当前 radio OFF/ON same-process gate 混为一个结果。

### Manual reconnect

自动尝试失败后，公共状态应为：

```text
manual-reconnect-required
```

用户显式点击 reconnect 后，再创建 fresh session 并重复 retrieve/scan/connect/GATT/subscription/value 验证。只有真实 callback 和 value 全部存在，才能产生 manual reconnect success evidence。

### 禁止的方案

- 不能程序化打开 iOS Bluetooth；
- 不能用 Settings/Appium readback 替代 BLE recovery；
- 不能用 sniffer advertisement 替代 App `didDiscover`；
- 不能合成 advertisement、connection、notification 或 EF value；
- 不能无限 retry Central recreation；
- 不能因为 Android 自动恢复通过，就把 iOS 标为通过；
- 不能把 state restoration API 当作 radio OFF/ON 的保证。

## 最终判断

社区没有找到绕过本项目 iOS/CoreBluetooth callback 缺失的可靠方案。可复用的部分是：

```text
stable restore identifier
retrieve connected/known peripheral
scan fallback
explicit reconnect callback
```

不可复用为“自动恢复保证”的部分是：

```text
radio OFF/ON 后必然重新收到 advertisement
```

因此 iOS 的正确产品语义不是“必然自动恢复”，而是“自动尝试 + 真实证据判定 + 明确 manual reconnect fallback”。
