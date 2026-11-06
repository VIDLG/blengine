# BLeng Recovery 架构设计

状态：`.plan/PLAN.md` 下的已批准 recovery 子设计；主计划仍是唯一的阶段与架构权威来源。

日期：2026-10-31

## 设计决策

Recovery 分为四层：

```text
crates/bleng-supervisor/src/recovery.rs
    纯 recovery policy、状态机和证据校验

crates/bleng-supervisor
    Rust session/recovery orchestration
    持有 recovery policy、Bleng owner、context/generation 和 plan fence

PlatformSessionFactory / native adapter
    Apple/Android backend 创建、dispose、resource snapshot 和平台状态

Bleng
    单代 operation runtime、backend assembly、event reader 和 shutdown
```

Recovery policy 不创建 native manager、不拥有 `Bleng`、不操作 Settings、不管理手机 lease，也不启动 Peripheral。Rust supervisor 根据 policy action 编排 owner/session；native adapter 只提供实际平台 backend 和 retirement 能力。

## 命名

### `bleng-supervisor::recovery`

`bleng-supervisor/src/recovery.rs` 是 supervisor 内部的纯 recovery policy 模块。它负责 phase、signal、identity、terminal、link 和 plan fencing；它不是 BLE backend，也不创建 native manager、`Bleng` 或 platform session。

它与 supervisor 放在同一个 crate，是为了让 recovery policy 和 owner orchestration 共享一个明确的 Rust 边界，同时保持 policy 代码不依赖 CoreBluetooth、Android BLE API、React Native 或具体 native implementation。

### `bleng-supervisor`

`bleng-supervisor` 是 Rust 中负责跨 owner/session 转换的生命周期协调层。它在同一个 crate 内组合纯 recovery policy、`Bleng` owner、native session factory 和 platform retirement；它不依赖 CoreBluetooth、Android BLE API、React Native 或具体 native implementation。

配置也按职责分层：`SupervisorConfig.recovery` 是完整的 `RecoveryPolicyConfig`，包含初始 context、旧 link、旧 operation terminal barrier 和 recovery fallback 开关；supervisor 配置只保留由 host/provider 注入的可选 `RecoveryTarget`。resource-zero 名称不再由 owner 或 FFI 配置重复传入，而是由 `PlatformOwnerSessionFactory` 提供 platform-owned resource contract，Rust supervisor 在构造时校验非空、非空白集合，避免不同 native owner 的资源词汇漂移。生命周期-only owner 可以不提供 target；只有真正执行 same-owner/fresh-owner BLE recovery action 时才要求 target 存在，缺失就在 Rust 边界 fail closed。baseline 建立晚于 supervisor 构造时，可以通过 `configure_old_operation_barrier(old_link, expected_terminals)` 一次性补入；如果构造配置没有 old link 且 terminal 集合为空，第一次 `RadioLost` 前必须显式调用它，不能用空集合隐式跳过旧 operation terminal barrier。该 mutation 只在 `Active` phase 接受，进入 recovery 后冻结。FFI 使用同构的 `ForeignSupervisorConfig.recovery`，只在边界上把字符串/vector 转换为 Rust 的强类型。这样 policy 参数不会一部分嵌套、一部分散落在 supervisor 顶层，也避免 FFI adapter 自己调用 `RecoveryPolicy::new`。

`RecoveryPolicy` 构造后只保留运行期状态和必要的策略开关，不保存整份初始化 config：初始 context、旧 link 和 expected terminal 集合会被拆入对应状态集合，避免重复保存同一份配置。这里不额外引入 builder 或 typestate：配置已经是单个 typed record，真正的运行期 phase 仍由 policy 状态机和 context/sequence fence 共同校验。

它负责：

- 持有当前 `Bleng` owner/session；
- 驱动 `RecoveryPolicy`；
- 登记并校验 `RecoveryPlan` 的 context、sequence 和 action；
- 执行 owner-boundary action（retirement、fresh-owner assembly），并把结果重新提交给 policy；
- 等待旧 owner shutdown 和 native retirement；
- 要求 resource-zero 后才能创建 fresh owner；
- 调用 `PlatformSessionFactory` 创建下一代 backend/session。

正式 `react-native-bleng` 已由 Rust supervisor 组合 native adapter；旧 Swift/Kotlin supervisor 只保留在尚未迁移的历史 RN/HIL lane 中，不能成为 production Rust supervisor 的依赖。它们应在对应 lane 完成迁移并取得独立证据后再删除，不能反向进入新的 Rust ownership path。`tests/hil` 中的 Supervisor 继续是 HIL 专用实现。

