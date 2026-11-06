# BLeng Runtime 阅读与 Review 教程

**适用源码：** `crates/bleng/src/runtime/`、`crates/bleng/src/waiter.rs`、`crates/bleng/src/facade/`

**目的：** 帮助第一次阅读 BLeng runtime 的开发者理解 `actor.rs`、`ReservationCoordinator` 和 `reservations/` 的关系，避免从一个很长的 actor 方法跳进多个 reservation 文件后失去上下文。

**性质：** 本文是当前实现的阅读教程，不是新的架构提案，也不是平台 BLE 行为或 RF 通过证明。平台限制、候选证据和 release gate 仍以项目计划及各候选报告为准。

相关设计记录：

- [BLeng actor 与 reservation 设计](bleng-actor-reservations-design-2026-10-07.zh-CN.md)
- [BLeng waiter design and operation completion](bleng-waiter-design-2026-10-06.md)
- [BLeng cancellation 与 native termination 设计](bleng-cancellation-and-native-termination-design-2026-10-10.zh-CN.md)
- [BLeng native queue、reservation 与 reconciliation](bleng-native-queue-reservation-reconciliation-2026-10-11.zh-CN.md)

---

## 1. 先记住一个核心心智模型

BLeng 中一个 BLE operation 同时存在三条不同的生命周期。大多数复杂代码都是为了不把这三条生命周期误认为一条。

```text
                    同一个 CommandId
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                  │
        ▼                  ▼                  ▼
  caller delivery    native/BLE evidence   submission/control
  调用方结果能否取到    原生工作是否完成       是否已 claim/cancel
  WaiterRegistry      Reservation roots    OperationSubmission
  ResultSlot          CommandProgress      SubmissionState
```

### 1.1 Caller delivery

调用方的 `wait()` 等待的是一个 typed result。它由 `WaiterRegistry` 和 oneshot sender/receiver 负责。

调用方可以：

- 超时；
- 丢弃 wait future；
- 丢弃 operation handle；
- 已经领取结果后不再等待。

这些动作只影响 **caller delivery**，不自动证明 native BLE 工作已经取消，也不自动释放 reservation。

### 1.2 Native/BLE evidence

真正的 BLE operation 可能已经：

- 被 native backend 接受；
- 收到 domain event；
- 收到 terminal `CommandCompleted`；
- 等待 recovery 或 late callback；
- 允许 retirement。

这些事实由 `reservations/` 中的 reservation root 和 `CommandProgress` 保存。它们不能因为 caller timeout 或 waiter 被 drop 就消失。

### 1.3 Submission/control

operation 刚提交时，还存在一个 predispatch 竞态：

```text
Pending  --try_claim-->  Claimed  --native dispatch-->  native work
   │
   └------cancel_before_claim------> Cancelled
```

`SubmissionState` 的 CAS 决定 caller 的 predispatch cancellation 是否赢得 native dispatch。这个阶段和后面的 native cancellation control 不是同一件事。

---

## 2. 从外到内看完整结构

先不要从 `actor.rs` 中间某个方法开始。先建立整个对象图。

```text
Public Bleng facade
    │
    ├── begin_*：同步、有界、非阻塞提交
    └── typed Operation handle
            ├── command_id()
            ├── wait(timeout)
            └── cancel()
                    │
                    ▼
             RuntimeOwner
             ├── private Tokio Runtime
             ├── ActorRef<BlengActor>
             ├── shared CommandId allocator
             ├── public event receiver
             └── shutdown coordinator
                    │ bounded mailbox
                    ▼
             BlengActor
             ├── lifecycle
             ├── WaiterRegistry                 # caller delivery
             ├── resource_id_gen
             ├── public_event_tx
             └── ReservationCoordinator         # BLE evidence + command route
                 ├── CommandDirectory
                 ├── InitializationState
                 ├── ScanState
                 └── ConnectionReservations
                     └── ConnectionReservation per peripheral
                         ├── root connect progress
                         ├── disconnect reservation
                         ├── GattReservation
                         ├── IoReservations
                         └── SubscriptionReservations
```

### 2.1 每个对象只回答一个问题

