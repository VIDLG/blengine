# BLeng facade 文件整理与生成物比较

日期：2026-10-26

**当前阅读边界：**本文保留 facade 移动及随后 async begin 入口统一时的候选证据。用户之后明确批准同步 `begin_*`、异步 wait/cancel 且不保留旧签名，当前实现与新候选验证见 [同步 operation 提交](2026-10-26-bleng-synchronous-operations.md)。下文 `begin_*_at`、handoff timeout、签名未变及计数不能用于描述新同步候选。

## 范围与结果

用户批准将原 `api.rs` 按职责整理。首轮只调整私有模块、适配器命名、测试挂载和源码导航；未新增公开类型/方法、BLE 状态、队列、timer 或结果通道，没有改变 operation/cancel/timeout/cleanup 策略。下文“后续：统一 operation 入口”记录用户随后指出的 convenience 转发重复及其收敛，首轮生成物比较与计数保留原候选范围。

当前布局：

```text
crates/bleng/src/
  facade/
    mod.rs          Bleng 对外入口、timeout 校验、private-runtime task 适配
    operations.rs   11 类具体 typed handle 与共享 OperationAdapter<M>
  error.rs          错误类型
  runtime/
    operation.rs    私有 caller ticket、control、result slot 与解码
  tests/
    facade_tests.rs 原 facade/API 回归，断言保持
```

- [facade/mod.rs](../../../crates/bleng/src/facade/mod.rs) 不再混入全部 handle 定义；修正原模块声称拥有 typed errors 的过时说明。
- [facade/operations.rs](../../../crates/bleng/src/facade/operations.rs) 将 `ApiOperation<M>` 改名为 `OperationAdapter<M>`。构造、wait/cancel 和结果映射仍共用原 runtime 机制；必要可见性仅限 facade 父模块，没有变成 public API。
- [lib.rs](../../../crates/bleng/src/lib.rs) 从 facade re-export 原来相同的产品类型；没有保留旧 api 模块或兼容 alias。
- 测试文件改为 [facade_tests.rs](../../../crates/bleng/src/tests/facade_tests.rs)，保留结果竞争、超时边界、调用方 runtime 和 task-drop 等原断言。
- [snapshot owner](../../../tools/api_snapshots.py) 显式读取 facade/mod.rs、facade/operations.rs、error.rs；[工具回归](../../../tools/test_api_snapshots.py) 继续逐个删除/添加 handle 检查拒绝，同时检查新 facade 来源缺失 Bleng 时不会漏检。未放宽声明 allowlist。
- 更新相关 Markdown 源码链接，不机械替换 public API、API snapshot 等仍然准确的术语。

## 公开签名未变，但 UniFFI checksum 发生变化

移动前先执行 `pixi run just binding bleng-snapshot-check`，保存原源码、生成物、reviewed snapshots 和 native export 名称。移动后用同一锁定 UniFFI 再生成并比较，实际结果如下：

| 项目 | 前后比较 |
|---|---|
| Rust/Kotlin/Swift 三份 reviewed API snapshots | 字节一致，未手改或重新批准新的签名 |
| Bleng exported impl | 除 ApiOperation → OperationAdapter 命名替换与空白外，方法实现一致 |
| UniFFI 导出符号名称 | 无新增、无删除 |
| 8 个生成文件 | 仅 Kotlin bleng.kt 与 Swift bleng.swift 有差异；其余文件一致 |
| 上述两份生成绑定 | 差异仅为 UniFFI API checksum 数值；没有新方法、参数、DTO 或行为桥接代码 |

**不能把内部模块移动称为二进制完全兼容。** UniFFI 记录的 module path 参与生成校验；旧 BLeng binding 与新 native image 混用会被 checksum 检查拒绝。当前 ABI 尚未冻结，本轮不绕过检查、不伪造旧 module path、不添加旧模块兼容层；通过维护入口重新生成、打包并锁步消费。

比较证据：`.work/validation/2026-10-26-bleng-facade-before/`、`2026-10-26-bleng-facade-binding-comparison.json`、`2026-10-26-bleng-facade-binding.diff`、`2026-10-26-bleng-facade-after-exports.txt`。

首轮移动中间态出现模块路径/重复声明导致的编译和工具失败，已保留 `2026-10-26-bleng-facade-first-{compile,tools}.log`，不计为生产缺陷 red。同步修正后执行下列最终检查。

## 实际验证

| 命令 | 结果与边界 |
|---|---|
| `pixi run just test bleng` | 228 passed / 3 suites，原测试只变更模块挂载路径。 |
| `pixi run just test all` | 330 passed / 18 suites，包含 BLeng，不重复累计。 |
| `pixi run just binding bleng-snapshot-check` | 9 tool tests / 3 snapshots verified。 |
| `pixi run just apple consumer-bleng-macos` | 当前候选五 target/三 slice package 与 artifact-only generated Swift consumer runtime passed，验证新 binding/library 配对；不是 iPhone runtime 或 CoreBluetooth RF。 |
| `pixi run just ffi android-bleng-build` | 当前双 ABI single-image AAR 与 artifact-only consumer/test APK static build passed；未执行 ART。 |
| `pixi run just fmt` / `pixi run just lint` / `pixi run just docs-check-mermaid` / `git --no-pager diff --check` | 通过。 |

