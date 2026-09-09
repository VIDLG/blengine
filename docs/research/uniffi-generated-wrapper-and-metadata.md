# Stable UniFFI wrapper、metadata 与纯 Rust 调用边界

- 状态：实现机制说明；架构约束以 [`.plan/PLAN.md`](../../.plan/PLAN.md) 为准
- 日期：2026-09-03
- 适用版本：Stable UniFFI `0.32.0`、当前 BleHub production candidate
- 相关源码：[`crates/blehub`](../../crates/blehub)、[`crates/blehub-uniffi`](../../crates/blehub-uniffi)

## 1. 结论摘要

BleHub 中的 UniFFI 标记承担三个不同职责：

1. `#[derive(uniffi::Object)]` 使 `Arc<T>` 能以 opaque object handle 跨 FFI 边界传递，并生成对应类型转换支持；
2. `#[uniffi::export]` 和 `#[uniffi::constructor]` 为公开函数、constructor 与方法生成 C ABI wrapper 和接口 metadata；
3. `uniffi::setup_scaffolding!()` 提供组件级 scaffolding，最终由 `uniffi-bindgen` 从 native library 中读取 metadata，生成 Swift/Kotlin binding。

`#[derive(uniffi::Object)]` 本身不会把 `BleHub` 的字段按值暴露给 Swift/Kotlin，也不会让纯 Rust 方法调用绕行 FFI。Rust 直接调用 `BleHub::start_scan` 时仍是普通 Rust 调用；只有 generated Swift/Kotlin API 才经过 object handle、参数 lower/lift、C ABI wrapper 与 `RustCallStatus`。

不过，带有这些标记的 crate 仍有编译依赖、生成代码和潜在二进制体积成本，因此不能把它们理解成完全无成本、可任意删除的注释。

## 2. BleHub 中各层的所有权

当前职责分布如下：

| 层 | 位置 | 责任 |
|---|---|---|
| Canonical Rust contract | [`crates/blehub`](../../crates/blehub) | 定义 `BleHub`、`BleBackend`、`BleEventSink`、DTO、error 和 UniFFI annotations |
| Production native image | [`crates/blehub-uniffi`](../../crates/blehub-uniffi) | 链接并导出唯一 production `cdylib`/`staticlib`，提供 bindgen 入口和 package staging |
| C ABI header | generated package 中的 `blehubFFI.h` | 声明可由 Swift/Kotlin/JNA/JNI 侧调用的 native symbols |
| Language binding | generated `blehub.swift` 或 `blehub.kt` | 提供语言原生 class、protocol、enum、record、error 和 converter |

[`crates/blehub-uniffi/src/lib.rs`](../../crates/blehub-uniffi/src/lib.rs) 只重导出 canonical crate：

```rust
pub use blehub;
pub use blehub::*;
```

它不定义第二套 API。UniFFI annotations 仍与 canonical Rust 类型放在一起，最终 wrapper 和 metadata 被链接进 `blehub-uniffi` production native image。

## 3. 从 Rust API 到外语 API

以 [`BleHub`](../../crates/blehub/src/hub.rs) 为例，源码形状是：

```rust
#[derive(uniffi::Object)]
pub struct BleHub {
    // private Rust state
}

#[uniffi::export]
impl BleHub {
    #[uniffi::constructor]
    pub fn new(
        backend: Arc<dyn BleBackend>,
        downstream: Arc<dyn BleEventSink>,
    ) -> Result<Arc<Self>, BleHubError> {
        // ...
    }

    pub fn start_scan(
        &self,
        command: StartScanCommand,
    ) -> Result<(), CommandDispatchError> {
        // ...
    }
}
```

生成链路是：

```text
Rust declarations and UniFFI annotations
-> proc-macro generated converters, C ABI wrappers and binary metadata
-> blehub-uniffi native static/dynamic library
-> uniffi-bindgen reads metadata from that library
-> blehubFFI.h plus blehub.swift / blehub.kt
-> application calls the generated language API
```

Bindgen 不需要重新解析 BleHub Rust 源码；它以候选 native library 中嵌入的 metadata 为输入。这保证 wrapper 与 language binding 来自同一个已编译 candidate。

## 4. Object handle 与 `derive(Object)`

### 4.1 为什么不是按值传递

`BleHub` 内部包含 backend、ingress、锁和其他 Rust-owned state，不能也不应该作为 C struct 复制到外语侧。`Object` 把它映射为 opaque `uint64_t` handle：