| 对象 | 它回答的问题 | 它不负责什么 |
|---|---|---|
| `RuntimeOwner` | 这个 BLeng instance 的 runtime、actor、事件出口和 shutdown 如何活着？ | 不解释 BLE reservation evidence |
| `BlengActor` | 当前 actor mailbox 输入如何串行编排？ | 不把每个 BLE operation 的细节都存成 actor 字段 |
| `WaiterRegistry` | caller 的 result sender 还在不在、结果是否已经交付？ | 不证明 native operation 已完成 |
| `OperationSubmission` | caller 提交与 actor claim 的 predispatch 竞态如何线性化？ | 不保存 BLE domain evidence |
| `CommandDirectory` | 一个 `CommandId` 属于哪个 reservation root？ | 不拥有 domain evidence 或 native retirement |
| `ReservationCoordinator` | 如何根据 route 把命令和事件交给正确的 root？ | 不发送 caller result，不管理 runtime lifecycle |
| `CommandProgress` | 一个 logical command 的 dispatch、terminal、buffer、cancel、completion 状态是什么？ | 不解释 read 数据、GATT database 或 subscription mode |
| `InitializationState` | adapter initialization 的 snapshot/readiness evidence 是否完整？ | 不保存最新 adapter snapshot cache |
| `ScanState` | 当前唯一 scan session 的 start/stop evidence 是否完整？ | 不管理 connection |
| `ConnectionReservations` | 哪个 peripheral 拥有哪些 connection root？ | 不实现单 connection 内部每个操作的 domain 规则 |
| `ConnectionReservation` | 一个 connection 的 GATT、I/O、subscription、disconnect evidence 如何推进？ | 不管理所有 peripheral 的 map |
| `OperationTicket` / `ResultSlot` | typed handle 如何一次性领取 caller result？ | 不拥有 BLE 状态 |

---

## 3. 一次 read operation 到底发生什么

用 characteristic read 贯穿全文，最容易理解其他 operation。

### 3.1 提交阶段

```text
facade.begin_read(...)
    ↓
RuntimeOwner 分配 CommandId
    ↓
OperationSubmission<Resource=()>
    ├── id
    ├── SubmissionState
    │   ├── phase = Pending
    │   ├── result_tx
    │   └── completion = OnceLock
    └── resource = OnceLock<()>
    ↓
BeginRead message.try_send 到 actor mailbox
    ↓
同步 begin 返回 typed ReadOperation handle
```

此时 begin 返回成功只表示：

- command 已分配；
- submission 已进入有界 mailbox；
- caller 获得了 handle。

它不表示：

- actor 已经处理 message；
- reservation 已经建立；
- native read 已经调用；
- read 已经成功。

### 3.2 Actor admission 阶段

`runtime/messages.rs` 的 `Message<BeginRead>` handler 大致执行：

```text
require_open
    ↓
require_initialized
    ↓
validate_gatt_access(connection, session, characteristic, Read)
    ↓
register_operation_waiter(submission, WaiterExpectation::CharacteristicRead(...))
    ├── WaiterRegistry 注册 caller sender
    └── ReservationCoordinator.register_command(id, CommandRoute::Connection{...})
    ↓
launch_dispatch(...)
```

这里的 `CommandRoute` 已经注册，但 connection reservation 尚未安装。它表示“这个 command 如果被接受，应该路由到哪里”，不等于 native 已接受。

### 3.3 `launch_dispatch` 的线性化点

```text
launch_dispatch
    │
    ├── lifecycle.hub_for_dispatch()
    │       失败：discard waiter + remove command route + 返回同步 admission error
    │
    ├── submission.try_claim()
    │       失败：caller predispatch cancellation 赢得竞态
    │              cancel waiter + remove command route
    │
    ├── reserve(&mut ReservationCoordinator)
    │       建立 ConnectionReservation 内的 read slot
    │
    ├── attach_completion_observer(command_id)
    │       同一个 observer 同时挂到 reservation root 和 CommandDirectory
    │
    ├── submission.observe_completion(observer.clone())
    │       把 observer handoff 给 caller-facing submission/handle
    │
    └── spawn_blocking(native read)
```

这里有三个重要不变量：

1. **claim 失败时不能留下 reservation。** 因为 native work 尚未开始。
2. **claim 成功后 reservation 安装不能再走普通失败路径。** Native dispatch 必须在 reservation 存在之后发生。
3. **三个 completion observer 位置不是三份状态。** 它们都指向同一个 `Arc<AtomicBool>`：reservation/command directory 用于 actor 侧观察，submission 用于 caller handle 侧观察。

### 3.4 Native dispatch result 与 BLE event 是两条输入

