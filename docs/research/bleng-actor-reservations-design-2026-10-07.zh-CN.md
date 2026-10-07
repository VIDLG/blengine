# BLeng actor 与 reservation 设计

## 状态与范围

本文记录 BLeng runtime actor/reservation 所有权边界与已实现的 predispatch 顺序简化，以及 reservation 所有权统一、按生命周期拆分目录和三项生命周期评审修正。本文不是新的架构提案，也不改变底层 `BleHub` 的薄契约或公共 ABI 形状。完整 waiter、事件流和初始化历史说明见英文 [BLeng waiter design and operation completion](bleng-waiter-design-2026-10-06.md)；cleanup 的独立完成语义见 [cleanup 设计](bleng-cleanup-design-2026-10-07.zh-CN.md)。

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

## 目录与职责

```text
runtime/
  owner.rs
  lifecycle.rs
  actor.rs
  actor/
    events.rs
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
  commands.rs
  subscriptions.rs
```

| 源码 | 职责 |
|---|---|
| [`actor.rs`](../../crates/bleng/src/runtime/actor.rs) | 保留 `BlengActor`、`Drop`、register/reserve 方法；协调跨 reservation 的动作和 waiter 结算，不再持有 prelaunch rollback guard。 |
| [`reservations/mod.rs`](../../crates/bleng/src/runtime/reservations/mod.rs) | 记录状态层边界，只重导出 runtime 实际使用的状态入口和协调结果。 |
| [`dispatch.rs`](../../crates/bleng/src/runtime/reservations/dispatch.rs) | 共享 dispatch 证据、pre-ack 缓冲边界，以及一次性 reconciliation 结果和错误。 |
| [`initialization.rs`](../../crates/bleng/src/runtime/reservations/initialization.rs) | 初始化证据、readiness 和初始化状态转换。 |
| [`scan.rs`](../../crates/bleng/src/runtime/reservations/scan.rs) | 唯一扫描槽位、start/stop 证据和 session 退休。 |
| [`connections.rs`](../../crates/bleng/src/runtime/reservations/connections.rs) | 连接集合、admission、操作登记、全局回调路由优先级和批量退休。 |
| [`connection.rs`](../../crates/bleng/src/runtime/reservations/connection.rs) | 单连接根记录、disconnect、局部事件关联、dispatch reconciliation 和退休。 |
| [`gatt.rs`](../../crates/bleng/src/runtime/reservations/gatt.rs) | 已提交数据库与 pending discovery 的不同生命周期。 |
| [`io.rs`](../../crates/bleng/src/runtime/reservations/io.rs) | RSSI、characteristic read/write 的局部证据与结果。 |
| [`subscription.rs`](../../crates/bleng/src/runtime/reservations/subscription.rs) | 订阅 route、enable/disable transition 与实际模式验证。 |

此前先统一 reservation 所有权与接口；当单文件增长到约 2300 行、内部边界稳定后，再按生命周期拆成 `reservations/`。这是源码组织拆分，不是新增运行时边界；不新增 actor、executor、锁或命令索引。集合与单条记录分别由 `connections.rs`、`connection.rs` 管理，不能因拆文件把全局操作类别路由顺序改成逐连接路由。

可见性由私有实现模块与受限 facade 共同控制：`mod.rs` 中所有实现模块保持私有，只有真正的状态入口与协调结果通过 `pub(super) use` 向 runtime 重导出。实现文件的这些入口使用 `pub`，但没有公开模块路径或更宽的重导出，因此不是 crate 公共 API，也不改变 ABI。目录内部共享的类型、字段与 helper 使用 `pub(super)`，文件局部实现保持私有，不再逐项重复 `pub(in crate::runtime)`。

初始化与扫描 enum 的 payload 类型必须与 enum 的声明可见性匹配，但不单独重导出，字段与局部方法也不开放。`pending()`、`reserved()`、`reserved_mut()`、`find()`、`find_mut()` 等原始记录查询均为内部实现。actor 断连结算通过 `ConnectionReservations::accepted_command_ids(connection_id)` 取得所需 command ID，不再取得整个连接记录。测试通过 dispatch resolution、相关事件、结果与 admission 判断验证行为，不为了检查内部字段放宽可见性。

