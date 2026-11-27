# BleHub 平台抽象、信息保留与兼容性手册

- 状态：平台事实与公共抽象维护基线；规范性契约仍以 [`.plan/PLAN.md`](../../../.plan/PLAN.md) 为准
- 日期：2026-08-30
- 范围：Windows WinRT、Android Framework BLE、Apple CoreBluetooth、Linux BlueZ D-Bus
- 官方证据：[`docs/platform-api-reference/`](../../platform-api-reference/README.md) 中的本地快照
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

- Windows [`RadioState`](../../platform-api-reference/windows/radiostate.md)、[`Radio.State`](../../platform-api-reference/windows/radio_state.md)、[`Radio.StateChanged`](../../platform-api-reference/windows/radio_statechanged.md)
- Android [`BluetoothAdapter.java`](../../platform-api-reference/android/BluetoothAdapter.java)
- Apple [`CBCentralManager`](../../platform-api-reference/apple/cbcentralmanager.md)、[`CBCentralManagerDelegate`](../../platform-api-reference/apple/cbcentralmanagerdelegate.md)
- BlueZ [`org.bluez.Adapter`](../../platform-api-reference/bluez/org.bluez.Adapter.rst)

Apple `CBManagerState` 详细页尚未保存在本地；除 `poweredOn` 可用性说明外，其余 case 映射仍需在 Apple backend 实现前补齐快照并复核。

### 3.3 Authorization 映射

| 平台 | Native authorization shape | `scan_authorization` | `connection_authorization` | 兼容与信息损失 |
|---|---|---|---|---|
| Windows | 没有 BLE scan/connect runtime consent status；`Radio.RequestAccessAsync` 只用于改变 radio state | `NotRequired` | `NotRequired` | package capability、system policy 和 per-operation `AccessDenied` 通过 operation error 表达 |
| Android | `BLUETOOTH_SCAN` 与 `BLUETOOTH_CONNECT` 分离；旧 target/OS 还有 location 条件 | effective scan permission 映射为 `Authorized/Denied/NotDetermined` | connect permission 映射为 `Authorized/Denied/NotDetermined` | Android API 不总能可靠区分 never-asked、普通 denied 和 permanently denied；不能猜成 `Restricted` |
| Apple | 一个全局 `CBManager.authorization` | 复制全局值 | 复制同一全局值 | 公共 shape 可供调用方按 operation group 使用，但 Apple 原生没有 scan/connect 差异 |
| BlueZ | 无统一 preflight authorization property；权限主要由 D-Bus method error 表达 | `NotRequired` | `NotRequired` | `NotAuthorized`/`NotPermitted` 按具体 operation 映射，不把一次方法拒绝伪装成稳定全局授权状态 |

关键边界：

- Windows [`Radio.RequestAccessAsync`](../../platform-api-reference/windows/radio_requestaccessasync_380675631.md) 请求的是 radio control 权限，不能映射成 BLE scan/connect authorization。
- Android scan API 要求 `BLUETOOTH_SCAN`，GATT/notification 等要求 `BLUETOOTH_CONNECT`；见 [`BluetoothLeScanner.java`](../../platform-api-reference/android/BluetoothLeScanner.java)、[`BluetoothGatt.java`](../../platform-api-reference/android/BluetoothGatt.java) 和 [`BluetoothStatusCodes.java`](../../platform-api-reference/android/BluetoothStatusCodes.java)。
- Apple [`CBManager`](../../platform-api-reference/apple/cbmanager.md) 暴露全局 Bluetooth authorization。`CBManagerAuthorization` 详细枚举页仍需补入快照。
- BlueZ discovery/GATT 的 `NotAuthorized`、`NotPermitted` 是逐方法结果；见 [`org.bluez.Adapter`](../../platform-api-reference/bluez/org.bluez.Adapter.rst) 和 [`org.bluez.GattCharacteristic`](../../platform-api-reference/bluez/org.bluez.GattCharacteristic.rst)。
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
| Windows | watcher filter API 可作 pushdown；当前证据不把它提升为完整公共 predicate | callback 用 advertisement `ServiceUuids`/`LocalName` 复核 | callback 用 `ManufacturerData.CompanyId` 复核 | watcher 持续交付 observation，但没有公共逐包或 duplicate switch 保证 |
| Android | `startScan(List<ScanFilter>, ...)` 可 pushdown，`ScanRecord` 负责最终字段映射 | native filter只作预筛选，callback仍按大小写敏感prefix复核 | native manufacturer filter只作预筛选，callback按company ID复核 | `CALLBACK_TYPE_ALL_MATCHES`支持重复报告；controller/OEM节流、去重和丢包仍不受保证 |
| Apple | `scanForPeripherals(withServices:)` 原生 service filter已验证 | 无 native prefix 证据，callback post-filter | 无 native company filter 证据，callback post-filter | 前台allow-duplicates key可关闭coalescing；后台scan option无效，仍不等价逐包无损 |
| BlueZ | `SetDiscoveryFilter.UUIDs` 是 any-of，可 pushdown；多 client filter 合并后必须复核 | `Pattern` 同时匹配地址或名称，且可能被其他 client 稀释，只能预筛选 | discovery filter 没有 company 字段；从 `ManufacturerData` property post-filter | `DuplicateData=true` 只明确增强 manufacturer/service data property updates，不等价逐包 |