native backend 可能先返回 dispatch result，也可能 callback 先到：

```text
native dispatch result --------------------┐
                                           │
backend ingress event --------------------┤
                                           ▼
                              ReservationCoordinator
                              ├── resolve_dispatch
                              ├── gate_event / buffer
                              ├── record_event_evidence
                              ├── waiter_result
                              └── advance_and_retire
```

不要把 dispatch result 当成 BLE 成功。dispatch result 只回答：

```text
native 调用是否被 backend 接受/拒绝？
```

read 成功还需要：

- dispatch accepted；
- matching `CharacteristicRead` domain event；
- matching `CommandCompleted` terminal；
- connection/session/characteristic identity 均正确；
- reservation 允许 retirement。

---

## 4. `actor.rs` 的职责分层

不要把所有函数当成同一层级。可以按职责分成四组。

### 4.1 第一组：字段和 forwarding

先看 `BlengActor` 字段：

```rust
public_event_tx
lifecycle
waiters
resource_id_gen
reservations: ReservationCoordinator
```

然后看 `delegate!`。这些方法主要是给 `messages.rs` 提供 actor-facing admission API：

- `require_open`
- `require_initialized`
- scan availability/stop validation
- connection/GATT/subscription validation
- reservation install closure 使用的 coordinator API

阅读原则：

> delegate 只说明“所有者是谁”，不包含流程逻辑。

真正的跨域顺序不要在 delegate 中找，要看下面的协调方法。

### 4.2 第二组：提交与 native control

这一组包含：

- `register_operation_waiter`
- `launch_dispatch`
- `request_operation_cancellation`
- `dispatch_native_cancellation`
- `handle_cancellation_result`

要回答的问题：

- waiter 何时注册？
- command route 何时注册？
- predispatch cancel 的 CAS 在哪里？
- reservation 何时建立？
- native cancellation receipt 和原 operation result 为什么分开？
- cancellation rejection 后原 operation 如何继续？

`handle_cancellation_result` 的顺序必须保持：

```text
ReservationCoordinator.resolve_cancellation_and_release_public_events
    ↓
actor publish released public events
    ↓
accepted: waiters.cancel
rejected: clear cancellation + try_settle_waiter
    ↓
advance_and_retire_reservations
```

### 4.3 第三组：事件入口和 dispatch result

这一组包含：

- `handle_ingress_event`
- `handle_dispatch_result`
- `process_event`
- `settle_event_waiters`
- `try_settle_waiter`
- `advance_and_retire_reservations`
- `publish_event`

#### `handle_ingress_event`

职责是入口保护和调用 coordinator gate：

```text
lifecycle closed/faulted -> discard
foreign disconnect -> bypass dispatch gate, still process publication policy
normal event -> ReservationCoordinator.gate_event
    Some(event) -> process_event
    None       -> event retained for dispatch replay
    Err        -> fault lifecycle
```

foreign disconnect 的特殊路径不是重复逻辑。它表示一个 disconnect callback 不应进入另一个 command 的 dispatch buffer，也不应修改另一个 command 的 native lifecycle。

#### `handle_dispatch_result`

职责是把 dispatch result 接回 reservation：

```text
检查 cancellation 与 dispatch rejection 的竞争
    ↓
必要时完成 cancellation receipt
    ↓
ReservationCoordinator.resolve_dispatch
    ↓
启动已延迟的 native cancellation
    ↓
执行 DispatchAction
    ↓
普通 dispatch rejection 交付 waiter
    ↓
回放 reservation 缓冲的 events
    ↓
retire
```

重点：replay 不是重新走 gate。事件已经由 reservation 保存，dispatch resolution 之后直接进入正常 `process_event`。

#### `process_event`

`process_event` 是 actor 保留的核心流程，因为它同时连接三个所有者：

```text
ReservationCoordinator.allow_publication
    ↓
public_event_tx.try_send
    ↓
检查 lifecycle / foreign disconnect
    ↓
ReservationCoordinator.record_event_evidence
    ↓
settle_event_waiters
    ↓
advance_and_retire_reservations
```

它不是单纯的 event sender，也不是单纯的 reservation handler。

#### `try_settle_waiter`

这是最容易误读的方法。它不是“只要有 terminal 就立即把 handle 设为成功”，而是：