reservation 是 commands 与 events 共用的 actor-confined 状态模型，不只是被动字段容器：它拥有局部 admission、回调关联、证据记录、dispatch reconciliation、结果判断与退休规则。reservation 是 actor 内的普通值，不单独调度。[`commands.rs`](../../crates/bleng/src/runtime/commands.rs)、[`actor/events.rs`](../../crates/bleng/src/runtime/actor/events.rs) 和 [`subscriptions.rs`](../../crates/bleng/src/runtime/subscriptions.rs) 仍负责消息与 ingress 集成；局部状态判断靠近其所有者。

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

回归统一见 [`runtime/tests/id_allocation.rs`](../../crates/bleng/src/runtime/tests/id_allocation.rs)：覆盖相同数值的精确事件关联、失败/取消后的跳号与不复用，以及最大 ID 只能发出一次且耗尽后不回绕。删除了仅证明两个独立局部 generator 互不影响的测试；实际 actor 的 command/resource 分配分离仍有覆盖，不再单独挂 `id_sequence` 测试模块。

## 连接 reservation 容器

`BlengActor.connections` 持有 [`ConnectionReservations`](../../crates/bleng/src/runtime/reservations/connections.rs)，不是向兄弟模块公开的 `HashMap`。容器内部唯一的 `by_peripheral` map 保存连接根记录；不增加按 `ConnectionId` 或 `CommandId` 的第二份索引，也不增加锁、共享所有权或调度设施。

职责分三层：

| 层次 | 职责 |
|---|---|
| `BlengActor` | lifecycle gate、ID/等待方登记、命令交接和 reservation 安装协调、事件发布、waiter 结算。 |
| `ConnectionReservations` | 按 peripheral 登记、按 connection/command 查找、terminal/result 路由、批量退休与根记录移除。 |
| `ConnectionReservation` | 单连接状态、GATT/I/O/subscription 证据、`validate_gatt_io()` 的局部 admission 条件、结果与退休判断。 |

`ConnectionReservations` 不包 `Option`：空容器已经表示没有 connection reservation，没有额外的「未创建容器」状态。唯一 scan slot 由 `ScanState` 表达 `Idle` 或 `Reserved(ScanReservation)`，根状态的验证、安装和释放也由它拥有。连接容器中的每一条 reservation 有各自的生命周期，但容器本身与 actor 同生共死。

`reserve_connection()` 只允许空 peripheral 槽位，不能覆盖仍持有 native 生命周期证据的旧记录。正常重复 connect 仍在 admission 阶段返回 `PeripheralBusy`；内部重复安装则是 invariant failure，而不是静默替换。连接结束并完整记账后，容器才释放该 peripheral，后续连接使用新 connection/command ID，旧 terminal 不能命中新记录。

容器不实现 `Deref<HashMap>` 或 `Index`，也不向外返回 map。pre-ack 路由和操作特有的拒绝回放筛选已交回容器及所属 reservation；事件协调层不再遍历连接记录，原 `iter_mut()` 接口删除。回放执行、fault/cleanup 和 waiter 交付仍由 actor 协调，没有引入通用集合策略框架。

`DispatchResolved` 先由容器按 command 找到所属 connection，再处理其中的具体 slot，不再为每个操作重复扫描整张连接 map。domain event 通过 connection ID 查到目标后，仍校验 command/session/characteristic/subscription 的对应关系。`validate_gatt_io()` 移至单连接记录，保留原来的错误优先级。

调用顺序仍是记录证据、结算 waiter、再调用 `connections.retire_completed()`；容器不持有或自行结算 waiter。相关回归覆盖重复安装不能覆盖旧根、跨连接 terminal 隔离，以及同一 peripheral 重连后拒绝旧 terminal。

