# Formal Android managed lifecycle：maintained runner 与离线入口

日期：2026-11-04。范围：正式 RN 非 RF lifecycle 的 host 接线、软件验证与后续真实 managed attempt。下文记录的候选 failed、当时 cleanup 未闭合并 quarantine；不是新的真机 lifecycle/RF pass，也不改变阶段出口。2026-11-05 已完成独立物理恢复及 lease 归档，并保存第二次失败与诊断候选，见[后续恢复/Close 诊断](2026-11-05-formal-android-close-diagnosis.md)；原 failed bundle 与缺失 DELETE 回执未改写。

## 本次结论

已将 `run_android_lifecycle` 接为维护入口：

```text
Just formal-host-android-managed-lifecycle
→ run_bleng_android_lifecycle（参数解析）
→ run_android_lifecycle（实际 owners / 共享 UI lifecycle）
→ sealed lifecycle bundle
→ verify_lifecycle_bundle（发布后离线复验）
```

独立 `formal-host-lifecycle-verify ATTEMPT` 只验证记录完整性与一致性，不操作设备、不授予 session authority，
不将 `host_gate_status=pending/failed` 改为 passed。忠实记录失败的 bundle 仍可通过离线完整性检查。
旧 `formal-host-ui` 保留 attach-only 语义，Android 裸入口的 owning handoff 拒绝没有被绕过。

实现与使用说明：

