# B 类首个场景：`passive-disconnect` 的实测阻塞

日期：2026-11-20（阻塞测量），2026-11-21（§5 引擎增量与场景改写）。范围：新 lane 的 B 类第一项
`passive-disconnect`（旧 `rf-passive-disconnect`）：host 端断链、App 端只作证。本文记录**已实现的内容**、
**一次真机测量**、**必须补的引擎能力**以及**补齐后的形状**。它是候选事实，不改变任何 gate。

## 1. 已完成并离线验证

- `DirectFixture.drop_peer()`：从 peer 侧结束链路，地址取固件 `GET_LIMITS` 的 live peer（不是本地缓存），
  返回前必须看到控制器自己报告 no-peer；没有 peer、会话不活跃都会在发任何 mutation 之前拒绝。
  离线 4 项测试（`tests/peripherals/nrf52840/test_peer_drop.py`），合成固件现在也模型化 GAP disconnect。
- `run_bleng_ui.exercise_passive_disconnect()` + `PeerDropSeam` + `exercise_owned_passive_disconnect` +
  scene 定义 + 两端 recipe。
- App 侧同类缺陷的修复：`link=disconnected` 之后所有需要链路的命令都必须不可用
  （read/write/discover/rssi/subscribe/pull/disconnect，只在 `link === 'connected'` 时提供），
  且成功 disconnect 会退掉连接/拓扑/订阅 capability。Jest 回归：`withdraws every link command once
  the link has ended`。这一条测出来的真问题是：以前断链后按钮仍然可点，只能失败。
- 场景自身的缺陷模型（离线）：死链上渲染出值、把 peer 的结束当 run error 报、静默窗口内又声称
  `connected`、`disconnected` 后仍提供 I/O —— 四种都有回归守着。

离线全绿：`tests/peripherals/nrf52840` 549、`tools/tests/react_native` 2199、Jest 112。

## 2. 真机测量（Android，APK `76c7427889a4…`，run `12bef431`）

场景走到 `link:lost` 就停了：peer 被 fixture 断开后，**操作员自己的 Disconnect 没有把 `link` 变成
`disconnected`**，而是把整轮 900 s 预算耗尽。

原因是结构性的，不是本场景的写法：

- 公开 RN/BLeng 接口里**没有任何连接状态事件**。`BlengSessionOwner` 只有 `start/beginScan/close`，
  `BlengConnection` 只有 `beginDisconnect/beginDiscoverGatt/beginReadRssi`，只有 scan 与 notification
  两条 lane 有 `nextEvent`。
- 于是 App 对被动的链路结束**只能从某条命令失败里间接得知**；而 `beginDisconnect()` 在链路已经消失时
  只能失败（或干脆不结算），App 于是继续发布 `link=connected` —— 这正是它唯一能诚实地做的。
- 结论：`passive-disconnect` 要求的「App 证实 `link-state=disconnected` + 静默」**当前不可达**，
  缺的是一条连接状态观测通道，而不是场景编排。

## 3. 需要补的最小能力

在 `BlengConnection` 上加一条被动结束观测（与 notification 的 `nextEvent` 同构，不引入新对象）：

```ts
/** 等一次链路结束；对端发起的结束不是错误。 */
awaitEnd(timeoutMs: number): Promise<BlengConnectionEnd>

export type BlengConnectionEndReason = 'operator' | 'peer' | 'ownerClosed' | 'failed'
export interface BlengConnectionEnd {
  reason: BlengConnectionEndReason
  failure?: BlengFailureInfo
}
```

- Rust supervisor：把已有的 `ConnectionStateChanged`/`Disconnected` 事件投影成一个单消费者 waiter
  （与 read/notification waiter 同一套 owner 语义），`operator`/`peer` 由 backend 的断连来源决定。
- 两端 bridge：Android 用 `onConnectionStateChange` 的 status/cause、Apple 用
  `didDisconnectPeripheral(error:)`，映射成同一组 reason。