## Actor 封装与纯转发

Rust 的 `pub(super)` 将可见范围限制在 `runtime` 及其子模块，不是对 crate 用户公开；此前主要用于让兄弟模块中的 `impl BlengActor` 访问字段。但字段可见并不等于所有调用点都需要容器本体。

`event_tx` 和 `waiters` 现在是 actor 私有字段。`publish_event()` 只负责入口 open gate、向外入队和队列失败时 fault；成功仅表示本地入队。`deliver()` 在发送后再次检查 lifecycle，才继续处理证据。前者是发送前置条件，后者是事件协调的继续处理条件，不在同一函数里重复 `require_open()`。发送后的检查应对并发 shutdown，但不能撤回已入队事件，也不保证发布与关闭原子化。等待方的取消、超时和交付通过 actor 方法，使用 `delegate!` 生成纯转发，而不是暴露 registry。

`require_open()` 的 phase-to-error 映射属于 `RuntimeLifecycle`；`require_initialized()` 的 readiness-to-error 映射属于 `InitializationState`，只有 `Ready` 通过，`NotStarted` 和 `Pending` 均返回 `NotInitialized`。actor 通过 `delegate!` 使用这两个门禁，不再重复判断状态。`reserve_connection` 和连接内的 `reserve_*` 入口也通过 `delegate!` 转发给 `ConnectionReservations`，actor 不再拼装 disconnect/RSSI/I/O/subscription 字段或持有 `connection_mut()` helper。`InitializationState::reserve_initialize()` 建立 pending 状态，私有 `InitReservation::new()` 拥有初始证据；actor 同样只转发。`ScanState` 拥有 scan 根的 admission、start/stop 登记、拒绝移除和退休，stop slot 的初始化仍属于 `ScanReservation`。所有纯转发集中在一个 `delegate!` 块中并按所有者分组；scan 的四个命令入口统一 `to self.scan`，不再由 actor 解包 `Option` 或表达 scan 可用条件。

actor 的 `lifecycle`、`initialization`、`scan`、`connections` 现在全部私有。命令和订阅模块不再取得 reservation 实体：初始化/扫描/连接/GATT/RSSI/读写/订阅的 admission 条件由对应所有者验证，actor 用 `delegate!` 转发，返回值只包含成功/错误或后续登记需要的 ID，保留原错误优先级。scan slot 的空闲/占用判断属于 `ScanState`；session 内 stop 验证属于 `ScanReservation`。

`launch_dispatch()` 移至 actor 根，只有这里内部克隆 lifecycle 给分离的 blocking dispatch task；命令模块不再获取或克隆 actor 的 lifecycle Arc。`actor::events` 是私有子模块，物理文件采用常规的 `runtime/actor/events.rs`，通过 `mod events;` 声明，不再使用 `#[path]` 重定向；它协调证据缓冲、发布、记录、结算和退休，但回调关联、证据修改、订阅模式验证及 dispatch 接受/拒绝的局部变化属于 reservation。它属于 actor 的实现，不是外部状态消费者；现在通过 owner 方法完成这些动作，不再逐层解包 reservation 字段，也不添加返回整个实体的 getter、重复状态或第二层调度。`resource_id_gen` 和 `IdGenerator` 同样私有，命令通过 actor 的 `allocate_resource_id()` 分配。

同一项 reservation 登记在 actor 和拥有者上采用同名 `reserve_initialize()`、`reserve_connection()`、`reserve_scan_stop()`，不再通过 `#[call(begin)]`、`#[call(register)]` 或 `#[call(begin_stop)]` 建立额外的名称对应。generator/registry 的上下文不同，`allocate_resource_id()` → `allocate()`、`cancel_waiter()` → `cancel()` 等改名转发仍保留。

`delegate!` 仅表达已明确所有者的纯转发。涉及 ID 分配、发布失败策略、跨 reservation 结算或先后顺序的方法仍显式实现；宏不是职责划分的替代品。

### 可见性与命令结果路由