1. 检查 caller waiter 是否还存在；
2. 检查 cancellation 是否仍待决；
3. 如果 cancellation 被拒绝，先发送保留的 deferred result；
4. 从 coordinator 查询 authoritative `waiter_result`；
5. 确认 completion observer；
6. 根据 cancellation 状态选择：
   - 正常 `finish`；
   - `defer_result`，等待 cancellation receipt；
   - caller 已退休，则只记录 completion，不发送结果。

因此：

```text
waiter 完成 != reservation 退休
caller 已经 timeout != native 已取消
result 已经 ready != 可以从当前 connection state 重新计算 result
```

### 4.4 第四组：actor Drop

actor `Drop` 只处理：

- actor 在 runtime 仍 Open 时异常结束，触发 lifecycle fault；
- 关闭剩余 waiter，区分 `Closed` 与 `TransportFault`。

它不负责逐个释放 reservation。native cleanup 属于 lifecycle/cleanup coordinator，reservation 随 actor 状态销毁；不要把 actor Drop 误解成正常 BLE operation retirement。

---

## 5. `ReservationCoordinator` 是什么，不是什么

### 5.1 它是什么

它是 reservation 层的聚合协调器：

```text
CommandId
    ↓ CommandDirectory
CommandRoute
    ├── Initialization
    ├── ScanStart / ScanStop
    └── Connection { connection_id, kind }
            ↓
    selected reservation root
```

它把 command route 与三个 domain root 组合起来，提供 actor 需要的跨 root API：

- `register_command`
- `remove_commands`
- `attach_completion_observer`
- `request_cancellation`
- `resolve_cancellation_and_release_public_events`
- `take_cancellation_dispatch`
- `resolve_dispatch`
- `gate_event`
- `allow_publication`
- `record_event_evidence`
- `waiter_result`
- `advance_and_retire`

### 5.2 它不是什么

它不是：

- 第二个 actor；
- executor；
- mailbox；
- waiter registry；
- BLE native queue；
- public event broadcaster；
- 全局 tombstone registry；
- generic operation state machine。

它可以调用 root 的 domain API，但不替 root 解释 read value、GATT database、subscription actual mode 等领域事实。

### 5.3 为什么 command directory 放在这里

`CommandDirectory` 与 reservation roots 的生命周期一致：

```text
register command
    ↓
claim/reserve
    ↓
dispatch + callback reconciliation
    ↓
waiter settlement
    ↓
reservation retirement
    ↓
remove command route
```

把它放在 coordinator 内部的好处是：

- actor 不需要单独维护 route index；
- actor 不需要反复查 route 再把 route 传回 coordinator；
- route lookup、reservation root 和 command entry 的清理属于同一个内部不变量；
- `CommandRoute` 不泄漏到 actor 的多个 helper。

### 5.4 为什么 `CommandRoute` 仍然保留

`CommandId` 只是 opaque identity。没有 route，actor/coordinator 就只能按顺序尝试：

```text
initialization -> scan -> every connection -> every child operation
```

`CommandRoute` 让 coordinator 先定位 domain root。它不复制 reservation evidence，只保存：

- root category；
- connection ID；
- connection operation kind。

`ConnectionOperationKind` 也直接用于 connection 的 pre-ack pending event category，因此不再需要第二个重复的 `DispatchPendingRoute`。

---

## 6. `reservations/` 每个文件的作用

### 6.1 `reservations/mod.rs`

这个文件定义 reservation 层边界和 `ReservationCoordinator` API，重点理解：

- 哪些 root 被 coordinator 持有；
- command route 如何选择 root；
- event gate 的全局顺序；
- cancellation 如何按 route 转发；
- retirement 如何返回并清理 command IDs；
- waiter result 为什么只查询，不发送 waiter。

### 6.2 `dispatch.rs`

这是整个 reservations 的共享底座。先理解 `CommandProgress` 的字段：

```text
CommandProgress
├── command_id
├── completion: CompletionObserver
├── dispatch_accepted
├── buffered_events
├── terminal_result
└── cancellation: CancellationState
```

它记录的是“一个 command 的共同进度”，不是某个具体 BLE operation 的全部状态。

重点方法：

- `resolve_accepted_dispatch`
- `resolve_rejected_dispatch`
- `buffer`
- `terminal_result`
- `is_complete`
- cancellation request/claim/resolve/clear
- `ScopedPublication`

不要在这里寻找 read value、GATT session 或 subscription mode；这些属于上层 reservation。

