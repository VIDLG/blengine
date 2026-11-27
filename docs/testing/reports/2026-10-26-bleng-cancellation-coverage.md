# BLeng cancellation：功能覆盖、精确错误分类与有限清理

日期：2026-10-26

## 结论

用户授权清理 legacy 设计/测试，并允许引入合适生态。本轮按 [Plan F12 / §16.1.8–10](../../../.plan/PLAN.md) 重新核对实现、测试和生产 backend，不把 fixture 的取消成功外推为 native 支持。

- **取消功能有测试，但并非所有操作的 native cancellation 都已支持或完成真实平台验证。** 下表分开记录 BLeng 策略、Rust/foreign 测试和 production 能力。
- 修复 Apple/Android 的一个具体生产接线缺陷：仍存在的 pending connect/discovery 被误报 OperationNotFound，现为准确的 OperationNotCancellable。没有增加 active native cancellation 能力。
- 补齐 disable 取消回执与恢复 active subscription 的组合回归，并在现有公开 Rust consumer 中直接保护 initialize/stop/disconnect 的本地不可取消策略。
- 引入固定版本 **Proptest 1.12.0，仅 BLeng dev-dependency，std-only**；对同一个协议 oracle 增加有界长序列生成、失败缩减与持久化重放。
- 未发现可安全删除的 BLeng 生产兼容支路。清理两个已由协议 oracle 和定向回归覆盖的私有重复测试；两份旧候选文档增加历史快照声明，保留原证据、失败和路径。

本轮没有运行手机、ART、XCUITest 或 RF，没有权限/radio mutation、固件 provisioning、长稳或 commit/push/branch。不因清理历史内容或 fixture 全绿宣布集中 review、ABI freeze 或 Phase exit。

## 1. 取消覆盖矩阵

“允许请求”不等于 backend 必须能撤销。BLeng 的 control receipt 和原 operation 结果始终分开；取消被拒不能取消原 waiter。公开 typed handle 共用相同私有 control/result 机制，不为每个 wrapper 复制全部排列。

| 操作 | BLeng 策略 | Rust 测试及资源语义 | Production Apple / Android 与边界 |
|---|---|---|---|
| Initialize | pending 本地 NotCancellable；完成后 AlreadyCompleted；close 后 Closed | 本轮在 11 类公开 typed consumer 中增加 pending veto；既有 completion/close 回归保留 | 不派发 native cancel；不是缺少一个必须成功的取消 driver |
| Start scan | 可以请求，backend 可拒绝 | scan 接受/拒绝回执、被动停止、FIFO/容量和退役；typed surface 执行 start | 当前未提供 native scan control 成功保证；未新增 scan handle 的 foreign/native cancel verdict |
| Stop scan | pending 本地 NotCancellable | 本轮直接断言 pending veto 和完成后 AlreadyCompleted，仍要求原 stop 正常完成 | 不派发 native cancel |
| Connect | 可以请求，结果独立 | public typed 并发 wait/cancel、接受/拒绝、完成/loss、回执保活、FIFO | 本轮两端 production live target 明确 NotCancellable，不再假称不存在；fixture 接受不证明 native 支持 |
| Disconnect | pending 本地 NotCancellable；完整完成后 AlreadyCompleted | 既有 retained-root 回归；本轮增加 public typed pending/completed 策略 | 不派发 native cancel |
| Discover GATT | 可以请求 | discovery reservation 的 pending receipt / terminal retirement、typed wait | 本轮两端 live discovery 准确 NotCancellable；不新增 native abort |
| Read | 可以请求 | 接受/拒绝、late evidence、wait/drop/timeout、完成/loss/close 与 public wait/cancel | 两端 queued I/O 可移除，active 拒绝；Rust/foreign 结果不证明实际空口回滚 |
| Write | 可以请求 | reservation receipt/terminal 保活、typed wait、两种 write mode 和 loss 模型 | queued 可取消、active 拒绝；Apple 另保护 no-response capacity-wait head 的既有路径 |
| Enable notifications | transition 可以请求 | enable cancellation receipt / retirement、严格模式、provisional route 清理 | queued 可取消、active 拒绝；没有把 controller 测试计为新的 CCC RF cancel |
| Disable notifications | transition 可以请求 | 本轮补 pending receipt → terminal → control 的 actor 回归，成功移除/失败恢复 active route、value/FIFO/retry | queued 可取消、active 拒绝；不重跑或放宽 iOS rejected-disable continuity |
| RSSI | 可以请求 | reservation receipt、typed result、完成分类和 drop 后显式 cancel | 同一 I/O lane 的 queued/active 规则；真实平台中断仍保留独立边界 |

