# BleHub contract revision 7：命名收敛、规范化校验与重复清单清理

这份报告记录一次**故意的公共形状变更**：把 `BLEHUB_CONTRACT_VERSION` 从 6 升到 7，并顺带修掉
`crates/blehub` 审阅中发现的命名、校验与重复清单问题。ABI 仍未冻结，但按
`docs/research/integrations/phase0-token-ingress-runtime-results.md` 的定义，新增 required foreign
backend method 属于**不兼容修订**，必须递增并重生成审定的 snapshots。

## 1. 为什么升到 7

`BleBackend` 这一版新增了 9 个 required 方法（桌面/移动后端必须实现）：`set_adapter_enabled`、
`query_devices`、`associate_companion_device`、`read_descriptor`、`write_descriptor`、`mtu`、
`set_connection_priority`、`create_bond`、`remove_bond`、`refresh_gatt_cache`。

未升版时，一个按 revision 6 构建的后端能通过 `BackendCapabilities::validated()`，直到 App 第一次
调用某个新命令才以 `UnexpectedUniFFICallbackError` → `FfiContract` 暴露；那既不是"不支持"，也不是
装配期错误。升版后，装配期即以 `BleHubError::FfiContract` 拒绝，诊断在构造点而不是使用点。

| 项 | 决定 | 理由 |
|---|---|---|
| `BLEHUB_CONTRACT_VERSION` | 6 → 7 | 新增 required backend method 是 backend 兼容性破坏；装配期失败优先于使用期失败 |
| 版本语义 | 只在**不兼容**修订时变化，附加式变更不升 | 已在常量文档中写明；历史修订 4/5/6 的说明保留在 `.plan/PLAN.md` |
| `BackendCapabilities` 的 `supports_*` | 保留，但明确为**观察元数据** | 它们回答"App 能否依赖这条观察"，不是命令支持矩阵；命令不支持统一由 `CommandDispatchError::NotSupported` 表达，任何 flag 都不得用来抢先拒绝派发 |

## 2. 命名收敛

| 旧 | 新 | 范围 |
|---|---|---|
| `BleEvent::MtuChanged` | `BleEvent::MtuRead` | 该事件必带 `command_id`，是查询/请求的**应答**；同族 `RssiRead`/`CharacteristicRead`/`DescriptorRead` 都是应答命名 |
| `WaiterExpectation::MtuChanged` / `WaiterSuccess::MtuChanged` | `…::MtuRead` | `crates/bleng` 内部词汇与事件同源，避免两套名字 |
| `CommandEventKind::MtuChanged` | `CommandEventKind::MtuRead` | `tests/support/blehub-testkit` 的 conformance 派生 |
| `MAX_SCAN_NAME_PREFIX_BYTES` | `MAX_LOCAL_NAME_BYTES` | 该上限同时约束 `name_prefix`、`exact_name` 与 companion `display_name`，旧名只描述了三分之一用途 |

Android 的 `onMtuChanged`（平台回调）与 Swift 的 `CBPeripheralDelegate` 名称未改：那是平台词汇，
不是本契约的。

历史报告 `2026-11-25-bleng-mtu-and-api-contract.md` 保留旧名 `MtuChanged`，作为当时事实的记录，未重写。

## 3. 规范化校验

| 变更 | 位置 | 说明 |
|---|---|---|
| bonding PIN 进入 canonical 契约 | `CreateBondCommand::validated()` + `BleHub::create_bond` | 规则（1–16 位 ASCII 数字）此前只存在于 Android backend 与 RN API 文档；现在由契约拒绝，`crates/bleng` 因为 `BeginCreateBond` 也走 `hub.create_bond` 而自动共享同一条规则；Android 侧保留边界防御 |
| 校验入口统一 | `crates/blehub/src/contract/commands.rs` | 命令一律 `validated(self) -> Result<Self>`；`validate(&self)` 只留给子记录（`ManufacturerDataFilter`） |
| UUID 规范化去重 | `canonicalize_service_uuids()` | 三处相同循环（scan options / device query / companion association）合成一条，错误文案不变 |
| 掩码长度 | `ManufacturerDataFilter::matches_payload` | mask 与 data 长度不一致时**不匹配**；此前 `zip` 截断会静默比较更少字节，等于放宽过滤条件 |
| 新增事件的结构校验 | `BleEvent::validate` | `DevicesListed` 的 `key` 必须非空且同一答案内唯一；`CompanionPresenceChanged` 的 `key` 必须非空（App 会把它当 map key 或 `adoptPeripheral` 的输入） |
| name 过滤规则去重 | `validate_name_filter(field, name)` | `name_prefix` / `exact_name` / companion `display_name` 三条同规则，现在共用一个 helper |
| **扫描过滤规则单一所有者** | `ScanOptions::validated()`（public）+ `bleng-supervisor::ForeignScanOptions` | 规范规则（UUID 拼写、名字空/长度、mask 长度、report delay 上限）以前在 supervisor 又实现了一遍。现在 supervisor 只做 **representability** 转换（f64 → u16/i16/u32：非有限、有小数、越界，共用一个 `integral()` helper），然后调用 `ScanOptions::validated()`；拒绝以 `invalid_scan_options` 报出，message 仍是规范层原文 |
| 同步拒绝不丢 | 同上 | `validated()` 在 submission 前执行，错误过滤仍由 `beginScan()` **同步**拒绝，不会退化成后来的 dispatch rejection（对应 pending-operation 层的“被拒的提交是同步事实”） |
| 过滤上界命名 | `MAX_MANUFACTURER_DATA_FILTER_BYTES` | 私有常量，说明这是调用方输入上界（数值沿用与写值相同的 wire guard），不是对空口载荷的声明 |