### 6.3 `initialization.rs`

它是 adapter initialization 的状态机：

```text
NotStarted
    ↓ reserve
Pending(InitReservation)
    ├── dispatch accepted/rejected
    ├── correlated AdapterSnapshotChanged
    └── CommandCompleted
          ↓ settlement 后 advance
Ready / Rejected
```

review 重点：

- 只有 correlated snapshot 才能成为 initialization success evidence；
- passive snapshot 不等于 initialization result；
- dispatch rejection 后仍要识别 late contradictory evidence；
- waiter 先结算，再允许 Pending 变 Ready；
- initialization failure 的错误会由 actor 转换成 lifecycle fault。

### 6.4 `scan.rs`

它拥有唯一 scan slot：

```text
Idle
  ↓ reserve_scan_start
Reserved(ScanReservation)
  ├── session_id
  ├── start_progress + started_evidence
  ├── session_end_evidence
  ├── stop: Option<StopReservation>
  └── publication scope
```

关键 API：

- `require_scan_available`
- `validate_scan_stop`
- `reserve_scan_start`
- `resolve_dispatch`
- `request_cancellation`
- `allow_publication`
- `waiter_result`
- `retire_completed`

注意：

- scan start 和 scan stop 是两个 command，但共用一个 scan reservation root；这个 root 是整个会话，不是 start 命令记录；
- `session_end_evidence` 属于会话：没有显式 stop 时，系统暂停或原生扫描错误也能结束扫描；
- stop 不是简单地取消 start；
- failed stop 的 retirement 和 active scan 的恢复有特殊语义；
- scan publication scope 可能在 cancellation receipt 待决时保留事件。

### 6.5 `connections.rs`

这是 peripheral-keyed collection，先理解“集合”，不要把它当成单个 connection。

它负责：

- `PeripheralId -> ConnectionReservation`；
- root connect 的安装和移除；
- 通过 `ConnectionId` 定位 connection；
- 通过 `CommandId` 在 connection 集合中定位 command；
- 全局 operation category priority；
- 跨 connection 的事件 gate；
- 已完成 root/child 的批量 retirement。

它不负责单个 connection 内的 GATT/read/subscription 细节，那些交给 `connection.rs`。

### 6.6 `connection.rs`

这是 reservations 中最复杂的单文件，理解它需要同时掌握共享 `CommandProgress`、集合级 connection lookup 和单连接 domain evidence。

结构是：

```text
ConnectionReservation
├── root CommandProgress                    # connect
├── ScopedPublication
├── connection_id
├── connected_evidence
├── disconnected_evidence
├── connect_error_evidence
├── disconnect: Option<DisconnectReservation>
├── gatt: GattReservation
├── io: IoReservations
└── subscriptions: SubscriptionReservations
```

方法按职责分组，`new` 位于首位；其后依次是身份/命令查询、准入、claim 后安装、事件门禁与证据/派发结果、取消、结果与退休及内部 helper。关键方法：

- `pending_dispatch_target_mut`
- `try_buffer_pending_dispatch_event`
- admission/validation 方法
- `resolve_dispatch`
- `record_event`
- cancellation methods
- `waiter_result`
- `retire_completed_operations`
- connection root `is_retirable`

`pending_dispatch_target_mut` 的核心不是“事件属于哪个 UUID”，而是：

```text
在 dispatch 尚未 resolve 时，事件应该暂存在 connection 内哪个 CommandProgress？
```

缓冲和记录 evidence 时都要检查精确的 command/connection/session/characteristic/subscription identity。I/O 分支通过 `IoReservations` 的 `pending_rssi_progress_for_event_mut()`、`pending_read_progress_for_event_mut()`、`pending_write_progress_for_event_mut()` 取得待缓冲的 `&mut CommandProgress`，不获取叶子 reservation。普通 I/O 回调按 command ID 直接查 map，再验证完整身份；连接状态事件仍寻找相关未决操作，不能拿它携带的 command ID 代替受影响连接和原有类别优先级。

同一个 `IoReservations` 只属于一条 connection，无需在其中再建 connection 索引。terminal、取消、命令归属和进度点查统一经过 `progress_for_cmd()` / `progress_for_cmd_mut()`，复用已有 I/O command map；全量 progress 遍历只负责集合查询和生命周期核算。disconnect 的不可取消策略仍在点查前单独判断。

