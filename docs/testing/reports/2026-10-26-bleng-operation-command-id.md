# BLeng operation 只读 CommandId：产品关联边界与候选验证

日期：2026-10-26

## 决策

用户允许按产品理想设计调整公开身份。源码核对后，为全部 11 类 typed operation 增加同步、只读、无失败的 `command_id() -> CommandId`。这不是为 Peripheral 协议添加的临时测试接口：公开 BleEvent、取消错误和日志已经携带 canonical CommandId，typed handle 若不能取得对应身份，调用方只能从 payload、事件顺序或私有 backend hook 猜关联。

范围与语义回写 [Plan §16.1.9](../../../.plan/PLAN.md#1619-已确认的补充决策)，职责说明见 [actor/reservation 设计](../../research/bleng/bleng-actor-reservations-design-2026-10-07.zh-CN.md)。本次不新增 OperationId、状态查询、取消机制、索引、registry、executor 或产品依赖。

## 1. API 与保证

```rust
let operation = bleng.begin_read_characteristic(connection, session, characteristic)?;
let command_id = operation.command_id();
let value = operation.wait(2_000).await?;
```

Swift / Kotlin 对应同步 `commandId()`，返回 canonical BleHub `CommandId`；不是数字字符串、异步 Promise 或另一个 DTO。

- 直接读取提交时已有的不可变字段，不分配 ID、不查 actor/runtime、不派发命令、不消耗结果。
- ID 属于这一 Bleng lifecycle；同一实例的 command 不复用，不同实例可能有相同数值。聚合日志仍需要 owner/session 上下文，不能把裸数值当作全局能力或跨 owner 的控制票据。
- 在 wait 消费、cancel、正常 shutdown 后，只要 operation 对象仍有效，关联身份仍可读取。foreign wrapper 显式 destroy 后不可继续使用对象，与 getter 无关。
- begin 同步拒绝时没有返回 operation；已成功提交但尚未 admission 或之后被拒绝的 operation 仍有自身身份。
- 不承诺 ID 对应的操作已经 actor admission、backend accepted、native pending 或 BLE 成功；这些需要各层实际证据。
- 公开 command ID 不提前分配 connection/GATT/subscription 等资源 ID，不增加资源 getter。
- eager submission 仍可在 begin 返回前产生事件；getter 不新增 listener 注册屏障、FIFO replay 或 broadcast。调用方需要有自己的早到事件处理策略，操作完成以 typed wait 为准。

## 2. 控制入口没有合并成裸 ID

推荐继续使用 `operation.cancel()`：它保留本地 predispatch cancellation 与已确认完成观察，不占有结果 slot。

既有 `Bleng::cancel_operation(id)` 仍保留 actor-routed control，适用于所属实例已有 command 的按 ID 控制；不承诺等价于 handle 的本地 predispatch shortcut。此次没有为了暴露身份新增一套取消路径，也没有借此删除已有产品入口。native cancellation 仍受操作策略和 backend 能力限制。

## 3. 为什么不回退 provider 协议

[system-loss provider 协议](2026-10-26-system-loss-provider-native-observation.md) 保持 ARM 后实际 dispatch/lane evidence 的一次性动态 binding：

1. getter 只能在 eager begin 成功后提供提交身份；不能在未来操作尚未创建时预测它。
2. 尚未产生的 subscription ID 仍由 actor admission 得到。
3. 获取 command ID 不证明 head HELD、backend 接受或 sibling 真正在 native FIFO 排队。

后续 native witness emitter 可以用 getter 预先标记“这是本次提交的操作”，再与真实 backend return、只读 lane snapshot 交叉核对，不能直接把 getter 输出升级为 admission/physical evidence。本轮未调整 firmware、provider gate 判据、radio 授权或 same-Hub 恢复验收。

## 4. 实现与测试

[facade/operations.rs](../../../crates/bleng/src/facade/operations.rs) 在原有 11 个具体 UniFFI impl 中显式导出 getter，返回原 `OperationAdapter.command_id`。没有新增生产状态，也未修改 begin、wait、cancel、native retirement 或 close 策略。

测试先写外部 Rust consumer 的 getter 调用，出现预期 E0599 编译红；实现后通过。此为缺少公开 surface 的结构红，不声称发现 native 行为缺陷。

- [Rust typed consumer](../../../crates/bleng/tests/typed_operations.rs) 扩展现有全 11 operation 流程，将公开身份与实际 backend command、domain/terminal 和取消错误关联，保留原有 once-only 结果、命令顺序和 disposal 断言。
- 在既有取消/生命周期场景中检查取消后、结果消费后和关闭后 ID 稳定；原 command 序列和 ID 断言同时排除 getter 额外 dispatch/分配。
- [Swift artifact consumer](../../../tests/consumers/apple/bleng-macos/Sources/BlengArtifactConsumer/TypedOperations.swift) 在每个 begin 后保存 commandId；与真实 foreign fixture 记录的 11 个命令集合核对，并对 read domain/terminal、cancel receipt/error 和 shutdown 后的身份做交叉检查。iOS unsigned consumer 复用同一 regression 源码。
- [Kotlin artifact consumer](../../../tests/consumers/android-bleng/app/src/androidTest/kotlin/dev/blehub/bleng/PublishedBlengTest.kt) 覆盖全部 getter 的可调用性、现有 backend command/error 的对应，以及 shutdown 后的稳定性；本轮只静态构建，未运行 ART。
- [snapshot checker tests](../../../tools/test_api_snapshots.py) 要求所有 handle 的 getter 为同步、无 Result/throws/suspend；[Android export audit](../../../tools/package_bleng_android.py) 精确增加每个 operation 的 `command_id` 导出，[包测试](../../../tools/test_package_bleng_android.py) 继续逐个移除导出验证 fail closed，没有放宽 allowlist。

未另建 11 个纯 getter 测试或构造器测试。BLeng / workspace 用例数保持原数量，扩大的是现有测试的有意义断言。

## 5. 实际验证

| 命令 | 结果与边界 |
|---|---|
| `pixi run just test bleng` | 243 passed / 3 suites；结构红及最终绿有日志 |
| `pixi run cargo clippy -p bleng --all-targets -- -D warnings` | passed；曾修正一个新增测试 lint，保留失败与重试记录 |
| `pixi run just test all` | 345 passed / 18 suites，包含 BLeng，不重复相加 |
| `pixi run just binding bleng-snapshot-update` | 从锁定 UniFFI 实际生成物更新三语言快照，不手改 generated binding |
| `pixi run just binding bleng-snapshot-check` | 9 checker tests passed / 3 reviewed snapshots verified |
| `pixi run just ffi android-bleng-test-tools` | 8 passed；包括逐个缺失 getter 导出的拒绝 |
| `pixi run just apple consumer-bleng-macos` | 五 target / 三 slice package、single-image、artifact-only Swift runtime passed |
| `pixi run just apple consumer-bleng-ios-unsigned` | generic device / Simulator unsigned consumer build passed；不是 iPhone runtime/RF |
| `pixi run just ffi android-bleng-build` | arm64-v8a / x86_64 single-image AAR 与 artifact-only consumer/test APK static build passed；未运行 ART |
| `pixi run just fmt` / `pixi run just lint` | passed |
| `pixi run just docs-check-mermaid` / `git --no-pager diff --check` | 27 个图 / 182 个 Markdown 文件通过；diff 无 whitespace error，仅三个既有文件 CRLF→LF 提示；5 个本轮 authored Markdown 文件通过 UTF-8/fence/whitespace/conflict-marker 检查 |

日志：`.work/validation/2026-10-26-operation-command-id-{red,bleng,clippy,workspace,snapshot-update,snapshots,package-tools,apple-consumer-final,ios-unsigned-final,android-build,fmt,lint,mermaid}.log`。Android 分步骤完整输出仍由 `.work/android-bleng/logs/` 保存。

新增方法有意改变当前未冻结 BLeng ABI；旧 binding/native image 不得混用，旧 Android/iPhone runtime 记录不自动成为本候选通过。本轮未运行设备、ART、RF、radio/权限、固件、长稳或 commit/push/branch。真实系统 loss / same-Hub gate 仍按具名证据关闭，不由 getter、snapshot 或包构建通过替代。