`BlengActor` 类型、`WaiterReceiver` 别名及 `RuntimeOwner.actor` 字段仅在 `runtime` 内可见；`runtime/mod.rs` 不再向 crate 其他模块重导出 actor 类型。actor 全部字段私有；供兄弟命令/订阅模块实际调用的验证、分配、登记和交接方法保留 `pub(super)`，只供事件实现使用的发布、terminal 路由、waiter 结算与失败传播方法保持私有。保留显式的策略与协调方法，不为了统一形式把它们改成纯转发。

runtime 的生产代码和测试改用显式导入，不再使用父模块或 prelude 的 glob 导入；`mod.rs` 不再为子模块集中导入外部依赖。测试物理文件仍位于 `runtime/tests/`，逻辑上作为 actor 内部测试编译，白盒断言可检查私有不变量，但不为测试放开生产字段、添加可变 getter 或增加测试专用的生产方法。可见性由生产依赖决定。`#[path = "tests/mod.rs"]` 仅为保留这套测试的既有物理位置，不改变生产模块边界。

测试按不变量而非行数保留：生命周期、错误优先级、取消、关联和耗尽边界有价值；独立局部变量互不影响、原样结果转发等无独立行为的重复测试删除。waiter 的单独 dispatch-rejection payload 转发测试已移除，恰好一次交付与 runtime 的实际拒绝处理仍由原有回归覆盖。

`record_terminal()` 按 initialization、scan、connections 的顺序逐段查找，前两段命中即 `return`，不将有副作用的调用组合在 `||` 中。reservation 返回的 `bool` 只表示「这个 command ID 属于我」，不表示操作成功；匹配到重复 terminal 也要停止查找，而已有首个 terminal 不被覆盖。actor 内 command ID 唯一，因此同一 terminal 不广播给多个 reservation。

`waiter_result()` 同样采用逐段 early-return，但停止条件是拿到 `Some(result)`，包括 `Some(Err(...))`；`None` 可能表示 ID 不属于当前 reservation，也可能表示证据未齐，不能当成操作错误或命令不存在。继续查找不会将未就绪的命令交给其他所有者，因为 command ID 不复用且唯一；无需额外结果路由 enum 或第二份命令索引。

## 共享门禁与派生状态

`DispatchReservation` 保存 `command_id`、`dispatch_accepted` 和 `buffered`。`is_awaiting_dispatch_ack()` 只表示尚未收到 dispatch acknowledgement，不表示 waiter 或原生操作仍 pending；dispatch 被接受不等于 BLE 操作完成。

`DispatchReservation::waiter_result()` 只共享 accepted-dispatch/terminal 边界：未确认 dispatch 或缺少 terminal 时不能成功；terminal 错误交付为操作错误；terminal 成功后由具体 reservation 根据 domain 证据构造结果。连接错误、被动断连等操作特有的优先级仍由具体 reservation 判断，不隐藏在通用状态机中。

| 已移除的冗余 bool | 唯一证据来源 |
|---|---|
| scan `failed` | `start_terminal` 为 `Some(Err(_))`。 |
| connection `failed` | `connect_error` 为 `Some(_)`。 |
| discovery `failed` | `terminal` 为 `Some(Err(_))`。 |
| discovery `discovered` | `draft_database` 为 `Some(_)`。 |

独立 domain 事实仍需保留。拿到 draft database 并不表示 discovery 已成功，还必须确认 dispatch 被接受且 terminal 成功。

## 证据、结算与回收顺序

```text
publish to bounded event FIFO
  -> record domain / terminal evidence
  -> settle affected waiters from reservation evidence
  -> retire completed reservations / commit successful discovery
```

必须先记录证据，再通过 [`WaiterRegistry::finish()`](../../crates/bleng/src/waiter.rs) 恰好一次交付结果，最后释放已完整记账的状态或提交数据库。domain 与 terminal 的先后不作假设；dispatch ack 前逃逸的回调先缓冲，再按关联规则处理。