dispatch resolution 直接交给 `IoReservations::resolve_dispatch()`：它定位目标命令，接受时转交缓冲事件，拒绝时先分类并取出缓冲事件，再移除自己的目标 entry。connection 不再分别取得三类 I/O progress 并删除 map entry，只注入 `classify_rejected_child_event()` 规则。discovery 由 `GattReservation::resolve_dispatch()` 自行管理私有槽位；`DisconnectReservation::resolve_dispatch()` 处理自身证据，拒绝后由 connection 清除自己的 disconnect slot，两者沿用这一分类规则。connection 的 GATT 事件记录、缓冲目标选择和结果查询也通过 `GattReservation`，不再直接访问 discovery；只有 progress 引用提供给整连接的取消和生命周期核算。普通子命令拒绝仅回放 passive connection loss；subscription 仍单独保留允许 commanded connection loss 的更宽策略。

### 6.7 `gatt.rs`

它区分：

- committed GATT database：`require_characteristic(session_id, characteristic_id)` 在 owner 内查目标，不返回数据库；缺失 database/session/characteristic 仍为 `GattNotReady`，并保留目标错误先于后续 I/O/订阅冲突和 discovery busy 的顺序；
- pending discovery；
- rediscovery 对旧 database 的保留/替换；
- discovery 作为 subscription/GATT operation barrier。

重点是“旧 database 仍可存在”不等于“所有 native subscription route 都仍然有效”。不要把 database preservation 当作 subscription preservation。

### 6.8 `io/`

```text
io/
├── mod.rs      # IoReservations、一次性命令的三张 map、聚合路由/回收
├── rssi.rs     # RSSI 匹配、证据记录、结果与完成判断
├── read.rs     # read 匹配、证据记录、结果与完成判断
└── write.rs    # write 匹配、证据记录、结果与完成判断
```

先读 [`io/mod.rs`](../../../crates/bleng/src/runtime/reservations/io/mod.rs) 的集合边界，再按操作阅读 [`rssi.rs`](../../../crates/bleng/src/runtime/reservations/io/rssi.rs)、[`read.rs`](../../../crates/bleng/src/runtime/reservations/io/read.rs)、[`write.rs`](../../../crates/bleng/src/runtime/reservations/io/write.rs) 的局部证据。三个叶子模块私有，叶子类型与聚合层必需的方法/字段仅以 `pub(super)` 开放到父 `io` 模块，不向 connection 或 reservations facade 重导出；connection 只通过 `IoReservations` 获取命令进度。这是源码职责拆分，不引入 `IoOperation` trait、通用框架或 public ABI 变化。

review 时问：

- command rejection 是否只删除目标 operation？
- sibling read/write/RSSI 是否被错误清理？
- discovery/disconnect barrier 是否正确阻止 admission？
- `waiter_result` 是否只在 evidence 完整时返回？
- timeout/drop 是否会错误移除 native reservation？

共享的 1,024 操作上限由 `connection.rs` 定义并核算，包含这里的 RSSI/read/write 命令和订阅的 enable/disable transition；不包含稳定 Active route、discovery 或连接生命周期命令。`io` 是短期命令账本，不是整个 GATT 功能层，也不是原生执行队列。

### 6.9 `subscriptions.rs` / `subscription.rs`

先读 [`subscriptions.rs`](../../../crates/bleng/src/runtime/reservations/subscriptions.rs)：`SubscriptionReservations` 私有拥有 `HashMap<SubscriptionId, SubscriptionReservation>`，负责查找、占用查询、登记、progress inventory、缓冲目标选择、事件与派发结果分派、结果查询和退休。connection 只通过集合接口操作，不直接遍历、读取或删除订阅 entry；不新增索引或 trait，也不为封装对称机械改造叶字段或 phase。`NotificationStateChanged` / `NotificationReceived` 按已有 subscription ID 查 map，再验证 connection/session/characteristic、状态回调的命令归属和 dispatch 状态；连接 loss 仍按 scope 查找相关未决 transition，即使携带另一个 command ID，也不能改成只查该命令。

再读 [`subscription.rs`](../../../crates/bleng/src/runtime/reservations/subscription.rs)：单条订阅拥有 enable/disable phase、模式验证、`validate_disable()` 与 `resolve_dispatch()`。集合先定位 route，单条校验 session/activity 和 transition busy，connection 随后检查连接与兄弟操作，保留错误优先级。拒绝时单条先排空 buffer 再返回 resolution 与 `Keep` / `Remove`，集合立即应用；订阅仍允许 commanded connection loss，普通 I/O/GATT/disconnect 只允许 passive loss。

