# BleHub Windows backend 对 btleplug 与 SimpleBLE 的借鉴计划

- 状态：本地源码调研、已采用决策与后续实施计划；不修改已批准架构，最终约束以 [`.plan/PLAN.md`](../../.plan/PLAN.md) 为准
- 日期：2026-08-31
- 当前目标：为后续评审 `crates/blehub-windows` 的内部组织、WinRT 调用、生命周期和测试策略准备证据
- btleplug 本地源码：`D:\projects\contrib\btleplug`
- btleplug 审计 revision：`ee381ac34fd8600cc0faae1b8e71470db84135cf`，tag `0.12.0`，2026-03-08
- SimpleBLE 本地源码：`D:\projects\contrib\simpleble`
- SimpleBLE 审计 revision：`ceb2f295a2cca0d38d19a5a30946e8997ffc2f61`
- 当前 BleHub 实现：[`crates/blehub-windows`](../../crates/blehub-windows/)
- 相关研究：[SimpleBLE 跨平台实现与 API 对 BleHub 的借鉴评估](simpleble-cross-platform-api-assessment.md)、[平台抽象、信息保留与兼容性手册](platform-abstraction-and-compatibility.md)

## 1. 目的与结论

本文不回答“是否改用 btleplug 或 SimpleBLE”。BleHub 已批准的方向仍是 Rust + `windows-rs` 的原生 Windows backend，并继续遵守：

- `BleBackend` 使用普通非 async command method；
- 同步 `Result` 只表示 dispatch accepted/rejected；
- 最终平台与 BLE outcome 通过 `BleEvent` 上行；
- BleHub core 不拥有 runtime、executor、worker、timer、mailbox、public event queue 或 canonical BLE state；
- backend 只拥有平台调用所必需的 native handles、callback correlation、GATT operation serialization 和 cleanup state；
- caller 继续拥有 timeout、retry、cancellation、stale/late/duplicate 处理和业务状态。

本次本地源码审计的结论是：

1. **btleplug 是当前最直接的 `windows-rs 0.62` 写法参考。** 它与 BleHub 使用相同主版本的 `windows`，并使用 `windows-future`、`TypedEventHandler`、`BluetoothLEDevice`、`GattSession`、`GattDeviceService`、`GattCharacteristic` 和 `DataReader/DataWriter`。
2. **SimpleBLE 是更成熟的 WinRT 运行行为与故障经验参考。** 它明确处理 MTA、API availability、event token、native object retention、disconnect cleanup 和若干 Windows advertisement 特例。
3. **两者都不能作为 BleHub 公共语义来源。** 两者都拥有自己的 runtime/state/cache/timeout/error/object 模型，并在 duplicate UUID、partial discovery、subscription rollback、identity 或 cleanup 上存在与 BleHub contract 不兼容的行为。
4. **最理想的借鉴方式不是复制实现，而是提炼 native observation、资源 ownership、状态映射和测试用例。** 每项先与本地官方 WinRT 快照核对，再决定进入 mapping fixture、runtime spike 或 production backend。

## 2. 证据边界

### 2.1 本地源码状态

本报告只使用当前 workspace 中的本地源码和项目本地官方 API 快照，没有使用在线文章。

审计时：

- btleplug 产品源码与 `0.12.0` tag 一致，worktree 只有未跟踪的 `.codegraph/`；
- SimpleBLE 产品源码与上述 revision 一致，worktree 只有未跟踪的 `.codegraph/`；
- 没有构建或运行 btleplug/SimpleBLE，也没有据此执行 Windows RF；
- btleplug integration tests 需要真实或虚拟 Peripheral，不能把测试文件存在视为当前 Windows 行为已经通过；
- 本报告记录的“源码行为”不等于 Microsoft 平台保证。

### 2.2 证据标记

