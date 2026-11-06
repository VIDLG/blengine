# BLeng Reservation 与 Native Per-Connection Queue 的对齐审查

**日期：** 2026-10-11
**状态：** BLeng 存储迁移与有界并发 admission 已实现；跨平台验证仍待完成

## 审查结论

native operation queue 的第一阶段已经完成，暴露出当前 BLeng reservation 模型与 native 能力之间的差异。

维护中的 Android 与 Apple backend 现在都使用每个 connection 独立的 GATT FIFO：同一 connection 上按队列串行执行，不同 connection 可以并行推进。native queue 负责排队、队头保护、queued cancellation、active operation 不可取消、watchdog 回收以及晚 callback 拒绝。

`ConnectionReservation` 现在通过按 command key 索引的 `IoReservations` 保存 read、write 和 RSSI entry。BLeng admission 已允许已接受的 read/write/RSSI sibling 以及不同 route 的 subscription transition 并存，并使用与维护中的 native queue 相同的每 connection 有界容量。pre-dispatch handoff 安全、disconnect/discovery barrier、重复 subscription route，以及同 characteristic 的 read/subscription 保护仍然是显式策略检查。

存储与 admission 对齐已经实现，并由确定性 Rust 测试覆盖。跨平台 native queue gate、Android/Apple artifact gate 和真实 runtime 证据仍需针对同一个 candidate 重跑，完成前不能宣称端到端设计全部通过。

## 当前所有权边界

- `ConnectionReservation` 拥有一个逻辑连接生命周期、GATT database/session 有效性、subscription route 和 child operation 证据。
- native backend 拥有真正的 per-connection execution FIFO 与 native handle。
- BLeng reservation 拥有 caller completion evidence 和 command correlation，不应复制 native execution state。
- `CommandProgress` 继续作为每个 logical command 共享的 dispatch/terminal/cancellation 状态。

## 推荐目标设计

在 connection 内引入按 `CommandId` 索引的操作集合，暂定名为 `IoReservations`，统一保存 read、write 和 RSSI child reservation。

```text
ConnectionReservation
├── root connect progress
├── disconnect operation             # 独占 teardown authority
├── GATT discovery operation         # 独占 database replacement barrier
├── IoReservations
│   ├── read: CommandId -> ReadReservation
│   ├── write: CommandId -> WriteReservation
│   └── rssi: CommandId -> RssiReservation
└── subscriptions: SubscriptionId -> SubscriptionReservation
```

`IoReservations` 应提供 `ConnectionReservation` 所需的共同操作：

- 按唯一 `CommandId` 插入 reservation；
- 为 dispatch buffering 查找 reservation；
- 按精确 command/domain correlation 记录 event；
- 处理 dispatch acceptance/rejection；
- 枚举 `CommandProgress`，服务于生命周期记账和 cancellation；
- 独立回收已完成 entry；
- 在不暴露内部 map 的情况下报告是否存在 pending operation。

各个 operation-specific reservation 继续拥有 domain evidence 和 waiter-result 转换；集合只负责存储、查找、遍历和共同生命周期 plumbing。

## Admission 策略

当前已实现的 BLeng 策略为：

- 一个已连接 connection 上可以同时 admission read、write 和 RSSI；
- 多个 read/write/RSSI 可以排队，但必须有与 backend capacity 对齐的 BLeng 有界 admission；
- 该 connection 上的实际执行顺序由 native backend FIFO 决定；
- 不同 connection 保持相互独立的并行推进；
- disconnect 仍是独占 teardown barrier，拒绝新的 child admission；
- GATT discovery 仍是 invalidation barrier，不能与 pending/active GATT work 竞争；
- subscription transition 在 CCCD 证据完成前仍是 route-specific barrier；
- 同一个 active subscription 的重复 route 仍然拒绝；
- 同 characteristic 的 read/subscription 策略必须明确写出，不能继续隐式继承旧的全局 `has_characteristic_io()` 判断。

backend 同步返回 `ResourceExhausted` 仍然属于 dispatch rejection。BLeng 只能移除被拒绝的那个 reservation，不能破坏同一 connection 上其他 queued reservation。

## Cancellation 与 timeout 要求

每个 queued child reservation 都有自己的 `CommandProgress` 和 cancellation result，因此：

- queued cancellation 只移除或回收目标 command；
- active cancellation 在 backend 无法取消 native head 时返回精确的 `OperationNotCancellable`；
- timeout 只回收 caller delivery，在 native evidence 或 recovery 回收之前保留 reservation；
- 一个 command 的晚 evidence 不能 settlement 其 sibling command；
- disconnect/dispose 回收完整 native lane，同时让 BLeng 对所有受影响 reservation exactly once settlement，并区分 local/native error。

## 实施顺序

1. **已完成：** 新增 private `IoReservations` storage abstraction；
2. **已完成：** 将 routing、dispatch resolution、cancellation lookup、accepted-command 记账、waiter lookup 和 retirement 从三个 `Option` 字段迁移过去；
3. **已完成：** 保留并扩展确定性测试，没有放宽 production visibility；
4. **已完成：** 在保留生命周期 barrier 的同时，使 BLeng admission 支持有界并发 read/write/RSSI；
5. **进行中：** 完成同一 connection FIFO admission、sibling completion、queued cancellation、active cancellation rejection、timeout 加迟到完成、dispatch rejection 隔离、subscription transition 容量和 disconnect retirement 的 deterministic fake-backend 覆盖；
6. **待完成：** 使用同一 candidate 重跑 Android JVM、Apple deterministic、Rust workspace 和 staged artifact 门禁。

不需要改变 public ABI。这是 BLeng 内部 reservation/storage 与 admission policy 的变化，不应把 native queue internals 暴露到 `blehub` 或 RN low-level module。

## 剩余验证

BLeng 的存储与有界 admission 现在已经与维护中的 native per-connection queue 对齐。剩余工作是验证而不是再次决定 admission policy：完成 sibling/FIFO/cancellation 的 deterministic 矩阵，并针对同一个 candidate 重跑 Android、Apple、Rust 和 staged artifact gate。native queue internals 仍由各平台 backend 私有拥有；BLeng 不宣称物理 RF 层面的并行传输。
