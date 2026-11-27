# RN-internal RF 场景迁移对照

本文件是 `react-native-internal` 退休前的场景级对照：旧的 29 个 `RuntimeMode` 各自应该落到哪一层、
今天有什么证据、缺什么。它不改变任何 gate——新 lane 目前有八个 managed UI 场景在**两端**真机通过：
`scan` / `notification` / `indication` / `io` / `cancellation` / `write-reject` / `ccc-enable-reject` /
`ccc-disable-reject`（见 [iOS lane](../testing/reports/2026-11-13-formal-ios-device-lane.md)、
[Android lane](../testing/reports/2026-11-13-formal-android-ui-lane.md) 与
[fault-reject 报告](../testing/reports/2026-11-20-managed-fault-reject-scenes.md)），另有两个 B 类
host 主导场景在两端通过（`passive-disconnect`、`recovery`/`recovery-ten`），以及 `multi` 在**两端**真机通过
（[报告](../testing/reports/2026-11-21-multi-link-isolation.md)）。

## 三个目标层，而不是一个

| 层 | 拥有什么 | 谁执行 | 现在的入口 |
|---|---|---|---|
| **A. Managed UI 场景** | 人能看见的 App 行为：会话、扫描、连接、GATT、订阅、RSSI、取消 | host producer + Appium/XCUITest 驱动同一个固定命令面 | `just react-native::formal-host-{android,ios}-managed-*` |
| **B. Host 主导 + UI 作证** | 宿主必须主动施加或等待的事件：多外设并发、soak、pending read、reload | host 拥有时序与 fixture；App 只证明用户可见状态 | `formal-host-*-managed-*` 扩展，或独立 producer |
| **C. 非 UI 原生/HIL** | 没有用户可见面的事实：adapter/permission 迁移、watchdog 内部、取消时延 | 原生测试与 HIL，不进 RN consumer | `tests/hil/*`、`platforms/*` 测试 |

把 C 类硬塞进 UI 场景是过去失败的主要来源（例如 adapter 迁移曾经需要滚动与权限弹窗），
所以分层不是便利，而是**让每个事实由能观测它的那一层证明**。

## 对照表

`旧模式` 是 `tests/consumers/react-native-internal/src/rf_contract.ts` 的 `RuntimeMode`。
`旧入口` 是 `just/react-native/mod.just` 里今天驱动它的 recipe 家族（Android 与 iOS 各有一套对称入口）。

