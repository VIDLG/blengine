# write/CCC 系统 loss：provider 协议与 Apple native lane 观察前置项

日期：2026-10-26

## 结论与范围

承接 [平台 loss 关闭条件](2026-10-26-platform-loss-closure-readiness.md) 的两个代表性组合：

1. active response-write + 已接受的 queued CCC enable。
2. 已建立订阅上的 active CCC disable + 已接受的 queued response-write。

本轮补齐 **provider-only 控制协议**、**Apple backend 的只读 native FIFO 观察端口**和**既有 BLeng iOS HIL 的 witness-only 场景**，没有宣称完成系统 loss / same-Hub gate。witness emitter 已接线并通过 generic device/Simulator unsigned build、确定性测试与 Swift → Python provider 协议回放；provider CLI / active-loss host、手机独占控制/恢复和同 owner 完整恢复流程仍待接线。没有增加 App、通用 runner/registry、firmware 或核心 API。

**后续 API 补充：**用户随后批准公开 operation 只读 `command_id()`，理由是产品的 command/event/error/log 关联，不是改变测试前置条件。参见 [关联身份决策与验证](2026-10-26-bleng-operation-command-id.md)。这允许 begin 成功后立即得到已提交请求的身份，但不代表 backend 准入或 native FIFO pending，且未来 subscription 仍需实际 admission；本报告的 ARM 后实际 dispatch/lane binding 协议不回退为预测 ID。

未运行真机/RF入口、修改 radio/权限、刷机、签名准备或长稳；未 commit/push/branch。`crates/blehub`、`crates/bleng` 未为本轮 Peripheral 测试修改，既有未冻结 ABI 不由本报告冻结。

## 1. 为什么不能复用旧模式名

- 现有 adapter/permission active-loss coordinator 明确 ARM READ，并按 READ/旧 Probe 身份和恢复协议验证；换成 write/CCC 名称不会补出对应物理边界。
- 现有 response-write / CCC REJECT coordinator 在连接存活时执行 REJECT。系统 loss 必须要求实际 GAP detach 与 DISCONNECT receipt，不能用 REJECT/COMMIT 代替。
- v2 `FaultController.arm(ArmRequest(...))` 已支持 RESPONSE_WRITE 与 CCC_WRITE；不需要新固件或复制 wire codec。
- BLeng 同步 begin 仅表示提交成功，既不证明 backend dispatch 接受，也不证明 native lane 的排队位置。必要观察必须来自真实生产 backend。

## 2. Provider-only 控制协议

新增 [system_loss.py](../../../tests/peripherals/nrf52840/system_loss.py)，借用调用者已经启动、live-attested 且持有物理租约的 `DirectFixture`、BTP session 与 doctor handle map。没有第二个串口 owner、进程 registry 或 radio 操作。显式用例名称为：

- `response-write-queued-ccc-enable`
- `ccc-disable-queued-response-write`

### 身份绑定顺序

```text
prepare_control：冻结 intent、已知 native owner/init、发现的 route 与 payload
  -> arm：用当前 peer/epoch/本地 getter 验证的真实 target ARM
  -> 实际 backend dispatch-return + native lane witness
  -> observe：验证 fresh HELD，一次性保存动态 binding
  -> authorize_off：重新采样 HELD/peer/epoch/deadline，返回 provider OFF readiness
  -> 实际系统动作由另一个已授权的 host 层负责
  -> poll_disconnect：真实 GAP detach + retained disconnect receipt
```

初版在 ARM 前冻结未来 command/subscription ID，与当时未公开 command_id getter 的 BLeng surface 不相容：begin 只返回 handle，enable 的订阅 ID 也尚未出现。即使后续 handle 公开 command_id，ID 仍在 eager submission 时才产生，不能为 ARM 前准备而预测未来操作或订阅身份。审查后先写失败测试，再改为 **不预测未来 ID**：

- control 只保存已经存在的初始化身份、GATT route 和操作意图。
- disable 用例要求已经观察到的 baseline command/subscription；write+enable 不要求未来 subscription。
- head/sibling command ID 与未来 enable subscription 从 ARM 后的实际 backend 接受记录获取，要求与初始化/baseline command 互异。
- 独立 native lane 记录必须指出同一 head pending、同一 sibling queued；单独 begin-return 或提交日志被拒绝。
- `system-loss-binding.json` 一次性保存，后续控制、binding 文件和累计 witness 不可漂移。OFF 请求绑定 control、binding 与 witness 的摘要。

