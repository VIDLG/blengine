# Android Appium HANS full lifecycle：同 APK 完整 UI 生命周期通过

日期：2026-11-09。接续[HANS start-hold control](2026-11-09-formal-android-appium-hans-start-hold.md)、[HANS idle navigation](2026-11-09-formal-android-appium-hans-idle-navigation.md)与[同 APK Appium 对照和恢复](2026-11-09-formal-android-appium-comparison-recovery.md)。本轮只记录已完成的 reviewed-ROM 单 UID HANS full lifecycle candidate；不修改生产 BLE/Rust、Appium settings、DUT BLE permission/radio 或 firmware，不重复执行真机场景。

## 1. 结论

**HANS 条件下，同一 reviewed APK 的 Appium full UI lifecycle 通过。** 在同一已审计 OnePlus/Oplus ROM、同一 managed Appium/UiAutomator2 helper 身份和同一设备上，完成：

```text
idle → started → closed
```

本次 sealed result 的状态为：

- `scenario_status=passed`
- `session_status=passed`
- `phone_status=passed`
- `status=passed`
- `host_gate_status=pending`
- `rf=not_run`
- `primary_error=null`
- `cleanup_errors=[]`
- `phone_cleanup_errors=[]`

这证明了**这一受控 HANS session** 可以完成 Appium Start → Current run → Close 的 UI lifecycle，并完成 host cleanup。它不证明 RF、native Close/resource-zero、Hermes reload、ABI、release/Phase gate，也不证明 HANS 是所有历史 Appium timeout 或 `socket hang up` 的唯一根因。无 HANS 的原 failed candidate、Maestro pass 和 transport-only pass 均保持原 verdict，不相互升级或转移。

## 2. 候选和身份

| 项目 | 实际观察 |
| --- | --- |
| run / scope | **`3c1165a4-1b1d-4107-bef0-6eb3319d4a16` / `formal-android-managed-lifecycle`** |
| bundle | `.work/formal-android-appium-hans-full-lifecycle/3c1165a4-1b1d-4107-bef0-6eb3319d4a16/` |
| device | `3ac5cc09` |
| APK | `c96fc9909ed056e33e1ce47927639da742614294a60114d8ae546ee49255a584` |
| App | `com.blehubblenghost`，PID **10977** / UID **10378** / start ticks **252346347** |
| UiA2 server | `io.appium.uiautomator2.server`，PID **11544** / UID **10255** / start ticks **252347185** |
| boot | `35df0f9f-6c54-42b0-800c-3c410469b6e7` |
| Appium/UI driver | Appium 3.9.0 / UiAutomator2 driver 8.7.3 / server 10.7.6；沿用 managed helper identity |
| ROM evidence | `/system/framework/oplus-services.jar`，SHA256 `889bdf044f565d94a4fb603402a0f7f3d394750f2d151c29eaa5122a25b09a24` |
| startup snapshot | `state=poweredOn`、`scan_authorization=denied`、`connection_authorization=denied` |
| sealed manifest | 498 个 file entries；manifest SHA256 `5f8851e55132ea77202301ee524a8cb85dbdc73987b08241a7e0a8e27eaa85c3` |
| sealed result | result SHA256 `ebd05ca6c2ac60776f8c26113250ef7c74abae93b32fd239595524f097305f84` |

App 页面按默认策略保留。当前成功 App 的 exact identity 是 PID **10977**、UID **10378**、start ticks **252346347**；后续任何设备 run 前必须先在独立授权下 fresh exact-identity retirement，不得直接 force-stop、接管或按旧 PID 推断身份。

## 3. HANS 事务和恢复

本次只对白名单目标 UID `10255` 的 UiA2 server 做 ROM-specific HANS classification mutation；没有同时启用 freezer、power、deviceidle exemption，也没有修改 DUT BLE permission/radio。

- ROM 与安装 helper path/hash、boot、package/UID inventory 和 fresh selector 均通过 admission 检查。
- UiA2 server classification：**THIRD → WHITE → THIRD**。
- 原始 debug property 是空字符串，`debug_restored=true`。
- `other_classifications_changed=false`；其他 UID classification 保持不变。
- HANS result 的 `scope` 明确为 `classification only; reconstructed transient state/timestamps not restored`，因此不声称恢复瞬态 map/timestamp。
- HANS restore 在 session finalization 前完成；server/App cleanup、helper restoration、candidate finalization 和 phone lease release 均成功。
- 当前共享 ADB listener 仍为 5037 PID **16841**；5038/8237/8937 无 listener；phone lease 已释放。

完整 HANS command/intent/result/restore receipts 和 readbacks 均保留在 sealed bundle 的 `platform/` 下。初始 intent 中可能出现的未完成 mutation intent 形状是历史 receipt，不得删除或通过重封 bundle 改写。

离线验证命令：

```sh
pixi run just react-native formal-host-lifecycle-verify \
  .work/formal-android-appium-hans-full-lifecycle/3c1165a4-1b1d-4107-bef0-6eb3319d4a16
```

