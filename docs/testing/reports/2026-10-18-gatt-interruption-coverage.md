# Android / Apple GATT 操作中断覆盖与 Android 无响应写入修复

日期：2026-10-18。范围：两端生产 backend 源码/确定性测试审查、Android native 槽位修复、AAR/消费者构建和具名消费者初始化检查。**不是两端 in-flight write/CCCD 真机 RF pass，不冻结 ABI。`IOS-RADIO-GATE` 继续暂缓。**

**后续进展导航：** 用户批准继续后，生产写入 lifecycle、Android CCCD 本地补偿、Apple capacity-wait watchdog 和消费者版本/失败清理已有后续实现；两端新候选 full-suite 与消费者整组重新验证见 [生命周期加固报告](2026-10-18-gatt-lifecycle-hardening.md)。下文仍是首轮候选的事实与当时缺口，原消费者 failed 不回写为 pass。

## 结论与改动

共用检查矩阵，不强行统一平台 native completion 语义。本轮没有新增 RN App、runtime mode、host runner、fixture 协议、依赖或 production API。既有 dirty 工作保留；当前 contract version `6` 不是本轮引入的 bump。

| Owner | 实际改动 / 边界 |
|---|---|
| [Apple GattIoControllerTests](../../../platforms/apple/Tests/BleHubAppleTests/GattIoControllerTests.swift) | 新增一个 11-case 组合回归，覆盖 write/CCCD FIFO head 中断、顺序、分类、清理和 replacement route 隔离；只改测试，未改 Apple production |
| [AndroidBackendState](../../../platforms/android/library/src/main/kotlin/dev/blehub/android/AndroidBackendState.kt) | 区分 command-terminal claim 与 native owner；将原有 FIFO retirement event projection 提取为生产使用的 `retirePendingIo()`，不是第二套测试实现 |
| [AndroidBleBackend](../../../platforms/android/library/src/main/kotlin/dev/blehub/android/AndroidBleBackend.kt) | 接受无响应写入后只发布 local-success terminal；保留 active head/watchdog 至 callback 或 route retirement；drain/retirement 不重复报告已终结命令 |
| [PendingIoQueueTest](../../../platforms/android/library/src/test/kotlin/dev/blehub/android/PendingIoQueueTest.kt)、[ConnectionRfResourceCountsTest](../../../platforms/android/library/src/test/kotlin/dev/blehub/android/ConnectionRfResourceCountsTest.kt) | 扩展 owner identity reuse、terminal/native ownership 分离、FIFO retirement 分类/去重和资源计数；使用生产 queue/helper，不伪装为 Framework callback 测试 |
| [Just native 入口](../../../just/ffi/mod.just) | 新增 `pixi run just ffi android-backend-test`，在 canonical bindings 构建后发现完整 JVM 回归，不依赖一次性 class filter |

## 两端中断矩阵：源码事实与测试证据分开

| 操作/边界 | Android 源码行为 | Apple 共享 backend/controller 行为 | 本轮证据限制 |
|---|---|---|---|
| With-response write | native callback 完成 active owner；成功 domain 在 terminal 前 | 等精确 native callback | Apple 组合中断/隔离有 controller 回归；Android 无完整 backend dispatch/callback 测试 |
| Without-response write | local submission 成功即 terminal，但 **native callback drain 前不释放 lane/watchdog** | 容量允许后本地提交即可终结；CoreBluetooth 没有对应 write-response callback | 不把 local success 当 remote delivery，也不要求两平台使用相同槽位生命周期 |
| CCCD enable / disable | descriptor callback 完成；失败不发布成功 notification state | notify-state callback 完成；失败/退役不伪造成功 state | Apple mixed FIFO 回归已补；Android native registration/descriptor rollback 未验证 |
| Disconnect | active/queued FIFO connection errors，再发布对应连接事件 | controller retirement，再由 backend 投影连接事件 | controller/helper 顺序不证明 OS callback 时序 |
| Watchdog | active timeout / queued operation_aborted；route fail-closed | callback-waiting active timeout / queued operationAborted | Android 已 local-complete 的 native head 不再收到第二个 timeout terminal；连接失败仍独立报告 |
| Dispose / close | 先关闭 ingress，退役投影不发布；调用者拥有 abandonment | 先脱离 ingress，再 drain controller | 不把 silent drain 说成已观察到每条 command terminal |

### Apple 新增组合回归

`testMixedGattInterruptionPreservesTerminalOrderAndIsolatesReplacementRoute`：四种 head（response write、容量阻塞 no-response write、CCCD enable、CCCD disable）分别覆盖 disconnect/close；其中三种 native callback-waiting head 另覆盖 watchdog，共 11 cases，仍计为 **一个 XCTest test**。

断言 mixed FIFO 每条 command 的精确 error/order、没有伪造 domain success、unsubscribe cleanup native identity、重复 retirement 不重复终结、旧 characteristic/notify callback 不完成 replacement write，以及最终 controller ownership 清空。复用现有 fake native-object support 和生产 event projection，不新增模拟 backend。