### 4.3 各平台处理

#### Windows

[`BluetoothLEAdvertisementWatcher`](../../platform-api-reference/windows/bluetoothleadvertisementwatcher.md) 提供 advertisement observation；callback 的 [`Advertisement`](../../platform-api-reference/windows/bluetoothleadvertisementreceivedeventargs_advertisement.md) 暴露同次 received payload，后端可从 [`LocalName`](../../platform-api-reference/windows/bluetoothleadvertisement_localname.md)、[`ServiceUuids`](../../platform-api-reference/windows/bluetoothleadvertisement_serviceuuids.md) 和 [`ManufacturerData`](../../platform-api-reference/windows/bluetoothleadvertisement_manufacturerdata.md) 做最终复核。service data需要按raw data section的16/32/128-bit AD type解析；UUID prefix字节序和payload边界仍须用官方Bluetooth格式证据与fixture闭环。无论native pushdown是否存在，统一predicate都必须在mapped observation或完全等价的native positive evidence上成立。

#### Android

[`BluetoothLeScanner.startScan`](../../platform-api-reference/android/BluetoothLeScanner.java) 接受 [`List<ScanFilter>`](../../platform-api-reference/android/ScanFilter.java) 并交给system service；offloaded filtering/tracking资源会影响部分settings。[`ScanRecord`](../../platform-api-reference/android/ScanRecord.java)提供local name、service UUID、manufacturer data与service data；[`ScanSettings`](../../platform-api-reference/android/ScanSettings.java)证明`ALL_MATCHES`与immediate reporting路径。native filter和Framework parser仍不能替代BleHub callback post-filter，backend也不得把deprecated wrapper的`containsAll`行为提升为BleHub any-of语义。空options应显式允许non-legacy结果，避免Android builder默认legacy-only缩窄公共最宽stream。

#### Apple

[`scanForPeripherals(withServices:options:)`](../../platform-api-reference/apple/cbcentralmanager-scanforperipherals.md) 可按service UUID预筛选；`nil`表示扫描全部。callback的[`advertisementData`](../../platform-api-reference/apple/cbcentralmanagerdelegate-diddiscover.md)可通过对应key暴露local name、service UUID、manufacturer/service data和connectable，但官方只描述它包含“any advertisement data”，不保证每次callback完整或原样呈现全部空口AD structure。已暴露的normal与overflow service UUID都属于advertised service observation，solicited UUID不混入；当CoreBluetooth在明确service filter下返回callback时，该native match可作为所请求service的positive evidence，但不能据此补造name/company等字段。name/company没有native pushdown证据，因此callback mapping仍是统一正确性边界。前台[`AllowDuplicates`](../../platform-api-reference/apple/cbcentralmanagerscanoptionallowduplicateskey.md)可关闭coalescing；后台扫描要求service filter且scan option无效，这会削弱duplicate hint，但不能改变公共predicate。