R1 已建立 `crates/bleng-supervisor` 的 Rust ownership seam：fake `PlatformSessionFactory` 可以创建一代 session，supervisor 将 backend 一次性装配到新的 `Bleng`，由 `Bleng` 负责已装配 backend 的 dispose，owner shutdown 后等待 event reader drain，再读取并校验 platform resource snapshot；未装配失败使用独立的 `dispose_unassembled` 路径，避免 double-dispose。这个顺序复用现有 `BleBackend::bind_event_ingress`、`BleHub` backend ownership 和 Apple/Android event-reader retirement 语义。R2 已增加 optional `ffi` feature 并通过编译/clippy，验证 object-safe foreign factory/session、`Arc<dyn BleBackend>`、`Arc<Bleng>`、async event-reader drain 和 async resource snapshot 的 UniFFI 机制；该 component 现已由 `bleng-uniffi` 组装并通过 Rust image check。R3 已生成 staged Swift/Kotlin bindings，Apple `ApplePlatformSession` 和 Android `AndroidPlatformSession`/factory 均已通过各自的静态编译 seam；Rust action loop、old-terminal barrier、same-owner/fresh-owner boundary、target validation、sequence fencing 和 resource-zero cleanup 已由 fake session 与 FFI tests 覆盖；Android 三组件 AAR（`blehub`、`bleng`、`bleng-supervisor`）已通过单一 native image、AAR 类表和 artifact-only consumer build；Apple Nitro bridge 也已在 Pixi 管理的 `apple-ci` 环境中通过 simulator compile。上述证据仍不等于 native runtime、RF、完整 Apple deterministic projection/runtime tests、公共 Nitro contract 或 ABI freeze 通过；历史 Swift/Kotlin supervisor 不属于新的 Rust ownership path，reviewed API snapshots 也不因这次生成而自动更新。

## 各层职责

### `crates/bleng-supervisor/src/recovery.rs`

负责纯 deterministic policy：

- recovery phase 转换；
- same-owner、fresh-owner、fresh-process mode；
- old operation terminal barrier；
- old/new session identity 校验；
- stale signal 和 duplicate signal 拒绝；
- connection/GATT/subscription link 按对应身份分量逐项校验不复用，不能仅比较完整三元组；
- 有界 retry policy；
- platform-timeout 和 manual-reconnect outcome；
- recovery evidence schema 与 verdict 分类。

`RecoveringSameOwner` 内部区分 same-owner attempt、Central pending 与 retirement pending；每个
子状态只接受对应工作的结果，拒绝重复或错配 signal 时不修改 policy/plan sequence。公开 phase
保持稳定，不以宽泛 phase 检查替代实际 action 的准入约束。

它不得依赖 Tokio、CoreBluetooth、Android BLE API、React Native、Settings、nRF provider 或 phone lease。它只包含 deterministic tests，不直接暴露 Stable UniFFI；UniFFI 只通过 supervisor 的 FFI facade 暴露经过边界转换的类型。

### Typed operation submission：native context fence

业务 operation 通过 `SessionSupervisor::begin_*` / UniFFI `Supervisor::begin*` 提交；不从
Supervisor 获取 raw `Bleng` 再自行派发。十类入口复用已有 BLeng typed handle：scan start/stop、
connect/disconnect、GATT discovery、read/write、notification enable/disable、RSSI。初始化仍由
`start()` 负责，不新增重复初始化入口。

一次提交的顺序为：

```text
native 请求携带产生其输入的完整 context
→ FFI 校验 context 格式并 try_lock
→ 确认存在实际 active owner
→ 完整 context 同时匹配 platform owner 与 recovery policy
→ phase 必须为 Active 或 Recovered
→ 调用原 BLeng begin_*，返回原 typed operation handle
```

校验与提交处于同一段同步临界区；不能先读取当前 owner，释放锁，再向其提交。FFI 不等待正在
执行的 lifecycle/observation 锁：竞争或 lifecycle 已取走 supervisor 时返回 `SupervisorBusy`，
poison 返回 `SupervisorUnavailable`。这不是等待 actor admission、BLE outcome 或 shutdown 的入口。

异步 lifecycle 调用由独立 in-flight guard 持有借出的 supervisor；只有仍有该 guard 的操作才是
Busy。future drop/unwind 后进入不可恢复的 Interrupted/Unavailable，不恢复部分执行的 owner，
也不永久伪装成 Busy。若逻辑 Closed 已确认，保留带中断失败的 close receipt；否则只报告
Unavailable，不推断 ownerClosed、cleanup 完成或 resource-zero。关闭/恢复中断后不能在原 facade
上自动重试装配；底层 BLeng 已发起的清理与调用方等待寿命仍独立。