owner 确认 actor 的 begin reply 后，超时或调用方取消只退休 waiter，保留 reservation，不承诺取消已派发的原生 BLE 操作。晚到回调仍可推进初始化就绪、原生/session slot 回收或 GATT commit，但不能重新给已退休的 waiter 交付旧结果。

## 事件层与 reservation 的分工

`actor/events.rs` 只保留事件路由与副作用执行，`InitializationState`、`ScanState`、`ConnectionReservations` 统一提供三个有实际职责的入口：

| 入口 | 所有者负责的行为 |
|---|---|
| `buffer_before_dispatch(event)` | 关联 callback，只有 dispatch 未确认时进入原有有界缓冲；`Ok(None)` 表示已消耗，`Ok(Some(event))` 交给下一个所有者，overflow 返回错误。 |
| `record_event(&event)` | 校验 command/connection/session/target，修改本地证据；不发布事件、不结算 waiter、不执行生命周期动作或完成退休。 |
| `resolve_dispatch(command_id, result)` | 接受 dispatch 并取出缓冲，或按操作规则移除被拒绝的 provisional slot；返回回放列表和必要的后续动作。 |

叶子 reservation 的关联方法由缓冲和证据记录复用。记录时仍先按 domain event 类型分支，因此匹配 terminal 或断连的宽泛缓冲规则不会被误用为 read/RSSI/value 等证据。连接状态与 explicit disconnect domain 在同一次连接查找中记录。首次 snapshot/read/RSSI/subscription 证据保持不变；GATT draft 仍保留最后一次关联结果，未改变既有策略。

pre-ack 探测顺序保持：

```text
initialization
  -> connections: disconnect -> discovery -> RSSI -> read -> write -> subscription -> connect
  -> scan start -> scan stop
  -> 未缓冲事件进入 deliver
```

连接类别优先级作用于整个容器，不是每条连接各自探测完所有操作再换下一条；不使用 `dispatches()` 的一般遍历顺序代替它，也不新增命令索引。带外来 command 的已知连接断连事件仍公开交付，但在缓冲前和证据记录前均阻止其改变当前生命周期。

`DispatchResolution` 只是一次处理结果，不是新增的 canonical state：其中 `replay` 保存允许回放的事件，`DispatchEffect` 表示无需动作、cleanup 或 fault。actor 按以下顺序执行：

```text
reservation 局部 dispatch 变化
  -> actor 执行 cleanup / fault（如需要）
  -> rejected command 交付 DispatchRejected
  -> 按顺序回放允许的事件
       -> 发布 -> 记录 -> 结算 -> 退休
```

普通 GATT/read/write/RSSI/disconnect 的拒绝只允许回放被动 loss；订阅拒绝保留原来的较宽 connection-loss 规则。其他已逃逸 callback 是矛盾证据，要求 fail closed。干净的初始化拒绝只启动 Hub cleanup，不把 runtime 改成 Faulted。`ReconcileFault` 区分缓冲超限、实际通知模式不符和 dispatch 矛盾，actor 使用已有 tracing 记录原因，不记录 callback payload。

有界缓冲由 `DispatchReservation` 统一限制为 64 条，不为重构添加新队列、锁或 executor。回归见 [`reconciliation.rs`](../../crates/bleng/src/runtime/tests/reconciliation.rs)：覆盖精确容量、外来事件不消耗容量、原序回放，以及 I/O 与订阅拒绝的不同 loss 策略；既有同步 callback、晚到证据和取消测试继续保护完整 actor 流程。

## ScanState 与扫描回收

```text
Idle
  -> admission + caller handoff 成功
  -> Reserved(ScanReservation)
       -> 匹配的 start dispatch 被拒绝 -> Idle
       -> 完整证据记账、先结算 waiter、再退休 -> Idle
```

`Reserved` 只表示持有 scan slot，不等于已经开始射频扫描；它覆盖启动中、扫描中、stop 中以及 native 已停但晚到 start/stop 证据尚未齐全的阶段。没有额外增加 Running/Stopping 等重复状态，也不把独立的 domain、terminal 和 dispatch ack 事实压成线性 enum。

