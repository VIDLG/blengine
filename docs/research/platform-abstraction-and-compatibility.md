# BleHub 平台抽象、信息保留与兼容性手册

- 状态：平台事实与公共抽象维护基线；规范性契约仍以 [`.plan/PLAN.md`](../../.plan/PLAN.md) 为准
- 日期：2026-08-30
- 范围：Windows WinRT、Android Framework BLE、Apple CoreBluetooth、Linux BlueZ D-Bus
- 官方证据：[`docs/platform-api-reference/`](../platform-api-reference/README.md) 中的本地快照
- 相关研究：[`cross-platform-command-event-contract.md`](cross-platform-command-event-contract.md)

## 1. 目的与证据边界

本文维护四类容易随时间丢失的设计上下文：

1. native 平台实际提供什么状态、过滤、控制和完成证据；
2. BleHub 如何把不同形状压缩为统一 DTO；
3. 统一后保留了什么、丢失了什么；
4. backend 如何通过 post-filter、optional data、capability、operation error 和 tracing 处理差异。

本文不重新定义 command acceptance、terminal event、ID ownership 或 dispose 规则；这些由 Plan 和 [`cross-platform-command-event-contract.md`](cross-platform-command-event-contract.md) 维护。

证据标记：

| 标记 | 含义 |
|---|---|
| **Verified** | 本地官方 API 快照或 Android Framework 源码直接说明 |
| **BleHub policy** | 已写入 Plan 的统一语义或兼容策略 |
| **Inferred** | 可由已验证事实推出，但原生 API 没有直接给出统一结论 |
| **Unverified** | 当前快照缺页、平台时序未保证或仍需 runtime spike |

“当前快照没有页面”不等于“平台没有 API”。冻结 capability 或声称不支持前，必须检查目标 SDK surface，必要时通过 `pixi run just docs-update` 补齐官方快照。

## 2. 公共抽象原则

### 2.1 强类型表示语义，不伪造构造不变量

`BluetoothUuid` 是 FFI-safe record：

```rust
pub struct BluetoothUuid {
    pub value: String,
}
```

它防止 UUID 与名称、地址或普通 diagnostic string 混用。UniFFI record 仍可被 foreign caller 直接构造，因此合法性不由构造器保证：

- command input 接受 4 位 16-bit、8 位 32-bit、32 位 simple 128-bit 和 36 位 hyphenated 128-bit 文本；
- BleHub 在进入 backend 前规范化为 lowercase hyphenated 128-bit；
- backend 产生的 advertisement、service data 和 GATT UUID 必须映射为相同 canonical shape；
- malformed command UUID 在 accepted 前返回 `InvalidRequest`，严格零 event。

Manufacturer Specific Data 的标识符使用 Bluetooth 标准术语 `company_id: u16`。它覆盖完整 16-bit wire domain，BleHub 不验证 Bluetooth SIG registry，也不为它增加无不变量的 newtype。

### 2.2 Native filter 只负责优化，统一 predicate 负责正确性

过滤分成三层，职责不能互换：

| 层 | 职责 |
|---|---|
| BleHub facade | accepted前校验wire shape并规范化UUID；不保存active scan、不接触observation、不执行predicate |
| Platform backend | 保存本scan session必要的options/correlation；做native pushdown；映射callback/property；在emit前完成公共predicate |
| Downstream | 对已满足公共predicate的结果做产品级软过滤、排序、聚合、选择和历史状态维护 |

任何 native filter、controller offload 或 daemon filter 都只能作为 optional pushdown。backend 必须对 native positive evidence 未完整覆盖的条件做 callback/property post-filter。

统一 predicate：

```text
service_match = no service condition
             || exact native positive evidence
             || observation advertises any requested service

name_match = no name condition
          || exact native positive evidence
          || observation local name starts with requested prefix

company_match = no company condition
             || exact native positive evidence
             || observation contains requested company ID

emit = service_match && name_match && company_match
```

`allow_duplicates`只影响平台delivery hint，不参与predicate。observation缺少某个被要求字段时不匹配；native pushdown的false-positive不得上行。

### 2.3 Snapshot 是可用性提示，不替代 operation result

`AdapterSnapshot` 是某一时刻的 adapter 与 app authorization 观察：