`SupervisorOperationError` 区分未启动/已关闭、stale context、recovery 未就绪、格式错误和内部
不可用；BLeng submission error 保留为嵌套的 typed `BlengError`，不降格成 platform 字符串。
拒绝发生在 BLeng 分配 command ID 之前。native 必须传入原请求的 context，不能遇到 stale 后
自动换成当前 context 重试；context/operation ID 仍不暴露到 JS。

返回的 handle 不持有 Supervisor 的锁，也不被重新包装成另一套 waiter：`wait`、deadline、
单次结果领取、独立 `cancel` 和 shutdown precedence 仍由 BLeng 定义。旧 handle 始终针对原
owner，不会在 fresh owner 建立后重新路由。same-owner recovery 保留该 owner 的 context，
恢复完成后可继续提交；fresh-owner recovery 更换 context，旧 context 不再合法。recovery 过程
中的平台内部恢复链仍使用当前 action 所授权的 owner，不通过拒绝业务提交的入口反向阻塞自身。

这是 native-only 的未冻结转发候选，不新增 JS operation/event/recovery contract；已有
`start_event_reader(owner, scan_projection)` 是 platform adapter 内部的 reader capability，不是给 JS 的 raw getter。
Rust 测试覆盖 context/phase 与 handle 生命周期；Apple/Android artifact consumers 分别验证其
生成绑定。构建成功、macOS fake-backend 执行和手机 runtime/RF 证据仍须分开记录。

### Scan event projection：固定来源，而非第二个 reader

扫描事件的内部转交使用 [`SessionScanProjection`](../../crates/bleng-supervisor/src/scan.rs)；
UniFFI 对应 [`ScanEventProjection`](../../crates/bleng-supervisor/src/ffi/scan.rs)。每个 assembled
owner 都有独立 endpoint，它由 Rust 在启动 reader 前创建，并与同一个 `Bleng` 一起交给
`start_event_reader(owner, scan_projection)`：

```text
一个 owner 的 Bleng public FIFO
→ 原来的一个 Swift/Kotlin reader
→ 同步 scan_projection.project(event)
→ 可选 scanEventSink(带完整 source_context 的 envelope)
→ 原 raw eventSink / Apple recovery inbox 继续按原路径处理
```

Rust 只投影 `ScanStarted`、`AdvertisementObserved`、`ScanStopped`，不消费或重建
`CommandCompleted`，不把事件当作 operation result。canonical advertisement 的所有字段，
包括可缺失值、manufacturer/service bytes，以及 passive/error stop 结果均保留。source context
固定为创建 endpoint 时的 owner/backend/generation/attempt；不查询“当前 active owner”重贴标签。
它是受信任 native reader 的来源约束，不是对任意 native 调用者传入的 `BleEvent` 做来源认证。

`ScanProjectionLease` 是 Rust 内部不可 clone 的 publication capability。正常 retirement 在
owner shutdown / reader 结束之前 revoke；启动失败在 cleanup 前 revoke；启动 future 被取消、
unwind 或 owner 被 drop 时，lease Drop 同样 revoke。保留 endpoint 的 native 引用不能重新
激活它。取消/panic 后 revoke **不证明**异步 cleanup、reader drain 或 resource-zero 已完成。

publication 以原子 revoked flag 的读取为线性化点：已经读取到有效状态的调用仍可能在 revoke
之后返回。已返回的 envelope 不能撤回，因此异步排队的下游仍须比较完整 `source_context`；
只比较数字 scan/peripheral ID 不够。endpoint 不持有 Supervisor lifecycle mutex，不增加
queue、runtime、registry、过滤器、第二套 waiter 或第二个 `next_event` reader。

**关闭与证据观察不能混淆：** operation `wait` 完成不是 public reader 已观察到 terminal 的屏障。
当前 BLeng `next_event` 在 lifecycle closed 时返回结束，不保证排空已有 FIFO。因此 native
consumer 在 shutdown 前显式、有界等待所需真实 terminal；随后仍核对完整事件、错误与资源结果，
不能用固定 sleep 或弱化断言代替观察。

### Apple recovery advertisement：按需、scan-scoped capture

Apple reader 不再为整个 owner 生命周期积压 advertisement。`recoverTarget` 在提交 recovery
scan **之前**调用 `captureAdvertisements()`，由调用方持有一次性的 capture；inbox 只保存弱引用。
scan 的 typed wait 返回后，capture 按准确 `scanSessionId` 和原 name/service/company 条件选择
观测，避免 callback-before-wait 丢失和将旧 scan 的相同过滤条件误认成新 scan 成功。