实际 payload 使用 `Box<ScanReservation>`：当前 reservation 内联大小为 432 字节，装箱避免空闲 scan slot 也占据整份会话的空间。代价是每次成功登记 scan 多一次分配，不是每个回调分配；不新增 `Arc`、锁、执行器或共享状态。

相较于 actor 持有 `Option<ScanReservation>`，新增类型的价值不是形式一致或方便宏转发，而是把 slot 可用性、登记和释放的策略放在同一个所有者上。`ScanState` 统一处理 terminal/result 路由、拒绝和退休，actor 只协调结算顺序。重复安装不能覆盖仍保留的会话；晚到的旧 start rejection 不能释放新会话。内部事件实现通过 `reserved()` / `reserved_mut()` 记录证据，这些访问不作为 actor 的对外 getter。


scan retirement 不再接收原始 event 参数，只根据已关联的 reservation 证据（包括 `stop.stop_error`）决定回收。无关 scan 的 `ScanStopped::Err` 不能回收当前 scan 或丢弃其 pending stop 关联。

scan 已停止也不代表 start 已完整记账：若 start 的 domain/terminal 证据不完整，必须保留 reservation，等证据足以结算 start waiter 后再回收。两种事件顺序、dispatch ack 前缓冲与晚到回调均遵循先结算、后回收。

## 先判取消，再修改 reservation

```text
全部可失败的操作检查
  -> 登记 waiter + command/operation IDs
  -> launch_dispatch：获取 Hub 并检查 lifecycle
       准备失败 -> 以对应错误结束 waiter；不登记、不派发
       准备成功 -> BeginHandoff::try_launch
                    取消获胜 -> 只取消 waiter；reservation 从未创建
                    交接成功 -> 同步、不可失败的 reserve closure
                                -> spawn_blocking 原生命令
```

waiter/ID 登记成功后才进入交接。成功交接、reservation 登记和 blocking task 启动之间没有 `await`，reserve closure 中也不再执行可失败的操作检查。原生命令不可能先于 reservation 创建而开始执行。这样，取消若先于 dispatch 获胜，不需要恢复任何旧 reservation/session；初始化仍为 `NotStarted`，已有 GATT 数据库和订阅路由也没有被临时修改。本轮因此移除 `ReservationRollback`、局部 prelaunch rollback 方法与 `RollbackOutcome`。

owner 的 `BeginHandoffGuard` **保留**：它处理 caller-versus-dispatch 的原子竞态，不是被移除的 actor-local rollback guard。

`BeginHandoff` 合并原来的 `BeginState` 与携带 Hub 的 `BeginContext`，只保存可克隆的原子交接 token，不携带 Hub 或 lifecycle。类型仍位于 `owner.rs`：owner 创建 token，并负责确认与取消；actor 仅消费 `try_launch()`。不能因为消费入口 `launch_dispatch()` 位于 actor，就将 caller 交接机制视为 actor 的私有状态。

共享状态与本地 RAII 状态分开表达：

```text
BeginHandoff atomic：Pending -> Claimed
                       |          |
                       +------> Cancelled
                                （取消前已 Claimed 则请求 lifecycle fault）

BeginHandoffGuard：armed -> acknowledge(self) -> disarmed 并消费 guard
                    |
                    +-- Drop -> 原子取消共享 token
```

`Claimed` 只表示 actor 取得派发权，不保证原生命令已进入。共享 token 不再保存 `Acknowledged`：actor 不需要观察 owner 是否已确认 reply。正常收到 begin reply 后，消费式 `acknowledge(self)` 只清除 guard 的本地 `armed`，随后 Drop 不写 atomic；未解除保护的 Drop 才取消共享 token，并在 actor 已取得派发权时请求 fault。非法 atomic 值仍按已取得派发权 fail closed。原 `BeginOperationGuard` 因此改名为更准确的 `BeginHandoffGuard`，没有增加同步设施或改变取消边界。

