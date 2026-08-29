# BoltFFI 原生后端与 Rust 直连平台 API 的技术评估

- 状态：架构调研证据与推荐；最终决策以 `.plan/PLAN.md` 为准
- 日期：2026-08-28
- 适用范围：BleHub BLE Central 后端选型
- 重点平台：Android、iOS、macOS
- 相关计划：[`.plan/PLAN.md`](../../.plan/PLAN.md)
- 本地平台证据库：[`docs/platform-api-reference/`](../platform-api-reference/README.md)
- BoltFFI 源码：workspace `D:\projects\contrib\boltffi`
- 并发与生命周期设计：[BleHub FFI 生命周期与并发竞态设计](ffi-lifecycle-concurrency.md)
- ingress workaround 与迁移：[BoltFFI event ingress：理想 handle 与 v0.1 token workaround](boltffi-event-ingress-workaround.md)

## 1. 评估问题

BleHub 已确定采用以下总体模型：

```text
调用方
  -> BleHub 统一命令 API
  -> BleBackend trait object
  -> 平台 BLE API

平台 BLE callback
  -> v0.1 push_backend_event(token, BleEvent)
  -> token registry 解析 Weak ingress state
  -> BleHub 事件桥
  -> 调用方提供的 BleEventSink
```

需要决定的是 Android 与 Apple 平台的 `BleBackend` 应如何实现：

1. Kotlin/Swift 直接调用平台原生 BLE API，通过 BoltFFI 实现 Rust 定义的 trait object；
2. Rust 直接调用 JNI/Objective-C/CoreBluetooth；
3. 在 BleHub 后端之下适配 `btleplug`、`bluest` 等现有 Rust BLE 库；
4. 按平台混合选择，而不是要求所有平台使用同一种实现语言。

这不是单纯比较“哪种语言更统一”。BleHub 的约束决定了评估重点：

- core 不拥有 async runtime、executor、线程、mailbox、channel 或 timer；
- backend trait 使用普通同步 `fn`；
- command 的同步 `Result` 只表达是否接管投递；
- native operation 的最终结果通过事件表达；
- command ID、timeout、取消策略和业务状态由调用方拥有；
- GATT service/characteristic/descriptor 必须保留稳定 identity，不能只按 UUID 合并；
- Android 优先级高于 Apple 和 Linux；
- FFI 生命周期和线程安全必须可验证，不能只依赖接口“看起来能生成”。

## 2. 结论摘要

建议采用按平台混合的实现方案：

| 平台 | 建议生产后端 | 结论 |
|---|---|---|
| Windows | Rust + `windows-rs` | 保持当前计划 |
| Android | Kotlin + Android Framework BLE + BoltFFI | 强烈推荐 |
| iOS | Swift + CoreBluetooth + BoltFFI | 推荐，但须通过 Phase 0 生命周期门槛 |
| macOS | 与 iOS 共用 Swift + CoreBluetooth backend | 推荐，避免过早维护两套 Apple backend |
| Linux | Rust + `dbus-rs` + BlueZ D-Bus | 保持当前计划，排在后续阶段 |

不建议把 `btleplug` 或 `bluest` 直接作为 BleHub v0.1 的生产 backend。

核心原因不是这些库质量不足，而是它们的抽象责任和 BleHub 不同：

- 现有跨平台 Rust BLE 库通常拥有 async/stream 执行模型；
- 它们可能拥有自己的对象状态、设备缓存、任务或事件流；
- 它们的平台统一模型可能丢失 BleHub 需要公开的原生差异；
- BleHub 需要 caller-owned command ID 和明确的 dispatch/event 两阶段语义；
- 再包一层会同时维护 BleHub contract 与上游库 contract，反而比直接做薄 backend 更重。

但该推荐目前是 **Conditional Go（有条件通过）**，不是无条件冻结 BoltFFI ABI。必须先验证 BleHub 特有的双向长期对象图：

```text
Rust BleHub
  -> 长期持有 Kotlin/Swift BleBackend trait object
  -> foreign backend 长期持有 opaque BackendEventIngressToken
  -> process-local registry 保存 token 到 Weak ingress state
  -> native callback 跨 FFI 调用 push_backend_event
  -> Rust push 同步调用 Rust application 的 BleEventSink trait object
  -> dispose、token unregister 与迟到 callback 并发
```

