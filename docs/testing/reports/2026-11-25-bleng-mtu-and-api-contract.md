# MTU 与连接优先级：两条有界命令、两种平台真相，以及随之收敛的 API 文档

这份报告记录三件事：把 MTU 和连接优先级各做成**一条**有界命令并打通到 RN API；把 API 文档从
“这两项未暴露”改成真实行为（Android 协商/应用、Apple 只读/拒绝）；以及一次由此触发的 `Result`
误差体积修复。ABI 仍未冻结。

## 1. 设计决定

| 项 | 决定 | 理由 |
|---|---|---|
| MTU 命令形状 | `MtuCommand { command_id, connection_id, request: Option<u16> }` | `None` 只查询，`Some(mtu)` 才协商；查询和请求共用一条命令、一个结果类型 |
| MTU 范围 | `MIN_MTU = 23`，`MAX_MTU = 517`，在 `BleHub` 校验 | 越界是 `InvalidRequest`，在 accepted 之前拒绝，零 event |
| MTU 结果 | `MtuInfo { mtu?, maximum_write_with_response?, maximum_write_without_response? }` | 三个字段都可缺失；“平台没说”不能写成 `0` |
| MTU 证据 | `BleEvent::MtuChanged` + `CommandCompleted` | 与应用层其他读写一致：值事件不是终态 |
| 优先级命令 | `SetConnectionPriorityCommand { command_id, connection_id, priority }` | 平台不回报生效模式，就不发明一个值事件 |
| 优先级回执 | 只有 `CommandCompleted` | 成功只表示平台接受了请求 |
| 平台差异 | Apple 请求 → `CommandDispatchError::NotSupported` | 映射为 `bleng.dispatch_rejected.not_supported`；不是静默成功，也不是 timeout |
| JS 形状 | `connection.beginQueryMtu()` / `beginRequestMtu(mtu)` / `beginSetConnectionPriority(priority)` | 遵循既有 `begin*` 命名，`beginPending()` 直接可用，没有新增便捷层 |

关键语义：**一次被接受的提交不是一次已协商的 MTU，也不是已生效的链路调度**。

## 2. 落地位置

- `crates/blehub`：`CommandKind::{Mtu, SetConnectionPriority}`、`MtuCommand`/`MtuInfo`/`ConnectionPriority`/
  `SetConnectionPriorityCommand`、`BleEvent::MtuChanged`、`BleBackend::{mtu, set_connection_priority}`。
- `crates/bleng`：`BeginMtu`/`BeginSetConnectionPriority`、`MtuOperation`/`SetConnectionPriorityOperation`、
  `MtuReservation`/`PriorityReservation`、`WaiterExpectation::{MtuChanged, ConnectionPriority}`。
- `crates/bleng-supervisor`：`SupervisedMtu`/`SupervisedConnectionPriority`、
  `ForeignMtu{Begin,Value}Result`/`ForeignPriorityBeginResult`。
- `platforms/android`：MTU 走 `requestMtu`+`onMtuChanged`（查询读跟踪值，payload = `mtu - 3`）；
  优先级走 `requestConnectionPriority`，bool 即回执（无回调）。
- `platforms/apple`：MTU 只报 `maximumWriteValueLength`（`mtu` 恒为 `nil`），请求抛 `NotSupported`；
  优先级同样 `NotSupported`。
- RN：Nitro spec、Kotlin/Swift bridge、`API.md` / `API.zh.md` 的 `## MTU` 与 `## Connection priority`。

## 3. 误差体积：`PlatformErrorDetails` 变重后必须装箱

本轮同时给 `PlatformErrorDetails` 加了 `status`/`att_code`（数值诊断）。这让 `BleEventError` 恰好到
128 字节，`BlengError`/`WaiterError`/`CommandProgress` 随之变大，`cargo clippy -D warnings` 的
`result_large_err`（122 处）和 `large_enum_variant`（1 处）全部报红。

处理方式不是放宽阈值，而是在三处**错误嵌入点**装箱（`Option<Box<PlatformErrorDetails>>`）：
`BleEventError`、`BleHubError::BackendCleanup`、`CommandDispatchError::BackendUnavailable`。
UniFFI 把 `Box<T>` 视为透明（`TypeId<Box<T>> = TypeId<T>`），因此生成 binding 完全不变：
Kotlin/Swift 仍是 `PlatformErrorDetails?`，只有 reviewed Rust 快照记录 `Box`。

## 4. 实际验证

| 命令 | 结果与边界 |
|---|---|
| `pixi run just test::all` | 595 passed / 25 suites |
| `pixi run just test::supervisor`（`--all-features`） | 169 passed / 6 suites；含 descriptor 单消费者回执与 retire 隔离 |
| `BLEHUB_APPLE_UNIFFI_PACKAGE_PATH=… swift test --package-path platforms/apple` | 99 passed / 0 failures；含 descriptor 相关性与 retire 后回调隔离 |
| `pixi run just binding::bleng-snapshot-check` | 32 checker tests passed + 6 snapshots verified |
| `pixi run just binding::snapshot-check` | 3 reviewed snapshots verified |
| `pixi run just binding::freeze-inventory` + `python -m unittest tools.tests.test_freeze_inventory` | inventory 重生成；4 passed |
| `pixi run just lint-common` | fmt / clippy(`-D warnings`) / ruff / profiles 全过 |
| `pixi run just ffi::android-bleng-build` | 单 image AAR + artifact-only consumer static build passed；**ART 未运行** |
| `pixi run just react-native::bleng-android-unit-test` | passed（补齐 descriptor Kotlin import 与 `descriptors()` 返回类型后） |
| `pixi run just react-native::stage-apple-bleng` | 三 slice package/symbol 检查 passed |
| `npm run typecheck`、`quickstart-typecheck`、`formal-host-check` | passed（host 的 `descriptors()` 投影改为 capability 对象） |
| `python3 tools/tests/react_native/test_api_docs.py`、`just docs-check-mermaid` | 6 passed / 27 diagrams |

