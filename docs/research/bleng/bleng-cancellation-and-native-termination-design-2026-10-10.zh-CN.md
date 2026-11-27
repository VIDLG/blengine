# BLeng Cancellation 与 Native Termination 设计

**状态：** 设计方向已确认；实现仍是部分完成，公共 ABI 尚未冻结。

## 范围

BLeng 对外提供 operation 级 cancellation，而 native backend 同时还需要 connection 级 disconnect 和 backend 全局 dispose。这三类动作可以共享底层 driver 基础设施，但不能共享同一套对外语义。

核心原则是：

> 共享 native termination 机制；分离 operation cancellation、connection disconnect 和 backend dispose 的策略。

## 为什么值得增加 cancellation

对于 characteristic read/write、GATT discovery、subscription transition 等可能长时间等待的 operation，cancellation 有实际价值。调用方可以停止一个 operation，而不必为了停止一个 read 就隐式断开整个 peripheral；当平台无法提供这种能力时，也可以得到精确的 `OperationNotCancellable`，而不是伪造成功。

不是每个平台、每种 operation 都必须支持 cancellation。backend 无法提供可靠 native control 时，必须同步返回 `OperationNotCancellable`。不能偷偷把“取消一个 operation”实现成 disconnect，除非调用方显式选择了 connection recovery 策略。

因此 BLeng handle 将两类所有权分开：

- 共享的 target identity 与 `OperationControl`，供 `cancel()` 使用；
- 一次性结果槽，供 `wait()` 使用。

调用方 timeout、task drop 或消费结果只会退休 caller delivery，不会隐式变成 native cancellation。

## 三类终止策略

| 动作 | 作用范围 | Native 目标 | 调用方结果 | 资源影响 |
|---|---|---|---|---|
| `cancel_operation` | 一个 operation | 一个 `CommandId` | `Cancelled` 或精确的 cancellation rejection | 尽可能保留 connection |
| `disconnect` | 一个 connection | connection 及其全部子 operation | disconnect 结果；子 operation 变为 `Disconnected` 或 `OperationAborted` | 使 GATT route 失效并释放 connection |
| `dispose` | 整个 backend/Hub | 所有 scan、connection、callback 和 handle | `Closed` 或 cleanup error | 关闭准入、排空 callback、释放全部 native root |

`disconnect` 和 `dispose` 内部可能会停止 native operation，但不应该通过逐个调用 public `cancel_operation` 来实现。它们的范围、排序、terminal error 和完成 barrier 都不同。

## 共享的 native 基础设施

每个平台应在适用的范围内，为每个 connection 提供一个串行 control boundary。该 boundary 拥有 native queue 和 callback correlation：

```text
ConnectionDriver
├── 有界 operation FIFO
├── 平台要求时的一个 active native operation
├── command 与 generation correlation
├── callback registration 与 late-callback barrier
├── exactly-once terminal settlement
├── native handle ownership
└── 内部 termination control
    ├── CancelOne(command_id)
    ├── Disconnect(disconnect_command)
    └── Shutdown(shutdown_reason)
```

共享基础设施负责：

- 将状态转换与 native submission 串行化；
- 区分 queued、active、completed、retired operation；
- 在需要时释放状态锁后调用 native cancel/close primitive；
- 防止 stale callback 完成新 generation；
- 在 teardown 期间排空或拒绝 queued work；
- exactly once 地释放 callback token、native handle 和 queue entry。

策略层负责：

- 哪些 operation 可取消；
- cancellation 是否保留 connection；
- 每个子 operation 对外暴露哪一种 terminal error；
- 是否要退休 connection 或整个 backend；
- 调用方应该等待哪一种 cleanup barrier。

## Cancellation 流程

```text
caller handle.cancel()
    -> BLeng OperationControl
    -> actor 对 target command 做线性化
    -> native driver 接受或拒绝 control
       ├─ 拒绝：清除 cancellation latch；原 operation 继续
       └─ 接受：调用方收到 cancellation receipt
                    -> target waiter 变为 Cancelled
                    -> native terminal/recovery evidence 仍继续协调
```

native cancellation receipt 只表示 driver 接受了 control request，不表示 peripheral 已经撤销 write、CCCD 修改或其他已经产生的副作用。原 reservation 仍要等待 native terminal 或 recovery evidence 后才能退休。

## Disconnect 流程

disconnect 是 connection 的生命周期转换，不是若干个独立 cancellation 的批处理：

```text
disconnect(connection)
    -> 线性化 connection teardown
    -> 停止/撤销子 route 与 queued work
    -> 请求 native disconnect / close
    -> 按 connection 语义产生子 operation terminal
    -> 产生 disconnect domain 与 terminal evidence
    -> 退休 connection generation
```

被 disconnect 终止的子 operation 不应该被重新标记为 caller cancellation。应区分：

- `Cancelled`：调用方赢得了 operation-level cancellation 竞态；
- `OperationAborted`：disconnect 或 recovery 连带终止了另一个已接受 operation；
- `Disconnected`：被动链路丢失或 native connection loss 终止；
- `Closed`：backend dispose 终止。

## Dispose 流程

dispose 是 backend-wide 且幂等的操作：

```text
dispose
    -> 关闭 command admission 与 ingress
    -> 在 backend state gate 内快照 native root
    -> 释放 state gate
    -> 停止 scan 并终止全部 connection driver
    -> cancel/close native operation 与 callback registration
    -> 等待必要的 callback barrier
    -> 发布一个被保留的 cleanup result
```

dispose 不应该为每个 command 等待一个 operation-level cancellation receipt。它拥有全局 cleanup barrier，只有在 backend 的 native root 和 callback registration 满足 cleanup contract 后才返回。

## 当前实现状态

当前工作区已验证：

- BLeng `OperationControl` 与一次性 read result slot 已分离；
- cancellation dispatch 的接受/拒绝通过 actor receipt 返回；
- actor 不会在 native control 被接受之前把 operation waiter 结算为 cancelled；
- Windows GATT I/O 已有 queued/active cancellation 初始切片，复用现有 per-connection FIFO 与 WinRT `IAsyncOperation::Cancel()`；
- Windows connect 阶段的 `BluetoothLEDevice` 与 GATT probe operation 已接入同一个 native cancel 入口；
- Windows dispose 已有独立的 backend-wide cleanup 路径。

仍未验证或未完成：

- native watchdog 与 connection recovery；
- Android 单 operation cancellation 语义；
- Apple 单 operation cancellation 语义；
- 跨平台 disconnect/cancellation/dispose 竞态 gates；
- 当前 macOS 主机对 Windows target 的编译与 RF 验证。

在这些边界独立测试通过之前，不能把任何平台结果描述为完整 native cancellation 证据。