| 标记 | 含义 |
|---|---|
| **Verified-source** | 当前本地 revision 的源码直接表现出的行为 |
| **BleHub policy** | 已批准 Plan 或共享 conformance 固定的统一语义 |
| **Inferred** | 可从本地实现合理推出，但没有 runtime/RF 复现 |
| **Spike-required** | 涉及 driver、COM apartment、cache、RF、callback 时序或 OS 版本，必须通过窄 spike 验证 |

引用 btleplug 和 SimpleBLE 的注释时，只能证明维护者按该假设实现过，不能自动提升为 Windows API 保证。

## 3. 三种架构不可混淆

| 维度 | btleplug Windows | SimpleBLE Windows | BleHub Windows |
|---|---|---|---|
| 公共调用模型 | Tokio/async trait、Stream | 阻塞同步方法、callback | 非 async dispatch、event outcome |
| 事件传输 | `AdapterManager` stream、Tokio broadcast | callback slot | retained `BackendEventIngressHandle` |
| Peripheral 模型 | `Arc<Shared>` canonical mutable object | shared backend object/cache | caller/session IDs + backend-private native map |
| timeout/retry | library 内部存在 | library 内部存在 | caller policy，backend不得擅自增加 |
| GATT identity | 主要按 UUID map | 主要按 UUID map | session-scoped opaque ID，保留 duplicate UUID instance |
| error | 通用 crate error | exception/`OperationFailed` | unified category + operation + platform details |
| runtime ownership | Tokio sync/time/channel | `MtaManager`、blocking wait、condition variable | core无runtime；backend只保留平台必需执行机制 |

因此，“借鉴 btleplug”在本文中只表示审计其 WinRT 事实、Rust projection 技巧和资源处理，不表示引入 `btleplug` dependency、Tokio API 或公共 `Peripheral` object。

## 4. btleplug 可借鉴的内容

### 4.1 `windows-rs` dependency 与 projection 位置

**Verified-source：** btleplug `Cargo.toml:85-87` 使用：

- `windows = 0.62`；
- `windows-future = 0.3.2`；
- `Devices_Bluetooth`、`Devices_Bluetooth_GenericAttributeProfile`、`Devices_Bluetooth_Advertisement`、`Devices_Radios`、`Foundation`、`Foundation_Collections`、`Storage_Streams`。

这与 BleHub 当前 dependency 方向一致。btleplug 可作为以下机械问题的本地参考：

- 哪个 WinRT async type 来自 `windows-future`；
- `IAsyncOperation` 转 future 与 completed handler 的类型形状；
- `TypedEventHandler` 的 sender/args projection；
- `DataReader`/`DataWriter` 与 `IBuffer` 转换；
- `GUID`、characteristic properties 和 CCCD enum 投影。

**适配要求：** BleHub不能照搬 `.await` 调用链。需要把相同 native operation 接入 backend-owned completion callback/correlation，并保留 fire-and-forget command contract。

### 4.2 Native device/session/object retention

**Verified-source：** `src/winrtble/ble/device.rs` 的 `BLEDevice` 同时保留：

- `BluetoothLEDevice`；
- `GattSession`；
- connection-status token；
- max-PDU token；
- discovered `GattDeviceService` objects。

其 `Drop` 会：

1. `RemoveMaxPduSizeChanged`；
2. `RemoveConnectionStatusChanged`；
3. 对 service 调用 `Close()`；
4. 对 device 调用 `Close()`。

**借鉴结论：** BleHub应采用同样明确的 ownership list，但不能只依赖 `Drop`：

- command lifecycle结束、disconnect、GATT invalidation和backend `dispose()`必须显式清理；
- `Drop`仅作为不应影响可观察contract的安全兜底；
- token、native object、session ID和generation应处于同一个 backend-private connection/GATT context；
- cleanup失败进入structured tracing或`BleHubError::BackendCleanup`，不能只写debug log。

### 4.3 非 `Send` WinRT collection 不能跨 await

