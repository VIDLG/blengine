# Formal RN：iOS 依赖构建与临时 helper 所有权交接

日期：2026-11-04。真实 Mac、本地 Pixi/Just、未提交工作树。依据：[Plan](../../../.plan/PLAN.md)、[前序 Android managed lifecycle](2026-11-04-formal-android-managed-lifecycle.md)、[host 职责](../../research/react-native/react-native-host-tooling-architecture.md)。**本轮没有手机/模拟器运行、App 安装、权限/radio 变更、固件操作或 RF。**

## iOS safe-area dependency 已同步并编译

正式 consumer 的 `react-native-safe-area-context@5.11.1` 已经用于 Android 标题安全区修复；本轮通过原 [`bleng_ios_consumer`](../../../tools/react_native/bleng_ios_consumer.py) 更新并验证 iOS graph，不另建 Pod/bootstrap 入口。

实际执行：

```sh
pixi run just --no-deps react-native formal-host-ios-build
pixi run python -m unittest tools.tests.react_native.test_bleng_ios_consumer -q
```

`--no-deps` 复用已检查的 installed package 和 staged combined image，不重跑 Rust staging/npm 安装；owner 内 installed Podspec、PlatformContract、Pods 与 App/native-test 校验完整保留。原 owner 已使用 `pod install --no-repo-update`，没有 `--deployment`，因此无需修改 owner/Just 或手工编辑 generated Pods。

| 检查 | 本轮结果 |
|---|---|
| installed formal Podspec：正式 Swift owner、Apple/UniFFI/Nitro dependencies | 通过 |
| Nitro-free Swift PlatformContract | `BUILD SUCCEEDED` |
| safe-area native autolinking/codegen/Pod 安装 | 通过 |
| 正式 App + native-test `build-for-testing` | `TEST BUILD SUCCEEDED`，iOS Simulator 编译/链接 |
| iOS build-owner unit regressions | 7 passed |
| formal host typecheck/autolinking、Jest | 通过；Jest 51 passed |

[`ios/Podfile.lock`](../../../tests/consumers/react-native-bleng/ios/Podfile.lock) 与执行前保存的 snapshot 比较，新增 73 行、无删除：safe-area 5.11.1、common/fabric subspec、dependency/external-source/checksum；其他 pod checksum 未变。这里比较的是执行前工作树，不是把原 dirty/untracked 内容冒充本轮 Git diff。

更新后 lock 与 `Pods/Manifest.lock` 完全相同，SHA-256：

```text
bf6c8b171ee905f4c262773897420f28f5e0e98051a535e51bcace916c5b2694
```

日志、执行前 lock 与精确 diff：`.work/logs/formal-ios-safe-area-20261104/`。构建在外层 180 秒上限内完成，无 timeout/retry。保留 safe-area C++ 初始化顺序、RN headers、XCTest deployment-target 链接提示和 Swift embedding override 等非阻塞 warnings，未越界修复。

**这不是 iOS runtime、真机、clean npm artifact 或 RF 证据**；没有运行 XCTest/Simulator，没有进行签名/Keychain/密码 setup 或工具链安装。

## 修复 prepared-scan 接线中的 helper rollback 缺口

此前真实 Android lifecycle 的 one-shot composition 手动执行 helper restore，而通用 `run_prepared_scan` 只持有 managed automation 并通过其 `require_closed()` 授权 phone release。如果临时 helper scope 没有被转交给该 owner，runner 可能漏掉这部分收尾。

现在 [`ScanAutomationOwner`](../../../tools/react_native/bleng_automation.py) 显式接收：

```python
android_owner=platform_owner,
android_preparation=prepared_helper_scope,
```

- 非空 preparation 必须是真实 `AndroidHelperPreparation`，搭配同一 phone/run/home、三个 expected hashes、system port 和冻结 scenario deadline 的实际 Android owner。
- 构造和首个 Popen/Remote 前核对 handoff；运行中继续核对本地 scope 与不可变 evidence，不把完整设备 inventory 塞进每次 UI read。
- `AndroidAutomationOwner.require_helper_scope()` 与 helper `require_handoff()` 是 scope/evidence 交接检查，不是假 session admission 或 closed receipt。
- `android_preparation=None` 只表示普通 run 使用外部已 provision 的基线、parent 没有接管临时变更；不是隐式 provision、自动检测或 legacy fallback。创建了临时 scope 的 host 必须显式传入，不能在转交后再自行重复 restore。

close 串行执行：

```text
已知 session DELETE
→ 本轮 local server reap
→ 原 platform finalizer
→ 已转交 helper restore
→ parent require_closed 复验全部收尾证据
```

因此 [`run_prepared_scan`](../../../tools/react_native/bleng_scan_runner.py) 原有 `automation.close → App → passive fixture → evidence → phone` 链无需再加一套 helper finalizer 或散落 callback。helper restore 与其他 cleanup 共用独立总 deadline，不延长 scenario；parent 不释放 phone。