## 4. 清理

- 删除 `crates/blehub/src/types.rs`。它与 `lib.rs` 的 `pub use contract::*` 完全重复，工作区内无人
  `use blehub::types`，且本次新增类型只补了一半 —— 重复清单必然漂移的实证。公共项仍可从 crate root
  与 `contract` 取得。
- `CreateBondCommand` 去掉无用途的 `Hash` 派生（它含 PIN 字符串，兄弟命令也都没有该派生）。

### 一项保留（先前判断已纠正）

`BackendCapabilities` 的 `supports_*` / `reports_*` 六个布尔曾被建议“无消费者、可整体退役”。复查发现
该结论基于只搜了 `*.rs`：**两个在用后端都填充它们**（`AppleBleBackend.swift`、`AndroidBleBackend.kt`），
且 `supports_connectable_flag = SDK_INT >= O` 是**真有条件**的平台事实；消费者与 HIL/FFI 契约测试也断言
它们。因此保留，改为明确文档化：它们是 **观察质量声明**（host 在依赖某条观察前判断它是否有意义），
不是命令支持矩阵；命令不支持的唯一回答仍是 `NotSupported`，flag 不得抢先拒绝派发。

## 5. Snapshots（reviewed act）

`pixi run just binding snapshot-update` 重生成 `api-snapshots/{rust,kotlin,swift}.txt`。本改动自身的
可审阅差异是四项：

```text
rust.txt:64    pub const BLEHUB_CONTRACT_VERSION: u32 = 7;
rust.txt:152   MtuRead {command_id: CommandId, connection_id: ConnectionId, info: MtuInfo,},
kotlin.txt:139 MtuRead(commandId: CommandId, connectionId: ConnectionId, info: MtuInfo),
swift.txt:138  case mtuRead(commandId: CommandId, connectionId: ConnectionId, info: MtuInfo)
rust.txt:395   pub const MAX_LOCAL_NAME_BYTES: usize = 248;
rust.txt:511   pub fn validated(mut self) -> Result<Self, CommandDispatchError>;   // ScanOptions
```

`git diff -- api-snapshots` 还包含约 1.1k 行的其它形状变化，那些是**本次改动之前就已在工作树里、尚未
随提交落地的 ABI 扩张**（descriptor I/O、MTU、priority、bonding、adapter control、inventory、
companion 等），本次只是把 snapshots 拉回与当前树一致。它们仍需单独的人工 review，不由本报告背书。

## 6. 验证（2026-11-27，host-only，无 RF）

| 证据 | 命令 | 结果 |
|---|---|---|
| Rust 全量 | `cargo test --workspace` | 632 passed, 0 failed |
| 静态检查 | `cargo clippy --workspace --all-targets -- -D warnings` | clean |
| BleHub 审定形状 | `pixi run just binding snapshot-check` | verified 3 reviewed API snapshots |
| BLeng/Supervisor 审定形状 | `pixi run just binding bleng-snapshot-check` | 32 tests OK，verified 6 |
| Apple 后端编译与前缀测试 | `pixi run just apple test` | 37 Swift tests, 0 failures（`prepare` 重生成 staged UniFFI 包，`.mtuRead` 编译通过） |
| Android 后端编译 | `pixi run just ffi android-backend-build` | BUILD SUCCESSFUL（`tests/ffi/generated/android-kotlin` 已重生成，`MtuRead` 4 处、`MtuChanged` 0 处） |
| 冻结清单 | `pixi run python -m tools.freeze_inventory` + 单测 | 通过 |
| RN 文档镜像 | `tools.tests.react_native.test_api_docs`、`docs-check-mermaid` | 6 tests OK；303 md / 28 mermaid |

**不构成**：任何 RF、真机、Android instrumented、iOS 设备/模拟器 App 构建或发布证据；也未声明 ABI
已冻结。

## 7. 后续

- 下一次平台候选构建（Android AAR / Apple package）会重新生成各自的 bindings；`tests/ffi/generated`
  是 git-ignored 的派生物，本次只由 Gradle 构建重建，未入库。
- `ForeignScanOptions` 仍保留 `invalid_company_id` / `invalid_min_rssi` /
  `invalid_manufacturer_company_id` / `invalid_report_delay` 四个 representability code（f64 不能无损
  转成整型）；它们不再承载任何语义规则。
- companion association 的 `cancel()` 在任何平台都返回 `operation_not_cancellable`；若未来某平台能
  撤回选择器，需要同时更新该能力形状与 `API.md` 的取消矩阵。
