# BLeng 设计收敛：显式取消状态与生态复用评估

日期：2026-10-26

## 结论

用户授权在集中人工 review 前继续完善 BLeng 内部设计及对应测试，并要求评估更能降低竞态风险的架构和可复用生态。本轮对照 [Plan §16.1.9–10](../../../.plan/PLAN.md)、当前源码及 [初始化发布审计](2026-10-26-bleng-initialization-publication.md)，完成一项有限内部重构，没有换框架、改变公共语义或新增依赖。

- 保留现有 owner / actor / reservation / waiter 分层和 Tokio/Kameo 主线，没有证据支持整体重写或换 actor 库。
- 将取消状态从两个 bool + optional receipt sender 收敛为私有 enum，消除标记与回执所有权不一致的表示；内部状态不再靠读取 watch 的回执值推断。
- 先在旧实现建立有界协议测试 baseline，再在相同测试下重构；这是行为保持的结构改进，不伪称发现并修复了一个新的生产 bug。
- 相关整合中实测发现并修复了测试 fixture 的过早完成通知，不以重复运行一次通过掩盖同步缺口。

后续用户授权的 test-only Proptest 接入、有限重复测试清理及 native 取消目标分类修复见 [取消覆盖审计](2026-10-26-bleng-cancellation-coverage.md)。本报告的“不新增依赖”和计数属于本次状态重构候选，不覆盖后续候选。

本轮没有新增基本 BLE 功能、公开对象/方法、actor、queue、timer、结果通道或 routing registry。没有手机操作、ART/XCUITest/RF、权限/radio mutation、固件 provisioning、长稳或 commit/push/branch。

## 1. 为什么不直接更换架构

近期缺陷跨越的是不同事实：dispatch acceptance、native/domain completion、control receipt、caller result delivery、public event publication 和 resource retirement。把它们合成一个 `completed/cancelled` 标记，会再次丢失合法的中间状态；换 mailbox 库也不能代替这些 BLE 契约。

现有职责应保持：

| Owner | 唯一职责 | 不应接管 |
|---|---|---|
| RuntimeOwner / lifecycle | 执行器、handoff/deadline、关闭 gate、唯一 shutdown coordinator | canonical BLE 状态或每操作证据 |
| actor / reservations | 串行证据协调、精确关联、operation/domain 完成、native reservation 退役 | 阻塞 foreign 调用、调用方结果消费 |
| WaiterRegistry | 有界 caller delivery、待交付完整结果、一次发送 | 解析 native callback、管理连接 |
| OperationControl / ResultSlot | 可共享取消控制与 single-consumer 结果领取 | 第二个 native reservation 或第二条结果通道 |
| platform backend | native identity、I/O FIFO、watchdog、generation barrier、物理清理 | 应用自动重连、远端回滚保证 |

三类事件缓冲也不合并：pre-ack buffer 尚不能正常记录证据；scoped publication buffer 的证据已经处理、只等公开；public FIFO 等调用者消费。若统一为“所有确认齐了才处理”，会延迟 loss 和 sibling 结算；若入队立即记证据，又会破坏 dispatch rejection 的 fail-closed 边界。

reservation 已提供可直接确定性测试的同步转换。将全 engine 改写成新的通用 reducer/effect 协议，需要重新表达 dispatch、结果交付、publication 和 cleanup，目前没有可验证收益足以抵消迁移风险。此判断不是宣称任何其他架构永远不可用。

## 2. 实际改动：取消状态成为显式协议

修改 [dispatch.rs](../../../crates/bleng/src/runtime/reservations/dispatch.rs)：原 `cancellation_requested`、`cancellation_dispatched` 与 `Option<watch::Sender<...>>` 改为：

- `CancellationState::Idle`：没有请求，也没有 receipt owner。
- `CancellationState::Active`：唯一持有 request phase 与 receipt sender。
- phase 区分 Requested / Dispatched；接受或拒绝后继续保留是否已经 claim dispatch 的事实。它是内部协议，不是取消能力或远端停止状态。

这样不能再构造“requested 但没有 receipt sender”或“idle 却保留一个 control channel”的组合。watch 负责发布/订阅回执，actor-owned enum 负责协议判断；并发 watch reader 不成为第二个状态写入者。

**没有借重构改变既定行为：**

- 原 command dispatch ack 之前不 claim native cancellation；每请求周期只 claim 一次。
- 重复 request 共享当前回执；terminal 早于 receipt 不导致 reservation 提前退役。
- 拒绝后 clear 可以重新请求，旧 receiver 保留旧周期的回执，不串到新周期。
- local control resolution 可以先于 original dispatch rejection，不因此创建 native control submission。
- 重复 `resolve_cancellation()` 仍沿用原 `send_replace` 覆写语义，没有偷偷改成 first-wins 或改变 actor 的控制裁决。
- 初始化/断连的 cancelability、完成结果保持、publication FIFO、watchdog、timeout/drop 与 public ABI 均未改变。

**明确代价：**Active request 使用一个 Box，每个实际取消请求周期多一次分配，Idle 无该分配，状态转换不重复分配；clear/retirement 释放它与 sender。这避免扩大所有闲置 command 的布局及触发既有 large-enum lint，没有压制 lint。不是吞吐性能提升，也不宣称该分配无成本。

## 3. 测试不是靠“多跑几次碰概率”

[dispatch tests](../../../crates/bleng/src/runtime/reservations/tests/dispatch.rs) 新增三个测试：

1. 长度为 5、8 种方法动作的 **32,768 条有界序列**，用请求周期账本核对 dispatch claim、回执共享/隔离、terminal 首次证据及 retirement。失败信息包含具体序列和步骤。
2. clear 后旧回执保持、重新请求，以及重复 resolution 保持原 last-write 行为。
3. local resolution → original dispatch rejection → retirement，不发送 native control，completion observer 按原路径完成。

