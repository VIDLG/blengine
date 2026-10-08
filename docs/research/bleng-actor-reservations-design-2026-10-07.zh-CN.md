# BLeng actor 与 reservation 设计

## 状态与范围

本文记录 BLeng runtime actor/reservation 所有权边界与已实现的 predispatch 顺序简化，以及 reservation 所有权统一、按生命周期拆分目录和三项生命周期评审修正。本文不是新的架构提案，也不改变底层 `BleHub` 的薄契约或公共 ABI 形状。完整 waiter、事件流和初始化历史说明见英文 [BLeng waiter design and operation completion](bleng-waiter-design-2026-10-06.md)；cleanup 的独立完成语义见 [cleanup 设计](bleng-cleanup-design-2026-10-07.zh-CN.md)。

mailbox 协议只保留 `runtime/messages.rs`，不再按命令/事件分设协议模块；actor 保留私有状态与协调，owner 保留调用方等待与 deadline。

2026-10-07 当前 reservations 重构以本地工作树源码为依据：共享 `CommandProgress` 合并 dispatch 与首个 terminal 证据，连接内使用具体 `GattReservation`，订阅状态由私有 enum 表达，各操作 admission 归单连接记录。此次重构不新增依赖、通用操作状态机或路由索引；本文的源码核对不代表当前候选已通过完整验证，既有验证记录保留其原有归属。

## Owner、actor 与 lifecycle

```text
Public BLeng API
  -> RuntimeOwner
       owns executor / ActorRef / event_rx / caller deadline / shutdown
       |
       +-- mailbox -> BlengActor
       |                owns reservations / WaiterRegistry / event_tx
       |                serializes BLE evidence and waiter delivery
       |
       +-- shares RuntimeLifecycle <--- actor / ingress / assembly / dispatch
                    owns phase gate / phase notifications / HubCleanup
```

- [`RuntimeOwner`](../../crates/bleng/src/runtime/owner.rs) 持有执行器、`ActorRef`、向外提供事件的 `event_rx`、调用方 deadline/等待策略和 shutdown 协调；它不解释 BLE reservation 证据。
- [`BlengActor`](../../crates/bleng/src/runtime/actor.rs) 串行处理 BLE 编排、操作证据、waiter 登记与结果交付，持有 reservation 和 `event_tx`。这些状态不搬到共享 lifecycle 中。
- [`RuntimeLifecycle`](../../crates/bleng/src/runtime/lifecycle.rs) 是共享生命周期控制对象，负责 phase gate、通知和唯一的 `HubCleanup`。fault 可直接启动清理，不依赖 actor 继续接收消息；它不是另一份操作状态库或 waiter registry。

`WaiterRegistry` 是调用方的命令账本。回调关联沿现有 scan/connection/GATT/subscription reservation 层级查找，没有新增 `CommandId` 索引，也没有新增 actor 或执行器。

### Owner 操作与绝对 deadline

[`owner.rs`](../../crates/bleng/src/runtime/owner.rs) 的私有 `run_operation()` 直接编排通用 begin、ticket 等待与操作特定的成功结果提取；`Duration` 参数统一称为 `timeout`，转换后的 `Instant` 才是 deadline。

```text
run_operation(timeout, make_message)
  -> 一次 Hub 预检查 -> 计算一次绝对 deadline -> scopeguard::guard(BeginHandoff, ...)
  -> 同步调用 make_message(handoff.clone()) 构造消息 M
  -> message.correlation() 保存轻量 ID，再将消息移交 Kameo
  -> owner 统一执行 timeout_at(deadline, actor.ask(message)) -> 收到 BeginReply
  -> drop(ScopeGuard::into_inner(handoff)) 解除保护并释放捕获与 token
  -> WaiterTicket::wait_until(同一 deadline) -> ? 传播错误
  -> M::decode_success(correlation, reply.resource, success)
       匹配 -> 提取 typed result；不匹配 -> lifecycle fault + TransportFault
```

`make_message` 是同步消息工厂，签名为 `FnOnce(BeginHandoff) -> M`：它只构造 Begin 消息，不发送 ask、不返回 future，也不等待 `BeginReply` 或原生 BLE 完成。owner 调用工厂后，在 Kameo 消费消息之前通过 `message.correlation()` 保存轻量的 `M::Correlation` ID；随后统一执行 actor ask，等待的 reply 仍只是交接元数据。消息构造不另设计时器，ask 与 ticket 等待使用同一个已计算的绝对 deadline。

[`messages.rs`](../../crates/bleng/src/runtime/messages.rs) 中的私有 `OperationResult` 由全部 11 个 `Begin*` 请求实现，定义 `Resource`、`Output`、`Correlation` 三个关联类型及 `correlation(&self)`、`decode_success(correlation, resource, success) -> Option<Output>`。`Resource` 对应 `BeginReply` 新分配的 typed ID，无新资源时为 `()`；`Correlation` 只保留原请求中验证成功所需的 ID，无需时为 `()`；`Output` 是操作的类型化返回值。

owner 在 ticket 成功后调用 `M::decode_success(correlation, reply.resource, success)`，保留原有全部 variant 与 ID 检查；`Some(output)` 移出结果，`None` 表示内部不变量不匹配，由 owner 触发 lifecycle fault 并返回 `TransportFault`，不是普通 BLE 操作失败。解码本身不依赖 lifecycle。Begin/dispatch/BLE 等错误沿原有错误路径传播，不进入成功解码。

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

该 trait 只把成功解码归属到请求类型，不克隆请求消息或 payload，不引入 boxing、执行层或新状态机。read bytes、数据库等结果按所有权移出，消息及写入 payload 交给 Kameo 消费；`handoff.clone()` 只克隆共享交接 token。`WaiterSuccess` 仍是数据 enum，不承载执行逻辑。`From` 不适用：解码可能失败，并且需要来自请求与 `BeginReply` 的预期 ID，不能只根据成功值进行无条件转换。

等待阶段不重新计算相对 timeout。`WaiterTicket::finish_timeout()` 统一处理进入等待时已过期与 timer 唤醒两条路径：发送退休消息、disarm，再调用 `require_open()`，只有 gate 仍 Open 才返回 `Timeout`。若退休消息因 mailbox 满或关闭而发送失败，并使 Open lifecycle fault，两条路径均返回 `TransportFault`，不掩盖为普通超时。

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
    io.rs
    subscription.rs
