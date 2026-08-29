# BleHub 采用 UniFFI 还是 BoltFFI 的技术评估

- 状态：Phase 0 决策研究；不直接修改已批准计划
- 日期：2026-08-29
- 规范性来源：[`.plan/PLAN.md`](../../.plan/PLAN.md)
- BleHub 当前机制证据：[`phase0-token-ingress-runtime-results.md`](phase0-token-ingress-runtime-results.md)
- UniFFI 对比 fixture：[`spikes/uniffi-lifecycle/`](../../spikes/uniffi-lifecycle/)
- Kotlin 错误字段 fixture：[`spikes/uniffi-error-field-collision/`](../../spikes/uniffi-error-field-collision/)

## 1. 决策问题

BleHub 需要选择一个 Rust 与 Kotlin/Swift 原生 backend 之间的 FFI 生成和运行时方案。这个选择不是一般意义上的“哪个项目功能更多”，而是哪个方案更适合以下已经批准的 BleHub 约束：

- Rust 拥有统一 DTO、同步 command API、event API 和薄 facade；
- `BleBackend: Send + Sync` 是非 async foreign trait；
- command 的同步 `Result` 只表示是否接受/拒绝投递；
- 最终平台或 BLE 结果通过 event 表达；
- BleHub core 不拥有 runtime、executor、worker thread、mailbox、timer 或 canonical BLE state；
- Kotlin/Swift backend 需要保存 Rust 提供的 event ingress，并在以后收到 OS callback 时直接 push event；
- Windows/Linux backend 由 Rust 实现；Android backend 由 Kotlin 实现；Apple backend 由 Swift 实现；
- public ABI 尚未冻结，因此 Phase 0 仍允许在充分证据下更换 FFI 框架。

当前 BoltFFI 路线已经完成大量 Kotlin/JVM、Android ART/AAR、Swift on Windows、token ingress 和生命周期验证，但官方 `0.30.1` 仍存在直接影响 BleHub 的 #869、#870、#871 和后续发现的 #872。

本报告要回答：

1. UniFFI 是否具有与 BoltFFI #869 对应的对象参数限制；
2. UniFFI 是否能正确保存 foreign backend 和 Rust ingress object；
3. declared/undeclared Kotlin failure 能否可靠映射回 Rust `Result`；
4. UniFFI 是否消除 #870、#871、#872；
5. 稳定 Kotlin/JNA 与实验 Kotlin/JNI 后端分别有什么代价；
6. 若切换，BleHub 已完成的打包、生成和测试工作需要重写多少；
7. 在 public ABI 冻结前，应继续 BoltFFI、立即切换，还是先完成有条件的迁移 gate。

## 2. 结论摘要

### 2.1 当前结论

**从 BleHub 的核心对象图和错误语义看，稳定 UniFFI `0.32.0` 比官方 BoltFFI `0.30.1` 更合适。**

原因不是 UniFFI 更“流行”，而是本地源码和本项目 fixture 已经验证：

- UniFFI foreign trait 可以直接接收并保存 `Arc<BackendEventIngressHandle>`；
- Kotlin backend 可以在 `bind_event_ingress` 返回后，从 Kotlin worker thread 调用该 Rust object；
- Rust 可以保存 `Arc<dyn BleBackend>`，也可以从 Rust-created thread 调用 Kotlin backend；
- foreign trait 的 declared error 和 undeclared Kotlin exception 都能通过 Rust `From<UnexpectedUniFFICallbackError>` 映射成 typed error；
- `thiserror::Error` 与 `uniffi::Error` 可以共同 derive；
- 因此 UniFFI 没有复现 BoltFFI #869、#870 和 #872 的关键限制。

但是，**现在还不应直接修改 `.plan/PLAN.md` 或把生产 crate 从 BoltFFI 改成 UniFFI**，因为：

- 稳定 UniFFI Kotlin backend 使用 JNA，而当前 BoltFFI 路径是直接 JNI；同机实测 UniFFI/JNA 的单次跨边界开销明显更高；
- Android ART、发布 AAR、clean consumer、生命周期、异常、payload 和性能比较已经通过，但性能取舍仍需用户明确接受；
- UniFFI 自己明确不提供完整 XCFramework/SPM packaging，BleHub 必须维护 Apple 打包脚本；
- Windows Swift host fixture 已通过，但真实 macOS Swift runtime、ARC、XCFramework 与 SPM consumer gate 尚未在 macOS/Xcode runner 上运行；
- UniFFI 稳定和实验 Kotlin 后端都仍有与 BoltFFI #871 等价的 `message`/`cause` 错误字段冲突。

### 2.2 推荐决策

建议把 UniFFI 保持为 **Phase 0 首选候选**。Android gate 已经完成；最终决定现在只剩两个明确条件：

> 1. 用户接受物理 Android 设备上约 `16×`（push）和 `21×`（command）的 JNA/JNI 相对开销；绝对 push 吞吐仍约为每秒 `7,900` 次，通常高于 BLE event 需求，但这是产品取舍而不是框架替用户决定。
> 2. Stable UniFFI Swift 在 macOS runner 上通过 retained direct handle、ARC/lifecycle、错误、XCFramework 和 clean SPM consumer gate。

如果两项都成立，建议在 public ABI 冻结前切换到 UniFFI，并删除 token ingress registry 与 BoltFFI ACK workaround 候选。如果 Apple gate 失败，或用户不接受 JNA 开销，则继续使用 BoltFFI并优先等待官方 #869/#872 修复，而不是把两套永久 workaround 静默冻结进 v0.1 contract。