#### BlueZ

[`SetDiscoveryFilter`](../../platform-api-reference/bluez/org.bluez.Adapter.rst) 的 `UUIDs` 明确是 any-of。`Pattern` 匹配地址或设备名 prefix，且多个 client 的 filters 会合并；backend 必须重新检查自己的条件。`Device1.ManufacturerData` 以 16-bit ID 为 key，适合映射 `company_id`，但不能在 discovery 前 native pushdown。`DuplicateData` 的保证只覆盖文档说明的数据 property update，不得描述为逐 RF packet。

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

[`WriteClientCharacteristicConfigurationDescriptorWithResultAsync`](../../platform-api-reference/windows/gattcharacteristic_writeclientcharacteristicconfigurationdescriptorwithresultasync_2005787286.md) 接受显式 CCCD value并返回 async result。backend 应先建立 value handler，再写 CCCD；成功只表示该配置操作成功。后续 [`ValueChanged`](../../platform-api-reference/windows/gattcharacteristic_valuechanged.md) 不携带 mode。

#### Android

- [`BluetoothGattCharacteristic`](../../platform-api-reference/android/BluetoothGattCharacteristic.java) 分别暴露 notify/indicate property。
- [`BluetoothGattDescriptor`](../../platform-api-reference/android/BluetoothGattDescriptor.java) 定义 notification、indication 和 disable bytes。
- [`BluetoothGatt.setCharacteristicNotification`](../../platform-api-reference/android/BluetoothGatt.java) 只建立 local/system routing。
- descriptor write 的完成由 [`BluetoothGattCallback.onDescriptorWrite`](../../platform-api-reference/android/BluetoothGattCallback.java) 表达。

Enable 必须按“local routing → CCCD write → descriptor completion”处理。Local registration 已成功而 descriptor submission/completion 失败时，只尝试一次 best-effort **本地注册补偿**：失败 enable 尝试 unregister，失败 disable 尝试 re-register，并保留原有 logical subscription；这不是补偿 descriptor write，不保证远端 CCCD rollback。补偿失败独立诊断，不替换原始 command failure；已失效/正在退役的 owner 不因此恢复。生产实现与各层验证边界见 [GATT 生命周期加固](../../testing/reports/2026-10-18-gatt-lifecycle-hardening.md)。remote `GATT_INSUFFICIENT_AUTHORIZATION` 属于 GATT operation error，不更新 app authorization snapshot。

#### Apple

[`setNotifyValue`](../../platform-api-reference/apple/cbperipheral-setnotifyvalue.md) 同时覆盖 notifications 和 indications；当 characteristic 两者都支持时选择 notification。因此：

- `PlatformDefault` 在 dual characteristic 上可得到 `Some(Notification)`；
- `RequireNotification` 可在 notify/dual capability 上兑现；
- `RequireIndication` 在 dual capability 上无法强制，必须 `NotSupported`；
- indicate-only 的 actual mode 仍需结合 characteristic properties 和成功 callback 证明。

[`CBCharacteristicProperties`](../../platform-api-reference/apple/cbcharacteristicproperties.md)已证明notify/indicate capability；[`didUpdateNotificationState`](../../platform-api-reference/apple/cbperipheraldelegate-didupdatenotificationstate.md)以`error == nil`表示配置成功。结合CoreBluetooth在dual-capability characteristic上选择notification的规则，Apple可在成功路径报告可证明的actual mode；strict indication只在indicate-only characteristic上可兑现。

#### BlueZ

[`GattCharacteristic1.StartNotify`](../../platform-api-reference/bluez/org.bluez.GattCharacteristic.rst) 没有 mode 参数；`Flags` 可分别包含 `notify` 与 `indicate`，`Notifying` 则是聚合 bool。single-mode characteristic 可在 successful reply 后推导 actual mode；dual-mode characteristic 既不能严格控制，也不能可靠报告，使用 `actual_mode=None` 或对 strict request 返回 `NotSupported`。

### 5.4 Capability 的解释

