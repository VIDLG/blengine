# BleHub 测试执行指南

本目录把[`.plan/PLAN.md`](../../.plan/PLAN.md)中的测试要求组织为可随实施阶段演进的执行指南。当前Plan状态为`已批准`，Windows/Android Phase 0 mechanism gate已完成，Phase 1统一薄契约实现正在进行；本文档不批准或启动Plan中尚未获批的实现工作。

## 文档定位与权威边界

- Plan 是唯一的架构与验收来源，规范公共契约、backend obligation、阶段退出条件和 v0.1 验收标准。
- 本目录只说明如何分层测试、如何组织 fixture、各 CI lane 执行哪些检查，以及如何记录结果。
- 本目录可以随目录结构、recipe 名称、runner 和报告格式演进，但不得削弱或改写 Plan 的规范性语义。
- 若本文档与 Plan 冲突，以 Plan 为准；涉及可观察行为、acceptance、阶段退出条件或公共契约的变更必须回到 Plan 评审，而不是在这里形成第二份架构 Plan。
- 测试结果只能证明已执行的场景。平台 callback、completion、disconnect、write、shutdown 或 cancellation 的语义不得强于 Plan 和原生平台证据。

## 测试层次

测试按从平台无关到真实无线环境的顺序推进。较高层不能替代较低层的确定性覆盖。

| 层次 | 首次形成阶段 | 责任 | 明确不负责 |
|---|---:|---|---|
| Contract 与 thin bridge | Phase 0 | 无平台 API、无硬件地验证 ID、dispatch/event 边界、ingress、串行 delivery 与 close 生命周期 | 判断 duplicate、orphan、stale 或业务状态是否合法 |
| `FakeBackend` 与共享 conformance | Phase 0 建骨架，Phase 1 完整化 | 通过真实 ingress/bridge 重放统一 backend contract，并固定所有 backend 的可观察行为 | 模拟 WinRT、Android、CoreBluetooth 或 BlueZ 的内部实现 |
| 平台 native mapping | 各平台 backend 阶段 | 用 private observation fixture 验证原生结果、callback 与统一 `BleEvent`/error/opaque ID 的映射 | 复制第二套 `BleBackend` contract |
| Scripted platform integration | 各平台 backend 阶段 | 驱动真实 backend 的窄 native seam 或受控 callback source，验证装配和调用路径 | 充当另一个 fake backend 或替代共享 conformance |
| HIL | 平台硬件环境可用后 | 验证真实 adapter、无线链路、外设互操作、资源释放和 soak 稳定性 | 穷举状态组合；阻塞普通 PR |

### Contract 与 thin bridge 的固定检查

此层至少验证：

- caller-owned command/session ID 原样下发，并在相关 event 中回显；调用方在 command 调用前登记这些 ID。
- backend-generated peripheral/GATT handle 作为 opaque typed ID 往返，不泄露 native object。
- backend 构造的统一 `BleEvent` 通过 bridge shape check 后原值转发，不由 bridge 合成 domain event 或业务状态。
- `EventIngressState.inner` mutex原子保护`Binding | Active | Closed` phase并串行化同一Hub的callback；downstream可自行编号，且sink不在BleHub protocol state lock内调用，因为BleHub不拥有这种状态锁。
- command 返回前的同步 callback、跨线程 ingress、close 后晚到 callback，以及重复 close。
- immediate `Err(CommandDispatchError)` 原样返回且不产生 event；accepted command 的异步成功或失败只通过 event 表达。只要 command method 返回前已经 emit event，就必须返回 `Ok(())`。
- BleHub不创建public ID allocator、BLE domain registry、process-local ingress routing registry、timer、queue或worker；backend只持有Stable UniFFI direct ingress handle。

Bridge 只负责 wire/shape、必填 ID、FFI size guard、closed 状态和串行 delivery。Backend sequence、session/handle 使用和 terminal obligation 由共享 conformance 检查；duplicate/orphan/stale/timeout 的运行时分类由测试用 downstream projection 演示和验证，不进入 BleHub production state。

### Android 本地测试设备选择

Android mechanism/instrumentation recipe 默认使用 `auto` 模式：先采用 `BLEHUB_ANDROID_SERIAL`或`ANDROID_SERIAL`显式指定的ready设备；未指定时优先唯一ready真机；没有ready真机才复用已启动的模拟器，仍没有时才启动项目管理的`medium_phone` AVD。真机与模拟器同时在线时只选择真机，并通过`ANDROID_SERIAL`与Gradle的`android.injected.device.serial`同时固定目标。多台同类ready设备不会随机选择，而是要求显式serial。