- snapshot 可能在 command 执行前过期；
- backend 必须在 operation 执行时重新检查相关平台条件；
- `Authorized` 或 `NotRequired` 不表示 operation 一定成功；
- remote ATT authorization、device policy、GATT security 和连接状态仍通过具体 operation error 表达。

### 2.4 无法证明时使用 unknown/optional/error，不猜测

- 平台不能报告 actual notification mode 时使用 `actual_mode=None`；
- 平台不能报告 connectable 时使用 `None`；
- native transitional adapter state 压缩为 `Unknown`；
- platform-specific code/name/message 保留在 `PlatformErrorDetails` 或 structured tracing；
- strict request 无法证明满足时终结为 `NotSupported`，不得静默替换后报告成功。

## 3. `AdapterState` 与 `AuthorizationState`

### 3.1 公共维度

```rust
pub enum AdapterState {
    Unknown,
    Unavailable,
    PoweredOff,
    PoweredOn,
}

pub enum AuthorizationState {
    NotRequired,
    NotDetermined,
    Denied,
    Restricted,
    Authorized,
}
```

`AdapterState` 只描述 central adapter 是否存在以及电源可用性；authorization 是正交维度：

- `scan_authorization`：start/stop scan 与 advertisement observation；
- `connection_authorization`：connect/disconnect、GATT、notification 和 connected RSSI。

正常可执行组合是 `PoweredOn + (Authorized | NotRequired)`。`NotRequired` 只表示平台没有对应的、可动态查询的 runtime user authorization 前置条件，不表示静态声明、系统 policy 或具体 operation 不会拒绝。

### 3.2 Adapter state 映射

| 平台事实 | BleHub 映射 | 保留的信息 | 压缩或丢失的信息 |
|---|---|---|---|
| Windows `RadioState::Unknown/On/Off/Disabled` | `Unknown/PoweredOn/PoweredOff/PoweredOff` | 电源是否可用于 BLE | `Disabled` 的 hardware switch、firmware 或 policy 原因被压到 `PoweredOff`；原始 state 应进入 tracing |
| Windows 无 default adapter 或无 LE central capability | `Unavailable` | central 不可用 | 缺失原因只在 diagnostic 中保留 |
| Android `STATE_ON/STATE_OFF` | `PoweredOn/PoweredOff` | public stable state | Framework 内部 BLE-only state 会被 public `getState()` 压成 OFF |
| Android `STATE_TURNING_ON/OFF` | `Unknown` | 明确不是稳定 ready | 具体转向和进度不进入公共 enum |
| Android adapter 缺失或不支持 LE central | `Unavailable` | central 不可用 | hardware/feature 缺失原因进入 diagnostic |
| Apple `poweredOn/poweredOff/unsupported` | `PoweredOn/PoweredOff/Unavailable` | CoreBluetooth 可用性 | resetting 等 transient detail 被压缩 |
| Apple `unknown/resetting` | `Unknown` | 状态尚未稳定 | 具体 transient case 不公开 |
| BlueZ adapter `Powered=true/false` | `PoweredOn/PoweredOff` | daemon adapter power | experimental `PowerState` 的 transition/rfkill detail 默认不公开 |
| BlueZ 无 Adapter1 object | `Unavailable` | adapter 不存在 | object-manager/daemon 原因进入 diagnostic |

证据：

- Windows [`RadioState`](../platform-api-reference/windows/radiostate.md)、[`Radio.State`](../platform-api-reference/windows/radio_state.md)、[`Radio.StateChanged`](../platform-api-reference/windows/radio_statechanged.md)
- Android [`BluetoothAdapter.java`](../platform-api-reference/android/BluetoothAdapter.java)
- Apple [`CBCentralManager`](../platform-api-reference/apple/cbcentralmanager.md)、[`CBCentralManagerDelegate`](../platform-api-reference/apple/cbcentralmanagerdelegate.md)
- BlueZ [`org.bluez.Adapter`](../platform-api-reference/bluez/org.bluez.Adapter.rst)

Apple `CBManagerState` 详细页尚未保存在本地；除 `poweredOn` 可用性说明外，其余 case 映射仍需在 Apple backend 实现前补齐快照并复核。

### 3.3 Authorization 映射

