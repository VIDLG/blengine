# C 类核对：`react-native-internal` 退休前还剩什么

本文件只回答一个问题：迁移表里标为 **C 类**（没有用户可见面、应当由原生/HIL 证明）的那些旧
`RuntimeMode`，在 `tests/consumers/react-native-internal` 退役之后各自由什么证明；如果答案依赖正在退役的
consumer，那就是退休阻塞项。它不改变任何 gate，也不把「已构建」当「已运行」。

## 逐项结论

`旧入口` 与 `依赖谁` 取自 `just/react-native/mod.just`：`host` = `tests/consumers/react-native-internal`，
`formal_host` = `tests/consumers/react-native-bleng`。

| # | 旧模式 | 今天的入口 | 入口构建/安装谁 | 退休后由谁证明 | 结论 |
|---|---|---|---|---|---|
| 2 | `basic` | `formal-host-{android,ios}-native-test`、`formal-host-android-engine-reload-test` | **formal** App | 正式 native instrumentation / 模拟器 XCTest + 平台原生测试 | 已就位 |
| 3 | `stress`（8 轮 create/close、late callback、post-close dispatch） | `host-android-lifecycle-stress`、`host-ios-device-lifecycle-stress` | internal host | 原生：supervisor 的退休/迟到回调/关闭后派发测试 + 两端 backend lifecycle 测试（见下表） | 事实已被原生覆盖；RN 入口是冗余，轮数只是参数 |
| 4 | `reload` | `formal-host-android-engine-reload-test` | **formal** App | 正式 native instrumentation | 已就位 |
| 5 | `permission-authorization` | `formal-host-ios-permission-authorization`（正式 `BleHubBlengHostUITests`，fresh-install deny/allow） | **formal** App | OS 授权弹窗由正式 UI 测试目标证明；引擎事实由 Apple backend 测试证明 | 已就位 |
| 16 | `adapter-transition` | `formal-host-android-native-adapter-transition`（正式 App instrumented lane）；iOS 走 `host-ios-rf-adapter-startup-candidate` | **formal** App（Android）；iOS 已是正式产物形态 | Android 正式 lane 真机通过；iOS 由既有正式入口证明 | 已就位 |
| 17 | `permission-transition` | `formal-host-android-native-permission-transition`（正式 App instrumented lane，三进程） | **formal** App | 正式 lane 真机通过（denied / active death / restored） | 已就位 |
| 21 | `watchdog` | 无仓库内 HIL 入口（`host-*-rf-watchdog` 已删除） | — | 两端原生 watchdog 测试 + supervisor 退休测试；无法用固件故障跨过 20 s HELD 上限 | 已关闭（native-only re-admit）；见 [决定报告](../testing/reports/2026-11-22-watchdog-hil-retirement.md) |

全部 C 类项均已在正式 lane 或原生测试里找到归属；`react-native-internal` 不再被任何 C 类项依赖。

## 原生证据盘点（离线可跑）

| 层 | 覆盖 | 本轮结果 |
|---|---|---|
| 正式 App native instrumentation（Android 真机） | `formal-host-android-native-test`（`HybridBlengCentralInstrumentedTest`） | **passed** |
| 正式 App native 测试（iOS 模拟器） | `formal-host-ios-native-test`（`BleHubBlengHostNativeTests`，`test-without-building`） | **passed** |
| `crates/bleng` + `crates/bleng-supervisor` | owner 退休/迟到信号/关闭后派发/scan 交付/恢复策略 | `cargo test -p bleng -p bleng-supervisor --all-features` → **470 passed** |
| Android backend（`platforms/android/library` JVM） | `AndroidConnectionLifecycleTest`、`NotificationLifecycleTest`、`CharacteristicWriteLifecycleTest`、`PendingIoQueueTest`、`ActiveWatchdogSlotTest`、`ConnectionRfResourceCountsTest` | `just react-native bleng-android-unit-test` → **passed**（`testDebugUnitTest --offline`） |
| Apple backend（`platforms/apple` SwiftPM） | `AppleBackendLifecycleTests`、`GattControllerTests`、`ConnectionControllerTests`、`GattIoControllerTests`、`GattWatchdog*Tests`、`AppleOperationCancellationTests` | **未在本轮运行**：`swift build/test --package-path platforms/apple` 在本会话中无输出阻塞（0% CPU、无子进程），已终止。清单按文件/测试名核对，不作通过声明 |

「stress」原有的 8 轮只是重复次数；上面这些测试各自证明的是**一次**退休/迟到回调/关闭后派发的语义，
重复不改变主张。如果确实要保留一个重复上界的证据，应加在 supervisor 的 FFI 测试里，而不是 RN 场景。

## 剩余阻塞项

无。

已关闭：

- #5 `permission-authorization`：正式 `BleHubBlengHostUITests` 的 fresh-install deny/allow，真机两次通过；
  见 [iOS permission lane 报告](../testing/reports/2026-11-22-ios-permission-authorization-formal-ui.md)。
- #16 `adapter-transition`：见 [adapter lane 报告](../testing/reports/2026-11-22-adapter-transition-formal-app.md)。
- #17 `permission-transition`：见 [permission lane 报告](../testing/reports/2026-11-22-permission-transition-formal-app.md)。
- #21 `watchdog`：显式 re-admit 为 native-only，两条 HIL recipe 已删除；固件 HELD 上限 20 s、
  native watchdog 30 s（不得为测试调小），HIL 无法制造“丢回调”。理由与代码位置见
  [watchdog 退役决定](../testing/reports/2026-11-22-watchdog-hil-retirement.md)。

## 与 B 类的关系

`recovery`、`passive-disconnect`、`multi`（含 `--rounds` / `--fault`）已经在正式 lane 两端真机通过；
C 类已无阻塞项。

## 删除已完成

2026-11-22：`tests/consumers/react-native-internal`、`integrations/react-native-internal`、全部 `host-*`
Just recipe、以及只服务于这些入口的 20 个 `tools/react_native` 模块和 15 个测试模块已物理删除；
正式 host 的 autolinking 守卫成为仓库里唯一保留的 `react-native-internal` 字样。删除证据、保留清单、
根因修复与验证命令见
[internal consumer 退休报告](../testing/reports/2026-11-22-react-native-internal-retirement.md)。该报告中
「Residual」一节列出仍需单独决策的部分（退役 lane 的 peripheral 模拟器与描述旧 consumer 的研究文档）。