成功选中、timeout、cancel、显式 close 或启动失败都会退出 capture；scope `defer` 覆盖 scan
提交/等待失败，弱引用避免 inbox 延长订阅寿命。无 capture 时根本没有 recovery advertisement
订阅或积压，这不是有消费者时的 lossy backpressure。独立的 source-bound scan sink 和原 raw
sink 仍由同一个 reader 收到事件。取消保持 `CancellationError`，不被包装为 recovery failure；
取消请求、capture 退出和尝试停止 scan 都不能被解释为远端 rollback 或已验证 native cleanup。

capture 仍采用原默认容量 64，且 overflow 锁存失败，不能从已有 matching buffer 返回假成功。
**active overflow 仍是 owner-fatal fail-closed：** `offer` 向共享 reader 抛错，reader 结束并关闭
整个 inbox；不能把 inbox 局部测试中捕获异常后仍能读取 terminal/radio，解释为生产路径也隔离
了 overflow。timeout/cancel 的局部退订不改变这条安全边界。绑定 scan ID 前或消费过滤前到达的
旧 scan 观测仍可能占用本 capture 的有限容量；准确 ID 防止错认成功，不保证旧流量绝不触发失败。

terminal/radio 继续保留原 owner-scoped、各容量 64 的历史证据和显式 overflow failure，以保留
等待开始前已到达的旧 operation terminal / radio transition。本次未将它们改成按需流、扩容或
静默丢弃；普通大量 operation/radio event 下的 history 消费责任仍需收紧。新的 capture 修复
不等于普通持续扫描、正式 JS scan 或平台 recovery gate 已通过。

无设备 Swift 回归入口为 `pixi run just apple bleng-recovery-test`：在既有 HIL project 中运行
standalone macOS XCTest bundle，复用同一批测试及 staged BLeng image，不启动 App、backend、
模拟器或 radio。runner 使用本次唯一 xcresult，并要求有实际测试、全部通过、无 skipped/
expected-failure 后才输出 Passed。它不是 iPhone、CoreBluetooth 或 RF 证据。正式 RN native
consumer 另有注入 fake backend 的 reader 接线测试；其 iOS build 与 runtime 结果仍应分开记录。

JS scan object、listener、stop/cancel、terminal delivery 与背压契约仍需评审；本节的内部接线
和 capture 生命周期不等于批准或完成这些 surface。

### Platform session adapter

Platform adapter 负责可执行的 native 能力，但不拥有跨平台 recovery policy：

- 创建 Apple/Android backend；
- 实现 native dispose 和 callback ingress retirement；
- 提供 native resource snapshot；
- 声明 resource-zero 名称契约，由 Rust supervisor 统一校验快照；
- 提供 platform identity/capability；
- 执行平台特有的 Central/GATT teardown；
- 通过 `PlatformSessionFactory` 将 backend/lease 交给 Rust supervisor。

Rust supervisor 不直接创建 Swift/Kotlin object，而是调用 platform factory。adapter 不得自行创建第二个 Rust runtime，也不得绕过 supervisor 重新 assemble 已关闭的 `Bleng`。fresh-owner recovery 必须创建新的 `Bleng` 实例和新的 backend 实例。

### Minimal native surface

Native platform 层只保留无法安全跨平台抽象的部分，不追求“少写代码”而牺牲 ownership 或 callback 正确性：

```text
必须留在 Apple/Android native adapter：
    CoreBluetooth / Android BLE API 调用
    platform queue / Handler 线程归属
    native callback 到 exact object/generation 的 correlation
    per-connection native FIFO、watchdog 和 active/queued retirement
    native disconnect/close/unsubscribe/central teardown
    ingress root 的 native 持有与释放
    platform identity、permission/radio observation
    native resource snapshot
    one bounded platform recovery action → one RecoverySignal

统一收到 Rust supervisor：
    RecoveryPolicy 和 RecoveryObservation
    RecoveryTarget（ScanOptions、service/characteristic UUID、notification mode、expected value）
    owner/backend/session replacement 顺序
    old operation terminal barrier
    context/generation/attempt fence
    RecoveryPlan context/sequence fence
    Bleng 创建、assembly、shutdown 编排
    resource-zero 验证和跨平台错误分类
    fresh-owner/fresh-process 的共同结果
```