因此当前比较是：

- API/object/error correctness：UniFFI 领先；
- Android direct JNI 性能：BoltFFI 明显领先；
- Android correctness 与发布消费：两条路径均已有运行证据；
- Apple packaging/runtime：BoltFFI 已有准备，UniFFI gate 已实现但仍待 macOS runner 实跑。

## 3. 证据边界

### 3.1 本地源码版本

| 项目 | 本地 revision | 版本边界 | 工作树说明 |
|---|---|---|---|
| BleHub | 当前工作树 | Rust `1.95`、BoltFFI crates.io `=0.30.1` | Phase 0 工作进行中，ABI 未冻结 |
| BoltFFI | `8f862c8dd35ec9d8437ac0212c001baf0d700dd2` | `v0.30.1-8-g8f862c8d` | 包含本地 callback exception patch，不能当作 upstream/released 证据 |
| UniFFI | `bfb0418edbaf11a60823e03c6e5c7117ae66159a` | checkout describe 为 `v0.31.2-169-gbfb0418ed` | 只有未跟踪 `.codegraph/` |
| UniFFI stable crates | 同上 | `uniffi = 0.32.0`、`uniffi_bindgen = 0.32.0` | 本报告 stable Kotlin/Swift 结论基于这些 crate |
| UniFFI experimental JNI | 同上 | generator/runtime `0.31.1` | 与 stable crates 存在版本错位，且 README 明确标记 experimental |

UniFFI checkout 的 `git describe` 不能代替 package version。报告按具体 crate manifest 记录版本，而不把整个 checkout 简化称为 `0.31.2`。

### 3.2 证据等级

本文使用以下标签：

- **已运行验证**：在 BleHub fixture 中实际 build/generate/compile/run；
- **本地源码确认**：由本地 implementation、template、manual 或 upstream test 直接确认；
- **推断**：从已验证机制推导到 BleHub production shape，但尚未在目标平台运行；
- **待验证**：必须由后续 Android/macOS spike 关闭。

### 3.3 本地证据索引

关键结论来自以下本地文件：

- BleHub 当前 backend contract：[`crates/blehub/src/backend.rs`](../../crates/blehub/src/backend.rs)
- BleHub 当前 token ingress assembly：[`crates/blehub/src/hub.rs`](../../crates/blehub/src/hub.rs)
- UniFFI foreign trait contract 与 error policy：[`docs/manual/src/foreign_traits.md`](../../../contrib/uniffi-rs/docs/manual/src/foreign_traits.md)
- UniFFI exported Rust object foreign-trait roundtrip：[`bindgen-tests/lib/src/trait_interfaces.rs`](../../../contrib/uniffi-rs/bindgen-tests/lib/src/trait_interfaces.rs)
- 对应 Kotlin runtime test：[`bindgen-tests/kotlin/tests/trait_interfaces.kts`](../../../contrib/uniffi-rs/bindgen-tests/kotlin/tests/trait_interfaces.kts)
- 对应 Swift runtime/ARC test：[`bindgen-tests/swift/tests/trait_interfaces.swift`](../../../contrib/uniffi-rs/bindgen-tests/swift/tests/trait_interfaces.swift)
- Stable Kotlin error template：[`ErrorTemplate.kt`](../../../contrib/uniffi-rs/uniffi_bindgen/src/bindings/kotlin/templates/ErrorTemplate.kt)
- Stable Kotlin JNA 与 thread attach 说明：[`docs/manual/src/kotlin/gradle.md`](../../../contrib/uniffi-rs/docs/manual/src/kotlin/gradle.md)
- UniFFI packaging 边界：[`docs/manual/src/Motivation.md`](../../../contrib/uniffi-rs/docs/manual/src/Motivation.md)
- Experimental Kotlin/JNI status：[`uniffi-bindgen-kotlin-jni/README.md`](../../../contrib/uniffi-rs/uniffi-bindgen-kotlin-jni/README.md)
- Experimental JNI design：[`uniffi-bindgen-kotlin-jni/DESIGN.md`](../../../contrib/uniffi-rs/uniffi-bindgen-kotlin-jni/DESIGN.md)
- Experimental JNI attach helper：[`runtime/src/attach.rs`](../../../contrib/uniffi-rs/uniffi-bindgen-kotlin-jni/runtime/src/attach.rs)

### 3.4 本次新增运行证据

执行：

```text
pixi run just ffi-spike-test-uniffi-kotlin-jvm
pixi run just ffi-spike-check-uniffi-error-field-collision
pixi run just ffi-spike-test-uniffi-android-emulator
pixi run just ffi-spike-test-uniffi-swift-windows
```

稳定 UniFFI/JNA fixture 在 Windows JVM 上通过 7 项测试，0 failure、0 error：

1. Kotlin backend 保存 direct Rust ingress object；
2. Kotlin worker thread 在 bind 返回后 push；
3. Rust-created thread 调用 Kotlin backend，再回到 Rust ingress；
4. declared 与 undeclared Kotlin failure 映射为 typed Rust error；
5. constructor/bind failure 的 typed error 映射边界；
6. `20`、`244`、`1024` byte payload 各 1,000 次；
7. 10,000 次 direct-handle create/dispose/late-push lifecycle。