身份包括 session/token、native PID/boot、owner/backend UUID、初始化 command、发现的 connection/GATT/characteristic/CCC descriptor。物理目标由现有 `resolve_target` 和本地 getter 验证；CCC descriptor 不混同 characteristic value handle。`native_witness_scope` 明确是 host 提供的协作观察，不是独立 RF attestation。

### 安全边界与证据

- 复用 `ArmCommon`、`ArmRequest`、`FaultController`、live receipt 与物理租约检查；ARM 前后重新核对 peer、boot/connection/bearer/database epoch。
- 当前 OFF budget 从 ARM 开始绝对 8 秒，轮询不续期；用于在现有 10 秒 Apple native watchdog 前留出余量，不表示任何 UI 动作一定能在该窗口完成。未来 host 必须绑定实际 watchdog 配置并验证动作预算，不能静默延长 watchdog。
- `authorize_off()` 只表示 **provider readiness**。证据始终 `radio_authorized=false`，不能将其解释为用户授权或手机已 OFF。
- 不调用 COMMIT/REJECT 制造断链。完成需要实际 GAP detach、精确 `Winner.DISCONNECT` / `Outcome.DISCONNECTED_UNCOMMITTED`、未提交副作用及 cleanup-ready/idle。
- retained receipt 单独保存为 `system-loss-disconnect-receipt.json`，在任何后继 ARM 前持久化；provider 不发布 same-Hub recovery 或最终 gate pass。
- 失败仅在租约/identity/非 quarantine 安全条件下清理本次 owned hold；primary error 与 cleanup/publication errors 分开。终态成功后错误的重复调用不能改写完成证据。

当前状态是 `prepared → armed → held-pending → off-ready → disconnected-retired` 或 `failed`；`recovery` 保持 `not-evaluated`。

## 3. Apple 真实 native FIFO 观察端口

扩展既有 `AppleDiagnostics` SPI，而非 Stable UniFFI contract：

```swift
public func diagnosticSnapshotGattIo(
    connectionId: ConnectionId,
    completion: @escaping @Sendable (AppleGattIoSnapshot?) -> Void
)
```

来源：[AppleBleBackend](../../../platforms/apple/Sources/BleHubApple/AppleBleBackend.swift) 与 [AppleGattIoController](../../../platforms/apple/Sources/BleHubApple/GattIoController.swift)。

`AppleGattIoSnapshot` 是 value-only 快照，包含 connection、head kind/command、head state 和 logical FIFO 中位于 head 之后的 queued command IDs。它不持有 CB 对象，不驱动 I/O，不增加 timer/queue/registry，不改变取消、watchdog 或 generation barrier。

- callback 在 backend 原有 serial queue 执行，与命令/delegate 排序。
- `awaitingNativeCallback`、`waitingForWriteCapacity`、`submittingWriteWithoutResponse`、`awaitingDrive` 和 `idle` 分开，不能用一个 pending count 冒充目标 head 已进入 native callback window。
- internal retire-subscription head 没有 caller command ID；先移除 logical head 再过滤 nil ID，避免误丢其后的第一个真实 queued command。
- 使用 logical `headIndex...`，不把已消费但尚未 compact 的 storage 当作 pending。
- 未连接/已退休的 connection 返回 nil；仍 connected 但没有 IO queue 返回 idle。

该端口现已接入下述 BLeng HIL 累计 witness emitter。其 unsigned build 与确定性回归不等于本场已采到真实 native pending；不能把测试 fixture 对 controller 的调用或字符串 `source=native-io-lane` 当作新的真机证据。

### 既有 BLeng iOS HIL 的 witness-only 接线

实现位于既有 App 的 [SystemLossScenario.swift](../../../tests/hil/apple/bleng-ios/Sources/SystemLossScenario.swift)、[SystemLossWitness.swift](../../../tests/hil/apple/bleng-ios/Sources/SystemLossWitness.swift)、[SystemLossWitnessBackend.swift](../../../tests/hil/apple/bleng-ios/Sources/SystemLossWitnessBackend.swift) 和 [SystemLossArguments.swift](../../../tests/hil/apple/bleng-ios/Sources/SystemLossArguments.swift)，由显式 `--rf-system-loss-witness <case>` 选择；还需 run ID、service UUID、name prefix 与 before/requested bytes，不内置设备地址、ATT handle、产物 hash 或未来 ID。