- `pixi run just android-test-device`：只报告`auto`将选择的目标；可追加`physical`或`emulator`检查显式模式。
- `pixi run just ffi-candidate-test-android`：默认`auto`，也可追加`physical`或`emulator`。
- 历史`ffi-phase0-*`与`ffi-spike-*` recipe已随旧fixture归档，不再是可执行入口；需要复核历史证据时先在独立分支显式恢复。

该fallback只适用于不依赖真实RF的FFI、ART、AAR和fake-backend instrumentation。Android BLE HIL必须显式使用`physical`模式并记录真机身份；模拟器通过绝不能替代权限、controller、vendor BLE callback或RF验收。

## `FakeBackend`、`EventScenario` 与 conformance 的职责

### `FakeBackend`

`FakeBackend` 位于计划中的 `blehub-testkit`，是测试基础设施，不是 production backend。它应：

- 记录完整 backend command，供测试断言 typed ID、payload 和调用顺序。
- 保存真实`Arc<BackendEventIngressHandle>`root，并只通过production `handle.push(event)`注入`BleEvent`，不得绕过weak state、shape validation或sink boundary；`dispose()`在所有返回路径释放root。
- 提供 deterministic/manual 推进，不依赖 sleep 或真实 timer。
- 支持同步、指定线程和并发 callback，覆盖 command 返回前事件、乱序、重复和 close 后晚到事件。
- 覆盖 immediate `Err` 且无 event、`Ok` 后延迟成功、`Ok` 后延迟失败、先同步 event 再返回 `Ok`，并把 `event -> Err` 和 `Err -> later event` 固定为 contract violation。
- 脚本化平台 timeout、caller deadline、有platform causality evidence证明cleanup/disconnect导致的实际取消、duplicate terminal/observation、stale/orphan event 和 dispose/close race。
- 提供 recording downstream sink 与 reference projection，分别验证 bridge 原样转发和下游分类。

Scenario 不携带 Windows HRESULT、Android status code 等平台细节；平台测试在同一个共享 scenario 上追加自己的 mapping assertion。

### `EventScenario`

每个 scenario 按以下数据流表达，不把测试 runner 或平台对象写进共享契约：

```text
caller-owned pre-registered IDs/state
-> public command request
-> expected backend request with identical IDs
-> expected immediate Result<(), CommandDispatchError>
-> backend-provided ordered BleEvent attempts, empty after Err
-> expected downstream accepted-event order and ingress statuses
-> optional test projection state/diagnostics
```

`EventScenario` builder 负责描述输入、backend emission attempts 和预期；`FakeBackend` 负责执行；recording sink/reference projection 负责观察。各 backend 的 conformance suite 复用同一组 scenario，不复制或改写它们的 contract 语义。

### `blehub-testkit` 代码边界

测试代码按以下单向依赖组织，避免 scenario、backend obligation 和 caller policy 混成第二套 production 状态机：

| 模块 | 责任 | 不负责 |
|---|---|---|
| `scenario.rs` | `RecordedCommand`、dispatch script 与单条 `EventScenario` 数据 | ingress、assertion、projection、平台对象 |
| `catalog.rs` | 命名的 Phase 1 场景数据和跨 backend 复用的 command/event 期望 | 执行、锁、sink behavior、平台 mapping |
| `runner.rs` | 精确 command 匹配、同步/延迟 emission、manual advance、status/attempt 记录 | backend lifecycle、caller projection、平台线程模型 |
| `conformance.rs` | 规范性的 ID、domain/terminal、error、strict mode 与 violation oracle | FakeBackend、sink、projection、平台实现 |
| `fake_backend.rs` | `BleBackend` trait 适配、capabilities、ingress root 与幂等 dispose | 定义新的契约规则或业务状态 |
| `projection.rs` | 非规范性的 caller command/session projection 示例 | 决定 bridge 是否交付、修复 backend event |
| `fixtures.rs` | advertisement、GATT 与完整 event catalog DTO | backend 执行与 ingress integration |
| `recording_sink.rs` | downstream accept/reject/panic 观察器 | scenario 与 contract 判断 |

`src`中的私有单元测试只验证 oracle/projection 的内部纯逻辑；经过真实 `BleHub -> BackendEventIngressHandle -> BleEventSink` 的场景放在 `crates/blehub-testkit/tests/`，按 `runner`、`fake_backend`、`conformance_dispatch`、`conformance_operations`、`projection`和`fixtures`分组。共享 helper 只保留机械构造与重复推进，不承载规范性断言。

### 共享 backend conformance