验证输出为 `verification=offline_integrity`、`integrity_status=passed`，且 scenario/session/phone/status 仍分别为 `passed/passed/passed/passed`；`host_gate_status=pending`、`rf=not_run` 未被升级。日志：`.work/formal-android-appium-hans-full-lifecycle-verify.log`。

## 3.1 离线请求时间线：可恢复启动 hangup 与致命活动 session stall 分开

随后对已封存的 Appium logs 做只读对照，修正“socket hang up”这一词的范围：

- HANS start-hold 的 UiA2 启动阶段第一次 `GET /status` 也出现一次 `socket hang up`，约一秒后同一 server 的 `/status` 返回 200，随后 session、element、click 和正文读取均成功。这是可恢复的启动瞬态，不是 HANS 把所有 socket hangup 消除了。
- 原同 APK failed full lifecycle 的 fatal 窗口在 `.work/formal-android-appium-post-close/353bf795-e38b-4a6f-9818-a99ed508080b/automation/appium.log`：前置多次 `displayed` 查询仍返回 200；最后一次 Evidence 页面 marker 的 `displayed` 请求返回 HTTP 500、耗时 **11263 ms**，错误为 `socket hang up`。紧接着 DELETE session 没有响应，Appium 记录 UiA2 instrumentation 已不再运行。
- transport-only candidate 也记录过启动期 socket hangup，但其后四项 query 全部 200，DELETE 返回 200；因此不能把任意一次启动 hangup 当作本次 lifecycle 根因。

当前最窄、可证实的边界是：**无 HANS 条件下，UiA2 在已建立活动 session 的后段进入不可响应/冻结状态，导致 element 请求和随后 DELETE 一起失败；启动期可恢复 hangup 是另一个现象。** `do_freezer_trap` 的 post-failure snapshot 与 HANS 下的成功差异支持 OEM process policy 是重要线索，但仍不是严格单变量因果证明。该结论不支持修改 RN/Rust Close 或把通用 retry/timeout 当作根治。

## 4. 与其他自动化方案的边界

| Candidate | 结果 | 结论边界 |
| --- | --- | --- |
| 原同 APK Appium lifecycle | failed；Start 后 Evidence `displayed` 查询出现 `socket hang up`，之后独立恢复 | 保留原失败和恢复事实，不重跑、不改写 |
| Appium transport-only | passed | 只证明四项 transport/query scope，不包含 Start/Close/scan/RF |
| 无 HANS start-hold | failed/quarantined；Start 前标题查询 timeout；后失败 server 有 freezer 快照 | 证明该条件下 automation stall 候选事实，不证明唯一因果 |
| HANS start-hold | passed | `idle → started → 5s hold → closed`，无完整 navigation/Evidence |
| HANS idle navigation | passed | idle Tests/Evidence/Current run 导航通过，不包含完整 lifecycle |
| Maestro 2.12.1 lifecycle | passed | 同 APK UI lifecycle comparison pass；Maestro 是 opt-in comparison，不改变默认 Appium |
| HANS Appium full lifecycle | passed | 本报告的 `idle → started → closed`；仍不是 RF/native/release gate |

HANS 条件改变了 OEM process policy，因而不同 candidate 不是严格单变量实验。当前证据支持“在已审计 ROM 上，显式单 UID HANS workaround 能使该受控 Appium lifecycle 完成”，不支持“所有 Appium 问题都由 HANS 修复”或“Appium 普遍优于 Maestro”。Detox 仍只有源码 feasibility，没有本项目编译或真机 pass。

## 5. 软件回归和后续边界

本轮不新增生产实现，也不把 HANS 变成 Android 通用默认策略。此前已通过的 Appium transport、binding/evidence/CLI/recipe/lifecycle、candidate regression、HANS/evidence/CLI regression、formal host Jest/typecheck/autolinking 以及 fmt/lint/Clippy/Ruff/Mermaid/Markdown/diff 检查仍作为软件基线；这些不替代本候选的真机身份、HANS restore 或 host cleanup receipts。

当前正确状态是：

- Appium 默认 driver 的 UI lifecycle 在 reviewed-ROM HANS 条件下有一份有效 candidate pass；
- Maestro 2.12.1 有一份独立 lifecycle comparison pass，仍为 opt-in；
- Detox 未进入采用或真机 gate；
- scan/RF、真实 peripheral fixture receipt、native Close/resource-zero、reload/recovery、ABI/package 和 release/Phase exit 仍 pending；
- 不再执行第二个手机 lifecycle 来“确认”本次 pass；如需进入 scan/RF，必须按计划重新做 fixture identity、firmware receipt、lease 和独立 RF authorization。

成功页面默认保留。任何下一次手机操作必须先对 PID **10977** 做完整 exact-identity retirement，并保留独立 retirement evidence；不能把页面消失或 PID 消失当成 native resource-zero 证明。
