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

配置也按职责分层：`SupervisorConfig.recovery` 是完整的 `RecoveryPolicyConfig`，包含初始 context、旧 link、旧 operation terminal barrier 和 recovery fallback 开关；supervisor 自己的配置只包含 resource-zero 要求和 `RecoveryTarget`。FFI 使用同构的 `ForeignSupervisorConfig.recovery`，只在边界上把字符串/vector 转换为 Rust 的强类型。这样 policy 参数不会一部分嵌套、一部分散落在 supervisor 顶层，也避免 FFI adapter 自己调用 `RecoveryPolicy::new`。

`RecoveryPolicy` 构造后只保留运行期状态和必要的策略开关，不保存整份初始化 config：初始 context、旧 link 和 expected terminal 集合会被拆入对应状态集合，避免重复保存同一份配置。这里不额外引入 builder 或 typestate：配置已经是单个 typed record，真正的运行期 phase 仍由 policy 状态机和 context/sequence fence 共同校验。

它负责：

- 持有当前 `Bleng` owner/session；
- 驱动 `RecoveryPolicy`；
- 登记并校验 `RecoveryPlan` 的 context、sequence 和 action；
- 执行 owner-boundary action（retirement、fresh-owner assembly），并把结果重新提交给 policy；
- 等待旧 owner shutdown 和 native retirement；
- 要求 resource-zero 后才能创建 fresh owner；
- 调用 `PlatformSessionFactory` 创建下一代 backend/session。

当前 Swift/Kotlin supervisor 是迁移期 active compatibility path。它们在 Rust supervisor 和 native adapter 取得对等证据前不得删除。`tests/hil` 中的 Supervisor 也继续保留为 HIL 专用实现，不能成为 production Rust supervisor 的依赖。

R1 已建立 `crates/bleng-supervisor` 的 Rust ownership seam：fake `PlatformSessionFactory` 可以创建一代 session，supervisor 将 backend 一次性装配到新的 `Bleng`，由 `Bleng` 负责已装配 backend 的 dispose，owner shutdown 后等待 event reader drain，再读取并校验 platform resource snapshot；未装配失败使用独立的 `dispose_unassembled` 路径，避免 double-dispose。这个顺序复用现有 `BleBackend::bind_event_ingress`、`BleHub` backend ownership 和 Apple/Android event-reader retirement 语义。R2 已增加 optional `ffi` feature 并通过编译/clippy，验证 object-safe foreign factory/session、`Arc<dyn BleBackend>`、`Arc<Bleng>`、async event-reader drain 和 async resource snapshot 的 UniFFI 机制；该 component 现已由 `bleng-uniffi` 组装并通过 Rust image check。R3 已生成 staged Swift/Kotlin bindings，Apple `ApplePlatformSession` 和 Android `AndroidPlatformSession`/factory 均已通过各自的静态编译 seam；Apple/Android native Supervisor 构造及 start/close migration seam 也已在各自 integration target 编译通过；Android 三组件 AAR（`blehub`、`bleng`、`bleng-supervisor`）已通过单一 native image、AAR 类表和 artifact-only consumer build；Apple Nitro bridge 也已在 Pixi 管理的 `apple-ci` 环境中通过 simulator compile。上述证据仍不等于 native runtime、RF、完整 recovery action executor、公共 Nitro contract 或 ABI freeze 通过；迁移期 Swift/Kotlin supervisor 继续保留，reviewed API snapshots 也不因这次生成而自动更新。

## 各层职责

### `crates/bleng-supervisor/src/recovery.rs`

负责纯 deterministic policy：

- recovery phase 转换；
- same-owner、fresh-owner、fresh-process mode；
- old operation terminal barrier；
- old/new session identity 校验；
- stale signal 和 duplicate signal 拒绝；
- connection/GATT/subscription link 不复用校验；
- 有界 retry policy；
- platform-timeout 和 manual-reconnect outcome；
- recovery evidence schema 与 verdict 分类。

它不得依赖 Tokio、CoreBluetooth、Android BLE API、React Native、Settings、nRF provider 或 phone lease。它只包含 deterministic tests，不直接暴露 Stable UniFFI；UniFFI 只通过 supervisor 的 FFI facade 暴露经过边界转换的类型。

### Platform session adapter

Platform adapter 负责可执行的 native 能力，但不拥有跨平台 recovery policy：

- 创建 Apple/Android backend；
- 实现 native dispose 和 callback ingress retirement；
- 提供 native resource snapshot；
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

### `Bleng`

`Bleng` 继续作为 operation runtime。现有一次性 assembly 和 shutdown 语义保持不变：

```text
Bleng::new
→ assemble_backend once
→ initialize/operations/events
→ shutdown
```

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

React view 的 mount/unmount 不得创建、退休或替换 BLE session。

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

### R3：Nitro bridge

- 将 Supervisor state bridge 到 Nitro event；
- JS 不感知 owner/backend handle；
- 验证 App/React view 生命周期不会退休 Supervisor；
- 增加 RN deterministic lifecycle tests。

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
