# 功能收敛：Android 断连回收接线与 BLeng 完成结果保持

日期：2026-10-26

## 结论与范围

承接 [平台 loss 审计](2026-10-26-platform-loss-functional-convergence.md)，对应 [Plan F05/F12/F15 与 §16.1.9](../../../.plan/PLAN.md)。本轮不是新增 BLE 功能、公开 API 或第二套测试框架，也不改变已批准的取消/恢复策略。

- Android：生产 connection admission、双索引和断连 retirement 使用同一个内部 owner，确定性测试走真实 owner，而非手工 `replaceRoute()`。补齐“所属工作退役 → 双索引释放 → 同 Peripheral 显式重新受理 → 旧回调隔离”的软件证据。
- BLeng：审计证明三个具体边界缺陷，按 red → 最小实现 → green 修复：完成结果被后续 loss 覆盖、完成 disconnect 的取消分类错误，以及完成结果尚待取消回执时被正常 close 丢弃。没有发现需要重做 actor/owner/reservation 架构的证据。
- 当前检查范围内，11 类 typed operation 没有发现新的基本功能缺席；这不保证审计已穷尽全部竞态，也不关闭真实平台 loss 或 iOS rejected-disable continuity。

后续初始化/扫描与结果路径复核发现的 snapshot 发布顺序问题及更新后的整合结果见 [初始化发布审计](2026-10-26-bleng-initialization-publication.md)。下文保留本候选当时的范围、失败和计数，不用新结果覆盖历史事实。

本轮没有安装或启动手机 App、执行 ART、切换 radio/权限、使用 Peripheral、刷固件、运行长稳或 commit/push/branch。旧 iPhone/Android RF 与 runtime 记录仍属于原候选，未转移成当前候选通过。

## 1. Android：真实 admission 与 retirement owner

### 实现边界

从 [AndroidBleBackend](../../../platforms/android/library/src/main/kotlin/dev/blehub/android/AndroidBleBackend.kt) 提取 [AndroidConnectionLifecycle](../../../platforms/android/library/src/main/kotlin/dev/blehub/android/AndroidConnectionLifecycle.kt)：

- 唯一持有原 connection-ID / Peripheral-ID 双索引和 generation；backend 不再保留另一份索引。
- `connect()` 在原 state lock 内调用其 `reserve()`；原 handler submission 和失败 rollback 保持。
- 原 `finishConnectionDisconnected()` 调用其 `disconnected()`：锁内移除 owner、退役 GATT/I/O/watchdog，锁外 native close，然后按原顺序发布 pending terminals 与 connection outcome。
- disconnect、watchdog、discovery invalidation、dispose 等原入口复用同一 GATT retirement；不改变 native timeout、cancelability、barrier 或 cleanup 策略。
- 该内部 owner 不新建 Handler、executor、timer、重试或自动重连。Framework 调用仍由 backend 执行。

这是为可测性明确既有生产职责的提取，不是修复一个已证明的 Android 回收 bug。测试首次编译存在 fixture 类型推导错误，修正显式类型后行为测试即绿；编译错误不记为生产 red。

### 代表性断言

[AndroidConnectionLifecycleTest](../../../platforms/android/library/src/test/kotlin/dev/blehub/android/AndroidConnectionLifecycleTest.kt) 两个测试保护：

1. `lossRetiresOnlyItsOwnerAndExplicitAdmissionReusesPeerWithoutOldCallbackContamination`
   - 活动 response write + queued CCC、已本地完成但仍占 native slot 的 no-response write + queued CCC、活动 CCC + queued write；输入普通断连与 status 133。
   - pending 命令按原顺序各失败一次；no-response 已发出的本地成功不被第二个 terminal 改写。
   - watchdog、subscription、session 与 pending lane 退役；另一 Peripheral 的 active write/watchdog 保留且可以完成。
   - 断连前同 Peripheral / 重复 connection ID admission 被拒；断连后用新 connection ID 显式 reserve 同 Peripheral 成功，generation 递增，保持独占。
   - 旧 callback、重复 loss 和旧 route removal 不影响新 owner。额外用 caller 重用 connection ID 的防御性输入验证 exact route identity；这不改变产品要求的 fresh connection identity。