| 平台 | Native authorization shape | `scan_authorization` | `connection_authorization` | 兼容与信息损失 |
|---|---|---|---|---|
| Windows | 没有 BLE scan/connect runtime consent status；`Radio.RequestAccessAsync` 只用于改变 radio state | `NotRequired` | `NotRequired` | package capability、system policy 和 per-operation `AccessDenied` 通过 operation error 表达 |
| Android | `BLUETOOTH_SCAN` 与 `BLUETOOTH_CONNECT` 分离；旧 target/OS 还有 location 条件 | effective scan permission 映射为 `Authorized/Denied/NotDetermined` | connect permission 映射为 `Authorized/Denied/NotDetermined` | Android API 不总能可靠区分 never-asked、普通 denied 和 permanently denied；不能猜成 `Restricted` |
| Apple | 一个全局 `CBManager.authorization` | 复制全局值 | 复制同一全局值 | 公共 shape 可供调用方按 operation group 使用，但 Apple 原生没有 scan/connect 差异 |
| BlueZ | 无统一 preflight authorization property；权限主要由 D-Bus method error 表达 | `NotRequired` | `NotRequired` | `NotAuthorized`/`NotPermitted` 按具体 operation 映射，不把一次方法拒绝伪装成稳定全局授权状态 |

关键边界：

- Windows [`Radio.RequestAccessAsync`](../platform-api-reference/windows/radio_requestaccessasync_380675631.md) 请求的是 radio control 权限，不能映射成 BLE scan/connect authorization。
- Android scan API 要求 `BLUETOOTH_SCAN`，GATT/notification 等要求 `BLUETOOTH_CONNECT`；见 [`BluetoothLeScanner.java`](../platform-api-reference/android/BluetoothLeScanner.java)、[`BluetoothGatt.java`](../platform-api-reference/android/BluetoothGatt.java) 和 [`BluetoothStatusCodes.java`](../platform-api-reference/android/BluetoothStatusCodes.java)。
- Apple [`CBManager`](../platform-api-reference/apple/cbmanager.md) 暴露全局 Bluetooth authorization。`CBManagerAuthorization` 详细枚举页仍需补入快照。
- BlueZ discovery/GATT 的 `NotAuthorized`、`NotPermitted` 是逐方法结果；见 [`org.bluez.Adapter`](../platform-api-reference/bluez/org.bluez.Adapter.rst) 和 [`org.bluez.GattCharacteristic`](../platform-api-reference/bluez/org.bluez.GattCharacteristic.rst)。
- Android `GATT_INSUFFICIENT_AUTHORIZATION` 是 remote ATT/GATT authorization，不是 app-level `connection_authorization`。

## 4. `ScanOptions` 平台兼容性

```rust
pub struct ScanOptions {
    pub service_uuids: Vec<BluetoothUuid>,
    pub name_prefix: Option<String>,
    pub company_id: Option<u16>,
    pub allow_duplicates: bool,
}
```

### 4.1 字段语义

| 字段 | BleHub 语义 | 不承诺什么 |
|---|---|---|
| `service_uuids` | canonical UUID any-of；空 vector 表示无 service 条件 | 不承诺平台用 controller/native filter 完成 |
| `name_prefix` | 对 mapped advertisement local name 做大小写敏感 UTF-8 prefix；`None` 无条件，空字符串非法 | 不使用 OS alias、缓存 friendly name 或地址冒充 local name |
| `company_id` | observation 中存在对应 Manufacturer Specific Data company ID | 不匹配 payload，也不验证该 ID 是否已在 SIG registry 分配 |
| `allow_duplicates` | 请求平台尽可能报告重复或变化 observation 的 hint | 不保证逐 RF packet、完整计数、固定采样率或跨平台相同 dedupe 行为 |

`allow_duplicates` 不要求 BleHub core 或 backend 维护长期 duplicate set。downstream 使用 `PeripheralId + ScanSessionId` 以及自己的产品策略决定合并、采样或保留历史。

v0.1故意不提供通用AND/OR表达式、任意closure、地址过滤、RSSI阈值或manufacturer payload mask。`service_uuids`内部any-of与三类条件之间固定AND足以表达跨FFI、跨四平台可稳定兑现的硬约束。需要更复杂boolean逻辑或软偏好的调用方应使用空或更宽松的`ScanOptions`，再在downstream处理；否则backend过滤掉的observation无法被downstream恢复。`supports_duplicate_scan_results` 也不能解释为“逐包无损”：它只描述 backend 是否能向当前公共 contract 提供可观察的重复结果能力，具体值需由平台实现和 runtime gate 固定。

