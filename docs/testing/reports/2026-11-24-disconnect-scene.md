# `disconnect` 场景：操作员自己的断链，以及它替掉的那条说谎的订阅

日期：2026-11-24

范围：新增第 13 个 managed 场景 `disconnect`，以及它在 App 侧暴露并修掉的一个真实产品缺口。
Android 真机已在同一候选上跑完整序列（13/13 passed）。iOS 尚未跑，因此 iOS 无本场景证据。

## 这条场景要证明什么

前面的 12 个场景里没有任何一条点过 `disconnect-peripheral`。`passive-disconnect` 证明的是「没人问过的
结束要如实宣布」，`recovery` 复用它；但**操作员自己发起的结束**是另一份承诺：

1. `BlengConnection.awaitEnd` 报的是 `by: operator`，不是 `unrequested`——一次由本屏提交的结束
   不得被叙述成一次没人问过的结束；
2. 所有需要活链路的控件（`CONTROLS_REQUIRING_LINK`）退场；
3. 链路上的订阅**不会比链路活得久**：App 不能在一条自己宣布已断的链路上继续显示 `subscribed`；
4. 断链之后不再计数任何新的通知值。

第 3 条正是这次发现的缺陷。`useManualScan` 的 `settleEnd` 是链路结束的唯一写者，它清掉了
`run.notifications`，但从未改 `notifyPhase`：断链后投影仍在说 `subscribed`。按钮因为 `linked()`
已经关掉，所以界面上「看起来没事」，但机器面在说谎——而这正是 managed 场景要抓的那类事实。

## 改法

### 1. 产品面（`tests/consumers/react-native-bleng/src/useManualScan.ts`）

`settleEnd` 在写链路终态的同时退掉它承载的订阅：

```ts
const carriedSubscription = run.notifications !== undefined && !run.notifyTerminal
...
if (carriedSubscription) run.notifyTerminal = true
publish(run, {
  link: 'disconnected', linkEnd: by,
  ...(carriedSubscription ? { notifyPhase: 'ended' as const, notifyError: undefined } : {}),
  ...(detail === undefined ? {} : { error: detail }),
})
```

选择在 `settleEnd` 而不是通知 lane 里做，是因为断链之后不再有任何 pull 会发生：订阅流自己的
terminal（`ended: reason`）永远不会被读到，只有链路结束这一刻能确定地写终态。三种结束（operator /
unrequested / owner）走同一个写者，所以三种都被覆盖。

### 2. 场景词汇

| 位置 | 内容 |
|---|---|
| `bleng_scan_evidence.py` | `DISCONNECT_SCENE`：15 项 trace，`characteristic_role="updates"`，`kind="disconnect"` |
| `scene_native.py` | `PHASES["disconnect"]` = `open` → `value`(host `publish`) → `sever`；`DISCONNECT_LEDGER`；`_disconnect_observations` |
| `scene_plans.py` | `_disconnect_open`（读一次 + 建立订阅）、`disconnect.value`（复用 `_pull_value`）、`_disconnect_sever`（tap → 静默窗口 → 控件退场 → close） |
| `bleng_{android,ios}_scan.py` | 目标推导改由 `GATT_TARGET_KEYS` 驱动，`disconnect` 的 GATT 目标就是它订阅的那条特征 |
| `scene_sequence.py` | `ORDER` 中置于 `io` 与 `cancellation` 之间：仍是 fault-free 路径，不排在任何需要固件 hold 的场景之后 |

trace：

```text
idle, started, scan:running, scan:advertisement-matched, scan:stopped,
link:connected, gatt:ready, read:initial, notify:subscribed, notify:value-matched,
link:operator-ended, notify:ended, link:silent, controls:withdrawn, closed
```

三阶段不是风格问题：一次通知只能送到已经建立的订阅，所以「host 发一次值」必须夹在订阅与断链之间，
否则 `notify:ended` 这条断言可以在一条从未投递过的 lane 上成立。

`characteristic_role="updates"` 而不是 `read-write`：`updates`（fff2）本身可读，所以「读一次」与
「订阅一次」落在同一条 profile 声明的特征上，不需要两条目标，也不需要改固件。

### 3. 推导里有三条不可省的检查

| 检查 | 为什么不能省 |
|---|---|
| `severed.linkEnd == "operator"` | 这正是与 `passive-disconnect` 的分界线；报 `unrequested` 说明 App 把自己做的事归给了别人 |
| `severed.notify == "ended"` | 本次修复的契约；修之前这里读到的会是 `subscribed` |
| `severed.notifyTotal == value.notifyTotal` | 「无残留通知」的可测形式：断链前后计数必须一致，晚到的值会把这条打红 |