- 一个 production Apple backend、一个 Bleng owner、一次 initialize、一个 bounded event reader。thin forwarding wrapper 仅在实际 delegate 成功返回后记录接受，不能把同步 begin 当作 native admission。
- 从真实 typed discovery database 与对应 public event 派生 route，实际 read 验证 before-state；disable case 另需已观察的 baseline enabled/EF。
- setup → host control → base witness → matching provider ARMED 后才开始 one-shot head/sibling。operation `commandId()` 提供真实请求身份，CCC subscription 来自实际 backend DTO。
- 当前 provider 没有独立 head-held 前置 marker：明确采用实际 head backend-return 后**仅一次** sibling submission；若 actor 返回 Busy/其他 admission error，保留真实错误并停止，不 sleep 猜 admission，不 retry。
- 只有 fresh production FIFO 同时显示匹配 head `awaitingNativeCallback`、唯一 matching queued sibling，且没有它们的 terminal event，才导出 pending witness。provider HELD 必须回绑同一 ticket、control、witness 与 binding digest。
- 保持 typed handles 到停止/清理；文件采用 run-local atomic export 与 frozen prefix 检查。无 radio 控制、无 loss 后恢复；终态始终 `scope=native-witness-only`、`gate=pending`、`rf=not-evaluated`、`same_hub_recovery=not-evaluated`。

构建发现既有 [ScanScenario.swift](../../../tests/hil/apple/bleng-ios/Sources/ScanScenario.swift) 与 [RecoveryScenario.swift](../../../tests/hil/apple/bleng-ios/Sources/RecoveryScenario.swift) 的三个 discovery call site 仍把 `GattDatabase` 当 `GattSessionId` 使用，现改为保留 typed database、读取 `.gattSessionId` 并核对 public discovery event。新场景的 advertisement 泛型推断也已显式标注；修复后 device/Simulator HIL App 均完成 unsigned build，不再以 artifact consumer build 代替 HIL 编译。

## 4. 测试与实际执行

### Provider

[test_system_loss.py](../../../tests/peripherals/nrf52840/test_system_loss.py) 与 owner-local [BTP emulator](../../../tests/peripherals/nrf52840/support/system_loss_emulator.py) 驱动真实 FaultController/序列化边界，native facts 显式为 synthetic。覆盖两种 head、ARM 后动态 ID 绑定、begin-only 拒绝、错 descriptor/peer/epoch/payload、早到/过期 OFF、receipt/累计 witness 变更、quarantine、租约丢失、disconnect-vs-expiry、primary/cleanup 和完成证据不可改写。

- 初始 API 缺失为结构红，不夸大为已发现生产故障。
- 动态 binding API、完成后 finish_failure 不改写成功、quarantine/清理及证据发布的失败用例先红后绿；详细失败保留日志。
- 定向最终 **24 passed**；与 fault-control、CCC enable/disable provider 的相关组合 **67 passed**，已包含 24，不重复相加。

### Apple

[GattIoControllerTests](../../../platforms/apple/Tests/BleHubAppleTests/GattIoControllerTests.swift) 与 [AppleBackendLifecycleTests](../../../platforms/apple/Tests/BleHubAppleTests/AppleBackendLifecycleTests.swift) 增加/扩展观察断言：两种混合队列、取消/消费后的 ID、no-response capacity/local submission、internal head、重复读取不推进 FIFO、不清 barrier/generation、missing/disposed queue。

新增诊断 API 不存在时结构编译红；实现后 targeted **48 passed**。完整 Apple backend **97 passed**，并附 **17 项 deterministic scenario/configuration tests passed**。不是 native callback timing、RF 或平台恢复 pass。

### Witness / 跨语言维护回归

[SystemLossWitnessTests.swift](../../../tests/hil/apple/support/SystemLossWitnessTests.swift) 的 19 项测试覆盖成功 delegate return 与 early callback 区分、reject 不记 accepted、累计 witness/route/owner 不可漂移、stale snapshot 不伪装 pending、one-shot Busy/其他 admission failure、绝对 deadline 和 provider HELD binding。连同原有 RF selection/configuration 共 **36 passed**。

维护入口 [test_rf_scenario.py](../../../tests/hil/apple/support/test_rf_scenario.py) 现在自动导出本轮 Swift fixture，并由真实 provider `prepare_control` / `SystemLossCoordinator` 消费，不另写 Python schema：