单条 notification route 的局部状态机：

```text
Enabling { transition }
    ↓ accepted + evidence
Active
    ↓ disable reservation
Disabling { transition }
    ↓ accepted + evidence
Active / removed
```

review 重点：

- enable rejection 如何移除 provisional route；
- disable rejection 如何恢复 Active；
- actual mode 如何来自 domain evidence；
- duplicate subscription route 如何拒绝；
- notification state/value/terminal 的顺序如何保留；
- 同一 characteristic 的 read 与 notification transition 如何互斥或并存。

---

## 7. 四个最重要的时序

### 8.1 普通 operation 成功

```text
Begin message
  -> waiter + command route registered
  -> claim
  -> reservation installed
  -> native dispatch accepted
  -> domain event recorded
  -> terminal recorded
  -> waiter_result becomes ready
  -> WaiterRegistry::finish
  -> advance/retire reservation
  -> remove command route
```

### 8.2 Native dispatch rejection

```text
native dispatch rejected
  -> reservation resolves rejection
  -> buffered events按 rejection policy replay/冲突
  -> waiter gets DispatchRejected
  -> no fake BLE success
  -> reservation/command entry only retire when owner permits
```

`DispatchRejected` 和 `Operation`/domain failure 不是同一个层级：前者是 native call admission failure，后者是 callback terminal/domain failure。

### 8.3 Explicit cancellation

```text
handle.cancel()
  -> actor finds command route
  -> reservation latches cancellation
  -> queued/pre-dispatch: local/native control claim
  -> native backend returns receipt
      ├── rejected: clear latch, preserve original operation result
      └── accepted: waiter becomes Cancelled
  -> late native evidence continues
  -> reservation retires only after evidence is complete
```

`cancel()` 返回或 cancellation receipt 成功，不等于 native side effect 已撤回，也不等于 reservation 已经退休。

### 8.4 Passive connection loss

```text
ConnectionStateChanged(Disconnected)
  -> event publication policy
  -> connection evidence records loss
  -> unfinished accepted sibling waiters fail as Disconnected
  -> explicit disconnect waiter may still need its own terminal
  -> completed result is not recomputed
  -> connection root retires only after all retained evidence is complete
```

这里最容易出现的错误是：看到 connection loss 就直接把所有 waiter 都改成 `Disconnected`。正确逻辑必须区分：

- 已经完整完成但尚未交付的 result；
- 尚未完成的 sibling operation；
- explicit disconnect 自己的 domain/terminal evidence；
- cancellation receipt 是否仍待决。

---

## 8. Review 时不要混淆的几个词

| 词 | 正确含义 | 不要理解成 |
|---|---|---|
| dispatch accepted | native 函数调用被接受 | BLE operation 已成功 |
| terminal event | backend 报告 operation terminal | domain success evidence 已完整 |
| waiter result ready | actor 可以生成 caller result | reservation 可以立即删除 |
| waiter finished | caller sender 已发送并移除 | native work 已取消 |
| cancellation accepted | backend 接受 cancellation control | native side effect 已撤销 |
| command route | 找到 reservation owner 的 metadata | BLE evidence 本身 |
| completion observer | actor 确认 operation 已完成的单调 latch | operation result 或 event |
| retirement | reservation/command 可以释放 | caller handle 一定已 drop |
| timeout | caller 等待预算耗尽 | native cancel |
| disconnect | connection lifecycle teardown | 对每个 operation 调 public cancel |
| cleanup | backend-wide lifecycle barrier | 某个 operation result |

---

## 9. 测试应该怎样对应源码

### `runtime/reservations/tests/`

这些测试直接验证 reservation domain 和 collection 不变量：

- `connections.rs`：peripheral root、跨 connection terminal 隔离、重连后的 stale terminal、GATT/I/O admission；
- `dispatch.rs`：`CommandProgress`、dispatch acceptance/rejection、buffer、cancellation phase；
- `subscription.rs`：subscription transition、enable/disable rejection、active route 恢复；
- `mod.rs`：test-only `ReservationCoordinator` accessor 的 privacy boundary。

### `runtime/tests/actor.rs` 与 `publication.rs`

这些测试验证 actor 跨层协调：