Gradle XML 记录总耗时 `1.289s`，但这是 debug desktop mechanism test，**不是 Android 性能 benchmark**。

Android producer instrumentation 在 API 36 x86_64 emulator 和 arm64 物理设备上均为 `6 tests/device, 0 failures`，发布制品 consumer 为 `1 test/device, 0 failures`。运行证据覆盖 ART、main `Handler`/`Looper`、Rust-created thread、8-worker concurrent direct ingress、`dispose()` 等待已 admission callback、10,000 undeclared exceptions、10,000 lifecycle、late callback、三档 payload、Rust/foreign handle counter 回 baseline，以及只声明 Maven artifact 的 clean consumer。发布 POM 传递 JNA 依赖，consumer 不读取 generated source。

Windows Swift host gate 使用 Swift `6.3.3` 和发布版 UniFFI `0.32.0` 通过 retained direct ingress、Rust-created thread、64 路并发 push、declared/undeclared error、late callback 与 10,000 lifecycle。它排除了大部分 Swift binding/ownership 机制问题，但不替代 macOS ARC/Xcode 证据。

错误字段 fixture 的 Kotlin 编译按预期失败，并明确报告：

```text
Conflicting declarations: val message: String
'message' hides member of supertype 'Throwable'
'cause' hides member of supertype 'Throwable'
```

## 4. BleHub 架构匹配度

### 4.1 BoltFFI 当前对象图

由于 #869，当前 production-shaped 设计不能直接写成：

```rust
fn bind_event_ingress(
    &self,
    ingress: BackendEventIngressHandle,
) -> Result<(), BleHubError>;
```

BleHub 当前采用：

```rust
fn bind_event_ingress(
    &self,
    token: BackendEventIngressToken,
) -> Result<(), BleHubError>;
```

然后 native backend 调用 top-level：

```rust
push_backend_event(token, event)
```

Rust 侧必须维护：

- process-local token allocator；
- token → `Weak<EventIngressState>` registry；
- registration/unregistration ownership；
- stale token、duplicate token、overflow 与 rollback；
- constructor ordering 和 close/unregister ordering。

这套实现已经经过认真验证，不是不可用；但它是由 FFI capability gap 引入的基础设施，不是 BLE domain 本身需要的状态。

### 4.2 UniFFI direct-handle 对象图

本次 fixture 实际使用：

```rust
#[uniffi::export(foreign)]
pub trait BleBackend: Send + Sync {
    fn bind_event_ingress(
        &self,
        ingress: Arc<BackendEventIngressHandle>,
    ) -> Result<(), BleHubError>;
}
```

对象图变为：

```text
Rust Phase0Fixture
  -> Arc<dyn BleBackend>
  -> Kotlin BleBackend implementation
  -> retained BackendEventIngressHandle wrapper
  -> cloned Arc<BackendEventIngressHandle> in Rust
```

OS callback 路径可以直接是：

```text
Android BluetoothGattCallback / ScanCallback
  -> Kotlin backend maps platform data to BleEvent
  -> retained ingress.push(event)
  -> Rust EventIngressState
  -> downstream BleEventSink
```

不需要再经过 backend trait，不需要全局 token lookup，也不需要 BleHub core 新增线程或 runtime。

### 4.3 direct handle 是否会自动形成强引用环

本次验证对象图中：

- Rust fixture 强持有 Kotlin backend；
- Kotlin backend 强持有 Rust ingress；
- Rust ingress 不持有 fixture 或 backend。

因此该图不是环。dispose 后 Rust 释放 backend，Kotlin backend 再释放 ingress wrapper。

UniFFI manual 明确警告 foreign trait 与 Rust object 很容易形成跨语言 cycle，且框架不会自动解决。生产实现仍必须保证：

- ingress 不反向持有 backend；
- downstream sink 如果能回到 Hub，不应与 Hub 形成不可打断的强环；
- dispose 显式关闭 ingress，再释放 backend；
- Kotlin/Swift wrapper 在生命周期边界显式 `destroy`/ARC release，而不是只依赖 GC/Cleaner。

这与现有 BleHub lifecycle discipline 一致，但 direct handle 删除了 token registry 这一层额外状态。

## 5. Foreign trait 与 `Send + Sync`

### 5.1 UniFFI

**本地源码确认：** UniFFI 当前推荐的形状是：

```rust
#[uniffi::export(foreign)]
pub trait BleBackend: Send + Sync { ... }
```

Rust 侧使用 `Arc<dyn BleBackend>` 保存 foreign implementation。UniFFI manual 明确要求 foreign trait 具有适当的 `Send + Sync` 约束，并说明 Rust references drop 后会清理 foreign references。

UniFFI 不支持 foreign trait method 中的 Rust borrowed reference；参数需要 by-value。对 BleHub 不是问题，因为 ingress 本来就应作为可保留的 `Arc<Object>` handle 传递，而不是临时 `&T`。

### 5.2 BoltFFI

BoltFFI 也能生成 foreign trait object，并且当前 BleHub JVM/Android/Swift fixture 已证明 backend retention 和多线程调用可以工作。问题集中在 foreign callback 的参数 shape 和异常协议，而不是“BoltFFI 完全不支持 trait object”。

### 5.3 两者都不能替 native code 自动证明线程安全

Rust trait 上的 `Send + Sync` 是 BleHub 对 foreign implementation 的行为契约。无论使用 BoltFFI 还是 UniFFI：