```

| 源码 | 职责 |
|---|---|
| [`messages.rs`](../../crates/bleng/src/runtime/messages.rs) | 统一 mailbox 协议：11 个 `Begin*` 请求及其 `OperationResult` 成功解码、`BeginReply` 与命令准入 handler；`IngressEvent`、`DispatchResolved` 与两个转交 handler；`RetireWaiter` 与 handler。不持有状态，不负责调用方 deadline 或等待。 |
| [`owner.rs`](../../crates/bleng/src/runtime/owner.rs) | 所有调用方操作入口，包括订阅 API；管理请求交接、deadline 与结果等待。 |
| [`actor.rs`](../../crates/bleng/src/runtime/actor.rs) | 定义 `BlengActor`、`Drop`、register/reserve 方法和全部状态协调方法；负责 ingress/dispatch-result 处理、跨 reservation 的动作和 waiter 结算，不持有 prelaunch rollback guard。 |
| [`backend_port.rs`](../../crates/bleng/src/runtime/backend_port.rs) | backend assembly 与非阻塞 ingress；直接从 messages 导入 `IngressEvent`，不持有 owner 或解释 reservation 证据。 |
| [`reservations/mod.rs`](../../crates/bleng/src/runtime/reservations/mod.rs) | 记录状态层边界，只重导出 runtime 实际使用的状态入口和协调结果。 |
| [`dispatch.rs`](../../crates/bleng/src/runtime/reservations/dispatch.rs) | `CommandProgress` 共享 dispatch/首个 terminal 进度、pre-ack 缓冲边界，以及一次性 reconciliation 结果和错误。 |
| [`initialization.rs`](../../crates/bleng/src/runtime/reservations/initialization.rs) | 初始化证据、readiness 和初始化状态转换。 |
| [`scan.rs`](../../crates/bleng/src/runtime/reservations/scan.rs) | 唯一扫描槽位、start/stop 证据和 session 退休。 |
| [`connections.rs`](../../crates/bleng/src/runtime/reservations/connections.rs) | 连接根集合、peripheral 槽位 admission、connection/command 查找与 peripheral key 解析、操作登记、全局回调路由优先级和批量退休。 |
| [`connection.rs`](../../crates/bleng/src/runtime/reservations/connection.rs) | 单连接根记录、各操作 admission、disconnect、子命令进度遍历、局部事件关联、dispatch reconciliation 和退休。 |
| [`gatt.rs`](../../crates/bleng/src/runtime/reservations/gatt.rs) | 已提交数据库与 pending discovery 的不同生命周期。 |
| [`io.rs`](../../crates/bleng/src/runtime/reservations/io.rs) | RSSI、characteristic read/write 的局部证据与结果。 |
| [`subscription.rs`](../../crates/bleng/src/runtime/reservations/subscription.rs) | 订阅 route、enable/disable transition 与实际模式验证。 |

此前先统一 reservation 所有权与接口；当单文件增长到约 2300 行、内部边界稳定后，再按生命周期拆成 `reservations/`。这是源码组织拆分，不是新增运行时边界；不新增 actor、executor、锁或命令索引。集合与单条记录分别由 `connections.rs`、`connection.rs` 管理，不能因拆文件把全局操作类别路由顺序改成逐连接路由。

可见性由私有实现模块与受限 facade 共同控制：`mod.rs` 中所有实现模块保持私有，只有真正的状态入口与协调结果通过 `pub(super) use` 向 runtime 重导出。实现文件的这些入口使用 `pub`，但没有公开模块路径或更宽的重导出，因此不是 crate 公共 API，也不改变 ABI。目录内部共享的类型、字段与 helper 使用 `pub(super)`，文件局部实现保持私有，不再逐项重复 `pub(in crate::runtime)`。

`InitializationState` 是不透明 struct：其 `InitializationPhase` enum、phase 字段和 `InitReservation` 全部私有，外部不能直接构造 `Ready` 或覆盖 pending 证据。扫描 enum 的 payload 类型仍与 enum 的声明可见性匹配，但不单独重导出，字段与局部方法也不开放。`pending()`、`reserved()`、`reserved_mut()`、`find()`、`find_mut()` 等原始记录查询均为内部实现。actor 断连结算通过 `ConnectionReservations::accepted_command_ids(connection_id)` 取得所需 command ID，不再取得整个连接记录。测试通过 dispatch resolution、相关事件、结果与 admission 判断验证行为，不为了检查内部字段放宽可见性。

reservation 是命令与事件处理共用的 actor-confined 状态模型，不只是被动字段容器：它拥有局部 admission、回调关联、证据记录、dispatch reconciliation、结果判断与退休规则。reservation 是 actor 内的普通值，不单独调度。[`messages.rs`](../../crates/bleng/src/runtime/messages.rs) 统一命令准入与派发结果/ingress 消息入口；状态协调集中在 [`actor.rs`](../../crates/bleng/src/runtime/actor.rs)，局部状态判断靠近其 reservation 所有者。这是既有 runtime 内按角色划分源码，不是新增运行时或架构边界。

建议按职责阅读：先看 `reservations/mod.rs` 的边界和 `dispatch.rs` 的共享进度，再看 `connections.rs` 的根查找与全局路由、`connection.rs` 的局部 admission/结算/退休，随后阅读 `gatt.rs`、`io.rs`、`subscription.rs` 的领域证据；初始化与扫描分别看 `initialization.rs`、`scan.rs`。最后对照 `messages.rs` 的准入交接和 `actor.rs` 的事件协调，避免把进度遍历误认为路由优先级。测试统一物理存放在 [`reservations/tests/`](../../crates/bleng/src/runtime/reservations/tests/)：`connections.rs` 通过容器行为接口覆盖跨 reservation 不变量；`subscription.rs` 由生产订阅模块通过 `#[path]` 挂载为自己的测试子模块，保持原有隐私边界。生产文件不内联测试函数，也不为测试放宽可见性；完整流程测试仍在 `runtime/tests/`。

## 命令 ID 与资源 ID 的独立分配

[`actor.rs`](../../crates/bleng/src/runtime/actor.rs) 中的 `command_id_gen` 与 `resource_id_gen` 是两个独立的 `IdGenerator`，都从 1 开始，由 actor 串行访问，不需要 atomic、锁或全局 generator。命令序列只分配 `CommandId`；资源序列分配新建的 scan session、connection、GATT session 和 subscription ID。资源类型之间暂时共用资源序列，不为每个类型增加单独计数器。

```text
操作检查全部通过
  -> 创建资源的命令：从 resource_id_gen 分配 typed resource ID
  -> register_waiter(expectation)：从 command_id_gen 分配 CommandId 并登记 waiter
  -> launch_dispatch：取消检查 -> reserve -> native dispatch
```

所有命令统一调用 `register_waiter()`，不再保留 `register_waiter_with_following_id()` 或构造 expectation 的闭包。资源 ID 先确定，再直接构造 expectation；它与 reservation 使用同一个 typed ID，不要求数值紧随命令 ID。

ID 是关联标识，不是成功操作的连续编号：允许跳号，不回滚、不复用、不回绕。waiter 容量拒绝、交接前取消、dispatch 拒绝或后续操作失败，都不会回收已分配的 ID。资源分配失败时尚未登记 waiter；waiter 登记失败时可能消耗资源和命令 ID，但不会创建 reservation 或启动原生命令。两个序列独立耗尽，`u64::MAX` 可分配一次，此后持续返回 `IdExhausted`，不会变成 0。

`CommandId(1)` 与 `ConnectionId(1)` 可以同时存在，关联按类型和对应字段进行，不依赖跨类型数值互异。`WaiterRegistry` 仍要求命令 ID 严格递增，但不要求连续；generator 不代替 waiter admission 或 backend callback correlation。

[`runtime/tests/id_allocation.rs`](../../crates/bleng/src/runtime/tests/id_allocation.rs) 覆盖相同数值的精确事件关联及失败/取消后的跳号与不复用；[`runtime/tests/actor.rs`](../../crates/bleng/src/runtime/tests/actor.rs) 在 actor 的私有边界内验证最大 ID 只能发出一次且耗尽后不回绕。删除了仅证明两个独立局部 generator 互不影响的测试；实际 actor 的 command/resource 分配分离仍有覆盖，不再单独挂 `id_sequence` 测试模块。

## 连接 reservation 容器

`BlengActor.connections` 持有 [`ConnectionReservations`](../../crates/bleng/src/runtime/reservations/connections.rs)，不是向兄弟模块公开的 `HashMap`。容器内部唯一的 `by_peripheral` map 保存连接根记录；不增加按 `ConnectionId` 或 `CommandId` 的第二份索引，也不增加锁、共享所有权或调度设施。

职责分三层：

| 层次 | 职责 |
|---|---|
| `BlengActor` | lifecycle gate、ID/等待方登记、命令交接和 reservation 安装协调、事件发布、waiter 结算。 |
| `ConnectionReservations` | 按 peripheral 登记、按 connection/command 查找、terminal/result 路由、批量退休与根记录移除。 |
| `ConnectionReservation` | 单连接状态、GATT/I/O/subscription 证据、disconnect/discovery/RSSI/I/O/订阅各操作的 admission 条件、结果与退休判断。 |

`ConnectionReservations` 不包 `Option`：空容器已经表示没有 connection reservation，没有额外的「未创建容器」状态。唯一 scan slot 由 `ScanState` 表达 `Idle` 或 `Reserved(ScanReservation)`，根状态的验证、安装和释放也由它拥有。连接容器中的每一条 reservation 有各自的生命周期，但容器本身与 actor 同生共死。

`reserve_connection()` 只允许空 peripheral 槽位，不能覆盖仍持有 native 生命周期证据的旧记录。正常重复 connect 仍在 admission 阶段返回 `PeripheralBusy`；内部重复安装则是 invariant failure，而不是静默替换。连接结束并完整记账后，容器才释放该 peripheral，后续连接使用新 connection/command ID，旧 terminal 不能命中新记录。

容器不实现 `Deref<HashMap>` 或 `Index`，也不向外返回 map。pre-ack 路由和操作特有的拒绝回放筛选已交回容器及所属 reservation；事件协调层不再遍历连接记录，原 `iter_mut()` 接口删除。回放执行、fault/cleanup 和 waiter 交付仍由 actor 协调，没有引入通用集合策略框架。