`BackendCapabilities` 回答两类问题，两者都不是“这个命令能不能用”：

- 身份与兼容性：`platform` 是装配后 `BleHub` 的唯一平台身份来源，`contract_version` 在 bind 前校验。
- 观察质量：`supports_*` / `reports_*` 声明 backend 能上报什么，让 host 在依赖某条观察前判断它是否有意义。Android 用 `supports_connectable_flag = SDK_INT >= O` 说明 API 26 前 `connectable` 不可上报；`reports_actual_notification_mode` 说明是否能给出可证明的 `Some(NotificationMode)`（不允许根据 value callback 猜测）；`supports_required_notification` / `supports_required_indication` 说明 strict 语义是否已实现（不表示每条 characteristic 都支持）。

命令不支持的唯一回答仍是提交后的类型化拒绝：`NotSupported`（映射为 `bleng.dispatch_rejected.not_supported`）。flag 不得抢先拒绝派发，也不能被读成“命令已支持”；目标 characteristic 的 properties 和 operation result 仍是 strict mode 与 actual mode 的最终依据。

### 5.5 Notification 信息损失

公共层不保留：

- native CCCD handle/object；
- Apple/BlueZ daemon 内部选择算法；
- BlueZ 其他 client 的 notification session 数量；
- value callback 对应的 ATT packet 是 notification 还是 indication；
- indication confirmation 到达时间；
- disable 后已经进入平台 callback queue 的 late value。

`Indication` 只表示 ATT indication mode，不表示应用业务已经处理数据。

## 6. Advertisement observation 字段模型

### 6.1 公共 shape 的证据

```rust
pub struct ManufacturerData {
    pub company_id: u16,
    pub payload: Vec<u8>,
}

pub struct ServiceData {
    pub service_uuid: BluetoothUuid,
    pub payload: Vec<u8>,
}

pub struct AdvertisementObservation {
    pub scan_session_id: ScanSessionId,
    pub peripheral_id: PeripheralId,
    pub local_name: Option<String>,
    pub advertised_services: Vec<BluetoothUuid>,
    pub manufacturer_data: Vec<ManufacturerData>,
    pub service_data: Vec<ServiceData>,
    pub rssi: Option<i16>,
    pub connectable: Option<bool>,
}
```

字段级来源：

| 字段 | Windows | Android | Apple | BlueZ |
|---|---|---|---|---|
| `local_name` | advertisement `LocalName` | `ScanRecord.getDeviceName()` | `CBAdvertisementDataLocalNameKey` | `Device1.Name`，但不保证来自当前包 |
| `advertised_services` | advertisement `ServiceUuids` | `ScanRecord.getServiceUuids()` | normal + overflow service UUID keys | `Device1.UUIDs`，语义是available remote services而非严格本包字段 |
| `manufacturer_data` | `CompanyId + Data` list | `SparseArray<company_id, bytes>` | 单个raw manufacturer `NSData`，company ID拆分仍需Bluetooth格式证据 | `dict<uint16, bytes>` |
| `service_data` | raw 16/32/128-bit service-data section；parser细节仍待闭环 | `Map<ParcelUuid, bytes>` | `dict<CBUUID, NSData>` | `dict<uuid, bytes>` |
| `rssi` | callback `short` dBm | `ScanResult.getRssi()` | discovery callback `NSNumber` dB | optional `int16` property |
| `connectable` | callback bool | `ScanResult.isConnectable()` | optional advertisement key | 无远端property，固定`None` |

证据：

- Windows：[`BluetoothLEAdvertisement`](../../platform-api-reference/windows/bluetoothleadvertisement.md)、[`BluetoothLEManufacturerData`](../../platform-api-reference/windows/bluetoothlemanufacturerdata.md)、[`ReceivedEventArgs`](../../platform-api-reference/windows/bluetoothleadvertisementreceivedeventargs.md)
- Android：[`ScanRecord`](../../platform-api-reference/android/ScanRecord.java)、[`ScanResult`](../../platform-api-reference/android/ScanResult.java)
- Apple：[`Advertisement Data Retrieval Keys`](../../platform-api-reference/apple/advertisement-data-retrieval-keys.md)、[`didDiscover`](../../platform-api-reference/apple/cbcentralmanagerdelegate-diddiscover.md)
- BlueZ：[`Device1`](../../platform-api-reference/bluez/org.bluez.Device.rst)