该枚举覆盖的是限定内部方法序列，包括用于防御性核对的非典型顺序；不是所有真实 async 调度、所有长度、native callback 或跨线程内存序的证明，也不是 long-soak。

实际命令：

```sh
pixi run cargo test -p bleng runtime::reservations::tests::dispatch
pixi run cargo test -p bleng runtime::actor::tests::publication
pixi run cargo test -p bleng runtime::owner::tests::cancellation
pixi run cargo test -p bleng runtime::reservations::tests
pixi run just test bleng
```

旧实现 baseline **8 passed**；最终 enum 同组 **8 passed**。相关 publication **16 passed**、owner cancellation **8 passed**、reservations **19 passed**；这些范围重叠，不重复累计。完整 BLeng **228 passed / 3 suites**。

日志：`.work/validation/2026-10-26-bleng-cancel-state-{baseline,green,regression}.log`。baseline 是绿灯，未追溯声称为缺陷 red。

### 实测发现的 fixture 屏障问题

首次完整回归中，`disconnect_rejects_while_sibling_rssi_dispatch_is_awaiting_dispatch_result` 在等待 `finished` 后看到命令数 1、预期 2。根因在 [support.rs](../../../crates/bleng/src/runtime/tests/support.rs)：`BlockingDispatchBackend::wait()` 在 inner submission 登记前就通知 finished。

修复仅在测试 helper：selected `read_rssi` / `set_notifications` 保存 inner 调用结果，返回前通知 finished，再原样返回 Result；Ok/Err 均通知，非 selected 调用不通知。没有 sleep、宽泛断言或生产行为修改。

**屏障证明边界：**selected inner submission 已返回、FakeBackend 命令已登记；不证明 actor 已处理 DispatchResolved，更不证明 BLE 操作完成。两个 connection 回归与一个 notification 回归以 8 test threads 有限重复 10 次，每次恰好 3 passed；之后完整 BLeng 通过。原失败保留在 cancellation-state regression 日志，后续日志为 `2026-10-26-bleng-dispatch-finished-{parallel,regression}.log`。

## 4. 生态复用：核查结果与当前选择

先核对实际依赖：`crates/bleng` 已复用 Tokio、tokio-util、Kameo、scopeguard 等；Kameo 固定 `0.22.2`、tokio-util 固定 `0.7.19`，具体来源见 [workspace manifest](../../../Cargo.toml) / [lockfile](../../../Cargo.lock)。没有因为网上存在另一种库就替换现有 owner 模型。

本轮通过 Context7 查询 Proptest 官方仓库/手册，并查阅 Loom 官方 rustdoc，未向查询发送源码、设备身份或凭据：

| 生态 | 官方能力与限制 | 对本项目的判断 |
|---|---|---|
| [Proptest 状态机测试](https://github.com/proptest-rs/proptest/blob/main/book/src/proptest/state-machine.md) | 生成转换序列、比较 reference model 与 SUT、失败时缩减序列；[失败持久化](https://github.com/proptest-rs/proptest/blob/main/book/src/proptest/failure-persistence.md) 可保存 seed 并优先重放 | 若后续复杂轨迹确实需要生成与 shrinking，适合小范围 test-only 接入；当前有限取消协议用现有确定性枚举已覆盖所选边界，暂不新增依赖或通用测试框架。它不自动证明 native callback 或 OS 调度。 |
| [Loom 0.7.2](https://docs.rs/loom/0.7.2/loom/) | 对使用 Loom 同步类型的并发模型探索调度/内存行为；未替换的 std/第三方同步对它不可见，模型有规模和内存序限制 | 更适合特定自有 atomic/lock 协议的最小模型，不是套住 Tokio/Kameo/CoreBluetooth/GATT 就能检查整个 BLE engine。没有为形式上的“全覆盖”改造全部同步类型。 |
| 当前 Tokio/Kameo | 提供既有 runtime、mailbox、oneshot、watch、计时与 task cancellation 基础设施 | 继续复用；CancellationToken 表示 task 取消，不能替代 native BLE cancellation/receipt、generation 或 watchdog。|

本轮不新增 production/test dependency；不声称使用 Proptest/Loom 跑过本项目测试。若以后引入 test-only 工具，应先有具体模型和能区分错误实现的断言，再锁定版本、限定预算与失败重放，不把研究建议变成新的强制门槛。

## 5. 整合结果与审计边界

| 实际命令 | 结果 |
|---|---|
| `pixi run just test all` | **330 passed / 18 suites**，包含 BLeng 228，不重复累计。 |
| `pixi run just apple consumer-bleng-macos` | 当前五 target/三 slice XCFramework、component symbols 与 artifact-only generated Swift consumer runtime passed；不等于 iPhone runtime/RF。 |
| `pixi run just fmt` / `pixi run just lint` / `pixi run just docs-check-mermaid` / `git --no-pager diff --check` | 通过。 |

整合日志前缀：`.work/validation/2026-10-26-bleng-design-convergence-{workspace,macos-consumer,fmt,lint,mermaid,diff}.log`。

本轮降低的是非法状态表示和测试同步误判风险，不能消除所有合法的外部事件竞争。仍未关闭的 native callback setup、真实 write/CCC radio/permission loss、same-Hub RF 和 iOS continuity，继续按 [平台 loss 审计](2026-10-26-platform-loss-functional-convergence.md) 保留。本轮没有发现需要新增基本 BLE 操作或更换整体架构的证据，但不将有限模型、旧设备证据或全绿计数当作集中 review/ABI freeze/Phase exit 的自动许可。
