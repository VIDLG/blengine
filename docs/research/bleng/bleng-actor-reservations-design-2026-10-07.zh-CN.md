# BLeng actor 与 reservation 设计

## 状态与范围

本文维护 BLeng runtime actor/reservation 所有权边界、predispatch 顺序与 typed operation 的共享提交/控制/结果模型，已按 2026-10-26 当前 Rust 源码同步。底层 `BleHub` 的薄契约不变；当前 11 类 `begin_*` 是同步、非阻塞、有界提交入口，不带 timeout，不保留旧 async begin 兼容入口。typed handle surface 与同步签名属于当前未冻结 BLeng ABI，不能宣称旧二进制兼容。功能范围与 public surface 以 [Plan §1.4/§16.1.9](../../../.plan/PLAN.md#1619-已确认的补充决策) 为准；同步提交的候选验证由[同步 operation 报告（待补）](../../testing/reports/2026-10-26-bleng-synchronous-operations.md)记录，本文不预先声明该报告的验证结论。完整 waiter、事件流和初始化历史说明见英文 [BLeng waiter design and operation completion](bleng-waiter-design-2026-10-06.md)；cleanup 的独立完成语义见 [cleanup 设计](bleng-cleanup-design-2026-10-07.zh-CN.md)。

对外门面按职责组织在 `facade/`：[`mod.rs`](../../../crates/bleng/src/facade/mod.rs) 保留 `Bleng`、`begin_*`/convenience 入口和 runtime 边界；[`operations.rs`](../../../crates/bleng/src/facade/operations.rs) 保留 11 类具体 typed handle 及私有 `OperationAdapter<M>`。错误类型仍由 `error.rs` 定义。这里不存 BLE 证据或建立第二套结果机制，底层 `runtime/operation.rs` 的 ticket/control/slot 职责不变。内部 Rust module 路径也可能影响 UniFFI checksum；本次移动的签名、导出和生成物比较见 [facade 整理验证](../../testing/reports/2026-10-26-bleng-facade-layout.md)。

mailbox 协议只保留 `runtime/messages.rs`，不按命令/事件分设协议模块；actor 保留 BLE 证据状态与协调，owner 管执行器、同步有界提交、事件出口与 shutdown。facade 统一参数映射并为 convenience 计算绝对 deadline，`runtime/operation.rs` 管共享的 caller ticket、结果领取与取消 control。

2026-10-07 的 reservations 重构以当时本地工作树源码为依据：共享 `CommandProgress` 合并 dispatch 与首个 terminal 证据，连接内使用具体 `GattReservation`，订阅状态由私有 enum 表达，各操作 admission 归单连接记录。此次重构不新增依赖、通用操作状态机或路由索引；本文的源码核对不代表当前候选已通过完整验证，既有验证记录保留其原有归属。

## Owner、actor 与 lifecycle

```text
Public BLeng facade
  +-- sync begin_* / async convenience -> shared private submit_*
  |     -> RuntimeOwner.submit_operation
  |          owns executor / ActorRef / shared command allocator / event_rx / shutdown
  |          -> bounded mailbox try_send (capacity 64) -> BlengActor
  |               owns resource allocator / reservations / WaiterRegistry / event_tx
  |               serializes admission / BLE evidence / native retirement / delivery
  +-- typed handle -> OperationControl + single ResultSlot / OperationTicket
  |                    sole result receiver; no BLE state ownership
  +-- convenience computes one absolute deadline; typed wait has its own budget

RuntimeOwner shares RuntimeLifecycle <--- actor / ingress / assembly / dispatch
                    owns phase gate / phase notifications / HubCleanup
```

- [`RuntimeOwner`](../../../crates/bleng/src/runtime/owner.rs) 持有执行器、`ActorRef`、唯一共享 command ID allocator、向外提供事件的 `event_rx`、同步提交入口和唯一 shutdown 协调；它不解释 BLE reservation 证据。
- [`operation.rs`](../../../crates/bleng/src/runtime/operation.rs) 的私有泛型 `ResultSlot<M>`、`OperationTicket<M>`、`OperationControl` 共用现有 oneshot 和 `OperationResult` 解码。没有每种操作独立的状态机、结果 task/channel 或 command registry；外语层仅保留具体 typed objects。
- [`BlengActor`](../../../crates/bleng/src/runtime/actor.rs) 串行处理 BLE 编排、操作证据、waiter 登记与结果交付，持有 reservation 和 `event_tx`。这些状态不搬到共享 lifecycle 中。
- [`RuntimeLifecycle`](../../../crates/bleng/src/runtime/lifecycle.rs) 是共享生命周期控制对象，负责 phase gate、通知和唯一的 `HubCleanup`。fault 可直接启动清理，不依赖 actor 继续接收消息；它不是另一份操作状态库或 waiter registry。

`WaiterRegistry` 是 actor 内有界的 caller delivery 账本，不是 native 命令存活账本。它登记提交记录提供的唯一 sender，登记新项时回收 receiver 已关闭的交付条目，最多保留 64 项，不为 Drop/timeout 发送退休消息或创建后台任务。回调关联仍沿 scan/connection/GATT/subscription reservation 层级查找，没有新增 BLE `CommandId` 路由索引、actor 或执行器。

### 同步提交、共享参数映射与绝对 deadline

[`facade/mod.rs`](../../../crates/bleng/src/facade/mod.rs) 为每种操作保留一个私有 `submit_*`，统一参数到 `Begin*` 消息的映射和具体 handle 构造。公开同步 `begin_*` 与 async convenience 都复用它；生产 owner 不再提供逐操作 begin/run-to-completion 包装。`run_operation()` 及操作 shorthand 只存在于 [`runtime/tests/support.rs`](../../../crates/bleng/src/runtime/tests/support.rs)，用于注入 admission/lifecycle 竞态，不能当作生产提交路径。

```text
sync begin_*(操作参数，无 timeout)
  -> facade.submit_*(retain_completed_result = true)
       -> owner.submit_operation(retain_completed_result, make_message)
            -> hub_for_dispatch() 预检查，立即释放临时 Hub Arc
            -> 共享 command allocator 分配 CommandId
            -> WaiterSender::new()：唯一 oneshot sender / receiver
            -> OperationSubmission + make_message(submission.clone())
            -> 保存 message.correlation() -> actor.tell(message).try_send()
                 满 -> 同步拒绝本次提交；不 fault
                 其他 transport 失败 -> 检查 lifecycle，必要时 fault
                 成功 -> OperationTicket::into_pending -> 具体 typed handle

actor 独立推进 admission / claim / reserve / dispatch，不以 wait 为触发条件
handle.wait(timeout_ms)
  -> 在调用方首次 poll 时校验 timeout 并计算 absolute deadline
  -> 将 deadline 交给 private runtime 的 ResultSlot::wait_until(deadline)
  -> 取走唯一 OperationTicket -> WaiterTicket::wait_until(deadline)
  -> M::decode_success(correlation, actor 写入的 resource, success)
       匹配 -> typed result；缺失 resource 或不匹配 -> fault + TransportFault

async convenience(操作参数, timeout_ms)
  -> OperationStart::completion：入口校验并计算一次 absolute deadline
  -> 相同 facade.submit_*(retain_completed_result = false)
  -> 相同 ResultSlot::wait_until(原 deadline)，不重置等待预算
```

`RuntimeOwner::submit_operation()` 是同步有界 enqueue，不等待 actor、backend 或 BLE，不 spawn 后台 begin/admission task。`make_message` 的签名为 `FnOnce(OperationSubmission<M::Resource>) -> M`，只构造消息、不返回 future。owner 在 Kameo 消费消息前保存轻量 `M::Correlation`，随后直接 `try_send`；成功返回仅证明进入容量 64 的 submission mailbox，不证明 actor 已准入或原生派发已发生。

Hub 未组装、Closed、TransportFault、submission queue full 和 command ID 耗尽可同步拒绝。公开错误映射分别包括 `NotReady { reason: BackendNotAssembled }`、`Closed`、`TransportFault`、`ResourceExhausted { resource: SubmissionQueue }` 和 `ResourceExhausted { resource: Identifiers }`。正常 queue full 仅为本次 backpressure；同时发生 close/fault 时保留 lifecycle 错误优先级。Busy、NotInitialized、失效 connection/GATT/subscription、actor waiter 容量和资源 ID 耗尽等 stateful admission 错误，以及 backend/BLE outcome，经原结果通道在 `wait()` 交付，不回写同步 begin 返回值。

[`messages.rs`](../../../crates/bleng/src/runtime/messages.rs) 的 crate 内部 `OperationResult` 由全部 11 个 `Begin*` 请求实现，定义 `Resource`、`Output`、`Correlation` 三个关联类型及 `correlation(&self)`、`decode_success(correlation, resource, success) -> Option<Output>`。`Resource` 是 actor 分配并写入提交记录的 typed ID，无新资源时为 `()`；`Correlation` 只保留原请求中验证成功所需的 ID，无需时为 `()`；`Output` 是操作的内部类型化返回值。

[`OperationTicket::wait_until()`](../../../crates/bleng/src/runtime/operation.rs) 在取得成功后读取共享 `OnceLock<Resource>`，再调用 `M::decode_success(correlation, resource, success)`，保留全部 variant 与 ID 检查。`Some(output)` 移出结果；缺失 resource 或 `None` 表示内部不变量不匹配，由 ticket 触发 lifecycle fault 并返回 `TransportFault`，不是普通 BLE 失败。解码本身不依赖 lifecycle；admission/dispatch/BLE 错误不进入成功解码。

| 请求 | 必须匹配的成功 variant 与 ID |
|---|---|
| `BeginInitialize` | `Initialized`；移出 snapshot。 |
| `BeginScan` | `ScanStarted`；scan 等于新分配的 `resource`。 |
| `BeginStop` | `ScanStopped`；scan 等于原请求 session。 |
| `BeginConnect` | `Connected`；connection 等于新分配的 `resource`。 |
| `BeginDisconnect` | `Disconnected`；connection 等于原请求 connection。 |
| `BeginDiscoverGatt` | `GattDiscovered`；数据库 connection 等于原请求 connection，GATT session 等于新分配的 `resource`。 |
| `BeginRssi` | `RssiRead`；connection 等于原请求 connection。 |
| `BeginRead` | `CharacteristicRead`；connection、GATT session、characteristic 均等于原请求 ID。 |
| `BeginWrite` | `CharacteristicWritten`；connection、GATT session、characteristic 均等于原请求 ID。 |
| `BeginEnableNotifications` | `NotificationsEnabled`；connection、GATT session、characteristic 均等于原请求 ID，subscription 等于新分配的 `resource`；返回 subscription 与 actual mode。 |
| `BeginDisableNotifications` | `NotificationsDisabled`；connection、GATT session、subscription 均等于原请求 ID；不新增 characteristic 比较。 |

该 trait 只把成功解码归属到请求类型，不克隆请求消息或 payload，不引入 boxing、执行层或新状态机。read bytes、数据库等结果按所有权移出，消息及写入 payload 交给 Kameo 消费；`submission.clone()` 只共享身份、唯一 sender 及 resource/completion metadata。`WaiterSuccess` 仍是数据 enum，不承载执行逻辑。`From` 不适用：解码可能失败，并需要原请求与 actor 分配的预期 ID。notification enable 的内部解码返回 subscription 与 actual mode；公开 handle/convenience 只返回 `SubscriptionId`，actual mode 仍由 canonical event 提供。

convenience 的提交、排队、admission 与结果等待共用入口的一个 absolute deadline；typed `wait(timeout_ms)` 的预算独立于提交时间，在调用方首次 poll 时由 `OperationAdapter::wait()` 校验并计算，然后交给 private runtime 的 `ResultSlot::wait_until()`。预算包含私有执行器的调度延迟与尚未完成的 admission，不到任务启动后重新计时；这不是 begin timeout，也不是 native safety watchdog。非法 timeout 不领取 slot。`WaiterTicket::finish_timeout()` 对已过期和 timer 两条路径都先检查 lifecycle，Open 才返回 `Timeout`；ticket Drop 关闭唯一 receiver，不发 mailbox 退休消息，不因 mailbox 满而 fault。关闭 receiver 不撤回已提交请求，也不释放 native reservation。

### Typed handle、完成观察与 publication 次序

全部 11 种 operation 的具体名称/结果见 [Plan §16.1.9](../../../.plan/PLAN.md#1619-已确认的补充决策)。`begin_*` 为同步函数，只有操作参数，返回 `Result<Arc<具体 handle>, BlengError>`，没有 begin timeout。handle 提供同步、只读、无失败的 `command_id() -> CommandId`，供调用方关联 canonical event/error/log，以及 async `wait(timeout_ms)` 与 `cancel()`；不调用 wait 也会推进 eager submission。command ID 直接复制已有 immutable identity，不查询 runtime 或分配新 ID，wait/cancel/shutdown 后仍可读；它只在所属 Bleng lifecycle 内有意义，不是 admission 或 native 成功证据，也不提前产生 connection/GATT/subscription ID。eager submission 可在 begin 返回前产生事件；getter 不新增事件注册屏障或 replay，调用方自行处理早到事件，typed wait 仍是操作结果入口。控制仍优先使用保留 predispatch 能力的 handle.cancel；现有按 ID 的 actor-routed control 不是第二套状态机，也不具备该本地 shortcut。convenience 沿同一 submit/slot/wait 机制传递一个 absolute deadline，不能重新给等待阶段完整 timeout。

结果领取与目标完成必须分开：wait timeout、已领取 ticket 的等待 future 被丢弃或最后一个未等待 handle 被丢弃只关闭 caller receiver，不撤回尚未 claim 的 eager submission；已经 claim 的 native command 仍由 reservation 持有。由 `CommandProgress` 的证据/退役路径确认 handle-local 单调 `CompletionObserver`；它只帮助区分 completed/unknown，不驱动 BLE 状态、不替代结果，也不建立 tombstone registry。这样 caller receiver 退休后，迟到 native 完成仍可使后续 cancel 正确返回 `OperationAlreadyCompleted`。wait 仍只能领取一次，重复领取返回 `OperationResultConsumed`；非法 timeout 不消费结果。未 poll 的 wait future 尚未领取 slot，单纯构造再丢弃它不等于结果已消费；取消请求也不占有 receiver。

原操作完成、结果交付和当前连接可用性是不同事实。control 回执未决时，actor 把首次完整结果放入现有有界 `WaiterEntry`，仍使用原 oneshot；后续 loss 立即退役连接、失败未完成 sibling，但不能改写已完成结果。取消拒绝后直接交付保存的结果，不从 loss 后的 reservation 重算。正常 close 同样保留已确认完成的 typed result；completion observer 只识别 actor 交付前的关闭窗口，fault 仍覆盖为 TransportFault，timeout/drop 不改变原有 retirement policy。pending disconnect 保持 NotCancellable，完整完成后为 AlreadyCompleted，即使其他 control 仍保留所属 root。具体红绿与证明范围见 [结果保持收敛](../../testing/reports/2026-10-26-connection-cancel-functional-convergence.md)。

`CommandProgress` 的取消控制使用私有 `CancellationState` / `CancellationPhase`：Idle 不持有 receipt，Active 同时持有 phase 和唯一 sender；请求、dispatch claim 和接受/拒绝的判断来自 actor-owned enum，watch 只发布回执，不承担第二份协议状态。原 dispatch acknowledgement、terminal/domain 完整性、结果交付仍是独立事实，不为减少字段强行合成一个 completed 标志。Active request 的分配与既定重复回执语义、有界转换序列测试及生态取舍见 [取消状态设计收敛](../../testing/reports/2026-10-26-bleng-cancel-state-design.md)。

取消协议测试用一个请求周期 oracle 同时驱动短序列穷举与有界 Proptest 长序列生成；Proptest 仅为 std-only dev-dependency，提供失败缩减/seed 重放，不进入生产图。保留独有的资源恢复、public typed policy 和 publication 阶段回归，不把 wrapper 数量或生成案例数当作覆盖。当前取消能力、native 精确拒绝及证明边界见 [取消覆盖审计](../../testing/reports/2026-10-26-bleng-cancellation-coverage.md)。

取消已请求不等于已接受。control 回执待决期间，同一 connection/scan 的 domain、terminal、通知/广告与被动 teardown 由现有 owner 的 `ScopedPublication` 延期，聚合最多 64 项；其他 connection/scan 独立发布。最后一个待决 control 返回后按原 scope 次序释放：拒绝保留原证据，接受仅抑制该 target 的非-terminal 事件。释放只调用 publication，不重录 evidence 或重复结算；缓冲/FIFO 满均 fail closed，不静默丢失。不能按每 command 分别缓冲后让同 scope teardown 越过先前成功。

初始化的 predispatch 缓冲同样保留已接纳的 adapter snapshot 顺序：首个关联 snapshot/terminal 被暂存后，后续被动 snapshot 共用原单个 64 项缓冲，不能越过较早证据；之前的被动 snapshot 仍立即公开。initialize 结果只取 correlated snapshot，不把被动状态当成功证据；接受后原序回放，拒绝与关联回调冲突仍 fail closed。这里只排序已接纳输入，不猜测平台因果，也不保存最新 canonical adapter 状态。证明范围见 [初始化发布审计](../../testing/reports/2026-10-26-bleng-initialization-publication.md)。

订阅 transition 的 predispatch 关联包含精确 connection/session/subscription/characteristic 的 value，复用原有单个 64 项缓冲。合法同步输入 `state → terminal → value` 不能被 BLeng 改成 value-first；若 backend 实际 value-first，也按其原序回放，不凭空制造 state-first 平台保证。value 不完成 enable waiter，拒绝前已有 matching callback 仍按原 ownership 歧义规则 fail closed。

最后一个 `Bleng` facade drop 非阻塞请求现有 close/cleanup。handle 的强引用可保活 executor，使已完成未领取结果仍可消费，但不保活逻辑 Hub；pending wait/cancel 返回 Closed。只 drop handle 不关闭仍存活的 Bleng，也不隐式 native cancel。Drop 不代表 cleanup 成功，显式 shutdown 仍是唯一完成/错误屏障。初始化/disconnect 在 dispatch claim 之后保持当前 NotCancellable 策略；claim 之前仍可由显式本地取消获胜。不借 typed surface 扩展 native driver 能力。

这些职责的 Rust/foreign 验证与当前缺口见 [功能收敛报告](../../testing/reports/2026-10-25-core-functional-convergence.md)，不从接口完整或 macOS fixture runtime 推导手机 RF 通过。

### 单一 shutdown coordinator

`RuntimeOwner::new()` 通过 Kameo `PreparedActor` 取得 `ActorRef` 并 spawn；实际 actor task 的 `JoinHandle` 保留在 `runtime.handle().spawn` 块中的唯一内联 shutdown task 内。该块克隆捕获 `actor`、`lifecycle` 和 `shutdown_result_tx`，不持有 `RuntimeOwner`，不再使用 first-close atomic 或两套 watcher。

```text
new 中的内联 shutdown task：select actor task 完成 / lifecycle 离开 Open
  -> lifecycle 分支：start_cleanup -> stop_gracefully -> join 实际 actor task
  -> 消费 task 结果；成功时显式 drop 返回的 BlengActor
  -> start_cleanup -> wait_for_cleanup -> 发布共享 shutdown result
close：订阅共享结果 -> 关闭 gate + start_cleanup
  -> shutdown_result_rx.wait_for(Option::is_some) -> clone 保留结果
```

`close()` 用 `shutdown_result_rx.wait_for(Option::is_some)` 替代手写 watch loop；predicate 只检查通道内的 `Option`，随后克隆结果，通道关闭错误映射为 `CleanupError::WorkerStopped`。它不读取外部 cleanup ledger，不能把这一替换照搬到 cleanup 的完成等待。

mailbox 关闭和 `ActorRef::wait_for_shutdown()` 仅证明 mailbox closure，不证明 actor 的 `on_stop`、unregister 或状态析构完成；join 之后仍须 drop 返回的 actor。actor 在 Open 时意外完成也会被 coordinator 消费并丢弃，由 actor 的 Drop fault lifecycle 并退休 waiter。actor task join/Kameo lifecycle 错误先等待 cleanup，再映射为 `CleanupError::WorkerStopped`；task 正常完成并释放 actor 后返回 cleanup 自身结果，runtime `Faulted` 不自动表示 disposal 失败。取消某次 close 等待不会取消 coordinator；`Drop` 仍是不能 await 的 kill 与 `shutdown_background()` fallback，独立 cleanup 线程不依赖 runtime 存活。英文细节及本地 Kameo 0.22.2 证据见 [cleanup 设计](bleng-cleanup-design-2026-10-07.md#logical-shutdown-and-cleanup-integration)。

## 目录与职责

```text
runtime/
  owner.rs
  operation.rs
  lifecycle.rs
  actor.rs
  messages.rs
  backend_port.rs
  reservations/
    mod.rs
    dispatch.rs
    initialization.rs
    scan.rs
    connections.rs
    connection.rs
    gatt.rs
    io/
      mod.rs
      rssi.rs
      read.rs
      write.rs
    subscriptions.rs
    subscription.rs
```

| 源码 | 职责 |
|---|---|
| [`messages.rs`](../../../crates/bleng/src/runtime/messages.rs) | 统一 mailbox 协议：11 个携带 `OperationSubmission` 的 `Begin*` 请求、`OperationResult` 成功解码及 unit-reply admission handler；ingress/dispatch-result 输入与取消 control 消息。只引用提交记录，不拥有 BLE 状态或调用方 deadline。 |
| [`owner.rs`](../../../crates/bleng/src/runtime/owner.rs) | 执行器、共享 command allocator、`OperationSubmission`/`SubmissionState`、同步有界 `submit_operation()`、事件出口和单一 shutdown coordinator；无生产逐操作等待包装。 |
| [`operation.rs`](../../../crates/bleng/src/runtime/operation.rs) | 泛型 caller ticket、唯一 receiver 的一次性结果槽、wait deadline/receiver 关闭、独立取消 control 和 typed success 解码；无 BLE 状态权威。 |
| [`actor.rs`](../../../crates/bleng/src/runtime/actor.rs) | 定义 `BlengActor`、`Drop`、register/reserve 方法和全部状态协调方法；负责 ingress/dispatch-result 处理、跨 reservation 的动作和 waiter 结算，不持有 prelaunch rollback guard。 |
| [`backend_port.rs`](../../../crates/bleng/src/runtime/backend_port.rs) | backend assembly 与非阻塞 ingress；直接从 messages 导入 `IngressEvent`，不持有 owner 或解释 reservation 证据。 |
| [`reservations/mod.rs`](../../../crates/bleng/src/runtime/reservations/mod.rs) | 记录状态层边界，只重导出 runtime 实际使用的状态入口和协调结果。 |
| [`dispatch.rs`](../../../crates/bleng/src/runtime/reservations/dispatch.rs) | `CommandProgress` 共享 dispatch/首个 terminal 证据、单调 completion observer、pre-ack 缓冲；`ScopedPublication` 提供 connection/scan owner 内的有界发布延期，不建路由 registry。 |
| [`initialization.rs`](../../../crates/bleng/src/runtime/reservations/initialization.rs) | 初始化证据、readiness 和初始化状态转换。 |
| [`scan.rs`](../../../crates/bleng/src/runtime/reservations/scan.rs) | 唯一扫描槽位、start/stop 证据和 session 退休。 |
| [`connections.rs`](../../../crates/bleng/src/runtime/reservations/connections.rs) | 连接根集合、peripheral 槽位 admission、connection/command 查找与 peripheral key 解析、操作登记、全局回调路由优先级和批量退休。 |
| [`connection.rs`](../../../crates/bleng/src/runtime/reservations/connection.rs) | 单连接根记录、各操作 admission、I/O 与订阅转换的共享容量上限、disconnect、子命令进度遍历、局部事件关联、dispatch reconciliation 和退休。 |
| [`gatt.rs`](../../../crates/bleng/src/runtime/reservations/gatt.rs) | 已提交数据库与 pending discovery 的不同生命周期。 |
| [`io/mod.rs`](../../../crates/bleng/src/runtime/reservations/io/mod.rs) | `IoReservations`、一次性 RSSI/read/write 命令的三张 map、聚合路由与回收；不持有长期数据库或订阅 route。 |
| [`io/rssi.rs`](../../../crates/bleng/src/runtime/reservations/io/rssi.rs)、[`io/read.rs`](../../../crates/bleng/src/runtime/reservations/io/read.rs)、[`io/write.rs`](../../../crates/bleng/src/runtime/reservations/io/write.rs) | 各操作的事件匹配、证据记录、结果与完成判断。 |
| [`subscriptions.rs`](../../../crates/bleng/src/runtime/reservations/subscriptions.rs) | `SubscriptionReservations`、私有订阅 map、查找/占用查询、登记、进度 inventory、缓冲目标、事件与派发结果分派、结果查询和退休；状态/数据通知按 subscription ID 点查后校验完整身份，连接 loss 保留 scope 查找。 |
| [`subscription.rs`](../../../crates/bleng/src/runtime/reservations/subscription.rs) | 单条订阅的 enable/disable phase、模式验证、`validate_disable()`、`resolve_dispatch()` 与 route 处置。 |

`gatt`、`io`、`subscriptions` 按状态生命周期并列，而不是按“是否产生原生 I/O”合并：GATT owner 保存已提交数据库和 discovery，I/O owner 保存一次性 RSSI/read/write 命令，订阅 owner 保存长期 route 及短期 enable/disable transition。connection 持有私有 `MAX_PENDING_NATIVE_IO_OPERATIONS_PER_CONNECTION = 1_024`，统一核算 `io.len() + subscriptions.transition_count()`；稳定 Active route、discovery 和连接生命周期命令不计入这一预算。共享预算不等于共享存储 owner，也不是对原生执行队列内容的完整镜像。

此前先统一 reservation 所有权与接口；当单文件增长到约 2300 行、内部边界稳定后，再按生命周期拆成 `reservations/`。这是源码组织拆分，不是新增运行时边界；不新增 actor、executor、锁或命令索引。集合与单条记录分别由 `connections.rs`、`connection.rs` 管理：集合按事件身份选择候选连接，记录解释事件类别；受影响连接的子命令门禁必须先于外来 command 所指向的 connect fallback。

可见性同时由私有模块、源声明和受限 facade 控制，不只依赖“私有模块中的 pub 不可达”。`mod.rs` 中所有实现模块保持私有；actor 实际消费的结果、动作、错误、取消回执和准入类别，在叶模块使用 `pub(in crate::runtime)`，经 `pub(super) use` 提供给 runtime。仅在目录内共享的 `CommandProgress`、缓冲分类、发布事实与 FIFO 类型使用 `pub(super)`，在 `mod.rs` 中只做普通 `use`，不向 runtime 重导出。类型与字段分别按实际使用范围限制；例如 actor 读取的 dispatch 结果字段也只开放到 runtime。

根 owner 类型及已有 runtime 测试确实直连的方法只开放到 runtime；仅由 coordinator 和目录内测试调用的方法进一步限制到 reservations。测试专属访问 helper 同样只开放到 runtime，不以测试为由使用 crate-wide 可见性。`io` 下的 `rssi`、`read`、`write` 叶子模块保持私有，类型与聚合层所需的构造、匹配、记录、结果、完成方法及字段仅以 `pub(super)` 开放到父 `io` 模块，其余实现保持私有；不向 connection 或 reservations facade 重导出叶子类型。connection 通过 `IoReservations::progress_for_cmd()` / `progress_for_cmd_mut()` 做命令点查，通过 `resolve_dispatch()` 交付派发结果，通过 `pending_*_progress_for_event_mut()` 选择事件缓冲目标；不接触叶子记录，也不分别删除 RSSI/read/write entry。`GattReservation` 同样持有私有 discovery 槽位，`GattDiscoveryReservation` 类型及字段只在 `gatt.rs` 内可见；connection 通过 owner 的事件记录、缓冲目标选择、派发结果、结果查询和 progress 接口操作，不直接筛选或删除 discovery。`ConnectionReservation.subscriptions` 持有 `SubscriptionReservations`，后者私有拥有 `HashMap<SubscriptionId, SubscriptionReservation>`；connection 只调用集合接口，不直接遍历、读取或删除订阅 entry。叶字段与 phase 不为封装对称而机械重构；其他跨 sibling 所需的 progress 字段和订阅身份字段保留 `pub(super)`，不为隐藏字段而增加等价 getter。这些都是内部 Rust 边界收窄，不新增 `IoOperation` trait 或通用框架，也不改变公开 ABI。

`InitializationState` 是不透明 struct：其 `InitializationPhase` enum、phase 字段和 `InitReservation` 全部私有，外部不能直接构造 `Ready` 或覆盖 pending 证据。扫描 enum 的 payload 类型仍与 enum 的声明可见性匹配，但不单独重导出，字段与局部方法也不开放。`pending()`、`reserved()`、`reserved_mut()`、`find()`、`find_mut()` 等原始记录查询均为内部实现。actor 断连结算通过 `ConnectionReservations::accepted_command_ids(connection_id)` 取得所需 command ID，不再取得整个连接记录。测试通过 dispatch resolution、相关事件、结果与 admission 判断验证行为，不为了检查内部字段放宽可见性。

reservation 是命令与事件处理共用的 actor-confined 状态模型，不只是被动字段容器：它拥有局部 admission、回调关联、证据记录、dispatch reconciliation、结果判断与退休规则。reservation 是 actor 内的普通值，不单独调度。[`messages.rs`](../../../crates/bleng/src/runtime/messages.rs) 统一命令准入与派发结果/ingress 消息入口；状态协调集中在 [`actor.rs`](../../../crates/bleng/src/runtime/actor.rs)，局部状态判断靠近其 reservation 所有者。这是既有 runtime 内按角色划分源码，不是新增运行时或架构边界。

建议按职责阅读：先看 `reservations/mod.rs` 的边界和 `dispatch.rs` 的共享进度，再看 `connections.rs` 的根查找与全局路由、`connection.rs` 的局部 admission/结算/退休，随后阅读 `gatt.rs`、`io/mod.rs` 及其三个叶子模块、`subscriptions.rs` 的集合边界与 `subscription.rs` 的领域证据；初始化与扫描分别看 `initialization.rs`、`scan.rs`。最后对照 `messages.rs` 的准入交接和 `actor.rs` 的事件协调，避免把进度遍历误认为路由优先级。测试统一物理存放在 [`reservations/tests/`](../../../crates/bleng/src/runtime/reservations/tests/)：`connections.rs` 通过容器行为接口覆盖跨 reservation 不变量；`subscription.rs` 由生产订阅模块通过 `#[path]` 挂载为自己的测试子模块，保持原有隐私边界。生产文件不内联测试函数，也不为测试放宽可见性；完整流程测试仍在 `runtime/tests/`。

## 命令 ID 与资源 ID 的独立分配

[`IdGenerator`](../../../crates/bleng/src/runtime/actor.rs) 使用 `AtomicU64::fetch_update` 与 checked increment，从 1 开始。`RuntimeOwner.command_ids: Arc<IdGenerator>` 是该 BLeng 实例唯一共享的 command allocator，同步提交在 caller 线程分配 `CommandId`；并发调用不通过 actor 串行分配。actor 的 `command_id_gen` 字段及 `register_waiter()` 仅在 `cfg(test)` 中存在，并复用同一 allocator，不是第二个生产命令序列。独立的 `resource_id_gen` 仍由 actor 单独拥有，虽复用同一个 atomic generator 类型，资源分配只在 actor admission 中执行。新建 scan session、connection、GATT session 和 subscription 暂时共用该资源命名空间，不为每个类型增加计数器。

```text
caller 同步提交：Hub 预检查 -> shared command allocator -> 原 sender/receiver -> try_send
  -> actor：操作检查 -> 如需则分配 typed resource ID
       -> register_operation_waiter(submission, expectation)
            用既有 CommandId 和同一个 WaiterSender 登记 delivery
       -> launch_dispatch：Hub 准备 -> 原子 claim -> reserve -> native dispatch
```

资源 ID 先确定，再直接构造 expectation；它与 reservation 使用同一个 typed ID，不要求数值紧随命令 ID。`register_operation_waiter()` 不分配 command ID 或第二个 channel；已本地取消的提交跳过登记，后续 claim 失败，因此不建 reservation、不派发。

ID 是关联标识，不是成功操作的连续编号：允许跳号，不回滚、不复用、不回绕。queue full、stateful admission 拒绝、取消、dispatch 拒绝或后续失败都不回收已分配的 command ID。资源分配失败时 command ID 已消耗但尚未登记 waiter；waiter 容量拒绝可能同时消耗资源 ID，但不创建 reservation 或启动原生命令。两个命名空间独立耗尽，`u64::MAX` 可分配一次，此后持续返回 `IdExhausted`，不会变成 0；command 耗尽在同步提交返回，resource 耗尽经结果交付。

`CommandId(1)` 与 `ConnectionId(1)` 可以同时存在，关联按类型和对应字段进行。command ID 分配顺序不等于并发 enqueue/admission 顺序；`WaiterRegistry::register_sender()` 接受非零且不与当前 pending 项重复的 ID，不再要求严格单调登记，也不保留无限历史 ID 集合。全生命周期不复用由唯一 allocator 保证，registry 负责当前 delivery admission，reservation 负责 callback correlation；三者不能互相替代。

[`runtime/tests/id_allocation.rs`](../../../crates/bleng/src/runtime/tests/id_allocation.rs) 保留精确关联、跳号与不复用边界；[`runtime/tests/actor.rs`](../../../crates/bleng/src/runtime/tests/actor.rs) 验证最大 ID/不回绕；[`runtime/tests/owner.rs`](../../../crates/bleng/src/runtime/tests/owner.rs) 的 `concurrent_synchronous_entries_share_one_non_reusing_allocator_and_bounded_mailbox` 与 [`waiter_tests.rs`](../../../crates/bleng/src/tests/waiter_tests.rs) 的 `registry_accepts_out_of_order_unique_ids_but_rejects_live_duplicates` 分别覆盖共享 allocator/有界 mailbox 和乱序登记。这些是当前测试源码导航，不是本次文档编辑重跑后的结果。

## 连接 reservation 容器

`BlengActor.connections` 持有 [`ConnectionReservations`](../../../crates/bleng/src/runtime/reservations/connections.rs)，不是向兄弟模块公开的 `HashMap`。容器内部唯一的 `by_peripheral` map 保存连接根记录；不增加按 `ConnectionId` 或 `CommandId` 的第二份索引，也不增加锁、共享所有权或调度设施。

职责分三层：

| 层次 | 职责 |
|---|---|
| `BlengActor` | lifecycle gate、资源 ID 分配、既有 submission 的 waiter 登记、dispatch claim/reservation 安装协调、事件发布和 waiter 结算。 |
| `ConnectionReservations` | 按 peripheral 登记、按 connection/command 查找、terminal/result 路由、批量退休与根记录移除。 |
| `ConnectionReservation` | 单连接状态、GATT/I/O/subscription 证据、disconnect/discovery/RSSI/I/O/订阅各操作的 admission 条件、结果与退休判断。 |

`ConnectionReservations` 不包 `Option`：空容器已经表示没有 connection reservation，没有额外的「未创建容器」状态。唯一 scan slot 由 `ScanState` 表达 `Idle` 或 `Reserved(ScanReservation)`，根状态的验证、安装和释放也由它拥有。连接容器中的每一条 reservation 有各自的生命周期，但容器本身与 actor 同生共死。

`reserve_connection()` 只允许空 peripheral 槽位，不能覆盖仍持有 native 生命周期证据的旧记录。正常重复 connect 仍在 admission 阶段返回 `PeripheralBusy`；内部重复安装则是 invariant failure，而不是静默替换。连接结束并完整记账后，容器才释放该 peripheral，后续连接使用新 connection/command ID，旧 terminal 不能命中新记录。

容器不实现 `Deref<HashMap>` 或 `Index`，也不向外返回 map。pre-ack 路由和操作特有的拒绝回放筛选已交回容器及所属 reservation；事件协调层不再遍历连接记录，原 `iter_mut()` 接口删除。回放执行、fault/cleanup 和 waiter 交付仍由 actor 协调，没有引入通用集合策略框架。

`DispatchResolved` 先由容器按 command 找到所属 connection，再处理其中的具体 slot，不再为每个操作重复扫描整张连接 map。domain event 通过 connection ID 查到目标后，仍校验 command/session/characteristic/subscription 的对应关系。容器先解析连接根与 peripheral key，再调用单连接记录的 `validate_disconnect()`、`validate_gatt_discovery()`、`validate_rssi()`、`validate_gatt_io()`、`validate_notification_enable()` 或 `validate_notification_disable()`，不在容器中重复局部条件；disable 还返回登记所需的 characteristic ID。

调用顺序仍是记录证据、结算 waiter、再调用 `connections.retire_completed()`；容器不持有或自行结算 waiter。相关回归覆盖重复安装不能覆盖旧根、跨连接 terminal 隔离，以及同一 peripheral 重连后拒绝旧 terminal。

## Actor 封装与纯转发

Rust 的 `pub(super)` 将可见范围限制在 `runtime` 及其子模块，不是对 crate 用户公开；此前主要用于让兄弟模块中的 `impl BlengActor` 访问字段。但字段可见并不等于所有调用点都需要容器本体。

`event_tx` 和 `waiters` 是 actor 私有字段。`publish_event()` 只负责入口 open gate、向外入队和队列失败时 fault；成功仅表示本地入队。私有 `process_event()` 在发送后再次检查 lifecycle，才继续处理证据。前者是发送前置条件，后者是事件协调的继续处理条件，不在同一函数里重复 `require_open()`。发送后的检查应对并发 shutdown，但不能撤回已入队事件，也不保证发布与关闭原子化。caller timeout/Drop 直接关闭结果 receiver，不再经过 `RetireWaiter`/`retire_waiter()`；registry 在后续登记时有界回收关闭的 delivery 项，native reservation 不受影响。显式 `cancel()` 则是独立 control path。actor 内部结果交付仍直接使用私有 registry；`launch_dispatch()` 的本地取消 claim 失败可直接 `waiters.cancel` 清除 admission draft，sender 已被本地取消取走时不会重复交付。

`require_open()` 的 phase-to-error 映射属于 `RuntimeLifecycle`；`require_initialized()` 的 readiness-to-error 映射属于 `InitializationState`，只有 `Ready` 通过，`NotStarted`、`Pending` 和 `Rejected` 均返回 `NotInitialized`。actor 通过 `delegate!` 使用这两个门禁，不再重复判断状态。`reserve_connection` 和连接内的 `reserve_*` 入口也通过 `delegate!` 转发给 `ConnectionReservations`，actor 不再拼装 disconnect/RSSI/I/O/subscription 字段或持有 `connection_mut()` helper。`InitializationState::reserve_initialize()` 建立 pending 状态，私有 `InitReservation::new()` 拥有初始证据；actor 同样只转发。`ScanState` 拥有 scan 根的 admission、start/stop 登记、拒绝移除和退休，stop slot 的初始化仍属于 `ScanReservation`。所有纯转发集中在一个 `delegate!` 块中并按所有者分组；scan 的四个命令入口统一 `to self.scan`，不再由 actor 解包 `Option` 或表达 scan 可用条件。

初始化的结算后推进统一调用 `advance_after_settlement() -> Result<(), WaiterError>`：未确认派发或 terminal 未到时保留 Pending；terminal 失败返回原操作错误，由 actor fault；成功且 snapshot 已到才转 Ready。它替代外部先查 `has_failed()` 再调用完成方法的组合，不克隆成功 snapshot，不改变 waiter 先结算的顺序。`record_event()` 直接匹配带关联命令 ID 的 snapshot，不再先执行包含 terminal 的通用匹配再重复解构；`pending()` / `pending_mut()` 保持私有，复用多处状态访问。`reserve_initialize()` 的断言复用 `require_not_started()`，不另保留 `has_started()` 查询。初始化用私有 `pending_for()` / `pending_for_mut()` 统一按 command ID 选取 Pending，scan 用私有 `stop_for()` / `stop_for_mut()` 统一选择 stop reservation；只抽取重复选择，不为每个仅使用一次的 connection slot 增加 getter。测试 fixture 使用 reserve、dispatch acceptance、snapshot、terminal 和完成推进建立就绪状态，不新增只供测试使用的生产构造器或 getter。

### Adapter snapshot 的获取与状态归属

`InitializationState::Ready` 只表示初始化证据已经完成，不保存 adapter 的最新快照，也不表示蓝牙处于 PoweredOn。`Bleng::initialize()` 成功返回首个关联初始化 snapshot；该结果是初始化证据，不保证等于函数返回瞬间的最新状态。后续平台观察到的电源或授权变化，经 `AdapterSnapshotChanged` 进入 `Bleng::next_event()`，即使初始化已 Ready 也继续对外发布，不重置初始化状态。

当前公共接口没有主动刷新或读取最新 adapter snapshot 的 getter，重复 initialize 会被拒绝，不能用于刷新状态。调用方应使用一个事件读取循环维护最后观察到的 adapter snapshot，再向 UI 或其他消费者提供状态；FIFO 不广播，也不向晚加入的读者重放最新状态。若已经处理了后续 adapter 事件，不要再用较早的 initialize 返回值覆盖该状态。初始化 waiter 超时或取消后，迟到 snapshot 仍可经事件流取得。未来若要增加缓存查询或原生刷新命令，应独立明确其所有者、时效和并发语义，不能把初始化 reservation 当作持续状态仓库。此边界与 [计划中的调用方状态归属](../../../.plan/PLAN.md#22-术语) 一致。

### Actor 内部状态与协调入口

actor 的 `lifecycle`、`initialization`、`scan`、`connections` 现在全部私有。messages 中的命令与订阅 handler 不取得 reservation 实体：初始化/扫描/连接/GATT/RSSI/读写/订阅的 admission 条件由对应所有者验证，actor 用 `delegate!` 转发，返回值只包含成功/错误或后续登记需要的 ID，保留原错误优先级。scan slot 的空闲/占用判断属于 `ScanState`；session 内 stop 验证属于 `ScanReservation`。

`launch_dispatch()` 位于 actor.rs，只有这里内部克隆 lifecycle 给分离的 blocking dispatch task；messages 不获取或克隆 actor 的 lifecycle Arc。`runtime/messages.rs` 与 actor.rs 平级，其中两个异步输入 `IngressEvent`、`DispatchResolved` 的 `Message` handler 各只调用 `self.handle_ingress_event(event)`、`self.handle_dispatch_result(message.id, message.result)`，入口名称不变。这两个同步入口在 actor.rs 中为 `pub(super)`；实际处理、提前回调缓冲、foreign disconnect 排除、发布、记录、结算、回收、`DispatchAction` 执行与回放都封装在 actor.rs 中，其余协调 helper 保持私有。messages 不访问 actor 字段，也不通过新 getter 取得状态；回调关联、局部证据修改和订阅模式验证仍属于 reservation。`resource_id_gen` 仍是 actor 私有字段，Begin handler 通过 `allocate_resource_id()` 分配资源；`IdGenerator` 类型限于 runtime 内使用，共享 command allocator 由 owner 持有。

同一项 reservation 登记在 actor 和拥有者上采用同名 `reserve_initialize()`、`reserve_connection()`、`reserve_scan_stop()`，不再通过 `#[call(begin)]`、`#[call(register)]` 或 `#[call(begin_stop)]` 建立额外的名称对应。generator 的上下文不同，`allocate_resource_id()` → `allocate()` 的改名转发仍保留；caller delivery 的关闭不再转发到 actor，显式 native 取消经 `request_operation_cancellation()` 协调。

`delegate!` 仅表达已明确所有者的纯转发。涉及 ID 分配、发布失败策略、跨 reservation 结算或先后顺序的方法仍显式实现；宏不是职责划分的替代品。

### 可见性与命令结果路由

`runtime/mod.rs` 的实现模块均为私有，不向 facade 重导出 `BlengActor`；actor 类型虽声明 `pub(crate)`，其路径仍受私有模块限制。`WaiterReceiver` 是 runtime 内的 `cfg(test)` 别名。`RuntimeOwner` 的全部字段（包括 `actor`、`command_ids`、`lifecycle`、`shutdown_result_tx`）均为私有，`BackendPort.lifecycle` 同样私有。actor 全部字段私有；messages handler 使用的验证、资源分配、登记、claim/派发与取消协调入口保留 `pub(super)`。两个异步输入 handler 只调用受限入口 `handle_ingress_event()` 与 `handle_dispatch_result()`；其余发布、terminal 路由、waiter 结算与失败传播保持在 actor.rs 的私有边界。没有 caller waiter 退休消息入口。

facade 需要的 11 个 `Begin*`、`OperationResult`、`PendingOperation`、`OperationControl`、`ResultSlot` 经 runtime 作 crate 内受限重导出；它们不是产品 API 或 foreign DTO。`OperationSubmission` 为 crate 内提交能力，`SubmissionState` 和 `OperationTicket`/`WaiterTicket` 仅供 runtime 内部使用；ticket 的字段与等待实现属于 `operation.rs`，不是 owner 私有类型。公开 Rust/UniFFI surface 仍是具体 handle、typed result 与公开 re-export 的错误/reason 类型，不泄露 Tokio/Kameo。

runtime 的生产代码使用显式导入，`mod.rs` 不为子模块集中导入外部依赖。测试物理文件位于 `runtime/tests/`，逻辑模块按被测所有者划分：owner 通过 `#[path = "tests/mod.rs"]` 挂载 owner/跨模块流程测试；actor 通过 `tests/actor.rs` 挂载私有不变量测试，通过 `tests/actor_support.rs` 定义访问私有状态的 fixture message handler。后者只在 `cfg(test)` 下受限重导出，不暴露 actor 字段。fixture 实现同一个 `OperationResult`，通过 `OperationSubmission`、unit-reply handler 和测试 support 的 `begin_operation()`/`run_operation()` 调用生产 `submit_operation()` 与原结果 slot；不建立另一套生产 API 或结果通道。测试 helper 与生产 owner 提交职责必须区分；可见性由实际依赖决定，不为测试开放 reservation 字段。

测试按不变量而非行数保留：生命周期、错误优先级、取消、关联和耗尽边界有价值；独立局部变量互不影响、原样结果转发等无独立行为的重复测试删除。waiter 的单独 dispatch-rejection payload 转发测试已移除，恰好一次交付与 runtime 的实际拒绝处理仍由原有回归覆盖。

`record_terminal()` 按 initialization、scan、connections 的顺序逐段查找，前两段命中即 `return`，不将有副作用的调用组合在 `||` 中。reservation 返回的 `bool` 只表示「这个 command ID 属于我」，不表示操作成功；匹配到重复 terminal 也要停止查找，而已有首个 terminal 不被覆盖。actor 内 command ID 唯一，因此同一 terminal 不广播给多个 reservation。

`waiter_result()` 同样采用逐段 early-return，但停止条件是拿到 `Some(result)`，包括 `Some(Err(...))`；`None` 可能表示 ID 不属于当前 reservation，也可能表示证据未齐，不能当成操作错误或命令不存在。继续查找不会将未就绪的命令交给其他所有者，因为 command ID 不复用且唯一；无需额外结果路由 enum 或第二份命令索引。

## 共享门禁与派生状态

`RuntimeLifecycle::is_open()` 读取既有原子 phase，只用于查询当前是否 Open；它不保留 admission、不阻止并发 close/fault，也不新增状态。需要区分 `Faulted` 与 `Closed` 并映射错误的路径仍使用 phase；`require_open()` 保留相应错误映射。

[`CommandProgress`](../../../crates/bleng/src/runtime/reservations/dispatch.rs) 替代原 `DispatchReservation` 加各记录独立 terminal 字段的组合。每个命令的 `progress` 保存 `command_id`、私有 `buffered_events`、私有 `dispatch_accepted` 和私有 `terminal_result: Option<Result<(), BleEventError>>`；scan start/stop、connect/disconnect、discovery、RSSI、读写与订阅 transition 都复用它，不再另存 `start_terminal` 或 `terminal`。

`record_terminal()` 仅记录首个结果，冲突的重复 terminal 不能覆盖它；读取通过 `has_terminal()`、`terminal_succeeded()`、`terminal_failed()`。`is_awaiting_dispatch_result()` 只表示仍在等待原生同步派发的结果被 actor 处理，不表示 waiter 或原生操作仍 pending；dispatch 被接受不等于 BLE 操作完成。这里的 result 是 `DispatchResolved` 携带的同步派发结果，不是 submission enqueue 结果或 BLE 原生应答。

`CommandProgress::terminal_result()` 不接收 success closure，也不依赖 `WaiterSuccess`：未确认 dispatch 或缺少 terminal 时返回 `None`；terminal 错误交付为操作错误；terminal 成功仅返回 `Ok(())`，不代表 domain 完整。具体 reservation 采用直线控制流组合领域证据：先用 `terminal_result()?` 等待已接受命令的 terminal；若返回错误，立即交付 `Some(Err(error))`；只有成功后才读取 domain 并构造 `Some(Ok(success))`，缺少 domain 仍返回 `None`。不再通过嵌套的 `map(...).transpose()` 转换容器，成功数据仍只在 terminal 成功后才克隆或构造，也不重新引入 success closure。`is_complete(domain_evidence_ready)` 判断命令证据是否完整：dispatch 已接受、terminal 已到达，且 terminal 失败或成功所需的关联领域证据已经齐全。这里的完整不等于 native 生命周期结束，也不依赖 waiter 是否仍存在；native 资源何时可安全移除仍由具体 reservation 判断。连接错误、被动断连等操作特有的优先级和退休条件仍由具体 reservation 判断，不隐藏在通用状态机中。

snapshot、连接状态/error、discovery draft、RSSI/read payload、write 完成事实与通知状态仍保留在对应 reservation；共享进度不拥有这些 typed domain 证据、waiter 或路由策略。

`buffered_events` 不向兄弟模块暴露，事件只能通过有界 `buffer()` 入队。派发结果只有两个入口，均由 `CommandProgress` 接管缓冲：

- `resolve_accepted_dispatch()` 标记派发已接受，并按原顺序移交全部缓冲事件；明确返回 `DispatchAction::None`，正常接受本身不要求 cleanup 或 fault。
- `resolve_rejected_dispatch(action_if_no_conflict, classify)` 取走全部缓冲，不标记接受；具体 reservation 对每条事件返回 `BufferedEventDisposition::Replay`、`Discard` 或 `Conflict`。共享层保序收集回放事件、丢弃合法可忽略的证据；任何冲突覆盖正常拒绝动作，要求 fault，但不提前结束后续分类。

initialization、scan start 和 connect 将所有提前回调分类为 `Conflict`，保留严格拒绝规则；无事件时 initialization 要求 `StartCleanup`，其他正常拒绝为 `None`。普通 I/O 只回放被动 connection loss；订阅保留较宽的 connection-loss 条件。scan stop 同样走统一入口，只在本地定义三路分类：被动停止、停止错误或其他命令的 Requested 通知为 `Replay`，属于被拒绝命令的 Requested 证据为 `Conflict`，被拒绝命令的 terminal 为 `Discard`。

`DispatchResolution` 只是结果数据，不再拥有独立分类构造入口；`rejected_resolution_with_replay()` 和 `from_rejected_events()` 已移除。`take_buffered_events()` 保持私有，外部既不直接取缓冲，也不自己循环组装拒绝结果。严格拒绝同样排空缓冲，不再依赖随后删除 reservation 来丢弃证据。

资源移除、订阅恢复和动作执行不在这些入口中。取走缓冲不表示接受，也不自行发布事件；actor 仍负责执行动作、结算拒绝、再回放，发生 fault 后不会继续公开回放。初始化与扫描的 `record_event()` 返回 `()`，不伪造不会发生的错误；actor 的协调方法仍返回 `Result`，因为连接侧的通知模式验证可以失败。

| 已移除的冗余 bool | 唯一证据来源 |
|---|---|
| scan `failed` | start 的 `progress.terminal_failed()`。 |
| connection `failed` | `connect_error` 为 `Some(_)`。 |
| discovery `failed` | discovery 的 `progress.terminal_failed()`。 |
| discovery `discovered` | `draft_database` 为 `Some(_)`。 |

独立 domain 事实仍需保留。拿到 draft database 并不表示 discovery 已成功，还必须确认 dispatch 被接受且 terminal 成功。

### 子命令进度复用不统一操作准入

`ConnectionReservation::child_progresses()` 枚举 disconnect、discovery、RSSI、read、write 和 subscription transition 的 `CommandProgress`；`has_child_awaiting_dispatch_result()` 只查询其中尚未确认 dispatch 的命令。`command_progresses()` 再加入 connect 根，仅用于整连接的 accepted command 收集、publication barrier 和根退休等集合查询。命令归属、terminal、取消和单命令进度查询统一使用 `progress_for_cmd()` / `progress_for_cmd_mut()`：固定槽位检查后，I/O 直接查询已有 command map，订阅 transition 则交由 `SubscriptionReservations` 在其私有集合内按 progress 查找；不再提供可变全量进度迭代器。disconnect 的取消决定由 `DisconnectReservation::cancellation_decision()` 返回；connection 只定位命令并优先采用该决定，不能被共享 progress 查找绕过。策略仍为未完整完成时 `NotCancellable`、完整完成后 `AlreadyCompleted`。

每个 `IoReservations` 已属于唯一连接，不另建 `ConnectionId` 索引。普通 RSSI/read/write 回调按事件的 command ID 查已有 map，再由叶 reservation 验证 dispatch、connection/session/characteristic identity。连接状态事件不能这样分流：即使带有 command ID，loss 仍可能影响其他子操作，继续按既有类别优先级寻找未决缓冲目标。characteristic 冲突检查与批量退休保留集合遍历，不为消除所有遍历维护第二份状态。

共享 predicate 只复用相同条件，不抹平各操作门禁或错误优先级：

| 操作 | 单连接记录保留的检查 |
|---|---|
| disconnect | 不要求已经收到 `Connected`；connect 已接受即可拥有 native 生命周期。先排除 disconnected/connect failure/未确认 connect，再检查已有 disconnect 与未确认的子命令。 |
| discovery | 先要求连接 active；disconnect、未确认 RSSI、pending discovery、read/write 或任何订阅 route 均返回 `GattBusy`。已接受的 RSSI 不因共享 predicate 被一概阻塞。 |
| RSSI | 先要求连接 active；已有 RSSI、disconnect 或未确认的子命令返回 `OperationBusy`。 |
| read/write | 保留连接 → disconnect/未确认 RSSI → committed session/characteristic → I/O/订阅冲突 → discovery 的顺序；目标错误仍先于后续 busy。read 还排斥同 characteristic 的订阅，write 不增加此限制。 |
| enable | 使用 `GattAccess::EnableNotifications` 复用 GATT 检查，在 pending discovery 检查后拒绝同 characteristic 已有的订阅 route。 |
| disable | 容器先找到连接；记录内先检查 subscription/session 与 active，再检查 transition busy，然后检查连接与兄弟操作。`Disabling` 仍是 active route，因此重复 disable 返回 `OperationBusy`，不是 `SubscriptionNotActive`。 |

`GattAccess::{Read, Write, EnableNotifications}` 替代 `read: bool`，由 `validate_gatt_access()` 明确表达准入目的，而不是让 enable 假装成 write。`SubscriptionReservations::validate_characteristic_access()` 只负责前置订阅局部规则：transition 占用阻止目标操作，稳定 route 阻止 read。connection 保留随后与 I/O、discovery 和共享容量的协调；enable 的稳定重复 route 必须在 discovery 检查之后拒绝，不能在局部前置检查中提前合并。枚举不统一这些错误优先级，也不表达 backend capability。

`is_connection_lost()` 是 reservations 共同层的纯事件判定，由 `mod.rs` 定义，供 actor 及各子 reservation 使用，不要求叶模块反向导入 connection owner。它只识别预期连接的 Disconnected 或 passive error；普通子命令拒绝和订阅拒绝是否允许回放该 loss，仍由各自策略决定。

这些迭代器不是 callback 路由索引，也不替代全局 pre-ack 类别顺序。

## 证据、结算与回收顺序

```text
publish to bounded event FIFO
  -> record domain / terminal evidence
  -> settle affected waiters from reservation evidence
  -> retire completed reservations / commit successful discovery
```

必须先记录证据，再通过 [`WaiterRegistry::finish()`](../../../crates/bleng/src/waiter.rs) 恰好一次交付结果（control 未决时先在同一有界 entry 保留首次完整结果），最后释放已完整记账的状态或提交数据库。`settle_waiter()` 先查 `WaiterRegistry::contains(command_id)`；entry 已移除时不再物化结果（例如克隆数据库或 read payload），但不跳过晚到证据和 retirement/commit。receiver 已关闭但尚未被 registry 回收时，entry 仍可能存在；关闭 receiver 本身不是 actor 已完成 entry 回收的证明。domain 与 terminal 的先后不作假设；dispatch ack 前逃逸的回调先缓冲，再按关联规则处理。

同步 begin 返回后，timeout/已领取 ticket 的等待 future drop/最后一个未等待 handle drop 只关闭 caller delivery，不撤回排队中的 eager submission，也不取消已 claim 的原生命令。晚到回调仍可推进初始化就绪、原生/session slot 回收或 GATT commit，但不能重新打开结果 receiver。只有显式 `cancel()` 竞争 preclaim 本地取消或请求 actor/native control；其接受回执、原操作 terminal 与 native retirement 是三个不同事实。

## 事件层与 reservation 的分工

`runtime/messages.rs` 中的两个异步输入 handler 只转交消息，不执行状态协调。`actor.rs` 在两个受限入口及私有 helper 内负责事件路由与副作用执行；`InitializationState`、`ScanState`、`ConnectionReservations` 统一提供三个有实际职责的入口：

| 入口 | 所有者负责的行为 |
|---|---|
| `gate_event(event)` | 在公开发布和证据记录前检查实时 callback；dispatch 结果未确认时，关联事件进入原有有界缓冲，此时 native dispatch 可能已经执行。`Ok(None)` 表示已缓冲而非丢弃；`Ok(Some(event))` 继续交给下一个所有者或正常处理，既可能是不归当前所有者的事件，也可能是当前所有者无需再缓冲的事件，因此它不是所有权查询或过滤器。overflow 或初始化拒绝后的矛盾证据返回错误。 |
| `record_event(&event)` | 校验 command/connection/session/target，修改本地证据；不发布事件、不结算 waiter、不执行生命周期动作或完成退休。 |
| `resolve_dispatch(command_id, result)` | 接受 dispatch 并取出缓冲，或按操作规则移除被拒绝的 provisional slot；返回回放列表和必要的后续动作。连接 reservation 确认消费 rejection 后，coordinator 同步移除 command directory entry，避免等待已无法再发现该命令的后续 retirement。 |

这套约束不只适用于初始化：scan 与 connections 的 `waiter_result()` 返回 `None` 同样可能表示未认领或证据未齐；已选定 I/O/GATT 叶子记录的无 command ID 查询则表示证据未齐。`record_terminal()` 在需要继续路由的层返回是否认领，connections 作为 actor 的最终路由无需返回布尔值。`retire_completed()`、`retire_completed_discovery()`、`retire_transition()` 保持资源退休的命名，但必须在 waiter 结算后执行；订阅的 `retire_transition()` 返回 `None` 表示没有可退休转换；`Some(SubscriptionTransitionRetirement)` 同时给出已完成的 command ID 和订阅的 `Keep` / `Remove` 处置，保留订阅不等于命令成功。`Remove` 由 `SubscriptionReservations` 实际执行删除，状态对象不自行管理外层容器。

叶子 reservation 的宽泛关联方法统一命名为 `matches_related_event()`，由缓冲和证据记录复用；它可匹配 terminal 或影响操作的连接丢失，不表示成功，也不表示该事件只与一个 reservation 相关。缓冲入口统一使用 `should_buffer*()` 决策，在 reservation 内部组合 dispatch 未决与事件关联：init、disconnect、GATT discovery、RSSI、read/write 和订阅各自提供 `should_buffer_event()`；没有 transition 的 Active 订阅返回 false。扫描会话使用 `should_buffer_start_event()`，其 start 指 scan-start 命令，范围包括同 session 的 advertisement 和 `ScanStopped`；stop 使用 `should_buffer_event()`，也涵盖被动结束。连接根使用 `should_buffer_connect_state_event()`，只对状态回调保留 command 或 connection 任一匹配的宽泛缓冲规则；terminal 已由通用 command gate 处理，不再在这个私有谓词中重复匹配。复用于证据记录的 `matches_related_event()` 仍是纯关联查询，不能加入 dispatch 未决条件；init 的纯关联函数也继续用于拒绝后晚到回调的矛盾检查。容器层只选择候选连接，记录负责类别优先级；不能因 command 路由而绕过连接级状态事件的门禁。记录时仍先按 domain event 类型分支，因此匹配 terminal 或断连的宽泛缓冲规则不会被误用为 read/RSSI/value 等证据。连接状态与 explicit disconnect domain 在同一次连接查找中记录；外来断连过滤与断连证据记录复用同一生命周期命令归属判定，属于本连接的 I/O command 也不能冒充 connect/disconnect 命令。首次 snapshot/read/RSSI/subscription 证据保持不变；GATT draft 仍保留最后一次关联结果，未改变既有策略。

初始化 API 的阶段约束保持明确：登记原 submission 的 sender 前检查准入，原子 dispatch claim 成功后才安装 reservation；记录证据后先结算 waiter，再调用 `advance_after_settlement()` 推进就绪状态或报告已接受操作的失败。该方法返回 `Ok(())` 不代表已经 Ready，证据不足时仍保留 Pending。`waiter_result()` 返回 `None` 也不代表“不归初始化所有”，还可能是证据未齐；与之不同，`record_terminal()` 的布尔值表示命令所有权，`resolve_dispatch()` 返回 `None` 表示没有匹配的 Pending 初始化。状态对象不接管 waiter registry 或 lifecycle 副作用。

pre-ack 探测顺序保持：

```text
initialization
  -> connections: 受影响连接 [disconnect -> discovery -> RSSI -> read -> write -> subscription -> connect]
                  -> 外来 command 的 connect fallback
  -> scan start -> scan stop
  -> 未缓冲事件进入 process_event
```

集合不再为每个事件执行“所有类别 × 所有连接”的探测。coordinator 对有 command route 的非状态事件仍沿既有路由定向选择连接；fallback 中的 domain event 按事件中的 connection ID 选择连接，terminal 按持有 command progress 的连接选择，无关事件直接放行。单连接仅对可能影响多个命令的状态事件保留上述类别顺序，其他事件只检查对应类别；enable/disable 使用同一个 `SubscriptionTransition` 门禁类别，不与 command kind 混同。状态事件即使携带已登记 command，也必须先检查其 connection ID 所指向的连接，再考虑外来 root connect 的宽泛关联，因此不会由 HashMap 遍历顺序决定子命令与外来 connect 的先后。不新增第二套连接或命令索引；按 connection ID 查找仍为现有 peripheral-keyed 容器上的线性查找。actor 对带外来 command 的已知连接断连事件仍保留公开交付、但禁止其进入缓冲或修改生命周期的防护。

`DispatchResolution` 是 dispatch 返回与早到回调归并后的临时处理结果，不是新增的 canonical state：其中 `events_to_replay` 将允许回放的事件移交给 actor，`action: DispatchAction` 表示尚待 actor 执行的动作：`NoAction`、`StartCleanup` 或 `FaultRuntime(ReconciliationFault)`。这种移交只是同步返回待执行工作，不是 `OperationSubmission::try_claim()` 的 caller/actor 提交认领 handoff，也不转移原生所有权或表示操作完成。`StartCleanup` 只要求启动清理，不承诺清理已经完成。actor 按以下顺序执行：

```text
reservation 局部 dispatch 变化
  -> actor 执行 cleanup / fault（如需要）
  -> rejected command 交付 DispatchRejected
  -> 按顺序回放允许的事件
       -> 发布 -> 记录 -> 结算 -> 退休
```

普通 GATT/read/write/RSSI/disconnect 的拒绝只允许回放被动 loss；订阅拒绝保留原来的较宽 connection-loss 规则。其他已逃逸 callback 是矛盾证据，要求 fail closed。初始化派发拒绝后转入 `Rejected { command_id }`，释放 pending 证据，只保留一个命令 ID 识别已逃逸的晚到回调；不会重新允许初始化，也不会继续按派发前事件缓冲。干净拒绝只启动 Hub cleanup，不把 runtime 改成 Faulted；若之后仍到达关联 snapshot 或 terminal，则判定为 `DispatchEvidenceConflict`，在公开发布前 fail closed。无 command ID 或其他命令 ID 的事件仍走正常事件流，不改变初始化状态。重复 dispatch result 不会重新认领已拒绝的操作。`reserve_initialize()` 断言当前为 `NotStarted`，防止内部调用绕过 admission 后覆盖既有状态。`ReconciliationFault` 区分缓冲超限、实际通知模式不符和派发结果与事件证据冲突（`DispatchEvidenceConflict`），actor 使用已有 tracing 记录原因，不记录 callback payload。

每个命令的有界缓冲由 `CommandProgress` 统一限制为 64 条，不为重构添加新队列、锁或 executor。回归见 [`reconciliation.rs`](../../../crates/bleng/src/runtime/tests/reconciliation.rs)：覆盖精确容量、外来事件不消耗容量、原序回放，以及 I/O 与订阅拒绝的不同 loss 策略；既有同步 callback、晚到证据和取消测试继续保护完整 actor 流程。

## ScanState 与扫描回收

会话事实、显式停止命令证据与 waiter 生命周期的区别，详见 [扫描 reservation 专题（中文）](bleng-scan-reservations-design-2026-10-09.zh-CN.md) / [English](bleng-scan-reservations-design-2026-10-09.md)。

```text
Idle
  -> admission + submission dispatch claim 成功
  -> Reserved(ScanReservation)
       -> 匹配的 start dispatch 被拒绝 -> Idle
       -> 完整证据记账、先结算 waiter、再退休 -> Idle
```

`ScanState` 与 `InitializationState` 一样是不透明的状态所有者：`phase`、`ScanPhase`、`ScanReservation` 和 `StopReservation` 均为私有，外部只能通过准入、登记、证据和退休方法操作，不能直接构造 variant 或替换 payload。

`ScanReservation` 持有扫描会话槽位；其 `stop: Option<StopReservation>` 占据会话内唯一的显式停止命令槽位，并保留自己的 command progress 和停止错误。与 connection 的 `connect_domain_error` 不同，`ScanStopped::Err` 没有 command ID，不能归属于 start；没有显式 stop 时它只建立 scan 会话结束事实。`Reservation` 既可以表示资源会话占位，也可以表示会话内部的命令槽位及其关联证据；嵌套关系描述所有权，不要求 reservation 独立存在。这与 connection 内的 `DisconnectReservation`、`RssiReservation`、`ReadReservation` 等命名一致。调用方 waiter 退休不释放尚未完成的停止命令关联；停止操作失败但扫描仍未结束时，只移除 stop，保留会话以允许重试。`ScanState::gate_event()` 只选择当前会话；start/stop 缓冲路由下沉到 `ScanReservation::gate_event()`。`CommandProgress::has_accepted_terminal_failure()` 统一检查 dispatch 已接受且 terminal 失败；父结构在完成结算并先检查整个会话能否退休后，决定只删除失败的 stop 槽位；`start_failed()` 明确只指启动命令失败。

`Reserved` 只表示持有 scan slot，不等于已经开始射频扫描；它覆盖启动中、扫描中、stop 中以及 native 已停但晚到 start/stop 证据尚未齐全的阶段。没有额外增加 Running/Stopping 等重复状态，也不把独立的 domain、terminal 和 dispatch ack 事实压成线性 enum。

实际 payload 使用 `Box<ScanReservation>`，装箱避免空闲 scan slot 也占据整份会话的空间；不把重构前的布局测量当作当前类型大小。代价是每次成功登记 scan 多一次分配，不是每个回调分配；不新增 `Arc`、锁、执行器或共享状态。

相较于 actor 持有 `Option<ScanReservation>`，新增类型的价值不是形式一致或方便宏转发，而是把 slot 可用性、登记和释放的策略放在同一个所有者上。`ScanState` 统一处理 terminal/result 路由、拒绝和退休，actor 只协调结算顺序。重复安装不能覆盖仍保留的会话；晚到的旧 start rejection 不能释放新会话。状态所有者内部通过私有 `reserved()` / `reserved_mut()` 访问当前会话，这些方法不作为 actor 的 getter。start 安装断言复用 `require_scan_available()`；stop 安装断言复用 `validate_stop()`，在 dispatch、start domain 或 terminal 不完整，以及已有 stop 或 session 已结束时拒绝内部误用，避免覆盖仍待结算的命令。


scan retirement 不再接收原始 event 参数，只根据已关联的 reservation 证据（包括 `stop.stop_error`）决定回收。外层 `retire_completed()` 明确先调用纯查询 `is_retirable()`，可移除整个 session 时转 Idle 并返回；否则调用 `retire_failed_stop()`，仅清除已接受且 terminal 失败的 stop 命令，以便当前 session 重试。父结构将启动进度明确命名为 `start_progress`；退休查询依次检查 start 证据完整、存在的 stop 证据完整、启动失败或会话已结束。`StopReservation::is_complete(session_end_evidence)` 统一支撑 waiter 结果和退休的就绪判断，父级退休查询不重复解释子级错误与 terminal。后者不删除 session，也不决定 caller 结果；不再通过 `is_some_and()` 隐藏修改，或让同一个布尔 helper 同时修改子状态并决定根删除。无关 scan 的 `ScanStopped::Err` 不能回收当前 scan 或丢弃其 pending stop 关联。

scan 已停止也不代表 start 已完整记账：若 start 的 domain/terminal 证据不完整，必须保留 reservation，等证据足以结算 start waiter 后再回收。两种事件顺序、dispatch ack 前缓冲与晚到回调均遵循先结算、后回收。

stop dispatch 被拒绝时，不能把缓冲的全部 `ScanStopped` 都判为矛盾。[契约的 `command_id()`](../../../crates/blehub/src/contract/events.rs) 明确区分：`SystemSuspended` 与 `ScanStopResult::Err` 没有 command ID，是独立的原生结束证据；`Requested` 则携带 command ID。`StopReservation::resolve_dispatch()` 负责拒绝事件分类，`ScanState::resolve_dispatch()` 负责路由并移除被拒绝的 stop reservation，允许的被动结束按原顺序回放；actor 先交付 `DispatchRejected`，再发布并记录回放事件，随后退休旧会话。被动结束不能把拒绝变成 stop 成功；被拒绝命令的 terminal 不参与回放。只有 command ID 属于本次被拒绝 stop 的 buffered `Requested` 才触发矛盾 fault，即使同批还有被动结束事件。其他命令的 `Requested` 保留公开回放，但不作为当前 stop 的结束证据；同会话的宽缓冲不能代替精确的命令归属检查。

`passive_scan_end_before_rejected_stop_preserves_rejection_and_allows_restart` 覆盖被动暂停和原生扫描错误两种情况，验证拒绝优先、事件可见、槽位释放、重新扫描，以及旧 session/command 的晚到事件不影响新扫描。已有 rejected-stop 回归扩展为两种混合回调顺序，保证 fail-closed 策略不因允许被动回放而放宽。`stop_rejection_after_failed_stop_ignores_foreign_requested_evidence` 另覆盖旧 stop 迟到通知与陌生命令 ID，验证它们不会把新的 dispatch 拒绝误升级为 runtime fault，且会话仍允许后续 stop。

## Begin 消息与唯一 submission/result 通道

`Begin` 表示 actor 对一次已提交请求执行 admission/dispatch claim，不代表所有 BLE 资源都要「启动」，也不代表操作已完成。`BeginStop` 是开始处理 stop，`BeginDisconnect` 是开始处理 disconnect。handler 的 `Reply = ()` 不交接 receiver 或资源；owner 使用 tell 而非 ask。backend 是否接受命令由 `DispatchResolved` 另行报告，BLE 完成仍依赖 reservation 证据与原 waiter 结算。

[`OperationSubmission<Resource = ()>`](../../../crates/bleng/src/runtime/owner.rs) 在 enqueue 前建立，携带既有 `CommandId`、共享 `SubmissionState` 和 `Arc<OnceLock<Resource>>`。`SubmissionState` 只持有本地提交阶段、唯一 `WaiterSender` 与一次写入的 completion observer；actor admission 成功后写入 resource metadata。scan、connection、GATT discovery 和 notification enable 写入新 typed resource ID；不新建资源的命令写入 `()`。这些是身份/交付元数据，不是第二套 BLE 状态。`BeginReply` 已不在当前协议中，receiver 从提交时起就属于原 ticket/slot，不再经 actor reply 转手。

`WaiterSender` 的克隆只共享 `Arc<Mutex<Option<oneshot::Sender<...>>>>`；本地取消、admission 拒绝和 actor 结算竞争取走同一个 sender，最多发送一次。`OperationTicket`、`ResultSlot` 与具体 handle 都围绕这一个 channel，不能为每种操作或每层 wrapper 再建结果 channel。`WaiterExpectation` 仍只描述 typed result 与 connection invalidation 规则，不拥有 receiver 或 native 状态。

### 统一 mailbox 协议与准入顺序

`messages.rs` 按操作、异步输入和取消 control 组织：

1. 11 个请求 `BeginInitialize`、`BeginScan`、`BeginStop`、`BeginConnect`、`BeginDisconnect`、`BeginDiscoverGatt`、`BeginRssi`、`BeginRead`、`BeginWrite`、`BeginEnableNotifications`、`BeginDisableNotifications`，对应 `OperationResult` 与 unit-reply admission handler。请求/trait 声明为 `pub(crate)`，经 runtime 受限重导出供 facade 的共享 `submit_*` 构造。
2. `IngressEvent`、`DispatchResolved` 及仅转交 actor 受限入口的 handler；派发结果不是新 admission 请求。
3. native cancellation 的 `CancellationResolved` 与 `RequestCancellation`；后者请求既有 command 的 control receipt，不分配新 CommandId。control receipt channel 与唯一 operation result channel 职责不同，不是第二个操作结果。

backend_port 从 messages 导入 `IngressEvent`，actor 导入 dispatch/cancellation 结果消息。公开订阅参数映射与 typed handle 归 facade，订阅 BLE 状态归 `reservations/subscription.rs`。`RetireWaiter` 已删除；timeout/Drop 不要求 mailbox 容量，不 spawn retirement fallback。

Begin handler 显式编排操作特定步骤：先检查 lifecycle/初始化与具体条件，分配所需资源 ID，再通过 `register_operation_waiter()` 登记 submission 原有 ID/sender，然后 `launch_dispatch()`。后者先重新获取可派发 Hub，再竞争 submission claim；成功后同步安装 reservation、关联 completion observer 并提交 blocking dispatch。claim 与 reserve/task launch 之间没有 await 或正常错误分支。handler 最后调用 `submission.resolve(result)`：成功写入 resource，拒绝经共享 CAS 和原 sender 交付。验证失败不安装 reservation；本地取消先赢时不派发，已消耗的 ID 不回滚重用。

局部变量明确区分 `command_id`、新建的 `session_id` 与连接集合的 `peripheral_id`。`OperationResult` 仅定义成功解码，不抽象 admission/派发流程；不新增 mega message enum、通用执行 trait 框架、handler 生成宏、队列或额外 BLE 状态机。验证/expectation/reservation/派发之间的显式对应关系有意保留。

### `Result` 成功语义分层

保留各层 `Result<(), E>`：unit 表示该层没有额外成功数据，不表示没有错误或结果记账，也不合并各层 Error 或增加无信息成功 marker。

| 层次 | 成功含义 |
|---|---|
| 同步 `begin_*` / `submit_operation()` | 请求已进入有界 mailbox，返回 typed handle；不表示 actor admission、dispatch claim、backend 接受或 BLE 完成。 |
| actor 验证 | 检查通过；不表示 claim、backend 接受或 BLE 完成。需要返回 ID 等数据的验证仍保留原值。 |
| `launch_dispatch()` | dispatch claim 成功、reservation 已安装、派发任务已提交；不表示 backend 已进入或接受命令，不向 caller 返回 Begin reply。 |
| handle `cancel()` | 本地 preclaim 取消获胜或 actor/native control 请求被接受；不消费 operation result，不保证原生已经停止或远端回滚。 |
| 同步 backend `Result<(), CommandDispatchError>` | `Ok(())` 表示派发接受，`Err` 表示拒绝；完整结果通过 `DispatchResolved` 送回 actor，不忽略，也不当作异步操作完成。 |
| 异步操作 / `WaiterSuccess` | 由 dispatch、domain 与 terminal 证据结算；read 数据、RSSI、数据库、ID/模式等有数据的成功结果保留原值。平台 write-without-response 的本地提交/进展不提升为远端处理保证。 |
| cleanup | `Ok(())` 表示已完成记账且没有报告清理错误；结果 slot 保留成功/错误。单纯「所有 slot 已结算」也可能产生 `Err`，不等于清理成功。 |

## 本地取消、拒绝与 dispatch claim 的原子边界

```text
同步提交建立 PENDING submission，并将 Begin 消息入队
  -> actor 执行全部可失败的操作检查 / 分配资源 ID
  -> 用原 sender 登记 waiter（已本地取消则跳过登记）
  -> launch_dispatch：获取 Hub 并检查 lifecycle
       准备失败 -> discard admission draft -> submission.resolve(Err)
                    -> rejection CAS；仅获胜者交付对应错误
       准备成功 -> submission.try_claim()：PENDING -> CLAIMED
                    取消先赢 -> 清除 delivery draft；不建 reservation、不派发
                    claim 获胜 -> 同步、不可失败的 reserve closure
                                  -> 关联 CommandProgress completion observer
                                  -> spawn_blocking 原生命令
  -> handler submission.resolve(Ok(resource)) 写入一次性 metadata
```

command ID 和唯一结果通道在提交时就已建立；actor 登记的是同一交付能力，不等待 caller 接收任何 begin reply。claim、reservation 安装和 blocking task 启动之间没有 `await`，reserve closure 中也不执行可失败的操作检查，原生命令不可能先于 reservation 创建而开始执行。取消若先于 claim 获胜，不需要恢复旧 reservation/session；初始化仍为 `NotStarted`，已提交 GATT database 和订阅 route 不被临时修改。`ReservationRollback`、局部 prelaunch rollback 与 `RollbackOutcome` 不属于当前实现。

[`SubmissionState`](../../../crates/bleng/src/runtime/owner.rs) 用同一个 `AtomicU8` CAS 仲裁三种互斥转换：

```text
                         +-- explicit cancel CAS --> CANCELLED
PENDING -----------------+-- admission reject CAS -> LOCALLY_SETTLED
                         +-- actor claim CAS ------> CLAIMED

CANCELLED / LOCALLY_SETTLED：本地结果已结算
CLAIMED：后续 cancel 走 actor/native control，不改成本地取消
receiver close：不改变上述任何 phase
```

- 显式 `cancel()` 在 PENDING 获胜：通过原 sender 交付 `Cancelled` 并返回接受回执；不创建 native reservation、不发原生命令，也不伪造 BLE terminal。queued message 仍占 mailbox 容量，直到 actor 消费它；取消不是队列摘除。
- admission 拒绝先赢：PENDING → LOCALLY_SETTLED 后才发送拒绝，避免「先发布错误、后让取消虚报成功」。后续 cancel 返回 `OperationAlreadyCompleted`，不把拒绝改写为 Cancelled。
- claim 先赢：`Claimed` 只确立 actor 的派发所有权，不保证 native 已进入。后续 cancel 经 `RequestCancellation` 进入 actor 既有 cancellation state，并等待真实 control receipt。initialize/disconnect 在 claim 后保持 `OperationNotCancellable`；其余操作仍可能被 backend 精确拒绝，拒绝不取消原结果 waiter。native retirement 继续由 reservation/证据决定。

`OperationControl` 不含结果 receiver，不会因并发 wait 领取 slot 而 consumed。`SubmissionState::is_completed()` 结合本地已结算阶段与 actor 关联的单调 `CompletionObserver`；它只帮助 typed control 在 reservation 已退休后识别 AlreadyCompleted，不建立 native tombstone registry。claim 后的本地 phase 不替代 `CommandProgress` 的 dispatch/domain/terminal/cancellation 状态。

owner 预检查与 actor 准备统一调用 `RuntimeLifecycle::hub_for_dispatch() -> Result<Arc<BleHub>, WaiterError>`。只有 cleanup 保留底层 `Option` 查询；lifecycle 在获取 cleanup 锁之后检查 phase，Closed/Fault 优先于缺失 Hub。owner 的临时 Hub Arc 在 command ID 分配和 enqueue 前释放，Begin 消息只携带 submission 与参数；actor 在处理排队请求时重新取派发引用。Hub 不可用、close 或 fault 时先移除 admission draft，再由 rejection CAS 决定是否交付；actor 不新增长期 Hub 字段，也不把预检查当作抵抗并发 cleanup 的 lease。

`try_claim()` 是改变派发所有权的 CAS，不是只读取消探测，必须放在可失败的 Hub/lifecycle 准备之后。early return 顺序为准备失败、claim 失败，然后才是不可失败的 reserve/task launch。reserve closure 将局部安装延迟到 claim 成功后，不增加安装 enum、trait 框架或 rollback。

当前没有 `BeginHandoff`、begin-reply acknowledgement、begin timeout 或生产 `scopeguard` abandonment 协议。丢弃结果不会因「claim 后但 reply 前」而将 owner fault；没有这样的 caller 确认窗口。行为边界是：

```text
eager submission 已入队
  +-- wait timeout / 已领取 ticket 的等待 future drop / 最后一个未等待 handle drop
  |     -> 关闭唯一结果 receiver；不取消、不撤回、不因 delivery 关闭 fault
  |     -> actor 仍可 claim / reserve / dispatch；native state 保留至正常退休
  +-- explicit cancel
        +-- PENDING CAS 获胜 -> 本地 Cancelled，无 native reservation/dispatch
        +-- rejection 已赢 -> AlreadyCompleted，保留原拒绝
        +-- CLAIMED -> actor/native policy + control receipt
```

接收端关闭与本地取消完全独立：前者可以发生在 admission 前或 claim 后，都不修改提交阶段。`WaiterTicket::Drop` 释放 receiver 与其 waker，不发送退休消息、不请求 native cancel；`WaiterRegistry::register_sender()` 在新登记时回收关闭的 delivery entry，扫描范围最多 64 项。已关闭但暂未回收的 entry 不构成无限 tombstone；后续 evidence 仍可结算/移除 entry，reservation 的完成、commit 与 native retirement 不以 receiver 存活为条件。

### 通用复用与保留的领域责任

| 机制 | 当前复用 / 必须保留的边界 |
|---|---|
| shutdown 结果等待 | `watch::Receiver::wait_for(Option::is_some)`；只检查通道值并克隆结果。 |
| 同步 submission 仲裁 | `SubmissionState` 的 PENDING → CANCELLED / LOCALLY_SETTLED / CLAIMED CAS；只仲裁提交所有权，不拥有第二套 BLE 状态；无需 begin 交接 RAII。 |
| facade task 生命周期 | [`facade/mod.rs`](../../../crates/bleng/src/facade/mod.rs) 用 `tokio_util::task::AbortOnDropHandle::new(private_handle.spawn(future)).await`，取代自定义 `AbortOnDrop`；外层 future 释放只请求 abort 本地 task，不取消原生工作，也不保证立即析构。 |
| caller delivery 关闭 | `WaiterTicket` 保留命令关联与 deadline；Drop 关闭原 receiver，registry 有界回收关闭的 entry；无 `RetireWaiter`、发送失败 fault 或后台 fallback。 |
| cleanup 完成 | 保留 subscribe → Mutex ledger 检查 → 解锁 → `changed().await` 循环；`CleanupSignal` 只是唤醒，锁序禁止把 ledger 检查放进 `wait_for` predicate。 |
| cleanup 执行与 phase | 保留 shutdown 后仍可调度的独立 OS worker，以及携带错误语义的 phase/结果；`CancellationToken` 不能替代。 |

历史生态复用记录曾报告根 workspace 新增 `scopeguard = "=1.2.0"`（`default-features = false`）与 `tokio-util = "=0.7.19"`（`default-features = false`，只启用 `rt`），BLeng 通过 workspace 复用；当时 `Cargo.lock` 仅新增这两个 package，其余所需 package 已存在，已离线解析。当时本地缓存源码审计覆盖 scopeguard 的 `guard` / `Drop` / `into_inner`、tokio-util 的 `Future` / `Drop` 和 Tokio 1.53.1 的 watch `wait_for` 读锁行为；原记录见[英文复用记录](bleng-waiter-design-2026-10-06.md#simplification-and-ecosystem-assessment)与[cleanup 锁序约束](bleng-cleanup-design-2026-10-07.zh-CN.md)。这些是当时的依赖变更/审计证据，不表示当前生产 begin 仍使用 scopeguard。当前 facade task 的 `AbortOnDropHandle` 与既有独立 cleanup worker 继续各司其职；本次仅同步文档，不修改依赖或 ABI artifact，不把历史「无 ABI 变化」结论应用到新增同步 public surface。

## GATT 提交、保留与局部回收

`ConnectionReservation.gatt` 始终是具体的 `GattReservation`，不再是 `Option<GattReservation>`。已提交的 `database` 为私有字段，connection 通过 `require_characteristic(session_id, characteristic_id)` 校验目标，由 GATT owner 内部查 session/characteristic；数据库缺失、session 不匹配或 characteristic 不存在仍返回 `GattNotReady`，且该检查仍先于后续 I/O/订阅冲突与 discovery busy。只有 GATT owner 可以提交替换。其 `database: Option<GattDatabase>` 与 `discovery: Option<GattDiscoveryReservation>` 是两个独立槽位：两者都为空表示尚无数据库或 pending discovery；rediscovery 期间两者可以同时存在，不能改成互斥 enum。draft 仍属于 discovery。

```text
ConnectionReservation.gatt: GattReservation
  +-- database: None / Some(已提交数据库)
  +-- discovery: None / Some(progress + session_id + draft_database)
       完整成功 -> 先结算 waiter -> 替换 database -> 清除 discovery
       拒绝/失败 -> 只清除 discovery -> 保留 database
```

waiter 先结算，随后 `retire_completed_discovery()` 退休已完成的 discovery，仅在成功证据完整时替换已提交数据库。此处不返回 bool，也不清空订阅：discovery 准入要求所有订阅已移除，pending discovery 又阻止新 enable，原生 callback 不会创建 route，因此完成时订阅容器必为空。外层 `Option`、`get_or_insert_with` 和「内层全空就将外层归一化为 `None`」的代码已无必要。

rediscovery 的 dispatch rejection 或 terminal failure 仍只移除临时 discovery，不抹掉已有数据库和可用 session；显式取消在 claim 前获胜则根本不创建临时 discovery，单纯 timeout/Drop 不撤回 eager submission。`IoReservations::resolve_dispatch()` 统一 RSSI/read/write 的命令定位、接受确认和拒绝移除：接受则转交 buffered events；拒绝则先分类并取出缓冲事件，再删除目标 command entry。connection 注入 `classify_rejected_child_event()`，只有独立的被动断开可以回放，其余回调是矛盾证据；I/O 集合不反向依赖 connection 的私有实现。`GattReservation::resolve_dispatch()` 自行确认或移除 discovery；`DisconnectReservation::resolve_dispatch()` 处理自己的派发证据，拒绝后由 connection 清除其 disconnect slot，两者沿用同一被动 loss 分类规则。discovery 的 slot 与已提交数据库分离，因此移除不会抹掉旧数据库。订阅派发结果经 `SubscriptionReservations::resolve_dispatch()` 转交单条 reservation：它仍允许 commanded connection loss，拒绝时先排空 buffered events，再返回 resolution 与 `Keep` / `Remove`，集合立即应用处置；enable 移除 provisional route，disable 恢复 Active。这些是 dispatch 拒绝/失败后的移除，不是 prelaunch rollback。`retire_completed_operations()` 与 `is_retirable()` 是证据完整后的 retirement，在 connection 内决定局部回收及连接是否可以移除，actor 仍拥有外层 `ConnectionReservations` 容器。

terminal 采用首个结果，重复且冲突的 terminal 不能在等待数据库期间覆盖已有结果。这与「保留已提交数据库」一起防止无效 rediscovery 破坏旧 session。

### 订阅状态与显式移除结果

[`SubscriptionReservations`](../../../crates/bleng/src/runtime/reservations/subscriptions.rs) 拥有单连接的订阅集合；其 `validate_disable()` 先定位 route，再交给单条订阅校验 session/activity 与 transition busy，connection 随后检查连接和兄弟操作，保持原错误优先级。单条 [`SubscriptionReservation`](../../../crates/bleng/src/runtime/reservations/subscription.rs) 保留 session/characteristic 身份，私有 `phase: SubscriptionPhase` 只表达以下合法组合：

```rust
enum SubscriptionPhase {
    Enabling {
        mode: NotificationModeRequest,
        transition: NotificationTransition,
    },
    Active,
    Disabling(NotificationTransition),
}
```

不再并列保存 `active: bool` 与可选 transition；`NotificationTransition` 只持有 `progress` 和 `domain_evidence: Option<NotificationState>`，不重复保存 action。`SubscriptionPhase` 自己实现阶段查询、progress 访问、disable 安装、状态与转换方向的匹配，以及阶段迁移和路由的退休/拒绝处置。`NotificationTransition::record_enabled()` / `record_disabled()` 负责模式校验与首个领域证据保留，`is_complete()` 合并 dispatch、terminal/domain 与取消控制屏障；transition 不重复保存 enable/disable 方向，也不决定订阅路由是否保留。`SubscriptionReservation` 通过已有 `delegate!` 转发阶段接口，不重复拆解 phase。命令归属、是否存在转换及是否等待 dispatch 结果由 phase 查询解释，外层事件关联不再取出原始 transition 或读取其 progress。related 判定中的断连仍按连接范围关联，不套用通知状态/terminal 的 command 所有权条件，也不改变 actor 对外来断连事件的防护。事件中的 command/connection/session/subscription/characteristic 归属仍先由 reservation 校验，再把已确认归属的 `NotificationState` 交给 `phase.record_state()` 选择对应 transition 记录入口；结果可交付性的判断由 `NotificationTransition::result_for_cmd()` 负责：先匹配 command，再等待已接受派发的 terminal，terminal 错误先于领域证据；phase 的 `transition_result()` 只选择当前转换。reservation 仅把成功的 `NotificationState` 映射为带订阅身份的 typed waiter 结果，不重复判断命令证据或退休条件。`NotificationTransition` 与 phase 的 `transition()` 均为订阅内部细节；集合通过 `progress()` / `progress_mut()` 访问单条订阅的共有命令进度，通过 `waiter_result(command_id, connection_id, subscription_id)` 获取具名结果，不再读取 transition 或拼装结果身份；`is_active()` 对 `Active` 和 `Disabling` 均为 true，`is_transitioning()` 对 enable/disable 为 true。disable 完成前保留 route 不等于承诺远端继续发送。

```text
Enabling -- 已接受 + terminal 成功 + 匹配 Enabled --> Active（Keep）
    +-- dispatch 拒绝 / 已接受的 terminal 失败 --> Remove
Active -- begin_disable --> Disabling
Disabling -- 已接受 + terminal 成功 + 匹配 Disabled --> Remove
    +-- dispatch 拒绝 / 已接受的 terminal 失败 --> Active（Keep）
证据尚未齐全、仍等待 dispatch ack 或取消控制结果 --> 保留当前状态（退休返回 None）
```

`retire_transition()` 返回 `Option<SubscriptionTransitionRetirement>`：`None` 表示没有 transition 或证据未齐；`Some` 显式携带已完成的 `command_id` 和独立的订阅 `disposition: SubscriptionDisposition`。`SubscriptionReservations::retire_completed()` 收集 command ID 并应用 `Keep` / `Remove`，不再通过比较退休前后的 progress 推断命令是否结束。`reject_transition()` 仍只返回 `Keep` / `Remove`，dispatch 拒绝的命令身份由调用方已知。集合在收到 `Remove` 后立即移除 route；订阅状态本身不增加 Removed tombstone。成功 transition 必须有对应的 domain，terminal 失败不要求 domain，但仍须等 dispatch ack 和待决取消结果才能退休。严格模式在每个相关 Enabled callback 上验证，即使已有首个 domain 也不能忽略冲突模式；actual mode 仍从 transition 的 domain 构造结果，不另存副本。

`begin_disable()` 只允许从稳定的 `Active` 安装新 transition；准入和 submission claim 仍由调用方先完成，内部断言用于阻止静默覆盖尚未结束的 enable/disable progress，不重复返回业务 Busy 错误。失败 disable 恢复的是本地 Active 路由状态，不证明 native rollback 或通知值连续性已恢复。

### Rediscovery admission 行为修正

目标 connection 存在任何 active subscription 或 pending enable/disable transition 时，actor 对 `BeginDiscoverGatt` 的派发前 admission 以内部 `GattBusy` 拒绝，经原结果通道由 `wait()` 交付公开 `Busy { reason: Gatt }`；同步 begin 入队成功不表示该检查已通过。调用方必须显式 disable 全部订阅并等待 disable 完整完成，才能 rediscovery；timeout/Drop 或取消请求回执不等于 disable 完成。这仍是原有 BLeng admission 决策，不因同步提交而放宽；它本身不增加新的 ABI 形状。

已评审计划区分 [§8.5 discovery 失败时不替换 cache](../../../.plan/PLAN.md#85-gatt-discovery) 与 [§8.8 subscription lifecycle](../../../.plan/PLAN.md#88-开启-notification)。Apple 会在 discovery 前[排队执行原生 unsubscribe](../../../platforms/apple/Sources/BleHubApple/AppleBleBackend.swift#L287-L301)，并[回收订阅路由](../../../platforms/apple/Sources/BleHubApple/GattIoController.swift#L340-L390)，不发 subscription-invalidation event；随后 discovery 仍可能失败。因此保留旧 GATT database 并不保证原生订阅仍然存活，BLeng 不能据此保留虚假的 active route。[Android discovery admission](../../../platforms/android/library/src/main/kotlin/dev/blehub/android/AndroidBleBackend.kt#L310-L312) 已要求先 disable 再 rediscovery。此门禁避免依赖 backend 的隐式 unsubscribe，也不修改薄契约。

## 回归证据与验证范围

### 当前同步提交测试导航

当前同步模型的批准依据为 [Plan §16.1.9](../../../.plan/PLAN.md#1619-已确认的补充决策)，候选红绿/包验证范围由[同步 operation 报告（待补）](../../testing/reports/2026-10-26-bleng-synchronous-operations.md)记录。以下只核对当前 Rust 测试源码中的断言，不把源码存在或历史 pass 当作本次运行通过：

- [`facade_tests.rs`](../../../crates/bleng/src/tests/facade_tests.rs)：`synchronous_submission_is_eager_bounded_and_cancel_wins_before_claim`、`queued_delivery_drop_does_not_retract_eager_submission`、`facade_convenience_keeps_one_deadline_while_typed_wait_gets_its_own_budget`，分别保护容量/backpressure、本地取消与 once-only wait、drop 不撤回和两类 deadline。
- [`runtime/tests/owner.rs`](../../../crates/bleng/src/runtime/tests/owner.rs)：共享 allocator/64 项 mailbox、登记后 claim 前取消、拒绝先赢不虚报取消接受，以及 `dispatch_claim_winner_uses_native_not_cancellable_policy_even_before_native_entry`。后者明确 claim 不等于 native entry，claim 后初始化仍使用 NotCancellable，而不是旧 handoff abandonment fault。
- [`waiter_tests.rs`](../../../crates/bleng/src/tests/waiter_tests.rs)：乱序唯一 ID/当前重复 ID 拒绝及 `dropped_receivers_are_reclaimed_without_reusing_ids` 的有界回收；不要求严格递增 admission。
- [`runtime/tests/lifecycle.rs`](../../../crates/bleng/src/runtime/tests/lifecycle.rs)：`waiter_deadline_with_full_mailbox_retires_delivery_without_fault_in_both_paths` 覆盖预过期与 timer 两条路径，满 mailbox 不妨碍 receiver 关闭，也不将普通 timeout 提升为 fault。
- [`typed_operations.rs`](../../../crates/bleng/tests/typed_operations.rs)：外部 Rust consumer 命名/执行 11 种 handle、同步 begin 无 caller runtime 仍 eager 且不在 caller 调 native、dispatch rejection 经 wait 交付、drop 不请求 native cancel，以及完成结果跨正常 close 保留等边界；不从 Rust fixture 推导 foreign runtime/RF 通过。

[`runtime/tests/gatt.rs`](../../../crates/bleng/src/runtime/tests/gatt.rs) 保留以下行为回归：

- rediscovery 拒绝或失败后，已提交数据库与旧 session 仍可读取。
- 数据库到达前，冲突的重复 terminal 不覆盖首个成功结果。
- disable 完整退休后才允许 rediscovery；收到新 draft 仍不能 enable，只有完整提交后新 session 才能启用订阅，旧 session 与旧 subscription 不会复活。

当前显式本地取消与 delivery-drop 回归区分如下：

- `prelaunch_rediscovery_cancellation_preserves_committed_database_for_reads`：显式 cancel 在 dispatch claim 前获胜，不创建 reservation，旧数据库/session 仍可读取。
- `connection_leaf_prelaunch_cancellations_preserve_siblings_database_and_active_subscription`：RSSI、读写、断连和订阅 transition 的 preclaim 本地取消不影响其他 pending 操作、已提交 database 和 active 订阅。
- `dropping_wait_after_dispatch_claim_retires_delivery_without_faulting`：claim 后丢弃等待只关闭 delivery，不 fault；native reservation/cleanup 继续由原 owner 管理。历史 `cancelling_begin_after_launch_handoff_before_reply_faults_and_disposes_once` 曾保护旧 begin-reply 窗口的 fail-closed 规则；同步提交取消了该窗口，不能继续以该旧测试说明当前 Drop 行为。

[`runtime/tests/owner.rs`](../../../crates/bleng/src/runtime/tests/owner.rs) 的 `cleanup_before_launch_finishes_registered_waiter_without_reserving_or_false_faulting` 覆盖 waiter 登记后、claim 前的三种准备失败：phase 仍 Open 但 cleanup 已开始、显式 close、fault。断言经原结果通道交付对应错误、不登记 reservation、不派发、准备失败不误升为 fault，以及 dispose 恰好一次；该测试名不表示生产路径仍有 handoff guard。

扫描回归新增 `foreign_scan_error_cannot_retire_suspended_stop_awaiting_terminal` 和 `native_stop_retains_live_start_until_domain_and_terminal_settle`，分别覆盖跨 session 错误隔离以及 start domain/terminal 两种顺序的完整记账。订阅回归覆盖 active subscription、disable 失败、disable 超时/取消后的晚到完成，以及 `notification_rediscovery_waits_for_late_disable_without_blocking_other_devices` 的跨连接隔离。

订阅 route 不再保存生产代码未读取的 `actual_mode` 副本；enable 的实际模式仍从 transition 证据构造结果，再释放该证据。

### 历史验证记录（不重标为当前候选）

此前 actor/lifecycle 简化记录曾报告 runtime actor 测试 115 项通过，并记录两个回归通过：旧 `waiter_deadline_retirement_mailbox_overflow_reports_transport_fault_in_both_paths` 覆盖旧退休消息发送失败的 fault；`close_waits_for_actor_teardown_after_mailbox_closes` 阻塞 Kameo unregister，证明 mailbox 已关闭、cleanup 已完成时 close 仍等待实际 actor teardown。前一项机制与期望已由当前 receiver-close/no-fault 回归替代；后一项 teardown 边界仍保留。历史 pass 不证明新同步实现通过。

此前新增 API 回归 [`dropping_facade_waiter_aborts_private_task_and_releases_its_capture`](../../../crates/bleng/src/tests/facade_tests.rs) 曾报告通过：先确认私有 task 已开始，再丢弃外层 future，并有界等待其捕获释放；这不是立即析构或 native cancellation 保证。此处保留该测试的原始说明，不将它当作同步 begin 的 task 证据；begin 本身不 spawn task。

2026-10-07 当时的 reservations 工作树记录了以下确定性验证：

- `pixi run rtk cargo test --locked --offline -p bleng -- --test-threads=1`：156 项通过。
- `pixi run rtk cargo test --locked --offline --workspace -- --test-threads=1`：244 项通过，18 个 suites。
- `pixi run just fmt`、`pixi run just lint`：通过，包括 workspace strict Clippy。
- `pixi run just binding bleng-snapshot-check`：3 个已评审 Swift/Kotlin API 快照通过。
- `pixi run just docs-check-mermaid`：112 个 Markdown 文件中的 21 个图通过。
- UTF-8、代码围栏、尾随空白、冲突标记与 `git diff --check`：通过。

以上仅是 2026-10-07 当时工作树的本机确定性结果，不是当前同步提交候选的重跑，也不是 RF、跨平台 runtime 或 ABI freeze 证据。当时 reservations 重构未新增依赖；其结论仍是：命令证据的乱序累积不能用线性状态机或 lexical RAII 替代。当前 `SubmissionState` 的本地提交 CAS 不替代 actor-owned BLE 证据状态。

此前记录的 locked workspace 验证为 222 个测试通过，其中 BLeng 的 134 个测试全部通过；workspace strict Clippy、格式、BLeng Swift/Kotlin 绑定快照及文档检查也通过。这些历史 gate 不重标为当前 candidate 的重跑；本次文档同步不声称新增 RF 验证或 ABI freeze。确定性 Rust 结果不等于平台 RF 证据。