```c
uint64_t uniffi_blehub_fn_clone_blehub(
    uint64_t handle,
    RustCallStatus *out_status
);

void uniffi_blehub_fn_free_blehub(
    uint64_t handle,
    RustCallStatus *out_status
);
```

外语代码不得解释这个数值。UniFFI 使用它恢复 Rust object reference，并通过 clone/free 管理对应引用生命周期。

### 4.2 Swift object wrapper

当前 generated Swift binding 中，`BleHub` 的核心形状是：

```swift
open class BleHub: BleHubProtocol, @unchecked Sendable {
    fileprivate let handle: UInt64

    required public init(unsafeFromHandle handle: UInt64) {
        self.handle = handle
    }

    public func uniffiCloneHandle() -> UInt64 {
        return try! rustCall {
            uniffi_blehub_fn_clone_blehub(self.handle, $0)
        }
    }

    deinit {
        try! rustCall {
            uniffi_blehub_fn_free_blehub(handle, $0)
        }
    }
}
```

对应 converter 只在 Swift object 与 handle 之间转换：

```swift
public struct FfiConverterTypeBleHub: FfiConverter {
    typealias FfiType = UInt64
    typealias SwiftType = BleHub

    public static func lift(_ handle: UInt64) throws -> BleHub {
        BleHub(unsafeFromHandle: handle)
    }

    public static func lower(_ value: BleHub) -> UInt64 {
        value.uniffiCloneHandle()
    }
}
```

所以 `derive(Object)` 的核心不是序列化 `BleHub` 的字段，而是定义引用对象的 FFI 表示和生命周期操作。

## 5. C ABI wrapper

当前 Apple production package 的 generated header 位于：

[`crates/blehub-uniffi/generated/apple-production/BleHubUniFfiNative.xcframework/macos-arm64_x86_64/Headers/blehubFFI.h`](../../crates/blehub-uniffi/generated/apple-production/BleHubUniFfiNative.xcframework/macos-arm64_x86_64/Headers/blehubFFI.h)

其中 constructor 和部分方法声明为：

```c
uint64_t uniffi_blehub_fn_constructor_blehub_new(
    uint64_t backend,
    uint64_t downstream,
    RustCallStatus *out_status
);

RustBuffer uniffi_blehub_fn_method_blehub_capabilities(
    uint64_t ptr,
    RustCallStatus *out_status
);

void uniffi_blehub_fn_method_blehub_start_scan(
    uint64_t ptr,
    RustBuffer command,
    RustCallStatus *out_status
);
```

它们是由 `#[uniffi::export]` 等标记生成的 native symbols，而不是手写 API。

### 5.1 Wrapper 的概念性逻辑

`start_scan` wrapper 的逻辑大致相当于以下伪代码；这用于解释调用边界，不是宏的逐字展开结果：

```rust
#[no_mangle]
extern "C" fn uniffi_blehub_fn_method_blehub_start_scan(
    handle: u64,
    command: RustBuffer,
    status: *mut RustCallStatus,
) {
    uniffi_rust_call(status, || {
        let hub = lift_blehub_handle(handle)?;
        let command = lift_start_scan_command(command)?;
        hub.start_scan(command)?;
        Ok(())
    });
}
```

Wrapper 负责：

1. 将 opaque handle 转成一次有效的 Rust object reference；
2. 将 C ABI 参数 lift 成 Rust DTO；
3. 调用真正的 `BleHub` Rust 方法；
4. 将返回值或 declared error lower 回 FFI 表示；
5. 防止 Rust panic 穿越 C ABI boundary。

它不改变 `BleHub::start_scan` 自身的实现。纯 Rust caller 直接调用该方法，不进入 wrapper。

## 6. `RustBuffer` 与 `RustCallStatus`

复杂 record、enum 和 error 不直接依赖 Rust struct layout，而通过 byte buffer 传输：

```c
typedef struct RustBuffer {
    uint64_t capacity;
    uint64_t len;
    uint8_t *data;
} RustBuffer;

typedef struct RustCallStatus {
    int8_t code;
    RustBuffer errorBuf;
} RustCallStatus;
```

例如，Swift `StartScanCommand` 先由 generated converter lower 为 `RustBuffer`，Rust wrapper 再 lift 为 canonical `StartScanCommand`。`BackendCapabilities` 的方向相反：Rust 将返回值 lower 为 `RustBuffer`，Swift 再 lift 为 Swift record。