2. `disconnectCallbackKeepsConnectAndExplicitDisconnectOutcomesDistinct`
   - connect 尚未完成 / 已完成，以及普通 / 错误断连；未到 Connected 不伪造 connect 成功，explicit disconnect 保留自己的 command identity、domain/terminal 和 error。

**证明边界：**测试执行 production connection owner 和 write/CCC lifecycle，以受控端口供应 native close 与 callback，不是构造真实 Context/Handler/BluetoothGatt。连接已建立是显式输入，随后新连接的验证截至生产 admission，并未执行新 `connectGatt()`、discovery 或 RF。真实 callback 的 exact GATT gate 仍在 backend，本测试不将其外推为已验证。真实系统 loss / same-Hub RF 仍 open。

实际命令：

```sh
pixi run just ffi android-backend-check --tests dev.blehub.android.AndroidConnectionLifecycleTest
pixi run just ffi android-backend-check
```

定向 **2 passed**；完整 Android backend JVM **38 passed / 8 suites，0 failed / errors / skipped**；debug/release AAR build passed。日志 `.work/validation/2026-10-26-android-connection-lifecycle-{targeted,regression}.log`；最初编译尝试另存 `2026-10-26-android-connection-lifecycle-compile-attempt.log`。

## 2. BLeng：完成事实、结果交付与连接状态分开

### 2.1 完整结果不能被之后的 loss 覆盖

原顺序：原操作 accepted → 请求 cancel，回执未决 → 原完整成功/typed failure 已确认 → passive loss → cancel 被拒。actor 原本暂缓原结果交付，但 loss 无条件以 `Disconnected` 领取该 waiter；connect 又会根据后来的 disconnected evidence 重算结果。因此 operation 已完成，却因回执等待窗口丢失原结果。

首次修复仅保护 `Cancelled` terminal。复核发现这不是完整根因修复，普通成功/失败仍受影响；现有 publication 测试虽保护 FIFO，却把此时结果写成 `Disconnected`。本轮保留 FIFO、容量、唯一性及其他 scope 断言，纠正错误结果预期，并增加独立成功/typed failure 与部分证据对照。

最终修改：

- [waiter.rs](../../../crates/bleng/src/waiter.rs)：在既有有界 `WaiterEntry` 内保存首次完整 `deferred_result`，复用原 oneshot；不新建 registry/channel/tombstone，不解析 BLE 事件。
- [actor.rs](../../../crates/bleng/src/runtime/actor.rs)：reservation 仍唯一确认完整证据与 completion observer；回执拒绝后交付保存结果，不依据后来的 connection 状态重新计算。
- loss 仍立即记录，未完成 sibling 立即 `Disconnected`；无证据、部分证据和 loss-first 的目标同样立即失败。不为等待取消回执延迟连接退役。
- timeout、caller retirement 与 receiver drop 仍按原 policy 清理 waiter；fault 仍为 `TransportFault`。仅 `Cancelled` 的临时特例已删除。

代表性回归位于 [publication.rs](../../../crates/bleng/src/runtime/tests/publication.rs) 的 `completed_loss_*` / `cancel_loss_*` 与 [waiter_tests.rs](../../../crates/bleng/src/tests/waiter_tests.rs)。一般结果保持的 red：publication **10 passed / 6 failed**；green **16 passed**。日志 `2026-10-26-bleng-completed-loss-{red,green,regression}.log`；较早窄化修复日志 `2026-10-26-bleng-cancel-loss-{red,green,regression}.log` 继续保留，不当作最终根治证据。

### 2.2 完成的 disconnect 不再被保留中的 root 误判

当 sibling 的取消回执未决时，connection root 会继续保留。即使 disconnect 已获得完整成功证据，其 slot 仍存在，旧 `request_cancellation()` 却无条件返回 NotCancellable。

[connection.rs](../../../crates/bleng/src/runtime/reservations/connection.rs) 现在先检查 disconnect slot 是否完整完成：完整完成为 `OperationAlreadyCompleted`；pending 或仅部分证据仍为 `OperationNotCancellable`。没有新增 native disconnect cancellation 能力。