**Verified-source：** `BLEDevice::discover_services` 将 WinRT `IVectorView` 先收集成 `Vec<GattDeviceService>`，注释说明 `IVectorView` 不能跨后续 await 保持 `Send`。

**借鉴结论：** 这是重要的 `windows-rs` projection约束。BleHub虽然不使用async trait，仍应：

- 尽快把 WinRT collection/iterator 转成 backend-owned Rust `Vec`；
- 不把临时 collection view 存入跨callback状态；
- mapping fixture覆盖 collection读取中途失败；
- native object需要保留时保留明确的 projected object，而不是保留隐式view。

### 4.4 Advertisement service data 原始 section 解析

**Verified-source：** `src/winrtble/peripheral.rs:183-235` 说明当前使用的 WinRT surface没有直接提供友好的service-data map，因此从 `Advertisement.DataSections()` 识别 AD type `0x16`、`0x20`、`0x21`，解析16/32/128-bit service UUID和payload。

**候选借鉴：** BleHub Windows advertisement mapper可参考这一入口和buffer处理，但在实施前必须：

- 与本地 Microsoft API快照和Bluetooth AD格式核对字节序；
- 对空/短/malformed section做有界解析，不能使用 `unwrap` 或越界 `split_at`；
- 明确同一callback内的字段和backend聚合字段边界；
- 添加16/32/128-bit service data mapping fixture；
- 使用 BleHub `BluetoothUuid` canonicalization和`ServiceData` DTO，不复制btleplug map shape。

### 4.5 `IReference<i16>` 的 projection footgun

**Verified-source：** `src/winrtble/peripheral.rs:283-291` 记录 `TransmitPowerLevelInDBm()` 返回的reference在该projection下应使用 `Value()`；其他看似合理的方法曾导致panic。

**候选借鉴：** 在BleHub的`windows-rs 0.62`组合上增加一个纯mapping/native projection test，确认：

- absent TX power映射为`None`；
- present value通过非panic路径读取；
- callback boundary不允许projection panic越过WinRT ABI。

这条注释属于实现经验，不是官方保证，仍需本地测试确认。

### 4.6 Connection 与 GATT session 建立路径

**Verified-source：** btleplug的路径是：

```text
BluetoothLEDevice::FromBluetoothAddressAsync
-> GattSession::FromDeviceIdAsync
-> ConnectionStatusChanged
-> MaxPduSizeChanged
-> uncached GetGattServices 触发/确认连接
```

**候选借鉴：** 该调用序列可作为BleHub connection spike输入，重点验证：

- 创建`BluetoothLEDevice`与真正connected evidence之间的差异；
- `GattSession`是否需要显式维护；
- connection status callback注册窗口；
- service query作为连接触发手段时，成功/失败/late callback如何关联原`ConnectionId`；
- disconnect时drop/close native objects产生什么callback；
- pending connect的disconnect是否提供足够causality evidence标记原command为cancelled。

btleplug的“async connect返回后即成功”不能直接定义BleHub event completion point。

### 4.7 GATT operation 的机械写法

**Verified-source：** btleplug展示了：

- read使用`ReadValueWithCacheModeAsync(Uncached)`和`DataReader`；
- write根据`WriteType`选择`GattWriteOption`并检查`GattCommunicationStatus`；
- descriptor read/write采用相同buffer转换；
- characteristic property映射为bit flags；
- notification通过`ValueChanged` handler和CCCD write实现。

**借鉴结论：** 可以借用API选择和buffer转换方式，但BleHub必须重新实现：

- command correlation；
- operation serialization；
- domain event与terminal event顺序；
-完整 status/protocol error mapping；
- close/disconnect时pending operation处理；
- session-scoped opaque ID lookup；
- notification/indication strict request和actual mode报告。

### 4.8 Integration scenario catalog

**Verified-source：** btleplug `tests/` 和 `test-peripheral/` 已组织以下跨平台真实Peripheral场景：