`BleBackend`、`BackendPort` 和 `Bleng::assemble_backend()` 已经是现有的最小交互边界，不再为 recovery 复制第二套 command/event contract。`SessionSupervisor::execute_platform_action()` 只从当前 fenced plan 取一个平台 action，连同 plan sequence 交给 `PlatformSession`；adapter 必须原样回显 sequence。Rust 校验 sequence 后，才把返回 signal 绑定到该 session context 并重新提交给 `RecoveryPolicy`，因此旧 action 的晚到结果不会推进新 transition。`RecreateCentral`、Android GATT reset 等 action 由 Rust policy 决定是否允许，但实际 native 操作仍由 platform adapter 执行并回报结果；Rust 不直接调用 CoreBluetooth 或 Android BLE API。`RetireOldOwner` 和 `CreateFreshOwner` 不经过 native action seam，由 Rust supervisor 独占执行。当前平台能力也必须保持诚实：Apple 的 `RecreateCentral` 只能调用 backend 已有的资源闲置检查型 diagnostic action；Android 没有安全的程序化 Bluetooth Central reset，因此返回 `CentralRecreationFailed`，不能合成 `CentralRecreated`。Apple/Android 的 same-owner action 已沿用同一条 typed operation 链执行 scan、connect、GATT discovery、subscription 和 expected-value readback；这些代码路径的静态编译不等于真实设备 RF recovery 已通过。

### Native recovery runner

Recovery runner 是 native host/provider 的一次性、有界 orchestration，不是新的 policy。它只驱动 Rust 已授权的 action，不解释或重写 `RecoveryPlan`：

```text
observe_signal(RadioLost)
→ execute_platform_action()
→ observe_signal(returned signal)
→ repeat platform actions
→ retire_for_recovery()
→ create_fresh_owner(new native identity)
→ execute fresh-owner actions
→ terminal recovery outcome
```

runner 必须满足：

- 每个 plan/action 只 dispatch 一次；
- 使用 Rust `observe_signal` 绑定当前 active context，不本地缓存 owner/generation；
- 不对 platform error、timeout 或 stale sequence 自动重试；
- `RetireOldOwner`、`CreateFreshOwner` 只调用对应 Rust owner-boundary API；
- 失败保留为 `platform-timeout`、`manual-reconnect-required` 或明确 recovery failure，不合成 success；
- 不进入 Nitro/JS public API，JS 只能看到经批准的最终 projection。

当前 Apple/Android owner 已提供 native-only external signal、platform action、retirement、fresh-owner 和一次性 old-operation barrier 配置 seam；外部输入只允许 `RadioLost`/显式 manual reconnect，`RadioOn`、旧 operation terminal、same/fresh-owner result 等 platform progress 只能从 Rust action loop 回传。`OwnerStart` 返回真实初始化 command/snapshot，`RecoveryPlan` 返回 terminal outcome，Rust runner 不再从 callback 猜测启动或结果。Apple HIL 的 system-loss/fresh-owner lifecycle 已切到 Rust-owned owner；剩余工作是补齐 Apple deterministic projection/runtime tests、无 RF 的 native reader/radio timeout/overflow tests，再由 host/provider 在 baseline 后注入 profile-derived target 和真实旧 operation terminal 配置，最后才申请真实平台 RF candidate。

### `Bleng`

`Bleng` 继续作为 operation runtime。现有一次性 assembly 和 shutdown 语义保持不变：

```text
Bleng::new
→ assemble_backend once
→ initialize
→ operations/events
→ shutdown
```

`SessionSupervisor::start()` 把 `initialize` 纳入 owner assembly barrier：event reader 必须先启动，以便接收初始化结果；只有 initialize 成功后，owner 才对 recovery action 可用。初始化失败与 assembly 失败使用同一套 shutdown、reader drain 和 resource-zero cleanup。

Adapter snapshot 的 command correlation 也属于 contract：带 `command_id` 的 `AdapterSnapshotChanged` 是 initialize operation 的 typed result，不能进入 recovery radio evidence；只有不带 command correlation 的后续 adapter transition 才能产生 `RadioOn`。这条规则防止第一次 recovery 消费旧的初始化 `PoweredOn` snapshot。

本设计不向 Stable BLeng facade 增加 `replace_backend`、`reopen` 或隐式 recovery API。

### Platform backend

`AppleBleBackend` 和 `AndroidBleBackend` 继续作为 transport/resource owner。

它们可以提供 private diagnostic SPI，例如 native resource snapshot 或有界 Central recreation 实验，但不拥有 recovery policy，也不创建第二个 `Bleng` owner。

### Nitro/RN

Nitro 的 HybridObject 业务实现直接采用平台语言：iOS 用 Swift，Android 用 Kotlin；不定义手写的 C++ BLeng/UniFFI 业务接口。Nitrogen 生成的 C++/JSI/JNI glue 仍然是 Nitro 框架基础设施，但不承载 BLE、recovery 或 UniFFI ownership 逻辑：

```text
React Native JS
    → Nitro generated JSI/Promise glue
    → Swift HybridObject / Kotlin HybridObject
    → UniFFI generated Swift/Kotlin bindings
    → Rust bleng-supervisor
    → PlatformSessionFactory
    → Apple/Android backend
```