- Kotlin/Swift backend 都必须自行串行化平台要求的操作；
- CoreBluetooth queue、Android GATT operation queue 不会由 Rust marker 自动实现；
- callback 可能并发进入 Rust，ingress 必须自己建立线性化边界；
- FFI generator 不能替业务代码证明 actor/queue/thread confinement 正确。

因此 UniFFI 的类型支持更自然，但不会减少平台 backend 必须承担的并发责任。

## 6. BoltFFI #869、#870、#871、#872 对比

| 问题 | BoltFFI `0.30.1` | UniFFI stable `0.32.0` | BleHub 影响 |
|---|---|---|---|
| #869 foreign method 接收 exported Rust class handle | compile probe 失败，必须 token ingress | local upstream Kotlin/Swift tests支持；BleHub JVM fixture 已运行通过并可长期保存 | UniFFI 可删除 token registry |
| #870 type-level `thiserror #[error]` 与 FFI marker 冲突 | 已复现；当前用 `derive_more` workaround | `thiserror::Error + uniffi::Error` 已在 upstream examples 和本 fixture 编译 | UniFFI 更自然，但 #870 不是单独换框架理由 |
| #871 Kotlin error field `message` | raw generated Kotlin 编译失败 | `message` 同样失败，`cause` 也失败 | 两边都应使用 `detail`/`reason` 等字段名 |
| #872 unchecked Kotlin `Result<(), E>` callback exception 被当作成功 | official `0.30.1` production blocker；本地 patch 不能当 released solution | BleHub fixture 中 undeclared bind/start exception 均映射为 `UnexpectedCallback` | UniFFI 在关键 correctness 边界明显更好 |

### 6.1 #869：UniFFI 的支持强度

本地 UniFFI upstream test 已包含：

```rust
fn roundtrip_interface(
    &self,
    value: Arc<TestInterface>,
) -> Arc<TestInterface>;
```

Kotlin 和 Swift 都实现并调用该 foreign trait method。BleHub fixture 在此基础上进一步验证：

- foreign method 参数是实际 `Arc<BackendEventIngressHandle>`；
- Kotlin 保存 wrapper，而不是只在 call stack 内使用；
- bind 返回后从另一个 Kotlin thread 调用；
- close 后 late push 返回 `Closed`；
- 10,000 次生命周期释放完成。

因此“UniFFI 理论上可能支持”已经升级为“BleHub-shaped stable Kotlin/JNA mechanism 已运行通过”。

仍未验证的是 Android ART 和 Swift retained ingress 的目标平台生命周期，而不是基本类型能力。

### 6.2 #870：UniFFI 与 `thiserror`

本 fixture 直接使用：

```rust
#[derive(Debug, thiserror::Error, uniffi::Error)]
pub enum CommandDispatchError { ... }
```

Rust build、Kotlin generation 和 Kotlin runtime test 全部通过。UniFFI 的 derive marker 与 `thiserror` helper attribute 没有 BoltFFI scanner 的命名冲突。

这能让 BleHub 恢复常规 Rust error 生态，但迁移决策不应只由少写一个 workaround derive 决定。

### 6.3 #871：UniFFI 没有解决

稳定 Kotlin template 对 data error variant 生成：

```kotlin
class Failed(
    val `message`: String,
) : CollisionException() {
    override val message
        get() = "message=${`message`}"
}
```

同一个 class 内出现两个 `message` property。`cause` 则与 `Throwable.cause` 冲突。

实验 JNI template 也把 field 直接生成为 `Exception` subclass property，因此同样存在冲突风险。

BleHub 应继续采用以下 wire naming policy，无论最终选择哪个框架：

- exported error payload 使用 `detail`、`reason`、`context`；
- 不使用 `message` 或 `cause` 作为 Kotlin exception variant field；
- 普通 DTO 中的 `message` 不受此规则影响，因为普通 DTO 不继承 `Throwable`；
- 保留 expected-failure fixture，upstream 修复后转换为 positive regression。

### 6.4 #872：比 #870/#871 更重要

BleHub command 的 synchronous `Result<(), E>` 是 accepted/rejected 语义边界。如果 foreign implementation 抛出 undeclared exception，而 Rust 观察到 `Ok(())`，就会把根本没有被 backend 接受的 command 报告为已投递。

这是 correctness blocker，不只是错误文案或生成代码美观问题。

BoltFFI 当前可选项是：

- 等待 official fix；
- 采用项目自己的 explicit ACK token 协议；
- 使用本地 fork/patch，但承担长期维护和发布责任。

UniFFI stable 已经具有 `UnexpectedUniFFICallbackError` 协议。只要 BleHub error type 实现：

```rust
impl From<uniffi::UnexpectedUniFFICallbackError> for CommandDispatchError
```

undeclared Kotlin exception 会映射为 typed `Err`，而不是 panic 或 success。本 fixture 对 constructor bind 和 start command 都已运行验证。

这使 UniFFI 同时避免 token ingress workaround 和 ACK workaround，是本次比较中最有价值的结果。

## 7. Error 与 exception 模型

### 7.1 UniFFI foreign trait method 应全部返回 `Result`

UniFFI manual 明确建议 foreign trait method 返回 compatible `Result`。若 foreign code 出现未声明异常：

- error type 实现 `From<UnexpectedUniFFICallbackError>`：转换成该 error；
- 没有该实现：generated bridge 可能 panic。

BleHub 应把该实现视为 contract lint requirement，而不是可选增强。适用方法包括：