### 4.2 Native pushdown 与 post-filter 矩阵

| 平台 | Service UUID | Name prefix | Company ID | Duplicates |
|---|---|---|---|---|
| Windows | 当前快照未闭环 filter API；即使可用也仅作 pushdown | 当前快照未闭环 | 当前快照未闭环 | watcher 有 observation event，但当前快照无逐包或 duplicate switch 保证 |
| Android | `startScan(List<ScanFilter>, ...)` facility 已验证；具体 `ScanFilter` 字段页缺失 | native exact/prefix 能力未由当前快照证明 | native manufacturer filter 细节未由当前快照证明 | `ALL_MATCHES` 路径存在，但不保证 controller/OEM 不去重或不丢包 |
| Apple | `scanForPeripherals(withServices:)` 原生 service filter 已验证 | 无 native prefix 证据，callback post-filter | 无 native company filter 证据，callback post-filter | options 详细页缺失；后台扫描时 scan option 无效已验证 |
| BlueZ | `SetDiscoveryFilter.UUIDs` 是 any-of，可 pushdown；多 client filter 合并后必须复核 | `Pattern` 同时匹配地址或名称，且可能被其他 client 稀释，只能预筛选 | discovery filter 没有 company 字段；从 `ManufacturerData` property post-filter | `DuplicateData=true` 只明确增强 manufacturer/service data property updates，不等价逐包 |

### 4.3 各平台处理

#### Windows

当前本地快照证明 [`BluetoothLEAdvertisementWatcher`](../platform-api-reference/windows/bluetoothleadvertisementwatcher.md) 提供 advertisement observation，但没有保存 filter、local-name、manufacturer-data 的完整属性页。Windows backend 实现前应补齐这些页面；在此之前不能把“快照缺页”写成“不支持”。无论 native pushdown 是否存在，统一 predicate 都必须在 mapped observation 上成立。

#### Android

[`BluetoothLeScanner.startScan`](../platform-api-reference/android/BluetoothLeScanner.java) 接受 `List<ScanFilter>` 并交给 system service；offloaded filtering/tracking 资源会影响某些 scan settings。当前快照没有 `ScanFilter.java`、`ScanSettings.java` 和 `ScanRecord.java`，因此 service/name/manufacturer 的精确 native 组合语义仍为 **Unverified**。backend 不应把 deprecated wrapper 的 `containsAll` 行为提升为 BleHub any-of 语义。

#### Apple

[`scanForPeripherals(withServices:options:)`](../platform-api-reference/apple/cbcentralmanager-scanforperipherals.md) 可按 service UUID 预筛选；`nil` 表示扫描全部。当前页面没有把多 UUID 精确说明为 BleHub any-of，也没有 name/company pushdown，因此 callback mapping 仍是统一正确性边界。后台扫描要求 service filter，且 scan option 在后台无效；这会削弱 duplicate hint，但不能改变公共 predicate。

#### BlueZ

[`SetDiscoveryFilter`](../platform-api-reference/bluez/org.bluez.Adapter.rst) 的 `UUIDs` 明确是 any-of。`Pattern` 匹配地址或设备名 prefix，且多个 client 的 filters 会合并；backend 必须重新检查自己的条件。`Device1.ManufacturerData` 以 16-bit ID 为 key，适合映射 `company_id`，但不能在 discovery 前 native pushdown。`DuplicateData` 的保证只覆盖文档说明的数据 property update，不得描述为逐 RF packet。

### 4.4 扫描信息损失

统一 observation 有意不保留：

- controller filter 是否 offload；
- advertisement packet 与 scan response 的原始分包边界；
- 平台内部 dedupe、batching 和采样决策；
- BlueZ filter 被其他 client 合并后的 daemon 内部状态；
- local name 来自 advertising packet 还是 scan response 的原始来源标记；
- 未映射的 vendor-specific advertisement section。

需要这些信息的产品应在未来以明确的 optional DTO/capability 扩展提出，而不是从现有字段反推。

## 5. Notification 与 Indication

### 5.1 请求与实际结果

`NotificationModeRequest` 是 command intent：

- `PlatformDefault`：允许 backend 选择；
- `RequireNotification`：严格要求 notification；
- `RequireIndication`：严格要求 indication。