owner 保留 backend availability 预检查，以维持现有错误优先级，但临时 Hub 引用在等待 mailbox reply 前即释放。begin message 只携带 `handoff` 与操作参数；actor 通过自身 lifecycle 重新获取派发引用，在获取 cleanup 锁之后检查 phase，再尝试交接。Hub 不可用、close 或 fault 都会以对应错误结束已登记的 waiter，跳过 reserve 和 dispatch。actor 不新增长期 Hub 字段；该检查也不承诺与并发 cleanup 原子化。

owner 预检查与 actor 准备统一调用 `RuntimeLifecycle::hub_for_dispatch() -> Result<Arc<BleHub>, WaiterError>`。只有 cleanup 保留底层 `Option` 查询，它只知道自己的 cleanup gate，不负责 runtime phase。lifecycle 组合该查询与获取 cleanup 锁之后的 phase 检查；runtime 不再选择「普通 Hub」与「open Hub」两种入口，也不重复错误映射。owner 与 actor 是两个不同时间点的检查：前者保持 admission 错误优先级，后者在等待 mailbox 后重新确认。

`try_launch()` 是改变所有权的 CAS，不是只读的取消探测，因此必须放在可失败的 Hub/lifecycle 准备之后。若先取得派发权，再因 Hub 不可用而返回，token 会留在 `Claimed`，caller guard 将普通准备失败误判为交接后的故障。early return 的顺序是：准备失败、交接失败，然后才是不可失败的 reserve 与 task launch。

reserve closure 仍有实际价值：将局部安装延迟到交接成功后，不增加安装 enum、trait 框架或 rollback。兄弟命令模块需要的纯转发继续保留；仅 actor 内部使用的 `finish_waiter` 机械转发删除，在同一隐私边界内直接使用 registry。

取消边界分三种结果：

```text
begin_operation 等待 actor reply
  +-- caller 取消先于 try_launch 获胜
  |     -> 跳过 reservation 和原生派发；只移除 waiter
  +-- try_launch 获胜 -> 同步 reserve -> 启动 blocking task
        +-- owner 确认 begin reply 前取消
        |     -> owner Faulted -> close/cleanup；不是 rollback 或原生取消
        +-- owner 确认 begin reply -> WaiterTicket
              -> timeout/cancel 只退休 caller waiter；保留 reservation
```

这里的确认指 owner 接收 actor 的 begin reply，不是 backend dispatch acknowledgement 或 BLE terminal。交接成功但 owner 尚未确认时取消，由 `BeginHandoffGuard` 将 owner fail closed，不能描述为普通 waiter-only retirement。handoff 只确立 dispatch 所有权，不证明原生命令已进入；cleanup 与 native dispatch 可以竞态，因此 fault 也不提供 native-entry 或原生取消保证。

## GATT 提交、保留与局部回收

`GattReservation` 分开保存已提交的 `database` 和正在进行的 `discovery` / `draft_database`。waiter 先结算，随后 `commit_completed_discovery()` 仅在 discovery 的成功证据完整时替换已提交数据库；连接此时清除旧 GATT session 的订阅路由。

rediscovery 的 dispatch rejection 或 terminal failure 仍只移除临时 discovery，不抹掉已有数据库和可用 session；交接前取消则根本不会创建临时 discovery。`take_discovery()` 统一临时 discovery 的移除；`take_subscription_transition()` 在拒绝 disable 时保留 active route，在拒绝临时 enable 时移除尚未生效的 route。这些是 dispatch 拒绝/失败后的移除，不是 prelaunch rollback。`retire_completed_operations()` 与 `is_retirable()` 是证据完整后的 retirement，在 connection 内决定局部回收及连接是否可以移除，actor 仍拥有外层 `ConnectionReservations` 容器。

terminal 采用首个结果，重复且冲突的 terminal 不能在等待数据库期间覆盖已有结果。这与「保留已提交数据库」一起防止无效 rediscovery 破坏旧 session。

