# Awaiting one BLeng operation：便捷层的边界

本文说明 `@blengine/react-native-bleng` 里那层 async/await 便捷接口（`src/pending.ts`）为什么存在、
它被允许做什么、以及它明确不做哪些事。提交契约本身以 Nitro spec 为准，本文只解释分层与命名。

## 结论

1. **提交模型不变**：`begin*()` 同步返回 receipt（接受能力或失败），`wait(timeoutMs)` 与 `cancel()`
   仍是独立的两步。这层只把"接受 → 等待 → 处理失败"包成可组合的派生形状。
2. **取消永远是显式的**：丢弃 `result` promise 不构成取消，也不会被悄悄升级成取消。只有
   `pending.cancel()`（或绑定到它的 `AbortSignal`）会提交取消；返回的 receipt 原样交还，取消请求
   的 receipt 不是回滚证明。
3. **失败不重新命名**：被拒的提交与结算后的失败都抛 `BlengOperationError`，内部持有原生
   `BlengFailureInfo`；`code`（配合闭合的 `domain`）是唯一可做控制流的字段，`message` 只用于展示。
4. **两条 lane 不覆盖**：`beginScan()` / `beginSubscription()` 用自己的控制名
   （`waitUntilStarted` / `cancelStart`、`waitUntilEnabled`）外加一条不是 operation 的流
   （`nextEvent`），这层不把它们改写成统一的 `wait`/`cancel`。调用点写显式适配器，让人一眼看出
   自己在哪条 lane 上。

## 两个维度，三个 helper

便捷层不是"三层互相竞争的包装"，而是"输入形状 × 想要什么"：

| | 同步拆包 | 异步结算 |
|---|---|---|
| **提交**（`begin*`） | `requireAccepted(submission)` → 能力 | `pendingOperation(submission, timeoutMs, signal?)` → `{ result, cancel }` |
| **已结算结果**（`wait` / `initialize`） | `requireOutcome(result)` → outcome 臂 | —（已结算） |

- `pendingOperation`：operation 路径。拆提交、**立即**启动有界等待、保留 cancel 句柄，并接受可选
  `AbortSignal`。它是打包好的便捷形式。
- `requireAccepted`：显式拆包。两条 lane（`beginScan` / `beginSubscription`）**必须**用它；对
  operation 也合法（之后自己调能力的 `wait()`/`cancel()`），只是 `pendingOperation` 更省事。
- `requireOutcome`：已结算的两臂结果联合（`initialize()`，或任何 operation 的 `wait` 值）。想分支
  而不抛错时直接用 `isBlengFailure`。

命名对齐 spec 词汇：提交的成功臂叫 `*Accepted`，结果的成功臂叫 `*Outcome`。旧的
`requireInitialized` 是 initialize 专属，和通用的 `requireAccepted` 看起来像一对兄弟、实际不在
同一维度；现在统一为 `requireOutcome`，成为"结果侧"的通用拆包。

**唯一的 one-liner 是 `run(submission, timeoutMs, signal?)`**，它就是
`pendingOperation(...).result`：一次有界等待，拿到 outcome，失败则抛。它刻意**不**暴露 cancel
句柄——要取消就用 `pendingOperation`（或传 signal）。也刻意**没有** per-capability 镜像
（`connect()` / `read()` / `write()` / …）：

- 那会把原生 `begin*` 面在 JS 里复述一遍，两套名字随 spec 漂移；
- 每个镜像都要决定"要不要吞掉 failure-as-data"，等于给同一操作第二套错误语义；
- 真正想省的是"accept → wait"这两步，`run` 一行就够，剩下全是逐操作改名。

`run` 只接受带 `wait`/`cancel` 的 operation 提交：两条 lane 没有 `wait`，`SettledOf` 对 lane
提交推出 `never`，所以拿不到成功臂；运行时 `pendingOperation` 也会以 `TypeError` 拒绝非 operation。

一个**刻意的非对称**：`requireAccepted` 会把唯一能力抽出来（提交在类型上强制"恰好一个能力"）；
`requireOutcome` 只返回 outcome 臂，不自动抽字段——因为回执合法地就是 `{}`，且 outcome 以后可能
增加字段，自动抽会脆弱。因此 `requireOutcome(await central.initialize())` 得到 `{ snapshot }`。

## 为什么不在 `begin*()` 上挂 `.pending()` / `.wait()`

`begin*()` 返回的是**数据联合**（`{ connect } | { failure }`，Nitro 生成的记录），不是带方法的
对象。让它能链式调用只有两条路：

- 改原生 ABI，让 `begin*` 返回一个带方法的 HybridObject —— 那方法就无法把失败表达为数据（要么抛，
  要么另开 `.failure` 字段）；
- 在 JS 里包一层 class —— 这层要么在构造时抛（丢掉 failure-as-data），要么又暴露 `.failure`
  （就是"联合 + 多一层"）。

而**能力本身已经能链**：拆完联合后，`requireAccepted(beginConnect()).wait(ms)` / `.cancel()` 就是
原生方法。所以"链式"只在能力上有意义，在联合上加会造出平行对象并藏掉失败语义。

## 返回形状：`{ result, cancel }`，不是裸 payload

`pendingOperation` 返回 `{ result, cancel }`，`result` 结算成该 operation 的 **outcome 或 receipt**：