`DispatchResolved` 先由容器按 command 找到所属 connection，再处理其中的具体 slot，不再为每个操作重复扫描整张连接 map。domain event 通过 connection ID 查到目标后，仍校验 command/session/characteristic/subscription 的对应关系。容器先解析连接根与 peripheral key，再调用单连接记录的 `validate_disconnect()`、`validate_gatt_discovery()`、`validate_rssi()`、`validate_gatt_io()`、`validate_notification_enable()` 或 `validate_notification_disable()`，不在容器中重复局部条件；disable 还返回登记所需的 characteristic ID。

调用顺序仍是记录证据、结算 waiter、再调用 `connections.retire_completed()`；容器不持有或自行结算 waiter。相关回归覆盖重复安装不能覆盖旧根、跨连接 terminal 隔离，以及同一 peripheral 重连后拒绝旧 terminal。

## Actor 封装与纯转发

Rust 的 `pub(super)` 将可见范围限制在 `runtime` 及其子模块，不是对 crate 用户公开；此前主要用于让兄弟模块中的 `impl BlengActor` 访问字段。但字段可见并不等于所有调用点都需要容器本体。

`event_tx` 和 `waiters` 现在是 actor 私有字段。`publish_event()` 只负责入口 open gate、向外入队和队列失败时 fault；成功仅表示本地入队。私有 `process_event()` 在发送后再次检查 lifecycle，才继续处理证据。前者是发送前置条件，后者是事件协调的继续处理条件，不在同一函数里重复 `require_open()`。发送后的检查应对并发 shutdown，但不能撤回已入队事件，也不保证发布与关闭原子化。命令侧取消/超时统一通过 actor 的 `retire_waiter(command_id, timed_out)`：`timed_out` 为 true 时调用 `waiters.timeout`，否则调用 `waiters.cancel`，取代两个独立 delegate，逻辑不变。actor 内部的结果交付直接使用私有 registry；`launch_dispatch()` 交接失败也直接调用 `waiters.cancel`，不绕行命令入口。

`require_open()` 的 phase-to-error 映射属于 `RuntimeLifecycle`；`require_initialized()` 的 readiness-to-error 映射属于 `InitializationState`，只有 `Ready` 通过，`NotStarted`、`Pending` 和 `Rejected` 均返回 `NotInitialized`。actor 通过 `delegate!` 使用这两个门禁，不再重复判断状态。`reserve_connection` 和连接内的 `reserve_*` 入口也通过 `delegate!` 转发给 `ConnectionReservations`，actor 不再拼装 disconnect/RSSI/I/O/subscription 字段或持有 `connection_mut()` helper。`InitializationState::reserve_initialize()` 建立 pending 状态，私有 `InitReservation::new()` 拥有初始证据；actor 同样只转发。`ScanState` 拥有 scan 根的 admission、start/stop 登记、拒绝移除和退休，stop slot 的初始化仍属于 `ScanReservation`。所有纯转发集中在一个 `delegate!` 块中并按所有者分组；scan 的四个命令入口统一 `to self.scan`，不再由 actor 解包 `Option` 或表达 scan 可用条件。

初始化的结算后推进统一调用 `advance_after_settlement() -> Result<(), WaiterError>`：未确认派发或 terminal 未到时保留 Pending；terminal 失败返回原操作错误，由 actor fault；成功且 snapshot 已到才转 Ready。它替代外部先查 `has_failed()` 再调用完成方法的组合，不克隆成功 snapshot，不改变 waiter 先结算的顺序。`record_event()` 直接匹配带关联命令 ID 的 snapshot，不再先执行包含 terminal 的通用匹配再重复解构；`pending()` / `pending_mut()` 保持私有，复用多处状态访问。`reserve_initialize()` 的断言复用 `require_not_started()`，不另保留 `has_started()` 查询。测试 fixture 使用 reserve、dispatch acceptance、snapshot、terminal 和完成推进建立就绪状态，不新增只供测试使用的生产构造器或 getter。

actor 的 `lifecycle`、`initialization`、`scan`、`connections` 现在全部私有。messages 中的命令与订阅 handler 不取得 reservation 实体：初始化/扫描/连接/GATT/RSSI/读写/订阅的 admission 条件由对应所有者验证，actor 用 `delegate!` 转发，返回值只包含成功/错误或后续登记需要的 ID，保留原错误优先级。scan slot 的空闲/占用判断属于 `ScanState`；session 内 stop 验证属于 `ScanReservation`。

`launch_dispatch()` 位于 actor.rs，只有这里内部克隆 lifecycle 给分离的 blocking dispatch task；messages 不获取或克隆 actor 的 lifecycle Arc。`runtime/messages.rs` 与 actor.rs 平级，其中两个异步输入 `IngressEvent`、`DispatchResolved` 的 `Message` handler 各只调用 `self.handle_ingress_event(event)`、`self.handle_dispatch_result(message.id, message.result)`，入口名称不变。这两个同步入口在 actor.rs 中为 `pub(super)`；实际处理、提前回调缓冲、foreign disconnect 排除、发布、记录、结算、回收、`DispatchAction` 执行与回放都封装在 actor.rs 中，其余协调 helper 保持私有。messages 不访问 actor 字段，也不通过新 getter 取得状态；回调关联、局部证据修改和订阅模式验证仍属于 reservation。`resource_id_gen` 和 `IdGenerator` 同样私有，Begin handler 通过 actor 的 `allocate_resource_id()` 分配。

同一项 reservation 登记在 actor 和拥有者上采用同名 `reserve_initialize()`、`reserve_connection()`、`reserve_scan_stop()`，不再通过 `#[call(begin)]`、`#[call(register)]` 或 `#[call(begin_stop)]` 建立额外的名称对应。generator 的上下文不同，`allocate_resource_id()` → `allocate()` 的改名转发仍保留；waiter 取消/超时则共用显式的 `retire_waiter(command_id, timed_out)` 入口。

`delegate!` 仅表达已明确所有者的纯转发。涉及 ID 分配、发布失败策略、跨 reservation 结算或先后顺序的方法仍显式实现；宏不是职责划分的替代品。

### 可见性与命令结果路由

`BlengActor` 类型与 `WaiterReceiver` 别名仅在 `runtime` 内可见；`RuntimeOwner` 的全部字段（包括 `actor`、`lifecycle`、`shutdown_result_tx`）均为私有，`BackendPort.lifecycle` 同样私有；`runtime/mod.rs` 不再向 crate 其他模块重导出 actor 类型。actor 全部字段私有；供 messages 中 handler 实际调用的验证、分配、登记、交接和 waiter 退休方法保留 `pub(super)`。两个异步输入 handler 只调用 `pub(super)` 入口 `handle_ingress_event()` 与 `handle_dispatch_result()`；它们不是 crate 公开 API。其余状态协调方法，包括发布、terminal 路由、waiter 结算与失败传播，均在 actor.rs 内保持私有。保留显式的策略与协调方法，不为了统一形式把它们改成纯转发。

runtime 的生产代码和测试使用显式导入，不使用父模块或 prelude 的 glob 导入；`mod.rs` 不为子模块集中导入外部依赖。测试物理文件仍位于 `runtime/tests/`，逻辑模块按被测所有者划分：owner 通过 `#[path = "tests/mod.rs"]` 挂载 owner/跨模块流程测试；actor 通过 `tests/actor.rs` 挂载私有不变量测试，通过 `tests/actor_support.rs` 定义访问私有状态的 fixture message handler。后者只有 `#[cfg(test)]` 下的受限消息重导出，不暴露 actor 字段。自定义 fixture 消息实现同一个 `OperationResult` 接口，以同步消息工厂和 typed `BeginReply` 回复运行生产 `run_operation(timeout, make_message)`，不再传入结果映射闭包或使用测试专用 begin helper；不新增生产 getter、通用 actor 访问接口或测试专用生产方法。`WaiterTicket`、其字段和等待方法，以及 `run_operation()`、`ticket()` 都是 owner 私有实现，不再由 runtime 重导出。可见性由生产依赖决定，测试依赖通过调整模块归属解决。

