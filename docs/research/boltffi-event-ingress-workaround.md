# BoltFFI event ingress：理想 handle 与 v0.1 token workaround

- 状态：架构调研与 v0.1 实施建议；最终约束以 [`.plan/PLAN.md`](../../.plan/PLAN.md) 为准
- 日期：2026-08-28
- 适用范围：Kotlin/Swift foreign `BleBackend` 如何长期保存 Rust event ingress capability
- 上游 issue：[boltffi/boltffi#869](https://github.com/boltffi/boltffi/issues/869)
- 相关调研：[FFI 生命周期与并发竞态设计](ffi-lifecycle-concurrency.md)、[BoltFFI backend 技术评估](boltffi-vs-rust-native-ble-backends.md)

## 1. 决策摘要

BleHub 保持一个不因 BoltFFI 能力变化而改变的装配 contract：

1. platform factory 构造不含 ingress、不会启动 BLE 工作或 callback source 的 inert backend；
2. Rust application 调用 `BleHub::new(backend, rust_sink)`；
3. constructor先通过typed`backend.capabilities()`读取、校验并缓存静态metadata；
4. constructor在内部完成一次性`bind_event_ingress`；
5. capabilities、bind和ingress routing都成功后才返回可用Hub；
6. downstream始终是普通Rust`BleEventSink`，不跨第二次FFI；
7. ingress最终只通过`Weak<EventIngressState>`到达Hub state，不形成强引用环。

理想方案是把 exported Rust class `BackendEventIngressHandle` 直接传给 foreign backend。当前 BoltFFI `0.30.1` 不能生成这个 foreign callback method parameter，因此 v0.1 使用：

```text
`BackendEventIngressToken`
+ process-local token -> Weak<EventIngressState> registry
+ exported push_backend_event(token, event)
```

这是明确、隔离、可删除的 FFI compatibility layer，不是 BLE domain registry。它优于 raw pointer、closure retention 和重新引入两阶段 builder。

### 1.1 Naming

`EventIngress` 表示“backend/platform event 进入 BleHub Rust boundary”的方向，与事件离开 bridge 后的 downstream `BleEventSink` 相对。保留该术语能避免把 backend ingress capability误叫成 application sink。

命名约定：

- `BackendEventIngressToken`：public FFI data type，强调它只能被 backend 用于 event ingress；
- `push_backend_event`：v0.1 exported free function；
- `EventIngressState`：Rust-private per-Hub delivery/lifecycle state；若实现放在 `event_ingress` module 内，可在模块内部简称 `State`；
- `BackendEventIngressRegistration`：Rust-private RAII registry-entry guard，名称明确表达它不是 event handle或第二层 bridge；
- `BleEventSink`：只保留给 Rust application downstream。

不使用 `BackendEventSinkToken`，因为 backend 不是 sink；也不使用泛化的 `CallbackToken`，因为项目中还存在平台 callback registration token，容易混淆。

## 2. 证据与可信度

### 2.1 已由本地源码和 fixture 验证

可执行 fixture 固定官方 crates.io `boltffi`/`boltffi_cli`：

```text
0.30.1
```

相邻 BoltFFI checkout 仅用于按 local-first 规则审计对应实现源码，不参与 BleHub 构建或生成。

`spikes/boltffi-class-handle-callback-param/` 复现：

```rust
#[boltffi::export]
pub trait ForeignReceiver {
    fn attach(&self, handle: RustHandle);
}
```

宏报错：

```text
unsupported expansion: callback method parameter shape
```

本地源码显示 class parameter 已在 lowering 阶段形成 handle plan，但 foreign callback wrapper expansion 没有对应 class-handle branch：

```text
D:\projects\contrib\boltffi\boltffi_binding\src\lower\callable\params.rs
D:\projects\contrib\boltffi\boltffi_macros\src\expansion\wrapper\callback.rs
```

没有 annotation 可以补齐该能力。`#[data]` 会把类型改成 value DTO；`#[custom_ffi]` 会变成另一套 registry/token 设计；closure 不能长期保存。

### 2.2 已通过 compile probe 与 Phase 0 JVM fixture 验证

以下形状在当前 BoltFFI 上可以生成：

```rust
#[boltffi::data]
pub struct BackendEventIngressToken {
    pub value: u64,
}

#[boltffi::export]
pub trait BleBackend {
    fn bind_event_ingress(
        &self,
        token: BackendEventIngressToken,
    ) -> Result<(), BleHubError>;
}

#[boltffi::export]
pub fn push_backend_event(
    token: BackendEventIngressToken,
    event: BleEvent,
) -> EventIngressStatus;
```

该最初 probe 只证明 generator 接受参数形状。现已新增 `spikes/boltffi-token-ingress/`，通过真实 `BleHub`、全局 registry、Rust-only sink 与 foreign backend 运行 production-shaped Windows JVM、Windows Swift 和 Android ART/AAR lanes。官方 `0.30.1` 的 JVM suite 有 19/21 项通过；两个 unchecked Kotlin exception case 暴露 [#872](https://github.com/boltffi/boltffi/issues/872) 并保持失败。Android emulator/AAR consumer 与 Windows Swift smoke 已通过。准确环境、已验证边界和未关闭 blocker 见 [`phase0-token-ingress-runtime-results.md`](phase0-token-ingress-runtime-results.md)。

### 2.3 仍属设计推导或跨目标待实测

以下结论尚未被当前 JVM fixture 完整证明：

- registry entry 的强弱引用和全部 wrapper/native handle 计数在所有失败与 race 后都回到 baseline；
- ArcSwap snapshot guard scope 和 notification workload 性能满足最终门槛；
- 官方 BoltFFI 后续版本修复 #872，并让完整 ABI 的 unexpected foreign exception 明确失败而不是伪装成功；
- macOS Swift runtime/ARC 与 XCFramework/SPM packaging 通过；Windows Swift host、Android ART/AAR 和普通 Windows/JVM artifact consumer 已通过；
- foreign runtime registration failure injection、完整 payload boundary catalog 与各语言 object counters 通过；pure Rust near-overflow、并发分配和 registration rollback 已有测试。

## 3. 理想 direct-handle 方案

理想 API：

```rust
pub struct BackendEventIngressHandle {
    state: Weak<EventIngressState>,
}

#[boltffi::export]
impl BackendEventIngressHandle {
    pub fn push(&self, event: BleEvent) -> EventIngressStatus {
        let Some(state) = self.state.upgrade() else {
            return EventIngressStatus::Closed;
        };
        state.push(event)
    }
}

#[boltffi::export]
pub trait BleBackend: Send + Sync {
    fn bind_event_ingress(
        &self,
        ingress: BackendEventIngressHandle,
    ) -> Result<(), BleHubError>;
}
```

Ownership：

```text
BleHub
  strong -> foreign BleBackend
  value -> cached BackendCapabilities
  strong -> Arc<EventIngressState>

EventIngressState
  strong while open -> Rust BleEventSink

foreign backend
  strong -> generated BackendEventIngressHandle wrapper

BackendEventIngressHandle
  weak -> EventIngressState
```

优点：

- 没有 process-global map；
- 没有 token allocator、lookup 或 unregister；
- generated wrapper 直接承担 retain/release；
- callback 调用路径最短；
- capability 不能被整数复制到无关位置。

当前阻塞点仅是 BoltFFI #869，不是 BleHub 对象模型本身。

## 4. v0.1 token workaround

### 4.1 FFI API

```rust
#[boltffi::data]
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct BackendEventIngressToken {
    pub value: u64,
}

#[boltffi::export]
pub trait BleBackend: Send + Sync {
    fn bind_event_ingress(
        &self,
        token: BackendEventIngressToken,
    ) -> Result<(), BleHubError>;

    // commands and dispose
}

#[boltffi::export]
pub fn push_backend_event(
    token: BackendEventIngressToken,
    event: BleEvent,
) -> EventIngressStatus;
```

当前 BoltFFI scanner 明确拒绝 `#[data] pub struct BackendEventIngressToken(u64);`：tuple struct 不进入 record AST。因此跨 FFI 的 v0.1 type 必须使用命名字段 record；仅为隐藏一个字段引入 `#[custom_ffi]` 不值得。Rust-private allocator/registry 可以直接使用内部 `u64` key，但 public BoltFFI contract 保留：

```rust
pub struct BackendEventIngressToken {
    pub value: u64,
}
```

`value` 因 BoltFFI data DTO 生成要求而公开，但语义上 opaque：

- backend 只能保存并原样回传；
- `0` 无效；
- 不能解析、加减、生成或持久化到进程外；
- 不能放进 `BleEvent`；
- 不能解释成地址或 native object handle；
- 只在创建它的 BleHub dynamic-library instance 和当前进程生命周期内有效。

### 4.2 Registry shape

这个 workload 是极端 read-mostly：正常进程通常只有少量 active Hub，insert/remove 只发生在 Hub 构造/关闭，notification/advertisement 却可高频 lookup。v0.1 因此优先使用 `arc-swap` 保存 immutable `HashMap` snapshot，而不是让每个 event 获取全局 `RwLock` 或 `DashMap` 分片锁：

```rust
type EventIngressRegistrySnapshot =
    HashMap<u64, Weak<EventIngressState>>;

struct BackendEventIngressRegistry {
    next_token: AtomicU64,
    entries: ArcSwap<EventIngressRegistrySnapshot>,
}

impl BackendEventIngressRegistry {
    fn allocate_token(
        &self,
    ) -> Result<BackendEventIngressToken, BleHubError>;

    fn register(
        self: &Arc<Self>,
        token: BackendEventIngressToken,
        state: Weak<EventIngressState>,
    ) -> Result<BackendEventIngressRegistration, BleHubError>;

    fn lookup(
        &self,
        token: BackendEventIngressToken,
    ) -> Option<Weak<EventIngressState>> {
        self.entries.load().get(&token.value).cloned()
    }

    fn unregister(&self, token: BackendEventIngressToken);
}

struct BackendEventIngressRegistration {
    registry: Arc<BackendEventIngressRegistry>,
    token: BackendEventIngressToken,
    registered: AtomicBool,
}

impl BackendEventIngressRegistration {
    fn unregister(&self) {
        if self.registered.swap(false, Ordering::AcqRel) {
            self.registry.unregister(self.token);
        }
    }
}

impl Drop for BackendEventIngressRegistration {
    fn drop(&mut self) {
        self.unregister();
    }
}

static BACKEND_EVENT_INGRESS_REGISTRY: LazyLock<
    Arc<BackendEventIngressRegistry>,
> = LazyLock::new(|| Arc::new(BackendEventIngressRegistry::new()));

fn global_backend_event_ingress_registry() -> &'static Arc<BackendEventIngressRegistry> {
    &BACKEND_EVENT_INGRESS_REGISTRY
}
```

`BackendEventIngressRegistry` 是完整 routing abstraction，而不是裸 static map：

- `allocate_token` 封装 monotonic checked allocator；
- `register` 执行 RCU insert 并返回绑定到同一 registry `Arc` 的 registration guard；
- `lookup` 执行 lock-free snapshot lookup并返回 cloned `Weak`；
- `unregister` 执行 RCU remove；
- `global_backend_event_ingress_registry()` 隔离 production static access；它不是运行时可变 service locator，不允许在 token 存活期间替换 registry；
- `push_backend_event` 是唯一 FFI free function，只把 global registry 传给 private `push_backend_event_with_registry`；
- pure Rust tests 直接构造 isolated registry并调用 helper，Hub assembly tests 调 private `BleHub::new_with_registry`，不共享或替换 process-global state。

`BackendEventIngressRegistration` 是 RAII 注销凭证，不参与 event forwarding：

- `register_ingress` 成功 insert 后创建它；
- `BleHub` 把它保存为私有字段；
- explicit `dispose/close` 调用 `unregister()`；
- `Drop` 对未完成 explicit close 的路径幂等兜底；
- atomic flag 保证 explicit close 与 Drop 不会重复执行 RCU remove。

没有这个 guard 也能把 token直接存进 Hub并在所有退出路径手写 remove，但 constructor rollback、dispose error 和 Drop fallback 更容易漏删 process-global entry。guard 把全局资源生命周期绑定回 Rust ownership，代价只是一个 token 和一个 atomic flag。

registry 本身必须位于 exported `push_backend_event` 所在的 Rust crate/library instance 中；不是 application 或 platform backend 各自维护一份。它只允许三类操作：

- constructor bind 成功后的 RCU insert；
- `push_backend_event` 的 lock-free snapshot lookup + clone `Weak`；
- close/rollback/Drop fallback 的 RCU remove。

lookup 必须在以下操作前释放 ArcSwap snapshot guard：

- upgrade `Weak`；
- validate `BleEvent`；
- 获取 downstream mutex；
- 调用 `BleEventSink::on_event`；
- 调用 foreign backend；
- dispose、等待 callback 或执行日志 sink/user code。

insert/remove 使用 `ArcSwap::rcu`：clone 当前 O(active Hub) 小 map、修改一个 entry、原子替换 snapshot。并发 writer 可能重试 closure，但 writer 只存在于 Hub assembly/close，不在 event hot path。

替代项：

- `DashMap` 是成熟的 sharded-lock map，但 read 不是 lock-free；此处 entry 很少，分片收益有限。
- `papaya::HashMap` 提供 guard/pinning 下的 lock-free read，适合 read-heavy concurrent map；若未来 active Hub/写入量明显增长可评估。
- `scc::HashIndex` 提供 lock-free read 与 immutable reachable values，也可作为增长后的候选。
- 当前 workspace 没有这些依赖的本地源码；实现前应固定版本并按项目 local-first 规则审计。Phase 0 benchmark 必须比较 `ArcSwap<HashMap>` 与简单 `RwLock<HashMap>` baseline，只有真实数据才能推翻默认选择。

### 4.3 Token allocation

v0.1 使用 process-local monotonic `AtomicU64` allocator：

- 从 `1` 开始；
- 每次成功分配后永不复用，即使 bind/registration 失败；
- 使用 checked atomic update，达到上限时 constructor 返回 `BleHubError`；
- 禁止 wrap 到 `0`；
- close 不把 token放回 free list。

永不复用消除 ABA：旧 backend 的迟到 callback 最多得到 `Closed`，不可能命中新 Hub。随机 token 对同进程内受信 backend 没有额外生命周期收益，因此 v0.1 不增加 RNG 依赖。

### 4.4 Constructor 顺序

```text
1. call backend.capabilities() and validate static metadata
2. if capabilities Err/invalid:
     best-effort backend.dispose
     return Err
3. create Arc<EventIngressState> with Rust sink
4. allocate never-reused token
5. call backend.bind_event_ingress(token)
6. if bind Err:
     state.close_and_drain
     best-effort backend.dispose
     return Err
7. register token -> Weak<EventIngressState>
8. if registration Err:
     state.close_and_drain
     best-effort backend.dispose
     return Err
9. store backend, capabilities, state, registration guard
10. return BleHub
```

bind 发生时 token 尚未注册。backend 必须保持 inert，不得在 bind 内或 bind 返回后、第一条 command 前创建 callback source。若违规调用 `push_backend_event`，只能得到 `Closed`，不会把事件交给尚未返回的 Hub/sink。

`bind_event_ingress` 的 contract：

- 只允许一次；
- `Ok` 表示 token 已保存，但 backend 仍无 BLE operation/callback source；
- `Err` 表示 token 未保存；
- repeated bind 返回 typed `BleHubError`；
- 不 emit event，不调用 native BLE operation；
- exception 必须由 generated/foreign wrapper 映射成可靠 failure，不能清除后伪装 `Ok`。

### 4.5 Push 算法

```rust
fn push_backend_event_with_registry(
    registry: &BackendEventIngressRegistry,
    token: BackendEventIngressToken,
    event: BleEvent,
) -> EventIngressStatus {
    let Some(state) = registry.lookup(token).and_then(|state| state.upgrade()) else {
        return EventIngressStatus::Closed;
    };
    state.push(event)
}

#[boltffi::export]
pub fn push_backend_event(
    token: BackendEventIngressToken,
    event: BleEvent,
) -> EventIngressStatus {
    push_backend_event_with_registry(
        global_backend_event_ingress_registry().as_ref(),
        token,
        event,
    )
}
```

语义：

- missing、unregistered、stale token：`Closed`；
- weak state 已释放：`Closed`；
- state closed 或 downstream 已取走：`Closed`；
- event shape 非法：`InvalidEvent { message }`；
- Rust sink 返回 typed error：`DownstreamFailed { message }`；
- delivery 成功：`Delivered`。

backend可以记录status/message，但不得解析message控制流程、自动retry event或把ingress failure当BLE operation completion。`InvalidEvent`/`DownstreamFailed`消费当前attempt：backend照常提交terminal/correlation cleanup并继续后续event；只有`Closed`才停止向该ingress push。

### 4.6 Close 顺序

```text
1. acquire exclusive command/close gate
2. set EventIngressState closed
3. acquire downstream mutex and drain in-flight on_event
4. take and release Rust sink
5. backend.dispose while registry entry still resolves to closed state
6. unregister token entry
7. release backend ownership
8. return lifecycle result; Hub remains closed even on dispose error
```

`BackendEventIngressRegistration` 的 explicit unregister 与 `Drop` fallback 必须幂等。backend dispose 期间 registry entry 暂时保留，确保 concurrent late callback稳定看到 closed state；unregister 后 callback 看到 missing token，同样返回 `Closed`。

## 5. Ownership 与状态边界

v0.1：

```text
BleHub
  strong -> foreign BleBackend
  value -> cached BackendCapabilities
  strong -> Arc<EventIngressState>
  owns -> BackendEventIngressRegistration

EventIngressState
  strong while open -> Rust BleEventSink

process-local registry
  token -> Weak<EventIngressState>

foreign backend
  value copy -> BackendEventIngressToken
```

registry 不保存：

- backend；
- Hub facade；
- downstream sink；
- command/session/device/GATT state；
- event queue；
- native object；
- timeout、retry 或 cancellation state。

它的空间复杂度是 O(active Hub)，每 Hub 一个小 entry。它不是此前禁止的 canonical BLE registry。

## 6. 竞态矩阵

| 竞态 | 结果 | 机制 |
|---|---|---|
| bind 内违规 push | `Closed` | token 尚未注册 |
| bind `Err` 后违规保存并 push | `Closed` | token 永不注册且永不复用 |
| bind `Ok` 与 registration | backend 仍 inert，无合法 callback | constructor contract |
| push lookup 先于 close | weak upgrade 后由 state closed flag/downstream mutex裁决 | per-Hub close drain |
| close 先标 closed、push 后 lookup | `Closed` | state closed double-check |
| push lookup 与 unregister 并发 | 已 clone 的 `Weak` 看到 closed，或新 snapshot lookup miss；均 `Closed` | ArcSwap snapshot + RCU replacement + state lifecycle |
| backend dispose 内迟到 push | `Closed` | entry 仍存在但 state 已 closed |
| unregister 后迟到 push | `Closed` | missing token |
| old token 与新 Hub | 永不误投 | token 不复用 |
| concurrent Hub push | ArcSwap snapshot lock-free lookup；delivery 独立 | snapshot guard 在 state push 前释放 |
| duplicate close/Drop | entry 只删除一次 | registration atomic/idempotent guard |

## 7. 为什么不采用其他绕路

### 7.1 Raw pointer encoded as `u64`

不采用。它绕过 Rust provenance、ownership 和 lifetime，容易形成 UAF/ABA，也无法让 BoltFFI wrapper管理 retain/release。

### 7.2 Retained closure

不采用。BoltFFI 文档明确 stored closures 不受支持；精确 compile probe 还遇到 generated safe/unsafe function-pointer mismatch。

### 7.3 两阶段 builder

不采用。它把 FFI 工具限制泄漏给 application assembly，要求 application 先取 ingress 再构造 backend，并重新引入原本要消除的循环装配。token 可以保留单一 `BleHub::new` 和 inert backend。

### 7.4 Fork BoltFFI immediately

不是 v0.1 前置条件。#869 值得 upstream 修复，也可以在具备维护能力时做 upstream-quality patch；但 token 路径允许 BleHub 实施与验证先继续。不得为了去掉一个小 registry仓促维护长期 fork。

## 8. Phase 0 验证

必须提交并运行接近最终 contract 的 token fixture，而不是只传整数的 compile demo。

### 8.1 Pure Rust

- token `0` rejected；
- concurrent allocation 唯一；
- failed bind/registration 的 token 不复用；
- missing/stale token 返回 `Closed`；
- registry 只存 `Weak`，不会延长 state/sink；
- `BackendEventIngressRegistry::lookup` lock-free，snapshot guard 不跨 `EventIngressState::push`；
- `push_backend_event_with_registry` 和 `BleHub::new_with_registry` 使用 isolated registry，parallel tests 不替换 global；
- push/unregister、push/close、dispose/push race；
- close drain 后无 active downstream callback；
- 10,000 次 assembly/close 后 registry baseline 为零；
- stale token 不能命中新 Hub；
- allocator near-overflow fixture 不 wrap。

### 8.2 Kotlin/JVM 与 Android

- generated `bind_event_ingress(token)` 与 top-level `push_backend_event` API；
- foreign backend 长期保存 token；
- JVM worker、Handler-style thread 和 Rust-attached thread push；
- bind success/failure/repeated bind/no-push-during-bind；
- unchecked exception 不得被清除后伪装 `Ok`；
- callback-before-command-return；
- close/dispose/late push；
- clean Gradle/AAR consumer。

桌面 JVM 先验证 common JNI/object graph；Android ART、Handler/Looper 和 AAR 在 Android lane 验证。

### 8.3 Swift

- generated token DTO、foreign protocol method 和 top-level push function；
- serial queue 与 concurrent queue push；
- bind failure/repeated bind；
- dispose/unregister/late callback；
- ARC/deinit 与 Rust drop counter；
- clean XCFramework/SPM consumer。

### 8.4 Go/No-Go

v0.1 token ABI 只有满足以下条件才可冻结：

- 无 UAF、double free、deadlock、token collision 或 stale misdelivery；
- registry entry 和所有对象计数回到 baseline；
- ArcSwap registry lookup 不成为 notification throughput 瓶颈，RCU writes 不影响正常 event latency；
- bind/constructor/exception failure 不暴露半初始化 Hub；
- Kotlin、Android 和 Swift 目标 lane 的生成、运行、打包通过。

BoltFFI #869 不再是 token ABI 的 No-Go；它是未来删除 registry 的迁移前置条件。

## 9. Direct-handle 迁移清单

只有同时满足以下条件才启动迁移：

1. 项目固定的 BoltFFI revision 已修复 #869；
2. expected-failure fixture 改为 expected-pass；
3. Kotlin 与 Swift 都验证 foreign backend 可长期 retain class wrapper并跨线程调用；
4. bind failure/repeated bind、exception、dispose/late callback、10,000 次 lifecycle 和 packaging 全部通过；
5. direct handle 性能不差于 token path，且 generated API 可维护。

迁移代码边界：

| v0.1 token | direct handle |
|---|---|
| `bind_event_ingress(BackendEventIngressToken)` | `bind_event_ingress(BackendEventIngressHandle)` |
| backend 保存 token value | backend retain generated class wrapper |
| `push_backend_event(token, event)` | `handle.push(event)` |
| process-local ArcSwap snapshot lookup | handle 内 `Weak::upgrade` |
| close 注销 registration | close 后 wrapper 可晚释放，Weak 保证安全 |

保持不变：

- `BleHub::new(backend, rust_sink)`；
- inert backend；
- one-time bind；
- bind failure rollback；
- `EventIngressState`；
- `BleEventSink`、`EventIngressStatus` 和所有 command/event DTO；
- command/close gate、downstream mutex、closed flag 和 close drain；
- application API 与 caller-owned runtime/state model。

参数变化会改变 generated backend ABI，因此需要协调升级 Kotlin/Swift backend package并提升 ABI version。迁移完成后删除 token allocator、registry、registration guard 和 exported free push；不要永久维护双入口，也不要让同一 backend 同时 push 两次。