`cancel_loss_completed_disconnect_is_completed_while_sibling_control_retains_root` 覆盖 domain/terminal 两种顺序、部分证据、完整结果、保留 root 与重复回执。

### 2.3 正常 close 保留尚待 control 的已完成结果

同一结果保持审计还发现：正常 close 会先通知 lifecycle，再由 coordinator 停止/释放 actor。完整结果虽已确认，若仍待 control 回执，原 waiter close 无条件丢弃它，已挂起的 typed wait 也可能先被 lifecycle 唤醒为 Closed。

- 正常 `WaiterRegistry::close` 将已保存结果交付到原 oneshot；未完成目标仍 Closed；fault 仍覆盖为 TransportFault。
- [operation.rs](../../../crates/bleng/src/runtime/operation.rs) 使用同一 completion observer，在正常 close 与 actor 交付之间保留已完成 typed result；不把该窗口当成未完成工作。
- control sender 关闭时重新检查 lifecycle，不把正常关闭误报为 TransportFault；不改已存在的 control 回执优先级。

[owner cancellation tests](../../../crates/bleng/src/runtime/tests/cancellation.rs) 的 `completed_close_*` 使用受控 native call 和 actor barrier，覆盖已挂起/未领取、成功/typed failure、fault、cleanup once 与 late ingress Closed。行为 red **1 passed / 4 failed**；green **5 passed**。

整合 workspace 首次发现一个新增测试时序问题：测试先释放 native cancel，尚未确认 actor 停止，原拒绝回执可能获处理，精确 Closed 断言失败。修复仅在测试先等待现有 actor-stop seam，再释放 native，让 dispose 完成；未宽泛接受两种结果或为测试修改生产优先级。原失败日志 `2026-10-26-connection-cancel-convergence-workspace.log` 保留。定向 8 test threads、有限重复 10 次，每次 5 passed；这是稳定测试时序验证，不是 BLE 长稳。

日志：`2026-10-26-bleng-completed-close-{red,green,regression}.log`、`2026-10-26-bleng-completed-close-ordering-{parallel,regression}.log`。

## 3. 最终验证与未关闭范围

| 实际命令 | 结果与边界 |
|---|---|
| `pixi run just test bleng` | **220 passed / 3 suites**；不与 workspace 重复累计。 |
| `pixi run just test all` | 修正新增测试时序后 **322 passed / 18 suites**；完整输出见 `2026-10-26-bleng-completed-close-ordering-regression.log`。 |
| `pixi run just binding bleng-snapshot-check` | 9 tool tests / 3 reviewed snapshots verified；没有新增公开签名或手改 snapshot。 |
| `pixi run just apple consumer-bleng-macos` | 当前 XCFramework 五 target/三 slice、component symbols、artifact-only generated Swift consumer runtime passed；不等于 iPhone runtime/RF。 |
| `pixi run just ffi android-bleng-build` | 当前双 ABI AAR single-image audit 与 artifact-only consumer/test APK static build passed；未执行 ART。 |
| `pixi run just fmt` / `pixi run just lint` / `pixi run just docs-check-mermaid` / `git --no-pager diff --check` | 通过。 |

本轮整合日志前缀：`.work/validation/2026-10-26-connection-cancel-convergence-{snapshot,macos-consumer,android-bleng-build,fmt,lint,mermaid,diff}.log`。所有通过只适用于该命令及候选；早期不完整修复的 green 和整合失败均保留。

**剩余项不是新增基本 BLE 功能清单：**production callback/native setup 的完整接线、Apple capacity timer orchestration、真实 write/CCC radio/permission loss 与 same-Hub RF，以及 iOS rejected-disable continuity 的解决或明确限制裁决，继续按平台 loss 报告保持 open。公开 RN surface、ABI freeze、最终同候选设备闭环及长稳仍遵从 Plan 的后续顺序。本轮不能单独宣布已达到集中 review 或 release/Phase exit 条件，也不因发现局部状态机问题推导需要重写 BLeng 架构。