## 5. 边界（不是证据的部分）

- 没有真机 RF 证据：Android 只到静态 consumer build 与单元测试，ART instrumentation 未运行；
  没有真实 Central 触发 `onMtuChanged`、`onDescriptorRead`/`onDescriptorWrite`，也没有验证调度
  真的变化。
- iOS Apple backend 有本地 `swift test`（99 passed），但 RN iOS host 完整编译未在本轮执行。
- `not_supported`（MTU 请求、连接优先级、bonding）只由 Apple 后端路径与合成测试覆盖，未在 iPhone 上实测。
- ABI 未冻结：`MtuCommand`/`MtuChanged`/`SetConnectionPriorityCommand`/`ReadDescriptorCommand`/
  `WriteDescriptorCommand`/`DescriptorRead`/`DescriptorWritten`/`CreateBondCommand`/
  `BondStateChanged`/`BondState` 属于未冻结 candidate 的 shape 演进，冻结前需并入下一个 revision
  说明。

## 6. descriptor 读写（已落地）

`descriptor` 读写已在同一轮的收尾中**落地到所有层**，不再是被回退的能力：

- **blehub**：`ReadDescriptorCommand`/`WriteDescriptorCommand`、`CommandKind::{ReadDescriptor,
  WriteDescriptor}`、`BleEvent::{DescriptorRead, DescriptorWritten}`、`BleBackend::{read_descriptor,
  write_descriptor}`，`WriteDescriptorCommand::validate()`。
- **bleng**：两个 reservation（`descriptor_read`/`descriptor_write`）接入 `IoReservations`，
  与 characteristic I/O 共用 reservation、wait、cancel 和单消费者回执模型。
- **bleng-supervisor**：`SupervisedGattDescriptor` 成为能力对象；`characteristic.descriptors()` 返回
  `Vec<Arc<SupervisedGattDescriptor>>`，原来的 `ForeignGattDescriptor` 纯数据记录被删除。
- **Apple**：`GattIoController` 新增 `descriptorRead`/`descriptorWrite` 两类 callback，
  `CallbackKey.descriptorIdentity` 做相关性；`didReadDescriptor`/`didWriteDescriptor` 消费回执，
  retire 后回调被隔离。CoreBluetooth 支持 descriptor I/O，所以 Apple **不**返回 `not_supported`。
- **Android**：`PendingIo.DescriptorRead/DescriptorWrite` + `onDescriptorRead`/`onDescriptorWrite`；
  `onDescriptorWrite` 先判通用 descriptor 且 `descriptor ===` 命中，再落回 CCCD 通知路径，
  避免把 CCCD 回执吞掉。
- **RN**：`BlengGattDescriptor` 变成 HybridObject，新增 `BlengDescriptorRead`/`BlengDescriptorWrite`；
  `descriptor.beginRead()` → `{ value }`，`descriptor.beginWrite(value)` **恒为 with-response**
  （descriptor 在链路上没有 write type，调用不接收 write-type 参数）。

bonding 仍延迟：Android 有 `createBond` + `ACTION_BOND_STATE_CHANGED` 广播，Apple 无公开等价 API，
需要 bond 状态模型与用户交互失败语义。

## 7. 绑定 bonding（已落地）

绑定作为连接上的有界 operation 落地，回执是平台报告的终态，不是“已加密”的承诺：

- **blehub**：`BondState { None, Bonding, Bonded }`、`CreateBondCommand`、`CommandKind::CreateBond`、
  `BleEvent::BondStateChanged { command_id: Option<..>, connection_id, state }`、
  `BleBackend::create_bond`。允许 12 个 test double 及两端生产后端显式实现（UniFFI foreign trait
  不允许 default 方法）。
- **bleng**：`BondReservation` 接入 `IoReservations`，与 MTU 同形（终态是值证据，`Bonding` 不算
  成功）；`CreateBondOperation` 的 `wait` 返回 `BondState`。
- **bleng-supervisor**：`SupervisedBond` 能力对象 + `ForeignBondBeginResult` / `ForeignBondValueResult`。
- **Apple**：`createBond` 校验连接后返回 `CommandDispatchError::NotSupported`（CoreBluetooth 无公开
  发起 API）；`not_supported` 语义不被系统托管的受保护访问配对冒充。
- **Android**：`create_bond` 检查 `getBondState()`；已绑定立即回 `bonded`，否则 `createBond()` + 复用
  adapter 广播 receiver 收听 `ACTION_BOND_STATE_CHANGED`；`BOND_NONE` 失败只发带错误的
  `CommandCompleted`（不伪造域事件）；连接移除/dispose 清理 pending bond。
- **RN**：`BlengBond` HybridObject + `BlengBondState`；`beginCreateBond()` / `beginRemoveBond()` → `{ bond }`，
  `bond.wait()` → `{ state }`；两个方向共享一个 `BondOperation`，由方向决定成功终态（create=`bonded`、
  remove=`none`）；文档 `API.md` / `API.zh.md` 增加 `## Bonding` 章节。

验证：`binding::snapshot-update/check`、`binding::bleng-snapshot-update/check`（含 32 个 snapshot
checker tests）、`freeze-inventory`、`test::all`(595)、`test::supervisor`(169)、Apple `swift test`(99)、
`ffi::android-bleng-build`（静态）、`react-native::bleng-android-unit-test`、`npm run typecheck` /
`quickstart-typecheck` / `formal-host-check`、`lint-common`、`test_api_docs`(6)、`docs-check-mermaid`(27)。
