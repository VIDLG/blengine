# BLeng 继续审计：初始化 snapshot 发布顺序

日期：2026-10-26

## 结论与范围

承接 [连接/取消结果保持收敛](2026-10-26-connection-cancel-functional-convergence.md)，本轮继续检查已批准 [Plan F03/F04/F12/F13](../../../.plan/PLAN.md) 的初始化、扫描、结果领取、取消、关闭和容量边界。

发现并用行为红灯证明一处新缺陷：初始化 dispatch 未确认时，后到的被动 adapter snapshot 可以越过较早的关联 snapshot/terminal；dispatch 确认后公开旧 snapshot，使事件流反序。本轮最小修复位于 initialization reservation，未更改公开 API、BleHub canonical ingress、actor 或其他 operation 机制。

本轮检查的 operation wait/close/deadline、deferred result 和 scan cancellation/retirement 路径没有再发现可证实的新缺陷；这只是所检查边界的结论，不是所有竞态穷尽证明，不宣布集中 review 或 Phase exit。真实平台 loss 与 iOS rejected-disable continuity 的 open 状态不变。

## 1. 可复现的错误顺序

输入顺序如下，均在 native `initialize` 返回 dispatch acceptance 之前：

```text
关联 AdapterSnapshotChanged(Some(init_id), PoweredOn)
CommandCompleted(init_id, success)
被动 AdapterSnapshotChanged(None, PoweredOff 或 Denied)
dispatch Ok(())
```

旧 [InitializationState](../../../crates/bleng/src/runtime/reservations/initialization.rs) 只缓冲精确 init command 的事件，被动 snapshot 立即通过；接受确认后才回放旧缓冲。因此公开变成：

```text
较晚的 PoweredOff/Denied
较早的初始化 PoweredOn
初始化 terminal
```

调用方按公开流更新最后观察状态，会被较早的 snapshot 覆盖。这不是要求推断 OS 因果总序，而是 BLeng 倒置了已经按明确顺序接纳的输入。

`initialize()` 返回其原关联 snapshot 本身不是 bug；operation 的历史完成结果不保证等于返回瞬间最新的 adapter 状态。

## 2. 最小修复与保护边界

`InitReservation` 增加一个私有标记，记录是否已经开始缓冲关联证据。只有在 dispatch 未确认、且前面已有相关 snapshot 或 terminal 被缓冲时，后续被动 snapshot 才共用原 pre-ack buffer。

- 首个关联证据之前的被动 snapshot 仍立即公开。
- 未知/其他 command 的事件仍通过，不扩大初始化 owner。
- 原初始化关联事件与后续被动 snapshot **合计最多 64 项**；第 65 项仍 overflow/fail closed，没有新建队列或放大上限。
- 接受后按接纳顺序回放；初始化结果仍只用首个精确 correlated snapshot，被动 snapshot 不能补足成功证据。
- 若 dispatch 拒绝却已存在关联回调，保持原 evidence-conflict/fail-closed 语义，不把暂存的后续状态泄漏到故障前 FIFO。
- dispatch 已确认后的被动状态继续正常公开；不保存 canonical adapter state，不控制系统权限/radio。

未为每种 OFF/Denied、terminal 顺序或平台建立独立状态机，没有引入框架、依赖、registry 或公开签名。

## 3. 测试先行与实际验证

新增测试位于：

- [initialization.rs](../../../crates/bleng/src/runtime/tests/initialization.rs)：`init_publication_preserves_off_update_after_correlated_evidence`、`init_publication_preserves_denied_update_after_correlated_evidence`，及对应 rejected-dispatch 的两项不泄漏断言。分别包含 snapshot/terminal 两种顺序。
- [reconciliation.rs](../../../crates/bleng/src/runtime/tests/reconciliation.rs)：`init_publication_uses_one_64_event_buffer_and_keeps_earlier_passive_snapshots_immediate`，保护 aggregate capacity、先前被动事件、外来 command、部分证据与接受后回放。

拒绝分支直接检查真实 actor 故障后的残留 FIFO，不仅断言 `next_event()` 返回 fault，避免掩盖已经提前公开的状态。

| 实际命令 | 结果与证明边界 |
|---|---|
| `pixi run cargo test -p bleng init_publication_`，修复前 | 编译成功，**0 passed / 5 failed**，行为 red。 |
| 同一定向命令，修复后 | **5 passed**。 |
| `pixi run cargo test -p bleng runtime::owner::tests::initialization` | **16 passed**。 |
| `pixi run cargo test -p bleng runtime::owner::tests::reconciliation` | **5 passed**。 |
| `pixi run cargo test -p bleng runtime::actor::tests::publication` | **16 passed**；上述相关三组共 37，包含新增用例，不与定向结果重复累计。 |
| `pixi run just test all` | **327 passed / 18 suites**；包含当前 BLeng、BleHub 与 testkit，不用数量替代需求覆盖。 |
| `pixi run just apple consumer-bleng-macos` | 当前五 target/三 slice XCFramework、component symbols、artifact-only generated Swift consumer runtime passed；不是 iPhone 或 BLE RF。 |
| `pixi run just fmt` / `pixi run just lint` / `pixi run just docs-check-mermaid` / `git --no-pager diff --check` | 通过。 |

完整日志位于 `.work/validation/2026-10-26-bleng-init-publication-{red,green,regression,workspace,macos-consumer,fmt,lint,mermaid,diff}.log`。没有修改或手工重写 API snapshots；本轮 private 状态修复不新增 ABI shape。

后续从架构层面收敛取消状态表示、修复测试完成屏障及评估生态的结果见 [取消状态设计收敛](2026-10-26-bleng-cancel-state-design.md)。本报告保留初始化修复当时的候选范围与计数。

本轮没有访问手机、执行 ART/XCUITest/RF、切权限或 radio、刷固件、启动长稳或 commit/push/branch。没有为了小范围 Rust 修复重复 Android/iPhone 全矩阵；旧包/真机记录保持原候选边界。

## 4. 设计审计判断

- **初始化/扫描不是因为代码形状相似就一律修改。** 未找到 completed scan-stop 被 sibling control 保留 root 的可达公开路径；start 未完成或其 cancellation receipt 未决会阻止 stop admission，stop 自身不建立第二项取消回执。初始化也没有这类 sibling root；故未复制之前 disconnect 的修复。
- **结果路径未扩大 scope。** 本轮核对既有单 receiver、completion observer、deferred result、normal close 与 fault 的区分，及 deadline/drop 的 retirement policy；没有依据新增另一套结果库或重写 actor。
- **仍不是“所有设计缺口已关闭”。** 完整 native callback setup、真实平台 write/CCC loss/same-Hub 恢复及 iOS continuity 裁决，继续按 [平台 loss 审计](2026-10-26-platform-loss-functional-convergence.md) 保留。未发现缺少新基本操作，不等于这些平台边界已解决。