测试按不变量而非行数保留：生命周期、错误优先级、取消、关联和耗尽边界有价值；独立局部变量互不影响、原样结果转发等无独立行为的重复测试删除。waiter 的单独 dispatch-rejection payload 转发测试已移除，恰好一次交付与 runtime 的实际拒绝处理仍由原有回归覆盖。

`record_terminal()` 按 initialization、scan、connections 的顺序逐段查找，前两段命中即 `return`，不将有副作用的调用组合在 `||` 中。reservation 返回的 `bool` 只表示「这个 command ID 属于我」，不表示操作成功；匹配到重复 terminal 也要停止查找，而已有首个 terminal 不被覆盖。actor 内 command ID 唯一，因此同一 terminal 不广播给多个 reservation。

`waiter_result()` 同样采用逐段 early-return，但停止条件是拿到 `Some(result)`，包括 `Some(Err(...))`；`None` 可能表示 ID 不属于当前 reservation，也可能表示证据未齐，不能当成操作错误或命令不存在。继续查找不会将未就绪的命令交给其他所有者，因为 command ID 不复用且唯一；无需额外结果路由 enum 或第二份命令索引。

## 共享门禁与派生状态

`RuntimeLifecycle::is_open()` 读取既有原子 phase，只用于查询当前是否 Open；它不保留 admission、不阻止并发 close/fault，也不新增状态。需要区分 `Faulted` 与 `Closed` 并映射错误的路径仍使用 phase；`require_open()` 保留相应错误映射。

[`CommandProgress`](../../crates/bleng/src/runtime/reservations/dispatch.rs) 替代原 `DispatchReservation` 加各记录独立 terminal 字段的组合。每个命令的 `progress` 保存 `command_id`、私有 `buffered_events`、私有 `dispatch_accepted` 和私有 `terminal_result: Option<Result<(), BleEventError>>`；scan start/stop、connect/disconnect、discovery、RSSI、读写与订阅 transition 都复用它，不再另存 `start_terminal` 或 `terminal`。

`record_terminal()` 仅记录首个结果，冲突的重复 terminal 不能覆盖它；读取通过 `has_terminal()`、`terminal_succeeded()`、`terminal_failed()`。`is_awaiting_dispatch_result()` 只表示仍在等待原生同步派发的结果被 actor 处理，不表示 waiter 或原生操作仍 pending；dispatch 被接受不等于 BLE 操作完成。这里的 result 是 `DispatchResolved` 携带的同步派发结果，不是 Begin reply 或 BLE 原生应答。

`CommandProgress::terminal_result()` 不接收 success closure，也不依赖 `WaiterSuccess`：未确认 dispatch 或缺少 terminal 时返回 `None`；terminal 错误交付为操作错误；terminal 成功仅返回 `Ok(())`，不代表 domain 完整。具体 reservation 使用 `terminal_result()?.map(|()| /* Option<WaiterSuccess> */).transpose()` 组合领域证据；`Result::map` 只在成功时执行，因此仍保留惰性构造及错误优先级，缺少 domain 仍返回 `None`。`is_complete(domain_evidence_ready)` 判断命令证据是否完整：dispatch 已接受、terminal 已到达，且 terminal 失败或成功所需的关联领域证据已经齐全。这里的完整不等于 native 生命周期结束，也不依赖 waiter 是否仍存在；native 资源何时可安全移除仍由具体 reservation 判断。连接错误、被动断连等操作特有的优先级和退休条件仍由具体 reservation 判断，不隐藏在通用状态机中。

snapshot、连接状态/error、discovery draft、RSSI/read payload、write 完成事实与通知状态仍保留在对应 reservation；共享进度不拥有这些 typed domain 证据、waiter 或路由策略。

`buffered_events` 不向兄弟模块暴露，事件只能通过有界 `buffer()` 入队。派发结果只有两个入口，均由 `CommandProgress` 接管缓冲：

- `accepted_resolution()` 标记派发已接受，并按原顺序移交全部缓冲事件；明确返回 `DispatchAction::None`，正常接受本身不要求 cleanup 或 fault。
- `rejected_resolution(on_clean_rejection, classify)` 取走全部缓冲，不标记接受；具体 reservation 对每条事件返回 `BufferedEventDisposition::Replay`、`Discard` 或 `Conflict`。共享层保序收集回放事件、丢弃合法可忽略的证据；任何冲突覆盖正常拒绝动作，要求 fault，但不提前结束后续分类。

initialization、scan start 和 connect 将所有提前回调分类为 `Conflict`，保留严格拒绝规则；无事件时 initialization 要求 `StartCleanup`，其他正常拒绝为 `None`。普通 I/O 只回放被动 connection loss；订阅保留较宽的 connection-loss 条件。scan stop 同样走统一入口，只在本地定义三路分类：被动停止或停止错误为 `Replay`，主动停止证据为 `Conflict`，被拒绝命令的 terminal 为 `Discard`。

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

`ConnectionReservation::child_progresses()` 枚举 disconnect、discovery、RSSI、read、write 和 subscription transition 的 `CommandProgress`；`has_pending_child_handoff()` 只查询其中尚未确认 dispatch 的命令。`command_progresses()` 再加入 connect 根，用于命令归属、accepted command 收集和根退休；`command_progresses_mut()` 供 terminal 按 command ID 查找并调用 `progress.record_terminal()`，不再维护 dispatch/terminal 成对列表。容器逐连接调用 `record_terminal()`，首次命中即停止，不先查找一遍命令再重复遍历。

共享 predicate 只复用相同条件，不抹平各操作门禁或错误优先级：

| 操作 | 单连接记录保留的检查 |
|---|---|
| disconnect | 不要求已经收到 `Connected`；connect 已接受即可拥有 native 生命周期。先排除 disconnected/connect failure/未确认 connect，再检查已有 disconnect 与未确认的子命令。 |
| discovery | 先要求连接 active；disconnect、未确认 RSSI、pending discovery、read/write 或任何订阅 route 均返回 `GattBusy`。已接受的 RSSI 不因共享 predicate 被一概阻塞。 |
| RSSI | 先要求连接 active；已有 RSSI、disconnect 或未确认的子命令返回 `OperationBusy`。 |
| read/write | 保留连接 → disconnect/未确认 RSSI → committed session/characteristic → I/O/订阅冲突 → discovery 的顺序；目标错误仍先于后续 busy。read 还排斥同 characteristic 的订阅，write 不增加此限制。 |
| enable | 使用 `GattAccess::EnableNotifications` 复用 GATT 检查，在 pending discovery 检查后拒绝同 characteristic 已有的订阅 route。 |
| disable | 容器先找到连接；记录内先检查 subscription/session 与 active，再检查 transition busy，然后检查连接与兄弟操作。`Disabling` 仍是 active route，因此重复 disable 返回 `OperationBusy`，不是 `SubscriptionNotActive`。 |

`GattAccess::{Read, Write, EnableNotifications}` 替代 `read: bool`，由 `validate_gatt_access()` 明确表达准入目的，而不是让 enable 假装成 write。read 对同 characteristic 的订阅冲突仍在 pending discovery 前检查，enable 的重复 route 检查仍在其后；枚举不统一这两种错误优先级，也不表达 backend capability。

这些迭代器不是 callback 路由索引，也不替代全局 pre-ack 类别顺序。

## 证据、结算与回收顺序

```text
publish to bounded event FIFO
  -> record domain / terminal evidence
  -> settle affected waiters from reservation evidence
  -> retire completed reservations / commit successful discovery
```

必须先记录证据，再通过 [`WaiterRegistry::finish()`](../../crates/bleng/src/waiter.rs) 恰好一次交付结果，最后释放已完整记账的状态或提交数据库。`settle_waiter()` 先调用 `WaiterRegistry::contains(command_id)`；调用方 waiter 已退休时不再物化 reservation 结果（例如克隆数据库或 read payload），但不跳过晚到证据记录和后续 retirement/commit。domain 与 terminal 的先后不作假设；dispatch ack 前逃逸的回调先缓冲，再按关联规则处理。

owner 确认 actor 的 begin reply 后，超时或调用方取消只退休 waiter，保留 reservation，不承诺取消已派发的原生 BLE 操作。晚到回调仍可推进初始化就绪、原生/session slot 回收或 GATT commit，但不能重新给已退休的 waiter 交付旧结果。

## 事件层与 reservation 的分工