- `capabilities`；
- `bind_event_ingress`；
- 所有 command dispatch；
- `dispose`；
- 如果 downstream sink 也是 foreign trait，则包括 `on_event`。

### 7.2 诊断文本仍然不可作为业务协议

UniFFI 可以保留 unexpected exception 的 reason string，但 BleHub 仍应维持现有规则：

- `reason`/`detail` 仅供日志和诊断；
- downstream 不应解析字符串决定业务状态；
- 可稳定处理的分支使用 enum variant/code；
- native throwable/class name 不应成为跨平台稳定 ABI。

### 7.3 Rust panic 边界仍需保留

UniFFI 对 foreign exception 的处理不等于 Rust panic containment。生产 ingress 仍需：

- 在导出 event ingress 边界 containment downstream panic；
- 避免 panic 穿越 FFI；
- fault injection 在 isolated subprocess 中验证 abort/panic policy；
- dispose/drop 不依赖可能 panic 的 foreign cleanup 成功执行。

## 8. Kotlin：稳定 JNA 与实验 JNI

### 8.1 稳定 Kotlin/JNA

**本地源码确认：** UniFFI stable Kotlin binding 依赖 JNA `5.12.0` 或更高。本 fixture 使用 `5.18.1`。

优点：

- 是当前稳定、完整、已有广泛 test matrix 的 Kotlin backend；
- foreign trait、object、record、enum、typed error 语义成熟；
- 本次 direct ingress 和 exception tests 无需 generator patch；
- 不需要 BleHub 维护自定义 JNI C bridge generator。

代价：

- Android AAR 增加 JNA AAR 依赖和 native ABI 产物；
- 每次 Kotlin↔Rust call 经过 JNA dispatch，而不是 BoltFFI 当前 direct JNI path；
- 需要验证 release shrink/consumer rules、ABI split 和 native library loading；
- 需要建立自己的 Rust Android build + generated Kotlin + AAR packaging pipeline。

### 8.2 JNA thread attach 影响

UniFFI local manual 明确警告：Rust-created persistent thread 调 Kotlin callback 时，如果没有显式永久 attach，JNA 可能在每次 callback 进行 attach/detach，代价较高。

对 BleHub 的实际影响需要区分：

1. Android `ScanCallback`/`BluetoothGattCallback` 通常先进入 JVM-managed thread，再由 Kotlin 调 ingress.push；这是 Kotlin→Rust，不是 Rust worker thread callback。
2. command 调用方向是 Rust→Kotlin。如果 caller 自己从 Rust-created thread 调 BleHub，JNA 仍需正确 attach。
3. BleHub core 自己不创建 worker thread，因此不应为了 UniFFI 引入 runtime 或 thread pool。
4. caller/backend 已有长期 Rust native thread 时，可以由 integration layer 选择显式 attach，但这不应成为 BleHub core runtime 责任。

本 fixture 已证明单次 Rust-created thread 调 Kotlin backend 可工作；尚未证明高频 persistent Rust-thread callback 的成本可接受。

### 8.3 实验 `uniffi-bindgen-kotlin-jni`

本地 README 明确写明：

```text
This is an experimental crate for generating JNI-based Kotlin bindings.
```

它具有很吸引 BleHub 的设计：

- JNI primitive/direct buffer，而不是 JNA；
- trait interface 使用 64-bit handle；
- Kotlin implementation 使用 handle map；
- Rust object 传给 Kotlin 时 clone，因为 Kotlin 可能长期保存；
- unexpected Kotlin error 由 Rust catch；
- Rust-created callback thread 使用 `AttachCurrentThread`。

但当前不能作为 production recommendation：

- generator/runtime 版本仍为 `0.31.1`，依赖 stable `0.32.0` components；
- scaffolding pipeline 与 stable macro scaffolding 不同；
- 只支持 top-level library crate 的特定生成布局；
- inspected `attach_current_thread` helper 调用 `AttachCurrentThread` 和 `PushLocalFrame`/`PopLocalFrame`，该 helper 内没有 detach；其 JVM-created/Rust-created thread 生命周期需要专门审计；
- packaging、upgrade churn 和 ABI stability 尚无 BleHub 证据。

结论：**不能用“UniFFI 有 JNI backend”来回避 stable JNA gate。** 如果选择 UniFFI v0.1，默认应评估 stable JNA；experimental JNI 只能作为独立研究，不应混入同一个 production path。

## 9. Swift 与 Apple 平台

### 9.1 已有本地 upstream 证据

UniFFI Swift tests 已覆盖：

- Swift 实现 Rust trait；
- Rust 保存/roundtrip foreign trait；
- foreign trait method 接收并返回 exported Rust object；
- typed error；
- repeated foreign trait reference creation/destruction 后 ARC counter 回到零；
- sync 和 async trait interface。

这比只读 generator API 名称更强，说明 #869 的基本 object shape 在 Swift 也存在 upstream runtime test。

### 9.2 BleHub-shaped Swift host 证据与 Apple 待验证项

Windows Swift `6.3.3` host fixture 已运行验证：Swift backend 保存 direct Rust ingress；Rust 保存 Swift backend；Rust-created thread 调用 Swift 后重入 Rust；64 路并发 push；declared/undeclared errors；`dispose()` 等待已经 admission 且被阻塞的 callback 完成；late callback；10,000 次 lifecycle 后 weak backend 释放，Rust ingress created/dropped counter 回到预期 baseline。