主要 Rust 入口：[typed_operations.rs](../../../crates/bleng/tests/typed_operations.rs)、[API tests](../../../crates/bleng/src/tests/facade_tests.rs)、[publication](../../../crates/bleng/src/runtime/tests/publication.rs)、[connection reservations](../../../crates/bleng/src/runtime/reservations/tests/connections.rs)、[dispatch protocol](../../../crates/bleng/src/runtime/reservations/tests/dispatch.rs)。

Foreign evidence 仍按 [核心功能审计](2026-10-25-core-functional-convergence.md) 中的 Swift/Kotlin typed artifact/runtime 候选记录：11 类 typed wait、single-consumer/lifecycle 和代表性 connect control，不宣称 11 类 pending cancel 都逐一在 foreign/native 中运行。共享机制、独有资源语义和平台事实各自选测；不能用“不逐 wrapper 复制”掩盖独有的 disable 恢复边界。

## 2. Test-first 修复 production 目标分类

### 根因与修改

两端原取消入口只查 GATT I/O lane；connect route 与 pending discovery 不在该 lane 中。因此 accepted、尚未完成的目标存在，却返回 OperationNotFound。

- Apple：[AppleOperationCancellation](../../../platforms/apple/Sources/BleHubApple/AppleOperationCancellation.swift) 是真实 `AppleBleBackend.cancelOperation` 使用的无状态接线。I/O 未命中后，查询已有 [connection owner](../../../platforms/apple/Sources/BleHubApple/ConnectionController.swift) 的 pending connect，以及 [discovery owner](../../../platforms/apple/Sources/BleHubApple/GattController.swift) 的 pending command。
- Android：真实 backend 在原 state lock 内调用既有 [AndroidConnectionLifecycle.cancelOperation](../../../platforms/android/library/src/main/kotlin/dev/blehub/android/AndroidConnectionLifecycle.kt)。I/O 未命中后，查询现有 connectCompleted/pendingDiscovery，不建立第二份 inventory。

live 但不可可靠取消 → OperationNotCancellable，携带准确 target ID；真正未知或完成/退役后已无当前记录 → OperationNotFound。没有添加 native 历史 tombstone，BLeng handle-local completion 仍负责完成目标分类。

queued I/O 移除、active 拒绝、容量、watchdog 和 generation/barrier 均不改变。

### Red / green 与 native checks

| 实际命令 | 结果 |
|---|---|
| `pixi run -e apple-ci swift test --package-path platforms/apple --filter AppleOperationCancellationTests` | 有效 red：2 failed / 1 passed，11 个断言指向 live target 被误报；green：3 passed。 |
| `pixi run just ffi android-backend-check --tests dev.blehub.android.AndroidConnectionLifecycleTest` | 有效 red：2 failed / 5 tests；green：5 passed，debug/release build passed。首次 fixture watchdog 方法名编译错误已保留，不计行为 red。 |
| `pixi run just apple test` | 完整 backend 91 passed；另含 17 项 RF scenario host tests，不是 RF 执行。 |
| `pixi run just apple build-ios` | generic iOS Simulator/device unsigned builds passed。 |
| `pixi run just ffi android-backend-check` | 41 passed / 8 suites，debug/release build passed。 |

日志：`.work/validation/2026-10-26-native-cancel-target-{apple,android}-{red,green,regression}.log`。

测试调用 production 使用的 seam/owner，保护准确 ID、重复请求、完成/退役后无历史记录、I/O head 保留、watchdog/sibling 不受 discovery 拒绝影响。不证明真实 Framework delegate 调度、RF 或远端取消。

## 3. BLeng 组合缺项补齐

[publication.rs](../../../crates/bleng/src/runtime/tests/publication.rs) 新增 `disable_cancellation_receipt_retains_transition_and_applies_original_outcome_once`：先 enable 成功，再 disable/cancel；共四条代表性序列。

- `Cancelled` terminal 早于接受回执：transition/receipt owner 不能提前丢失，结果一次 Cancelled，原 active subscription 保留，允许显式 retry。
- 取消被拒、原 disable typed failure：恢复 active route。
- 取消被拒、原 disable success：domain/terminal 两种顺序均移除 route。
- 共同保护精确五类 ID、value bytes、公开 FIFO、重复 request/receipt、不重复 claim/settle、retry 和 rediscovery barrier。
- 控制/退役本身不生成 value；测试显式输入 canonical value 检查路由，不合成平台事件。

首次直接 green，是补测现有实现，不称为生产 bug。定向 1 passed；publication 17 passed、reservation 22 passed（含有重叠，不重复累计）。日志 `2026-10-26-bleng-disable-cancel-{targeted,regression}.log`。