### 6.2 Observation 的统一强度

`AdvertisementObservation`是一次**platform-normalized observation**：

- 它不是raw RF packet；
- 它不是peripheral的完整或latest properties；
- fields不保证来自同一physical packet；
- empty vector或`None`只表示本observation没有可上行值，不表示设备永久没有该数据；
- manufacturer/service data list不保证保留原始重复AD structure边界或顺序。

具体损失：

- Android对同company ID的多个manufacturer sections拼接payload，对同service UUID的service data以后值覆盖前值；BleHub不重新解析raw bytes恢复边界。
- Apple和BlueZ的dictionary形态每个key最多提供一个当前值；Apple一次callback最多提供一个manufacturer-data blob。
- BlueZ property observation可能混合缓存与不同时刻property，不能描述成单包快照。
- Windows service data需要从raw section拆分UUID与payload；在parser证据闭环前仍是Windows mapping gate，不是删除公共字段的理由。

### 6.3 v0.1 不增加的 advertisement metadata

Windows提供scan-response和callback timestamp；Android提供timestamp、legacy/extended、data completeness、PHY、SID、TX power和periodic interval；BlueZ提供TxPower与AdvertisingFlags。v0.1暂不公共化这些字段：

- 当前没有跨平台业务用例要求它们；
- timestamp分属不同clock domain和观察时点，不能形成可信统一语义；
- Android completeness不能证明其他平台的observation完整，增加`Unknown`不会改善公共predicate；
- scan-response、PHY和SID属于raw/extended advertisement层，不应零散混入基础DTO。

native值保留在backend tracing与平台mapping tests。若未来需要raw advertisement API，应整体设计packet provenance、timestamp、data type和raw bytes，而不是逐个添加低上下文optional字段。

## 7. GATT database 跨平台模型

### 7.1 Instance identity 与 duplicate UUID

四个平台都要求按**native instance**寻址，UUID不是实例identity：

| 平台 | Native identity evidence |
|---|---|
| Windows | discovery API针对具体service/characteristic instance返回child objects |
| Android | service/characteristic instance ID区分同UUID实例；descriptor callback返回discovery tree对象 |
| Apple | read/write/discovery API接收具体`CBService`/`CBCharacteristic` object |
| BlueZ | service/characteristic/descriptor各有独立D-Bus object path与handle |

因此`GattServiceId`、`GattCharacteristicId`和`GattDescriptorId`必须保留，并只在`ConnectionId + GattSessionId`内有效。backend必须枚举vector/list，不使用UUID-keyed map或“first matching UUID” accessor构造database；re-discovery可以为同一native attribute生成新public ID。

### 7.2 Characteristic properties

五个公共bool在四个平台均有直接映射：

```rust
pub struct CharacteristicProperties {
    pub readable: bool,
    pub writable_with_response: bool,
    pub writable_without_response: bool,
    pub notifiable: bool,
    pub indicatable: bool,
}
```

它们是common procedure capability subset，不是native flag的无损镜像，也不保证operation一定获准。公共层有意省略broadcast、signed/reliable write、extended properties以及encrypt/authenticate/authorize等平台flags；实际security、policy和remote ATT拒绝仍通过operation error表达。

证据：

- Windows [`GattCharacteristicProperties`](../../platform-api-reference/windows/gattcharacteristicproperties.md)
- Android [`BluetoothGattCharacteristic`](../../platform-api-reference/android/BluetoothGattCharacteristic.java)
- Apple [`CBCharacteristicProperties`](../../platform-api-reference/apple/cbcharacteristicproperties.md)
- BlueZ [`GattCharacteristic1.Flags`](../../platform-api-reference/bluez/org.bluez.GattCharacteristic.rst)

### 7.3 Discovery 完整性与 cache