- 两个 case 均走 ARM → actual witness validation → binding → fresh OFF readiness → synthetic GAP detach → retained disconnect receipt。
- 逐字节检查 sorted compact JSON、explicit null 和 control/witness digest，保护超过 `2^53` 的两个 opaque command IDs；数字 ID、浮点 round-trip ID、缺 null 字段与错误摘要均被实际 validator 拒绝。
- 使用 owner-local BTP emulator，不访问 USB、SDK、安装、radio 或 RF。跨语言首次运行暴露 emulator 的 GATT getter reply 长度固定为 1 字节，已按实际 payload 长度编码；没有修改生产 BTP codec 或放宽 provider 校验。
- 初始红是测试 harness 不支持导入 native control 的结构缺项，随后出现上述 emulator 长度错误；不称为生产 BLeng bug。跨语言回放是软件互操作证据，不是独立 native/RF attestation。

### Native host 状态机前置实现

新增 `tools/apple/system_loss_host.py`，并把 `NATIVE_SYSTEM_LOSS_SCOPE` 接入现有 `IosRadioLease` 的同一物理手机锁/恢复校验；`tools/tests/react_native/test_ios_system_loss_host.py` 现有 10 项无设备测试（含 native lease 消费）。它是后续 live runner 唯一应使用的 host-side protocol owner，不执行 xcrun、XCUITest、手机、provider 或 RF：

- `pending → off-ready → radio-off → disconnected → restored-on → recovered → finished` 是唯一成功顺序；provider 的 `radio_authorized=false` readiness 不等于用户授权。
- OFF 只能调用一次；OFF 结果不确定时只能调用一次 ON 恢复，失败进入 `quarantined`，不自动重试，也不把恢复成功当 gate 通过。
- host 只在相应阶段生成 immutable `authorize-loss` / `restore-on` relay command；App 必须校验 run、control、witness 和 terminals 摘要后才可从 pending 进入 loss terminal 或 same-owner recovery。
- 既有 BLeng HIL App 新增 `BlengAppleIOSProbeUITests` UI-testing target；`SystemLossBluetoothSettingsUITests` 只用 Settings accessibility cell/switch，精确验证 ON/OFF readback，不使用坐标、Settings URL 或自动 alert 接受。UI 在 OFF 前等待 `off-authorized`，恢复 ON 前等待 host 的 `restore-on-authorized`。
- `FileRadioSwitch` 是 host 与该 UI ledger 的唯一 adapter：host 写授权 marker、读取 signed UI ledger，UI test 执行唯一 tap；两边不各自操作 Settings。
- disconnect receipt 必须是同一 control/binding/witness 摘要对应的 `disconnected-uncommitted`；旧操作必须一一对应真实 terminal。
- recovery 必须保持相同 native PID/boot/owner/backend，并且 connection/GATT/subscription 使用新身份；旧 command ID 不能被新 link 重用。
- primary error、cleanup errors、mutation count、恢复次数和每个阶段的 switch readback 都进入 canonical JSON；它不能单独证明平台行为。

### 整合命令与边界

| 实际命令 | 结果 |
|---|---|
| `pixi run python -m unittest tests.peripherals.nrf52840.test_system_loss -q` | 定向 24 passed |
| `pixi run python -m unittest discover -s tests/peripherals/nrf52840 -t . -p 'test_*.py' -q` | provider 完整发现集 436 passed，25.900s |
| `pixi run just apple test` | Apple backend 与 deterministic HIL/configuration 检查通过；完整 backend 另保存 97 项原始计数 |
| `pixi run just apple build-ios` | generic iOS device / Simulator unsigned build passed |
| `pixi run just apple bleng-ios-apple-init-build` | active-loss phase 接线后重新执行 passed：既有 BLeng HIL App 的 generic iOS device / Simulator unsigned image 均通过 |
| `pixi run -e apple-ci python tests/hil/apple/support/test_rf_scenario.py` | 本轮 active-loss phase 接线后 37 passed；20 项 SystemLoss witness + 17 项既有 RF/configuration，另两种 Swift → Python provider interop case 均 passed，synthetic / no RF |
| `pixi run python -m unittest discover -s tests/peripherals/nrf52840 -t . -p 'test_*.py' -q`（witness 接线后重跑） | 本轮 436 passed，28.762s；未与前次 436 重复相加 |
| `pixi run just fmt` / `pixi run just lint` | passed；新 provider/测试/emulator 三文件另经 scoped Ruff check / format --check 通过 |
| `pixi run just docs-check-mermaid` / `git --no-pager diff --check` | 27 个图 / 181 个 Markdown 文件通过；diff 无 whitespace error，仅三个既有文件的 CRLF→LF 提示；两个本轮 authored Markdown 文件通过 UTF-8/fence/whitespace/conflict-marker 检查 |
| `pixi run just apple bleng-ios-system-loss-ui-build` | 本轮新增 UI-testing target 的 `build-for-testing` passed，导出唯一 `.xctestrun`；不启动 UI、不操作 radio |
| `pixi run python -m unittest tools.tests.react_native.test_ios_system_loss_host tools.tests.react_native.test_ios_radio_lease -q` | 11 项 native host/UI contract + 12 项既有 iOS lease 测试，共 23 passed |
| `pixi run just react-native tools-test` | **120 秒工具上限超时，未完成、不得计为 passed**。日志仍在既有 RN recovery-ten synthetic 回归，出现既有 fixture advertisement / foreign payload-url 噪声与后续 synthetic passed marker，未看到最终 unittest 汇总；检查无本次遗留测试进程。新 host contract 的独立 8 项已单独通过。 |