`runtime/messages.rs` 中的两个异步输入 handler 只转交消息，不执行状态协调。`actor.rs` 在两个受限入口及私有 helper 内负责事件路由与副作用执行；`InitializationState`、`ScanState`、`ConnectionReservations` 统一提供三个有实际职责的入口：

| 入口 | 所有者负责的行为 |
|---|---|
| `gate_event(event)` | 在公开发布和证据记录前检查实时 callback；dispatch 结果未确认时，关联事件进入原有有界缓冲，此时 native dispatch 可能已经执行。`Ok(None)` 表示已缓冲而非丢弃；`Ok(Some(event))` 继续交给下一个所有者或正常处理，既可能是不归当前所有者的事件，也可能是当前所有者无需再缓冲的事件，因此它不是所有权查询或过滤器。overflow 或初始化拒绝后的矛盾证据返回错误。 |
| `record_event(&event)` | 校验 command/connection/session/target，修改本地证据；不发布事件、不结算 waiter、不执行生命周期动作或完成退休。 |
| `resolve_dispatch(command_id, result)` | 接受 dispatch 并取出缓冲，或按操作规则移除被拒绝的 provisional slot；返回回放列表和必要的后续动作。 |

这套约束不只适用于初始化：scan 与 connections 的 `waiter_result()` 返回 `None` 同样可能表示未认领或证据未齐；已选定 I/O/GATT 叶子记录的无 command ID 查询则表示证据未齐。`record_terminal()` 在需要继续路由的层返回是否认领，connections 作为 actor 的最终路由无需返回布尔值。`retire_completed()`、`retire_completed_discovery()`、`retire_transition()` 保持资源退休的命名，但必须在 waiter 结算后执行；订阅返回 `Keep` 可能表示尚未完成、维持 Active 或恢复 Active，不等于命令成功。`Remove` 由 connection 容器实际执行删除，状态对象不自行管理外层容器。

叶子 reservation 的关联方法由缓冲和证据记录复用。记录时仍先按 domain event 类型分支，因此匹配 terminal 或断连的宽泛缓冲规则不会被误用为 read/RSSI/value 等证据。连接状态与 explicit disconnect domain 在同一次连接查找中记录。首次 snapshot/read/RSSI/subscription 证据保持不变；GATT draft 仍保留最后一次关联结果，未改变既有策略。

初始化 API 的阶段约束保持明确：登记 waiter 前检查准入，认领 caller handoff 后才安装 reservation；记录证据后先结算 waiter，再调用 `advance_after_settlement()` 推进就绪状态或报告已接受操作的失败。该方法返回 `Ok(())` 不代表已经 Ready，证据不足时仍保留 Pending。`waiter_result()` 返回 `None` 也不代表“不归初始化所有”，还可能是证据未齐；与之不同，`record_terminal()` 的布尔值表示命令所有权，`resolve_dispatch()` 返回 `None` 表示没有匹配的 Pending 初始化。状态对象不接管 waiter registry 或 lifecycle 副作用。

pre-ack 探测顺序保持：

```text
initialization
  -> connections: disconnect -> discovery -> RSSI -> read -> write -> subscription -> connect
  -> scan start -> scan stop
  -> 未缓冲事件进入 process_event
```

连接类别优先级作用于整个容器，不是每条连接各自探测完所有操作再换下一条；不使用 `command_progresses()` / `child_progresses()` 的一般遍历顺序代替它，也不新增命令索引。带外来 command 的已知连接断连事件仍公开交付，但在缓冲前和证据记录前均阻止其改变当前生命周期。

`DispatchResolution` 只是一次处理结果，不是新增的 canonical state：其中 `events_to_replay` 保存允许回放的事件，`action: DispatchAction` 表示尚待 actor 执行的动作：`None`、`StartCleanup` 或 `FaultRuntime(ReconciliationFault)`。`StartCleanup` 只要求启动清理，不承诺清理已经完成。actor 按以下顺序执行：

```text
reservation 局部 dispatch 变化
  -> actor 执行 cleanup / fault（如需要）
  -> rejected command 交付 DispatchRejected
  -> 按顺序回放允许的事件
       -> 发布 -> 记录 -> 结算 -> 退休
```

普通 GATT/read/write/RSSI/disconnect 的拒绝只允许回放被动 loss；订阅拒绝保留原来的较宽 connection-loss 规则。其他已逃逸 callback 是矛盾证据，要求 fail closed。初始化派发拒绝后转入 `Rejected { command_id }`，释放 pending 证据，只保留一个命令 ID 识别已逃逸的晚到回调；不会重新允许初始化，也不会继续按派发前事件缓冲。干净拒绝只启动 Hub cleanup，不把 runtime 改成 Faulted；若之后仍到达关联 snapshot 或 terminal，则判定为 `DispatchEvidenceConflict`，在公开发布前 fail closed。无 command ID 或其他命令 ID 的事件仍走正常事件流，不改变初始化状态。重复 dispatch result 不会重新认领已拒绝的操作。`reserve_initialize()` 断言当前为 `NotStarted`，防止内部调用绕过 admission 后覆盖既有状态。`ReconciliationFault` 区分缓冲超限、实际通知模式不符和派发结果与事件证据冲突（`DispatchEvidenceConflict`），actor 使用已有 tracing 记录原因，不记录 callback payload。

每个命令的有界缓冲由 `CommandProgress` 统一限制为 64 条，不为重构添加新队列、锁或 executor。回归见 [`reconciliation.rs`](../../crates/bleng/src/runtime/tests/reconciliation.rs)：覆盖精确容量、外来事件不消耗容量、原序回放，以及 I/O 与订阅拒绝的不同 loss 策略；既有同步 callback、晚到证据和取消测试继续保护完整 actor 流程。

## ScanState 与扫描回收

```text
Idle
  -> admission + caller handoff 成功
  -> Reserved(ScanReservation)
       -> 匹配的 start dispatch 被拒绝 -> Idle
       -> 完整证据记账、先结算 waiter、再退休 -> Idle
```

`Reserved` 只表示持有 scan slot，不等于已经开始射频扫描；它覆盖启动中、扫描中、stop 中以及 native 已停但晚到 start/stop 证据尚未齐全的阶段。没有额外增加 Running/Stopping 等重复状态，也不把独立的 domain、terminal 和 dispatch ack 事实压成线性 enum。

实际 payload 使用 `Box<ScanReservation>`，装箱避免空闲 scan slot 也占据整份会话的空间；不把重构前的布局测量当作当前类型大小。代价是每次成功登记 scan 多一次分配，不是每个回调分配；不新增 `Arc`、锁、执行器或共享状态。

相较于 actor 持有 `Option<ScanReservation>`，新增类型的价值不是形式一致或方便宏转发，而是把 slot 可用性、登记和释放的策略放在同一个所有者上。`ScanState` 统一处理 terminal/result 路由、拒绝和退休，actor 只协调结算顺序。重复安装不能覆盖仍保留的会话；晚到的旧 start rejection 不能释放新会话。内部事件实现通过 `reserved()` / `reserved_mut()` 记录证据，这些访问不作为 actor 的对外 getter。


scan retirement 不再接收原始 event 参数，只根据已关联的 reservation 证据（包括 `stop.stop_error`）决定回收。无关 scan 的 `ScanStopped::Err` 不能回收当前 scan 或丢弃其 pending stop 关联。

scan 已停止也不代表 start 已完整记账：若 start 的 domain/terminal 证据不完整，必须保留 reservation，等证据足以结算 start waiter 后再回收。两种事件顺序、dispatch ack 前缓冲与晚到回调均遵循先结算、后回收。

stop dispatch 被拒绝时，不能把缓冲的全部 `ScanStopped` 都判为矛盾。[契约的 `command_id()`](../../crates/blehub/src/contract/events.rs) 明确区分：`SystemSuspended` 与 `ScanStopResult::Err` 没有 command ID，是独立的原生结束证据；`Requested` 则携带 command ID。`ScanState::resolve_dispatch()` 移除被拒绝的 stop reservation，按原顺序回放被动结束；actor 先交付 `DispatchRejected`，再发布并记录回放事件，随后退休旧会话。被动结束不能把拒绝变成 stop 成功；被拒绝命令的 terminal 不参与回放。任何 buffered `Requested` 仍触发矛盾 fault，即使同批还有被动结束事件。