`GattDatabase`表示backend在本`GattSessionId`内成功构造的v0.1 traversal结果，不承诺是同一时点、uncached或remote ATT database的原子快照：

- Windows多层async result分别有status、collection和optional protocol error；backend使用明确cache policy，任一层失败即丢弃staging。
- Android successful `onServicesDiscovered`后Framework提供service/characteristic/descriptor hierarchy。
- Apple必须聚合service → characteristic → descriptor多层callback；每层`error != nil`均使整体失败。
- BlueZ在`ServicesResolved=true`后枚举自洽object tree；ObjectManager初始snapshot与signal排序仍未由当前BlueZ快照证明。

统一policy是all-or-error：所有计划内层级成功才先发`GattDiscovered`，再发terminal success；任一失败不发布partial database。empty child vector只有在完整traversal成功后才表示该层确实为空。

### 7.4 Included services 与 `primary`

四个平台都能表示included-service relation，但当前Plan没有include edge。v0.1建议继续采用flat service → characteristic → descriptor scope，不遍历、不flatten、不公开included-service graph；“完整database”只指该明确scope。

`primary`是否保留是ABI review项：

- Android、Apple和BlueZ能直接提供primary/secondary；
- Windows device-level `GattDeviceService`被定义为primary root，但included是另一个显式relation；
- 保留`primary: Option<bool>`可提供真实分类，但不能替代include graph；
- 删除它可进一步缩小DTO，但会丢失三平台直接提供、且可能用于诊断/选择的service分类。

当前研究倾向**保留`primary`并明确它只是分类，不表示include topology**；除非调用方确认该分类没有用处，不因Windows root恒为primary而删除跨平台事实。

## 8. GATT value routing 与 invalidation

### 8.1 Read/notification source ambiguity

Windows与Android分别有独立read result/callback和notification callback，可无歧义映射`CharacteristicRead`与`NotificationReceived`。Apple和BlueZ不同：

- Apple [`didUpdateValueFor`](../../platform-api-reference/apple/cbperipheraldelegate-didupdatecharacteristicvalue.md)同时由explicit read和notification value change触发，没有source/token/order discriminator。
- BlueZ `GattCharacteristic1.Value` property既在successful read后更新，也在notification/indication后更新；`PropertiesChanged`没有reason。

因此“存在pending read，所以下一次value callback一定属于read”不是可移植保证；按value相等或时间窗口去重同样会丢弃真实同值notification。

ABI冻结前必须review以下策略：

1. **推荐的最小策略**：统一禁止同一characteristic上的read与subscription生命周期重叠；backend在自己的串行边界以现有`CommandDispatchError::InvalidState`拒绝冲突dispatch，严格零event，保留现有两个清晰event。
2. `ReadCharacteristic`与该characteristic的enable/active/disable-in-flight subscription冲突；pending read也与enable冲突。disable成功后的late value是否会与紧随其后的read混淆仍需Apple/BlueZ runtime gate；无法证明安全时，backend可在本`GattSessionId`剩余生命周期继续拒绝该characteristic的read，而不能猜测callback来源。
3. 若产品必须重叠：重新设计value event/correlation；仅增加`source=Unknown`仍无法确定pending read何时完成。
4. 不允许平台backend各自使用未文档化heuristic，让相同公共API在Apple/BlueZ上产生静默误分类。

该决策不需要新增error variant，但会收紧command state语义，必须先更新并review Plan，再实现DTO/conformance。

### 8.2 GATT database invalidation

底层存在明确的数据库变化或旧对象失效信号：Android `onServiceChanged`、Apple [`didModifyServices`](../../platform-api-reference/apple/cbperipheraldelegate-didmodifyservices.md)、Windows system GATT cache invalidation和BlueZ object lifecycle。当前v0.1 Plan把service-changed/cache invalidation延后，public event没有`GattDatabaseInvalidated`。

v0.1若维持延后，backend最低义务是：

- 立即废弃受影响`GattSessionId`的native mapping；
- 不把旧对象callback关联到新session；
- 后续旧`Gatt*Id` command明确失败；
- 不用`BackendError`伪装domain invalidation；
- 文档承认downstream可能直到下一次operation failure才发现失效。