Swift/Kotlin HybridObject 只负责：

- JS-facing lifecycle method projection；
- Promise/coroutine/Task completion；
- native Supervisor construction and serialization；
- PlatformSession adapter ownership；
- public session-level state/event projection。

它们不持有 recovery policy、BLE operation state 或第二套 owner lifecycle。Android 的手写 C++ 仅保留最小 JNI loader；不得把 UniFFI C ABI、CoreBluetooth 或 Android BLE orchestration 放入 Nitro C++ 层。

公共 RN/Nitro contract 只暴露 Android 和 iOS 都能表达的共同策略与结果：

```text
active
radio-lost
waiting-for-radio-on
recovering-same-owner
creating-fresh-owner
recovering-fresh-owner
recovered
platform-timeout
manual-reconnect-required
```

以下能力只允许作为平台内部 diagnostic，不进入公共 Nitro recovery API：

```text
CBCentralManager recreation
BluetoothGatt-specific reset
CoreBluetooth restore-state callback
platform-specific process relaunch
```

如果某个平台不支持某一种 recovery mode，Rust supervisor 必须把它转换为共同的 `platform-timeout` 或 `manual-reconnect-required`，不能让 JS 依赖平台特有 mode。Same-owner/fresh-owner action 没有 `RecoveryTarget` 时必须拒绝执行；不能使用旧 native handle、猜测 GATT UUID 或合成 value evidence。

React view 的 mount/unmount 不得创建、退休或替换 BLE session。正式 consumer 的
Tests / Current run / Evidence 只切换展示；owner、pending action 和本次观察结果位于其上。
UI 的 `close: succeeded` 只表示该调用成功返回，不能替代 host 的 resource-zero 或 RF verdict。

Engine invalidation 必须独立接线，不能借 UI navigation 或 JS GC 代替。当前
[锁定的 Nitro 0.37.1](../../integrations/react-native-bleng/package.json) 本地源码中，
Swift/Kotlin `HybridObject.dispose()` 和 C++ `disposeRaw()` 是 JS 显式调用的清理钩子，
不是每次 runtime invalidation 都会触发的通知；默认实现也不调用 Supervisor shutdown。
因此“实现 dispose”或“对象可能析构”不证明旧 owner、reader 与 backend 已退休。
正式 engine-lifetime seam 及实际 Hermes reload 的旧 owner 退休证据仍是删除 internal
前的独立条件，不能因正式工作台或生命周期软件测试通过而关闭。

### Host

Host tooling 负责 phone lease、Settings/Appium/XCTest、provider、relay 和最终 evidence verdict。Host 向 App 发送已观察到的 restoration/authorization signal，但不创建或销毁 `Bleng` 对象。

## 文档分类

Recovery 相关文档按职责分层：

```text
.plan/PLAN.md
    唯一 approved phase/architecture plan

docs/architecture/bleng-recovery.md
    稳定的 recovery 架构、职责、契约和退出条件

docs/research/
    竞品比较、候选方案、平台行为研究和未决探索

docs/testing/reports/
    具体设备、candidate、run、失败和通过证据
```

本文件是规范性架构文档，不是第二个独立总计划。新的 phase、ABI 或产品范围仍必须回到 `.plan/PLAN.md`。

## Lifecycle fencing 与可复用基础库

Recovery 使用的是业内常见的 stale-work fencing 模式，但这里的 fence 不是 CPU memory fence，也不是锁；它是异步生命周期的“门票”校验：事件或 action 必须携带创建时的 owner、generation、attempt 和 sequence，才能推进当前状态。

```text
source_context == active_context
    旧 owner 的 callback 才能进入当前 owner 生命周期

owner_generation > previous_generation
    fresh owner 必须属于更新的一代

recovery_attempt == previous_attempt
    fresh owner 仍属于同一次 recovery workflow

plan sequence == returned sequence
    旧 platform action 的结果不能覆盖新 plan

old operation barrier closed
    RecoveringSameOwner 之后的旧 terminal 不能重新启动 recovery
```

这些规则对应几个通用生态概念：

- `OwnerGeneration` 是 epoch/generation token；
- `RecoveryAttemptId` 和 `RecoveryOperationId` 是 workflow/request correlation ID；
- `RecoveryContext` 是结构化 lifecycle fencing token；
- `RecoveryPlan.sequence` 是 plan/action fencing token；
- `LateOldOperationTerminal` 是 stale event rejection。

### 可复用的基础库

优先复用基础库，但不要把业务 fencing 规则外包给通用库：