`RustCallStatus` 区分正常返回、declared error 和 unexpected panic。Swift binding 根据它返回正常值、抛出 typed error，或者报告 UniFFI internal failure。

当前 generated Swift `startScan` 形状为：

```swift
open func startScan(command: StartScanCommand) throws {
    try rustCallWithError(
        FfiConverterTypeCommandDispatchError_lift
    ) { status in
        uniffi_blehub_fn_method_blehub_start_scan(
            self.uniffiCloneHandle(),
            FfiConverterTypeStartScanCommand_lower(command),
            status
        )
    }
}
```

实际调用路径是：

```text
Swift startScan
-> clone BleHub handle
-> lower StartScanCommand into RustBuffer
-> call C ABI wrapper
-> lift handle and command
-> Rust BleHub::start_scan
-> lower success/error into RustCallStatus
-> Swift returns or throws
```

## 7. Metadata 的实际形态

UniFFI metadata 不是项目维护的 JSON schema，也不是运行时读取 Rust 类型的 reflection。它是 proc macro 生成并嵌入 native object/library 的静态二进制描述。

当前 macOS production archive 中可观察到以下 symbols：

```text
_UNIFFI_META_BLEHUB_INTERFACE_BLEHUB
_UNIFFI_META_BLEHUB_CONSTRUCTOR_BLEHUB_NEW
_UNIFFI_META_BLEHUB_METHOD_BLEHUB_CAPABILITIES
_UNIFFI_META_BLEHUB_METHOD_BLEHUB_CONNECT
_UNIFFI_META_BLEHUB_METHOD_BLEHUB_DISCONNECT
_UNIFFI_META_BLEHUB_METHOD_BLEHUB_DISCOVER_GATT
_UNIFFI_META_BLEHUB_METHOD_BLEHUB_DISPOSE
_UNIFFI_META_BLEHUB_METHOD_BLEHUB_INITIALIZE
_UNIFFI_META_BLEHUB_METHOD_BLEHUB_READ_CHARACTERISTIC
_UNIFFI_META_BLEHUB_METHOD_BLEHUB_READ_RSSI
_UNIFFI_META_BLEHUB_METHOD_BLEHUB_SET_NOTIFICATIONS
_UNIFFI_META_BLEHUB_METHOD_BLEHUB_START_SCAN
_UNIFFI_META_BLEHUB_METHOD_BLEHUB_STOP_SCAN
_UNIFFI_META_BLEHUB_METHOD_BLEHUB_WRITE_CHARACTERISTIC
```

概念上，一条 method metadata 描述以下内容：

```text
component: blehub
kind: method
object: BleHub
name: start_scan
arguments: [StartScanCommand]
return: ()
throws: CommandDispatchError
async: false
```

库内实际保存的是 UniFFI 私有的紧凑编码，不应由 BleHub 自己解析或形成第二套 schema。Bindgen 使用这些 metadata 生成目标语言中的 class、protocol、record、enum、error、converter 和 callback glue。

## 8. API checksum 与版本校验

Native library 为公开接口导出 checksum 函数，例如：

```c
uint16_t uniffi_blehub_checksum_method_blehub_start_scan(void);
uint16_t uniffi_blehub_checksum_constructor_blehub_new(void);
```

Generated Swift binding 在一次性初始化时验证这些值：

```swift
if uniffi_blehub_checksum_method_blehub_start_scan() != 3477 {
    return InitializationResult.apiChecksumMismatch
}

if uniffi_blehub_checksum_constructor_blehub_new() != 23480 {
    return InitializationResult.apiChecksumMismatch
}
```

它还验证 binding 与 native scaffolding 的 UniFFI contract version：

```swift
let bindingsContractVersion = 30
let scaffoldingContractVersion = ffi_blehub_uniffi_contract_version()

if bindingsContractVersion != scaffoldingContractVersion {
    return InitializationResult.contractVersionMismatch
}
```

这可以在开始正常调用前发现以下错误组合：

- 旧 Swift/Kotlin binding 搭配新 native library；
- 新 binding 搭配旧 native library；
- 不兼容的 UniFFI scaffolding 版本；
- method 参数、返回值或 error shape 已发生不兼容变化。

### 8.1 两种 contract version 不相同

必须区分：

| 版本 | 当前示例值 | 责任 |
|---|---:|---|
| UniFFI scaffolding contract version | `30` | UniFFI generated binding 与 native scaffolding 自身的兼容性 |
| `BLEHUB_CONTRACT_VERSION` | `5` | BleHub backend capability 和 public command/event contract revision |