- service filter、advertisement services/manufacturer data；
- connect/disconnect/reconnect/passive disconnect；
- service/characteristic/descriptor discovery；
- read、write with response、write without response、long value；
- notification、indication、unsubscribe；
- descriptor roundtrip；
- MTU、RSSI和connection parameters。

**借鉴结论：** 借鉴scenario名称、覆盖维度和fixture可编程性，不复制其API assertion。BleHub继续使用自己的`tests/conformance/scenarios`、Android development Peripheral和未来BLESS fixture，并额外检查command/event、typed ID、terminal、late/stale和cleanup语义。

## 5. btleplug 不能照搬的内容

### 5.1 Tokio runtime、channel 和 canonical `Peripheral`

**Verified-source：** btleplug `Peripheral::Shared`持有：

- `tokio::sync::Mutex<Option<BLEDevice>>`；
- `DashMap<Uuid, BLEService>`；
- Tokio broadcast channel；
- connected/MTU atomics；
- advertisement name/RSSI/manufacturer/service data/service UUID等累积状态。

这些是btleplug公共async/object模型的一部分。BleHub不能引入：

- Tokio dependency；
- library-ownednotification broadcast queue；
- public canonical Peripheral object；
- library-owned cumulative advertisement history；
-由库决定的channel容量和lag/drop策略。

### 5.2 Watcher/radio event token没有完整ownership

**Verified-source：** `BLEWatcher::start`调用`Received(&handler)`但不保存返回token，也没有对应remove；`Adapter::new`注册`Radio::StateChanged`时同样不保存token。

**Inferred：** 如果同一watcher重复start，handler可能累积或至少缺少显式生命周期证明。无论实际WinRT source如何保留handler，这都不满足BleHub的可审计cleanup要求。

BleHub当前scan实现保存received/stopped token并在失败、stop callback和dispose路径显式remove，这一点应保持，不能向btleplug退化。

### 5.3 Scan filter语义不匹配且有driver workaround

**Verified-source：** btleplug watcher刻意清空native service UUID filter，注释称部分Windows driver对128-bit UUID filter会静默丢失匹配结果，改由callback软件过滤。其软件predicate要求所有请求service都出现。

BleHub policy不同：

- native pushdown只作优化；
- backend callback补过滤保证统一语义；
- `service_uuids`是any-of，不是btleplug的all-of；
- name prefix和company ID还要与service条件做AND；
- observation缺少必要字段时不匹配。

应借鉴“native filter不可信时post-filter”的方向，但不能复制all-of predicate。128-bit filter driver问题必须用当前目标机和Android fixture做spike，不能直接宣称为平台事实。

### 5.4 UUID map丢失duplicate instance

**Verified-source：** btleplug使用：

- `DashMap<Uuid, BLEService>`；
- `HashMap<Uuid, BLECharacteristic>`；
- `HashMap<Uuid, BLEDescriptor>`；
- discovery时只保留第一个同UUID characteristic；
- `BLEService::to_service`把`primary`固定为`true`。

这与BleHub contract直接冲突。BleHub必须：

-保留每一个native service/characteristic/descriptor instance；
-为每个instance分配session-scoped opaque ID；
-UUID只作为属性，不作为唯一key；
-准确映射native primary evidence，无法证明时使用`None`；
-rediscovery后旧ID立即失效。

### 5.5 Partial discovery 与错误吞并

**Verified-source：** btleplug在部分characteristic discovery失败时记录warning后继续；descriptor discovery失败可回退为空vector；某些非success status返回空characteristic列表。

BleHub v0.1是all-or-error `GattDatabase`。不能把partial database伪装为完整成功。每层失败必须终结discover command，并清理本次临时native map。

### 5.6 Library-owned cache timeout和fallback

**Verified-source：** btleplug characteristic/descriptor discovery对Uncached调用设置Tokio timeout，超时后自动回退Cached；service discovery本身使用Cached。

这提供了Windows cache行为的候选实验矩阵，但不能复制为BleHub默认策略，因为：