- `uuid` 或 `ulid`：适合生成 opaque owner/backend identity；
- `tokio::sync::oneshot`：适合异步结果回传；
- `tokio-util::sync::CancellationToken`：适合请求取消；
- `tokio::sync::watch`：适合广播最新状态；
- tracing correlation fields：适合记录 attempt/operation/sequence。

这些工具不能替代 context/epoch/sequence 校验。取消 token 不能保证 native callback 不再到达；`oneshot` 也不能阻止迟到结果；`crossbeam-epoch` 是内存回收机制，不是 BLE lifecycle fencing。当前 policy 因而使用小型 typed value objects，而不引入一个假设业务语义的通用 fencing framework。

### 本项目的边界

```text
通用库
    identity、取消、异步传输、日志 correlation

bleng-supervisor::recovery
    owner/backend identity、generation、attempt、operation barrier、plan sequence

bleng-supervisor
    pending plan、platform context、resource retirement

native adapter
    callback/operation 创建时绑定 context，并原样回显 action sequence
```

supervisor 对 platform action 采用 single-use in-flight 语义：dispatch 前消费 `pending_plan`，执行中保存 sequence/context；平台错误、sequence 不匹配或 policy 拒绝结果后都不隐式重放旧 action。可预期的 bounded recovery failure 必须作为 `RecoverySignal` 返回；真正的执行错误由上层显式决定后续 recovery 或 manual reconnect。

## Recovery mode

### Same-owner recovery

```text
owner_id unchanged
backend_id unchanged
event reader unchanged
App PID/boot unchanged
```

必须取得以下证据：

```text
fresh advertisement
fresh connection
fresh GATT
fresh subscription
real expected value
```

成功状态：

```text
same-owner-recovered
```

### Fresh-owner recovery

```text
old operation terminals
→ old reader closes
→ old Bleng shutdown
→ old backend dispose
→ native resources zero
→ new Bleng owner
→ new backend
→ fresh scan/connect/GATT/subscription/value
```

进程可以保持不变，但以下 identity 必须变化：

```text
owner_id
backend_id
connection_id
gatt_session_id
subscription_id
```

对于同进程 fresh-owner recovery，process 和 boot identity 可以保持不变。

成功状态：

```text
fresh-owner-recovered
```

不得把它标记成 `same-owner-recovered`。

### Fresh-process recovery

这是未来独立的 mode，不属于当前 same-process 实现：

```text
old App/process exits
→ new App/process
→ new Bleng owner/backend
→ fresh BLE session
```

成功状态：

```text
fresh-process-recovered
```

它会拥有不同的 PID/boot/session evidence，必须作为独立 gate。

## 状态机

```text
idle
  → active
  → radio-lost
  → draining-old-operations
  → waiting-for-radio-on
  → same-owner-recovery
      ├── same-owner-recovered
      └── same-owner-platform-timeout
  → retiring-old-owner
  → creating-fresh-owner
  → fresh-owner-recovery
      ├── fresh-owner-recovered
      └── fresh-owner-platform-timeout
  → manual-reconnect-required
```

规则：

- Bluetooth ON 只是观察结果，不是 BLE recovery verdict；
- same-owner recovery 的 attempt budget 最多为一次；
- Central recreation 的 attempt budget 最多为一次；
- 只有旧 owner retirement 成功后，才允许创建 fresh owner；
- 旧 event、operation、connection、GATT、subscription 不得进入新 session；
- 不接受 synthetic advertisement、notification 或 EF value；
- CoreBluetooth callback 缺失必须归类为 `platform-timeout`，不能判为通过。

## Evidence contract

Same-owner 文档：

```text
system-loss-recovery.json
state=same-owner-recovered
```

Fresh-owner 文档：

```text
system-loss-fresh-owner-recovery.json
state=fresh-owner-recovered
recovery_mode=fresh-owner
```

Fresh-owner 文档必须绑定：

```text
run_id
control_sha256
terminals_sha256
old_identity
new_identity
old_terminals
new_link
```

Host 校验必须要求：

- old identity 等于原始 native witness identity；
- new owner/backend identity 不同；
- 同进程 fresh-owner mode 下 PID/boot 相同；
- old terminal 集合完整；
- new link ID 合法且不复用旧 link；
- fresh connection/GATT/subscription/value evidence 在 verdict 前全部存在。

平台终态文档：

```text
system-loss-recovery-diagnostic.json
state=recovery-diagnostic
recovery_outcome=platform-timeout | manual-reconnect-required
```

Host 在已验证 Bluetooth ON 后记录该终态，但不调用 `finish`，因此它可以安全归档已恢复的手机 lease，同时仍将 BLE recovery gate 标记为未通过。诊断必须继续绑定 `run_id`、`control_sha256`、`witness_sha256`、原始 owner identity 和明确的 failure reason；不能用 host 的 ON readback 或 sniffer 广播替代 BLE recovery evidence。