这在真实 Mac 上运行共享 Apple controller，但**没有本轮 iOS device/CoreBluetooth/RF 执行**。旧 RF 或 macOS controller 结果不能外推为当前 iOS interruption pass。

## Android 已修复：local terminal 不能过早释放 Framework write 槽位

### 本地官方证据与故障调度

[Framework BluetoothGatt snapshot](../../platform-api-reference/android/BluetoothGatt.java) 的 `writeCharacteristic()` 在 native submission 前设置 `mDeviceBusy=true`；成功返回后不清除，write callback 才清除该状态。具体核对 `onCharacteristicWrite` 与 submission busy/return 分支；不手改官方快照。

修复前，`WITHOUT_RESPONSE` accepted 后直接调用 `finishCharacteristicWrite()`，它同时发布 terminal、清 watchdog、移除 head 并提交下一项。这留下两种源码可推导的调度：

1. native write 尚忙，下一项已提交而被拒绝；
2. Framework callback 清 busy 后、后端 Handler 消费旧 callback 前，下一次同 characteristic write 已成为 head，旧 callback 可能匹配到新 owner。

这是源码和 Framework 实现支持的缺陷调度；本轮未人为制造该真实 RF 时序，也不声称已在 OEM 设备复现。

### 修复边界

- 每个 `PendingIo` 的 terminal claim 在现有 `stateLock` 下最多一次；不是 downstream delivery receipt，不改变 DTO equality 或 native identity。
- 无响应写入 accepted 时保持现有成功 terminal（没有 `CharacteristicWritten`），但不 complete native head、不 drive 下一项，也不取消原 watchdog。
- 原生 callback（包括错误 status）drain head 后才 drive；已经 local-complete 的命令不再发布第二个 success/error terminal。
- Watchdog/disconnect 仍清空真实 native owner、watchdog/database/subscriptions；已 claimed terminal 不重复，其余 pending commands 保持原有 FIFO/error 分类。
- Native submission 期间若另一线程请求 disconnect、将 phase 改为 DISCONNECTING，但同 route/GATT/head 仍拥有提交，接受结果仍可终结一次。若 dispose 已关闭 lifecycle/移除 route，不重新开放 ingress 或恢复 publication。
- 资源计数保持真实：local terminal 后 pendingIo、callback owner 和 watchdog 仍可非零；原 ID/队列容量保持占用，直到 native drain/retirement。取消语义未改：native-active head 不能被 queued cancellation 移除。

**仍缺关键 backend-level 回归：** 当前新增测试通过 Framework-free owner 调用生产 queue/terminal/retirement helper；并未直接执行 `writeCharacteristicOnHandler()`、callback handler 和真实 Framework。若只把 backend acceptance 分支错误地改回提前 `finishCharacteristicWrite()`，helper 测试仍可能通过。后续需要生产路径的可控 native submission/callback 测试，或具名 native/RF 时序证据；不能用 14 个 JVM tests 消除此缺口。任意重复的同 route/同 characteristic callback，在前一个 callback 已 drain、下一个提交之后如何区分，也不属于本次修复已证明的范围。

## 未顺手更改的待审风险

1. **Android CCCD local registration failure recovery。** `configureNotificationsOnHandler()` 先调用 local `setCharacteristicNotification()`，再写 descriptor；失败的 `finishNotification()` 不恢复前一 local registration。尤其 disable 失败时，subscription bookkeeping 可能仍 active，而 local registration 已请求禁用。未做真实失败复现。局部恢复、恢复失败时 fail-closed、accepted descriptor error 后远端状态未知，应单独评审策略；不能把重新注册本地回调当作远端 CCCD rollback。本轮未改这条路径。
2. **Apple capacity-wait liveness policy。** No-response write 因 capacity 不足而未提交时，controller 没有 active native stage；backend 当前不为此状态 arm native I/O watchdog。如果 readiness 永不出现，lane 需其他取消/断连/close 才退役。该状态是否需要独立 bounded backend safety policy 尚待明确；不假造已有 timeout，也不为了测试通过改变生产 watchdog。
3. **物理故障可控性。** 当前 [multifunction capability declaration](../../../tests/peripherals/nrf52840/fixture_contract.py) 和 [read-hold controller](../../../tests/peripherals/nrf52840/diagnostic/read_hold.py) 声明的是 ATT **read** hold。Read WAITING 不能改名当作 active write/CCCD proof。后续设计必须先证明目标操作确实 in-flight；不能在普通 run 中悄悄刷入新固件或仅凭 dispatch accepted 后立即断连就宣称覆盖。

以上是审查发现/待验证边界，不静默修改唯一 Plan 或将全部项定性为已复现 production blocker。

## 实际验证与日志

### 已通过