`passive_scan_end_before_rejected_stop_preserves_rejection_and_allows_restart` 覆盖被动暂停和原生扫描错误两种情况，验证拒绝优先、事件可见、槽位释放、重新扫描，以及旧 session/command 的晚到事件不影响新扫描。已有 rejected-stop 回归扩展为两种混合回调顺序，保证 fail-closed 策略不因允许被动回放而放宽。

## Begin 命令与具名交接结果

`Begin` 表示一次命令的 admission/交接阶段，不代表所有 BLE 资源都要「启动」，也不代表操作已完成。`BeginStop` 是开始处理 stop 命令，`BeginDisconnect` 是开始处理 disconnect 命令。actor 完成验证、交接、reservation 安装和原生派发任务提交后回复；backend 是否接受命令由 `DispatchResolved` 另行报告，BLE 完成仍依赖证据与 waiter 结算。

[`messages.rs`](../../crates/bleng/src/runtime/messages.rs) 统一定义 runtime 内部的 `BeginReply<Resource = ()>`，具名字段为 `command_id`、`result_rx` 和 `resource`。scan、connection、GATT discovery 和 notification enable 携带新分配的 typed resource ID；不新建资源的命令使用 `()`，无需 `Option` 或资源 ID enum。所有命令共用该结构，替代原四种 tuple 别名与订阅 tuple。owner 使用具名字段，保留原有结果与资源 ID 的对应检查。

`WaiterExpectation` 保持独立：它描述等待结果与 connection invalidation 规则，不负责接收端所有权。`BeginReply` 是一次性交接数据，不是新的可变操作状态或 guard；泛型表示不增加分配或同步，不改变公共 ABI、派发顺序、deadline 或取消行为。

### 统一 mailbox 协议与准入顺序

`messages.rs` 按三组组织类型及 handler，所有协议类型均为 `pub(super)`，限于 runtime 内：

1. 11 个请求 `BeginInitialize`、`BeginScan`、`BeginStop`、`BeginConnect`、`BeginDisconnect`、`BeginDiscoverGatt`、`BeginRssi`、`BeginRead`、`BeginWrite`、`BeginEnableNotifications`、`BeginDisableNotifications`，以及 `BeginReply` 与命令准入 handler。
2. `IngressEvent`、`DispatchResolved` 及两个仅调用 actor 受限入口的异步输入 handler；`DispatchResolved` 是派发结果，不是新的准入请求。
3. `RetireWaiter` 及调用 actor `retire_waiter(id, timed_out)` 的 handler；只退休调用方 waiter，不退休 native reservation。

owner 直接从 messages 导入请求与 waiter 退休消息，并接收具名 reply，负责调用方等待/deadline，不经 runtime 根重导出；backend_port 从 messages 导入 `IngressEvent`，actor 从同一模块导入 `DispatchResolved`。调用方订阅 API 仍归 `owner.rs`，订阅状态仍归 `reservations/subscription.rs`。

Begin handler 仍显式编排操作特定步骤，通过 actor 的受限方法依次检查 lifecycle/初始化与具体操作条件、分配所需资源 ID、注册 waiter，再调用 `launch_dispatch()`；它们不是一行转发。最后一步先取得可派发 Hub，再竞争 handoff，成功后同步安装 reservation 并提交后台派发；两者之间没有 await 或正常错误分支。验证失败不安装 reservation，预交接取消只退休 waiter，ID 跳号允许但不回滚重用。`launch_dispatch()` 保留在 actor 的私有状态边界，避免为源码整理重新开放字段。命令注释指向 actor.rs 中的协调实现。

第一组 Begin handler 按初始化、扫描 start/stop、连接、GATT、I/O、订阅顺序排列，随后是异步输入组与 waiter 退休组。局部变量明确区分 `command_id`、新建的 `session_id` 与连接集合的 `peripheral_id`。`OperationResult` 仅定义请求对应的成功解码，不抽象 admission/派发流程；不新增 mega message enum、通用执行 trait 框架、handler 生成宏、队列或额外状态机；验证/expectation/reservation/派发之间的显式对应关系是有意保留的可读性边界。

### `Result` 成功语义分层

保留各层 `Result<(), E>`：unit 表示该层没有额外成功数据，不表示没有错误或结果记账，也不合并各层 Error 或增加无信息成功 marker。

| 层次 | 成功含义 |
|---|---|
| 验证 | 检查通过；不表示交接、backend 接受或 BLE 完成。需要返回 ID 等数据的验证仍保留原值。 |
| `launch_dispatch()` / `BeginReply` | 交接成功、reservation 已安装、派发任务已提交；不表示 backend 已进入或接受命令。 |
| 同步 backend `Result<(), CommandDispatchError>` | `Ok(())` 表示派发接受，`Err` 表示拒绝；完整结果通过 `DispatchResolved` 送回 actor，不忽略，也不当作异步操作完成。 |
| 异步操作 / `WaiterSuccess` | 由 dispatch、domain 与 terminal 证据结算；read 数据、RSSI、数据库、ID/模式等有数据的成功结果保留原值。平台 write-without-response 的本地提交/进展不提升为远端处理保证。 |
| cleanup | `Ok(())` 表示已完成记账且没有报告清理错误；结果 slot 保留成功/错误。单纯「所有 slot 已结算」也可能产生 `Err`，不等于清理成功。 |

## 先判取消，再修改 reservation

```text
全部可失败的操作检查
  -> 登记 waiter + command/operation IDs
  -> launch_dispatch：获取 Hub 并检查 lifecycle
       准备失败 -> 以对应错误结束 waiter；不登记、不派发
       准备成功 -> BeginHandoff::try_claim
                    取消获胜 -> 只取消 waiter；reservation 从未创建
                    交接成功 -> 同步、不可失败的 reserve closure
                                -> spawn_blocking 原生命令
```

waiter/ID 登记成功后才进入交接。成功交接、reservation 登记和 blocking task 启动之间没有 `await`，reserve closure 中也不再执行可失败的操作检查。原生命令不可能先于 reservation 创建而开始执行。这样，取消若先于 dispatch 获胜，不需要恢复任何旧 reservation/session；初始化仍为 `NotStarted`，已有 GATT 数据库和订阅路由也没有被临时修改。本轮因此移除 `ReservationRollback`、局部 prelaunch rollback 方法与 `RollbackOutcome`。

owner 的交接保护责任**保留**，但自定义 `BeginHandoffGuard` 类型及本地 `armed` bool 已由 `scopeguard::guard` 替代（旧名称仅表示已被取代的实现）。原子 caller-versus-dispatch 竞态仍由 `BeginHandoff` 处理，不是被移除的 actor-local rollback guard。

`BeginHandoff` 合并原来的 `BeginState` 与携带 Hub 的 `BeginContext`，只保存可克隆的原子交接 token，不携带 Hub 或 lifecycle。类型仍位于 `owner.rs`：owner 创建 token，并负责确认与取消；actor 仅消费 `try_claim()`。不能因为消费入口 `launch_dispatch()` 位于 actor，就将 caller 交接机制视为 actor 的私有状态。

共享原子状态与通用 RAII 保护分开表达：

```text
BeginHandoff atomic：Pending -> Claimed
                       |          |
                       +------> Cancelled
                                （取消前已 Claimed 则请求 lifecycle fault）

scopeguard::guard(BeginHandoff::new(), closure)
  +-- 未解除保护的 Drop -> abandon() -> was_claimed 时 lifecycle.fault()
  +-- 收到 BeginReply -> drop(ScopeGuard::into_inner(handoff))
                         不执行 closure；释放捕获的 lifecycle 与返回的 token
```

`Claimed` 只表示 actor 取得派发权，不保证原生命令已进入。共享 token 的 `Pending` / `Claimed` / `Cancelled` 原子协议不变，也没有 `Acknowledged` phase。`run_operation()` 创建 `scopeguard::guard(BeginHandoff::new(), move |handoff| { ... })`；闭包只捕获 `Arc<RuntimeLifecycle>`，不捕获 `RuntimeOwner` 或 Hub。未解除保护的 Drop 调用 `abandon()`，返回的 `was_claimed` 表示 actor 此前是否已认领；仅在已认领时调用 `lifecycle.fault()`，不承诺取消原生工作。非法 atomic 值仍按已取得派发权 fail closed。