- waiter settlement；
- cancellation receipt 与原结果竞态；
- public event release；
- connection loss 对 sibling waiter 的影响；
- dispatch result 与 callback 顺序；
- mailbox/actor/lifecycle fault。

`publication_order_retired_sibling_keeps_fifo_without_restoring_command_route` 覆盖 B 无 cancel、先完成并退休 reservation/command route，但其事件仍留在 connection FIFO；A 的取消回执拒绝后，事件按原序恰好一次释放，不恢复 B route。原 dispatch-rejection + other-pending fixture 则先接受 B 的派发，再安装 pending A，符合 admission 约束。这些是现有测试源码导航，不是本次运行通过或最终 validation 结论。

### `runtime/tests/actor_support.rs`

这是 actor 私有 fixture support，不是第二套 production API。它可以构造：

- 已初始化 actor；
- blocked reservation；
- predispatch cancellation；
- late terminal/domain evidence；
- 受控 begin handler。

测试专用 root accessor 位于 `reservations/tests/mod.rs`，不在 production `ReservationCoordinator` impl 中。这样测试可以构造低层状态，但核心字段保持私有。

### 测试阅读方法

不要先看测试数量。先找到测试中的状态建立 helper，例如：

```text
reserve root
  -> resolve dispatch Ok
  -> record domain event
  -> record terminal
  -> query waiter_result / retirement
```

然后把它映射回生产流程：

```text
messages.rs admission
  -> actor.launch_dispatch
  -> backend callback
  -> ReservationCoordinator
  -> actor settlement
```

如果一个测试直接修改了某个字段，优先确认它是否已经被封装成 root method；不要根据测试 fixture 推断生产代码可以绕过 admission。

---

## 10. 以后修改时的安全检查清单

### 修改 admission

- 是否在 `try_claim` 前完成所有可失败检查？
- claim 失败是否没有安装 reservation？
- native dispatch 是否一定发生在 reservation 建立之后？
- dispatch rejection 是否只清理目标 command？

### 修改 event reconciliation

- dispatch result 与 domain event 是否仍然允许任意顺序？
- pre-ack callback 是否被正确 buffer？
- replay 是否避免重新 gate/重复 record？
- event publication 失败是否 fail closed？
- foreign disconnect 是否会误改其他 command 的 reservation？

### 修改 cancellation

- cancellation receipt 是否只是 control acceptance？
- rejection 是否保留原 operation evidence/result？
- accepted cancellation 是否仍等待 native terminal/recovery evidence？
- 同一 scoped publication 内的事件是否保持顺序？
- waiter cancel 是否误删 native reservation？

### 修改 retirement

- waiter 是否已经先 settlement？
- domain evidence、terminal、dispatch evidence 是否都完整？
- connection root 是否仍有 child/subscription/late callback 证据？
- command route 是否与 reservation 同时删除？
- 初始化失败是否触发 lifecycle fault？

### 修改 API 边界

- 是否把 `WaiterRegistry`、lifecycle 或 public event sender放进 reservation？
- 是否把 reservation root 字段暴露给 production sibling module？
- 是否为了测试放宽 production visibility？优先使用 `reservations/tests` 的 test-only impl。
- 是否引入第二个 command index、全局 tombstone registry 或 generic state machine？
- 是否只是为了减少几行代码而牺牲了一个清晰的流程命名？

---

## 11. 最后记忆版

如果只记住六句话：

1. **Waiter 管 caller delivery，reservation 管 BLE evidence。**
2. **CommandDirectory 只管 route metadata，不管 domain evidence。**
3. **CommandProgress 管一个 command 的共同 dispatch/terminal/cancellation 进度。**
4. **ReservationCoordinator 根据 command route 协调 initialization、scan、connections。**
5. **Actor 管跨域顺序、lifecycle、事件出口和 waiter settlement。**
6. **任何 timeout、drop、cancel、dispatch result、event、retirement 都不能被默认为同一个事实。**

按这个模型阅读，`actor.rs` 是流程编排，`reservations/mod.rs` 是跨 root 路由，`dispatch.rs` 是共同进度，`connection.rs` 是单连接领域状态，`connections.rs` 是 peripheral 集合，`subscriptions.rs` 是订阅集合，`gatt/io/subscription` 是局部领域证据。这样就不需要一次性理解整个 runtime，而可以沿着一个 command 的生命周期逐层展开。