日志前缀 `.work/validation/2026-10-26-system-loss-`：`provider-{red,green,binding-red,binding-green,final,all}.log`、`lane-{red,green,regression}.log`、`apple-ios-build.log`、`tools-fixture.log`、`fmt.log`、`lint.log`、`mermaid.log`。witness 接线与跨语言重跑日志为 `.work/validation/2026-10-26-bleng-loss-witness-{app-build-recheck,tests-recheck,interop-tests,provider-all,fmt,lint,mermaid,diff}.log`；互操作初次结构红单独保存于 `2026-10-26-bleng-loss-interop-red.log`。本轮文档检查为 27 个图 / 182 个 Markdown 文件通过。另新增 `tools/apple/system_loss_host.py` 及 `tools/tests/react_native/test_ios_system_loss_host.py`：8 项无设备测试通过，进入既有 `tools-test` discovery；该状态机只拥有协议与证据规则，不执行 xcrun、XCUITest、手机、provider 或 RF。provider 新测试进入既有 canonical discovery，跨语言检查进入既有 Apple deterministic 入口，不新增平行测试入口。没有因组合套件超时删除用例、放宽断言或修改无关 host 代码；本轮未改 JS/RN App，未重复 Jest/UI 全矩阵。

## 5. 尚未接通，不能直接运行的部分

1. **真实候选的 witness 采集。** emitter 已在既有 HIL 接线并通过 unsigned build / 软件协议回放，但尚未在这两个 case 的真实 candidate/session 取得 native FIFO + provider HELD 证据；不能用软件 fixture 或 low-level RN Probe 替代。
2. **provider/relay 生命周期与 native active-loss radio host。** 现有 iOS startup-off runner 的身份/控制 schema 绑定 RN startup，不是可随意换 App 的通用控制器。复用已有 SDK capture、Settings 开关定位和同一手机锁命名空间，新增具名 native active-loss 控制与严格 restore/quarantine 校验，而非让 native gate 启动 RN App。ARM 前完成 UI runner 预热和慢动作；ARM 后仍受绝对 8 秒 provider budget 限制，不能沿用单次 30 秒文件 copy 或重新导航 Settings 的长等待。provider readiness 本身不能授权 radio。
3. **同一 owner 上的 loss 结算、真实恢复与最终账本。** 现有 witness-only 场景仍把 setup/pending 期间的 terminal 当作失败，结束后 shutdown；不能直接把它当 active-loss。需按阶段区分授权前早到完成、授权后的真实 typed/public terminal、本地 wait timeout/native watchdog，再在原 owner 上显式新 connect/GATT/subscription，验证真实 read/write/value、新 ID、旧 handle/迟到结果隔离、native retirement 和最终 cleanup。后续 loss/recovery ledger 与冻结的 provider witness 分离，不向固定 witness schema 塞新事件。provider 的 DISCONNECT receipt 不证明这些。
4. **Android 对应层。** 本轮 Apple Swift 观察端口不覆盖 Android native lane；已有 held-READ/fresh-Probe 的 Android 证据保持原范围，不新增第二台手机或多 API tier 门槛。

因此仍没有一个就绪命令可以关闭这两个系统-loss/same-Hub gate。下一步应把上述机制接入既有 BLeng HIL 与 host，而非创建第二个 App、通用测试框架，或为缩短命令把它并入 full-suite。实际 radio/revoke 前仍需 fresh、具名授权；iOS 已接受的 rejected-disable continuity 限制不重复 RF 确认，原失败 verdict 保留。