## 失败语义没有放宽

- 未请求 session 的 prepare/admission/spawn 失败可以按真实 owner 的 unsubmitted 状态恢复 helper，不伪造 DELETE。
- requesting/binding/未知 Remote outcome 即使随后 inventory 看似 idle，也不能当成 cleanup success；不接管或盲删别的 session。
- server reap/platform cleanup 失败仍保留主错误，同时尝试当前可安全归属的 helper rollback；其自身 unknown-session/file/AppOps 判据不取消。
- helper 的文件、UID scope 或 evidence 漂移、restore/publication 失败都使 parent `require_closed()` 失败，阻止外层 phone release；恢复旧文件字节不能复活已退休的失败 scope。
- `require_closed()` 重新执行 helper `require_restored()`，不只相信先前的 parent `close.json`。`host_gate_status=pending` 和 resource-zero 边界未改。
- iOS 没有接 Android preparation；原 WDA/forwarder callbacks 与其必填 finalizer 不变。

实现仅修改 `bleng_automation.py`、`bleng_android_preparation.py`、`bleng_android_session.py` 及 owner-local tests；没有新 CLI、registry、通用 settings 框架、产品 JS diagnostics 或 Rust/native ABI 变更。

## 已执行的软件验证

Test-first：新 parent-ownership case 先因缺少 handoff 接口 red；实现后 green。测试使用实际 concrete owners、真实临时文件/receipt 和既有 device-facing SDK/Popen fake 边界，不 mock 整个 prepare/restore 为成功，不让 synthetic device identity 到真机。

```sh
pixi run just --command python -W error::ResourceWarning -m unittest \
  tools.tests.react_native.test_bleng_android_preparation -q
pixi run just --command python -W error::ResourceWarning -m unittest \
  tools.tests.react_native.test_bleng_android_preparation \
  tools.tests.react_native.test_bleng_android_session \
  tools.tests.react_native.test_bleng_automation -q
pixi run just --command python -W error::ResourceWarning -m unittest \
  tools.tests.react_native.test_bleng_scan_host -q
```

- preparation targeted：44 passed，23.987 秒。
- preparation/session/automation：97 passed，60.803 秒。
- scan host：13 passed，6.417 秒。
- 四个修改 Python 文件的 targeted Ruff lint/format check 通过。
- 这些分组有重叠，不相加为独立覆盖数。日志：`.work/formal-android-helper-parent-{red,targeted,combined,scan-host}.log`。

特别覆盖 cleanup 在 scenario 过期后用独立预算完成、wrong scope 在首个 SDK/Popen 前拒绝、未提交 session 的失败、unknown Remote、helper 文件/UID/evidence 漂移、关闭后 evidence 篡改，以及 reap 失败仍尝试安全 rollback。

## 最终组合复验

```sh
pixi run just --command python -W error::ResourceWarning -m unittest \
  tools.tests.react_native.test_bleng_android_preparation \
  tools.tests.react_native.test_bleng_android_session \
  tools.tests.react_native.test_bleng_automation \
  tools.tests.react_native.test_bleng_scan_host \
  tools.tests.react_native.test_bleng_ios_consumer -q
pixi run just --command python -W error::ResourceWarning -m unittest \
  tools.tests.react_native.test_bleng_android_candidate \
  tools.tests.react_native.test_bleng_ios_candidate -q
```

- owner/handoff/build-owner 组：**117 passed，73.306 秒**。
- Android/iOS candidate 与 prepared-scan 组：**73 passed，111.062 秒**；保留 App retention、独立 cleanup/interruption、publication 与 phone quarantine 回归。
- `pixi run just fmt`、`pixi run just lint` 全量通过，含 workspace/all-targets Clippy；Ruff format 报告 301 文件未变。
- `pixi run just --no-deps react-native formal-host-check` 的 typecheck/autolinking 通过；正式 App Jest **51 passed**。
- `pixi run just docs-check-mermaid`：205 份 Markdown、27 个图通过；五份本轮 authored/updated Markdown 的 UTF-8、围栏、尾随空白、冲突标记及本地链接检查通过。
- 完整日志：`.work/formal-ios-helper-handoff-{fmt,owner-tests,candidate-tests,lint,host-check,host-test,mermaid}.log`。
- 这些是有针对性的组合，不声称完整 `tools-test`、移动 runtime 或 RF 通过。无 timeout、无 Git commit。

## 尚未完成

上一候选的 Android 真机 lifecycle 通过不自动继承为**本次新增 parent→helper handoff** 的真机结果；本轮仅有软件组合回归。正式 scan 的真实 fixture/permission 准备、完整 outer host bundle、iOS concrete WDA/forwarder producer、其他 operation/reload/RF ledger 迁移仍 pending。本轮不执行 RF、不改变 BLE 权限，也不能据此删除 internal 或完成 ABI/Phase exit。