- [薄 managed CLI](../../../tools/react_native/run_bleng_android_lifecycle.py)
- [实际 runner](../../../tools/react_native/bleng_android_lifecycle.py)
- [独立 lifecycle evidence](../../../tools/react_native/bleng_lifecycle_evidence.py)
- [Just entries](../../../just/react-native/mod.just)
- [consumer 用法](../../../tests/consumers/react-native-bleng/README.md#managed-android-lifecycle)
- [持久职责](../../research/react-native/react-native-host-tooling-architecture.md#正式-android-managed-lifecycle非-rf-的完整入口)

CLI 要求 reviewed APK/hash、三个唯一且完整的 helper hashes、显式安装与 helper settings 授权。
不隐式 build、安装 helper APK、grant DUT BLE 权限、切换 radio、准备 firmware 或运行 scan/RF。
root App installer 是显式选项，不失败后 fallback。Just 使用 recipe-local positional arguments，
保留空格/引号/特殊字符的参数边界，不将 variadic 参数合成一个字符串或当作 shell 程序展开。

## 结果与收尾边界

runner 复用原 `exercise_lifecycle`，不是第二套 Swift/Kotlin/UI 场景。
收尾顺序为 automation（session DELETE → server reap → platform finalizer → helper restore）→
App finalizer → prepared evidence → phone journal → final bundle。成功保留 Evidence，旧运行 App/session
拒绝接管，不为取得 COLD launch 擅自重启旧进程。主失败、各 cleanup、phone 与 publication 错误分开。

场景成功不合并不同 verdict；成功非 RF lifecycle 仍保留 `host_gate_status=pending`、`rf=not_run`。
初始化快照可为 denied/poweredOff，不从 start 成功推断 BLE scan readiness。这里的 helper mock-location
准备是真实 Android helper 配置及回滚，不是 BLE mock。

## 测试时钟缺陷与保留的失败

本次组合回归在 90 秒预算内未完成，日志为 `.work/android-managed-runner-regression.log`；未计通过。
此前扩展 discovery/组合测试的 120 秒、60 秒超时同样不计通过，也不据此声称整个 tools-test 通过。
有界诊断没有发现正常 lifecycle 永久停住；正常成功路径本身仍包含较多真实文件/identity/receipt 检查。

发现一个确定的 synthetic harness 缺陷：继承的 `time.monotonic()` 冻结为 `h.clock`，只有 fake SDK
调用推进；`time.sleep()` 却真实等待。持续缺失 UI 节点的 polling 中没有 SDK 调用，虚拟 deadline
因此永不到期。修复仅在 `LifecycleHarness` 上下文内配套推进同一虚拟时钟，未改变 production timeout。
新增缺失节点/状态不推进回归，验证真实 `_UiDeadline`、单次点击及清理；重复目录回归先要求首次
lifecycle 真正成功，防止诊断注入错误后仍出现假通过。实际 SDK 白名单、owner/identity/receipt 检查保留。

## 本次软件验证

| 实际命令 | 结果 |
|---|---|
| `pixi run just --command python -m unittest tools.tests.react_native.test_run_bleng_android_lifecycle -q` | 7 passed；新增 CLI 前先确认 import 失败；首次接线暴露参数合并失败，修正后通过 |
| `pixi run just --command python -m unittest tools.tests.react_native.test_bleng_lifecycle_evidence tools.tests.react_native.test_run_bleng_android_lifecycle -q` | 38 passed |
| `pixi run just --command python -m unittest tools.tests.react_native.test_run_bleng_android_lifecycle tools.tests.react_native.test_recipe_wiring tools.tests.react_native.test_bleng_lifecycle_evidence tools.tests.react_native.test_rf_evidence_bundle -q` | 119 passed，15.531s |
| `pixi run just --command python -m unittest tools.tests.react_native.test_bleng_android_automation tools.tests.react_native.test_bleng_android_session tools.tests.react_native.test_bleng_automation -q` | 76 passed，46.112s |
| 下述有界 lifecycle 命令 | 23 passed，129.451s |
| 下述有界 helper preparation 命令 | 44 passed，25.078s |
| `pixi run just react-native formal-host-android-managed-lifecycle --help` | passed，无设备执行 |
| `pixi run just --no-deps react-native formal-host-check` | typecheck/autolinking passed |
| `pixi run just react-native formal-host-test` | 2 suites / 51 tests passed |
| `pixi run just fmt` | passed |
| `pixi run just lint` | passed，含 RTK Cargo fmt/clippy 与 Ruff |
| `pixi run just docs-check-mermaid` | 206 Markdown / 27 diagrams passed |
| authored Markdown UTF-8/whitespace/conflict 检查、`git --no-pager diff --check` | passed；后者仅提示已有文件的 CRLF normalization warnings |

两个 owner 回归实际使用的有界命令如下，输出在当次工具会话中，未保存为独立日志文件：

```sh
pixi run python -c 'import faulthandler, unittest; faulthandler.dump_traceback_later(180, exit=True); unittest.main(module=None, argv=["unittest", "tools.tests.react_native.test_bleng_android_lifecycle", "-v"])'
pixi run python -c 'import faulthandler, unittest; faulthandler.dump_traceback_later(120, exit=True); unittest.main(module=None, argv=["unittest", "tools.tests.react_native.test_bleng_android_preparation", "-q"])'
```

其余本次日志位于 `.work/android-managed-runner-{combined,owners,help,typecheck,jest,fmt,lint,docs}.log`、
`.work/android-managed-cli-evidence.log` 与 `.work/android-lifecycle-cli-{red,green}.log`。

## 真机边界与下一步

本地 release APK 新鲜读取仍为：

```text
path: tests/consumers/react-native-bleng/android/app/build/outputs/apk/release/app-release.apk
size: 43357825
sha256: 02ad94fef0a904d667930682a078093d12c75f11aae414fe472414483f71d0da
```

对 `3ac5cc09` 的只读观察：phone lease path 不存在，`pidof com.blehubblenghost` 返回 `23624`。
它与先前要求保留成功页面的状态一致，但 PID 本身不是本轮 authority，也不证明旧 owner 已关闭或
资源为零。本次未停 App、未安装、未准备 helper、未 grant DUT BLE、未切换 radio、未操作 DK。
这是运行前的只读观察；用户随后明确授权继续关闭当前正式测试 App 并执行 managed lifecycle。
后续实际 attempt 与失败如下，不能继承[旧 one-shot candidate](2026-11-04-formal-android-managed-lifecycle.md) 的 pass。

### 后续真实 maintained attempt：failed / quarantined

1. 原 `da9a7148…` 的 retained identity 通过 fresh boot/PID/UID/start ticks/installed hash 检查后，在新
   App-only lease 下执行一次 `am force-stop`，`pidof` 确认不存在。临时收尾脚本错误地按任意空白分割
   `ps NAME`，遇到内核线程/zygisk 名称内的空格而失败，保守 quarantine：
   `.work/formal-android-explicit-close-8396af25-70c0-446a-ac13-01d610e3dd0e/`。
   后续用固定三列 `split(maxsplit=2)` 重新观察同 boot、无原 UID/package 进程，并核对 exact lock bytes/inode
   后归档该已知 quarantine。原失败和 journal 未改写，未执行 radio/reset。
   恢复记录见 `.work/formal-android-explicit-close-recovery.log` 及其具名 evidence 目录。
2. 首次 maintained CLI 在本地 helper 校验即拒绝，未 acquire phone 或操作设备：误复用 JSON evidence 的
   8 MiB `_record_digest` 限制；真实 UiAutomator2 server APK 为 17,968,722 bytes。
   日志 `.work/formal-android-maintained-runner-device.log` 保留。
3. 先添加大于 receipt budget 的 helper APK 回归确认 red，再从既有 inventory 提取
   `hash_helper_apk` 并复用到本地 preflight。保持原 128 MiB APK 上限、非链接 regular file、逐 chunk
   guard/deadline、descriptor/path signature 和 drift 检查；不扩大 JSON receipt 预算。
   targeted green 及后续 100 项 owner 组合已通过。
4. 修复后通过正式 Just/CLI 执行新 run `6f7d93ab-648c-4b21-a5fe-67af2447904a`：
   冻结 reviewed APK → helper/platform prepare → root-aware install/COLD launch → 真实 Appium session
   binding → 正式 `idle → started` 和初始化 snapshot 已观察。
   Close 点击的 HTTP 请求在 20 秒处 timeout，不能推断逻辑 close 已完成；点击未重放。
5. 随后的 DELETE 也 timeout，local Appium TERM 超时后升级 SIGKILL；fresh host process sweep 未发现该
   nonce route 的 Appium 进程，但本地进程消失不证明 device session 已退休。platform finalizer 和 helper
   restore 拒绝把未知/未闭合 session 当成功；App finalizer 的 `am get-current-user` 也 timeout。
   phone 保留 quarantine，临时 helper AppOp/store **未验证恢复**，App 最终状态 **未知**。

该 run 的状态：

```text
status=failed
scenario_status=failed
session_status=failed
host_gate_status=failed
phone_status=quarantined
observations=[idle, started]
startup_snapshot=poweredOn / scan denied / connection denied
rf=not_run
```

设备证据：

```text
.work/formal-android-managed-lifecycle/6f7d93ab-648c-4b21-a5fe-67af2447904a/
.work/formal-android-maintained-runner-device-fixed.log
.work/formal-android-maintained-failed-replay.log
.work/formal-android-maintained-failure-inspection/
```

场景预算 180 秒、独立 cleanup 90 秒、UI phase 20 秒、外层 tool 300 秒；实际命令以退出码 2 结束，
不是外层 timeout。完整调用参数保留在上述 device log；没有 DUT BLE grant、radio、firmware 或 scan/RF。

失败后 ADB `devices -l` / `get-state` 仍显示 `device`，但 `cat boot_id`、`ps`、
`am get-current-user`、`dumpsys activity instrumentation` 均在独立 5 秒检查中 timeout。
仅对本机选定 serial 执行一次 host-side `adb reconnect`，没有 kill 全局 adb server、手机 reboot 或
蓝牙 mutation；随后的有界 `wait-for-device` 未恢复，最后 shell 返回 `device offline`。
这是设备 transport/shell 不可用的实测，不足以归因 Rust close、Android backend 或 BLE 状态机。

离线执行：

```sh
pixi run just react-native formal-host-lifecycle-verify .work/formal-android-managed-lifecycle/6f7d93ab-648c-4b21-a5fe-67af2447904a
```

通过：完整保留 failed/quarantined 与 pending cleanup，不升级为通过。

本次修复后验证：

- `pixi run just --command python -m unittest tools.tests.react_native.test_bleng_android_lifecycle.AndroidLifecycleTests.test_helper_apk_above_receipt_budget_is_hashed_before_device_admission tools.tests.react_native.test_bleng_android_automation tools.tests.react_native.test_run_bleng_android_lifecycle -q`：31 passed / 8.805s，日志 `.work/formal-helper-apk-bound-green.log`；修复前 red 为 `.work/formal-helper-apk-bound-red.log`。
- `pixi run just --command python -m unittest tools.tests.react_native.test_bleng_android_lifecycle tools.tests.react_native.test_bleng_android_preparation tools.tests.react_native.test_bleng_android_session -q`：100 passed / 190.519s，240 秒上限，日志 `.work/formal-helper-apk-owner-regression.log`。

- `pixi run just fmt`、`pixi run just lint`、`pixi run just docs-check-mermaid`：修复后均通过；最后一项为 206 Markdown / 27 diagrams。日志 `.work/formal-managed-device-{fmt,lint,docs}.log`。`git --no-pager diff --check` 通过，仅提示三个既有文件的 CRLF normalization warnings。

下一步先恢复真实 Android 设备连接，再按本轮 immutable receipts 和 fresh OS identity 审核 session
retirement、App 与 helper settings/store 恢复；保留失败，不能仅因 server death/ADB reconnect 释放
租约或盲目开始第二轮。之后才是正式 typed scan 的权限 provisioning/readback、live installed-image
receipt 与 host composition。DUT BLE permission mutation、RF 和 firmware provisioning 仍须独立明确
范围；iOS concrete WDA/forwarder owner、iPhone runtime、typed operations/reload 与 legacy RF ledger
迁移尚未完成，不满足删除 internal 的条件。