| # | 旧模式 | 旧入口 | 目标层 | 目标形态 | 缺口 |
|---|---|---|---|---|---|
| 1 | `rf-workbench` / `rf-lab` / `rf-lab-suite` | `host-{android,ios}-*rf-lab*` | — | 不迁移：它们就是被替换的 workbench 本身 | 无（随 consumer 一起退役） |
| 2 | `basic` | `host-*-runtime` | C | **已就位**：正式 App 的 native instrumentation（`formal-host-{android,ios}-native-test`）与平台原生 runtime/lifecycle 测试 | 无；见 [`c-class-retirement-audit.md`](c-class-retirement-audit.md) |
| 3 | `stress` | `host-*-lifecycle-stress` | C | **事实已由原生覆盖**：8 轮 create/close、迟到回调、关闭后派发分别由 supervisor 与两端 backend lifecycle 测试证明；RN 入口冗余（轮数只是参数） | 见 [`c-class-retirement-audit.md`](c-class-retirement-audit.md)；若要保留重复上界，应加在 supervisor FFI 测试而非 RN 场景 |
| 4 | `reload` / `rf-reload` | `host-*-runtime-reload` / `*-rf-reload` | B | **建议 C 类**：新架构里 owner 就是 JS 创建的 Nitro 对象，重载即退役，不存在「跨 reload 的 native generation」 | 见 [`b-class-scene-reachability.md`](b-class-scene-reachability.md) |
| 5 | `permission-authorization` | `formal-host-ios-permission-authorization`（正式 `BleHubBlengHostUITests`） | C | fresh-install deny/allow：OS 弹窗由正式 UI 测试目标回答，App 从自己的初始化 snapshot 断言 | **已就位**：真机两次通过，见 [`2026-11-22-ios-permission-authorization-formal-ui.md`](../../testing/reports/2026-11-22-ios-permission-authorization-formal-ui.md) |
| 6 | `rf`（综合） | `host-*-rf` | A | 拆成 #8–#14 的独立场景 | 拆分而非整体迁移 |
| 7 | `rf-connect` | `host-ios-device-rf-connect` | A | 并入 `notification`/`indication` 的 `link:connected` | 无 |
| 8 | `rf-gatt` | `host-ios-device-rf-gatt` | A | 并入 `notification`/`indication` 的 `gatt:ready` | 无 |
| 9 | `rf-io` | `host-ios-device-rf-io` | A | **已迁移**：`io` 场景（read → write → read-back → RSSI，12 项 trace） | 无；fixture 决定写入长度，profile 声明被 provision 的值长度，不声明读回内容 |
| 10 | `rf-rssi` | `host-ios-device-rf-rssi` | A | **已迁移**：并入 `io` 场景的 `rssi:observed`（连接链路上的真实读数，非 idle 占位） | 无 |
| 11 | `rf-notification` | `host-ios-device-rf-notification` | A | **已迁移**：`notification` 场景 | 无 |
| 12 | `rf-indication` | `host-ios-device-rf-indication` | A | **已迁移**：`indication` 场景 | 无 |
| 13 | `rf-passive-disconnect` | `host-ios-device-rf-passive-disconnect` | B | **已迁移，两端真机通过**：fixture 主动断链，App 从连接自己的结束 lane 宣布 `link=disconnected` + `linkEnd=unrequested`（不提交任何命令），接静默窗口与命令退场断言 | 无；需要 `BlengConnection.awaitEnd`（已实现），见 `docs/testing/reports/2026-11-21-connection-end-lane-and-passive-disconnect.md` |
| 14 | `rf-cancellation` | `host-*-rf-cancellation` | A | **已迁移**：`cancellation` 场景（13 项 trace，fault-armed）：fixture 在固件里 hold 住第一条读，App 排队第二条，UI 取消排队那条，释放被 hold 的读，再读一次证明 lane 恢复 | 无；Android `f1f30fb6` 与 iOS `99c23a6e` 均真机通过 |
| 15 | `rf-recovery` / `rf-recovery-ten` | `host-*-rf-recovery(-ten)` | B | **已迁移，两端真机通过**：`recovery`（3 轮）与 `recovery-ten`（10 轮）各自成 scene，每轮在同一 owner 内重做 scan/link/discover/read，peer 侧结束链路、App 自己宣布，轮间由 host 显式恢复广播；轮数与预算同源声明 | 无；需要 `DirectFixture.resume_advertising()`（已实现：固件在链路建立后不再自行广播） |
| 16 | `rf-adapter-transition` | `formal-host-android-native-adapter-transition`（Android）；`host-ios-rf-adapter-startup-candidate`（iOS） | C | Android：正式 App 非 UI instrumented lane；iOS：正式产物形态入口 | **已就位**：Android 真机通过，见 [`2026-11-22-adapter-transition-formal-app.md`](../../testing/reports/2026-11-22-adapter-transition-formal-app.md) |
| 17 | `rf-permission-transition` | `formal-host-android-native-permission-transition`（正式 App，三进程） | C | denied / active 进程死亡 / restored 均由正式 instrumented lane 断言 | **已就位**：Android 真机通过，见 [`2026-11-22-permission-transition-formal-app.md`](../../testing/reports/2026-11-22-permission-transition-formal-app.md)；iOS 无对应入口（归 #5） |
| 18 | `rf-multi` / `rf-multi-soak` / `rf-multi-fault` | `host-*-rf-multi*` | B | **已迁移，两端真机通过；soak 与 fault 都是声明参数**：正式 host 的非 UI 仪器化 lane，两片 multifunction v2 A/B 各自持有 lease 与会话回执，host 只结束第一条声明的链路，其余每条都必须收到**在该结束之后**注入的值，各链路读回互不相串 | 无；`--rounds`（iOS 3 轮 / Android 2 轮）与 `--fault write-reject:1`（两端 + 与轮次组合）均真机通过，见 [`multi-link-isolation` 报告](../testing/reports/2026-11-21-multi-link-isolation.md)。该场景真机第一次就抓到一个产品缺陷：通知 lane 曾是单槽，后一条订阅会接管前一条的投递，已按「lane 是集合」修掉；iOS 侧的「值未抵达」最终证实是 staged 产物陈旧，而不是后端缺陷 |
| 19 | `rf-pending-read-reload` | `host-*-pending-read-reload` | B | **建议 C 类**：旧场景依赖 native host 的 generation 闸门与 reload 通知，正式 App 无此形状；等价性质由 cancellation + retirement 契约承担 | 见 [`b-class-scene-reachability.md`](b-class-scene-reachability.md) |
| 20 | `rf-write-reject` / `rf-ccc-enable-reject` / `rf-ccc-disable-reject` | `host-*-rf-{write-reject,ccc-*-reject}` | A | **已迁移，两端真机通过**：三个独立 managed 场景。`write-reject` 在固件里停住 App 精确下发的那次写并由 host REJECT，App 必须给出带 ATT status 的失败、无写回执、读回不变，再用同一次写成功并读回等于请求；`ccc-enable-reject` 让被拒的 enable 终止为 failed subscription，重建订阅并真收到一条 host 注入的值；`ccc-disable-reject` 被拒后订阅进入 `unverified`，一次有界探针要求 App 如实描述（值到达→`subscribed`，观测超时→仍 `unverified`，`ended` 即失败），第二次 disable 才退役 | 无；Android APK `33a60d3a…` / iOS 树 `2db7b1e6…` 三项均 passed |
| 21 | `rf-watchdog` | 无仓库内 HIL 入口（已删除 `host-*-rf-watchdog`） | C | **接缝事实已由原生覆盖**（`GattWatchdogRegistryTests` / `GattWatchdogOrchestrationTests` / `ActiveWatchdogSlotTest` 等） | **已关闭（native-only）**：固件 HELD 上限 20 s < native watchdog 30 s，HIL 无法制造“丢回调”；见 [`2026-11-22-watchdog-hil-retirement.md`](../../testing/reports/2026-11-22-watchdog-hil-retirement.md) |