- timeout由caller拥有；
-自动fallback会改变freshness语义；
- cached/uncached成功不等于database未变化；
- timeout后的原operation可能仍晚到；
- fallback结果如何与原`GattSessionId`关联需要明确。

需要先用fixture验证Cached/Uncached在首次连接、重连、Peripheral重启和database变化时的行为，再决定Windows backend policy并写入Plan或平台手册。

### 5.7 Error mapping过于粗糙

**Verified-source：** btleplug `utils::to_error`把：

- `AccessDenied`映射为permission；
- `Unreachable`映射为not connected；
- `ProtocolError`映射为not supported；
-其他状态压成communication error。

它还大量把`windows::core::Error`格式化为字符串。BleHub不能复制这种映射：

- ATT protocol error不天然等于`NotSupported`；
-必须区分dispatch HRESULT、async completion、`GattCommunicationStatus`和`ProtocolError`；
-保留operation、category、message和platform details；
-同一command的failure state与terminal复用值相等的统一error；
-不能因为读取status失败就改成`DeviceNotFound`。

### 5.8 Subscription transaction不满足BleHub rollback

**Verified-source：** btleplug `BLECharacteristic::subscribe`先注册`ValueChanged`并保存token，然后才检查mode和写CCCD；若property不支持或CCCD write失败，没有立即remove token。`unsubscribe`则先remove handler，再写CCCD None；CCCD失败后native和logical状态可能分离。

BleHub需要failure-atomic transaction：

1. accepted前验证target/session/conflict和requested mode capability；
2. 注册handler；
3. 写CCCD；
4. 成功后提交active subscription context并报告actual mode；
5. 任一步失败都回滚已注册token和临时state；
6. disable失败时保留可证明的真实state，不能无条件宣布disabled；
7. disconnect/dispose显式remove handler并使旧subscription ID失效。

### 5.9 Notification mode由properties隐式选择

**Verified-source：** btleplug `to_descriptor_value`在同时支持notify/indicate时优先indicate，公共subscribe不表达strict request。

BleHub已经有`PlatformDefault | RequireNotification | RequireIndication`，并要求报告actual mode。不能照搬隐式优先级；Windows backend必须按request和native capability选择或明确失败。

### 5.10 `read_rssi`不是connected RSSI operation

**Verified-source：** btleplug Windows `read_rssi`只返回最近一次advertisement callback缓存的RSSI，不发起connected RSSI native operation。

这不能证明Windows支持BleHub的`ReadRssiCommand`。当前`supports_read_rssi`必须继续由官方surface和runtime evidence决定；不能把scan RSSI伪装成connected RSSI completion。

### 5.11 Disconnect语义过强且过早

**Verified-source：** btleplug `disconnect`清空service map、drop device、设置connected false并立即emit disconnected，不等待WinRT callback证明native link状态。

BleHub必须区分：

- logical connection/session结束；
- owned native handles已释放；
-平台是否提供实际disconnect observation；
- pending connect是否真的被本次disconnect取消；
-晚到connected/disconnected callback属于哪个generation。

不能复制“drop object后立即把native disconnect当作已确认”的语义。

## 6. SimpleBLE 提供的补充与反例

已有[`simpleble-cross-platform-api-assessment.md`](simpleble-cross-platform-api-assessment.md)负责完整跨平台评估；本文只记录与Windows implementation计划直接相关的差异。

### 6.1 值得交叉采用的证据

SimpleBLE Windows比btleplug更明确地展示：

- adapter构造和阻塞WinRT调用被安排在MTA context；
-使用`ApiInformation::IsPropertyPresent`检查`AllowExtendedAdvertisements`；
-保存scanner received/stopped和radio state event token；
-扫描callback过滤RSSI `-127` synthetic out-of-range event；
-连接后保留service/characteristic/descriptor objects；
-被动断连时显式`Close()` service并清空GATT map；
- read/write/CCCD检查`GattCommunicationStatus`；
- read和discovery大量使用`BluetoothCacheMode::Uncached`。