完整日志前缀 `.work/validation/2026-10-26-bleng-facade-{baseline,tests,tools,snapshots,workspace,macos-consumer,android-build,fmt,lint,mermaid,diff}.log`。

本轮没有操作手机、权限/radio、固件、RF 或长稳，没有 commit/push/branch。不将旧 Android/iPhone runtime 证据转移成当前 checksum 候选通过；[取消覆盖矩阵](2026-10-26-bleng-cancellation-coverage.md) 和 [平台 loss 审计](2026-10-26-platform-loss-functional-convergence.md) 的未关闭边界保持不变。

## 后续：统一 operation 入口

用户进一步要求 convenience API 真正复用 `begin_*` operation，不保留两份逐操作映射。原 convenience 已经共用 actor reservation、ticket、slot 和 deadline 机制，并非同步 BLE 实现或第二套状态机；但 facade 的 begin 与 runtime owner 的 convenience 各自构造 Begin 消息，仍有重复维护风险。本次收敛为：

```text
public begin_* ──── handoff deadline ──┐
                                      ├─ private begin_*_at ── owner.begin_operation_at
convenience ── completion deadline ───┘                         └─ same ticket/control/slot
              └─ operation.wait_until(the same deadline)
```

- 每个操作的参数 → Begin 消息只在 facade 的一个私有 `begin_*_at` 中映射；11 类 public begin 和 convenience 均复用它。
- `OperationStart` 在 facade 入口校验并计算 absolute deadline；runtime handoff 和 convenience wait 不重置预算。显式 typed handle 仍允许独立的 `wait(timeout_ms)` 预算。
- typed handle 保持已完成结果跨正常 close 可领取；convenience 保持原 in-flight close/fault 优先级。只是同一 ticket 的交付策略，未复制 native reservation、channel 或 completion。
- production `RuntimeOwner` 不再暴露逐操作 convenience 和 `run_operation`。owner/actor 测试仍需直接制造 admission/transport 竞争，相关简写移入 test-only [runtime/tests/support.rs](../../../crates/bleng/src/runtime/tests/support.rs)，不编入产品，也不冒充 facade 覆盖。
- `next_event`、assembly、shutdown 仍是 stream/lifecycle；不为统一外观把它们包装成可取消 BLE operation。初始化/disconnect 的既有 NotCancellable 策略及 backend 能力边界不变；drop/超时仍只退役 caller delivery，不自动 native cancel。

### 新增回归与验证边界

[facade_tests.rs](../../../crates/bleng/src/tests/facade_tests.rs) 补充 handoff 消耗总预算与独立 typed wait 的对照，以及 settled success/error、close/fault、过期 deadline 的优先级；[typed_operations.rs](../../../crates/bleng/tests/typed_operations.rs) 对照全部 11 类操作的实际 command、typed result 和 event，并验证 drop convenience waiter 不发 native cancel、原 reservation 仍要求显式控制。这是已实现机制的重构/补测，不将首次绿伪装成行为缺陷 red。

| 命令 | 本次结果 |
|---|---|
| `pixi run just test bleng` | 232 passed / 3 suites。 |
| `pixi run just test all` | 334 passed / 18 suites，包含 BLeng。 |
| `pixi run just binding bleng-snapshot-check` | 9 checker tests passed / 3 reviewed snapshots verified；未修改公开签名或模块路径。 |
| `pixi run just apple consumer-bleng-macos` | 五 target/三 slice package、single-image 与 artifact-only Swift consumer runtime passed。 |
| `pixi run just ffi android-bleng-build` | arm64-v8a / x86_64 single-image AAR、artifact-only consumer/test APK static build passed；未运行 ART。 |

整合检查 `pixi run just fmt`、`pixi run just lint`、`pixi run just docs-check-mermaid`、`git --no-pager diff --check` 均通过；Mermaid 检查 179 个 Markdown 文件中的 27 个图。本次修改的 6 个 authored Markdown 文件也通过 UTF-8、fence 配对、trailing whitespace 和 conflict marker 检查。diff 仅提示三个既有文件的 CRLF→LF 警告，没有 whitespace error。

完整输出在 `.work/validation/2026-10-26-operation-unification-{bleng,workspace,snapshots,apple-consumer,android-build,fmt,lint,mermaid}.log`；Android 分步骤日志在 `.work/android-bleng/logs/`。本次未取得新 iPhone runtime、Android ART 或 RF 证据；API snapshot 通过不宣称二进制兼容，bindings 与 native image 仍按候选锁步生成消费。

用户同日明确接受的 iOS CCC disable REJECT continuity 限制已回写 Plan §1.4 / AGENTS；原 gate 保持 failed/pending，该项不再单独阻塞 review。[平台 loss 关闭条件](2026-10-26-platform-loss-closure-readiness.md) 中其他真实系统 loss / same-Hub 项仍未关闭，不能由本次重构及无设备验证代替。