- `pixi run just apple test`：通过，含 canonical package staging、Apple deterministic gate 和 16 项 pure selection/configuration tests。日志 `.work/apple-gatt-interruption-audit/apple-test.log`。
- `pixi run -e apple-ci swift test --package-path platforms/apple --filter GattIoControllerTests`：35 tests，零失败，含新增 11-case matrix。日志 `.work/apple-gatt-interruption-audit/gatt-controller-tests.log`。
- Android targeted queue/watchdog/resource/router：12 tests；完整 JVM：14 tests / zero failures/errors/skips，生产 bindings 重建后另以 `--rerun-tasks` 实际重跑。日志 `.work/android-no-response-{targeted,all,all-after-build}.log`。
- `ffi android-backend-build`、新增 `ffi android-backend-test`、`ffi android-backend-consumer-build`：通过。新 canonical test 调用中 Gradle test task 为 UP-TO-DATE；14 项实际执行来自前述 forced rerun，不将缓存再算一次运行。
- Android `3ac5cc09` 的 published backend 初始化单方法检查：`OK (1 test)`，从 staged AAR consumer 加载生产 backend，得到 adapter snapshot。它不执行 write/CCCD，也不是整组消费者通过。

Android 本机已有环境的执行使用：

```sh
PIXI_NO_INSTALL=true PIXI_FROZEN=true PIXI_OFFLINE=true BLEHUB_VERBOSE=true pixi run --as-is --offline just ffi android-backend-test
PIXI_NO_INSTALL=true PIXI_FROZEN=true PIXI_OFFLINE=true BLEHUB_VERBOSE=true pixi run --as-is --offline just ffi android-backend-consumer-build
pixi run --as-is --offline -e android python -m tools.run_android -- gradle -p platforms/android :library:testDebugUnitTest --rerun-tasks --no-daemon --offline --console=plain
```

普通 Pixi targeted JVM 调用首次在环境准备阶段超时 180 秒、日志为空，没有计为测试执行；随后显式复用已有环境，不安装/升级工具链、接受 license 或改锁文件。各命令完整日志保留于 `.work/android-gatt-interruption-audit-*`、`.work/android-no-response-*` 及 `/tmp/blehub-gatt-interruption-*`。

### 整组 Android 消费者失败：原样保留

对 `dev.blehub.ffi.runtime.consumer.PublishedArtifactConsumerTest` 的真机整组执行未通过：

- `publishedBackendAarLoadsAndReleasesItsCallerOwnedHandler` 的旧硬编码 `assertEquals(5u, ...contractVersion)` 失败，实际为 `6`。
- 该断言之后的 `backend.dispose()` 未被执行（它不在 finally 中），后续初始化测试因 process backend lease 未释放报 ResourceBusy。
- 中间 production object-graph consumer test 通过，但 runner 收到 per-test `-2`，整组仍为 failed；没有升级为 package runtime pass。

本轮没有修改这个无关旧断言/清理路径，也没有隐藏或移除失败测试。之后通过相同 installer/runner 独立运行初始化单方法，范围分开记录：

```sh
PIXI_NO_INSTALL=true PIXI_FROZEN=true PIXI_OFFLINE=true pixi run --as-is --offline -e android python -m tools.android.run_instrumentation --device physical --serial 3ac5cc09 --app-apk tests/consumers/android/app/build/outputs/apk/debug/app-debug.apk --test-apk tests/consumers/android/app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk --test-component dev.blehub.ffi.runtime.consumer.test/androidx.test.runner.AndroidJUnitRunner --test-class 'dev.blehub.ffi.runtime.consumer.PublishedArtifactConsumerTest#publishedBackendAarInitializesAndDeliversAdapterSnapshot' --log .work/android-instrumentation/gatt-interruption-consumer-init-20261018-01.log
```

整组日志 `.work/android-instrumentation/gatt-interruption-consumer-20261018-01.log`；独立初始化日志见命令。两次均固定 serial，未 revoke 权限、切 radio、启动 fixture 或执行 RF。

### 候选产物身份

| 产物 | SHA-256 |
|---|---|
| Android backend release AAR | `369ab89ac99d5093efa21b89f5f593db97690dcaa805778053141d9716500bf5` |
| Consumer debug APK | `1a2e8251fb178e8f1547e98bb3f615b13b94816be3b9a68faab52d8aef8bdb69` |
| Consumer instrumentation APK | `1ae52603b026bbae2019d05e7683831cc1f9321be8857c6d5a78e765470c8f4d` |

消费者 APK 的 `classes.dex` 包含新 `claimCommandTerminal` symbol，作为打包内容的有限核对，不替代精确 class-body/installed binary attestation 或 source-to-build proof。现有 RN APK 未据此自动更新或继承通过；未重跑 RN full-suite、iOS device、ART 完整 ABI 矩阵、10,000 lifecycle、长稳或 API-tier RF。

最终执行 `pixi run just fmt`、`pixi run just lint`、`pixi run just docs-check-mermaid`、`git --no-pager diff --check` 均通过；新报告 UTF-8/fence/whitespace 检查通过。AndroidBleBackend 的编辑器 diagnostics 刷新后无 error/warning。上述静态检查、测试和构建本身不证明任一未执行门禁。