- `{ connection }` / `{ value }` 这类命名 outcome；
- 写、MTU、adapter control 这类结算成 `BlengReceipt`，成功形态就是 `{}`（"没有 failure 就是成功"）。

它刻意不是裸 payload：

- **cancel 必须是头等控制**。要"既返回裸值又保留 cancel"，只能把 Promise 变异出 `.cancel()`
  （promise 带副作用），或把 cancel 丢掉；两种都会让"取消是请求、不是回滚"变得不显眼。
- **不能自动抽 outcome 的唯一键**：receipt 是零键 `{}`，outcome 以后可能多字段，自动抽会制造
  `TypeError` 或锁死演进。

## AbortSignal

`pendingOperation` 的可选第三个参数是结构化的 `AbortSignalLike`（真实的
`new AbortController().signal` 可直接传入，包不依赖 DOM lib 类型）。语义：

- `signal.abort()` → 提交该 operation 的原生 `cancel()`；它是**请求**，不是回滚。
- **不伪造本地拒绝**：`result` 仍按原生结果结算；原生取消通常以 `BlengOperationError` reject。
- 传入时若已 `aborted` → 立即提交 `cancel()`（`begin*` 已经提交了操作）。
- operation 结算后自动 `removeEventListener`，避免长生命周期 signal 持有监听。
- `cancel()` 与 signal 走**同一条取消路径**，不是两套取消。

## 时序与预算

- 预算由调用方声明并传给原生 `wait(timeoutMs)`；这层**不额外加 JS 定时器**，否则会出现两个
  互相打架的 deadline。
- 被拒的提交在 `requireAccepted` / `pendingOperation` 里**同步抛出**：它是同步事实，返回一个
  "注定立刻 reject 的 promise"只会制造未处理拒绝的风险。
- 结算失败在 `result` 上 reject，`await` 与 `try/catch` 是同一条代码路径。

## 类型不替代原生侧的运行时兜底

这层把成功臂从联合里剥出来（`Exclude<TValue, BlengFailure>`），但**原生边界仍必须保留运行时检查**：
一个类型上是 `{ connection }` 的答案，如果实际没有 `connection`，不能因为"类型说它有"就当成已连接。
宿主 App 的 connect / discover 两处就保留了这个检查并注明了理由——这是原生边界，不是类型能证明的地方。

## 狗粮（这是它不被冻结成"无人使用的公开面"的理由）

宿主 App（`tests/consumers/react-native-bleng/src/useManualScan.ts`）已经按这条**规则**全量采用：

| 提交形状 | 用法 | 原因 |
|---|---|---|
| connect / disconnect / discover / read / write / rssi / notify-disable | `pendingOperation(...)` | 一次有界等待 + 独立 cancel，正好是这层的形状 |
| 同上，但不需要 cancel（例如示例屏的 `screen.ts`） | `run(...)` | 一次有界等待 + 抛失败，就是 `pendingOperation(...).result` |
| `beginScan()` / `beginSubscription()` | 显式适配器 + `requireAccepted` | 提交返回的是 **lane**（`scan` / `notifications`）而不是一次操作，控制名也不一样 |
| `scan.beginStop()` | 保持手写 | App 允许在 `invalid_timeout` 后**再次**等待同一个 stop（输入级超时不是终态），单个 Promise 无法表达 |

也就是说，"用不用这层"不是口味，而是**提交是否只有一个有界等待**。read 的取消队列保留在 App：
队列项现在存的是 `pending` 而不是原生 capability，`cancel()` 走 `pending.cancel()` —— 取消请求仍然是
用户按下的那个按钮，永远不会因为丢弃 `result` 而发生。

## 守卫

`tests/consumers/react-native-bleng/src/pending.test.ts`（纯 JS，不触原生，全部通过）：

- 接受 → 按声明预算 `wait` 一次，`result` 给出成功臂；
- 被拒提交同步抛错，且**不提交任何 wait**；
- 原生 failure 对象原样携带（`failure` 引用相等，`code`/`domain`/`message` 对得上）；
- 结算失败 → reject `BlengOperationError`；
- **丢弃 `result` 不会调用 `cancel`**；
- 显式 `cancel()` 恰好提交一次，receipt 原样返回（失败 receipt 也不改写）；
- 两个 pending 互不影响（各自的 wait/cancel 不串台）；
- 非 `read` 形状（`write`）同样可用；
- lane 命名控制走显式适配器；
- `AbortSignal`：abort 只提交 `cancel()`、`result` 仍按原生结算；已 aborted 立即提交；结算后解绑监听；
- 非法预算 `RangeError`；提交不携带恰好一个能力 → `TypeError`。
- `run`：成功即 resolution；结算失败与提交被拒都 reject `BlengOperationError`；lane 提交在类型层
  不可用。

## 与冻结的关系

D7 的决定是：`begin*`/`wait`/`cancel` 仍是唯一提交原语，**允许**在其上加这一层派生接口。因此
0.1 ABI 冻结时，`pendingOperation`、`run`、`requireAccepted`、`requireOutcome`、`AbortSignalLike`、
`BlengOperationError`、`Operation`、`OperationSubmission`、`PendingOperation` 会作为 JS 面的新增
签名进入冻结清单；它们不引入新的提交语义，也不改变任何 Rust/UniFFI 形状。
