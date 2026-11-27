# 前后台切换下的连接：场景设计与决策

## 结论（先说事实）

App 目前**没有声明任何后台能力**：iOS `Info.plist` 没有 `UIBackgroundModes: bluetooth-central`，
Android 没有前台服务，JS 没有 `AppState` 处理。因此「后台时尽量保持连接」现在是**未实现**，
不是「已实现待验证」。写一个后台场景之前必须先决定要哪一档语义。

## 三档语义（从便宜到贵）

| 档 | 承诺 | 需要的产品改动 | 能断言什么 |
|---|---|---|---|
| **T1 诚实** | 不主动拆链；系统若结束了链路，App 必须如实报告，不得仍显示「已连接」 | **无**（现有 backend 的连接终止广播 + `passive-disconnect` 场景已经证明 fail closed） | 后台→前台后，App 的状态与 fixture 的真实持有状态一致：链路还在就显示 connected/subscribed，被系统结束就显示终态 |
| **T2 真保持（iOS）** | 后台期间链路保持，事件仍能到达 App | `UIBackgroundModes: bluetooth-central`（Info.plist）；可选 CoreBluetooth 状态恢复 | 后台期间 fixture 的值仍被 App 记录；回到前台无需重连 |
| **T3 真保持（Android）** | 后台期间进程不被杀 | 前台服务 + 常驻通知 + `FOREGROUND_SERVICE_CONNECTED_DEVICE` 权限 | 同上；且 App 进程 pid 不变 |

Android 的 T3 代价最显性：用户会看到一个**常驻通知**。这不是测试决定，是产品决定。

## 建议

先做 **T1**，因为它不需要任何产品改动，而且它恰好是「尽可能保持」里最不能妥协的一半：**不能谎报连接**。
T2/T3 作为独立决定，各自带一个明确的用户可见后果。

## 场景定义（T1，可立即实现）

新场景 `background-subscription`，在已订阅的链路上做一次前后台往返：

1. `open`：建链、发现、声明目标（沿用 `_ccc_open`）。
2. `subscribe`：`gatt-subscribe`，App 报 `notify=subscribed`。
3. **宿主动作 `publish`**：fixture 发第一个值（证明订阅真的建立）。
4. `background`：`press(.home)`（iOS）/ `pressHome()`（Android），等待 App 进入后台。
5. **宿主动作 `publish`**：fixture 在后台期间发第二个值。这是「保持」的直接证据面——
   App 是否在后台收到它，由回前台后的账本回答。
6. `resume`：`activate`，等 `state=started`。
7. `settle`：读 `run-status` 与 `action-ledger`。

派生的判定（host 侧，用 fixture 的持有状态作 oracle）：

- 若 fixture 仍持有该链路 → App **必须**报 `link=connected` / `notify=subscribed`，否则判「谎报了断链」；
- 若 fixture 已失去链路 → App **必须**报终态，否则判「谎报了连接」；
- 后台期间发出的值：App 若记录到，要求账本里恰好一次（不重复投递）；若没记录到，要求它**不**声称收到过。

第三条是本场景最有价值的部分：它把「后台丢事件」变成可观测的事实，而不是靠推断。

## 驱动侧需要的新步骤

两端各加一个 `background` 操作（`press(.home)` / `pressHome()`），语义是「让 App 进入后台并证明它确实不在前台」，
并带有**声明窗口** `for`：

- iOS：`XCUIDevice.shared.press(.home)` 后要求 `app.state != .runningForeground`，然后按 `for` 秒持续采样同一事实；
- Android：`device.pressHome()` 后要求 `currentPackageName != appPackage`，同样按住 `for` 秒。

瞬时离开不算后台窗口：一个从未被挂起的 App 不可能丢掉订阅（iOS 在数秒内就挂起后台 App），所以 `for` 是**要守住的窗口**
而不是超时；App 在窗口内回到前台就判失败。窗口由计划声明（`BACKGROUND_DWELL_SECONDS`），驱动不接受没有 `for` 的步骤，
`observed["background"]` 记录实际离开时长（例：`not-foreground for=30.8s`）。

注意与 `activate` 相反：`activate` 用 `currentPackageName`/`app.state` **证明在前台**，这里用它**证明在后台**。

## 已实现（2026-11-24）

`background` 场景已落地并两端真机通过：

| 项 | 内容 |
|---|---|
| 阶段 | `open` → `subscribe` → `value`（宿主 publish）→ `away` → `back`（宿主 publish） |
| 证据词汇 | `BACKGROUND_SCENE`，trace 到 `notify:backgrounded` / `notify:resumed` / `notify:value-after-resume` |
| 派生 | 后台阶段的 App 必须真的不在前台，并且**撑满声明的 30 s 窗口**；回前台后 `link=connected`、`notify=subscribed`，且 `notifyTotal >= 2`（后台期间那个值必须到达，否则判“订阅没有活过后台窗口”） |
| 产品能力 | iOS `UIBackgroundModes: bluetooth-central`；Android `BleHubKeepAliveService`（`connectedDevice` 前台服务，仅 `onStop`→`onStart` 之间） |
| 真机 | 2026-11-24 加了 30 s 声明窗口后重跑：Android background **55 s**（窗口 receipt `for=30.8s`）、iOS **94 s**（`for=30.1s`），两端序列 12/12 通过，24/24 bundle `integrity=passed`。窗口之前是瞬时离开（Android 23 s / iOS 64 s），不足以证明挂起 |

一个真机细节：iOS 的 `XCUIDevice.press(.home)` **偶尔会被吞**（App 仍在前台），所以步骤在 3 秒后补按一次，并把最终 `app.state` 原值写进证据；Android 用 `currentPackageName` 判断，不需要重试。

三档里 **T1（诚实）由现有机制承担**：`passive-disconnect` 场景已经证明链路被结束时 App 会如实报终态；本场景证明的是 T2/T3 —— 链路真的活过了后台窗口。

## 不做什么

- 不在本场景里做冷启动恢复、CoreBluetooth state restoration、进程被杀后的重连——那些是各自独立的场景，
  需要自己的证据词汇。
- 不把 T2/T3 混进 T1 的判定：T1 通过不代表链路真的保持住了，只代表 App 没有说谎。