- App：连接建立后开始等；`peer` → `link=disconnected`（无 error），`operator` → 已经由自己的命令处理，
  `failed` → `link=disconnected` + failure 身份；同时观察边界继续用完整 identity。
- 场景：`link:disconnected` 由这个通道驱动，不再依赖操作员的 Disconnect；静默窗口仍然保留。

这是一次公开 API 增量（UniFFI spec + 两个 bridge + API snapshot + App），按 PLAN「ABI 未冻结」允许，
但它属于需要单独审查的一步，不应混在场景迁移里默默做掉。

## 4. 当前状态

- 场景与 host seam 已就位，离线全绿；**真机不通过**，且失败信息现在会在 ~20 s 内点名缺失的通道
  （`_require_link_settled`），不再吃掉整轮预算。
- `react-native-internal` 的 `rf-passive-disconnect` 因此**仍未迁移**：B 类第一项卡在引擎能力上。
- 下一个动作二选一：
  (A) 先做 §3 的 `awaitEnd` 增量，再让本场景自然通过；
  (B) 先把 B 类其余项（`recovery`、`multi`、`pending-read-reload`、`reload`）的可达性各自评估一遍，
      把「缺能力」的清单一次列全，再决定增量顺序。

## 5. 已选 (A)：`awaitEnd` 增量与场景改写（2026-11-21）

§3 的形状按理想架构收紧后实现，改动落在拥有它的仓库里：

- `crates/bleng-supervisor/src/connection_delivery.rs`：一条连接自己的结束 lane。它在 connect 提交时
  与提交同锁安装，只绑定**自己那条 connect 命令**产生的连接，只接受该连接的终止；**第一个终止就是最终
  终止**，被保留而非一次性消费（晚到的观察者仍能读到）。`Connections` 是一个集合而非单一 `current`
  槽位，因为同一 owner 的多条链路共存；`revoke()` 与既有两条 lane 一起关闭它。
- 归因是能力自己的事实，不是对 peer 的猜测：本能力提交的 disconnect 产生了终止 → `operator`，其余一律
  `unrequested`；平台错误原样携带，缺失不等于「peer 干净关闭」。`owner` 表示**观察**结束，不代表链路已断。
- `awaitEnd(timeoutMs)` → `{ ended } | { failure }`：deadline 是调用方自己的界，超时返回观察失败而终止
  仍然保留；同一连接最多一个 pending pull。Rust 侧新增 `ForeignConnectionEnd{By,Observation}` 与
  `SupervisedConnection::await_end`，bleng 侧新增共用的 `OperationAdapter::observe_with_timeout`
  （scan/enable/connect 三处重复实现收敛为一个）。
- App（`useManualScan.ts`）：连接成功后在自己那条 lane 上观察；超时重新挂上，非超时失败停止观察。
  `publish` 成为 `link` 的唯一写者并拒绝让已结束的链路复活，`settleEnd` 是唯一的「结束」写入点，
  接收回执与 lane 终止走同一条路。run-status 增加 `linkEnd` 标量。
- 场景（`exercise_passive_disconnect`）不再提交任何命令去发现结束：断链后只等 App 自己宣布
  `link=disconnected` + `linkEnd=unrequested`，要求归因不是 `operator`、静默窗口内三元组不变、需要链路的
  命令全部退场，并检查 App 自己的 receipt 账本里**没有** `gatt-read-wait: failed`（即没有回到旧路径）。
- 契约与快照同步：`tests/consumers/README.md` 增加 `linkEnd` 与「连接结束 lane」一节；
  `api-snapshots/{bleng,supervisor}-*` 与 `tools/test_api_snapshots.py` 的产品契约表同步更新。

离线全绿（本轮）：`tools/tests/react_native` 2200 passed / 21 skipped、Jest 116、
`cargo test -p bleng -p bleng-supervisor -p blehub --all-features`、`pixi run just fmt` / `lint` /
`binding::bleng-snapshot-check`。真机口径见 `docs/testing/reports/` 中本轮的后续记录；本文件不预判。