该结果说明生成的 Swift foreign trait 与 direct handle 对象图能按 BleHub shape 工作，但 Windows Swift runtime 不等于 Apple ARC/Xcode/package 环境。仍必须在 macOS runner 上验证：

- Swift backend 在 bind 返回后长期保存 `BackendEventIngressHandle`；
- 后续 `DispatchQueue` callback 调 `push`；
- Rust 保存 Swift backend；
- explicit dispose、late callback、in-flight callback drain；
- 10,000 次 ARC create/dispose，所有 strong/weak counter 回 baseline；
- declared 与 undeclared Swift error；
- generated header/modulemap/Swift source 组成 XCFramework；
- 独立 SPM consumer 只依赖 produced package。

iOS simulator 可以验证 FFI、ARC、package 和多数线程行为，但不能替代真实 CoreBluetooth/BLE hardware。该限制与 BoltFFI 相同。

## 10. Packaging 与 toolchain

### 10.1 BoltFFI 当前优势

BleHub 已经为 BoltFFI 建立：

- pinned CLI bootstrap/doctor；
- Kotlin 和 Swift generation；
- Android `arm64-v8a`/`x86_64` AAR assembly；
- API 36 emulator 和 arm64 device ART tests；
- clean AAR-only consumer；
- Windows Swift host test；
- Apple XCFramework/SPM packaging recipe 和 macOS CI 准备；
- project-owned Android/Swift/native toolchain resolution；
- generated API/ABI 后续 snapshot 方向。

这些是真实迁移成本，不能因为 UniFFI API 更干净就忽略。

### 10.2 UniFFI 明确不提供 end-to-end packaging

UniFFI manual 明确说明：它生成 Kotlin/Swift bindings，但不负责：

- 编译 Android Rust target；
- 把 native libraries 和 Kotlin source/class 打入 AAR；
- 形成完整 Android consumer workflow；
- 形成完整 XCFramework/SPM release pipeline。

Swift generator 可以生成：

- Swift source；
- C header；
- modulemap；
- XCFramework-compatible modulemap。

但 library slicing、lipo/xcodebuild、XCFramework layout、SPM binary/source target、codesign 和 consumer test 仍由 BleHub 管理。

### 10.3 已建立的 UniFFI 发布路径

Android 侧已经建立并运行：

- `aarch64-linux-android` 与 `x86_64-linux-android` release native libraries；
- generated Android Kotlin source；
- AAR + Maven POM 发布；
- POM 传递 JNA，clean consumer 只声明 `dev.blehub:blehub-uniffi-lifecycle:0.0.0`；
- API 36 emulator 与 arm64 physical device producer/consumer instrumentation。

Apple 侧已经建立但尚未在 macOS runner 实跑：

- 五个 Rust staticlib target：iOS device、两种 iOS simulator、两种 macOS architecture；
- `lipo` 形成 iOS simulator/macOS universal archives；
- stable `uniffi-bindgen-swift` 生成统一 Swift/header/modulemap；
- `xcodebuild -create-xcframework`；
- 每个 slice 的 architecture、`Info.plist` 与 `module.modulemap` 检查；
- 高层 Swift source target + binary XCFramework target；
- macOS clean SPM runtime consumer；
- generic iOS simulator/device compile-link，禁用 code signing。

### 10.4 可复用与必须重写的部分

可复用：

- Pixi JDK/Rust/Android/Apple toolchains；
- Huawei Gradle wrapper distribution；
- Aliyun Gradle plugin/dependency repositories；
- Android SDK/NDK bootstrap/doctor；
- target architecture matrix；
- emulator/device orchestration；
- lifecycle test cases与 counter design；
- consumer project验收原则；
- docs/ABI snapshot policy。

必须改写：

- BoltFFI annotations 与 generated API；
- `boltffi.toml` generation/pack integration；
- JNI/native library naming与loading；
- Android AAR source/class/native layout；
- Swift header/modulemap/package assembly；
- generated API snapshots；
- typed error constructors/naming；
- production token ingress registry，如果迁移到 direct handle 则应删除；
- #872 explicit ACK workaround 候选，不应迁移到 UniFFI。

## 11. 性能、依赖与二进制影响

### 11.1 Android 同 workload 实测

对 stable UniFFI/JNA 与 BoltFFI/JNI 使用相同机制 workload：push `20,000` 次、command `10,000` 次，每类 warmup `100` 次，push 使用 empty payload 以减少 DTO 差异。

| 环境 | Transport | Push | Command |
|---|---|---:|---:|
| arm64 物理设备 | UniFFI/JNA | 约 `126,654 ns/op` | 约 `126,134 ns/op` |
| arm64 物理设备 | BoltFFI/JNI | 约 `7,920 ns/op` | 约 `5,896 ns/op` |
| API 36 x86_64 emulator | UniFFI/JNA | 约 `167,344 ns/op` | 约 `187,481 ns/op` |
| API 36 x86_64 emulator | BoltFFI/JNI | 约 `7,067 ns/op` | 约 `5,443 ns/op` |

物理设备相对差距约为 push `16×`、command `21×`；emulator 约为 `24×` 与 `34×`。JNA 相对开销明显且真实，不能描述为与 JNI 性能相当。另一方面，物理设备 empty-event push 的绝对吞吐约为每秒 `7,900` 次，通常仍高于 BLE event 流量。是否接受这一余量必须由用户 review。

