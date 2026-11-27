# ABI freeze review packet（0.1 冻结前需要人工签署的 9 项）

状态：**准备中**。ABI 未冻结。本文件只列"需要人判断"的项，机械工作由我完成。

## 冻结意味着什么

冻结的是**三个对外层的形状**，不是实现：

| 层 | 冻结内容 | 位置 |
|---|---|---|
| Rust Stable UniFFI | 导出的函数/方法、record/enum/object/error 的字段与变体 | `crates/blehub/src`（6 export + 55 类型）、`crates/bleng/src`（12 + 18）、`crates/bleng-supervisor/src`（25 + 73），由 `crates/{blehub,bleng}-uniffi/src/lib.rs` 汇总成单镜像 |
| JS/Nitro | HybridObject 接口、方法签名、union 字面量 | `integrations/react-native-bleng/specs/*.nitro.ts`（6 个 spec）、`src/index.ts`（唯一入口） |
| 入口标识 | JS 可见的 HybridObject 名与 TurboModule 名 | `BlengCentral`、`BleHubBlengEngineLifecycle` |

**不冻结**（可随时改，不构成 ABI）：marker 字符串、evidence JSON schema、fixture contract/profile、`tools/` 与 recipe、任何 `*_ledger`。

冻结不等于发布，也不等于功能完成：它只保证"以后改这里必须走评审"。

## 已签署的决定（2026-11-24）

| 项 | 决定 |
|---|---|
| D1 | 按推荐：各语言保留本地惯用名（JS `close()` / Rust `shutdown()`） |
| D2 | 按推荐：按能力 pull（`scan.nextEvent` / `connection.awaitEnd` / `notifications.nextEvent`），不加 owner 级 fan-in |
| D3 | 按推荐：显式 `timeoutMs` 参数，不引入全局 deadline 对象 |
| D4 | 按推荐：冻结字段形状；`code` 只允许追加；`message` 仅展示 |
| D5 | 按推荐：缺省字段即该分支 |
| D6 | 按推荐：`BlengPlatform = string` |
| D7 | **改**：`begin*` + `wait` + `cancel` 仍是唯一提交原语；**允许在其上加一层可移植 async/await 便捷接口**，该层内部只调用这三个原语，不新增提交语义。promise-only / callback 风格仍不新增 |
| D8 | **改**：当前处于开发阶段，**不承诺版本兼容性**；0.x 期间允许直接破坏形状，重建产物即可，不设弃用期 |
| D9 | 按推荐：冻结包名与 `createBlengCentral()`；native 标识（bundle id / package 名）留到真正对外发布时再核对 |

**D7 已实现**：`integrations/react-native-bleng/src/pending.ts`（`pendingOperation` / `requireAccepted` /
`requireOutcome` / `AbortSignalLike` / `BlengOperationError` / `Operation` / `OperationSubmission` /
`PendingOperation`），契约见
[`pending-operation-layer.md`](../../research/react-native/pending-operation-layer.md)，纯 JS 守卫见
`pending.test.ts`。冻结清单需要包含这些 JS 签名。

D7 的 async/await 层是一条**派生**面，不是第二套操作模型：它只能由 `begin*` 得到的 receipt 驱动，
取消必须仍然是显式 `cancel()`（不允许“丢弃 Promise 就算取消”），并且它自己的错误必须仍是
`BlengFailureInfo`，不得新造错误词汇。实现顺序由用户指定：先清理测试架构，再加这一层；
加完它之后才做 0.1 冻结清单（冻结要包含这一层的新增签名）。

## 需要你签署的 9 项

每项给出**当前状态**、**你只需要回答的选择**、**推荐**。你可以只回"全部按推荐"，或逐条改。

### D1. 跨语言关闭命名

- 现状：JS 是 `close(): Promise<BlengCloseResult>`，Rust 是 `shutdown()`。
- 要决定：**保持不一致**（各语言用本地惯用名），还是统一成一个名字。
- 推荐：保持现状。理由：Rust 的 `shutdown` 与 UniFFI Kotlin wrapper 自带的同步 `close()` 会混淆，这一点 PLAN 已记录；JS 侧 `close` 是宿主生命周期语汇。

### D2. 事件读取器粒度

- 现状：没有 owner 级统一 `nextEvent`；每条 lane 自带 pull —— `scan.nextEvent()`、`connection.awaitEnd()`、`notifications.nextEvent()`。
- 要决定：**冻结为"按能力 pull"**，还是加一个 owner 级 fan-in 读取器。
- 推荐：按能力 pull。三条 lane 各自有界、各自有终止事件，fan-in 会把三个不同的有界队列合成一个无界选择器，是纯负担。

### D3. deadline 表达

- 现状：每个 `wait*` 都接收显式 `timeoutMs: number`，没有全局 deadline 对象。
- 要决定：冻结为**显式参数**，还是引入可复用的 deadline/cancellation token。
- 推荐：冻结为显式参数（与 Rust 侧"deadline 是调用方自己的界"一致）。

### D4. 错误 taxonomy

