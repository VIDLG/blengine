# BleHub / BLeng 功能收敛：需求审计与测试先行修复

日期：2026-10-25

## 结论与范围

**尚未达到集中核心 review 交接条件。** 用户明确要求先完善既定功能及对应测试，再人工 review；外围 smoke 完成、接口存在或旧测试全绿都不足以交接。§1–6 保留各轮候选边界；已补齐 11 类 typed handle、Rust 可命名性、共享 operation 架构、代表性异常回归、macOS foreign runtime 与现有 Android typed ART。§7 修复 Team 匹配误判后，新 iPhone typed/lifecycle artifact runtime 也已通过，不能继续描述为未实现或仍被签名准备阻断。F15 平台故障 / same-Hub 的必要真实系统证据仍未关闭。用户于 2026-10-26 已明确接受已记录 iOS/CoreBluetooth 候选的 CCC disable REJECT continuity 平台限制，已回写 Plan §1.4；该项不再单独阻塞集中核心 review，原 gate 仍 failed/pending，不等于已验证恢复。后续 F15 实现/确定性测试/真实系统 gap 的重新分类及 Apple 修复见 [平台 loss 审计](2026-10-26-platform-loss-functional-convergence.md)；F12 完成结果在 loss/close 下的保持与 Android 生产 admission/退役 owner 接线见 [后续功能收敛](2026-10-26-connection-cancel-functional-convergence.md)。这些不由本报告的 artifact runtime 外推。本报告对应 [Plan §1.4](../../../.plan/PLAN.md#14-功能收敛验收清单2026-10-25-用户授权完善) 的候选审计，不建立第二个架构计划。

当前取消功能的逐 operation 策略、Rust/foreign/native 分层覆盖、精确错误分类修复及有限清理统一见 [取消覆盖审计](2026-10-26-bleng-cancellation-coverage.md)。用户随后明确批准的同步 begin / 异步 wait/cancel 模型、predispatch 取消、无等待 eager submission、deadline 及新候选验证见 [同步 operation 提交](2026-10-26-bleng-synchronous-operations.md)；它替代旧 async begin 签名，不沿用旧候选 runtime/RF 结果。

当前 Rust 已有主要 operation 实现，不是只有 BLeng 骨架。本轮从需求与源码查出具体缺陷，再以红灯复现和最小实现修复；已有测试不追溯宣称为历史 TDD。工作树原有改动保留，没有 commit、push 或 branch。本轮有意修改 BLeng 的取消/结果生命周期及测试参考层，不再沿用早先“核心 67 文件未变”的外围阶段结论。

§1–5 的软件轮次没有运行设备/RF或安装 App；§6 明确记录后续 Android 安装/ART 与 iPhone 只读预检。没有修改系统签名设置、Keychain、TCC、radio 或固件。Android 仍只要求现有一台手机；长期 stress 后置。

## 1. 需求 → 实现 → 测试映射

下表列出实际检查的代表性实现和断言，不表示每行所有功能、平台、竞态已全部关闭。测试名称来自当前源码；本轮 workspace 回归包括这些 Rust 测试。私有 owner 的切片测试不是公开 facade、foreign runtime 或真机证明。

| Plan ID | 实现与测试入口 | 本轮判断 |
|---|---|---|
| F01 | [DTO 校验](../../../crates/blehub/src/contract/commands.rs)、[Hub facade 测试](../../../crates/blehub/src/tests/hub_tests.rs)：`facade_rejects_invalid_scan_and_write_without_dispatch_or_events_at_utf8_boundary` | 新增非法 UUID/空名/249-byte UTF-8/513-byte write 的零 dispatch、零 event；248-byte 名称/512-byte write 作有效边界对照。未由此宣称所有 nested event validation 分支覆盖。 |
| F02 | [Hub lifecycle 测试](../../../crates/blehub/src/tests/hub_tests.rs)：`dispose_waits_for_backend_command_exit_without_closing_its_ingress_early`；既有 assembly/ingress 测试 | 新增真实 command gate 与 dispose 的有界 channel 握手，验证 command 内仍可交付、退出后才 cleanup、cleanup once 与 late ingress Closed；不靠 sleep 猜顺序。 |
| F03 | [初始化测试](../../../crates/bleng/src/runtime/tests/initialization.rs)：`initialize_requires_correlated_snapshot_and_terminal_in_either_order`、`powered_off_and_denied_snapshot_still_completes_initialization` | 已有精确双证据与初始化前置条件；Off/Denied 初始化成功不证明操作授权。 |
| F04 | [扫描测试](../../../crates/bleng/src/runtime/tests/scan.rs)：`stop_scan_requires_matching_domain_and_terminal_then_allows_new_scan`、`timeout_keeps_scan_slot_until_matching_native_stop` | 已有 singleton/start/stop/迟到退役测试；过滤 wire 校验与真实平台过滤分属不同层，不能合成所有组合的通过。 |
| F05 | [连接测试](../../../crates/bleng/src/runtime/tests/connection.rs)、[单 Hub 双设备测试](../../../crates/bleng/src/runtime/tests/multi_device.rs)：`one_hub_two_peripherals_keep_gatt_operations_and_notification_routes_independent` | 已有多设备逻辑隔离；本轮修复 testkit 将 Connecting/错误最终状态误算成功、合法进度误判重复的问题。双设备模拟输入不等于双 DK RF。 |
| F06 | [GATT 测试](../../../crates/bleng/src/runtime/tests/gatt.rs)：`rejected_or_failed_rediscovery_preserves_committed_database_for_reads`；[reference projection](../../../tests/support/blehub-testkit/src/projection.rs) | 已有 committed/pending discovery 分离；本轮补旧 discovery 不得覆盖新 session 和 subscription 的回归。 |
| F07–F08 | [read/write 测试](../../../crates/bleng/src/runtime/tests/characteristic_io.rs)：`read_requires_committed_gatt_identity_and_preserves_binary_value`、`write_both_modes_require_exact_domain_and_terminal_and_keep_payload` | 已有 binary value、两种 write mode、domain/terminal 与迟到 reservation 断言；without-response 只证明本地提交，不证明远端交付。 |
| F09 | [订阅测试](../../../crates/bleng/src/runtime/tests/notifications.rs)：`notification_enable_requires_state_and_terminal_and_value_does_not_complete_it`、`notification_disable_failure_releases_transition_for_retry`、`notification_require_indication_mismatch_faults_instead_of_succeeding` | Rust 路由/严格模式已有测试；iOS rejected-disable continuity 保留实测失败，用户于 2026-10-26 已接受该候选级平台限制，不再单独阻塞集中核心 review。 |
| F10 | [RSSI 测试](../../../crates/bleng/src/runtime/tests/rssi.rs)：`read_rssi_requires_exact_domain_and_terminal_in_any_order`、`read_rssi_timeout_does_not_block_a_sibling_rssi_operation` | 已有精确关联与 sibling 隔离，不外推平台未验证的中断行为。 |
| F11 | [BLeng reconciliation](../../../crates/bleng/src/runtime/tests/reconciliation.rs)、[conformance oracle](../../../tests/support/blehub-testkit/src/conformance.rs) | 本轮收紧连接最终状态的 oracle；进度仍计入 event attempt，同步拒绝零 event 与 terminal 顺序不放宽。 |
| F12 | [actor 回归](../../../crates/bleng/src/runtime/tests/actor.rs)、[公开 read handle 测试](../../../crates/bleng/src/tests/facade_tests.rs)、[result owner](../../../crates/bleng/src/runtime/owner.rs) | 取消回执竞态、完成未领取结果、wait/cancel 可见性和 fault 优先级已补红绿；其他 operation typed handle 仍是实现缺项。 |
| F13 | [lifecycle](../../../crates/bleng/src/runtime/tests/lifecycle.rs)、[transport](../../../crates/bleng/src/runtime/tests/transport.rs)、[reservations](../../../crates/bleng/src/runtime/reservations/tests/connections.rs) | 已有有界容量、overflow、worker stop、cleanup；本轮修复 testkit panic 行为持锁导致自身中毒。foreign dispatch panic 的特定 catch/unwind 路径仍需补明确回归。 |
| F14 | [公开 API](../../../crates/bleng/src/facade/mod.rs)、[Rust 导出](../../../crates/bleng/src/lib.rs)、[snapshot owner](../../../tools/api_snapshots.py)、Apple/Android artifact consumers | 本轮修复既有 ReadOperation 在 snapshot allowlist/提取器中的漏接线，生成三语言快照；Rust 可命名性与新增 handle surface、各 operation foreign runtime 仍未闭环。 |
| F15 | [RF/故障差距](../../research/react-native/react-native-rf-lab-test-architecture.md#当前覆盖与缺口优先级) | iOS active-loss/活动撤权与 same-Hub 恢复、write/CCCD 系统 loss、永久 capacity stall 等既有缺口保持 open；不拿本轮 Rust 测试或 ATT REJECT 替代。 |

## 2. 已复现并修复的缺陷

### 2.1 取消回执不能随原操作 terminal 提前丢失

原顺序：read accepted → 请求 cancel → 原操作 terminal 在 native control 返回前到达 → reservation 提前退休 → watch sender 丢失。调用者可能得到 `TransportFault`，原 waiter 又被取消或一直等到超时。

修复位于 [dispatch reservation](../../../crates/bleng/src/runtime/reservations/dispatch.rs)、[connection root](../../../crates/bleng/src/runtime/reservations/connection.rs) 和 [actor](../../../crates/bleng/src/runtime/actor.rs)：未决取消回执阻止自身及所属 root 提前退休；回执返回后分别结算控制和原 waiter，再推进 retirement。拒绝取消时保留已到达的原成功结果，不改写 native terminal、不增加普通 command ID。

关键回归：

- `cancellation_receipt_survives_cancelled_terminal_before_control_returns`
- `cancellation_rejection_preserves_read_completed_before_control_returns`
- `terminal_retirement_preserves_pending_cancellation_receipts_across_connection_operations`
- `failed_connect_retirement_preserves_pending_cancellation_receipt`

红灯 `.work/validation/2026-10-25-bleng-cancel-race-red.log`：6 项本问题回归失败，另含并行结果槽回归的 2 项失败，不能将混合计数全归为取消问题。编译失败日志另存，不冒充行为红灯。绿灯为 `2026-10-25-bleng-cancel-race-green.log`；最终整合计数见下表。

### 2.2 完成结果、领取权和 runtime 终态分开

原实现将正常 shutdown 的 lifecycle 检查放在领取既有完成值之前；未调用 wait 的已完成操作取消也误报 `OperationNotFound`。复核又复现 wait 已取 ticket 时的可见性窗口，以及 retained-result 分支错误绕过 `Faulted`。

[owner](../../../crates/bleng/src/runtime/owner.rs) 让 typed handle 与 waiter 观察同一 oneshot 的 readiness/领取状态，不新增结果通道或无限 tombstone。正常 shutdown 保留此前完成结果；pending/部分证据 shutdown 仍 `Closed`，transport fault 仍 `TransportFault`。短锁只用于领取/观察，不跨 await；ticket drop 不保留接收器/waker，也不隐式 cancel native work。

[API 回归](../../../crates/bleng/src/tests/facade_tests.rs) 包括：

- `read_result_slot_preserves_completed_success_and_failure_after_shutdown`
- `read_result_slot_cancel_completed_unclaimed_success_and_failure_keeps_result`
- `read_result_slot_cancel_observes_completion_while_wait_owns_ticket`
- `read_result_slot_fault_overrides_completed_success_and_failure`
- `read_result_slot_fault_overrides_ready_result_before_waiter_resumes`
- `read_result_slot_shutdown_pending_or_partial_evidence_is_closed`

使用测试专用 actor 屏障、暂停点和手动 poll，不以 sleep 保证时序。原红灯与复核红灯分别为 `2026-10-25-bleng-result-slot-red.log` 和 `2026-10-25-bleng-result-slot-followup-red.log`，对应 green 日志同名替换后缀。

### 2.3 参考判定器不能提供假绿

仅修改 [blehub-testkit](../../../tests/support/blehub-testkit/src/)：

- Connect 成功必须有 Connected；Disconnect 成功必须有 Disconnected。合法 Connecting/Disconnecting 不算最终 domain，但仍受同步拒绝零 event、terminal 先后和 failure error 一致性约束。
- 显式新 GATT session 使先前 pending discovery 过期；旧回调不撤销新 session/read/subscription。后来合法登记的新 discovery 仍可替换。
- `RecordingSink` 在执行 Panic 行为前释放 behavior mutex；恢复 Accept 后可正常测试后续 terminal，不重试已消费的 panic attempt。

新增 `connection_progress_before_success_preserves_one_final_domain`、`connect_success_requires_connected_final_state`、`disconnect_success_requires_disconnected_final_state`、`old_discovery_cannot_replace_an_explicit_new_gatt_session`、`deferred_sink_panic_can_recover_to_accept_without_retrying_the_attempt` 等。红灯 `2026-10-25-testkit-functional-audit-red.log`：58 passed / 6 failed；修复后 conformance 64 passed。

### 2.4 已存在 read handle 的快照接线

维护的 `binding bleng-snapshot-check` 首次实际失败：`unexpected=['ReadOperation']`。不是新加了一个未经批准的 handle，而是当前快照 allowlist/生成声明提取没有跟上已有源码。

[工具回归](../../../tools/test_api_snapshots.py) 改为检查真实 Rust 和代表性 Swift/Kotlin 声明，不再用同一 allowlist 模拟所有提取结果。精确计入 `ReadOperation` 及对应 interface/protocol；没有放开未知 component、canonical DTO 重定义或额外文件。

三份快照仅通过 `pixi run just binding bleng-snapshot-update` 生成。diff 只补记当前已批准的 read handle/cancel 方法及 cancellation errors，不引入其他 handle 或新签名。红绿日志：`.work/validation/functional-convergence-bleng-snapshot-{red,green}.log`；首次真实失败另存 `2026-10-25-functional-convergence-bleng-snapshot.log`。

## 3. 首轮未关闭清单与后续顺序

本节保留首轮结束时的分类；后续关闭项及仍 open 的范围以 §5 为准，不将历史候选的未实现结论当作当前源码状态。

1. **实现缺项：其他 typed operation handle。** 当前只有 `ReadOperation` / `begin_read_characteristic`。§16.1.9 已批准按 operation 类型提供 control/result 分离，并保留 convenience 包装；connect/GATT/write/notification transition/RSSI 等只有 run-to-completion 方法不能算补齐。具体公开签名需显式评审，不能借此次 bug 修复暗中决定，也不能把行为要求降为可选。
2. **Rust surface 缺项：公开类型可命名。** 当前 `lib.rs` 只 re-export `Bleng`/`BlengError`，现有 `ReadOperation` 和细分 error reason 类型位于私有 module。Rust 外部调用者可用类型推导，不等于能命名存储这些类型；应以外部 consumer 回归明确导出清单，与后续 handle surface 一并处理。
3. **测试缺项：必要异常路径。** foreign dispatch panic 的独立退役分支、完整 Hub Drop fallback 与并发 push 的串行交付、nested event UUID/跨层实例 ID 校验仍需按风险补代表性断言。不能为了测这些而给薄核心增加 BLE 状态机或公开私有实现。
4. **foreign/runtime 未验证。** 当前 [macOS BLeng consumer](../../../tests/consumers/apple/bleng-macos/Sources/BlengArtifactConsumer/main.swift) 和 [Android BLeng consumer](../../../tests/consumers/android-bleng/app/src/androidTest/kotlin/dev/blehub/bleng/PublishedBlengTest.kt) 主要覆盖 assembly/initialize/scan/event/shutdown/caller cancellation，不覆盖所有 GATT operation 与 typed read handle 的外语生命周期。build/snapshot 通过不能关闭它们。公开 RN surface 仍按 Plan 等 BLeng/API review 后建立，internal low-level RN 不是替代证据。
5. **平台未验证/已测失败。** F15 与 iOS continuity 按具名报告保留；对功能承诺的影响需解决或取得明确限制裁决。不得继续以“可以 review”为由自动忽略，也不为重复确认已知失败重跑 RF。

未决 attach/rejected backend 的 cleanup ownership、同 Hub sink 析构递归 dispose 等，没有足够已批准契约支持新增行为；先把要求讲清，不靠测试猜一个答案。以上不是追加 MTU、bonding、后台恢复、自动重连或新设备的授权。

## 4. 本轮实际验证

| 命令 | 结果与边界 |
|---|---|
| `pixi run just test blehub` | 35 passed；新增两个必要 facade/lifecycle 边界测试，首次即绿，是补测现有实现，不声称先有缺陷红灯。 |
| `pixi run just test bleng` | 最终局部回归 182 passed；覆盖本轮取消和结果槽红绿。 |
| `pixi run just test conformance` | 64 passed；参考判定器、projection 和 sink 故障恢复。 |
| `pixi run just test all` | 281 passed / 16 suites；`.work/validation/2026-10-25-functional-convergence-workspace.log`。 |
| `pixi run just binding bleng-snapshot-check` | 修复接线后 4 tool tests / 3 snapshots verified；首次失败保留。 |
| `pixi run just apple consumer-bleng-macos` | 五个 Rust target、三 slice XCFramework、component symbols 与 artifact-only macOS lifecycle runtime passed；`2026-10-25-functional-convergence-apple-consumer.log`。不是 read-handle foreign runtime 或 iPhone RF。 |
| `pixi run just ffi android-bleng-build` | 双 ABI native image、AAR 发布、ELF single-image 与 artifact-only APK/test APK static build passed；`2026-10-25-functional-convergence-android-build.log`。未运行 ART instrumentation。 |
| `pixi run just fmt` / `pixi run just lint` / `pixi run just docs-check-mermaid` | 通过；日志 `2026-10-25-functional-convergence-{fmt,lint,mermaid}.log`。相关 authored Markdown 的 UTF-8、围栏、空白、冲突、本地链接与 `git --no-pager diff --check` 通过。 |

所有日志路径均相对忽略目录 `.work/validation/`。本轮新核心行为改变后，之前的真机/包/RF 通过仍属于各自原候选；不转移为当前候选全矩阵通过。长稳、ABI freeze 和最终发布闭环仍后置，但本节剩余功能/测试缺项不能借后置规则关闭。

## 5. Typed operation 完整 surface 与架构收口

用户进一步授权检查并调整 BLeng 架构。此次补齐既定操作，不新增 BLE 产品能力或第二套 engine；新增 public objects/methods 改变当前未冻结 BLeng ABI，所有生成绑定和包须锁步消费，不承诺旧二进制兼容。具体 surface 与生命周期裁决已回写 [Plan §16.1.9](../../../.plan/PLAN.md)，持久职责见 [actor/reservation 设计](../../research/bleng/bleng-actor-reservations-design-2026-10-07.zh-CN.md)。

### 5.1 已关闭的实现缺项

- 全部 11 类 concrete typed handle 和 `begin_*` 已实现：initialize、scan start/stop、connect/disconnect、GATT discovery、read/write、enable/disable notifications、RSSI。每个提供 single-consumer wait 与独立 cancel；enable 只返回 SubscriptionId，actual mode 保留在 canonical event。
- [公开 Rust 导出](../../../crates/bleng/src/lib.rs) 包含全部 handle 和 error reason/resource；[外部 Rust consumer](../../../crates/bleng/tests/typed_operations.rs) 实际命名并执行 11 类操作，不靠 crate 内类型推导假装外部可用。
- [operation.rs](../../../crates/bleng/src/runtime/operation.rs) 从 owner 提取私有泛型 ticket/slot/control/结果解码；[owner.rs](../../../crates/bleng/src/runtime/owner.rs) 保留执行器、handoff、deadline、FIFO 出口和单一 shutdown coordinator。actor/reservations 仍唯一拥有 BLE 证据与 native retirement。没有为 11 种操作创建 11 套状态机/task/channel。
- typed begin 仅限制 handoff；后续 wait 有独立预算。convenience 方法共用同一机制，begin 和 wait 传递一个 absolute deadline，不重复计时。初始化与 disconnect 保持既有本地 NotCancellable 策略，不将 handle 存在误当 native cancellation 支持。
- 最后一个 Bleng facade drop 非阻塞请求既有 close/cleanup；handle 只保活执行器和已有结果，不保活逻辑 Hub。pending wait/cancel Closed、已完成未领取结果保留。仅 drop handle 不关闭 Bleng、不 native cancel；显式 shutdown 仍是 cleanup 完成/错误屏障。

### 5.2 架构复核发现并红绿修复的边界

1. **caller receiver 退休后目标完成不可见。** timeout/drop 移除 receiver 后，旧实现没有机会再更新 handle 完成标记，迟到成功/失败完成再 cancel 会误报 NotFound。现在由既有 CommandProgress 的 actor evidence/退役路径确认共享单调 completion observer，不依赖 receiver、不增加 tombstone registry。
2. **取消请求尚未获接受就丢 public domain。** 旧实现按 requested 提前抑制，拒绝取消后 waiter 成功却缺公开 domain。新增有界 publication 延期，拒绝释放、接受仅抑制 target 非-terminal。
3. **per-command 延期倒置 teardown 次序。** 第一版延期在合法输入 Connected → terminal → passive Disconnected 下，拒绝取消后公开成 Disconnected → Connected → terminal。actor 红灯证实后，改由现有 connection/scan owner 的 ScopedPublication 保序，每 scope 聚合硬上限 64；多个 control 混合裁决仍保序，其他 scope 不阻塞。释放只 publish，不二次 record/settle；容量溢出/FIFO 满 fail closed。
4. **同步订阅 value 越过先收到的 enable evidence。** 首次真实 Swift fixture 同步 push state → terminal → value 后才返回，BLeng 因只 gate 带 command ID 的回调，公开成 value → state → terminal。actor 红灯复现后，subscription transition 以四类精确 ID 关联 value，复用其原有单个 64 项 predispatch buffer 保序。原生真正 value-first 仍保留，不制造平台 state-first 保证，value 不完成 enable waiter，拒绝前已有 callback 继续 fail closed。

红灯/绿灯日志保留：

- `2026-10-25-typed-operations-red.log` / `2026-10-25-typed-operations-green.log`：新 API 先以编译红灯证明缺席；首次可执行测试中的错误假设另有标记，不冒充生产行为 bug。
- `2026-10-25-typed-operations-boundaries-{red,green}.log`：receiver 退休、publication 缺失及 facade-drop 三项边界。
- `2026-10-25-typed-publication-order-{red,green}.log`：同 scope teardown、混合 control、容量与其他 scope 进展。
- `2026-10-25-typed-notification-order-{red,green}.log`：真实 actor 订阅回调顺序、同步拒绝、身份隔离和原缓冲硬上限。

### 5.3 真实 artifact 门禁发现的问题

- snapshot 精确声明集合扩展到 11 个 handle 及各自 interface/protocol，独立测试逐项缺失/额外未知/重复 canonical。三语言快照通过维护 recipe 生成，没有手改或宽泛接受任意 Operation。
- 第一次 Android build 被旧 packaging allowlist 拦截，未进入新 consumer 编译；修复该 owner 的精确 method/object/class accounting，逐项检查 wait/cancel/clone/free/class，并继续拒绝额外组件、probe、第二或重复 native image。red/green 为 `2026-10-25-typed-android-packaging-{red,green}.log`；最初失败在 `2026-10-25-typed-operations-android-build.log`。
- 第一次 Swift consumer 已编译链接，但因上述真实订阅重排失败。异步 main 未捕获 throw 进入 Swift fatal-crash 处理，外层 300 秒超时。保留 `2026-10-25-typed-operations-apple-consumer.log`，没有称为通过。consumer 现在最外层保留错误详情、stderr 标记并 exit(1)，内部断言/cleanup 不变，不靠改 fixture 或判据变绿。检查无该 consumer 残留；只清理身份匹配的本次 Android telemetry 子进程，记录 `2026-10-25-typed-operations-cleanup.log`。

### 5.4 本候选实际验证与剩余范围

| 实际命令 | 结果 / 日志 |
|---|---|
| `pixi run just test all` | **305 passed / 17 suites**，其中 BLeng 206（199 unit + 7 integration）；`2026-10-25-typed-operations-workspace-final.log`。不以数量替代需求映射。 |
| `pixi run just binding bleng-snapshot-check` | **9 tool tests / 3 snapshots verified**；`2026-10-25-typed-operations-snapshot-final.log`。 |
| `pixi run just ffi android-bleng-test-tools` | **8 tests passed**；`2026-10-25-typed-android-packaging-green.log`。 |
| `pixi run just apple consumer-bleng-macos` | 当前 XCFramework 五 target/三 slice、component symbols、真实 generated Swift 的 11 类 handle 全链路、binary/typed error、并发 wait/cancel 和完成结果跨 shutdown **passed**；`2026-10-25-typed-operations-apple-consumer-fixed.log`。foreign backend 是严格有限 fixture，不是 CoreBluetooth RF。 |
| `pixi run just ffi android-bleng-build` | 当前双 ABI AAR、ELF/single-image、全部 handle classes 和扩展 Kotlin consumer/test APK **static-build-passed**；`2026-10-25-typed-operations-android-build-fixed.log`。新增测试含 11 类 handle、single-consumer、pending/完成 shutdown、独立 cancellation receipt；**未运行 ART**。 |
| `pixi run just apple consumer-bleng-ios-unsigned` | 当前 installed-package consumer 的 generic device + Simulator **unsigned build passed**；`2026-10-25-typed-operations-ios-unsigned.log`。不是 iPhone runtime，也不把 macOS typed fixture 计为 iOS 通过。 |
| `pixi run python -m unittest tools.test_api_snapshots tools.test_package_bleng_android -q` | **17 tests / OK**；`2026-10-25-typed-operations-tools-final.log`。 |
| `pixi run just fmt` / `pixi run just lint` / `pixi run just docs-check-mermaid` | 通过；27 diagrams / 172 Markdown files。最终相关 Markdown UTF-8/围栏/空白/冲突/260 个本地链接目标与 `git --no-pager diff --check` 通过，日志 `2026-10-25-typed-operations-{fmt,lint,mermaid,markdown,diff}-final.log`。 |

**截至 §5 结束仍 open：**新候选 Android ART 与 iPhone 适用的 handle/lifecycle runtime、必要异常路径的代表性补测、F15 平台故障及已测 iOS continuity 失败。旧报告中真机通过不转移到新候选；§5 没有运行手机、RF、权限/radio 或固件操作。public RN surface 仍等核心/API review 后建立，未为本轮另建 App 或长稳框架。后续关闭项见 §6，集中人工 review 交接条件没有因此自动满足。

## 6. 异常路径补测与现有 Android ART

### 6.1 已有实现的必要补测

- [BLeng transport tests](../../../crates/bleng/src/runtime/tests/transport.rs) 实际阻塞 foreign dispatch 后注入 Rust panic，覆盖真正的 catch/unwind 分支而非 actor.kill：scan/RSSI/connect pending 与已阻塞 event reader 有界返回 TransportFault，已排队成功 callback 不转成成功，worker 释放后 cleanup once、late ingress Closed。合法 backend 不应 panic，此测试不是任意 FFI panic 可恢复承诺。已有 absolute-deadline 测试已能区分重新分配 wait 预算，未重复扩矩阵。
- [Hub tests](../../../crates/blehub/src/tests/hub_tests.rs) 补完整 Hub Drop fallback，在 dispose 成功/失败两种情况下检查 backend cleanup once、sink/ingress/root 释放。
- [Ingress tests](../../../crates/blehub/src/tests/ingress_tests.rs) 补 5 线程的有界 channel/callback gate 串行交付检查，以及 18 个 nested UUID/跨 service characteristic/descriptor ID 拒绝案例；非法输入零交付，随后合法事件仍可交付。

本轮仅补测试，没有改生产 Rust 实现，首次有效用例即绿，不包装成修复生产 bug 的 TDD。BLeng 测试初版错误等待“native dispatch 仍阻塞时 cleanup 完成”导致 timeout，已按既有 command-gate 契约修正，失败记录保留。

实际日志：`2026-10-25-bleng-fault-boundaries.log`、`2026-10-25-blehub-ingress-boundaries.log`。整合 workspace 为 **309 passed / 17 suites**（BleHub 38、BLeng 207、testkit 64），日志 `2026-10-25-functional-runtime-workspace.log`。

### 6.2 Android：真实 ART 先失败，修正测试预期后通过

显式使用唯一现有设备 `3ac5cc09`，本次读回 model `PHB110`、Android `16` / API `36`。执行的是 published BLeng AAR 的 native artifact consumer instrumentation，不是 RN、production Android BLE 或 DK RF；没有改变权限/radio。

使用已有 named runner `tools.android.run_instrumentation`，显式 physical/serial、App/test APK、component、class 和 120 秒运行边界。root-controlled `adb push + pm install` 安装通过；原始 SDK 命令与输出保存在各 instrumentation log，没有将 synthetic identity 送到真实设备。

首次 `connectWaitAndCancelHaveIndependentAcceptedAndRejectedReceipts` 因测试无条件等待原 Connected 成功事件而超时：取消接受后该目标成功事件按已批准契约必须抑制。测试修正为拒绝分支要求精确 Connected + terminal；接受分支逐事件 drain 到原 terminal，并显式拒绝目标成功事件泄漏。未增加 timeout、合成 event 或修改 Rust cancellation 语义，仍验证独立回执、wait Cancelled/原结果及一次领取；阶段诊断明确标注期待/禁止的事件。

首次 failed log `2026-10-25-bleng-typed-art.log` 与 host log 保留；首次 App/test APK/AAR digests 存入 `2026-10-25-bleng-typed-art-failed-inputs.json`，不是事后回写为通过。

重建静态包后实际执行：

| 实际运行 | 结果 / 原始日志 |
|---|---|
| 精确 class `dev.blehub.bleng.PublishedBlengTest#connectWaitAndCancelHaveIndependentAcceptedAndRejectedReceipts` | **OK (1 test)**，instrumentation code -1；`2026-10-25-bleng-typed-art-cancel-fixed.log` |
| 完整 class `dev.blehub.bleng.PublishedBlengTest` | **OK (9 tests)**，instrumentation code -1；`2026-10-25-bleng-typed-art-fixed.log` |

9 项包含全部 11 类 generated handle、typed outputs/bytes/actual mode、并发 single-consumer、完成未领取结果/pending shutdown、caller coroutine cancellation 和独立接受/拒绝回执。门禁没有把测试个数当 operation 数；一项全链路测试可以覆盖多个操作。Runner 在结束时只 force-stop 本次 instrumentation package，不停止成功 RN App 或未知 iOS App。

当前 App/test APK/AAR 的实际 SHA-256 和型号/API 读回存于 `2026-10-25-bleng-typed-art-fixed-inputs.json`；重建日志 `2026-10-25-bleng-typed-art-rebuild.log`。这些 hash 是读取实际文件的证据，不是代码里的 candidate 常量；本次通过不外推其他 Android/OEM 或新二进制。

### 6.3 iPhone：接线与 unsigned 已过，签名预检阻断

先执行只读 `pixi run just apple ios-device-doctor build-signing '' 00008110-000C61160C39401E`：device available/unlocked、Xcode/SDK/Swift/XcodeGen、configured Team verified，但 `signing-identity=blocked: no visible Apple Development identity for configured Team`。日志 `2026-10-25-bleng-ios-readiness.log`。没有推断一定是 Keychain locked，没有尝试签名/安装、解锁或收集密码，也没有把设备 ready 升级为 TCC/UI/RF ready。

[现有 iOS App](../../../tests/consumers/apple/bleng-ios/Sources/AppDelegate.swift) 现在先运行 macOS 同一份平台中立 [TypedOperations.swift](../../../tests/consumers/apple/bleng-macos/Sources/BlengArtifactConsumer/TypedOperations.swift)，再保留原 lifecycle checks；[XcodeGen 输入](../../../tests/consumers/apple/bleng-ios/project.yml) 只共享 authored fixture 源，不增加 checkout-relative native header/library。receipt 必须同时含唯一当前 run ID、`typed_operations=passed`、最终 pass；旧 initialize/scan-only receipt 不能冒充新覆盖。

[Host runner](../../../tools/apple/consumer_bleng_ios.py) 在签名前以已有 owner 做 10 秒预算的 ready/unlocked 检查，安装/launch 前以正式 device process receipt 拒绝旧 consumer，移除 terminate-existing 和 launch 自动重试；没有引入第二套权限或 UI runner。纯 host red/green 覆盖 marker、identity、Locked 和单次失败；日志 `2026-10-25-bleng-ios-typed-host-{red,green}.log`。单模块最终 12 tests passed，相关 owner 回归共 43 tests passed。

`pixi run just apple consumer-bleng-ios-unsigned` 的 generic device + Simulator 构建已通过，包含共享 11 类 handle 测试；日志 `2026-10-25-bleng-ios-typed-unsigned.log`。macOS artifact consumer 同源复跑通过，日志 `2026-10-25-functional-runtime-macos.log`。**新 iPhone typed runtime 仍未运行**；需要操作者准备可见匹配 identity/私钥访问后，以新的预检和 App receipt 验证，不能借 macOS、unsigned 或旧 iPhone 报告关闭。

### 6.4 最终软件验证与剩余项

本轮最终 `pixi run just test all` 为 309 passed，相关 iOS host/device SDK 隔离回归为 43 tests / OK（`tools.apple.test_consumer_bleng_ios`、`tools.apple.test_bleng_ios_backend`、`tools.tests.react_native.test_ios_device`、`tools.tests.react_native.test_ios_sdk`）。`pixi run just fmt`、`pixi run just lint`、`pixi run just docs-check-mermaid`、相关 Markdown 的 UTF-8/围栏/空白/冲突/65 个本地链接目标与 `git --no-pager diff --check` 均通过；日志 `2026-10-25-functional-runtime-{workspace-final,fmt,lint,mermaid,markdown,diff}.log` 及 `2026-10-25-bleng-ios-typed-related-host-final.log`。

11 类 core operation 实现、公开 Rust 可命名性、此次列明的代表性异常补测和 Android typed ART 缺口已关闭。没有证据支持再大改 BLeng actor/operation 架构；但这不是零缺陷或所有状态组合全覆盖的保证。

截至 §6 结束仍需新 iPhone typed/lifecycle runtime；该项随后由 §7 关闭。F15 的适用平台中断/恢复证明与已知 iOS continuity 限制仍需解决或明确裁决，并按当前候选重新核对功能映射和必要 package/runtime 边界。公开 RN surface、最终 ABI freeze/发布闭环与长稳继续按 Plan 原顺序，不提前宣称集中核心 review 已 ready。没有要求第二台 Android、没有运行 RF、刷机或权限/radio mutation，也未 commit/push/branch。

## 7. Team 证书匹配误判修复与 iPhone runtime 通过

### 7.1 根因不是密码或错误 Team 配置

用户显式人工完成 Keychain 准备后，工具仍报 `no visible Apple Development identity for configured Team`。本机只读核对发现，同一个有效 identity 的公开证书 fingerprint 完全一致，Common Name 末尾的个人标识与 Team OU 不同；真正的 subject `2.5.4.11` 与配置 Team 匹配。旧 `_identities` 把 CN 括号后缀当 Team，造成 false block；§6 当时只记录了工具阻断，不能据此断言缺少真实匹配证书。

新增轻量 [signing identity owner](../../../tools/apple/signing_identity.py)，供 doctor 和旧 runtime signing helper 两个真实调用者共用：从 `security find-identity -v -p codesigning` 的当前有效 fingerprint 关联只读公开 PEM 的 DER SHA-1，再通过 OpenSSL 的数字 OID subject 读取唯一、格式合法的 Team OU。SHA-1 仅作 OS identity 指纹关联，不作新安全协议；不按名称猜测、不只凭公开证书存在放行、不导出私钥、不修改 Team 或 Keychain。prebuilt mode 不因此增加签名环境/证书查询。

红灯覆盖真实问题形状（CN 后缀与 OU 不同仍应匹配），也覆盖伪 CN 匹配但 OU 属于外 Team、没有有效 identity 关联、畸形/多个 OU、命令失败和超时。`test_ios_preflight` 17 项与相关 57 项回归通过，红绿日志 `2026-10-25-signing-team-{red,green}.log`。agent 的宽泛 `tools-test` 在 180 秒限时内未形成完整 verdict，不计作通过；未盲目重试同一限时。公开证书只进入临时文件并清理，测试 mock 实际命令边界。

修复后的真实本机 doctor 与显式设备 doctor 均 verified，`team_match_source=certificate-subject-ou`、matching identities=1；日志 `2026-10-25-signing-team-doctor.log` 和 `2026-10-25-signing-team-device-doctor.log`。doctor 的 private-key/TCC/UI/RF 仍分别 not-checked，没有把只读观察升级为通用授权。

### 7.2 当前 iPhone typed artifact runtime：通过

实际执行：

```text
BLEHUB_APPLE_DEVICE=00008110-000C61160C39401E pixi run just apple consumer-bleng-ios-device
```

本轮当前 package 五 target/三 slice 与 component/single-image 核验、development signing、安装及单次 launch 均通过；未使用 terminate-existing、未接管未知旧 App、未启动 XCUITest/CoreBluetooth 或 DK RF，没有修改 TCC/radio/固件，也没有 agent 解锁或密码输入。

当前 run ID：`7326c24b-4bd4-4c7d-86bd-fa28fc7b47c5`。设备原始 receipt：

```text
apple_bleng_ios_run_id=7326c24b-4bd4-4c7d-86bd-fa28fc7b47c5
typed_operations=passed
apple_bleng_ios_artifact_consumer=passed
```

这证明该 signed App 在本次 iPhone session 实际执行共享的 11 类 typed handle foreign fixture、结果只消费一次、actual-mode 原事件、并发 wait/cancel、已完成 read 结果跨 disconnect/shutdown 保留，以及原有初始化/event-reader/shutdown lifecycle。它不是 production CoreBluetooth、RF 或长期压力证明。

完整输出 `2026-10-25-bleng-typed-ios-device.log`；receipt 副本 `2026-10-25-bleng-typed-ios-result.txt`；实际 signed App SHA-256、设备和 run ID 保存在 `2026-10-25-bleng-typed-ios-inputs.json`。成功结果页按默认保留。实际签名成功证明这次 build 可使用私钥，不改写 doctor 的只读证明范围，也不承诺下一次运行无需新预检。

最终维护入口 `pixi run just react-native tools-fixture-test`：**1269 tests / OK，236.224 秒**，日志 `2026-10-25-signing-team-tools-fixture-final.log`。首次完整组合运行发现新测试直接依赖旧 `test_ios_runtime` runner 违反既有 layout 规则，失败日志 `2026-10-25-signing-team-tools-fixture.log` 保留；测试已改为直测共享签名机制，没有放宽规则或删除 OU/fingerprint 边界。fmt、lint、Mermaid、相关 Markdown 和 diff 检查通过，日志 `2026-10-25-signing-team-*`。未修改核心 Rust、权限或固件，也未 commit/push/branch。