现有运行没有发现 deadlock、事件丢失、错误 success 或 lifecycle counter 漂移，但尚未形成低端设备、长时间运行、p95/p99、GC 与 per-process RSS 证据。不能用 Android `/proc/meminfo` 的系统级数据推导 app RSS。

当前 fixture 制品大小：UniFFI debug/release AAR 约 `573/569 KiB`，BoltFFI debug AAR 约 `823 KiB`；包含传递 JNA 的 UniFFI consumer debug APK 约 `2.9 MiB`，BoltFFI consumer约 `3.3 MiB`。这些是 fixture pipeline 数据，不是 production size 保证。

### 11.2 BLE 场景中的实际频率

BleHub event 不是音视频级数据流，但可能出现：

- 扫描期间较高频 advertisement event；
- notification burst；
- connect/discover/read/write status event；
- shutdown/late callback race。

因此 JNA overhead 可能可接受，但必须测量，不能凭“BLE 一般不高频”直接通过。

### 11.3 如需收紧性能结论的后续 benchmark

当前 mechanism benchmark 足以证明数量级，但不等于 production workload profiling。如果用户认为约每秒 `7,900` 次的 physical-device push 余量仍不足，再补：

- 20/244/1024 byte payload 分档 benchmark；
- single thread 与 8 callback threads 的 median、p95/p99；
- 长时间 advertisement/notification burst；
- allocation、GC pause 与 app process RSS；
- 一台目标最低配置 Android 设备；
- release/minified consumer 的启动和 library loading。

不建议在没有产品 event-rate 上限时继续增加 synthetic loop 次数。当前决策应先由用户 review 已测得的 BoltFFI baseline、UniFFI 绝对吞吐和架构简化收益。

## 12. 迁移成本评估

| 项目 | 难度 | 说明 |
|---|---:|---|
| Rust DTO/error annotations | 中 | 语义接近，但所有 generated names/snapshots 会变化 |
| `BleBackend` foreign trait | 低 | UniFFI shape 更接近批准设计 |
| direct ingress | 中 | 删除 registry 是简化，但需重写生命周期和 rollback tests |
| Kotlin backend source | 中 | trait/error/object API 名称变化，Android BLE code尚未开始，当前时点仍有窗口 |
| Android build/AAR | 高 | UniFFI 不提供 BoltFFI pack 等价能力，需要 project-owned packaging |
| Swift bindings | 中 | generation成熟，但 package assembly需重接 |
| XCFramework/SPM | 高 | 现有 BoltFFI packaging work不能直接复用 generator output |
| test fixtures | 中 | case design可复用，binding-specific glue需改写 |
| public ABI | 当前低、冻结后高 | 现在版本 `0.0.0` 且 ABI未冻结，是切换成本最低阶段 |
| 长期 workaround 维护 | UniFFI 较低 | 可删除 token registry 和 ACK；保留 `message`/`cause` naming rule |

迁移不是从零开始重写整个项目，也不是简单替换一个 Cargo dependency。最重的部分在 Android/Apple packaging，而不是 Rust trait declaration。

## 13. 风险矩阵

| 风险 | BoltFFI | UniFFI stable | 判断 |
|---|---:|---:|---|
| direct Rust ingress object | 高：#869/workaround | 低：JVM 已验证 | UniFFI 胜 |
| unchecked foreign Unit exception | 高：#872 blocker | 低：JVM 已验证 | UniFFI 胜 |
| Rust error ecosystem | 中：#870 workaround | 低 | UniFFI 胜 |
| Kotlin `message`/`cause` | 中 | 中 | 平局，均需 naming rule |
| Android call overhead | 低：direct JNI | 高于 JNI：physical push约 `16×`、command约 `21×` | BoltFFI 胜 |
| Android packaging | 低/中：已有 AAR lane | 中：AAR/POM/clean consumer已运行 | 均已关闭 correctness risk |
| Apple basic foreign trait/object | 中：已有 host evidence | 低/中：upstream + Windows host fixture | UniFFI 略胜 |
| BleHub-shaped macOS ARC/package | 待完成 | recipe已建立、待 macOS实跑 | 均未关闭 |
| framework maturity/test matrix | 中 | 低/中 | UniFFI 胜 |
| generator/API churn | 中 | 中 | 均需 pin + snapshot |
| 实验 JNI 风险 | 不适用 | 高 | 不应作为 v0.1 选择依据 |
| 已有项目投入 | 低风险 | 迁移成本高 | BoltFFI 胜 |

## 14. Go/No-Go gate

### 14.1 UniFFI Android gate：已通过

已使用 stable Kotlin/JNA 而不是 experimental JNI，完成：

1. Rust build `x86_64-linux-android` 与 `aarch64-linux-android`；
2. generated Kotlin + JNA `5.18.1` 组成 debug/release AAR；
3. clean AAR-only consumer，无 generated-source 泄漏；
4. API 36 emulator 和 arm64 device 上运行 direct ingress lifecycle；
5. declared/undeclared bind、command、dispose exception；
6. Kotlin platform callback thread、Rust-created thread和并发 push；
7. dispose/in-flight/late callback；
8. 10,000 lifecycle baseline counter；
9. representative payload与 BoltFFI baseline benchmark；
10. AAR size、native ABI、R8/consumer rules、library loading。

该 gate 已在 API 36 x86_64 emulator 与 arm64 physical device 上通过。它不需要真实 BLE hardware；验证的是 FFI、ART、JNA 和 packaging。真实 Android BLE/permission/RF 仍在 production backend/HIL 阶段验证。