begin 等待超时分支先显式 `drop(handoff)`，再读取 phase 分类错误，保留 `Closed` / `TransportFault` 优先于普通 `Timeout` 的规则；不能先选定 `Timeout` 再让 guard 析构。收到 `BeginReply` 后立即执行 `drop(ScopeGuard::into_inner(handoff))`：不执行 abandonment 闭包、不写 atomic，先释放闭包捕获的 lifecycle，再释放取出的 token，均发生在等待操作证据之前。不再保留自定义 `acknowledge(self)` 或本地 armed 状态。

owner 保留 backend availability 预检查，以维持现有错误优先级，但临时 Hub 引用在等待 mailbox reply 前即释放。begin message 只携带 `handoff` 与操作参数；actor 通过自身 lifecycle 重新获取派发引用，在获取 cleanup 锁之后检查 phase，再尝试交接。Hub 不可用、close 或 fault 都会以对应错误结束已登记的 waiter，跳过 reserve 和 dispatch。actor 不新增长期 Hub 字段；该检查也不承诺与并发 cleanup 原子化。

owner 预检查与 actor 准备统一调用 `RuntimeLifecycle::hub_for_dispatch() -> Result<Arc<BleHub>, WaiterError>`。只有 cleanup 保留底层 `Option` 查询，它只知道自己的 cleanup gate，不负责 runtime phase。lifecycle 组合该查询与获取 cleanup 锁之后的 phase 检查；runtime 不再选择「普通 Hub」与「open Hub」两种入口，也不重复错误映射。owner 与 actor 是两个不同时间点的检查：前者保持 admission 错误优先级，后者在等待 mailbox 后重新确认。

`try_claim()` 只原子认领交接，不安装 reservation、不提交 dispatch task，也不证明原生调用已开始；完整派发编排仍由 actor 的 `launch_dispatch()` 负责。它是改变所有权的 CAS，不是只读的取消探测，因此必须放在可失败的 Hub/lifecycle 准备之后。若先取得派发权，再因 Hub 不可用而返回，token 会留在 `Claimed`，caller guard 将普通准备失败误判为交接后的故障。early return 的顺序是：准备失败、交接失败，然后才是不可失败的 reserve 与 task launch。

reserve closure 仍有实际价值：将局部安装延迟到交接成功后，不增加安装 enum、trait 框架或 rollback。messages 中 Begin handler 需要的纯转发继续保留；仅 actor 内部使用的 `finish_waiter` 机械转发删除，在同一隐私边界内直接使用 registry。

取消边界分三种结果：

```text
run_operation 中由 owner 统一 ask 并等待 actor reply
  +-- caller 取消先于 try_claim 获胜
  |     -> 跳过 reservation 和原生派发；只移除 waiter
  +-- try_claim 获胜 -> 同步 reserve -> 启动 blocking task
        +-- owner 确认 begin reply 前取消
        |     -> owner Faulted -> close/cleanup；不是 rollback 或原生取消
        +-- owner 确认 begin reply -> WaiterTicket
              -> timeout/cancel 只退休 caller waiter；保留 reservation
```

这里的确认指 owner 接收 actor 的 begin reply，不是 backend dispatch acknowledgement 或 BLE terminal。交接成功但 owner 尚未确认时取消，由 handoff 的 scope guard 将 owner fail closed，不能描述为普通 waiter-only retirement。handoff 只确立 dispatch 所有权，不证明原生命令已进入；cleanup 与 native dispatch 可以竞态，因此 fault 也不提供 native-entry 或原生取消保证。

### 通用复用与保留的领域责任

| 机制 | 当前复用 / 必须保留的边界 |
|---|---|
| shutdown 结果等待 | `watch::Receiver::wait_for(Option::is_some)`；只检查通道值并克隆结果。 |
| Begin 交接 RAII | `scopeguard::guard` / `ScopeGuard::into_inner`；原子 `BeginHandoff` 及 claimed 后 fault 的领域规则保留。 |
| facade task 生命周期 | [`api.rs`](../../crates/bleng/src/api.rs) 用 `tokio_util::task::AbortOnDropHandle::new(private_handle.spawn(future)).await`，取代自定义 `AbortOnDrop`；外层 future 释放只请求 abort 本地 task，不取消原生工作，也不保证立即析构。 |
| waiter 退休 | 保留 `WaiterTicket`：命令关联、deadline、`RetireWaiter`、发送失败 fault 与 Drop fallback 不能由通用 guard 替代。 |
| cleanup 完成 | 保留 subscribe → Mutex ledger 检查 → 解锁 → `changed().await` 循环；`CleanupSignal` 只是唤醒，锁序禁止把 ledger 检查放进 `wait_for` predicate。 |
| cleanup 执行与 phase | 保留 shutdown 后仍可调度的独立 OS worker，以及携带错误语义的 phase/结果；`CancellationToken` 不能替代。 |