前者由 UniFFI 管理；后者由 BleHub 管理。它们不能互相替代。

## 9. 对纯 Rust 路径的影响

### 9.1 不会发生的开销

纯 Rust caller 直接调用：

```rust
hub.start_scan(command)?;
```

不会经过：

- C ABI wrapper；
- `RustBuffer` 序列化；
- `RustCallStatus`；
- API checksum；
- Swift/Kotlin converter；
- foreign callback handle。

`derive(Object)` 也不会向 `BleHub` struct 注入隐藏实例字段。

### 9.2 仍然存在的成本和约束

| 维度 | 影响 |
|---|---|
| 纯 Rust direct-call 热路径 | 基本无额外 UniFFI runtime 开销 |
| 编译 | 需要编译 UniFFI runtime 与 proc macros |
| Native image | 包含 wrapper、converter、metadata、checksum 和通用 scaffolding symbols |
| 最终 Rust-only binary | Link-time dead stripping 可能移除未使用内容，但不能假定与无 UniFFI 时完全同体积 |
| Public API shape | 需要 FFI 可表达的 DTO/error，并使用 `Arc` object ownership 等模型 |
| Foreign call path | 承担 handle clone/free、lower/lift、error/panic boundary 和 callback glue 成本 |

`Arc<BleHub>` 的引用计数、FFI-safe DTO shape 等属于公开 API 与 ownership 设计成本，不应错误归因成每次 `derive(Object)` 自动插入的隐藏工作。

## 10. 标记分别生成什么

| Rust 标记 | 主要结果 | 不单独负责 |
|---|---|---|
| `#[derive(uniffi::Object)]` | Object type metadata、`Arc<T>`/opaque-handle converter 支持、clone/free 所需类型机制 | 不直接生成完整 Swift/Kotlin 文件；不导出所有方法 |
| `#[uniffi::export] impl T` | 每个公开 method 的 C ABI wrapper 和 method metadata | 不负责 package staging |
| `#[uniffi::constructor]` | Constructor wrapper、返回 object handle 和 constructor metadata | 不负责普通未导出 constructor |
| `#[uniffi::export(foreign)] trait` | Foreign callback interface、proxy、callback registration/dispatch glue | 不实现平台 backend 本身 |
| `uniffi::setup_scaffolding!()` | Component-level scaffolding、contract version 和通用 runtime entry points | 不定义 BleHub domain contract |
| `uniffi-bindgen` | 从已编译 metadata 生成 C header 与 Swift/Kotlin source | 不重新定义或验证 BLE 业务语义 |

## 11. 维护约束

1. Generated `blehubFFI.h`、`blehub.swift` 和 `blehub.kt` 是候选产物，不手工编辑；
2. Canonical API 变化只修改 `crates/blehub` 中的 Rust contract，然后从同一个 final native image 重新生成 bindings；
3. `crates/blehub-uniffi` 只负责最终 native image、bindgen 入口和 package staging，不复制 canonical API；
4. Binding/native checksum 通过不代表 BleHub domain contract 正确，仍需 Rust conformance、foreign runtime、platform deterministic、artifact consumer 和 RF/HIL gates；
5. 纯 Rust 测试通过也不证明 wrapper、callback ownership、Swift ARC、Kotlin/JNI 或 package loading 正确。

## 12. 已验证证据与结论强度

本文的 wrapper、header、Swift converter、checksum 和 metadata symbol 示例来自当前本地 generated Apple production candidate：

- [`blehubFFI.h`](../../crates/blehub-uniffi/generated/apple-production/BleHubUniFfiNative.xcframework/macos-arm64_x86_64/Headers/blehubFFI.h)
- [`blehub.swift`](../../crates/blehub-uniffi/generated/apple-production/Sources/BleHubUniFfi/blehub.swift)
- `.work/blehub-uniffi-apple-production/libraries/macos/libblehub_uniffi.a` 的 native symbol inspection

由这些本地产物可以确认：

- `BleHub` 在 foreign binding 中确实表示为 `UInt64` opaque handle；
- final native image 确实导出 constructor、method、clone/free、checksum 和 component contract-version symbols；
- bindgen 确实生成 lower/lift、typed error、lifecycle 和初始化校验代码；
- 这些结论适用于当前锁定的 Stable UniFFI `0.32.0` candidate，不应无条件外推到其他 UniFFI 版本。