BoltFFI 已经分别具备 trait callback、data token、top-level exported function、Kotlin/Swift binding、JNI 和 Apple packaging 等基础能力；但“分别存在”不等于上述完整组合已经安全。Phase 0 必须把 token 对象图作为硬 Go/No-Go 门槛。理想 direct-handle 参数当前受 [issue #869](https://github.com/boltffi/boltffi/issues/869) 阻塞，但不再阻塞 v0.1 token 实施。

## 3. 证据边界与可信度

### 3.1 已经本地化的官方平台证据

平台 API 资料已经下载并锁定在 [`docs/platform-api-reference/`](../platform-api-reference/README.md)：

| 平台 | 本地资料 | 用途 |
|---|---|---|
| Windows | `windows/` | WinRT watcher、device、GATT operation、status 与 close 语义 |
| Android | `android/` | Android 36 Bluetooth Framework Java 源码与 Javadocs |
| Apple | `apple/` | CoreBluetooth manager、peripheral、delegate 与 operation 文档 |
| Linux | `bluez/` | BlueZ Adapter、Device、GattCharacteristic 等 D-Bus API |

Android 生产代码使用 Kotlin，不改变 Android BLE Framework API 由 Java class/interface 定义的事实。Kotlin 直接调用同一 API；因此本地 Java Framework source 是分析返回值、callback、权限和错误语义的权威证据。

### 3.2 已经本地化的 BoltFFI 证据

BoltFFI 源码已经在 workspace 中，可直接检查：

```text
D:\projects\contrib\boltffi
```

本次评估使用的证据类别包括：

- trait/callback lowering；
- Rust class handle lowering；
- Kotlin/Swift 生成绑定；
- Android JNI runtime 与 callback 处理；
- Apple retain/release 和 Swift wrapper；
- typed error 映射；
- Apple XCFramework/SPM packaging；
- Android 生成物与示例 consumer；
- Kotlin/Swift 平台测试。

例如，BoltFFI 本地源码中已存在：

- callback trait lowering：`boltffi_binding/src/lower/callbacks.rs`；
- class handle lowering：`boltffi_binding/src/lower/classes.rs`；
- Apple local/remote Swift Package 模板：
  - `boltffi_cli/templates/ApplePackageLocal.swift`；
  - `boltffi_cli/templates/ApplePackageRemote.swift`；
- Apple consumer tests：`examples/platforms/apple/Tests/DemoConsumerTests/`。

这些证据能够证明 BoltFFI 的组成能力，但不能替代 BleHub 对精确对象图、精确 DTO 和 dispose race 的集成验证。

### 3.3 仍然必须通过 spike 验证的部分

以下内容目前不能写成“已经保证”：

- Kotlin/Swift backend 接收并长期保存 Rust exported class wrapper 的完整生命周期；
- foreign backend、Rust event ingress 与 Rust downstream sink 的混合对象图能否完全释放；
- 任意 native callback thread 调入 Rust ingress，再同步调用 Rust sink 的稳定性；
- push 与 dispose 并发时是否可能 UAF、double release 或死锁；
- Kotlin unchecked exception、Swift error、Rust panic 的实际边界行为；
- BleHub 大型 `BleEvent` enum/record 生成 API 的可维护性；
- 高频 notification payload 从 native→Rust 的延迟和分配成本；
- Android 最终 AAR consumer 和 Apple XCFramework/SPM consumer 的干净集成。

因此，本报告的“推荐”是一项待实验放行的架构选择，而不是对 BoltFFI 风险的忽略。

## 4. 为什么 Android 应优先使用 Kotlin + BoltFFI

### 4.1 Android 没有可替代 Framework BLE 的公开 NDK Central API

Android 应用侧 BLE Central/GATT Client 的公开入口是 Android Framework Bluetooth API。核心类型包括：

- `BluetoothAdapter`；
- `BluetoothLeScanner`；
- `ScanCallback`；
- `BluetoothDevice`；
- `BluetoothGatt`；
- `BluetoothGattCallback`；
- `BluetoothGattService`；
- `BluetoothGattCharacteristic`；
- `BluetoothGattDescriptor`。

这些 API 的真实本地源码在：

```text
docs/platform-api-reference/android/
```

即使业务 backend 主体写成 Rust，也不能绕开 JVM API。Rust 方案最终仍然需要 JNI 与 Java/Kotlin callback 对象。

### 4.2 `BluetoothGattCallback` 天然适合 Kotlin backend

Android BLE operation 不是简单的同步函数调用：

- connect 后由 connection-state callback 报告结果；
- service discovery 由 callback 完成；
- characteristic read/write 由 callback 完成；
- descriptor write 用于 CCCD 配置；
- notification value 由 characteristic-changed callback 到达；
- RSSI read 由 callback 完成。

`BluetoothGattCallback` 是 Java abstract class，而不是 Rust 能直接实现的 C ABI interface。Kotlin 可以自然地：

1. 继承 callback class；
2. 保存 `BluetoothGatt` 和 GATT object identity；
3. 在 callback 中映射为统一 `BleEvent`；
4. v0.1 调用 BoltFFI 生成的 top-level `push_backend_event(token, event)`；未来 #869 修复后可迁移为 `BackendEventIngressHandle.push`。

若全部在 Rust 中手写 JNI，需要自行处理：

- JVM 获取和线程 attach/detach；
- local/global/weak global reference；
- Java exception 检测与清除；
- class lookup 与 Android classloader；
- callback subclass 或桥接 Java class；
- API level 差异；
- nullable/platform type；
- ProGuard/R8 keep rules；
- AAR/JNI library packaging；
- native thread 与 Handler/Looper thread；
- `BluetoothGatt.close()` 和迟到 callback 的竞争；
- GATT operation serialization。

这套工作不会让 BleHub 更薄，只会把原生集成职责从 Kotlin 移到更难审计的 JNI glue。

### 4.3 Kotlin 不等于 BleHub 引入 runtime

Android Framework 和应用本身已经有 Binder、Looper、Handler/Executor 和 framework callback thread。BleHub 的“无 runtime”原则应解释为：

- BleHub core 不创建自己的 runtime/thread/executor；
- BleHub 不要求 Tokio；
- backend 使用平台已经要求的 callback/Handler 机制；
- backend 在必要时把 BLE GATT operation 约束到其选择的串行上下文；
- downstream 自行决定事件最终投递到哪个执行环境。

因此 Kotlin backend 可以使用调用方提供的或平台要求的 Android execution context，同时仍然满足 core 无 runtime。

### 4.4 Android GATT operation queue 仍属于 backend

“BleHub 无状态”不等于 backend 完全无状态。Android backend 必须保留足以正确驱动 Framework API 的状态，例如：

- `BluetoothGatt` 实例；
- connection handle 到 GATT object 的映射；
- service/characteristic/descriptor native object identity；
- 当前 in-flight GATT operation；
- 后续待投递 operation；
- callback 与 caller-owned `CommandId` 的相关信息；
- notification 本地 routing 状态；
- disposed/closed 状态。

这些是平台适配状态，不是 BleHub 的 canonical device/business state。将它们放在 Kotlin backend 比强行放进 Rust core 更符合职责边界。

### 4.5 Android API 的立即返回不等于 operation 完成

本地 Android 36 源码显示部分现代 API 返回 `BluetoothStatusCodes`，旧 API 中也常见 boolean immediate return。这些值需要按 operation 逐个解释。

BleHub 应统一成：

```text
command method 的 Result
  = backend 是否接受并接管命令

后续 CommandCompleted / 专用事件
  = native operation 的最终结果
```

例如 API 33+ characteristic write 返回 `SUCCESS`，表达请求被 Framework 接受，不等于 remote GATT write 已完成。最终结果仍来自 `BluetoothGattCallback`。

Kotlin backend 直接位于 API 和 callback 之间，最容易准确维护这种映射。

### 4.6 Android 方案比较

| 方案 | 平台契合 | BleHub 契合 | 主要问题 | 综合判断 |
|---|---:|---:|---|---|
| Kotlin + Framework BLE + BoltFFI | 高 | 高 | BoltFFI 双向生命周期需 spike | 推荐 |
| Rust + 手写 JNI + 少量 Kotlin callback | 中 | 中 | 大量 JNI、引用、线程和打包工作 | 不作为首选 |
| 适配 `btleplug` Android backend | 中 | 低 | async/runtime、内部抽象与 identity/command 模型冲突 | 不推荐生产使用 |
| 纯 NDK Rust BLE | 不可行 | 不适用 | 没有公开应用级 NDK BLE Central API | 排除 |

近似适配度：

| Android 选项 | 适配度 |
|---|---:|
| Kotlin + BoltFFI | 9/10 |
| 手写 Rust JNI | 5/10 |
| `btleplug` adapter | 3/10 |
| 纯 NDK Rust BLE | 0/10 |

该分数是架构适配度，不是库质量评分。

## 5. 为什么 Apple 优先使用 Swift + BoltFFI

### 5.1 CoreBluetooth 是 delegate/queue 驱动模型

Apple BLE Central 的核心对象包括：

- `CBCentralManager`；
- `CBCentralManagerDelegate`；
- `CBPeripheral`；
- `CBPeripheralDelegate`；
- `CBService`；
- `CBCharacteristic`；
- `CBDescriptor`。

本地官方文档位于：

```text
docs/platform-api-reference/apple/
```

CoreBluetooth operation 通过 manager/peripheral delegate 返回结果。Swift 原生实现可以直接表达：

- manager state；
- scan discovery；
- connect/fail/disconnect；
- service/characteristic/descriptor discovery；
- read/write completion；
- notification state 和 value update；
- RSSI result；
- CoreBluetooth object identity。

### 5.2 Swift 减少 Objective-C runtime glue

Rust 可以通过 `objc2` 生态调用 Objective-C Framework，包括 CoreBluetooth；这条路线技术上可行。但完整 backend 还需要处理：

- Objective-C delegate class 定义；
- ARC 对象的 strong/weak ownership；
- delegate 生命周期；
- `Retained` 等 Rust wrapper；
- dispatch queue confinement；
- CoreBluetooth 对象并非普通 `Send + Sync` 值；
- callback 进入 Rust 时的对象图和线程约束；
- 必要的 `unsafe` 封装与人工不变量。

这相当于在 Rust 中重建 Swift/Objective-C 已经自然提供的语言层集成。

Swift backend 可以把 CoreBluetooth 对象限制在一个明确的 dispatch queue，向 BleHub 暴露线程安全的 command entry，并在 v0.1 通过 opaque token + exported free function 推送 value DTO；未来可迁移为 BoltFFI class handle。这样 unsafe 留在生成边界，token/registry 生命周期由 Rust guard 和 Swift backend 的显式 dispose contract 共同验证。

### 5.3 `BleBackend: Send + Sync` 是行为契约

Rust trait 上的 `Send + Sync` 不会自动让 Swift/Kotlin 对象线程安全。它要求 foreign backend 的实现满足等价行为：

- command method 可从多个调用线程进入；
- shared mutable state 必须受锁或 serial queue 保护；
- CoreBluetooth object 只能在约定 queue 使用；
- callback 与 dispose 的并发必须定义；
- foreign wrapper 的 retain/release 必须覆盖所有在途调用。

Swift 6 strict concurrency 下，backend 可能需要一个经过审计的 `@unchecked Sendable` 边界。这个标记只能放在非常窄的 wrapper 上，并由以下事实支撑：

- 所有 CoreBluetooth state 都 queue-confined；
- public method 只向该 queue 投递；
- close/dispose 与 callback 使用同一个 lifecycle gate；
- v0.1 ingress token 的 bind/dispose 顺序明确，未来 direct handle 的 retain/release 顺序同样明确。

不能因为 BoltFFI 生成了 protocol 就把 `Send + Sync` 当成已经验证。

### 5.4 iOS 与 macOS 应优先共用一个 Swift backend

CoreBluetooth 在 iOS 和 macOS 上具有共同的 manager/peripheral 模型。首版应尽量：

- 共用 DTO mapping；
- 共用 command queue 和 callback correlation；
- 共用 object identity 管理；
- 用小型条件分支处理 availability、entitlement、权限和平台行为差异；
- 共用 BoltFFI 生成 bindings 和 Apple packaging 流程。

过早建立一套 Swift iOS backend 和一套 Rust macOS backend 会制造两个行为实现、两套 race 和两套测试矩阵。只有出现明确的纯 Rust macOS consumer 需求，或 BoltFFI Apple 路径未通过门槛时，才重新评估 Rust direct backend。

### 5.5 Apple direct-Rust 仍是可信 fallback

与 Android 不同，Apple 上存在比较完整的 Rust Objective-C binding 生态。可评估的基础包括：

- `objc2`；
- `objc2-core-bluetooth`；
- `dispatch2`。

这意味着 Swift + BoltFFI 失败后，Apple 不必退回手写 C wrapper；可以实现 queue-confined Rust wrapper。但该方案成本包括：

- delegate class 与 Objective-C method signature；
- ARC/weak delegate/object graph；
- `!Send`/`!Sync` native object confinement；
- 较多 `unsafe`；
- iOS/macOS target 和 framework linking；
- 与 Swift consumer 的最终 packaging。

因此它应是 fallback，而不是在 BoltFFI spike 前并行实现的第二套 backend。

### 5.6 Apple 方案比较

| 方案 | iOS 契合 | macOS 契合 | 主要问题 | 综合判断 |
|---|---:|---:|---|---|
| Swift + CoreBluetooth + BoltFFI | 高 | 高 | 双向生命周期和 strict concurrency 需 spike | 推荐 |
| Rust + `objc2` | 中高 | 中高 | delegate/ARC/queue/unsafe 成本 | 可行 fallback |
| 适配 `bluest` | 中 | 中 | async facade、内部状态与 BleHub contract 适配成本 | 参考实现优于直接依赖 |
| 适配 `btleplug` | 低 | 低至中 | async/runtime 和统一模型不匹配 | 不推荐 |

近似适配度：

| Apple 选项 | iOS | macOS |
|---|---:|---:|
| Swift + BoltFFI | 8.5/10 | 8/10 |
| Rust + `objc2` | 6.5/10 | 7/10 |
| `bluest` adapter | 5.5/10 | 6/10 |
| `btleplug` adapter | 3.5/10 | 4/10 |

## 6. Windows 与 Linux 为什么仍然适合 Rust backend

### 6.1 Windows

Windows backend 使用 `windows-rs` 的优势：

- WinRT metadata 到 Rust 的映射成熟；
- 不需要额外的 JVM/Swift runtime 边界；
- 与 Rust core 同一语言；
- Windows 是首个开发平台，FakeBackend 和真实 backend 都容易在当前主机上测试；
- 可以直接把 WinRT async operation 的完成转换为 BleHub event，而不把 async runtime 暴露给 core。

需要注意：WinRT API 本身有异步 operation 和事件对象。所谓“非 async trait”不是要求底层平台同步执行，而是要求 backend 在内部接管 operation，并最终通过 event 汇报。

### 6.2 Linux

Linux backend 使用 Rust + `dbus-rs`/BlueZ D-Bus 的优势：

- D-Bus 是 BlueZ 对应用公开的主要控制面；
- Rust 可直接管理 proxy、object path、signal subscription 和 method reply；
- 不需要再引入另一种 native implementation language；
- 与 Windows backend 形成 Rust-native 的对称实现。

Linux 环境和 USB HIL 当前明确后置，不应阻塞 Windows/Android v0.1。

## 7. 为什么不直接选择跨平台 Rust BLE 库

## 7.1 抽象层级重复

BleHub 自己要定义：

- stable IDs；
- command DTO；
- event DTO；
- dispatch/result 语义；
- backend capability；
- lifecycle/close 规则；
- native error mapping；
- duplicate UUID object identity。

若在其下再放一个跨平台统一 BLE 库，会形成：

```text
BleHub 统一 contract
  -> adapter/correlation layer
  -> 第三方统一 BLE contract
  -> 第三方平台 backend
  -> native API
```

中间 adapter 必须把第三方库的 async future/stream、对象生命周期和错误模型重新转换回 BleHub command/event contract。这样并没有减少 BleHub 的核心难点。

### 7.2 async/runtime 不匹配

BleHub 的 core 不选择 runtime，第三方库若返回 async future 或 stream，就必须有人 poll：

- BleHub 自己启动 runtime：违反当前约束；
- 要求调用方提供特定 runtime：污染统一 API；
- 每个平台 backend 内部启动 runtime：隐藏线程和 shutdown 复杂度；
- 写自定义 poller：重新实现 runtime 责任。

如果某个库内部已拥有执行线程，即使外层包装成同步 trait，也只是隐藏了 runtime，而不是消除了集成成本。

### 7.3 identity 与能力模型可能被压平

BLE service、characteristic 和 descriptor 可能出现重复 UUID。BleHub 计划使用 opaque stable ID 指向具体 native object。第三方库若只暴露 UUID 查找或重建对象集合，adapter 需要额外 identity state。

类似地，各平台存在以下差异：

- adapter/permission/power state；
- scan start/stop completion；
- connect/disconnect 语义；
- without-response write completion；
- notification 与 indication 配置；
- RSSI 支持；
- passive disconnect/scan stop；
- close 是否代表物理链路断开。

BleHub 需要保留这些差异的诚实表达，而不是为了统一而声明过强保证。

### 7.4 仍然可以把现有库作为参考

不直接依赖不等于忽略生态。现有库的价值包括：

- 平台 API 调用顺序参考；
- delegate/callback ownership 参考；
- error/status mapping 参考；
- platform bug/workaround 线索；
- target/linking/entitlement 配置参考；
- 测试场景参考。

但按项目规则，若要对大型外部库做实现级审计，应先请用户把对应仓库和明确 revision 放入 workspace，再用 codegraph 检查源码，而不是临时在线抓取后形成不可复现结论。

## 8. BoltFFI 路线的主要收益

### 8.1 原生语言直接访问平台 API

Kotlin 对 Android Framework、Swift 对 CoreBluetooth 是平台惯用路径。它们天然支持：

- callback/delegate inheritance；
- native object ownership；
- platform availability annotation；
- Android Handler/Executor；
- Apple dispatch queue；
- Android permission与 lifecycle；
- Apple ARC 与 framework linking。

BoltFFI 只承担 BleHub contract 的跨语言传递，不试图替代 native platform framework。

### 8.2 保持 callback-first 的 fire-and-forget 模型

平台 operation 可以按以下方式映射：

```text
BleBackend.command(command DTO)
  -> 本地校验
  -> native API 接受/投递
  -> 返回 Ok(())
  -> native callback
  -> 构造 BleEvent
  -> push_backend_event(token, event)
```

这与 Android `BluetoothGattCallback` 和 Apple delegate 模型一致，无需先转成 future，再转回 event。

### 8.3 不强迫 BleHub 选择 runtime

BoltFFI 的同步 foreign backend trait、data token 与 exported top-level push function可以表达 v0.1 command/event ingress；#869 修复后可在不改变 Hub 装配语义的前提下替换为 exported Rust ingress handle。平台 backend 可以使用已有 native queue；Rust application 可以在 `BleEventSink` 内选择：

- Rust channel；
- actor；
- Tokio/async-std 或其他调用方 runtime；
- 完全同步的测试 collector；
- 调用方自己拥有的 Rust adapter。

`BleEventSink` 是固定的 Rust-only contract，不由 Kotlin/Swift 实现，BleHub 也不提供 native consumer adapter。若下游项目需要向 native application 暴露事件，由下游项目在 BleHub 之外自行使用 BoltFFI 或其他 FFI 包装其 Rust application/sink。

### 8.4 生成统一 DTO 与 typed error

BoltFFI 可以生成 record/enum/error/trait/class 对应绑定，减少手写 JNI/C ABI DTO 的数量。Rust contract 保持统一，native backend 负责映射平台类型。

但应控制 DTO：

- FFI wire 使用 BoltFFI 最稳定的 value shape；
- 不把 `windows-rs`、Android、CoreBluetooth 或 D-Bus object 泄漏到公共 API；
- 高频 payload 首版使用明确的 byte vector；
- native object 用 stable opaque ID 表示；
- error 中保留 platform/category/code/message，而不跨 FFI 传 native exception object。

### 8.5 已有跨平台组成能力

本地 BoltFFI 源码和测试已经覆盖多个组成部分：

- foreign trait callback；
- class handle；
- Kotlin/Swift wrapper；
- typed `Result`/error；
- Apple package 模板；
- BoltFFI 自身的 native consumer tests。

这些是 BoltFFI 作为 backend/ingress FFI 工具的证据，不表示 BleHub 要提供 native application consumer API。这使 BoltFFI 成为合理的首选实验对象，优于从零设计专用 JNI + Swift C ABI。

## 9. BoltFFI 路线的主要风险

### 9.1 双向强引用环

最危险的对象图是 ingress routing 反向强持有 Hub/state，或 backend 直接强持有 downstream sink。v0.1 推荐结构已经收敛为：

```text
BleHub
  strong -> foreign BleBackend
  value -> cached BackendCapabilities
  strong -> Arc<EventIngressState>

EventIngressState
  open phase strong -> downstream BleEventSink

BleHub
  owns -> BackendEventIngressRegistration

process-local registry
  token -> weak EventIngressState

foreign backend
  value -> BackendEventIngressToken
```

close 在 state 的 downstream mutex 下排空 callback并 `take()` sink，再 dispose backend、注销 token entry；registry entry 只持 `Weak`，不会延长 Hub/downstream 生命周期。ArcSwap snapshot guard 在 weak upgrade 和 downstream call 前释放。完整 ownership、锁顺序和竞态矩阵见 [BleHub FFI 生命周期与并发竞态设计](ffi-lifecycle-concurrency.md)，token 细节见 [workaround 文档](boltffi-event-ingress-workaround.md)。准确结构仍必须通过 registry/drop/deinit counter 验证，不能只通过代码阅读推断。

### 9.2 dispose 与迟到 callback

平台 callback 可能在 logical close 后到达。BleHub 需要定义：

1. `dispose` 把 bridge 标为 closed；
2. 新 command 被拒绝；
3. backend 开始注销 callback/释放 native handle，此时 token entry 仍解析到 closed state；
4. 已经在途的 callback 仍可调用 `push_backend_event`；
5. ingress 看到 closed 后返回 `EventIngressStatus::Closed`；
6. backend dispose 后注销 token entry，后续 lookup miss 同样返回 `Closed`；
7. 不再调用 downstream；
8. registry entry 和所有 foreign/Rust owners 最终释放。

这要求 push 与 dispose 都不能持有会造成 foreign re-entry 死锁的锁。

### 9.3 `Send + Sync` 不能由生成器替 foreign code 保证

Rust 编译器只能验证 Rust wrapper 的自动 trait 条件，不能证明 Kotlin/Swift implementation 真的线程安全。因此需要：

- Android backend 内部 lock/Handler serialization；
- Apple backend dispatch queue confinement；
- 多线程 command stress；
- callback 与 close race；
- 文档化 callback thread 与 reentrancy；
- 禁止下游 callback 重入同一 backend lock。

### 9.4 unchecked exception

即使 Rust trait method 在 contract 中“不抛异常”，Kotlin 仍能抛 unchecked exception，Swift wrapper 或生成代码也可能遇到不可预期错误。

风险位置包括：

- backend command callback 抛异常；
- backend `bind_event_ingress(token)` 抛异常；
- exported `push_backend_event` 的 DTO 转换或分配失败；
- dispose 时 native code 抛异常。

必须验证 BoltFFI 生成边界的实际行为。Rust downstream 不会抛 Kotlin/Swift exception；其 typed `Err` 与 panic containment 在 Rust ingress 内处理。任何 native exception 都不能穿越 Rust ABI 或破坏 callback registry/refcount。

### 9.5 Rust panic

Rust panic 不能穿越 FFI 边界。`BleEventSink::on_event` contract 要求实现不得 panic，所有可预期失败返回 `Result<(), EventSinkError>`；该 message 通过 `DownstreamFailed { message }` 返回 backend 诊断，但不是稳定机器协议。开放的 Rust trait 无法在类型系统中证明实现绝不 panic，因此还必须明确：

- library panic strategy；
- generated synchronous exported trampoline 是否统一捕获 panic；
- 若生成层没有 containment，v0.1 是否只在最外层 `push_backend_event` trampoline 增加 guard；未来 direct-handle 路径对应放在 `BackendEventIngressHandle::push`；
- contract-violation panic 的 process abort/FFI failure 策略，且不得伪装成普通 downstream rejection。

Phase 0 应做 fault injection，而不是只检查 happy path。

### 9.6 高频事件有一次 native→Rust 语言边界

v0.1 路径是：

```text
native BLE callback
  -> push_backend_event(token, event)
  -> registry lookup and Weak upgrade
  -> Rust ingress state
  -> Rust downstream sink
```

高频 notification 只在 native backend→exported Rust push 发生一次 FFI crossing，随后是短 registry lookup 和普通 Rust trait 调用。ArcSwap snapshot guard 必须在进入 per-Hub state 前释放。该 crossing 仍需转换 event enum、IDs、metadata 和 payload。潜在成本：

- byte buffer copy；
- wrapper allocation；
- enum/record encoding；
- JNI local/global refs；
- ARC retain/release；
- lock contention；
- downstream 同步处理阻塞 platform callback queue。

这不一定是性能问题，但必须测真实 payload，而不是只 benchmark 空 callback。

### 9.7 Android packaging 仍由 BleHub 负责

BoltFFI 能生成 Android 所需的 Kotlin/JNI/native library 组成物，不应假设它自动完成 BleHub 最终发布所需的所有 Gradle/AAR 工程策略。

BleHub 仍需定义并验证：

- Gradle plugin 和 Kotlin version；
- ABI splits/jniLibs；
- Rust target artifacts；
- generated Kotlin source placement；
- consumer ProGuard/R8 rules；
- manifest/permission 声明；
- API level；
- AAR publish metadata；
- clean sample application consumption。

### 9.8 生成 ABI 的演进风险

公共 trait 增加 method、调整 method 顺序、修改 enum variant 或 DTO 字段，都可能改变生成 header/vtable/API。BleHub 应把生成结果纳入 review：

- Rust contract snapshot；
- C header/ABI snapshot；
- Kotlin generated API snapshot；
- Swift generated API snapshot；
- symbol list；
- consumer compile test。

在 Phase 0 通过前，不冻结 v0.1 public ABI。

## 10. `capabilities()` 的特殊风险与已采纳方案

原草案的foreign callback使用infallible value return：

```rust
fn capabilities(&self) -> BackendCapabilities;
```

如果Kotlin/Swift implementation意外抛异常，Rust侧没有typed failure channel。Plan现已采纳一次性typed assembly query：

```rust
fn capabilities(&self) -> Result<BackendCapabilities, BleHubError>;
```

`BleHub::new`在bind前只调用一次，校验后把immutable value缓存到Hub；public`BleHub::capabilities()`只返回缓存，不再跨FFI，也不引入capability-changed event。query失败或DTO非法使constructor best-effort dispose inert backend并返回`BleHubError`。

该方案保留`BleHub::new(backend, downstream)`单一装配API，不要求application额外传一份可能与backend不一致的metadata，也避免设计`RegisteredBackend`包装。收益：

- capability仍是backend type/platform contract的装配metadata，不是动态adapter state；
- foreign invocation和DTO conversion具有typed failure channel；
- capability可在backend进入运行态前验证；
- facade缓存immutable value，不产生重复FFI调用或第二套metadata真值。

Phase 0仍必须验证这一foreign trait `Result<data,error>`的Kotlin/Swift生成shape与未声明exception行为。

## 11. Phase 0 必须升级为硬 Go/No-Go 门槛

## 11.1 精确测试对象图

Phase 0 不应只验证“BoltFFI 能生成 trait”。必须完整运行：

```text
foreign backend + Rust downstream sink
  -> BleHub::new queries/validates/caches capabilities exactly once
  -> BleHub::new creates state and never-reused token
  -> bind_event_ingress(token) exactly once
  -> register token to Weak state
  -> foreign backend 长期保存 token
  -> 多线程调用 commands
  -> backend 在 command 返回前同步 push_backend_event
  -> backend 从 Handler/DispatchQueue 异步 push_backend_event
  -> registry lock-free lookup releases snapshot guard
  -> Rust ingress 同步调用 Rust downstream sink
  -> 显式 dispose and unregister token
  -> 迟到 callback 得到 Closed
  -> backend、registry、ingress、sink 的计数回到 baseline
```

应使用接近最终 contract 的 DTO 和 `BleEvent`，而不是只传整数的玩具 demo。

## 11.2 Android spike

必须在 ART 上运行，不能只在桌面 JVM test 中验证。

测试线程来源：

- Java/Kotlin caller thread；
- main Handler thread；
- backend serial HandlerThread 或 Executor；
- framework-style callback thread；
- Rust 创建的 detached native thread 进入 JVM；
- push 与 dispose 并发线程。

测试场景：

- token bind 和 registration 成功；
- bind/registration partial failure；
- repeated bind 与 bind 期间违规 push；
- stale/missing token；
- command 返回前同步事件；
- command 返回后异步事件；
- Rust downstream sink re-entry；
- backend typed error；
- Kotlin unchecked exception；
- Rust downstream `EventSinkError`、`DownstreamFailed { message }` 生成 shape 与 panic；
- close 后迟到事件；
- 重复 dispose；
- 10,000 次 bind/dispose；
- token 不复用且 registry entry 回到 baseline；
- 进程退出前所有 handle/entry 归零；
- clean Gradle project 引用最终 AAR。

## 11.3 Swift spike

即使真实 CoreBluetooth backend 排在 Android 后面，Phase 0 也应创建 fake Swift backend 验证对象图。初期在 macOS Swift test 上运行即可；这比等到 iOS 阶段才发现 ABI/lifecycle 问题便宜得多。

要求：

- Swift 6 strict concurrency；
- backend 从多个 serial queue push；
- concurrent command callers；
- callback-before-return；
- Rust downstream sink callback；
- explicit dispose；
- late callback；
- push/dispose race；
- ARC `deinit` counter；
- Rust drop counter；
- 10,000 次 bind/dispose，token/registry/drop counter 回到 baseline；
- clean XCFramework/SPM consumer build。

若需要 `@unchecked Sendable`，必须在 spike 文档中列出保护该不变量的 queue/lock 和测试。

## 11.4 fault injection

至少覆盖：

- backend command 抛 unchecked exception；
- bind 或 registry registration 中途失败；
- stale token 与 unregister/push 竞争；
- Rust downstream对domain/terminal event返回`EventSinkError`，验证backend收到`DownstreamFailed { message }`、不重试当前event、清理correlation并继续后续event；
- Rust event conversion panic；
- Rust downstream panic；
- allocation/encoding failure 能模拟时进行模拟；
- dispose 中 native cleanup failure；
- callback 在最后一个 external owner release 后竞争进入。

目标不是让所有错误都可恢复，而是确认不会发生：

- unwind 穿越 FFI；
- UAF；
- double free/release；
- callback registry 泄漏；
- 永久死锁；
- command 返回 `Err` 后又产生该 ID 的事件。

## 11.5 性能与背压测试

使用真实 `BleEvent` shape 运行以下矩阵：

| 频率 | Payload |
|---:|---:|
| 20 Hz | 20 B |
| 100 Hz | 20 B、244 B |
| 1000 Hz | 20 B、244 B、1 KiB |

每组至少运行 60 秒，记录：

- ingress p50/p95/p99 latency；
- callback 总吞吐；
- CPU；
- allocation count 或可观察替代指标；
- RSS/heap 变化；
- JNI/ARC handle 数；
- close latency；
- concurrent close 时的 delivered/closed/rejected 数量；
- downstream 故意变慢时 platform callback 是否被阻塞。

BleHub core 不内建 queue，因此 v0.1 `push_backend_event(token, event)`（未来为 `BackendEventIngressHandle::push`）的同步语义必须写清楚。registry lookup lock-free，ArcSwap snapshot guard 必须在进入 state 前释放；downstream sink 自身应快速、non-blocking，并把事件投递到用户选择的 channel/Handler/actor。

最终阈值应在 Windows FakeBackend baseline 和 Android/Swift spike 后确定，避免在无测量前编造数字。Go/No-Go 至少要求：

- 无 crash/UAF/deadlock/leak；
- 目标产品预期 notification 频率下无持续 backlog；
- p99 和内存增长可被 caller 集成接受；
- close 不等待下游无限阻塞。

## 11.6 ABI 与生成 API snapshot

Phase 0 应提交并 review：

- Rust public contract；
- BoltFFI 生成 C header/vtable；
- Kotlin interface/class/DTO API；
- Swift protocol/class/DTO API；
- Android JNI symbol list；
- Apple exported symbol list；
- Android AAR 文件结构；
- Apple XCFramework/SPM 文件结构。

需要建立 recipe，在 contract 变化后重新生成并比较 snapshot。

## 12. Go/No-Go 判定

### 12.1 Go

满足以下条件才正式冻结 BoltFFI backend ABI：

- foreign backend 能安全长期持有 opaque token；
- `bind_event_ingress(token)` 和 top-level `push_backend_event` 生成 API 稳定；
- token 非零、永不复用，registry 只持 `Weak` 且 lifecycle 后回到 baseline；
- Kotlin/Swift 多线程 callback 稳定；
- Rust downstream sink 的 typed result、串行 delivery 与 close drain 稳定；
- dispose/late callback race 无 crash、泄漏或 double release；
- exception/panic 行为符合明确策略；
- generated DTO/trait API 可维护；
- Android AAR 和 Apple XCFramework/SPM consumer 构建通过；
- notification benchmark 满足产品负载；
- `Send + Sync` 的 foreign 行为不变量已文档化并经 stress test；
- 10,000 次 lifecycle loop 后 backend/state/sink/registry 所有计数回到 baseline；
- BoltFFI #869 expected-failure fixture 保留，未来 direct-handle 迁移边界已记录。

### 12.2 No-Go

任何一项出现都应暂停冻结 ABI：

- token collision/reuse、stale token 误投或 registry entry 泄漏；
- ArcSwap snapshot guard 跨 downstream call，或 RCU registry造成不可接受的全局阻塞/内存增长；
- cross-thread push crash 或 JVM attach 不稳定；
- dispose 后 callback 可能 UAF；
- handle/refcount/registry entry 无法回到 baseline；
- unchecked exception 会破坏 registry/refcount 或穿越 ABI；
- Rust panic 行为不可接受；
- generated event/API 过于庞大或不稳定；
- Android/Apple consumer packaging 不能可靠复现；
- 目标 notification 负载明显超过可接受预算；
- 必须在 BleHub core 内加入 runtime/thread 才能让设计工作。

No-Go 不代表推翻整个跨平台 contract，只表示 trait-object + token ingress 的具体 FFI shape 需要进一步降级。BoltFFI #869 本身不是 v0.1 token No-Go；它只阻塞未来删除 registry。

## 13. 失败后的退路顺序

如果 v0.1 token Phase 0 未通过，按以下顺序继续收缩，不要立刻切换到大型跨平台 Rust BLE 库：

### 13.1 第一退路：保留 Kotlin/Swift backend，缩窄 FFI

把双向 foreign trait object 改为显式 command/event facade：

```text
Rust/Native 固定 backend handle
+ 每个 command 的显式 exported function
+ 保留一个方向明确的 token event push function
+ 显式 retain/release/dispose
```

优点：

- 仍然使用最适合平台的 Kotlin/Swift；
- 避免复杂 callback vtable 或双向 trait ownership；
- ABI 更机械、更容易审计；
- BleHub 公共领域 DTO 可以保持不变。

代价是 binding 代码更显式、接口稍重复。

### 13.2 第二退路：Apple 使用 Rust + `objc2`

如果问题只发生在 Swift/BoltFFI 路径，可让 Apple backend 改为 Rust direct，同时 Android 继续 Kotlin。不要为了“实现语言统一”让 Android 承担手写 JNI 成本。

### 13.3 第三退路：评估专用平台 adapter

若某个成熟库刚好满足单一平台要求，可在用户把其源码仓库加入 workspace 后做实现级评估。评估必须覆盖 runtime、identity、callback、error、close 和 packaging，而不是只看公开 API。

### 13.4 不推荐的直接跳转

不建议在 BoltFFI spike 失败后直接：

- 把 Android 切成 `btleplug`；
- 为 Android 编写完整手工 Rust JNI backend；
- 把 Tokio 变成 BleHub 公共要求；
- 在 core 内新增全局 runtime、worker 或 event loop；
- 为了隐藏平台差异而削弱 command/event 语义。

FFI ownership 失败与 BLE domain contract 失败是两个不同问题，应只替换失败的边界。

## 14. 对主计划的建议修改

本调研建议在 `.plan/PLAN.md` 中正式加入以下决策：

1. 新增“BoltFFI 与 Rust-native backend 选型”章节；
2. 平台实现表保持：
   - Windows：Rust + `windows-rs`；
   - Android：Kotlin + Framework BLE + BoltFFI；
   - iOS/macOS：Swift + CoreBluetooth + BoltFFI；
   - Linux：Rust + `dbus-rs`；
3. 把该方案标记为 `Conditional Go`；
4. 把 Phase 0 从信息性 spike 升级为硬 Go/No-Go gate；
5. Phase 0 同时验证 Android fake backend 和 Swift fake backend；
6. 使用完整 BleHub object graph，而不是 toy callback；
7. 添加 exception/panic fault injection；
8. 添加 20/100/1000 Hz 真实 event benchmark；
9. 添加 generated API/header/vtable/packaging snapshot；
10. 明确 BoltFFI Android 输出不等于最终 AAR 发布流程全部完成；
11. 以constructor一次性typed query取得并缓存immutable`BackendCapabilities`，避免infallible或重复foreign query；
12. 记录 No-Go 后的 fallback 顺序。

在用户 review 并批准主计划前，这些仍是调研建议，不启动 BLE 实现。

## 15. 最终判断

从 BleHub 的目标看，“所有 backend 都用 Rust”不是最重要的一致性。真正需要一致的是：

- 统一 command DTO；
- 统一 event DTO；
- caller-owned IDs；
- dispatch 与 completion 分离；
- 明确的 platform capability；
- 诚实的 operation completion 语义；
- 可验证的 ownership/close/error contract。

平台 backend 使用最适合 native API 的语言，反而能让公共 Rust core 更薄：

```text
Windows/Linux：Rust 直接适配 native system API
Android：Kotlin 直接适配 JVM Framework BLE
Apple：Swift 直接适配 CoreBluetooth
```

BoltFFI 的价值是连接这些实现，而不是把所有平台强行变成 Rust。当前证据足以支持把 Kotlin/Swift + BoltFFI 作为首选方向，也足以说明不能跳过生命周期 spike。

因此最终建议是：

> 采用混合平台 backend；Android 使用 Kotlin，iOS/macOS 使用 Swift；BoltFFI 作为统一 trait/DTO/event 边界。架构状态为 Conditional Go，必须先通过完整双向对象图的 Phase 0 Go/No-Go 验证，再冻结公共 ABI并进入真实 BLE backend 实现。