这些应转化成BleHub mapping fixture或spike问题，而不是直接复制C++类。

### 6.2 不能复制的策略

SimpleBLE同时包含BleHub明确拒绝的策略：

-全局`MtaManager`与blocking `async_get`；
-固定十秒WinRT wait；
-connect内部自动重试三次；
-scan stop内部condition-variable等待；
-deferred disconnect与sleep；
-按UUID/object map形成canonical mutable Peripheral；
-`OperationFailed`丢失native status；
-access-denied service可跳过并产生partial GATT database；
-subscribe注册handler后CCCD失败缺少完整rollback；
- timeout、retry和状态恢复由library替caller决定。

### 6.3 两个库互相矛盾时的处理

典型差异包括：

| 问题 | btleplug | SimpleBLE | BleHub处理 |
|---|---|---|---|
| WinRT执行 | async/Tokio | MTA worker + blocking wait | 先做apartment spike；默认completion callback，不引入core runtime |
| service discovery cache | Cached | Uncached | 用重连/database变化fixture验证后决定 |
| characteristic cache | Uncached timeout后Cached | Uncached | 不自动fallback，先确定平台事实和late-operation风险 |
| scan native service filter | 禁用并软件过滤 | 未建立同等公共predicate保证 | native pushdown可选，BleHub post-filter保证正确性 |
| duplicate UUID | 丢失 | UUID lookup也不能稳定区分 | BleHub opaque instance ID保留全部实例 |
| subscription mode | properties隐式选择 | notify/indicate分方法 | BleHub按request选择并报告actual mode |
| disconnect completion | drop后立即更新状态 | deferred/condition state | BleHub按native evidence和logical cleanup分开建模 |

当两者冲突时，不投票决定；回到本地官方API快照和窄runtime/RF spike。

## 7. 当前 `blehub-windows` 已经做对的部分

当前Phase 2 slice已经实现adapter initialization、scan/stop和logical connect/disconnect；connect使用`FromBluetoothAddressWithBluetoothAddressTypeAsync -> ConnectionStatusChanged -> Uncached GATT service probe -> ConnectionStatus`作为完成证据，临时service立即关闭，pending connect disconnect保留原correlation且不伪造`Cancelled`。该路径已通过源码编译、deterministic crate tests和共享conformance，但尚未执行真实RF connect；GATT discovery/read/write/notification仍显式返回未实现。与两个参考库相比，以下设计应保留：

1. **没有引入Tokio或blocking MTA manager。** WinRT async通过completed handler进入backend state和统一event。
2. **显式保存async operation和event token。** 初始化、scan registration失败和dispose都有可审计cleanup路径。
3. **scan generation防止旧callback污染新session。** btleplug和SimpleBLE当前公开实现没有同等明确的session correlation。
4. **command ID和scan session ID原样关联。** native callback不能自行选择当前command。
5. **同步dispatch与最终event分离。** `watcher.Start()`之后的平台失败通过event表达，而不是伪装成accepted前rejection。
6. **公共scan predicate由backend保证。** native filter不是contract正确性的唯一来源。
7. **Peripheral key至少包含address和address type。** 比只按地址缓存更谨慎，但其跨scan/地址轮换边界仍需继续评审。
8. **事件交付与state lock分开。** 不在backend protocol-state mutex内调用下游。
9. **重复stop遵守BleHub contract。** 不照搬第三方库的通用幂等或阻塞stop语义。

因此后续改进应是内部可维护性、native mapping完整性和测试增强，而不是替换现有command/event模型。

## 8. 候选内部组织

`backend.rs`已经同时承担lifecycle、adapter/radio、scan、advertisement mapping、error mapping、event grouping和cleanup。继续直接加入connection和GATT会使ownership与状态转换难以审计。

建议后续讨论以下拆分；这里只是候选组织，不是已批准改动：

