# 平台 loss 功能收敛：Apple watchdog、BLeng 模型与 Android 权限异常边界

日期：2026-10-26

后续已补 Apple capacity/watchdog 的生产调度接线，并核对现有 runner 能否覆盖 write/CCC 与 same-Hub；当前执行准备度与关闭条件见 [关闭条件与执行准备度](2026-10-26-platform-loss-closure-readiness.md)。用户于 2026-10-26 明确接受已记录 iOS/CoreBluetooth 候选的 continuity 限制，已回写 Plan；该项不再单独阻塞集中核心 review，原 gate 仍 failed/pending。此裁决没有新增 RF 证据，也不关闭其他系统 loss / same-Hub 缺口。

## 范围与结论

承接 [核心功能收敛报告](2026-10-25-core-functional-convergence.md)，本轮按 [Plan F15](../../../.plan/PLAN.md#14-功能收敛验收清单2026-10-25-用户授权完善) 重新核对实际实现、确定性测试与未验证平台行为，没有重复 typed artifact smoke 或启动 RF。

- 修复一处已用 red 证明的 Apple production 漏接线：adapter/authorization unavailable 退役连接时，未立即取消所属 watchdog。
- 补齐同一公开 Bleng instance 的 write/CCC loss → snapshot restore → 显式新 connection/GATT 的 canonical-evidence 模型。现有实现首次有效测试即通过，不包装为生产缺陷修复。
- Apple capacity wait、混合 write/CCC interruption 机制已经实现且有测试，不能继续笼统标为“功能未实现”。真实永久 stall、系统 loss、same-Hub RF 仍未验证。
- Android `SecurityException` 后补齐 accepted sibling 的确定性行为刻画：后续命令仍逐个提交、各自结算、释放 queue/watchdog，route 保持原 owner。首次测试即通过，未修改 Android production 行为；这不裁决真实撤权后的连接退役策略。

后续 [Android connection owner / BLeng 结果保持收敛](2026-10-26-connection-cancel-functional-convergence.md) 已补生产 admission/双索引/断连 retirement 的确定性接线，并修复完成结果与 loss/close 的竞争；该报告记载更新后的候选验证，不将下文历史计数覆盖成新结果。

本轮没有访问手机、切换系统 radio/权限、使用 Peripheral、provision 固件、启动长稳或 commit/push/branch。既有 iOS CCC disable continuity 失败保持原 verdict，未重复 RF 确认。

## 1. Apple：先 red，再最小修复

### 1.1 根因

[AppleBleBackend](../../../platforms/apple/Sources/BleHubApple/AppleBleBackend.swift) 的 `centralManagerDidUpdateState` 在 adapter/authorization unavailable 时 drain connections、retire GATT/I/O 并投影失败，但没有取消该 connection 的 watchdog。它可能残留至 expiry、被替换或 dispose；既有 token/generation 检查仍防止旧 expiry 领取新工作，因此没有将此夸大为永久泄漏或已证明误杀新连接。

普通 disconnect 路径已经有 watchdog cancellation，问题是 unavailable 分支缺少同一动作。

### 1.2 生产 seam 与修复

提取 [AppleUnavailableConnectionRetirement](../../../platforms/apple/Sources/BleHubApple/AppleUnavailableConnectionRetirement.swift)，由真实 delegate 使用本次已采样的 state/authorization 调用。它在既有 controllers/registry 上执行 unavailable 退役与事件投影；native unsubscribe/delegate detach/cancel 仍在 backend 闭包执行。seam 没有自有状态、timer、queue 或第二套 lifecycle，没有为测试开放整个 backend 私有字段。

每个被 drain 的 connection 在 I/O/discovery 退役前执行：

```swift
watchdogs.cancel(connectionId: connection.connectionId)?.cancel()
```

未改变 capacity deadline、watchdog timeout、queued/active cancellation、callback/token identity 或旧代 barrier。特别没有为恢复测试清除旧 callback barrier。

### 1.3 Red / green 与断言

[GattIoControllerTests](../../../platforms/apple/Tests/BleHubAppleTests/GattIoControllerTests.swift) 新增 `testUnavailableAdapterOrAuthorizationRetiresActiveCccAndQueuedWriteWatchdog`：活动 CCC enable + queued response-write，分别输入 powered-off/allowed 与 powered-on/denied。

- 修复前编译成功；1 个测试的两个输入出现 6 项预期失败，明确指向 watchdog 未取消及 registry 残留，exit 1。
- 修复后验证精确 adapterUnavailable/permission error、每命令一次 terminal、无伪造成功 domain、重复 loss 不重复结算。
- 所属 watchdog 立即取消；无关 token 保留；旧 CCC barrier 保留并消费 late callback；新 generation/fresh characteristic 的工作可正常完成，旧 token 不领取新 watchdog。

实际命令：

```sh
pixi run -e apple-ci swift test --package-path platforms/apple --filter GattIoControllerTests.testUnavailableAdapterOrAuthorizationRetiresActiveCccAndQueuedWriteWatchdog
pixi run -e apple-ci swift test --package-path platforms/apple --filter 'GattIoControllerTests|GattWatchdogRegistryTests|ConnectionControllerTests|AppleConnectionEventComposerTests|AppleEventMapperTests|GattControllerTests'
```

定向 1 passed；上述相关六组 **共 70 passed**，已含新增用例，不重复相加。red/green 日志在 `.work/validation/2026-10-26-apple-unavailable-watchdog-{red,green}.log`。

**证明边界：**测试调用 backend 真实使用的生产退役函数，不是测试手工先 cancel 再 retire；但没有构造真实 CBCentralManager/CBPeripheral delegate setup，native cleanup 闭包只记录调用。late expiry 检查覆盖 handler 使用的 registry gate，不等于 Dispatch timer 实际调度或完整 backend handler 的运行。新代恢复使用 fresh characteristic，不证明 CoreBluetooth 复用相同 native object 时也能恢复。

## 2. BLeng：同一 owner 的 write/CCC loss 模型

新增外部 consumer 测试 [write_ccc_loss_model.rs](../../../crates/bleng/tests/write_ccc_loss_model.rs)，入口 `initialized_once_write_ccc_loss_restore_requires_explicit_fresh_connection`。

| 代表性 head | accepted sibling | 输入/预期 |
|---|---|---|
| response write | CCC enable | canonical passive loss；两个 pending 均 Disconnected |
| no-response local-success，模型 native head 仍占用 | CCC enable | canonical adapter-unavailable outcome；旧 write 保持已完成，pending CCC 保留对应 Operation error |
| CCC enable | response write | canonical passive loss；两个 pending 均 Disconnected |
| CCC disable，先合法建 subscription | response write | canonical permission outcome；pending 分别保留输入的 Permission Operation error |

每场使用真实公开 Bleng facade/FakeBackend 与 owner-local LaneBackend/channel 控制模型 native submission 边界，不靠 sleep 推断时序。它不复制 production backend，也不向不存在的 adapter API 发送指令。

检查同一 Hub 初始化一次、最终 dispose 一次；snapshot 恢复不自动重连或重放旧写入；必须显式 connect，connection/GATT/subscription 使用新 ID。旧 domain/错误 terminal/旧 connection loss，以及新 command ID 搭配旧 connection/session，都不能提供新代操作结果。已有多连接隔离测试保留，未为目录对称重复新增。

定向 1 test / 4 scenarios 通过；BLeng 全量 **208 passed / 3 suites**，日志 `.work/validation/2026-10-26-bleng-write-ccc-loss-model.log`。测试首次绿，仅补证据，没有修改 `crates/bleng` production。

**不是平台 verdict：**这里的 Permission/AdapterUnavailable 是显式输入的 canonical 证据，不证明 Android/iOS 真正 loss 时一定发这些事件、以相同顺序到达或进程继续存活；no-response lane 由模型控制，不证明平台 native slot；same Bleng 模型也不等于真实 same-Hub RF。

## 3. Android：权限提交异常与已接受 sibling

[CharacteristicWriteLifecycleTest](../../../platforms/android/library/src/test/kotlin/dev/blehub/android/CharacteristicWriteLifecycleTest.kt) 新增 `permissionFailureAdvancesAcceptedSiblingWithoutRetiringConnectionOwner`，复用现有 fixture，直接调用 production `CharacteristicWriteLifecycle`，没有新增 runner、模拟平台框架或生产状态机。

代表性输入为两个已入队的 response write：首个 native submission 抛 `SecurityException`，后续 submission 分别再次拒绝或正常接受并回调成功。断言：

- 首命令只有一次精确 Permission terminal，没有 `CharacteristicWritten`。
- 后续已接受命令确实被提交；再次拒绝时独立 Permission terminal，成功时按 written → terminal 顺序结算，不被首个异常污染。
- 两个 watchdog 均被移除，pending queue 归零；后续无主 callback 和额外 drive 不产生重复事件或 submission。
- lifecycle fixture 中的 connection/GATT owner identity 不变，phase 仍为 CONNECTED。

定向 write + notification lifecycle 共 **22 passed / 2 suites**；完整 Android backend JVM 回归 **36 passed / 7 suites，0 failed / errors / skipped**，debug/release AAR 构建通过：

```sh
pixi run just ffi android-backend-check --tests dev.blehub.android.CharacteristicWriteLifecycleTest --tests dev.blehub.android.NotificationLifecycleTest
pixi run just ffi android-backend-check
```

日志：`.work/validation/2026-10-26-android-permission-sibling-{targeted,regression}.log`。完整构建仍有 SDK XML v4 / parser v3 warning，但命令成功；本轮没有替换工具链。

**证明边界：**这是当前 per-operation error 行为的 characterization，不是新策略的 red/green。fixture 供应 Handler 调度与 owner predicate，没有构造真实 BluetoothGatt、检查系统权限、访问 production connection 双索引或调用真实新 connect。不能把保留 owner 解释成已证明连接仍可用，也不能把异常推断成全局 authorization change。实际授权丢失后是否需要整条 route retirement 仍须基于明确语义与平台证据裁决；ATT authentication status 和 UID process death 继续分别归类。

## 4. F15 剩余项重新分类

| 项目 | 当前真实状态 | 未关闭部分 |
|---|---|---|
| Apple no-response capacity wait | 已有 controller/registry 机制；[后续生产调度 seam](2026-10-26-platform-loss-closure-readiness.md) 补齐 nextAction→schedule/cancel→expiry 的 4 项确定性接线测试，重复 drive 不续期、stale token 不领取后继工作 | GCD 实际投递、native cleanup/callback 效果及真实永久 capacity stall 仍未验证；不能把调度端口触发或正常流控恢复当作 RF pass |
| Apple write/CCC unavailable | 本轮修复所属 watchdog 漏取消；生产 seam 的 adapter-off/denied 退役通过 | 真实系统 transition、实际 native cleanup/callback 顺序、same-Hub RF；resetting 仍要求 dispose/reassemble，不与 powered-off 恢复混同 |
| Android write/CCC lifetime | 既有 lifecycle 保护 local completion/native drain、owner loss、旧 callback 和 disable continuity；[后续报告](2026-10-26-connection-cancel-functional-convergence.md) 已补真实生产 owner 的双索引回收、显式新 admission 与旧 route 隔离 | Context/Handler/BluetoothGatt callback gate 与新 connectGatt 的完整 backend 执行、真实系统 loss / same-Hub RF；不能把 admission 通过当作新连接已建立 |
| Android SecurityException | 当前 write/notification submission exception 结算 Permission；新增 write characterization 验证 accepted sibling 的拒绝/成功路径、queue/watchdog 释放与 owner 保留，未改 production | 需要明确与实际 authorization observation、route 不可信/恢复策略的关系；不能把所有 SecurityException 或 ATT authentication status 当成全局撤权，更不能把 root revoke 的 process death 解释成旧 Hub cleanup |
| BLeng same owner write/CCC recovery | 本轮 4 个 canonical-evidence 模型通过，真实 facade 不自动重连/重放，旧代不污染新操作 | 依赖 production backend 的实际证据，仍不是平台 loss/RF pass |
| iOS rejected-disable continuity | 已在 RN 与 production Native HIL 测出 continued value 未交付，既有 RN sniffer 是另一个 session；用户于 2026-10-26 明确接受该候选级平台限制 | 原 gate 保持 failed/pending，发布能力说明仍须披露；不改 firmware、不合成事件、不放宽判据、不重复已知实验；显式恢复路径尚未验证 |

因此本轮**关闭了一个具体实现 bug、一个 BLeng 确定性测试缺项和一个 Android accepted-sibling 行为刻画缺项，不关闭整个 F15，也不自动交接集中核心 review**。签名、typed runtime、原有 full-suite 的通过各保留候选/session 范围。

## 5. 整合验证

实际执行：

```sh
pixi run -e apple-ci python -m tools.apple.test_backend check
pixi run just test all
pixi run just fmt
pixi run just lint
pixi run just docs-check-mermaid
git --no-pager diff --check
```

- 完整 macOS backend tests passed。
- Apple production backend 的 generic iOS Simulator + device unsigned builds passed；没有签名/设备执行。
- Rust workspace **310 passed / 18 suites**（包括本轮新增 BLeng model integration suite）。
- 修复新增 Rust model 测试中的 `clone_on_copy` lint（`SetNotificationsCommand` 直接传值，保留含 Vec 的 write command clone）；最终 workspace 仍为上述 310 passed / 18 suites。
- fmt、lint、Mermaid 与 diff whitespace 检查通过；Mermaid 共检查 173 个 Markdown 文件中的 27 个图。
- 日志：`.work/validation/2026-10-26-apple-unavailable-backend-check.log`、`2026-10-26-platform-functional-{workspace-final,fmt,lint,mermaid,diff}.log`。

受影响的是 Apple internal backend source，不新增 public ABI；构建通过不是新 RF 或旧真机结果的重新背书。未重复无关 RN/Jest/工具全矩阵或长稳。