## 迁移顺序（按依赖，不按编号）

1. **先补 A 类的缺项**：`rf-cancellation`（已迁移并在两端通过）、三项 fault reject（已实现，离线全绿，待真机）。
   它们与已通过的 `scan`/`notification`/`indication`/`io` 共用同一个固定命令面，风险最低、收益最快。
2. **再做 B 类**：`passive-disconnect` → `recovery(-ten)` → `multi*` → `pending-read-reload` → `reload`。
   这一层需要「host 施加事件、App 仅作证」的新 producer 语义，不能靠加 UI 控件解决。
   `passive-disconnect` 的第一块缺口（没有连接状态观测通道）已由 `BlengConnection.awaitEnd` 补上，
   场景随之改成「零命令观察」，两端待真机确认。
3. **C 类最后核对**：`basic`/`stress`/`reload` 由正式 lane 或原生测试覆盖；`adapter-transition`、
   `permission-transition` 已迁成正式 App 的 instrumented lane 并在 Android 真机通过；
   `permission-authorization` 已迁成正式 `BleHubBlengHostUITests` 的 fresh-install deny/allow，
   iPhone 真机两次通过；`watchdog` 显式 re-admit 为 native-only 并删除了两条 HIL recipe。
   详见 [`c-class-retirement-audit.md`](c-class-retirement-audit.md)。
4. **`rf-workbench`、`rf-lab`、`rf-lab-suite` 不迁移**：它们是旧 consumer 的容器，迁移它们等于迁移整个旧实现。

## `multi` 之后还差什么

`rf-multi-soak` 与 `rf-multi-fault` 不与 `multi` 分成三份 runner：同一条 lane 已经声明「N 条 peer、
第一条被打断」，soak 只是把同一场景按轮重复、fault 只是把「peer 消失」换成一次注入的故障。
先在现有 runner 上加轮次/故障参数并证明它仍然是同一份回执形状，再决定是否需要新入口。

## 判定「一个场景迁移完成」的条件

对每个场景，只有当下面四项同时成立才算完成（缺一项就是没有迁移，而不是部分迁移）：

- 目标层有自己的入口（just recipe），且入口不吃手工步骤以外的参数；
- 证据是**该入口自己产出的回执**，包含手机释放、fixture 关闭与清理错误为空；
- 断言按名字失败：观测不到就点名缺什么，而不是靠超时；
- 旧 consumer 里对应的 mode 被删除或标注为已退役，且不留下会漂移的第二份实现。

## 当前状态

- A 类：**8 个场景两端已通过**（`scan`、`notification`、`indication`、`io`、`cancellation`、`write-reject`、`ccc-enable-reject`、`ccc-disable-reject`）。
- B 类：`passive-disconnect`、`recovery`（3 轮）、`recovery-ten`（10 轮）已完成迁移并在两端真机通过（待评审）；
  `multi` 已完成迁移并在 **Android** 真机通过。iOS 侧还没有同一形状的 lane——这是**缺失的入口**，
  不是「iOS 不支持」：引擎与 Swift 后端已按连接/会话/特征身份隔离，缺的是在真机上跑 XCTest 而不是
  XCUITest 的 host 工具（Android 侧就是 `am instrument`）。剩下 `reload`/`pending-read-reload` 判为 C 类。
  见 [`b-class-scene-reachability.md`](b-class-scene-reachability.md)。
- C 类：0 个核对完成。
- 所以 A 类的迁移已经清零；`react-native-internal` 仍不能退休，因为 B/C 类的 29 个 mode 还没有等价新证据。

一个场景迁移成立的隐形前提是本文件之外的一条布局规则：机器读的每个值必须在 pinned 命令面内。
`io` 场景第一次真机失败就是因为它读的 `gatt-result`/`rssi-value` 在可滚动内容区，iOS 的
`is_displayed` 恒为 false——值存在且有文本，但场景无法观测。把结果行移进 pinned console 后，
两个平台都能零手势读到动作自己的回执。

第二条隐形前提是**预算规则**：场景在「对端已被卡住」和「释放对端」之间能花的 host 往返时间是
production backend 的 native watchdog（Android/Apple 均 **30 s**，防丢回调，不因场景放宽），
以及固件自己的 10 s ARM / 20 s HELD 上界——后者先到。`cancellation` 第一次真机跑就撞在这条上：
从提交被 hold 的读到 `hold.release()` 的 Appium 往返约 10–11 s，旧 10 s watchdog 到点把整条连接退役，
App 只能如实报 `operation.timeout`。因此该窗口内不设独立 `ui.identity()` 检查点——
`hold.*` 的每个动作前后都已跑完整 host-authority guard，`ui.click()` 在 dispatch 前也做
foreground+identity+guard，中间的检查点只是重复证明并吃掉预算。三个 fault reject 场景沿用同一规则，
并且把「打字」这类 UI 工作放在 `hold.arm()` 之前，不让它占用 hold 窗口。