公开 Rust [11 类 typed consumer](../../../crates/bleng/tests/typed_operations.rs) 增加 initialize/stop/disconnect 的 pending NotCancellable，以及 stop/disconnect 完成后 AlreadyCompleted 断言。其 backend 特意配置 Accept：若 BLeng 错误地转交这些取消，便无法靠 backend 的拒绝产生假绿。原 operation 仍正常完成、无额外普通命令；定向通过，日志 `2026-10-26-bleng-local-cancel-policy.log`。

## 4. 有限清理与生态接入

### 实际清理

`dispatch tests` 抽出一个 `check_cancel_trace` oracle，供短序列穷举和 Proptest 长序列共同使用。移除两个私有测试：

- `cancellation_receipt_waits_for_dispatch_and_is_shared_by_retries`：其 request/retry/ack/claim/receipt 断言已经由短序列 oracle 覆盖。
- `rejected_cancellation_receipt_is_observable_and_clears_the_latch`：其拒绝、clear、再请求断言由 oracle 和更完整的旧/新 receipt 周期定向测试保护。

保留 terminal-before-receipt、local resolution-before-rejection、原始/公开缓冲阶段区别及完成目标分类等独有断言。检查没有 imports、Just CLI filter、discovery 或手动说明依赖被删除名称；Cargo `--all-targets` 仍发现新测试。

未发现 BLeng 内有可安全删除的生产 legacy 路径，故没有删除 convenience API、测试专用同步 seam、native safety 路径或历史错误 shape。较早的 [foreign API candidate](../../research/bleng/bleng-foreign-api-candidate-2026-09-29.md) 与 [assembly boundary](../../research/bleng/bleng-foreign-assembly-boundary-2026-09-29.md) 增加历史声明和现行入口；其中 6 个指向已移除 candidate 路径的死链接改为明确的历史路径文本，原候选发现、失败、命令及路径事实不改写。

### Proptest 的实际用途和预算

[manifest](../../../crates/bleng/Cargo.toml) 只在 dev-dependencies 使用 Proptest；[workspace pin](../../../Cargo.toml) 为 `=1.12.0`，关闭 default features，仅 std。该版本 MSRV 1.88，兼容 workspace Rust 1.95。已核对官方文档和实际下载的 1.12.0 源码，不依赖未锁定示例。

- 原 8 种动作、长度 5 的 32,768 条穷举保留：它给限定短轨迹的确定性覆盖。
- 新 property 每次 256 个生成案例，长度 0–96；失败最多 2,048 次 shrink / 1,000 ms，避免引入无限搜索。
- 复用同一 oracle，失败可缩减并通过默认 failure persistence / `PROPTEST_RNG_SEED` 重放；回归 seed 不在 gitignore 中，发现失败时保留文件并加入具体边界回归。本轮无失败，不生成伪 seed。
- 不引入 fork/timeout/bit-set 默认 feature，不改生产同步类型、runtime 或 BLE driver；不引入 Loom 或 proptest-state-machine 额外框架。
- `cargo tree -p bleng --edges normal` 不含 Proptest，inverse tree 确认为 dev-dependency；feature tree 只有 std 及其所需 regex-syntax，没有进入生产图。

定向 `pixi run cargo test -p bleng cancel_state_` 4 passed（包含穷举与生成测试）。图检查日志：`2026-10-26-bleng-property-{production-deps,dev-deps,features}.log`；测试日志 `2026-10-26-bleng-property-cancel-targeted.log`。

## 5. 最终整合与未关闭项

| 实际命令 | 结果 / 边界 |
|---|---|
| `pixi run just test bleng` | 228 passed / 3 suites。 |
| `pixi run just test all` | 330 passed / 18 suites，含 BLeng，不重复累计。 |
| `pixi run just apple consumer-bleng-macos` | 当前 package/component/slice 与 artifact-only generated Swift runtime passed，不是 CoreBluetooth RF。 |
| `pixi run just ffi android-backend-consumer-build` | 当前生产 binding/native image、backend AAR 与 artifact-only APK/test APK build passed；未安装、未运行 ART。 |
| `pixi run just fmt` / `pixi run just lint` / `pixi run just docs-check-mermaid` / `git --no-pager diff --check` | 通过。 |

日志前缀：`.work/validation/2026-10-26-bleng-cancel-coverage-{workspace,regression,macos-consumer,android-consumer-build,fmt,lint,mermaid,diff}.log`。

当前未发现新的 BLeng 基本 operation 实现缺席，不能由此断言所有设计/平台缺口消失。production callback setup、真实 write/CCC radio/permission loss、same-Hub 恢复及 iOS rejected-disable continuity 仍按 [平台 loss 审计](2026-10-26-platform-loss-functional-convergence.md) 保留。接受 NotCancellable 是精确能力边界，不是伪造“取消成功”；fixture、属性测试和包构建不能替代相关真实平台裁决或最终同候选设备闭环。
