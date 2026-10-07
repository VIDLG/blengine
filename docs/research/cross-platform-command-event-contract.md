# BleHub 跨平台命令下行与事件上行契约

- 状态：架构调研与证据基线；最终约束以 [`.plan/PLAN.md`](../../.plan/PLAN.md) 为准
- 日期：2026-08-29
- 范围：Windows WinRT、Android Framework BLE、Apple CoreBluetooth、Linux BlueZ D-Bus
- 证据：[`docs/platform-api-reference/`](../platform-api-reference/README.md) 中的本地官方快照

## 1. 结论摘要

BleHub 的命令下行和事件上行必须逐 operation、逐平台对照官方 API；这是公共契约冻结前的必要工作。但需要区分三类不同性质的结论：

1. **平台事实**：native method 的实际返回类型、callback/delegate/D-Bus reply、原始错误以及平台没有 completion 的情况。
2. **BleHub policy**：统一使用 `Result<(), CommandDispatchError>` 表示 command 是否被 backend 接管，使用 `BleEvent` 表示 accepted command 的 operation result 和被动平台事件。
3. **待验证结论**：官方快照没有保证的 callback 时序、exactly-once、dispose drain、厂商行为和 FFI exception mapping，必须通过源码补证或 runtime spike 验证。

因此，下面两句话性质不同：

- “Apple `scanForPeripherals` 返回 `Void`，没有 scan-start completion”是平台事实。
- “BleHub 在执行该调用并建立本地 scan session 后发出 start terminal success”是 BleHub policy，不是 Apple 提供的成功保证。

BleHub 不把 Windows `HRESULT`、Android boolean/status/exception、CoreBluetooth callback `Error` 和 BlueZ D-Bus error直接暴露成四套公共方法签名。公共边界保持统一，backend 负责保留原始 platform diagnostic 并映射到稳定 category。

## 2. 证据等级

本文使用以下标记：

| 标记 | 含义 |
|---|---|
| **Verified** | 本地官方 API 快照直接说明，或本地 Android Framework 源码直接实现 |
| **Inferred** | 可由多项官方事实合理推出，或属于明确标注的 BleHub/backend policy |
| **Unverified** | 当前本地快照未覆盖，或平台没有给出足够强的时序/lifecycle 保证，需要补充证据或 spike |

“快照未包含某 API”不能单独证明平台全局不存在该 API。对 capability=false 的结论，需要同时记录检索范围并在实现阶段复核目标 SDK/API surface。

## 3. 统一边界不是 native API 镜像

### 3.1 三个结果阶段

```text
Rust application
  -> BleHub command method
  -> BleBackend dispatch
  -> native API / platform execution context
  -> callback, async result, delegate or D-Bus reply
  -> backend constructs BleEvent
  -> backend event ingress
  -> Rust BleEventSink
```

三个阶段必须分开：

| 阶段 | 公共表示 | 含义 |
|---|---|---|
| Hub/backend assembly 与本地 lifecycle | `Result<_, BleHubError>` | bind、构造、FFI contract、dispose 等非 BLE command 失败 |
| command dispatch | `Result<(), CommandDispatchError>` | backend 是否接管 request 和 caller-owned IDs；`Err` 后该 `CommandId` 严格无事件 |
| operation completion | `BleEvent`，通常终结于 `CommandCompleted` | accepted command 的平台/BLE结果；可在 trait method 返回前同步上行，也可异步、迟到或缺失 |

`Ok(())` 绝不统一表示：

- native API 已成功完成；
- controller 已执行；
- remote peripheral 已确认；
- peripheral 业务逻辑已处理；
- 后续 callback 必达。

### 3.2 为什么所有 command 仍可使用同一种 `Result`

有些 native method 返回 boolean、status 或 async operation；另一些只返回 `void/Void/Unit`。这不要求 BleHub 为每个平台或每个 operation 定义不同的 public `Result`，因为该 `Result` 描述的是统一 dispatch boundary，而不是 native result。

即使 native operation 没有同步 error，Hub/backend 层仍可能拒绝 dispatch：

- Hub 已关闭；
- request wire shape 或本地不变量非法；
- backend 未绑定或 dispatcher 已永久关闭；
- command 无法进入平台要求的执行位置；
- foreign implementation 违反已声明的 FFI contract。

native API 的 immediate boolean/status/exception不自动等同于`CommandDispatchError`。v0.1 Plan已固定accepted线性化点：backend在自己的串行执行边界内原子完成correlation登记、cleanup ownership取得和执行路径提交时，command成为accepted。这里的“原子”是协议上的单一线性化点，不要求 lock-free 或硬件事务；Windows direct call、Android GATT queue、CoreBluetooth dispatch queue和BlueZ D-Bus request可以使用不同内部机制。此前可以同步拒绝，但不得创建callback source、保存command状态或emit event；此后必须返回`Ok(())`，native boolean/status/HRESULT/exception无论多快都成为terminal result。各backend mapping tests必须证明这条边界。

不采用“所有结果都走事件、command method永远返回成功/void”：

- Hub已关闭、wire request非法或FFI调用根本没有进入backend时，event通道可能不可用，也没有合法的accepted operation可供终结；
- caller将无法区分“从未接管”与“已接管但native callback永久缺失”，只能把所有情况拖到deadline；
- backend将被迫为未接管的request伪造completion event，反而破坏`CommandCompleted`只描述accepted command的语义；
- FFI invocation/typed conversion本身仍需要同步失败边界，删除`Result`只会把它变成exception、默认值或静默失败。

因此当前形状已经是最小双通道：同步`Result`只回答dispatch acceptance，所有已接受operation的native/BLE结果都走event。

`BleBackend`不是每个method都返回`CommandDispatchError`，而是按method性质分层：

| method类别 | 返回类型 | 原因 |
|---|---|---|
| `capabilities`、`bind_event_ingress`、`dispose` | `Result<_, BleHubError>` | assembly/static metadata/local lifecycle，不是带`CommandId`的BLE command；`capabilities`在constructor中读取一次并由Hub缓存 |
| `initialize/start_scan/stop_scan/connect/disconnect/discover_gatt/read_characteristic/write_characteristic/set_notifications/read_rssi` | `Result<(), CommandDispatchError>` | 全部是带caller-owned`CommandId`的command，同步结果只表示accepted/rejected |

public `BleHub` command method与backend command一一对应并返回相同`CommandDispatchError`；`BleHub::new`和`dispose`使用`BleHubError`，`BleHub::capabilities()`只返回constructor缓存，不再次跨FFI，因此不需要`Result`。

公共实现边界如下：

