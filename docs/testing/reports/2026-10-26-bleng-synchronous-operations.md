# BLeng 同步 operation 提交与异步 wait/cancel

日期：2026-10-26

**后续 public identity 决策：**用户随后批准为全部 11 类 operation 增加只读同步 `command_id() -> CommandId`，用于真实 command/event/error/log 关联。同步提交、wait/cancel 与 native capability 边界不变，但新增方法改变未冻结 ABI，当前候选验证见 [operation 关联身份](2026-10-26-bleng-operation-command-id.md)；下文包/runtime 结果仍只属于原同步提交候选。

## 决策与范围

用户明确批准以同步创建/提交 operation、异步领取结果和取消替代旧 async begin，不考虑旧签名兼容。本轮落地 [Plan §16.1.9](../../../.plan/PLAN.md#1619-已确认的补充决策)，不建立第二份架构计划。此前 [facade 整理报告](2026-10-26-bleng-facade-layout.md) 保留移动及 async begin 统一时的候选事实，不再描述当前签名。

本次没有改动 BleHub 的薄核心职责、平台 native cancellation 能力或固件。未运行手机、radio/权限、RF、ART、签名准备或长稳，没有 commit/push/branch。

## 1. 当前公开模型

```rust
let operation = bleng.begin_read_characteristic(connection, session, characteristic)?;
let value = operation.wait(2_000).await?;
// 另一个任务可持有同一 operation 并调用 cancel().await；不占用结果槽。
```

- 全部 11 类 `begin_*` 为同步函数，只有操作参数，无 timeout，返回 `Result<Arc<具体 operation>, BlengError>`。
- begin 成功表示进入有界 submission mailbox，不表示 actor admission、backend acceptance 或 BLE completion。请求无需 wait 就会推进。
- wait 为 single-consumer；非法 timeout 不消费结果，第二次有效 wait 返回 OperationResultConsumed。
- typed wait 的 deadline 在调用方首次 poll 时计算，包含 private-runtime 调度延迟及尚未完成的 admission。convenience 保留异步返回值接口，提交和等待共用同一个 absolute deadline。
- drop/timeout 只关闭 caller delivery，不撤回 eager request、不隐式 native cancel。native reservation 仍依据自身证据/清理退役。
- typed 已完成结果跨正常 close 保留；pending close 返回 Closed，fault 优先。handle 可保活执行器以领取结果，但不能保活逻辑 Hub。

### 为什么 begin 仍返回 Result

begin 是“创建并提交”，不是必定成功的纯内存构造。保留同步错误可以明确表达请求根本没有进入执行入口，不必制造一个已失败的 handle，也不引入无限队列。

| 失败边界 | 当前交付位置 |
|---|---|
| Hub 未组装、已关闭、runtime 已 fault | begin 同步拒绝；并发 close/fault 仍可能在提交成功后由 wait 观察到 |
| submission mailbox 满 | begin 返回 ResourceExhausted / SubmissionQueue，仅拒绝本次提交，不 fault Hub |
| command ID 耗尽 | begin 返回 ResourceExhausted / Identifiers，绝不回绕复用 |
| actor 的 NotInitialized、Busy、旧 connection/GATT/subscription、waiter 容量 | begin 已提交，wait 领取精确 admission error |
| backend dispatch 拒绝、异步 BLE 结果 | wait 领取原有 typed error/result；dispatch acceptance 不升级为 BLE success |

无旧 async begin/timeout 重载或兼容 alias。Rust、Swift、Kotlin ABI shape 有意改变，必须重生成并锁步消费新 bindings/native image；旧真机记录不自动转移到本候选。

## 2. 单一机制及有界性

- [facade/mod.rs](../../../crates/bleng/src/facade/mod.rs) 的每种操作只保留一个私有 `submit_*` 参数映射，公开 begin 与 convenience 共用。
- [owner.rs](../../../crates/bleng/src/runtime/owner.rs) 直接调用 Kameo bounded `tell(...).try_send()`；mailbox 当前容量 64，与 ingress/control 共用。没有每个 begin 的后台 admission task、阻塞 actor 等待或 caller-thread native 调用。
- command ID 在提交时经唯一共享 atomic allocator 分配；资源 ID 由 actor 在独立 namespace 分配。并发分配与实际入队可乱序，registry 接受唯一非零 ID，不要求到达顺序等于数值顺序。失败允许 ID 间隙，不回滚、不复用、不回绕。
- [OperationSubmission](../../../crates/bleng/src/runtime/owner.rs) 共享身份、一次性 sender 和 actor 写入的 resource/completion metadata。原 actor oneshot 仍是唯一结果通道；[OperationTicket/ResultSlot/OperationControl](../../../crates/bleng/src/runtime/operation.rs) 继续分离 delivery 与控制。
- actor/reservations 仍唯一管理 BLE admission、reservation、证据及 native retirement，没有 facade 侧第二套 BLE 状态机或路由 registry。
- receiver 关闭不再依赖 RetireWaiter 消息。registry 在注册时清除关闭的 delivery entry，内部容量仍有界，最终 teardown 统一清理；没有异步 retirement fallback 或无限 tombstone。可靠 ingress 已接受后无法入队的原 fail-closed 策略不变。

## 3. cancel、拒绝与 dispatch claim

本地 PENDING 只有一个原子竞争点：

```text
PENDING
  -> CANCELLED        handle cancel 先赢：回执接受，wait 得 Cancelled
  -> LOCALLY_SETTLED  admission 拒绝先赢：wait 得原错误，cancel 得 AlreadyCompleted
  -> CLAIMED          actor 先赢：安装 reservation，再派发 native 工作
```

取消先赢时没有 native reservation/命令，也不伪造 BLE terminal。claim 后不再走本地 shortcut，按既有 actor/native control 回执结算；初始化/disconnect 保持 NotCancellable 策略。立即拿到 handle 不等于必然能在 claim 前取消，取决于真实线性化顺序。

typed handle 的 predispatch cancel 不消费结果；wait 已超时或被 drop 后仍可显式取消未完成目标。`Bleng::cancel_operation(id)` 保留 actor-routed control，不冒充 handle 的本地 predispatch capability。完成、取消回执、delivery 消费与 native 退役是独立事实。

## 4. 测试先行与回归

| 保护边界 | 实际过程 / 结果 |
|---|---|
| 同步 API 编译、无 begin timeout | 先写同步调用时结构编译红，再实现；不是声称旧行为 bug |
| admission 拒绝与 cancel 竞争 | 定向测试先失败：拒绝已结算仍可错误返回本地取消接受；改为同一 CAS 裁决后通过 |
| mailbox 满时普通 wait timeout | 定向测试先失败：旧 retirement 消息导致 fault；改为 receiver-close 后两条 deadline 路径通过，不削弱 ingress 丢失的 fail-closed |
| private-runtime 调度耗时 | 实际阻塞全部私有 executor workers，先 poll wait，再越过 deadline 后释放；旧实现得到已结算 NotInitialized 而非 Timeout，修复 caller-side deadline 后通过。通道 gate 有界且析构可释放，不靠 sleep 推断任务是否启动 |
| eager/drop/no-wait | actor gate 下 drop handle 不撤回请求，释放后 native entry 一次；无 caller runtime 也可同步提交，native 不在 caller thread 执行 |
| bounded/concurrent IDs | 8 个线程向被 gate 的 actor 提交 64 项，ID 唯一且拒绝不制造容量；queue full 精确拒绝，取消与 delivery drop 不额外占 mailbox |
| preclaim/postclaim | 注册后 claim 前取消立即结算且不派发；claim 先赢保留 native NotCancellable 策略 |
| wait/cancel/close/fault | 消费一次、并发 wait/cancel、完成未领取、queued admission error、本地取消、pending close 和 fault precedence；timerless caller runtime 仍可 wait/cancel |

主要入口：[facade tests](../../../crates/bleng/src/tests/facade_tests.rs)、[owner tests](../../../crates/bleng/src/runtime/tests/owner.rs)、[lifecycle tests](../../../crates/bleng/src/runtime/tests/lifecycle.rs)、[外部 Rust consumer](../../../crates/bleng/tests/typed_operations.rs)。没有删除独有的 native reservation/结果保持/旧代安全断言；测试简写仅编入测试。

red/green 日志在 `.work/validation/2026-10-26-sync-operation-{red,admission-cancel-red,admission-cancel-green,retirement-red,retirement-green,wait-deadline-red,wait-deadline-green}.log`。中间候选的 241/343、242/344 等计数保持原记录，不作为最终计数。

## 5. Foreign/package 和最终候选验证

[Swift typed consumer](../../../tests/consumers/apple/bleng-macos/Sources/BlengArtifactConsumer/TypedOperations.swift) 与 [Kotlin typed consumer](../../../tests/consumers/android-bleng/app/src/androidTest/kotlin/dev/blehub/bleng/PublishedBlengTest.kt) 已迁移至同步 begin；保留 11 类 typed output、并发控制/结果、shutdown 等断言，并检查 closed owner 同步提交被拒。iOS unsigned consumer 共用 Swift regression 源码。snapshot 工具的代表性 bindings/断言同步迁移，仍精确要求全部 handle，不放宽 allowlist。

| 实际执行命令 | 结果与边界 |
|---|---|
| `pixi run just test bleng` | 最终 deadline 候选 243 passed / 3 suites |
| `pixi run just test all` | 整合最终 345 passed / 18 suites，包含 BLeng，不重复相加 |
| `pixi run just binding bleng-snapshot-update` | 用户批准的 breaking shape 通过真实生成物更新三语言 snapshots，不手改生成 bindings |
| `pixi run just binding bleng-snapshot-check` | 9 checker tests passed，3 reviewed snapshots verified；首轮 checker 仍期待 async begin 的失败已保留 |
| `pixi run just apple consumer-bleng-macos` | 五 target/三 slice package、single-image 与 artifact-only generated Swift runtime passed |
| `pixi run just apple consumer-bleng-ios-unsigned` | device + Simulator artifact-only unsigned build passed，不是签名/真机运行 |
| `pixi run just ffi android-bleng-build` | arm64-v8a / x86_64 single-image AAR 与 artifact-only consumer/test APK static build passed，未运行 ART |
| `pixi run just fmt` / `pixi run just lint` | 通过；typed wait 改为 absolute deadline 后，仅测试使用的相对 wait 简写已限定为 test-only，不保留无用生产入口 |
| `pixi run just docs-check-mermaid` / `git --no-pager diff --check` | 27 个图 / 180 个 Markdown 文件检查通过；diff 无 whitespace error，仅三个既有文件的 CRLF→LF 提示。6 个本轮 authored Markdown 文件通过 UTF-8、fence 配对、whitespace、conflict marker 与最终换行检查 |

最终日志：`.work/validation/2026-10-26-sync-operation-{bleng-integrated-final,workspace-integrated-final,snapshot-check-final,apple-consumer-final,ios-unsigned-final,android-build-final,fmt-integrated,lint-integrated,mermaid}.log`。Android 分步骤完整输出由 `.work/android-bleng/logs/` 保存。

## 6. 没有关闭的边界

本轮收敛同步提交和结果/控制架构，不证明所有平台取消可用，也不新增 Android ART、iPhone runtime 或 RF 通过。write/CCC 系统 loss、same-Hub 显式恢复、真实 capacity stall 与最终同候选发布闭环继续按 [平台 loss 关闭条件](2026-10-26-platform-loss-closure-readiness.md) 分别处理。iOS rejected-disable continuity 已被用户接受为已记录候选的平台限制，原 gate 仍 failed/pending，不重跑来 reconfirm、不合成通知。集中核心 review readiness 仍需按 Plan 功能映射判断，不由本报告测试总数自动宣布。