根 workspace 精确新增 `scopeguard = "=1.2.0"`（`default-features = false`）与 `tokio-util = "=0.7.19"`（`default-features = false`，只启用 `rt`）；BLeng 通过 workspace 依赖复用。`Cargo.lock` 仅新增这两个 package，其余所需 package 已存在，已离线解析。本地缓存源码审计覆盖 scopeguard 的 `guard` / `Drop` / `into_inner`、tokio-util 的 `Future` / `Drop` 和 Tokio 1.53.1 的 watch `wait_for` 读锁行为；详见[英文复用记录](bleng-waiter-design-2026-10-06.md#simplification-and-ecosystem-assessment)与[cleanup 锁序约束](bleng-cleanup-design-2026-10-07.zh-CN.md)。本轮复用不引入网络、额外 runtime、线程或 ABI 变化；既有 cleanup 线程保留。

## GATT 提交、保留与局部回收

`ConnectionReservation.gatt` 始终是具体的 `GattReservation`，不再是 `Option<GattReservation>`。已提交的 `database` 为私有字段，外部通过 `committed_database()` 查询，只有 GATT owner 可以提交替换。其 `database: Option<GattDatabase>` 与 `discovery: Option<GattDiscoveryReservation>` 是两个独立槽位：两者都为空表示尚无数据库或 pending discovery；rediscovery 期间两者可以同时存在，不能改成互斥 enum。draft 仍属于 discovery。

```text
ConnectionReservation.gatt: GattReservation
  +-- database: None / Some(已提交数据库)
  +-- discovery: None / Some(progress + session_id + draft_database)
       完整成功 -> 先结算 waiter -> 替换 database -> 清除 discovery
       拒绝/失败 -> 只清除 discovery -> 保留 database
```

waiter 先结算，随后 `retire_completed_discovery()` 退休已完成的 discovery，仅在成功证据完整时替换已提交数据库。此处不返回 bool，也不清空订阅：discovery 准入要求所有订阅已移除，pending discovery 又阻止新 enable，原生 callback 不会创建 route，因此完成时订阅容器必为空。外层 `Option`、`get_or_insert_with` 和「内层全空就将外层归一化为 `None`」的代码已无必要。

rediscovery 的 dispatch rejection 或 terminal failure 仍只移除临时 discovery，不抹掉已有数据库和可用 session；交接前取消则根本不会创建临时 discovery。`resolve_pending_slot()` 统一 discovery、RSSI、read、write、disconnect 的 command 匹配、接受确认和拒绝处理：接受则回放 buffered events；拒绝则移除本次 pending slot，并且只允许被动断开回放。discovery 的 slot 与已提交数据库分离，因此移除不会抹掉旧数据库。订阅不使用此 helper，因为它允许 commanded connection loss，且 enable/disable 的拒绝恢复不同；`reject_subscription_transition()` 取代旧 `take_subscription_transition()`：先取出 transition 的 buffered events，再由订阅状态决定保留或移除 route。这些是 dispatch 拒绝/失败后的移除，不是 prelaunch rollback。`retire_completed_operations()` 与 `is_retirable()` 是证据完整后的 retirement，在 connection 内决定局部回收及连接是否可以移除，actor 仍拥有外层 `ConnectionReservations` 容器。

terminal 采用首个结果，重复且冲突的 terminal 不能在等待数据库期间覆盖已有结果。这与「保留已提交数据库」一起防止无效 rediscovery 破坏旧 session。

### 订阅状态与显式移除结果

[`SubscriptionReservation`](../../crates/bleng/src/runtime/reservations/subscription.rs) 保留 session/characteristic 身份，私有 `state: SubscriptionState` 只表达以下合法组合：

```rust
enum SubscriptionState {
    Enabling {
        mode: NotificationModeRequest,
        transition: NotificationTransition,
    },
    Active,
    Disabling(NotificationTransition),
}
```

不再并列保存 `active: bool` 与可选 transition；`NotificationTransition` 只持有 `progress` 和 `domain: Option<NotificationState>`，不重复保存 action。`NotificationTransition` 与 `transition()` 均为订阅内部细节；连接只通过 `progress()` / `progress_mut()` 访问共有命令进度，通过 `waiter_result(command_id, connection_id, subscription_id)` 获取具名结果，不再读取 transition 或拼装结果身份；`is_active()` 对 `Active` 和 `Disabling` 均为 true，`is_transitioning()` 对 enable/disable 为 true。disable 完成前保留 route 不等于承诺远端继续发送。

```text
Enabling -- 已接受 + terminal 成功 + 匹配 Enabled --> Active（Keep）
    +-- dispatch 拒绝 / 已接受的 terminal 失败 --> Remove
Active -- begin_disable --> Disabling
Disabling -- 已接受 + terminal 成功 + 匹配 Disabled --> Remove
    +-- dispatch 拒绝 / 已接受的 terminal 失败 --> Active（Keep）
证据尚未齐全或仍等待 dispatch ack --> 保留当前状态（Keep）
```

`retire_transition()` 与 `reject_transition()` 显式返回 `SubscriptionDisposition::Keep` / `Remove`，不再用 bool 隐含 route 生命周期。连接在收到 `Remove` 后立即移除 route；订阅状态本身不增加 Removed tombstone。成功 transition 必须有对应的 domain，terminal 失败不要求 domain，但仍须等 dispatch ack 才能退休。严格模式在每个相关 Enabled callback 上验证，即使已有首个 domain 也不能忽略冲突模式；actual mode 仍从 transition 的 domain 构造结果，不另存副本。

### Rediscovery admission 行为修正

目标 connection 存在任何 active subscription 或 pending enable/disable transition 时，`BeginDiscoverGatt` 在派发前返回 `GattBusy`。调用方必须显式 disable 全部订阅并等待 disable 完整完成，才能 rediscovery；超时或取消 waiter 不等于 disable 完成。这是有意的 BLeng admission 行为修正，不只是文件合并；公共 ABI 形状不变。

已评审计划区分 [§8.5 discovery 失败时不替换 cache](../../.plan/PLAN.md#85-gatt-discovery) 与 [§8.8 subscription lifecycle](../../.plan/PLAN.md#88-开启-notification)。Apple 会在 discovery 前[排队执行原生 unsubscribe](../../platforms/apple/Sources/BleHubApple/AppleBleBackend.swift#L287-L301)，并[回收订阅路由](../../platforms/apple/Sources/BleHubApple/GattIoController.swift#L340-L390)，不发 subscription-invalidation event；随后 discovery 仍可能失败。因此保留旧 GATT database 并不保证原生订阅仍然存活，BLeng 不能据此保留虚假的 active route。[Android discovery admission](../../platforms/android/library/src/main/kotlin/dev/blehub/android/AndroidBleBackend.kt#L310-L312) 已要求先 disable 再 rediscovery。此门禁避免依赖 backend 的隐式 unsubscribe，也不修改薄契约。

## 回归证据与验证范围

[`runtime/tests/gatt.rs`](../../crates/bleng/src/runtime/tests/gatt.rs) 保留以下行为回归：

- rediscovery 拒绝或失败后，已提交数据库与旧 session 仍可读取。
- 数据库到达前，冲突的重复 terminal 不覆盖首个成功结果。
- disable 完整退休后才允许 rediscovery；收到新 draft 仍不能 enable，只有完整提交后新 session 才能启用订阅，旧 session 与旧 subscription 不会复活。

真实取消路径回归已替换原 prelaunch RAII 测试：

- `prelaunch_rediscovery_cancellation_preserves_committed_database_for_reads`：取消在 dispatch 交接前获胜时，不创建 reservation，旧数据库/session 仍可读取。
- `connection_leaf_prelaunch_cancellations_preserve_siblings_database_and_active_subscription`：RSSI、读写、断连和订阅 transition 的取消不影响其他 pending 操作、已提交 database 和 active 订阅。
- `cancelling_begin_after_launch_handoff_before_reply_faults_and_disposes_once`：通过实际 `launch_dispatch` helper 覆盖 dispatch 获胜侧；reserve closure 内的有界同步 gate 在交接后阻止 begin reply 返回，此时丢弃 caller，断言 `Faulted`、close 完成以及 dispose 恰好一次。该测试有意不断言 native entry，因为 cleanup 与 native dispatch 可以竞态。

[`runtime/tests/owner.rs`](../../crates/bleng/src/runtime/tests/owner.rs) 的 `cleanup_before_launch_finishes_registered_waiter_without_reserving_or_false_faulting` 覆盖 waiter 登记后、交接前的三种准备失败：phase 仍 Open 但 cleanup 已开始、显式 close、fault。断言对应 waiter 错误、不登记 reservation、不派发、guard 不误报 fault，以及 dispose 恰好一次。

扫描回归新增 `foreign_scan_error_cannot_retire_suspended_stop_awaiting_terminal` 和 `native_stop_retains_live_start_until_domain_and_terminal_settle`，分别覆盖跨 session 错误隔离以及 start domain/terminal 两种顺序的完整记账。订阅回归覆盖 active subscription、disable 失败、disable 超时/取消后的晚到完成，以及 `notification_rediscovery_waits_for_late_disable_without_blocking_other_devices` 的跨连接隔离。

订阅 route 不再保存生产代码未读取的 `actual_mode` 副本；enable 的实际模式仍从 transition 证据构造结果，再释放该证据。

此前 actor/lifecycle 简化记录曾报告 runtime actor 测试 115 项通过；[`runtime/tests/lifecycle.rs`](../../crates/bleng/src/runtime/tests/lifecycle.rs) 中两个新回归均通过：`waiter_deadline_retirement_mailbox_overflow_reports_transport_fault_in_both_paths` 覆盖已过期与 timer 两条路径的退休发送失败；`close_waits_for_actor_teardown_after_mailbox_closes` 阻塞 Kameo unregister，证明 mailbox 已关闭、cleanup 已完成时 close 仍等待实际 actor teardown。

此前新增 API 回归 [`dropping_facade_waiter_aborts_private_task_and_releases_its_capture`](../../crates/bleng/src/tests/api_tests.rs) 曾报告通过：先确认私有 task 已开始，再丢弃外层 future，并有界等待其捕获释放；这不是立即析构或 native cancellation 保证。此处保留该测试的原始说明；当前 reservations 波次的验证见下文。

2026-10-07 当前 reservations 工作树已完成以下确定性验证：

- `pixi run rtk cargo test --locked --offline -p bleng -- --test-threads=1`：156 项通过。
- `pixi run rtk cargo test --locked --offline --workspace -- --test-threads=1`：244 项通过，18 个 suites。
- `pixi run just fmt`、`pixi run just lint`：通过，包括 workspace strict Clippy。
- `pixi run just binding bleng-snapshot-check`：3 个已评审 Swift/Kotlin API 快照通过。
- `pixi run just docs-check-mermaid`：112 个 Markdown 文件中的 21 个图通过。
- UTF-8、代码围栏、尾随空白、冲突标记与 `git diff --check`：通过。

以上是当前未提交源码的本机确定性结果，不是 RF、跨平台 runtime 或 ABI freeze 证据。未新增依赖：命令证据的乱序累积不能用线性状态机或 lexical RAII 替代，现有枚举、迭代器和所有权已经覆盖本次复用。

此前记录的 locked workspace 验证为 222 个测试通过，其中 BLeng 的 134 个测试全部通过；workspace strict Clippy、格式、BLeng Swift/Kotlin 绑定快照及文档检查也通过。这些历史 gate 不重标为当前 candidate 的重跑；本次文档同步不声称新增 RF 验证或 ABI freeze。确定性 Rust 结果不等于平台 RF 证据。