| 层 | 可以统一的逻辑 | 必须保留的平台逻辑 |
|---|---|---|
| `BleHub` | command/close gate、context-free wire/shape/hard-limit校验、原样转发request和同步`Result` | 不保存pending command，不解释native状态，不生成completion |
| `BleBackend` contract | accepted/rejected不变量、terminal attempt、cleanup与ID回显规则 | correlation map、native handle lookup、平台串行执行位置、immediate result/callback映射 |
| testkit/conformance | 同一套race与mapping scenarios、FakeBackend、断言 | 各平台fixture、错误码表和runtime/HIL证据 |

把acceptance transaction整体上提到`BleHub`会迫使core持有pending/correlation状态，并且仍无法替Android/CoreBluetooth/WinRT/BlueZ管理native object与callback queue；这既重复状态又破坏thin-core边界。实现阶段可以为Rust backends提供crate-private helper，Kotlin/Swift也可各自封装小型`accept/complete` helper，但它们是backend实现复用，不是新增public Hub state或第二套协议真值。

### 3.3 公共错误分层

下面是概念上的公共分类。`CommandKind`只标识调用的是哪一种BleHub command，不表示平台callback类型或执行状态；`ErrorCategory`位于可复用的`BleFailure`中，分类accepted command和passive state event都可能报告的BLE failure。它与`CommandDispatchError`中的同名概念由结果阶段区分：accepted前的lifecycle拒绝是`CommandDispatchError::InvalidState`；accepted command的terminal状态失败是`BleOperationError { operation: CommandKind::Connect, failure: BleFailure { category: ErrorCategory::InvalidState, .. } }`；没有command的passive scan/connection failure只携带`BleFailure`，不得为其伪造`CommandKind`。

```rust
pub enum CommandKind {
    Initialize,
    StartScan,
    StopScan,
    Connect,
    Disconnect,
    DiscoverGatt,
    ReadCharacteristic,
    WriteCharacteristic,
    SetNotifications,
    ReadRssi,
}

pub enum ErrorCategory {
    Permission,
    AdapterUnavailable,
    InvalidState,
    NotFound,
    Timeout,
    Cancelled,
    Connection,
    Gatt,
    Protocol,
    NotSupported,
    Backend,
    Internal,
}

pub enum CommandDispatchError {
    Closed { operation: CommandKind },
    InvalidRequest { operation: CommandKind, message: String },
    InvalidState { operation: CommandKind, message: String },
    BackendUnavailable {
        operation: CommandKind,
        message: String,
        platform: Option<PlatformErrorDetails>,
    },
    FfiContract { message: String },
}

pub struct BleFailure {
    pub category: ErrorCategory,
    pub message: String,
    pub platform: Option<PlatformErrorDetails>,
}

pub struct BleOperationError {
    pub operation: CommandKind,
    pub failure: BleFailure,
}

pub struct PlatformErrorDetails {
    // Platform identifies Windows, Android, Apple, Linux or Test.
    pub platform: Platform,
    pub code: Option<String>,
    pub name: Option<String>,
    pub message: Option<String>,
}
```

稳定 `ErrorCategory` 位于 `BleFailure.category`，用于跨平台业务决策；`PlatformErrorDetails` 保留诊断证据。accepted command的terminal error应通过`operation_error.failure.category`读取分类；`BleOperationError`不再重复category、message或platform字段。不得让application依赖platform message文本控制流程。

Rust trait约定也分层：