### Rediscovery admission 行为修正

目标 connection 存在任何 active subscription 或 pending enable/disable transition 时，`BeginDiscoverGatt` 在派发前返回 `GattBusy`。调用方必须显式 disable 全部订阅并等待 disable 完整完成，才能 rediscovery；超时或取消 waiter 不等于 disable 完成。这是有意的 BLeng admission 行为修正，不只是文件合并；公共 ABI 形状不变。

已评审计划区分 [§8.5 discovery 失败时不替换 cache](../../.plan/PLAN.md#85-gatt-discovery) 与 [§8.8 subscription lifecycle](../../.plan/PLAN.md#88-开启-notification)。Apple 会在 discovery 前[排队执行原生 unsubscribe](../../platforms/apple/Sources/BleHubApple/AppleBleBackend.swift#L287-L301)，并[回收订阅路由](../../platforms/apple/Sources/BleHubApple/GattIoController.swift#L340-L390)，不发 subscription-invalidation event；随后 discovery 仍可能失败。因此保留旧 GATT database 并不保证原生订阅仍然存活，BLeng 不能据此保留虚假的 active route。[Android discovery admission](../../platforms/android/library/src/main/kotlin/dev/blehub/android/AndroidBleBackend.kt#L310-L312) 已要求先 disable 再 rediscovery。此门禁避免依赖 backend 的隐式 unsubscribe，也不修改薄契约。

## 回归证据与验证范围

[`runtime/tests/gatt.rs`](../../crates/bleng/src/runtime/tests/gatt.rs) 保留以下行为回归：

- rediscovery 拒绝或失败后，已提交数据库与旧 session 仍可读取。
- 数据库到达前，冲突的重复 terminal 不覆盖首个成功结果。

真实取消路径回归已替换原 prelaunch RAII 测试：

- `prelaunch_rediscovery_cancellation_preserves_committed_database_for_reads`：取消在 dispatch 交接前获胜时，不创建 reservation，旧数据库/session 仍可读取。
- `connection_leaf_prelaunch_cancellations_preserve_siblings_database_and_active_subscription`：RSSI、读写、断连和订阅 transition 的取消不影响其他 pending 操作、已提交 database 和 active 订阅。
- `cancelling_begin_after_launch_handoff_before_reply_faults_and_disposes_once`：通过实际 `launch_dispatch` helper 覆盖 dispatch 获胜侧；reserve closure 内的有界同步 gate 在交接后阻止 begin reply 返回，此时丢弃 caller，断言 `Faulted`、close 完成以及 dispose 恰好一次。该测试有意不断言 native entry，因为 cleanup 与 native dispatch 可以竞态。

[`runtime/tests/owner.rs`](../../crates/bleng/src/runtime/tests/owner.rs) 的 `cleanup_before_launch_finishes_registered_waiter_without_reserving_or_false_faulting` 覆盖 waiter 登记后、交接前的三种准备失败：phase 仍 Open 但 cleanup 已开始、显式 close、fault。断言对应 waiter 错误、不登记 reservation、不派发、guard 不误报 fault，以及 dispose 恰好一次。

扫描回归新增 `foreign_scan_error_cannot_retire_suspended_stop_awaiting_terminal` 和 `native_stop_retains_live_start_until_domain_and_terminal_settle`，分别覆盖跨 session 错误隔离以及 start domain/terminal 两种顺序的完整记账。订阅回归覆盖 active subscription、disable 失败、disable 超时/取消后的晚到完成，以及 `notification_rediscovery_waits_for_late_disable_without_blocking_other_devices` 的跨连接隔离。

订阅 route 不再保存生产代码未读取的 `actual_mode` 副本；enable 的实际模式仍从 transition 证据构造结果，再释放该证据。

最终 locked workspace 的 222 个测试通过，其中 BLeng 的 134 个测试全部通过；workspace strict Clippy、格式、BLeng Swift/Kotlin 绑定快照及文档检查也通过。确定性 Rust 结果不等于平台 RF 证据。