执行入口：`pixi run just conformance`。该 recipe 只运行 `blehub-testkit` 的平台无关 scenario、contract oracle、ingress fixture 和 caller projection；普通 `pixi run just test` 仍包含它。`pixi run just ble-fixture-check` 则通过 Bleak 和真实 RF 扫描、连接并验证 managed/LightBlue 外设 profile，需要独占本机 BLE adapter 和一个已启动广播的 fixture；它不是共享 backend conformance，也不证明尚未实现的平台 backend。

共享 fixture 只组织输入、emission 和断言责任；规范预期直接引用 Plan 的[扫描配置 6.6](../../.plan/PLAN.md#66-扫描配置)、[统一 event payload 6.15](../../.plan/PLAN.md#615-unified-event-payload)、[public event 不变量 6.16](../../.plan/PLAN.md#616-public-event)和[backend 可观察契约 7.1](../../.plan/PLAN.md#71-backend-可观察契约规范性)，不在此复制完整矩阵。共享 conformance 至少验证：

- accepted/rejected 线性化：immediate `Err` 后零 event；同步 emit event 的调用只能返回 `Ok(())`。
- caller-owned ID 和 target ID 原样回显；passive/晚到 event 保留原 session ID。
- 到达 completion point 的 accepted command 恰好尝试一次 terminal；同一 `CommandId` 至多一次 terminal attempt。
- 成功时需要domain data/state的command按`domain event -> CommandCompleted { error: None }`尝试；失败默认只发送`CommandCompleted { error: Some(error) }`。result-like data enum、`NotificationState`和terminal `Option<BleEventError>`直接提供互斥shape，不维护第二张组合表。
- failure fixture把passive/state event携带的`BleEventError`与accepted command terminal的`CommandCompleted.error`分开；terminal target从caller预登记的command解析，稳定分类从`error.category`读取。command-associated failure若按`failure state -> terminal`尝试交付，两处必须复用值相等的`BleEventError`。
- scan predicate fixture覆盖`service_uuids`中`BluetoothUuid.value`为空字符串或其他malformed文本时在accepted前返回`InvalidRequest`且零event，并以空`service_uuids` vector单独验证“无service约束”；另覆盖canonical service UUID any-of、大小写敏感name prefix、manufacturer匹配、三类已指定条件AND、缺少必需字段不匹配和`Some("")`的accepted前拒绝。service filtering始终是contract保证，不存在`supports_service_scan_filter` capability；native pushdown只是optional optimization，backend callback补过滤必须使结果始终符合Plan 6.6，bridge/downstream不代替backend过滤。
- advertisement fixture使用platform-normalized observation：字段不要求来自同一RF packet，空vector/`None`只表示本次mapped observation缺少证据；不把timestamp、scan-response flag、completeness、PHY/SID、TX power或raw bytes伪造进v0.1 DTO。
- GATT fixture断言`GattDiscovered.database`嵌套flat `service -> characteristic -> descriptor`的完整`GattDatabase`，connection/GATT session ID不在variant顶层重复，保留各层duplicate UUID instance，并固定all-or-error而非partial database。
- read/subscription conflict fixture固定同一characteristic的pending read与enable，以及enable-pending/active/disable-in-flight subscription与read，在accepted前返回`InvalidState`且零event；不得使用pending-read/next-value heuristic。
- native GATT invalidation fixture固定旧native map立即失效、旧ID command显式失败、旧callback不关联新`GattSessionId`，且不伪造`BackendError` invalidation event。
- cleanup fixture区分Plan的重复调用结果：重复stop是幂等terminal success且不伪造第二个`ScanStopped`；已移除或unknown target的重复disconnect/disable在accepted前返回`InvalidState`且零event、零重复native side effect。具体流程引用Plan [8.3](../../.plan/PLAN.md#83-停止扫描)、[8.4](../../.plan/PLAN.md#84-建立连接)和[8.8](../../.plan/PLAN.md#88-开启-notification)。
- pending-connect fixture按Plan 8.4区分cancellation evidence：只有平台证据证明本次disconnect导致原connect被取消时才为原command尝试`Cancelled`；独立发生的connect failure仍是`Failed`。不能证明该因果关系时disconnect仍可在logical lifecycle/owned resources结束后成功，原connect继续受原completion evidence与caller deadline约束，晚到结果保留原connect IDs且不得成为当前连接。
- sink 的 `InvalidEvent`/`DownstreamFailed`/`Closed` 状态、同一 command 的程序顺序和 close 后行为。
- advertisement、notification 和 passive disconnect 的 session ID；GATT opaque handle 只在声明的 connection/GATT session 内使用。
- 相同 caller-owned ID、backend attempt order 和 sink acceptance 产生相同可观察 delivery 结果。

平台 conformance 可以增加能力与原生 mapping 断言，但不得删减共享 scenario。

## 阶段演进

| 阶段 | 测试能力增量 | 主要执行位置 |
|---|---|---|
| Phase 0 | 建立 contract/bridge 闭环、`blehub-testkit`、最小 scan/connect scenario、真实 ingress 注入、跨线程与 lifecycle/FFI 回归 | `core-primary`、`core-portability` |
| Phase 1 | 补齐 adapter、scan、connection、GATT、notification 的共享 conformance 和 caller-responsibility projection | `core-primary`、`core-portability`，供后续所有 backend 复用 |
| Phase 2 | 项目Android开发Peripheral、Windows private mapping fixture、按真实调用点形成的窄native adapter、scripted conformance、package与硬件可用后的HIL | `tests/peripherals/android`、`windows`、`windows-hil` |
| Phase 3 | Android JNI/Gradle/AAR、emulator fake-backend instrumentation 和真机 RF HIL | `android`、`android-device-hil` |
| Phase 5 | Swift/XCFramework、simulator fake-backend tests 和 Apple 真机 RF HIL | `apple`、`apple-device-hil` |
| Phase 6 | guest-native Linux build、D-Bus mapping、scripted/real BlueZ integration 和双 VM RF HIL | `linux-backend`、`linux-hil` |

阶段表描述 lane 的启用顺序，不替代 Plan 的阶段工作项或退出条件。

## CI lane 分工

| Lane | Host/环境 | 执行内容与边界 |
|---|---|---|
| `core-primary` | Windows | fmt、clippy、core/event tests、Stable UniFFI binding generation与Windows production-candidate runtime gate |
| `core-portability` | 标准 Linux runner | core/event/`FakeBackend` tests，防止 core 被塑造成 Windows API；不建设 QEMU/BlueZ 环境 |
| `windows` | Windows | Windows backend、scripted conformance、package |
| `windows-hil` | Windows + QEMU Linux Peripheral VM + 2 BLE controllers | Windows Central 到 BLESS managed peripheral 的 smoke/soak；独占 runner；不阻塞普通 PR |
| `android` | Linux | JNI、Gradle unit test、AAR、Android Emulator fake-backend instrumentation |
| `android-device-hil` | Android 真机 + QEMU Linux Peripheral VM | 至少两个 API 档位的 Central 到 BLESS RF contract/smoke/soak；独占 runner |
| `apple` | macOS | Swift tests、XCFramework、simulator fake-backend tests；Apple 阶段启用 |
| `apple-device-hil` | iPhone/iPad 或真实 Mac + QEMU Linux Peripheral VM | CoreBluetooth real-device RF；Apple 阶段启用，独占 runner |
| `linux-backend` | 两台 QEMU Linux VM/runner | Phase 6 启用；guest-native build、D-Bus mapping、scripted BlueZ、real BlueZ integration |
| `linux-hil` | 2 QEMU Linux VMs + 2 passed-through USB BLE controllers | Phase 6 启用；Linux Central 到 BLESS Peripheral RF smoke/soak；每 VM 一个 controller |

具体 workflow、recipe、runner label、缓存和 artifact 路径属于执行细节，可在不改变上述责任边界的前提下更新。

## 平台测试文档索引

- [Android开发外设](android-peripheral.md)：Phase 2物理Android真机上的项目管理GATT Server、Android CLI/ADB控制与notification smoke。
- [LightBlue手工测试外设](lightblue.md)：Android项目fixture不可用时的共享basic GATT scenario手工fallback；不作为notification delivery gate。
- [Windows](windows.md)：Phase 2 native mapping fixture、窄 adapter seam、event matrix、HIL 与报告要求。
- [Managed Peripheral](managed-peripheral.md)：跨平台QEMU/BlueZ/BLESS外设、provider、doctor、scenario与硬件spike。
- [Linux](linux.md)：Phase 6 guest-native build、D-Bus/BlueZ测试层、adapter选择与双VM HIL。
- [FFI Spikes](ffi-spikes.md)：token ingress、Kotlin/JVM、Swift、typed error、race、性能与packaging gate。
- Android：在Phase 3开始时新增平台执行文档。
- Apple：在Phase 5开始时新增平台执行文档。

新增平台文档应引用本页的共享层次与 conformance 责任，只记录平台 mapping、scripted integration、HIL 环境和报告差异，避免复制公共契约。