```text
crates/blehub-windows/src/
├── backend.rs       # BleBackend impl、顶层lifecycle、assembly/dispose
├── adapter.rs       # BluetoothAdapter、Radio、AdapterSnapshot mapping
├── scan.rs          # watcher、tokens、generation、scan state
├── advertisement.rs # WinRT advertisement observation与DTO mapping
├── connection.rs    # BluetoothLEDevice、GattSession、connection generation
├── gatt.rs          # discovery context、opaque native maps、serialized operations
├── subscription.rs  # 仅在重复逻辑形成后保存ValueChanged/CCCD transaction
└── error.rs         # HRESULT/AsyncStatus/Gatt status/protocol error纯mapping
```

拆分原则：

- 按native ownership/lifecycle边界拆，不按每个public command机械建文件；
-不增加第二套Windows public DTO或`WindowsBackend`镜像trait；
- observation type保持crate-private plain data，只用于mapping test；
-小型event-token guard只封装注册/解除和ownership，不隐藏command状态机；
- explicit dispose仍是规范路径，`Drop`只做best-effort安全兜底；
-不要提前建立通用WinRT async framework；出现至少两个相同且稳定的rollback模式后再抽取。

## 9. 候选实施顺序

以下顺序记录实施依赖。9.2中的最小connection context与logical disconnect已经落地；其中runtime/RF行为仍需真实fixture验证，其余步骤继续作为后续slice计划。

### 9.1 先做源码级整理和测试seam

1. 把adapter/radio和scan从单文件拆到native ownership模块，不改变行为。
2. 将advertisement/error mapping提取为可构造plain observation的纯函数。
3. 固定scan registration rollback、start/stop callback、generation和dispose tests。
4. 为每个event token记录owner、注册点、显式remove点和fallback drop点。

### 9.2 Connection context与runtime验证

1. 已采用`BluetoothLEDevice::FromBluetoothAddressWithBluetoothAddressTypeAsync`取得device object，再以Uncached service query触发连接；只有`GattCommunicationStatus::Success + ConnectionStatus::Connected`完成connect，未引入`GattSession.MaintainConnection`。
2. 当前继续使用WinRT completion callback，不因SimpleBLE存在MTA worker就复制；COM apartment/MTA是否需要backend-private执行上下文仍由runtime evidence决定。
3. 已建立backend-private`ConnectionContext`，保存caller IDs、generation、device operation/device、connection token、service probe和最小late-callback correlation。
4. 已实现active/passive logical disconnect、pending connect retirement、late completion隔离和dispose cleanup；active/passive race、pending disconnect、reconnect及真实WinRT cleanup顺序仍需deterministic seam与RF验证。

### 9.3 再做all-or-error GATT discovery

1. 用Uncached/Cached矩阵验证首次连接、重连、Peripheral restart和database变化。
2. 在临时context中完整枚举service/characteristic/descriptor，全部成功后一次提交。
3. 为每个native instance分配opaque ID，不按UUID去重。
4. discovery失败关闭临时native objects，旧database和新database不混用。
5. rediscovery成功后原子替换map并使旧ID失效。

### 9.4 再做serialized GATT operations

1. 明确Windows是否允许并发native GATT calls以及callback/completion顺序。
2. backend只为平台所需的GATT serialization保存最小queue/correlation，不引入通用runtime。
3. read/write/CCCD operation分别固定accepted、completion、error和late result。
4. caller timeout不取消或重写backend事实；晚到event保留原command/session ID。

### 9.5 最后做subscription transaction和RF

1. properties/requested mode accepted前验证；
2. ValueChanged registration与CCCD write做failure-atomic transaction；
3. 报告actual notification/indication mode；
4. notification payload关联原subscription和GATT session；
5. disable/disconnect/dispose清理token和native object；
6. 使用Android fixture完成notification和indication RF gate；
7. USB controller到位后再进入QEMU/BLESS fault injection与soak。