### 14.2 Apple 对比 gate：recipe 已建立，待一次 macOS workflow 实跑

`.github/workflows/phase0-apple.yml` 在同一次 workflow run 中启动两个相互隔离的 `macos-15` job：BoltFFI job 验证当前 token-ingress baseline，UniFFI job 验证 retained direct-ingress 候选。两边都保存环境、runtime、package 日志和生成的 Apple package，避免只为候选方案保留证据。

BoltFFI job 已准备验证 declared/undeclared callback error、bind rollback、Rust-created thread、64 路并发 push、in-flight dispose drain、late push、幂等 dispose、ARC backend release、10,000 token lifecycle、XCFramework metadata、clean macOS SPM consumer，以及 generic iOS simulator/device compile-link。

UniFFI job 已建立以下自动化检查，仍需在 macOS/Xcode runner 完成：

1. Swift backend 保存 direct Rust ingress；
2. DispatchQueue later callback；
3. declared/undeclared error；
4. ARC strong/weak/final-free counters；
5. dispose/in-flight/late callback；
6. 10,000 lifecycle；
7. macOS dynamic/static consumer；
8. iOS device/simulator slices；
9. XCFramework；
10. clean SPM consumer。

该 gate 同样不验证 CoreBluetooth RF；但必须验证真实 Swift runtime、ARC、Clang module 和 package consumer。

### 14.3 切换 Go 条件

满足以下全部条件后，建议修改计划并迁移：

- Android stable JNA correctness 全通过；**已满足**；
- Android 性能和包体积经用户 review 可接受；**待用户明确接受约 `16×`/`21×` 相对开销**；
- macOS ARC/XCFramework/SPM 全通过；**待 macOS runner**；
- 不需要 experimental JNI；
- direct ingress 删除 token registry 后 rollback/close invariant 仍完整；
- generated API/ABI snapshot 和 consumer test 建立；
- migration diff 明确，不混入 production BLE backend 实现。

### 14.4 UniFFI No-Go 条件

任一情况成立则不切换：

- JNA 在目标 Android workload 下有不可接受的 latency、GC、attach 或包体积；
- AAR consumer 无法稳定处理 JNA/native library packaging；
- Swift retained ingress/ARC 出现无法可靠关闭的 lifecycle gap；
- 必须依赖 experimental JNI 才能满足基本 correctness/performance；
- migration 需要改变 caller-owned runtime 或 thin-core architecture。

No-Go 后优先顺序应是：

1. 等待/推动 BoltFFI official #869/#872 fix；
2. 若时间不允许，单独 review token ingress 与 explicit ACK 的长期成本；
3. 不把本地未发布 BoltFFI patch 静默当作 production dependency；
4. 不因 FFI 问题改变 BleHub 的 fire-and-forget、caller-owned runtime 设计。

## 15. 对已批准计划的影响

本报告不直接修改 `.plan/PLAN.md`。当前计划仍以 BoltFFI 和 token ingress workaround 为已批准方向。

如果用户批准执行 UniFFI gate，建议只新增一个 Phase 0 候选评估任务，不立即重写 production crate：

1. 复用本报告 fixture 形成 Android stable JNA AAR lane；
2. 形成 macOS Swift direct-handle lane；
3. 用相同 workload 与 BoltFFI 对比；
4. 提交独立 Go/No-Go 结果供 review；
5. 只有 Go 后，才在计划中显式把 BoltFFI 改为 UniFFI，并列出 token registry 删除和 packaging migration。

这样避免一边实现 production backend、一边更换 FFI foundation，也避免 sunk cost 代替技术决策。

## 16. 最终判断

**如果只比较 BleHub 的核心 contract，UniFFI 是当前更好的选择。** 它原生支持 BleHub 想要的 `Arc<dyn BleBackend>` 与 direct Rust ingress object，正确处理 undeclared foreign exception，并与 `thiserror` 协作；这些正是薄层设计最重要的 ownership 和 dispatch correctness。

**Android gate 已经证明 stable UniFFI/JNA 可以正确运行和发布消费，但不能忽略约 `16×`/`21×` 的物理设备相对开销。** 其绝对吞吐对通常 BLE workload 仍有很大余量，且 fixture APK/AAR size 没有表现出劣势；是否接受应由用户明确决定。

**最终技术 gate 只剩 Apple。** Windows Swift host 已证明 BleHub-shaped direct handle、跨线程回调、error 与 10,000 lifecycle 能工作；项目也已实现 static slices、XCFramework、SPM 和 iOS compile-link recipes。只有这些 recipe 在真实 macOS/Xcode runner 上通过后，才能把 Apple 风险关闭。

因此当前推荐：

> 暂不修改 `.plan/PLAN.md`，也不重写 production。先运行 UniFFI Apple gate；若通过且用户接受 Android JNA 性能取舍，则在 ABI freeze 前迁移到 UniFFI。若 Apple gate 失败或性能取舍不可接受，则保留 BoltFFI，并等待 official #869/#872 或显式 review workaround。

完整 gate 跑完后，证据已经足以做 UniFFI/BoltFFI 的最终选择；不再存在必须等真实 BLE 硬件才能判断的 FFI foundation 风险。真实 Android/iOS BLE、权限和 RF 仍属于后续 backend/HIL gate，而不是本次 FFI 选型 gate。