## ABI 策略

recovery policy 只作为 supervisor 内部 Rust policy，不直接进入 Stable UniFFI。这样可以避免在 Apple 和 Android integration 收敛前冻结 recovery name 和 payload；ABI 只由 `bleng-supervisor::ffi` 的 foreign records/enums 定义。

当两个平台都完成 review 后，可以考虑通过现有唯一的 BLeng UniFFI image 暴露一小组 private/internal policy surface。禁止创建第二个 native image 或第二套 recovery ABI。

## 实施阶段

### R1：Policy kernel

- 在 `crates/bleng-supervisor/src/recovery.rs` 实现 phase、signal、action 和 identity validation；
- 拒绝 duplicate/stale signal 和 retry loop；
- 增加 deterministic transition/evidence tests；
- 不依赖 native/RF。

### R2：Native lifecycle

- 以 Apple `BleSessionSupervisor` 作为参考实现；
- 将其接入 policy kernel，但不改变 `Bleng` assembly 语义；
- Android Supervisor 保持结构等价，但采用平台原生实现；
- 通过 no-RF lifecycle tests 证明旧 resources 归零和新 identity 生成。

### R3：Native recovery runner seam

- native host 只驱动 Rust Supervisor 的 bounded plan/action loop；
- 使用 Rust-owned active context 和 `observe_signal`，不复制 generation/attempt 状态；
- fake session 先证明 terminal barrier、retirement、fresh-owner 和 single-use action；
- Apple/Android adapter 分别接入真实 event reader/radio source；
- Nitro/JS 仍只保留 lifecycle surface，不暴露 recovery policy 或 BLE handles。

### R4：真实 Apple recovery candidate

只有 R1–R3 全部通过后才执行：

- 一次明确授权的 OFF/ON candidate；
- 一次 same-owner attempt；
- 一次 fresh-owner attempt；
- 不进行自动 destructive retry；
- 使用独立 sniffer 和 provider evidence；
- 最终结果必须是 `same-owner-recovered`、`fresh-owner-recovered` 或 `platform-timeout` 之一。

### R5：Android candidate

复用 Rust policy contract，不复用 Apple native 实现。Android 必须在现有物理设备上独立证明 adapter/GATT/permission/lifecycle 行为。

## Platform foundation completion gate

在 Android 和 iOS 两端的 native foundation 未完成前，不冻结公共 Nitro operation/recovery contract，也不把单个平台的实现投影为跨平台 ABI。两端必须分别完成以下底层验收：

1. production `BleSessionSupervisor` 位于维护的 native integration/package lane，不依赖 `tests/`、HIL App 或 synthetic backend；
2. 一个 Supervisor 独占一个 `Bleng` owner、backend、event reader 和资源退休屏障；
3. `start → initialize → operations/events → retire`、fresh-owner `replace`、`close` 有 deterministic lifecycle tests；
4. old operation terminals、owner/backend identity、connection/GATT/subscription identity 和 resource-zero evidence 可验证；
5. artifact consumer 能从正式 package image 构建，且不存在第二个 UniFFI image 或测试目录泄漏；
6. 真实平台行为单独记录：通过、失败、`platform-timeout` 和 `manual-reconnect-required` 不能互相替代。

当前顺序因此是：

```text
Apple production Supervisor/package
→ Apple deterministic + artifact checks
→ Android formal Bleng recovery integration
→ Android/iOS common state/evidence contract
→ Nitro public contract
→ one explicitly authorized RF candidate per platform
```

这意味着当前 iOS `platform-timeout` 不能通过修改 JS、合成 callback 或重复 OFF/ON 被“修复”为通过。先完成 iOS native foundation 和准确的失败边界；如果 CoreBluetooth 仍不交付 fresh advertisement callback，则保留平台限制，并把它转换为共同 contract 允许的非通过终态。

## 当前证据和开放边界

当前已经取得以下真实观察：

- healthy-process Central recreation：可以观察到 fresh advertisement；
- 真实 iOS Bluetooth OFF/ON 加 same-owner recovery：没有 advertisement callback；
- same-process fresh-owner recovery attempt：仍然没有 fresh advertisement callback；
- provider、firmware、Settings readback 和 cleanup evidence 独立成立，不是当前失败边界。

当前产品 verdict 仍为：

```text
automatic recovery: platform-timeout
manual reconnect required: true
system-loss gate: not passed
```

下一步安全实现顺序是 R1/R2 deterministic policy 和 native lifecycle 收敛。在 recovery boundary 有实质代码改进或平台限制获得明确裁决前，不再重复 destructive RF。