`NotificationMode` 是 backend 能证明的实际结果：

- `Notification`；
- `Indication`。

strict request 无法证明满足时必须 terminal `NotSupported`。value callback 本身在四个平台上都不能可靠证明 mode；actual mode 必须来自 capability、明确的配置动作和 positive completion evidence。

### 5.2 平台矩阵

| 平台 | Mode 控制 | Enable completion | actual mode 证据 | 兼容限制 |
|---|---|---|---|---|
| Windows | 显式写 notification/indication CCCD value | CCCD async write result | backend 选择的明确 value + positive result，可报告 `Some` | `ValueChanged` 不携带 mode；late callback/drain 仍需 runtime 验证 |
| Android | 检查 properties，建立 local routing，再写 `0x0001` notification 或 `0x0002` indication | `onDescriptorWrite(..., GATT_SUCCESS)` | retained CCCD bytes + positive callback，可报告 `Some` | `setCharacteristicNotification(true)` 不是 CCCD completion；value callback 不携带 mode |
| Apple | 统一 `setNotifyValue`; dual-capability 时文档明确选择 notification | `didUpdateNotificationStateFor(..., error)` | characteristic properties + Apple selection rule；能证明时 `Some` | dual-capability 时不能强制 indication；CoreBluetooth 不暴露显式 CCCD |
| BlueZ | `StartNotify()` 无 mode 参数 | successful D-Bus reply | single-mode flags 可推导；dual-mode 时 `None` | `Notifying` 是 notification/indication 与多 session 的聚合状态 |

### 5.3 平台事实

#### Windows

[`WriteClientCharacteristicConfigurationDescriptorWithResultAsync`](../platform-api-reference/windows/gattcharacteristic_writeclientcharacteristicconfigurationdescriptorwithresultasync_2005787286.md) 接受显式 CCCD value并返回 async result。backend 应先建立 value handler，再写 CCCD；成功只表示该配置操作成功。后续 [`ValueChanged`](../platform-api-reference/windows/gattcharacteristic_valuechanged.md) 不携带 mode。

#### Android

- [`BluetoothGattCharacteristic`](../platform-api-reference/android/BluetoothGattCharacteristic.java) 分别暴露 notify/indicate property。
- [`BluetoothGattDescriptor`](../platform-api-reference/android/BluetoothGattDescriptor.java) 定义 notification、indication 和 disable bytes。
- [`BluetoothGatt.setCharacteristicNotification`](../platform-api-reference/android/BluetoothGatt.java) 只建立 local/system routing。
- descriptor write 的完成由 [`BluetoothGattCallback.onDescriptorWrite`](../platform-api-reference/android/BluetoothGattCallback.java) 表达。

Enable 必须按“local routing → CCCD write → descriptor completion”处理；中途失败需要 best-effort rollback。remote `GATT_INSUFFICIENT_AUTHORIZATION` 属于 GATT operation error，不更新 app authorization snapshot。

#### Apple

[`setNotifyValue`](../platform-api-reference/apple/cbperipheral-setnotifyvalue.md) 同时覆盖 notifications 和 indications；当 characteristic 两者都支持时选择 notification。因此：

- `PlatformDefault` 在 dual characteristic 上可得到 `Some(Notification)`；
- `RequireNotification` 可在 notify/dual capability 上兑现；
- `RequireIndication` 在 dual capability 上无法强制，必须 `NotSupported`；
- indicate-only 的 actual mode 仍需结合 characteristic properties 和成功 callback 证明。

`CBCharacteristicProperties` 与 detailed notification-state delegate 页面尚未保存，Apple backend 开始前需补齐。

#### BlueZ

[`GattCharacteristic1.StartNotify`](../platform-api-reference/bluez/org.bluez.GattCharacteristic.rst) 没有 mode 参数；`Flags` 可分别包含 `notify` 与 `indicate`，`Notifying` 则是聚合 bool。single-mode characteristic 可在 successful reply 后推导 actual mode；dual-mode characteristic 既不能严格控制，也不能可靠报告，使用 `actual_mode=None` 或对 strict request 返回 `NotSupported`。

### 5.4 Capability 的解释