再加 `still` 静默窗口（同时守住 `link` / `linkEnd` / `notify` 与计数）与 `_require_withdrawn_controls`。

`DISCONNECT_LEDGER` 是第二证人：它要求记录里出现 `disconnect` + `disconnect-wait`。同一份 preamble
加一次读、没有 `disconnect` 条目，描述的是 `passive-disconnect` 而不是本场景——测试里就是把
`_PASSIVE_LEDGER` 喂进来证明这一点。

## 无设备守卫

`tools/tests/react_native/test_scene_semantics.py`（executor-free，`_ScriptedDriver` 只供给 App 的回答）：

- `test_an_operator_end_holds_and_retires_what_the_link_carried`（完整 trace；peer 侧只有一次
  `publish:updates`，断链本身不是 peer 命令）
- `test_a_subscription_still_claimed_after_the_end_cannot_pass`（`notify=subscribed` → 拒绝）
- `test_an_end_the_app_blames_on_nobody_cannot_pass`（`linkEnd=unrequested` → 拒绝）
- `test_a_value_counted_across_the_end_cannot_pass`
- `test_a_record_without_the_disconnect_is_a_different_scene`
- `test_a_disconnect_that_never_settled_cannot_pass`
- `test_the_quiet_window_must_have_been_sampled_after_an_operator_end`

App 侧：`Notifications.test.tsx` 的
`test`-等价的 `it('retires the subscription when the link ends instead of claiming it on a dead link')`
——链路在自己的 end lane 上结束后，`notify-state` 必须是 `ended`，`pull-notification` 与
`gatt-subscribe` 必须撤场。

`test_scene_sequence.py` 的 `test_the_shared_order_is_every_managed_scene_once` 自动要求新场景进入
共享顺序；`test_the_shared_order_is_the_dependency_order` 显式钉住 `io < disconnect < cancellation`。

## 已验证（本机，无设备）

```sh
pixi run python -m pytest tools/tests -q                      # 1015 passed, 21 skipped, 2877 subtests
pixi run just react-native::formal-host-test                  # 137 passed (jest, 10 suites)
pixi run just react-native::formal-host-check                 # tsc + autolinking
pixi run just react-native::quickstart-typecheck
pixi run just fmt && pixi run just lint                       # clippy -D warnings, ruff, profiles
```

## 真机结果（Android，同一候选）

`.work/android-native-sequence/2026-11-24-e`：13/13 场景 `status=passed`、`integrity=passed`、
`host_gate=pending`。

| 项 | 值 |
|---|---|
| 设备 | OnePlus PHB110 `3ac5cc09`（rooted） |
| App APK | `df1ac6f3984f6b42001defb3a225c5e552c3f6c6565e06a6e45c8662a2b4f5b5` |
| test APK | `0ab5572c401a9a5f0d1ea2f8b795f7b3a9e09bd3a33b56129a0dc746f03fe289`（未变：Kotlin 未改） |
| fixture | `jlink:1050252028` / `/dev/cu.usbmodem0010502520281`，multifunction v2-a |
| 序列顺序 | scan, notification, indication, background, io, **disconnect**, cancellation, write-reject, ccc-enable-reject, ccc-disable-reject, passive-disconnect, recovery, recovery-ten |

`disconnect` 单跑（`.work/android-native-disconnect/2026-11-24-a`）与序列内跑都拿到完整 15 项 trace：
`… link:operator-ended, notify:ended, link:silent, controls:withdrawn, closed`。场景片耗时 ≈ 25 s。

同批 12 个旧场景全部重挣，说明 `settleEnd` 的改动没有动到 `passive-disconnect` / `recovery` 的语义，
也没有在 `notification` / `indication` / `ccc-*-reject` 上留下回归。

## 未验证 / 下一步

- **iOS 未跑**。App 源码改了 `useManualScan.ts`，所以 iPhone 的设备测试包必须重建；
  重建后用同一份计划跑 `formal-host-ios-managed-disconnect`，也可以直接用
  `formal-host-ios-scene-sequence` 跑完整序列。在 iOS 通过之前，`disconnect` 只有 Android 证据。
- 剩余三条候选场景（`dual-subscription`、`radio-loss-live`、`connect-failure`）尚未实现。