- 现状：`BlengFailureInfo { domain, code, message, platform? }`；`domain` 是 6 值封闭集（`input`/`submission`/`operation`/`observation`/`delivery`/`lifecycle`）；**`code: string` 是唯一允许做控制流的稳定字段**；`message` 仅展示。
- 要决定：是否把 `code` 的取值集合也纳入冻结（即"新增 code 需要评审"），还是只冻结字段形状。
- 推荐：冻结字段形状 + **code 集合只允许追加**。
- code 的来源已确认：native 桥只透传 Rust 的 `ForeignOperationFailure.code`（`crates/bleng-supervisor/src/ffi/failure.rs`）并额外产生一个桥级 `bridgeFailure`；两端 `BlengFailureMapping` 都不做二次命名。
- 当前可见值（示例，非完整清单）：`operation_not_found` / `operation_already_completed` / `operation_not_cancellable` / `operation_result_consumed` / `operation_aborted` / `invalid_configuration` / `invalid_request` / `invalid_state` / `invalid_timeout` / `not_found` / `not_supported` / `resource_busy` / `resource_exhausted` / `timeout` / `transport_fault` / `worker_stopped` / `adapter_unavailable` / `backend_unavailable` / `backend_assembly` / `backend_cleanup` / `ffi_contract` / `cancelled` / `closed` / `disconnected` / `protocol` / `permission` / `internal` + 桥级 `bridgeFailure`。冻结前我会用脚本从该映射生成完整清单并附在签名页。

### D5. 可选字段语义

- 现状：`BlengReceipt.failure?`（缺失=成功）、`BlengAdapterSnapshot.name?`、`BlengFailureInfo.platform?`。
- 要决定：是否冻结"缺省即该分支"的约定。
- 推荐：冻结。它避免了 `{ok: true}` 这种第二套状态位。

### D6. 平台名类型

- 现状：`BlengPlatform = string`（诊断数据，注释说明是为避开 NDK 的 `ANDROID` 宏与 Nitrogen 生成 enum 冲突）。
- 要决定：永久保持字符串，还是冻结成一个 union。
- 推荐：保持字符串。它是数据而非控制状态。

### D7. 操作模型

- 现状：统一为 `begin*()` 提交（同步返回 receipt）→ 独立 `wait(timeoutMs)` → 独立 `cancel()`。
- 要决定：冻结为**唯一**操作模型（不再增加 Promise-only 或 callback 风格 API）。
- 推荐：冻结。

### D8. 版本与弃用策略

- 要决定：0.1 冻结后，(a) 允许的兼容变更范围（仅追加 optional 字段 / 追加 union 成员 / 追加方法），(b) 破坏性变更是否需要 1.0 或走 minor 并保留弃用期。
- 推荐：0.x 期间只接受追加式变更；任何删除/改签名为 0.x minor + `CHANGELOG` 明示；1.0 才承诺 SemVer 弃用窗口。

### D9. 公开包边界

- 现状：包 `@blengine/react-native-bleng` 仍 `private: true`、`version 0.0.0`，消费外部 staged 原生产物。
- 要决定：ABI freeze 时是否**同时**冻结包名、入口函数 `createBlengCentral()`、两个 native 标识名。
- 推荐：冻结包名与入口函数；native 标识（bundle id / package 名）留到真正对外发布时再核对，避免为一个未发布的包提前改名。

## 我会自动完成的部分（不需要你参与）

1. **已有且已验证**：`api-snapshots/{rust,kotlin,swift}.txt`、`bleng-*.txt`、`supervisor-*.txt` 共 9 份 reviewed 快照，由 `pixi run just binding snapshot-check`（3 份）与 `just binding bleng-snapshot-check`（6 份 + 32 个单测）守护；本轮实测均通过。所以"导出面漂移"已经有硬门禁，不需要新造。
2. 生成**人可读冻结清单**（逐符号 + 字段 + union 成员 + D4 的 code 全集），作为签名附件——快照是机器格式，签名需要可读版。
3. 复跑 artifact-only consumer（Android `dev.blehub:*`、Apple staged XCFramework）确认冻结面在产物侧一致。
4. 产出**同 candidate 双端矩阵**报告（见下）。

## 冻结门禁（必须全部满足才 freeze）

| 门禁 | 现状 |
|---|---|
| iOS 真机 managed 场景 | **已通过**：2026-11-24 同一候选上 12/12（含 `background`），bundle `integrity=passed`/`host_gate=pending`，见 [窗口报告](../../testing/reports/2026-11-24-background-dwell-window.md) |
| Android 同 candidate 复跑 | **已通过**：同一候选、同一天的两台 DK 并行 12/12，见同一报告 |
| artifact-only consumer 双端 | 历史通过；**冻结前仍需在同一 candidate 上复跑** |
| 冻结清单 + 守护测试 | 未做（我负责） |
| 文档：用户向 API 文档 | 未做（我负责） |
| 9 项决定签署 | 未签（本文） |

## 你的一句话怎么回

最简单的形式：

```text
D1-D7 按推荐；D8 按推荐但弃用期写 6 个月；D9 同意冻结包名与入口。
```

或只挑要改的，例如：

```text
D2 我想加 owner 级 nextEvent；其余按推荐。
```