- `BleHubError`和`CommandDispatchError`是同步`Result`的顶层error：实现`std::error::Error`，当前由`derive_more::Display/Error`消除样板，跨BoltFFI时同时使用`#[boltffi::error]`。`BleHubError::BackendCleanup`专门表示dispose/cleanup失败；该错误不重新打开已经closed的Hub。
- `BleFailure`是event中可复用的failure data record，承载稳定category、诊断message和optional platform evidence；它既可直接用于无command的passive state failure，也可嵌入`BleOperationError`。
- `BleOperationError`只为accepted command的terminal failure增加原command的`CommandKind`。两者在Rust侧都实现`std::error::Error`以便ergonomics，但跨FFI仍按`#[boltffi::data]`传值，不自动throw。
- `PlatformErrorDetails`只是嵌入`BleFailure`的FFI-safe diagnostic DTO，不独立参与`Result`控制流，默认不实现`std::error::Error`；需要展示时由包含它的failure/error负责format。
- 所有自由文本`message`只用于诊断。`CommandDispatchError::Closed { operation }`的结构化信息已足够，因此不附加message；application不得解析其他variant、`BleFailure.message`或`PlatformErrorDetails.message`控制流程。
- BoltFFI `0.30.1`把thiserror在record error类型上必须使用的未限定`#[error(...)]` helper误判为自己的marker（上游[#870](https://github.com/boltffi/boltffi/issues/870)）；已验证的scanned-contract组合是`#[boltffi::error/data] + derive_more::Display/Error`。升级宏或generator时必须重新生成并编译Kotlin/Swift binding。

建议映射原则：

| Native failure | 稳定 `BleFailure.category` 示例 | 保留的 platform evidence |
|---|---|---|
| permission/authorization | `Permission` | Android exception/status、Apple error/state、WinRT access/HRESULT、D-Bus error name |
| adapter/radio unavailable | `AdapterUnavailable` | native state、HRESULT、null handle、D-Bus `NotReady` |
| invalid native lifecycle | `InvalidState` | Android false/status/exception、CoreBluetooth state context、WinRT status、D-Bus `InProgress` |
| stale/unknown backend handle | `NotFound` | backend-local lookup diagnostic；不伪造 native object |
| platform timeout | `Timeout` | native timeout/error；caller deadline不是 platform timeout |
| confirmed cancellation | `Cancelled` | native callback/reply证据；pending connect还必须证明disconnect导致取消，独立connect failure仍是`Failed`；caller-local abandon不伪造成backend cancellation |
| connection failure | `Connection` | connection callback status/error/HRESULT/D-Bus error |
| GATT/ATT failure | `Gatt` 或 `Protocol` | GATT status、protocol error、CB error、BlueZ error name |
| unsupported capability | `NotSupported` | capability + platform evidence |
| dispatcher/backend failure | `Backend`/`Internal` | 原始 code/name/message，避免泄露 secret |

精确 error-code table 应由每个平台 backend 的 mapping module 和 tests 固定；本文不把所有 native code 扁平化成一个不稳定的公共枚举。

## 4. 跨平台 operation completion 矩阵

本节不是backend实现流程图。每一行按以下问题阅读：

| 列 | 含义 |
|---|---|
| 官方资料已确认的原生行为 | 本地官方snapshot已经明确记载的method返回值、async result、callback/delegate、状态属性或D-Bus reply。它是BleHub制定映射的证据，通常只是与completion相关的关键片段，不是完整backend执行步骤。 |
| BleHub contract规定的成功判定点 | 公共契约选择的规范性映射：backend观察到什么证据后，才可按顺序尝试domain event（若有）和`CommandCompleted(Succeeded)`。实际判定和event构造仍由backend完成，不表示`BleHub`对象维护operation状态。 |
| 成功不代表什么 | 限制成功语义，防止把“请求已提交”“本地对象已建立”夸大成controller、无线链路、remote ATT或peripheral业务已经完成。 |
| 待验证项 | 当前本地资料没有证明的时序、错误码、liveness、共享副作用或runtime行为；实现前需要补证或spike，不能先当作平台保证。 |

后续表因operation不同，可能把最后一列写成“失败/迟到事件”“关联限制”或“共享限制”，但原生证据与BleHub contract的分层不变。“成功判定点”和“最强成功语义”共同回答：**backend收到哪些证据后可以成功终结该command，以及这个成功最多能说明什么**。

### 4.1 `Initialize`

| 平台 | 官方资料已确认的原生行为 | BleHub contract规定的`Initialize`成功判定点 | 成功不代表什么 / 待验证项 |
|---|---|---|---|
| Windows | `BluetoothAdapter.GetDefaultAsync`、`GetRadioAsync` 是 async operation；adapter 暴露 LE/central capabilities；`Radio.StateChanged` 后读取 `State`。 | 建立 state subscription并发布初始 `AdapterStateChanged` 后 terminal；初态可为 Off/Unavailable，不要求 Ready。 | 当前快照未闭环无 adapter/radio 的 HRESULT 与 `Unauthorized` 映射；初始化成功不等于 radio On。 |
| Android | `BluetoothManager.getAdapter()`/scanner 可能为空；adapter state 可读取，变化由 `ACTION_STATE_CHANGED` broadcast 表达；没有 initialize completion callback。 | 获取当前 snapshot、注册状态监听并发布初始事件后 terminal。 | receiver 交付时序未在选定快照中验证；成功不等于 adapter On 或 scanner 可用。 |
| Apple | manager 构造 API存在；`centralManagerDidUpdateState` 是 required state callback；`CBManager` 暴露 state/authorization。 | 等待首次可用的 state callback并发布初始 snapshot 后 terminal。 | 首次 callback 的 exactly-once、与构造返回的顺序和 queue 细节在当前快照中未闭环。 |
| BlueZ | `Adapter1` 暴露 `Powered`、实验性 `PowerState` 和 `Roles`；没有统一 initialize method。 | 解析 adapter/object tree 和初始 properties，发布 snapshot 后 terminal。 | 当前快照未包含 client-side ObjectManager contract；adapter 选择与 Unauthorized 映射未验证。 |

证据：

- Windows：[GetDefaultAsync](../platform-api-reference/windows/bluetoothadapter_getdefaultasync_1549573963.md)、[GetRadioAsync](../platform-api-reference/windows/bluetoothadapter_getradioasync_1950748834.md)、[Radio.StateChanged](../platform-api-reference/windows/radio_statechanged.md)
- Android：[BluetoothManager.java](../platform-api-reference/android/BluetoothManager.java)、[BluetoothAdapter.java](../platform-api-reference/android/BluetoothAdapter.java)
- Apple：[CBCentralManager](../platform-api-reference/apple/cbcentralmanager.md)、[CBCentralManagerDelegate](../platform-api-reference/apple/cbcentralmanagerdelegate.md)、[CBManager](../platform-api-reference/apple/cbmanager.md)
- BlueZ：[org.bluez.Adapter](../platform-api-reference/bluez/org.bluez.Adapter.rst)

### 4.2 `StartScan`

| 平台 | 官方资料已确认的原生行为 | BleHub contract规定的`StartScan`成功判定点 | 失败、被动事件与语义限制 |
|---|---|---|---|
| Windows | watcher `Start(): void`；状态可进入 `Started`，但文档仍称 scan request pending；失败可进入 `Aborted`；error/suspend 可触发 `Stopped`。 | watcher 已进入 `Started`，本 `ScanSessionId` 已建立。 | `Aborted`/`Stopped` 映射失败或被动 stop；没有独立 started callback。 |
| Android | `startScan(..., callback): void`；没有 success callback；`onScanFailed` 报告部分失败。Framework 源码中扫描过频存在不回调分支，registration exception 还可能 post 多个 failure。 | public native call 已返回，backend 已建立本地 scan session/correlation。 | 不能保证 registration/controller 已开始；必须防止同一 command 被多个 failure callback 重复终结。 |
| Apple | `scanForPeripherals(...): Void`；发现结果由 `didDiscover` 提供；没有 scan-start completion；再次调用覆盖现有参数。 | backend 已在可调用状态执行 native call并建立本地 session。 | `didDiscover` 是 passive observation，不是 start completion；不能保证后续 observation 或 failure。 |
| BlueZ | `StartDiscovery()` D-Bus reply/error；每 client/adapter 一个 discovery session；filter 需要独立 `SetDiscoveryFilter()` reply。 | 本 client 成功取得 discovery session，所需 filter call 也已成功。 | procedure/filter 在 clients 间共享；backend 必须重新应用自己的 filter。 |

不能把任何平台的 start success 统一描述成“controller 已开始 over-air 扫描且一定会收到设备”。按Plan 6.6，malformed service UUID在accepted前返回`InvalidRequest`且零event；service filtering始终是公共contract保证，不设置`supports_service_scan_filter` capability。native filter只作为optional pushdown，backend仍须对未由positive native evidence覆盖的条件补做callback mapping，确保只emit满足统一predicate的observation。

证据：

- Windows：[Watcher.Start](../platform-api-reference/windows/bluetoothleadvertisementwatcher_start_1587696324.md)、[WatcherStatus](../platform-api-reference/windows/bluetoothleadvertisementwatcherstatus.md)
- Android：[BluetoothLeScanner.java](../platform-api-reference/android/BluetoothLeScanner.java)、[ScanCallback.java](../platform-api-reference/android/ScanCallback.java)
- Apple：[scanForPeripherals](../platform-api-reference/apple/cbcentralmanager-scanforperipherals.md)
- BlueZ：[org.bluez.Adapter](../platform-api-reference/bluez/org.bluez.Adapter.rst)

### 4.3 `StopScan`

| 平台 | 官方资料已确认的原生行为 | BleHub contract规定的`StopScan`成功判定点 | 迟到事件与共享限制 |
|---|---|---|---|
| Windows | `Stop(): void` 先进入 `Stopping`，完成后 `Stopped`；`Stopped` 也可能由 error/suspend 引起。 | 主动 stop correlation 确认 `Stopped`；已 stopped/aborted 可幂等完成。 | `Stop()` 返回不是完成；其他应用扫描不受影响。 |
| Android | `stopScan(sameCallback): void`；未注册/重复 stop 是 no-op；没有 completion。 | native stop/unregister request 已发出并关闭本地 session。 | 已 post 的普通/batch result 可能迟到。 |
| Apple | `stopScan(): Void`；没有 stop completion。 | native call 已执行并关闭本地 session/routing。 | 不能保证已排队 discovery callback 已 drain。 |
| BlueZ | `StopDiscovery()` reply 只释放本 client session。 | 成功释放本 client session。 | adapter `Discovering` 是聚合状态，其他 clients 可继续扫描。 |

证据：

- Windows：[Watcher.Stop](../platform-api-reference/windows/bluetoothleadvertisementwatcher_stop_1201535524.md)、[Watcher.Stopped](../platform-api-reference/windows/bluetoothleadvertisementwatcher_stopped.md)
- Android：[BluetoothLeScanner.java](../platform-api-reference/android/BluetoothLeScanner.java)
- Apple：[stopScan](../platform-api-reference/apple/cbcentralmanager-stopscan.md)
- BlueZ：[org.bluez.Adapter](../platform-api-reference/bluez/org.bluez.Adapter.rst)

### 4.4 `Connect`

| 平台 | 官方资料已确认的原生行为 | BleHub contract采用的`Connect`完成证据 | 成功不代表什么 / liveness待验证项 |
|---|---|---|---|
| Windows | `BluetoothLEDevice.FromIdAsync` 取得 object但不自动保证连接；uncached GATT operation 或 maintain-connection policy 才触发连接；状态通过 `ConnectionStatusChanged`。 | backend 执行明确触发动作后观察本 connection 进入 Connected。 | 不能把 `FromIdAsync` 完成当 connected；v0.1 的精确触发 recipe、HRESULT 和取消证据仍需 spike，无证据时不能把resource release解释成connect `Cancelled`。 |
| Android | `connectGatt` 返回 handle或 null；非空 handle也不证明内部 registration/dispatch 成功；终态是 `onConnectionStateChange(status,newState)`。 | 匹配 handle 的 `GATT_SUCCESS + STATE_CONNECTED`。 | Framework 部分 registration/Binder 失败可能只有日志或无 callback；必须保留 caller deadline。 |
| Apple | `connect(): Void`；成功 `didConnect`，失败 `didFailToConnect(error)`；平台不会自动 timeout。 | `didConnect`。 | 不能声称 GATT 已发现；pending connect 需要显式 cancel，caller deadline不是平台 timeout。 |
| BlueZ | `Device1.Connect()` 尝试多个 profile，至少一个成功即可 reply success；`Connected` 是独立 property。 | backend 把 reply/property与本 `ConnectionId` 关联并确认所需 LE connection state。 | reply不表示所有 profile/GATT ready；双模 bearer 与 AlreadyConnected adoption policy 未验证。 |

证据：

- Windows：[BluetoothLEDevice.FromIdAsync](../platform-api-reference/windows/bluetoothledevice_fromidasync_1322863552.md)、[ConnectionStatusChanged](../platform-api-reference/windows/bluetoothledevice_connectionstatuschanged.md)
- Android：[BluetoothDevice.java](../platform-api-reference/android/BluetoothDevice.java)、[BluetoothGatt.java](../platform-api-reference/android/BluetoothGatt.java)、[BluetoothGattCallback.java](../platform-api-reference/android/BluetoothGattCallback.java)
- Apple：[connect](../platform-api-reference/apple/cbcentralmanager-connect.md)、[CBCentralManagerDelegate](../platform-api-reference/apple/cbcentralmanagerdelegate.md)
- BlueZ：[org.bluez.Device](../platform-api-reference/bluez/org.bluez.Device.rst)

### 4.5 `Disconnect`

| 平台 | 官方资料已确认的原生行为 | BleHub contract规定的`Disconnect`成功判定点 | 成功不代表什么 / 平台副作用 |
|---|---|---|---|
| Windows | 没有统一 explicit BLE disconnect；关闭 owned handlers/session/service/device；`BluetoothLEDevice.Close()` 仅在它是唯一连接 app 时可能关闭连接。 | BleHub-owned logical connection resources 已释放。 | 不能保证系统物理链路立即断开、产生callback或实际取消pending connect；logical disconnect成功不是原connect `Cancelled` evidence。 |
| Android | `BluetoothGatt.disconnect(): void`；未注册时 no-op，Binder exception 可被记录后吞掉；正常终态仍通过 connection-state callback。 | 正常路径等待匹配的 Disconnected；liveness不足时只能按明确 fallback关闭本地 lifecycle。 | callback不保证必达；不能声称共享物理 ACL 已消失。 |
| Apple | `cancelPeripheralConnection(): Void` 且 nonblocking；终态 `didDisconnectPeripheral(error)`；其他 app 可继续持有链路。 | 本 `ConnectionId` 的 local lifecycle 已断开。 | pending GATT command可能不再完成；不保证物理链路立即消失。 |
| BlueZ | `Device1.Disconnect()` 断开所有 connected profiles并终止 ACL；可取消 pending Connect。 | D-Bus operation成功且本 BleHub logical connection结束。 | 不是 per-client release，可能影响其他 profile/process；必须显式记录这种平台影响。 |

`ConnectionState.Disconnecting`是可选的backend进度事件，不是跨平台必需的中间序列。Android backend可以在调用`BluetoothGatt.disconnect()`前发出该事件；Apple `cancelPeripheralConnection`是nonblocking，可靠终态来自`didDisconnectPeripheral`，因此Apple backend只需要交付最终`Disconnected`或带连接错误的终态。共享caller/test必须以最终逻辑生命周期结果和`CommandCompleted`为边界，不能因为Apple没有`Disconnecting`而判失败。

BlueZ 的设备级 disconnect 语义比其他平台更强且更具系统副作用，不能被公共“logical disconnect”措辞掩盖；Linux backend 文档必须单独提示。pending connect的取消语义必须服从平台因果证据：只有证明disconnect导致原connect被取消时才可为原command尝试`Cancelled`；独立connect failure仍是`Failed`。Windows等缺少该因果证据的平台可完成logical disconnect，但原connect仍按既有completion evidence/liveness和caller deadline处理，任何晚到结果保留原connect IDs。

证据：

- Windows：[BluetoothLEDevice.Close](../platform-api-reference/windows/bluetoothledevice_close_811482585.md)、[GattSession.Close](../platform-api-reference/windows/gattsession_close_811482585.md)
- Android：[BluetoothGatt.java](../platform-api-reference/android/BluetoothGatt.java)
- Apple：[cancelPeripheralConnection](../platform-api-reference/apple/cbcentralmanager-cancelperipheralconnection.md)
- BlueZ：[org.bluez.Device](../platform-api-reference/bluez/org.bluez.Device.rst)

### 4.6 `DiscoverGatt`

| 平台 | 官方资料已确认的原生行为 | BleHub contract规定的`DiscoverGatt`成功判定点 | 语义限制 / 待验证项 |
|---|---|---|---|
| Windows | services、characteristics、descriptors 分层 async operation并返回 result/status；可显式选择 cache mode。 | 所有计划内层级成功后一次性发布 `GattDiscovered`，失败不发布 partial database。 | 部分 result status pages、精确 HRESULT 和 connect触发关系仍需补证。 |
| Android | `discoverServices(): boolean`；完成 `onServicesDiscovered(status)`；Framework object hierarchy提供 services/characteristics/descriptors。 | `GATT_SUCCESS` 后从 hierarchy构造完整 DTO并一次性发布。 | 不承诺 uncached/fresh；失败 callback 前 cache也可能更新，不能发布 partial。 |
| Apple | services、characteristics、descriptors 各自 `Void` method与 delegate callback；没有 aggregate completion。 | backend跟踪全部分层 callback，构造 v0.1 scope 下的完整 DTO。 | callback exactly-once、零结果、included services、断连 race与原子 snapshot 未闭环。 |
| BlueZ | 没有 discover method；`ServicesResolved` 标记 service resolution；GATT hierarchy通过 D-Bus objects暴露。 | 等待/读取 resolved状态并枚举 object tree，构造 DTO。 | ObjectManager枚举原子性、descriptor完整性、signal/object export顺序和 fresh语义未验证。 |

“完整 GATT database”是 BleHub 对 v0.1 traversal scope 的定义，不是四个平台共同提供的原子、uncached ATT snapshot 保证。

证据：

- Windows：[GetGattServicesAsync](../platform-api-reference/windows/bluetoothledevice_getgattservicesasync_1174771000.md)、[GetCharacteristicsAsync](../platform-api-reference/windows/gattdeviceservice_getcharacteristicsasync_1845166286.md)、[GetDescriptorsAsync](../platform-api-reference/windows/gattcharacteristic_getdescriptorsasync_1141906880.md)
- Android：[BluetoothGatt.java](../platform-api-reference/android/BluetoothGatt.java)
- Apple：[discoverServices](../platform-api-reference/apple/cbperipheral-discoverservices.md)、[discoverCharacteristics](../platform-api-reference/apple/cbperipheral-discovercharacteristics.md)、[discoverDescriptors](../platform-api-reference/apple/cbperipheral-discoverdescriptors.md)
- BlueZ：[org.bluez.Device](../platform-api-reference/bluez/org.bluez.Device.rst)、[GattService](../platform-api-reference/bluez/org.bluez.GattService.rst)、[GattCharacteristic](../platform-api-reference/bluez/org.bluez.GattCharacteristic.rst)、[GattDescriptor](../platform-api-reference/bluez/org.bluez.GattDescriptor.rst)

### 4.7 `ReadCharacteristic`

| 平台 | 官方资料已确认的原生调用 | BleHub contract采用的完成与value证据 | 关键correlation限制 |
|---|---|---|---|
| Windows | `ReadValueAsync(cacheMode)` 返回 async operation。 | `GattReadResult` status success与 result value；v0.1显式使用 uncached。 | `ValueChanged` 是 notification，不是 read completion。 |
| Android | `readCharacteristic(): boolean`；`true` 只表示 initiation。 | `onCharacteristicRead(characteristic,value,status)`，仅 `GATT_SUCCESS` 成功。 | callback没有 command ID；同 connection operation需要 backend串行/correlation。 |
| Apple | `readValue(): Void`。 | `didUpdateValueFor(error)` 成功后读取 value。 | read与 notification共用 callback，native没有 reason/token。 |
| BlueZ | `ReadValue()` D-Bus reply直接返回 bytes。 | method reply bytes。 | `Value.PropertiesChanged` 也用于 notifications，不能作为 read correlation。 |

证据：

- Windows：[ReadValueAsync](../platform-api-reference/windows/gattcharacteristic_readvalueasync_2134129157.md)、[GattReadResult](../platform-api-reference/windows/gattreadresult.md)
- Android：[BluetoothGatt.java](../platform-api-reference/android/BluetoothGatt.java)、[BluetoothGattCallback.java](../platform-api-reference/android/BluetoothGattCallback.java)
- Apple：[readValue](../platform-api-reference/apple/cbperipheral-readvalue.md)、[CBPeripheralDelegate](../platform-api-reference/apple/cbperipheraldelegate.md)
- BlueZ：[GattCharacteristic](../platform-api-reference/bluez/org.bluez.GattCharacteristic.rst)

### 4.8 `WriteCharacteristic`

#### With response

| 平台 | 官方资料已确认的完成证据 | BleHub contract可声明的最强成功语义 |
|---|---|---|
| Windows | write async result/status success。 | Windows GATT write operation成功；具体 ATT/protocol diagnostic按 result映射。 |
| Android | immediate `BluetoothStatusCodes.SUCCESS` 后等待 `onCharacteristicWrite(GATT_SUCCESS)`，使用 response write type。 | Android/ATT acknowledged write operation成功。 |
| Apple | `.withResponse` 后等待 `didWriteValueFor(error)`。 | CoreBluetooth报告该 write成功。 |
| BlueZ | `WriteValue(type="request")` D-Bus reply。 | BlueZ request procedure成功返回。 |

即使 with-response成功，也不能声称 peripheral业务应用已消费、持久化或完成业务 transaction。

#### Without response

| 平台 | 官方资料已确认的原生行为 | BleHub contract可声明的最强成功语义 |
|---|---|---|
| Windows | 使用 without-response option的 async operation/result；当前快照缺少完整 option enum证据。 | Windows stack接受/完成本地发送尝试；不表示 remote acknowledgement。 |
| Android | immediate status后 Framework仍可能调用 write callback，但 write type明确不要求 remote response。 | Android stack完成本地 no-response write attempt；厂商 callback一致性需真机验证。 |
| Apple | `.withoutResponse` 没有逐 write callback/failure；CoreBluetooth只说 attempts to write且不保证成功；ready callback只是背压解除。 | backend满足 readiness/length policy并实际调用 native write；不能使用更强措辞。 |
| BlueZ | `WriteValue(type="command")` reply；没有 remote completion。 | BlueZ接受/完成本地 command发送尝试；不保证 reply时已经 over-air。 |

证据：

- Windows：[WriteValueAsync](../platform-api-reference/windows/gattcharacteristic_writevalueasync_991358569.md)、[WriteValueWithResultAsync](../platform-api-reference/windows/gattcharacteristic_writevaluewithresultasync_2074671654.md)
- Android：[BluetoothGatt.java](../platform-api-reference/android/BluetoothGatt.java)、[BluetoothGattCharacteristic.java](../platform-api-reference/android/BluetoothGattCharacteristic.java)
- Apple：[writeValue](../platform-api-reference/apple/cbperipheral-writevalue.md)、[maximumWriteValueLength](../platform-api-reference/apple/cbperipheral-maximumwritevaluelength.md)
- BlueZ：[GattCharacteristic](../platform-api-reference/bluez/org.bluez.GattCharacteristic.rst)

### 4.9 `SetNotifications`

| 平台 | 原生enable流程与完成证据 | 原生disable流程与完成证据 | BleHub语义限制 / 待验证项 |
|---|---|---|---|
| Windows | 先注册 `ValueChanged` handler，再写 notification/indication CCCD并检查 result。 | CCCD disable成功后撤销本地 handler。 | late callback/drain没有 native保证。 |
| Android | `setCharacteristicNotification(true)`只建 local routing；按 mode写 CCCD并等待 `onDescriptorWrite(GATT_SUCCESS)`。 | 先确认 disable CCCD callback，再关闭 local routing；后一步失败是 partial failure。 | 已 post到 Handler的 value可在 disable后迟到。 |
| Apple | `setNotifyValue(true)` 后 `didUpdateNotificationStateFor(error)`。 | protocol摘要表明 stop也使用 notification-state callback。 | API不暴露显式 CCCD；若 characteristic同时支持 notify和indicate，文档说明该方法启用 notification，不能承诺 caller可强制 indication。 |
| BlueZ | `StartNotify()` reply创建共享 session。 | `StopNotify()` reply只释放一个 session。 | `Notifying` 是聚合状态；`StartNotify` 没有 mode参数，双 capability时无法从当前快照证明可兑现 caller指定 mode。 |

Plan采用“platform default + strict request”组合：`PlatformDefault`允许native API选择，成功event在backend能证明时携带`actual_mode=Some(...)`，不能证明时为`None`；`RequireNotification`/`RequireIndication`只有backend能证明满足时才成功，否则terminal `NotSupported`。capabilities分别声明strict notification/indication与actual-mode reporting能力。disable仅使用原`SubscriptionId`，不要求caller重复mode。任何路径都不得静默替换strict mode后仍报告成功。

证据：

- Windows：[CCCD write](../platform-api-reference/windows/gattcharacteristic_writeclientcharacteristicconfigurationdescriptorwithresultasync_2005787286.md)、[ValueChanged](../platform-api-reference/windows/gattcharacteristic_valuechanged.md)
- Android：[BluetoothGatt.java](../platform-api-reference/android/BluetoothGatt.java)、[BluetoothGattDescriptor.java](../platform-api-reference/android/BluetoothGattDescriptor.java)
- Apple：[setNotifyValue](../platform-api-reference/apple/cbperipheral-setnotifyvalue.md)
- BlueZ：[GattCharacteristic](../platform-api-reference/bluez/org.bluez.GattCharacteristic.rst)

### 4.10 `ReadRssi`

| 平台 | 官方资料已确认的原生行为与完成证据 | BleHub v0.1 contract判定 |
|---|---|---|
| Windows | 选定快照只有 advertisement observation RSSI，没有 connected one-shot completion证据。 | capability=false；若仍接受 command，terminal `NotSupported`；不使用 cached scan RSSI伪装。 |
| Android | `readRemoteRssi(): boolean`；完成 `onReadRemoteRssi(rssi,status)`。 | `GATT_SUCCESS` 后发布 connected RSSI。 |
| Apple | connected `readRSSI(): Void`；iOS/tvOS和macOS使用不同 delegate/value extraction路径。 | delegate成功后发布 RSSI；其他 Apple target availability需补证。 |
| BlueZ | `Device1.RSSI` 明确是 inquiry/advertising RSSI，没有 connected one-shot method。 | capability=false；不把 property值关联到 active `ConnectionId`。 |

证据：

- Windows：[RawSignalStrengthInDBm](../platform-api-reference/windows/bluetoothleadvertisementreceivedeventargs_rawsignalstrengthindbm.md)
- Android：[BluetoothGatt.java](../platform-api-reference/android/BluetoothGatt.java)、[BluetoothGattCallback.java](../platform-api-reference/android/BluetoothGattCallback.java)
- Apple：[readRSSI](../platform-api-reference/apple/cbperipheral-readrssi.md)
- BlueZ：[org.bluez.Device](../platform-api-reference/bluez/org.bluez.Device.rst)

### 4.11 `dispose/close`

`dispose` 不是 BLE command，也没有跨平台统一 native completion：

| 平台 | 官方资料已确认的原生清理行为 | BleHub只能承诺 |
|---|---|---|
| Windows | stop watcher、撤销 handlers/tokens、close owned sessions/services/device；device close最多可能影响物理连接。 | backend进入closed lifecycle并best-effort释放owned resources。 |
| Android | 对同一 scan callback调用 stop；`BluetoothGatt.close()` unregister client但没有整体 completion，也不等同于 disconnect。 | 本地对象/correlation清理完成或报告清理异常；ingress gate拒绝晚到 callback。 |
| Apple | stop scan、nonblocking cancel、release references；没有 aggregate shutdown completion。 | 本地 lifecycle关闭；不等待所有无线操作、ARC deallocation或callback drain。 |
| BlueZ | 显式释放自己取得的 discovery/notify sessions和D-Bus routing；没有daemon shutdown。 | 本 client ownership best-effort释放；当前快照未证明 unique-name消失自动完成全部central cleanup。 |

即使 `dispose` 返回 `Err(BleHubError)`，Hub仍必须保持closed，不能继续发令。

## 5. 统一 `BleEvent` 是语义层，不是 callback 合集

### 5.1 Native source 到统一事件

| Unified event | Native source示例 | 是否对应 command |
|---|---|---|
| `AdapterStateChanged` | radio/adapter state event、broadcast、delegate、D-Bus property | initialize时可带 command；后续通常 passive |
| `ScanStarted` | native started证据或backend-local start policy | 是 |
| `AdvertisementObserved` | watcher/scan callback/didDiscover/Device properties | 否，携带 `ScanSessionId` |
| `ScanStopped` |主动 stop completion或被动 abort/suspend/error | 主动stop可带command；被动failure无command并携带`BleFailure` |
| `ConnectionStateChanged` | connection callback/delegate/property/signal | 主动connect/disconnect可带command；被动断连failure无command并携带`BleFailure` |
| `GattDiscovered` | 分层 async/callback或object tree完成 | 是 |
| `CharacteristicRead` | read result/callback/reply | 是 |
| `CharacteristicWritten` | write completion；成功强度取决于 write type/platform | 是 |
| `NotificationStateChanged` | CCCD/notification-state/session reply | 是 |
| `NotificationReceived` | characteristic value callback/property | 否，携带 subscription/session IDs |
| `RssiRead` | connected RSSI callback | 是 |
| `CommandCompleted` | BleHub统一 terminal envelope | 是 |

backend必须使用自己的 native handle/correlation state补齐 caller-owned IDs。native callback不携带 `CommandId` 是常态，不是异常；native object不能进入 public `BleEvent`。无法归入domain result的backend/ingress diagnostic使用out-of-band structured tracing，不冻结为v0.1 public event，也不得替代required terminal。

### 5.2 Domain event 与 terminal event

对于成功且产生可观察数据/状态的 command：

```text
domain event
-> CommandCompleted(Succeeded)
```

对于 accepted command 的失败：

```text
CommandCompleted(Failed/TimedOut/Cancelled, BleOperationError { operation, failure })
```

`BleOperationError`只用于accepted command的terminal failure。passive `ScanStopped`或`ConnectionStateChanged` failure使用`error: Some(BleFailure)`且没有command lifecycle，不为其发明`CommandKind`或合成`CommandCompleted`。accepted command失败同时造成真实可观察状态变化时，failure state event中的`error`使用`BleFailure`，terminal event再使用`BleOperationError { operation, failure }`，且`terminal.error.failure`必须与前一state event的`BleFailure`值相等；passive advertisement、notification和adapter change同样不合成`CommandCompleted`。

平台没有 positive completion 时，`Succeeded` 必须使用本 operation/platform在第4节定义的最弱、准确语义，不能套用全局“操作已完成”。

### 5.3 Event shape 的唯一真值

概念 API 应避免同一 ID 同时存在于 `BleEvent` 顶层和 payload variant：

```rust
pub enum BleEvent {
    ScanStopped {
        command_id: Option<CommandId>,
        scan_id: ScanSessionId,
        reason: ScanStopReason,
        error: Option<BleFailure>,
    },
    ConnectionStateChanged {
        command_id: Option<CommandId>,
        connection_id: ConnectionId,
        phase: ConnectionPhase,
        error: Option<BleFailure>,
    },
    CharacteristicRead {
        command_id: CommandId,
        connection_id: ConnectionId,
        gatt_session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
        value: Vec<u8>,
    },
    CommandCompleted {
        command_id: CommandId,
        outcome: CommandOutcome,
        error: Option<BleOperationError>,
    },
    // ...
}
```

该建议已写回Plan：`BleEvent`本身就是语义enum，不再增加单字段payload wrapper；IDs只存在于对应variant。若Stable UniFFI complex enum生成不满足要求，再把`BleEvent`降为explicit tagged record；tag与IDs仍各有一个权威位置，不恢复外层kind/IDs加variant IDs的双重真值。

Phase 0 必须用真实 Kotlin/Swift生成结果比较：

- 直接作为`push`/`on_event`参数的`BleEvent` complex data enum；
- explicit tagged record fallback。

最终 shape应优先保证无重复真值、生成 API可读、unknown variant/versioning策略明确，而不是仅追求 Rust端最短定义。

## 6. 关键平台差异不能被统一层抹平

### 6.1 Android liveness与迟到 callback

本地 Android 36 Framework源码直接暴露以下风险：

- scan registration过频可静默失败；
- registration exception路径可能 post多个 failure callback；
- stop后已经post的普通/batch result可能迟到；
- public `connectGatt`忽略内部 connect boolean，部分 Binder exception只有日志；
- `disconnect`可no-op且没有同步status；
- GATT callback不携带 command ID；
- notification runnable在排队后不重新检查 BleHub subscription；
- `close()`不提供 callback queue drain保证。

因此 Android backend需要严格 operation serialization、handle/correlation map、terminal去重和late-event保留旧session IDs。caller deadline不能被描述成取消 native operation。

### 6.2 Apple无 completion的操作

CoreBluetooth scan start/stop和write-without-response没有逐 operation native completion。BleHub只能定义backend-local terminal strength。`peripheralIsReady(toSendWriteWithoutResponse:)`是背压解除，不是某笔write的成功回调。

### 6.3 BlueZ共享与系统副作用

BlueZ discovery和notification是跨client共享/session化的：

- `Discovering`和`Notifying`是聚合状态；
- stop只释放本client session，不要求聚合property变false；
- discovery filters会合并，backend必须本地再过滤；
- `Device1.Disconnect()`却是设备级操作，会断开所有profiles并终止ACL，不是per-client release。

### 6.4 Windows object acquisition不等于连接

`BluetoothLEDevice.FromIdAsync`完成只取得object。实际连接需要另一个GATT触发动作或maintain-connection策略。Windows backend必须在实现前固定connect recipe、completion evidence和resource ownership；关闭owned resources可以完成BleHub logical disconnect，但在缺少“该disconnect导致原connect被取消”的native causality evidence时不得伪造原connect `Cancelled`。独立connect failure仍terminal `Failed`；原connect继续受其既有evidence/liveness和caller deadline约束，只保留关联晚到callback所需的最小correlation，late result必须携带原IDs且不得恢复为当前logical connection。

## 7. API freeze 前必须解决的问题

### 7.1 已写回 Plan 的公共契约决策

1. accepted线性化为backend原子完成correlation、cleanup ownership和执行路径提交；此前严格无callback source/event，此后native结果全部走event。
2. terminal语义是“到达completion point后必须且至多一次作push attempt”，不是delivery guarantee；native callback可永久缺失，dispose可撤销尚未成熟的义务。
3. `InvalidEvent`/`DownstreamFailed`消费当前attempt：不重试，backend照常提交terminal/correlation cleanup并继续后续event；只有`Closed`停止push。
4. v0.1只保证同Hub `on_event`不并发和同command `domain -> terminal`程序顺序；不分配sequence、不提供sink-rejection gap检测。
5. `BleEvent`只有一层variant和一套IDs；BoltFFI fallback也不得恢复双重tag/ID真值。
6. notification使用`PlatformDefault`或strict request，成功event报告可知的actual mode，strict无法证明时`NotSupported`。
7. initialize每backend lifecycle只允许一次；成功前其他BLE command同步`InvalidState`，setup terminal failure后dispose/recreate。
8. dispose使caller把pending projection标记为`AbandonedByDispose`，撤销尚未到completion point的terminal义务；不drain caller queue。
9. 一次write对应一次native write，不隐式分片；wire hard guard与platform/connection write limit分层处理。
10. v0.1采用lockstep ABI packaging，禁止core/generated backend混装；multi-Hub默认不共享同一physical adapter，factory用`BleHubError::ResourceBusy`拒绝第二个active owner。
11. capability只描述当前public contract可实际消费的能力；service filtering本身是contract保证，不定义`supports_service_scan_filter`，native pushdown只是optional optimization；deferred MTU/PHY/pairing/multiple-adapter不提前占字段。
12. ingress token是trusted-process routing ID而非认证能力；公开error与默认structured tracing必须限长和redact。
13. v0.1不定义public diagnostic event；backend/ingress异常走out-of-band structured tracing，不能递归通过失败的sink报告。
14. malformed service UUID在accepted前返回`InvalidRequest`且零event；backend对每个平台都必须兑现Plan 6.6 predicate。
15. command-associated failure state与terminal使用值相等的`BleFailure`；pending connect只有在平台证据证明disconnect导致原connect被取消时才可terminal `Cancelled`，独立connect failure仍terminal `Failed`，Windows logical disconnect不得伪造该因果证据。

仍需在实现阶段固定hard guard具体常量、每个平台error-code mapping和`ResourceBusy.message`的平台上下文，但这些不再改变上述语义边界。

### 7.2 证据与 spike 缺口

#### Windows

- 固定connect触发recipe；
- 补齐 GATT result status、write option、watcher stopped error与HRESULT mapping证据；
- 复核 connected one-shot RSSI capability结论；
- 验证close后late callback和event token释放。

#### Android

- 真机/厂商栈验证 connect registration failure liveness；
- 验证write-without-response callback一致性；
- 验证descriptor immediate failure后的busy/session行为；
- 验证disconnect/close与Handler callback race；
- 固定permission exception/status到`BleFailure`的映射；accepted command terminal再用对应`CommandKind`包装成`BleOperationError`。

#### Apple

- 补齐manager initialization/state、delegate detailed symbol pages；
- 验证首次state callback、queue与ARC/deinit race；
- 验证分层GATT discovery在零结果/断连/错误下的completion；
- 固定without-response readiness state machine；
- 复核不同Apple target的RSSI callback和availability。

#### BlueZ

- 补充ObjectManager evidence；
- 验证 `ServicesResolved`、GATT object export和枚举顺序；
- 决定dual notify/indicate capability的处理；
- 验证D-Bus client消失时discovery/notify session cleanup；
- 评审设备级 `Disconnect()` 对其他profiles/processes的产品影响。

## 8. Conformance tests 的统一预期来自 Plan 7.1

Conformance tests 的统一预期行为派生自 [Plan 7.1](../../.plan/PLAN.md#71-backend-可观察契约规范性)，不是由本文创设。本文只为各平台 mapping fixtures 与 evidence 提供逐项研究依据；这些材料用于实现和验证 Plan 契约，不具有独立规范性，也不覆盖 Plan。

每个平台backend至少覆盖：

1. immediate dispatch `Err` 后严格无event；
2. callback-before-method-return时method必须返回 `Ok`；
3. accepted后native immediate failure映射唯一terminal failure；
4. domain event先于terminal success；
5. duplicate native terminal不导致多个有效terminal；
6. missing native callback允许caller deadline，不能由Hub伪造完成；
7. stop/disconnect/disable后的late event保留旧session IDs；
8. passive event无command ID但有正确session/opaque IDs；
9. write-without-response不产生remote acknowledgement语义；
10. dispose后late callback只能得到closed ingress结果，不能访问已释放sink；
11. platform error保留原始code/name并映射稳定category；
12. unsupported capability不以相似但错误的数据源伪装成功；
13. accepted线性化前后注入每一种native immediate failure，验证`Err -> no event`与`Ok -> terminal attempt`；
14. sink分别对domain/terminal返回`Err`，验证当前attempt不重试、correlation清理且后续event继续；
15. initialize前command、并发/重复initialize和setup-failure后重建；
16. dispose与pending command、正在执行sink call、已进入caller queue的event竞态；
17. notification strict/default request、actual mode unknown和disable只按subscription路由；
18. write wire hard limit、platform effective limit及禁止隐式分片；
19. 多Hub同physical adapter被factory拒绝或按明确platform policy处理；
20. incompatible ABI产物在assembly前被packaging/factory阻止，不能等首个event decode才失败；
21. malformed service UUID在accepted前`InvalidRequest`且零event，service predicate在无native pushdown时仍由backend mapping保证；
22. command-associated failure state与terminal携带值相等的`BleFailure`；
23. pending connect cleanup按platform causality evidence分支：只有证明disconnect导致原connect被取消时才使用`Cancelled`，独立connect failure仍为`Failed`；Windows无该证据时logical disconnect可完成而原connect继续受原evidence/deadline约束，late result保留原IDs。

这些tests验证的是 Plan 7.1 的统一contract；各平台 mapping fixtures 与 evidence 从本文的调用链和平台研究派生，不要求四个平台native callback序列完全相同。

## 9. 证据边界

本文引用的文件是本地官方快照，不是BleHub public API。快照来源、revision和hash记录在 [`sources.lock.json`](../platform-api-reference/sources.lock.json)。

- Windows、Android、BlueZ快照固定到上游revision/archive。
- Apple是带内容hash的live snapshot，不具有公开源码commit。
- Android Java Framework API是Kotlin backend实际调用的权威API定义。
- 未收录页面、系统service内部行为、OEM差异和runtime lifecycle不能凭经验提升为平台保证。

研究报告记录证据、BleHub policy与待决建议；它不静默覆盖Plan。公共shape或语义变更必须在 [`.plan/PLAN.md`](../../.plan/PLAN.md) 中明确修改并由用户Review。