## 10. 建议转化成测试的第三方经验

| 经验 | 来源 | BleHub测试位置 | 状态 |
|---|---|---|---|
| event token注册失败必须回滚前序token | 两者 + 当前实现 | Windows scripted lifecycle | 应固定 |
| repeated watcher start不得累积handler | btleplug反例 | Windows scripted/resource counter | 需新增 |
| radio handler必须显式remove | btleplug反例 | initialize/dispose test | 需新增 |
| RSSI `-127`不是普通advertisement | SimpleBLE | advertisement mapping + RF | Spike-required |
| 128-bit native service filter可能漏包 | btleplug注释 | Android fixture scan comparison | Spike-required |
| TX power reference读取不得panic | btleplug | projection/mapping test | 需新增 |
| service data 16/32/128-bit解析 | btleplug | mapping fixture | 需新增 |
| device/session/service对象释放顺序 | 两者 | connection/dispose counter seam | 需新增 |
| duplicate UUID instance全部保留 | 两者反例 | GATT mapping/conformance | 已有contract，Windows需实现 |
| partial discovery不得成功 | 两者反例 | GATT failure fixtures | Windows需实现 |
| subscribe CCCD失败移除handler | 两者反例 | subscription rollback | Windows需实现 |
| unsubscribe失败不伪造disabled | 两者反例 | subscription state fixture | Windows需实现 |
| disconnect后旧native map立即失效 | 两者经验 | reconnect/rediscover fixture | Windows需实现 |
| cached/uncached不静默fallback | btleplug策略反例 | cache policy spike | Spike-required |
| scan RSSI不能代替connected RSSI | btleplug反例 | capability/mapping test | 应固定 |

## 11. 后续评审议题

在修改现有`blehub-windows`前，建议依次讨论并形成明确结论：

1. **是否现在先做无行为变化的文件拆分？** 还是先完成connection最小slice后按真实ownership拆分？
2. **Windows execution/apartment策略是什么？** 继续直接completed handler，还是runtime evidence证明需要backend-private MTA executor？若需要，如何保证它不变成BleHub core runtime？
3. **scan配置是什么？** active/passive、extended advertisements、native service pushdown分别采用什么默认和fallback？
4. **Peripheral identity key的生命周期多长？** address + address type在同一次backend lifetime内是否足够，地址轮换如何处理？
5. **connection completion point是什么？** device创建、GattSession创建、service query还是ConnectionStatus callback提供哪种证据？
6. **GATT cache policy是什么？** service/characteristic/descriptor分别使用Cached还是Uncached，是否允许显式fallback？
7. **GATT serialization如何实现？** callback chain、backend-private operation queue或其他最小机制；如何close/drain并保留原command correlation？
8. **subscription transaction顺序是什么？** handler与CCCD的提交/回滚、disable失败和actual mode如何表达？
9. **错误映射矩阵是什么？** HRESULT、AsyncStatus、GattCommunicationStatus、ProtocolError和BluetoothError如何进入统一category/details？
10. **哪些第三方经验必须先做RF spike？** 不把维护者注释或单机workaround直接升级成BleHub policy。

## 12. 最终建议

后续Windows实现应采用以下主次：

1. 本地Microsoft API快照决定平台surface与官方语义上限；
2. btleplug提供`windows-rs 0.62` projection、buffer、event和native object写法参考；
3. SimpleBLE提供MTA、driver workaround、资源释放和真实维护经验参考；
4. BleHub Plan/conformance决定command/event、ID、error、timeout、cleanup和跨平台语义；
5. 两个第三方实现的缺陷或差异转化为mapping fixture、rollback test和runtime/RF spike，而不是复制进production。

本报告不建议新增btleplug或SimpleBLE dependency。除非未来发现一个独立、低层、无runtime且经过审计的辅助crate能显著降低WinRT机械风险，否则应继续直接使用`windows-rs`，并以小型backend-private helper吸收重复代码。