更强且更及时的候选是新增：

```rust
GattDatabaseInvalidated {
    connection_id: ConnectionId,
    gatt_session_id: GattSessionId,
}
```

收到native invalidation后整体废弃旧session，由caller用新`GattSessionId`重新discover。是否把它提升到v0.1是Plan review项，不在研究文档中静默决定。

## 9. 信息保留与降级规则

### 9.1 必须保留

- caller-owned command/session/connection/subscription IDs；
- canonical `BluetoothUuid`；
- manufacturer company ID 与 payload；
- requested notification mode、backend选择和completion evidence；
- Windows HRESULT/status、Android status、Apple Error、BlueZ D-Bus error name/message；
- backend 私有 tracing 中的 raw adapter state、native flags 和 rollback result。

### 9.2 公共 enum 有意压缩

- Windows `Disabled` 的原因不进入 `AdapterState`；
- Android turning direction 不进入 `AdapterState`；
- Apple global authorization 被复制到 scan/connection 两组；
- Windows/BlueZ 没有动态 scan/connect authorization，被表示为 `NotRequired`；
- BlueZ dual notification mode不可知时使用 `None`；
- duplicate observation 不代表 RF packet 计数。

### 9.3 不允许的兼容方式

- 不用 cached device alias 冒充 advertisement local name；
- 不把 native filter capability 变成公共 predicate 的前提；
- 不把 radio-control consent 映射成 Windows BLE authorization；
- 不把 remote ATT authorization 映射成 Android app permission；
- 不把 `PlatformDefault` 的实际选择伪装成 strict request成功；
- 不从 value callback 猜 notification/indication mode；
- 不因为当前快照缺页就冻结 capability=false。

## 10. Conformance 与 runtime 验证

### 10.1 共享 mapping fixtures

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
10. enable/disable partial failure与late value保留原`SubscriptionId`；
11. advertisement字段来自同一platform-normalized observation，empty/absent和重复key损失不被解释成canonical device state；
12. duplicate service/characteristic/descriptor UUID保留为不同opaque ID，且旧`GattSessionId` ID不复用于新discovery；
13. GATT discovery任一层失败不发布partial database，成功结果只覆盖v0.1 flat traversal scope；
14. read/subscription冲突在accepted前返回`InvalidState`且零event，不以pending-read heuristic分类Apple/BlueZ value callback；
15. native GATT invalidation至少废弃旧mapping、隔离late callback并使旧ID command明确失败；若Plan提升public invalidation event，再增加domain-event conformance。

### 10.2 仍需 runtime 或补快照

| 平台 | 重点缺口 |
|---|---|
| Windows | service-data UUID/payload parser；local-name absence；duplicate行为；RSSI `-127`；multi-layer discovery与close后late callback |
| Android | target SDK permission矩阵；OEM duplicate/extended truncation；rotating identity；service-changed与late callback行为 |
| Apple | manufacturer company-ID字节格式；read/subscription overlap；前后台duplicate；disable后的late value与service invalidation |
| BlueZ | ObjectManager lifecycle、polkit/system bus policy、read-induced `Value`歧义、dual notify/indicate选择、多client filter/session干扰 |

真实 RF、厂商栈、permission revocation、adapter transition和multi-client行为不能仅由 API 文档证明；对应 backend phase 必须增加 mapping tests、runtime spike 和 HIL gate。

## 11. 维护规则

1. 平台 API 快照只通过 `pixi run just docs-update` 更新，不手改四个平台目录。
2. 新平台事实先引用本地官方快照，并标明 Verified/Inferred/Unverified。
3. 本文记录解释与兼容策略；如公共 shape 或语义改变，必须先更新并 review `.plan/PLAN.md`。
4. capability 值必须由目标 SDK surface和runtime evidence支持，不能由“其他平台通常如此”推断。
5. backend实现完成后，把已验证项升级并链接具体 mapping test/HIL recipe；不要删除仍有产品影响的信息损失说明。
