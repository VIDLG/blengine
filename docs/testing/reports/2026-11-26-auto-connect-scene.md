# `auto-connect` 场景：对端不在时提出的"出现即连"请求

日期：2026-11-26
设备：OnePlus PHB110 `3ac5cc09`（rooted）；DK-A `jlink:1050252028`（v2-a）
范围：第 18 个 managed 场景 `auto-connect`，以及为它新增的 **`Probe` 页**。状态：Android 真机通过、
`integrity=passed`；**无 iOS 设备证据**。

## 为什么是这一条

`beginAutoConnect()` 与 `beginConnect()` 的区别只有一句话：前者是**常驻请求**——对端现在不在，
平台也一直等到它出现。所以场景必须把"现在不在"变成事实，否则测的还是普通 connect：

```text
open   : 扫描并匹配 fixture → 停止扫描（拿到对端身份）
host   : 撤销广播（controller 回读 advertising bit = clear）
arm    : tap Auto-connect → auto=pending, link=connecting
host   : 恢复广播
settle : auto=done, link=connected
```

`peer:withdrawn` 由 host 执行并在 fixture 侧回读，App 完全不知情；因此后面出现的链路只能来自那个
常驻请求。

## App 面：新开 `Probe` 页，而不是继续加高 console

`CommandConsole` 是固定高度预算，已因加控件回归两次（`link-facts` 挤掉 `disable-notifications`、
`descriptor-io` 挤掉自身控件）。而 `auto-connect` 这类能力风险等级也不同（radio、配对、companion
会弹系统 UI），不该和 17 条观测场景共用同一块 pinned 区。

因此导航变为 `Tests / Current run / Probe / Evidence`：

- `Probe` 有自己的 `probe-console`（pinned，不滚动）；
- 主线 17 条 plan **一行未改**；
- `run-status`、`start-session` / `close-session` 在**所有页面** pinned（会话控制不再依赖"恰好是
  内容第一项"）。

## 唯一一次失败：plan 少了一条页面声明

首跑失败于：

```text
scene step 24 {"id":"auto-connect","op":"tap"} failed: timed out before auto-connect presence
```

控件在 Probe 页，而 plan 停在 Current run。修法不是"把按钮挪回去"，而是让场景**声明自己所在的页面**：

```json
{"op":"tap","id":"workbench-probe"},
{"op":"wait","id":"workbench-page","label":"Probe","timeout":30},
```

这样"控件换页"会变成具名失败，而不是 90 秒超时。这是 `workbench-page-architecture.md` 里 R2 的第一
个落地实例。

## trace 与账本

```text
idle, started, scan:running, scan:advertisement-matched, scan:stopped,
peer:withdrawn, auto:answered, closed
```

trace 只说"平台回答了"，不写"连上了"——因为 Apple 没有这个能力，会以 `not_supported` 拒绝。
两种答案由派生分别钉住：Android 必须 `auto=done` 且 `link=connected`；iOS 必须 `auto=unsupported`
且 `failure` 带平台自己的 `not_supported` 码、`link=none`。超时/裸 `failed` 都不是答案。

账本两种形状：Android 有 `auto-connect` + `auto-connect-wait` 且全 succeeded；iOS 只有被拒绝的
`auto-connect`，且其 detail 必须点名 `not_supported`（`_require_auto_connect_ledger` 显式接受这一种）。

## 真机结果（Android）

| 项 | 值 |
|---|---|
| 运行 | `.work/android-native-auto-connect/2026-11-26-b`，`status=passed`，trace 8/8，场景片 19 s |
| arm 相 | `auto=pending;link=connecting;failure=none` |
| settle 相 | `auto=done;link=connected` |
| 离线 | `integrity=passed`、`cleanup_errors=[]` |
| App APK | `54e43818…`；test APK `0066f06b…` |
| ledger | 9 行全 succeeded，`omitted=0` |

## 守卫（不需要设备）

- `test_scene_semantics.py`（+4）：常驻请求被回答后链路出现；无该能力的平台以自身码拒绝；
  `pending`（超时）不能通过；`unsupported` 若不是平台码不能通过。另断言 host 动作顺序恰为
  `withdraw → resume`。
- `AutoConnect.test.tsx`（+3）：Probe 页提交并记账、`unsupported` 不冒充失败、无 peer 时控件禁用。
- `test_workbench_ids.py`：`workbench-probe` / `probe-console` 进入词表；滚动内容守卫同时扫描
  `App.tsx` 的内联 content。
- `test_scene_sequence.py`：顺序钉住 `connect-failure < auto-connect < cancellation`。

## 尚未完成

- **iOS 设备证据已有**（2026-11-26）：`auto-connect` 与其余六条最新场景在真机上通过，iOS 分支以
  平台自己的 `bleng.dispatch_rejected.not_supported` 结束（落在操作终态，不是提交）；见
  [`2026-11-26-ios-seven-scenes.md`](2026-11-26-ios-seven-scenes.md)。
- 全序列现为 19 条（后续加了 `devices-kinds`，见
  [`2026-11-26-devices-kinds-scene.md`](2026-11-26-devices-kinds-scene.md)），仍不声称连续通过
  （`background` 的老问题未定位）。
- `Probe` 页后续成员：`adapter-control`（有 HANS OFF / UiA2 卡顿风险，单独立项）、`bond`（需固件
  SMP）、`companion`（需人工确认）、`adopt-peripheral`（需 Central 角色 peer）。
