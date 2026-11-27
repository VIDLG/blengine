# Watchdog HIL 入口的退役决定（C 类 #21）

结论：**watchdog 不再有 standalone HIL 入口**。接缝事实由两端原生测试证明；两条
`host-*-rf-watchdog` recipe 已删除，因为它安装的是正在退役的 internal host App，而且在今天的
时序参数下它**不可能**产生自己声称的证据。

本文件记录做这个决定的算术与代码位置，避免以后有人“再把入口修好”时重复踩一遍。

## 主张本来是什么

旧入口（`tools/react_native/run_rf_watchdog.py`）让 App 发起一个对端不响应的 GATT 操作，
期望 native watchdog 到期，然后：

- 活动操作以 timeout 终态结束；
- 连接被 retired，队列里的操作以 aborted 结束；
- App 用新的 link 恢复。

## 为什么现在做不到

| 事实 | 数值 | 位置 |
|---|---|---|
| Android backend watchdog | **30 000 ms**（`DEFAULT_ACTIVE_OPERATION_WATCHDOG_MS`） | `platforms/android/library/src/main/kotlin/dev/blehub/android/AndroidBleBackend.kt:83` |
| Apple backend watchdog | **30 000 ms**（`gattIoWatchdogMilliseconds` 默认值） | `platforms/apple/Sources/BleHubApple/AppleBleBackend.swift:66-76` |
| 固件 ARM 预算 | 100–10 000 ms | `att-fault-arm-timeout-10000` capability |
| 固件 HELD 预算 | 100–20 000 ms | `att-fault-hold-timeout-20000` capability |
| 旧入口当时的期望 | `backend_watchdog_expected_ms: 10000` | `tools/react_native/run_rf_watchdog.py:1323` |

固件的 HELD 上限是 20 s，而 native watchdog 是 30 s，所以 hold 会在 watchdog 之前到期——
`docs/research/peripheral/nrf52840-fault-firmware-v2.md` 已经写明这一点（“firmware 的 20 s HELD
会先到期：场景应把 held 窗口做短，而不是依赖 backend 计时”）。旧入口能工作是因为它写于
backend watchdog 还是 10 s 的时代。

两侧 watchdog 的 30 s 不是可以随手调小的测试参数：

- Apple 的注释直接给出规范理由（Core spec Vol 3 Part F 3.3.3，ATT 事务最长 30 s）；它防的是
  **丢回调**，不是“对端慢”。调小会把合法的慢对端变成致命连接拆除，属于产品行为变化。
- Android 同理，且在 `AndroidBleBackend` 里是构造参数默认值，RN 桥没有暴露它。

更根本的是：watchdog 要抓的不是“对端不响应”，而是“平台没有回调”。固件只能制造前者
（hold / reject / defer），无法制造后者。**没有任何受支持的 fixture 故障能让 ATT 事务在
30 s 后仍然 pending**，除非把 watchdog 调小或让链路静默到 supervision timeout（那是另一类
事实，且会先触发 disconnect 回调）。

## 因此的决定

1. 删除两条 standalone recipe：`host-android-rf-watchdog`、`host-ios-device-rf-watchdog`。
   它们安装/启动 `com.blehubnitrohost`，是 `react-native-internal` 退休的直接阻塞项。
2. `run_rf_watchdog.py` 保留，只因为 `run_rf_cancellation.py` 复用了它的准备/收尾路径；
   watchdog 模式不再有任何仓库内入口，也不会被当成 gate。
3. C 类 #21 记为 **已关闭（native-only re-admit）**，不记为“已通过 HIL”。

## 现在由谁证明

| 层 | 测试 | 证明什么 |
|---|---|---|
| Apple backend | `GattWatchdogRegistryTests`、`GattWatchdogOrchestrationTests`、`AppleOperationCancellationTests` | 定时器所有权、取消、迟到回调不会完成已退休操作 |
| Android backend | `ActiveWatchdogSlotTest`、`AndroidConnectionLifecycleTest`、`PendingIoQueueTest` | 单槽所有权、watchdog 退休活动+排队操作、连接级终态 |
| supervisor / core | `cargo test -p bleng -p bleng-supervisor --all-features` | 退休后派发、迟到信号、恢复策略 |

这些测试证明的是**一次** watchdog 语义；重复次数不是主张（与 `stress` 的处理一致）。

## 仍然存在的旧面（不要当成证据）

`host-android-rf-lab-suite`（`run_rf_lab_full`）里仍有一个 `rf-watchdog` flow。它属于整个
`rf-lab-suite` 容器，而这个容器本来就不迁移、随 consumer 一起退役；按上面的算术，它也无法
跨过 20 s HELD 上限。**任何来自该 flow 的“watchdog passed”都不应作为 native watchdog 的证据**，
它只能证明“某个更早的超时发生了”。清掉整个 `rf-lab-suite` 是 consumer 退休那一步的工作。

## 什么会让这个决定改变

只有在出现一个**显式、经过评审的测试接缝**时，例如正式 App lane 可以为一个 attempt 指定
更短的 backend watchdog（并且生产默认值不变、证据里记录该覆盖）。那是一次产品 API 变更，
需要单独 review，不是把入口“修一修”。

在那之前，任何声称“watchdog HIL passed”的记录都应被视为**不可信**：它要么用了退役 App，
要么把别的超时（hold expiry / platform connect timeout / supervision timeout）当成了 native
watchdog。