- `supports_required_notification` / `supports_required_indication` 表示 backend 实现了对应 strict 语义，不表示每个 characteristic 都支持；目标 characteristic 的 properties 和 operation result仍是最终依据。
- `reports_actual_notification_mode` 表示 backend在可支持的路径上能提供可证明的 `Some(NotificationMode)`，不允许根据 value callback猜测。
- global capability 无法表达每个 characteristic 的 property差异；这是有意保留在 GATT database 与 operation result中的动态信息。

### 5.5 Notification 信息损失

公共层不保留：

- native CCCD handle/object；
- Apple/BlueZ daemon 内部选择算法；
- BlueZ 其他 client 的 notification session 数量；
- value callback 对应的 ATT packet 是 notification 还是 indication；
- indication confirmation 到达时间；
- disable 后已经进入平台 callback queue 的 late value。

`Indication` 只表示 ATT indication mode，不表示应用业务已经处理数据。

## 6. 信息保留与降级规则

### 6.1 必须保留

- caller-owned command/session/connection/subscription IDs；
- canonical `BluetoothUuid`；
- manufacturer company ID 与 payload；
- requested notification mode、backend选择和completion evidence；
- Windows HRESULT/status、Android status、Apple Error、BlueZ D-Bus error name/message；
- backend 私有 tracing 中的 raw adapter state、native flags 和 rollback result。

### 6.2 公共 enum 有意压缩

- Windows `Disabled` 的原因不进入 `AdapterState`；
- Android turning direction 不进入 `AdapterState`；
- Apple global authorization 被复制到 scan/connection 两组；
- Windows/BlueZ 没有动态 scan/connect authorization，被表示为 `NotRequired`；
- BlueZ dual notification mode不可知时使用 `None`；
- duplicate observation 不代表 RF packet 计数。

### 6.3 不允许的兼容方式

- 不用 cached device alias 冒充 advertisement local name；
- 不把 native filter capability 变成公共 predicate 的前提；
- 不把 radio-control consent 映射成 Windows BLE authorization；
- 不把 remote ATT authorization 映射成 Android app permission；
- 不把 `PlatformDefault` 的实际选择伪装成 strict request成功；
- 不从 value callback 猜 notification/indication mode；
- 不因为当前快照缺页就冻结 capability=false。

## 7. Conformance 与 runtime 验证

### 7.1 共享 mapping fixtures

每个平台至少验证：

1. 4/8/32/36 位 UUID得到相同 canonical `BluetoothUuid`；malformed input accepted前失败且零event；
2. service any-of、三类条件 AND、缺少必需字段不匹配；
3. name source不使用alias，prefix大小写敏感；
4. company filter只判断ID存在，不隐含payload匹配；
5. native filter false-positive被post-filter移除；
6. `allow_duplicates`不产生逐包保证；
7. adapter native state到公共state的完整映射；
8. scan/connection authorization独立性或明确复制规则；
9. notification default/strict、dual capability和actual mode unknown；
10. enable/disable partial failure与late value保留原`SubscriptionId`。

### 7.2 仍需 runtime 或补快照

| 平台 | 重点缺口 |
|---|---|
| Windows | advertisement filter/local-name/manufacturer pages；duplicate行为；CCCD enum/result status；close后late callback |
| Android | `ScanFilter`/`ScanSettings`/`ScanRecord`；target SDK permission矩阵；OEM duplicate与descriptor callback行为 |
| Apple | `CBManagerState`、`CBManagerAuthorization`、scan options、advertisement keys、characteristic properties；前后台duplicate差异 |
| BlueZ | ObjectManager lifecycle、polkit/system bus policy、dual notify/indicate选择、多client filter/session干扰 |

真实 RF、厂商栈、permission revocation、adapter transition和multi-client行为不能仅由 API 文档证明；对应 backend phase 必须增加 mapping tests、runtime spike 和 HIL gate。

## 8. 维护规则

1. 平台 API 快照只通过 `pixi run just docs-update` 更新，不手改四个平台目录。
2. 新平台事实先引用本地官方快照，并标明 Verified/Inferred/Unverified。
3. 本文记录解释与兼容策略；如公共 shape 或语义改变，必须先更新并 review `.plan/PLAN.md`。
4. capability 值必须由目标 SDK surface和runtime evidence支持，不能由“其他平台通常如此”推断。
5. backend实现完成后，把已验证项升级并链接具体 mapping test/HIL recipe；不要删除仍有产品影响的信息损失说明。
