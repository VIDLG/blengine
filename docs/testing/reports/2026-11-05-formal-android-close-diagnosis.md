# Formal Android Close：已知 session 恢复、重复失败与最小阶段诊断

日期：2026-11-05。真实 Mac、同一 Android `3ac5cc09` / PHB110 / API 36。
前序：[maintained runner 与首个失败](2026-11-04-formal-android-managed-runner.md)。
后续：[点击前真实 PNG/XML/bounds 与 HTTP retry 预算修复](2026-11-05-formal-android-preclose-evidence.md)；取得按钮可见证据但正式 UI 仍失败，新 run 已分别恢复归档。
**正式 UI lifecycle/RF 仍无新增 pass。四个失败 run 均保留 failed 原证据，已分别完成原身份的物理恢复并归档租约；当前无活动 phone lease。原生 Kotlin 关闭隔离用例实测通过；clickGesture 验证未获改善，实验代码已撤回。**
未改 DUT BLE 权限、radio、firmware，也未执行 scan/RF。

## 1. 首轮 quarantine 的实际恢复

用户解锁后，ADB `get-state` 和 shell 恢复；boot 仍为
`35df0f9f-6c54-42b0-800c-3c410469b6e7`，不是手机重启。无需为了流程而重新插拔 USB。
原 run `6f7d93ab-648c-4b21-a5fe-67af2447904a` 的已记录 App 与 UiAutomator2 仍存活，
helper AppOp 为 allow、原临时 store 存在、三个 hidden-API policy 为 1。

恢复只使用本次明确授权、原 immutable bundle、fresh OS identity 和 exact quarantine lock
bytes/inode。没有把 JSON 报告重新包装成活动 `AndroidAutomationOwner`，没有修改其 one-shot
finalizer 或伪造原 DELETE 回执。

- 原 App PID `12178`、server PID `13156`，均核对 boot/start ticks/UID/cmdline/installed hash。
- 短暂恢复原 serial/systemPort 的 `--no-rebind` forwarding 后，`/sessions` 查询亦 timeout；未发盲目
  DELETE，失败证据与本次 forward 的删除记录保留。
- 后续正常 shell 一度也 timeout；`exec-out`/`shell -T` 对照读回恢复。该差异未证明 shell PTY 是根因。
- 最终强制停止 **身份仍与原记录一致的 instrumentation 进程**，证明相应 helper UID 无进程、
  active instrumentation 为空、Settings services 为空；原 device/host DELETE 均没有补写为成功。
- 原 App fresh identity 核对后 force-stop，证明原 UID/package 无进程。
- 仅恢复三个原来 absent、被本轮 driver 改为 1 的 global hidden-API keys；不用全局 reset。
- 仅删除 metadata/inode/bytes 均匹配本轮创建记录的 mock store；仅将原 helper 的 package-scope
  mock-location AppOp 恢复 deny。输入法、installed helper hashes、boot、forwarding 基线均复查。
- 原 failed bundle 再次离线复验后，归档 exact 已知 quarantine lock；原 journal/result 不改写。

最终恢复记录：

```text
.work/formal-android-managed-recovery-dbfd8276-5730-4ae9-ac4b-808511d95f8a/
.work/formal-android-managed-forced-retirement-connected.log
```

恢复结果 `status=restored / app_stopped=true / helper_restored=true / lease_archived=true`。
原 verdict 仍 failed；`graceful_blen_close` 与 `native_resource_zero` 明确 `not-claimed`。
恢复中 `/sessions` timeout 和一次首条 shell timeout 的失败目录及日志保留，不混入成功恢复：
`.work/formal-android-managed-recovery-8e6a20ef-01db-431f-838e-0e8075c42238/`、
`.work/formal-android-managed-quarantine-recovery.log`、`.work/formal-android-managed-forced-retirement.log`。

## 2. 同候选新 run 仍失败，不继续无诊断重跑

在独立 fresh readiness 检查中观察到 `mWakefulness=Awake`、`mStayOn=true`、keyguard 不显示，
`screen_off_timeout=1800000`。未修改这些设置；不能把第一次失联简单归因为自动锁屏。

原 APK `02ad94fef0a904d667930682a078093d12c75f11aae414fe472414483f71d0da` 通过正式
`formal-host-android-managed-lifecycle` 执行新 run：

```text
run_id=d4b5635f-af36-4a4e-84f2-cc050e6d363b
scenario timeout=180s; UI phase=20s; cleanup=90s; outer tool bound=300s
observations=[idle, started]
startup=poweredOn / scan denied / connection denied
status=failed; host_gate_status=failed; phone_status=quarantined; rf=not_run
```

这次 Close click HTTP **200 / 105ms**，但之后寻找 `session-state` 的请求不返回，20 秒 HTTP timeout；
与前次 click 自身未返回有区别。点击成功不证明 JS 回调、Rust close 或 UI `closed` 已完成。
随后 DELETE/本地 server TERM 清理失败，升级 SIGKILL；platform/helper 正确拒绝闭合，App finalizer
的 `am get-current-user` 也 timeout。没有增加等待时间或重放点击。

证据：

```text
.work/formal-android-managed-lifecycle/d4b5635f-af36-4a4e-84f2-cc050e6d363b/
.work/formal-android-maintained-runner-after-recovery.log
.work/formal-android-d4b5635f-offline-replay.log
```

原 failed bundle 离线完整性通过，未升级 verdict。

随后先保存原 PID 的定向 logcat、只读进程/thread/status 与 boot：

```text
.work/formal-android-close-diagnostic-44c4f1b3-c2cd-4be3-b859-2973f11e852c/
```

其中有 RN Fabric `SurfaceMountingManager:MissingViewState` 的 soft exception，但没有足够材料
把它、Rust 或 backend 指认为根因。现有 thread 输出没有可用的具体 Java/native 阻塞栈，
不能将 WCHAN 0 解释为线程健康或无死锁。

此 run 已按同样的原身份检查、强制 retirement、精确 helper/driver 恢复和 fresh final readback 完成
独立物理恢复；同时仅删除与原 run 完全匹配的 `tcp:8237 → tcp:6790` forward。

```text
.work/formal-android-managed-recovery-e6732d87-db03-446f-843f-4e60b7c0c685/
.work/formal-android-managed-d4b5635f-retirement.log
```

结果 `restored / helper_restored=true / lease_archived=true`，原 failed/quarantined journal 保持不变。
没有第三次无诊断执行或未知进程 adoption。

## 3. 本次代码变更：只增加必要阶段可观测性

未改 Rust、UniFFI contract、native 关闭策略、资源语义、timeout 或取消行为。

| 位置 | 阶段记录 |
|---|---|
| [正式 App](../../../tests/consumers/react-native-bleng/src/App.tsx) | `js.close.call / promise / receipt / rejected / finally`；区分同步返回 Promise 与异步结算 |
| [Kotlin Hybrid owner](../../../integrations/react-native-bleng/android/src/main/kotlin/com/margelo/nitro/blehubreactnativebleng/HybridBlengSessionOwner.kt) | 同步 close enter/returnPromise，coroutine action enter/return，Promise resolve/reject enter/return |
| [Kotlin Rust owner](../../../integrations/react-native-bleng/android/src/main/kotlin/com/blehub/reactnative/bleng/AndroidRustSupervisorOwner.kt) | `rust.closeResult.enter / return / fail`；这是 Kotlin→UniFFI 边界，不冒充 Rust 内部阶段 |
| [PlatformSession](../../../integrations/react-native-bleng/android/src/main/kotlin/com/blehub/reactnative/bleng/AndroidPlatformSession.kt) | `platform.waitEventReader`、`platform.resourceSnapshot` 的 enter/return/fail |

native 日志由 `Log.isLoggable("BleHubBlengClose", Log.DEBUG)` 门控，默认未开启；没有新增 JS/public
诊断接口。只输出固定 phase、monotonic timestamp 和线程 ID/name，不输出 business IDs、context、
payload、receipt 内容或错误详情。`fail` 只表示抛异常，带失败的 typed receipt 仍是 `return`。
JS host 日志只输出固定 phase 与 `performance.now()`，诊断 sink 抛错不影响操作交付/结算。

两端时间域不同，不能直接相减；应结合 logcat 时间/线程与 phase 顺序对齐。启用日志会扰动时序，
coroutine 可以早于同步 returnPromise 被调度，并发阶段可能交错；未为此新增 routing ID 或状态机。
Platform retirement 标记也可能来自启动失败等清理，不单独证明发生了用户 Close。

代码只用于下一轮定向诊断，不是根因修复。源码审阅未发现明显 main/JS 同步等待或 runtime self-join，
但这不能排除未观测到的阻塞。发现的独立 HandlerThread 正常退休疑点未在本次顺带修复或认作本次根因。

## 4. 验证与新候选边界

- `pixi run just react-native formal-host-test`：pending-close 回归先确认缺少阶段日志时 red；补实现后 **52 tests / 2 suites passed**，
  包含跨页面保持 pending owner、Promise 前后阶段顺序，以及诊断 sink 失败不改变实际 close。
- `pixi run just --no-deps react-native formal-host-check`：typecheck/autolinking passed。
- `pixi run just react-native bleng-check`：正式 spec typecheck/Nitrogen codegen passed。
- `timeout --kill-after=10s 180s pixi run just --no-deps react-native formal-host-android-build release`：
  **BUILD SUCCESSFUL / 30s**，编译 native Kotlin/JS bundled App；未安装、未执行新诊断候选。
- `pixi run just fmt` / `pixi run just lint` / `git --no-pager diff --check`：passed（diff check 仅提示既有 CRLF normalization warnings）。
- `pixi run just docs-check-mermaid`：207 Markdown / 27 diagrams passed；authored Markdown UTF-8/whitespace/conflict 检查通过。
- 两个原 run 的 `verify_lifecycle_bundle` 再次通过并保留 failed/quarantined；本地 active phone lock path 不存在，两把原 lock 位于各自恢复目录的归档中。

日志：`.work/formal-close-phase-{js-red,js-green,typecheck,codegen,android-build,fmt,lint,docs}.log`，
Gradle 完整输出 `.work/logs/formal-host-android-release-build.log`。

新诊断 APK（与两次 failed run 使用的原 APK不同，不继承其任何 runtime 观察）：

```text
path=tests/consumers/react-native-bleng/android/app/build/outputs/apk/release/app-release.apk
sha256=079d5b9a8cb166f514d0323069df0f0f95ec08d912aabc6bd2d70b12e4dd4d4e
size=43358253
```

## 5. 显式日志 scope 与真实诊断候选

已新增具体的 [`AndroidCloseDiagnostics`](../../../tools/react_native/bleng_android_close_diagnostics.py)，
并接入原 maintained runner 的 `--close-diagnostics` opt-in。普通入口默认无此 mutation。
它只在同 run 的实际 phone lease 下读/写固定 `log.tag.BleHubBlengClose`：保存合法原值、写前 intent、
设置 DEBUG/readback、原 candidate PID 的有界只读 logcat、独立 cleanup budget 恢复及 readback；
receipt hash/inode、parent directory、phone/run 与 deadline 漂移均检查。不释放 phone，不接受 JSON
readiness 代替 authority。原 mutation/receipt 不确定性不会被恢复读回洗成成功。

诊断日志在 session/进程清理前采集；设置恢复在其他独立收尾后、prepared evidence/phone release 前执行。
日志采集失败不阻碍 session/helper/日志开关的独立收尾，也不把实际成功 session DELETE 改写成失败；
总体结果仍失败。恢复或证据校验不完整则禁止释放 phone。收集的 PID/时间戳只是 diagnostic selector，
不是 live session authority、Close result 或 RF verdict。

bundle 成功语义亦检查可选诊断 scope：首读原值、DEBUG 准备读回、恢复 intent/末次读回及 verified
恢复回执一致；tag/command 必须固定，captured PID 与原 bound identity 一致，selector hash 对应
manifest 中的原 identity 文件。失败 bundle 允许真实缺失的 capture/restore，但不允许已有身份冲突；
重封 manifest 不能把错误恢复或替换 PID 变成成功，不从日志文本推导 Close 成功。

### 新真实 run

使用上述 `079d5b9a…` APK、原 180/20/90 秒预算，显式 `--close-diagnostics`：

```text
run_id=daed9cf0-0e44-4150-b37d-b3ad2770df13
original log.tag.BleHubBlengClose=""; DEBUG write/readback passed
original candidate PID=31729
observations=[idle, started]
status=failed; host_gate_status=failed; phone_status=quarantined; rf=not_run
```

Close click 请求 20 秒 timeout，未重放。清理前 logcat 命令成功返回，但当前带双 tag 的原 PID 日志
只有 `ReactNativeJS: Running "BleHubBlengHost"`，没有 JS `close.call` 或 native Close phases。
额外只读检查确认 APK Hermes bundle 确含 `BLEHUB_FORMAL_CLOSE`，DEX 确含 native tag 与
`rust.closeResult.enter`。这些观察不能证明 Close 回调已执行，也不能仅凭无日志就排除运行时阻塞；
更不足以把问题直接归因 Rust shutdown。

DELETE、本地 Appium 收尾再次失败，原 App finalizer 与日志属性恢复的 shell 命令也 timeout。
日志 scope 的首个 cleanup `getprop` 未返回，因此 `restored.json` 未发布，**不能声称 tag 已恢复**。
helper scope 仍拒绝未知 session 闭合；本次 phone lease 保留 quarantine。没有改变 DUT BLE/radio/firmware。

证据：

```text
.work/formal-android-managed-lifecycle/daed9cf0-0e44-4150-b37d-b3ad2770df13/
.work/formal-android-close-diagnostic-device.log
.work/formal-close-diagnostics-failed-replay.log
.work/formal-close-shell-differential-ae7e2372-8c07-48ac-b94b-221153f3054f.json
.work/formal-close-diagnostic-recovery-readiness-0891db72-9a72-47e4-b4a6-4550eea99131.json
```

ADB `get-state` 为 device，但普通 shell、`shell -n -T` 与 `exec-out` 的只读 boot_id 均有界 timeout。
没有继续重连、增加 timeout、重跑测试或根据先前 boot/PID 猜当前清理安全。对本轮已记录身份的
恢复必须等待真实 shell 可执行；此前两轮已归档的锁不等于本轮可以释放。

### 本次软件验证

| 实际命令 | 结果 |
|---|---|
| `pixi run just --command python -m unittest tools.tests.react_native.test_bleng_android_close_diagnostics -q` | 27 passed；实际 SDK/adb lookup 均隔离 |
| `pixi run just --command python -m unittest tools.tests.react_native.test_bleng_android_close_diagnostics tools.tests.react_native.test_run_bleng_android_lifecycle tools.tests.react_native.test_bleng_android_lifecycle.AndroidLifecycleTests.test_opt_in_diagnostics_capture_and_restore_before_phone_release tools.tests.react_native.test_bleng_android_lifecycle.AndroidLifecycleTests.test_diagnostic_capture_failure_preserves_session_cleanup_and_restoration tools.tests.react_native.test_bleng_android_lifecycle.AndroidLifecycleTests.test_diagnostics_restore_failure_blocks_phone_release_without_hiding_ui_pass -v` | 38 passed / 49.035s；缺入口时先确认 red |
| `pixi run python -m unittest tools.tests.react_native.test_bleng_lifecycle_evidence -q` | 36 passed / 0.770s；诊断语义先确认 red |
| `pixi run just --command python -m unittest tools.tests.react_native.test_bleng_android_lifecycle.AndroidLifecycleTests.test_opt_in_diagnostics_capture_and_restore_before_phone_release tools.tests.react_native.test_bleng_lifecycle_evidence tools.tests.react_native.test_run_bleng_android_lifecycle -v` | 45 passed / 16.650s |
| `pixi run just --no-deps react-native formal-host-check` / `pixi run just react-native formal-host-test` | typecheck/autolinking；52 Jest passed |
| `pixi run just fmt` / `pixi run just lint` / `git --no-pager diff --check` | passed；diff check 仅提示既有 CRLF normalization |
| `pixi run just docs-check-mermaid` | passed，日志 `.work/formal-close-diagnostics-docs.log` |
| `pixi run just react-native formal-host-lifecycle-verify .work/formal-android-managed-lifecycle/daed9cf0-0e44-4150-b37d-b3ad2770df13` | offline integrity passed，仍 failed/quarantined |

目标日志 `.work/formal-close-diagnostics-{scope-test,composition-red,composition-green,evidence-test,final-composition,fmt,lint,typecheck,jest}.log`。
扩展 owner/lifecycle 合成回归另有 60 秒 timeout，定位 watchdog 也有终止；均保留在 evidence-test log，
未算通过，没有扩大等待预算或声称全 tools-test 通过。

## 6. 解锁后恢复、原生通道隔离与 clickGesture 实验

### `daed9cf0…` 已恢复，不改写原失败

用户解锁后，shell boot_id 恢复且同 boot。已执行本轮专用 recovery script：原 server/App OS identity
匹配后强制停止，helper/hidden-API/IME/forwarding 回到原基线，并按原 logging receipts 仅将
`log.tag.BleHubBlengClose` 从本轮 DEBUG 恢复原空值、读回核对后归档 exact lock。

```text
.work/formal-android-managed-recovery-0a5a2d1f-376a-490c-874e-ed7956a2b95c/
.work/formal-android-daed9cf0-retirement.log
```

`app_stopped/helper_restored/diagnostic_tag_restored/lease_archived=true`。
原 device/host DELETE 仍没有成功回执，graceful BLeng close 与 native resource-zero 仍不声称。

### 电源时间线不支持直接归因休眠

恢复后的只读 Android event/power/window 记录：Awake、stayon、未显示 keyguard，原超时配置 30 分钟；
过滤后的 event ring 没有提供失败窗口内锁屏的直接证据。Mac `pmset -g log` 在 2026-11-05 08 点窗口
没有 Sleep/Wake/DarkWake 转换行。它们不是完整锁屏/USB activity trace，不能排除短暂安全策略或传输
异常，也不能把“用户解锁后 shell 恢复”直接当作已证明的根因。

```text
.work/formal-close-power-correlation-41706a83-c020-4da1-9a94-1017ea9f08c9/
.work/formal-close-host-power-61ab061f-7d1d-4a37-9a22-7b462ef924cb/
```

### 真实原生 Kotlin 用例通过，隔离 JS/UI 点击路径

使用正式 consumer 已有的
[`closeIsIdempotentAndOwnerCanRestart`](../../../tests/consumers/react-native-bleng/android/app/src/androidTest/kotlin/com/blehubblenghost/HybridBlengSessionOwnerInstrumentedTest.kt)，
默认 production owner，验证空 owner close、start/close、重复 close、restart 与 `@After` close。
通过既有 root-aware installer **仅安装本轮冻结的 test APK**，`grant=False`，不重装/替换原 release
候选，不启动 Activity/JS/Appium；复用既有 instrumentation parser，要求 exact class/method、numtests=1、
JUnit 完成及 `INSTRUMENTATION_CODE: -1`。phone lease 下安装/命令受总 120 秒与单测试 60 秒预算约束；
结束确认目标进程、active instrumentation 消失及原 installed candidate hash/boot 未变后释放租约。

```text
run_id=f7e5fa19-cc1d-4728-b07d-1b45b9d42f9e
native_test_status=passed; phone_status=passed; cleanup_errors=[]; rf=not_run
JUnit: OK (1 test), 0.149s
installed target APK=079d5b9a8cb166f514d0323069df0f0f95ec08d912aabc6bd2d70b12e4dd4d4e
test APK=e146357aba1575b3c43f631135b29607d20d2b89e443d79eb2036ef5e2da1ffd
```

证据 `.work/formal-android-native-close-f7e5fa19-cc1d-4728-b07d-1b45b9d42f9e/`、
`.work/formal-native-close-isolation.log`；本轮 Debug/App/androidTest 编译
`timeout --kill-after=10s 180s pixi run just --no-deps react-native formal-host-android-build debug`
为 **BUILD SUCCESSFUL / 20s**，日志 `.work/formal-native-close-debug-build.log`。

这是 native Kotlin/Nitro Promise → UniFFI → Rust → backend 的一个具体 lifecycle 用例，不经过真实
JS Promise/UI 点击；不能升级为 RN lifecycle pass、完整 native test suite、重复生命周期稳定性或 RF。
它仅证明此候选通过该通道的 start/close 可以完成，不能排除真实 JS/线程/调度交互中的其他问题。

### 有依据的单次 clickGesture 实验亦失败，已撤回代码

本地已安装 UiAutomator2 **8.7.3** 源码：标准 click 转发 `/element/{id}/click`；execute map 提供
`mobile: clickGesture(elementId)`，gesture 走独立接口。官方文档也将其描述为标准 Element Click
不可靠时的替代方式。版本源码摘录 `.work/formal-appium-click-source.txt`；补缺官方文档
`.work/formal-appium-click-official-docs.log`，来源：
[UiAutomator2 execute methods](https://github.com/appium/appium-uiautomator2-driver/blob/master/docs/reference/execute-methods.md#mobile-clickgesture)。

先增加实际 SDK 边界 red→green 测试，临时仅将 Android 的单次已定位元素点击改为官方 gesture；
不加坐标猜测、fallback/重试，不减弱前台/identity/状态/证据断言，iOS 不变。targeted UI/scan 与
opt-in managed 合成回归通过后，使用同一 `079d5b9a…` App/原 180/20/90 秒预算执行新 run：

```text
run_id=548f7c0a-cc91-45db-a1f0-466fa0d07ffe
observations=[idle, started]
status=failed; host_gate_status=failed; phone_status=quarantined; rf=not_run
```

真实 `mobile: clickGesture` 在原 Close 元素上仍 timeout；DELETE/App/helper/log tag 清理也未闭合，
采集的 filtered logcat 为空。**该实验没有解决问题，不能宣称标准 click endpoint 是根因。**
已精准撤回本次 gesture 分支、专用 fake SDK 与该分支测试，恢复 maintained `element.click()`；保留原
独特安全回归、诊断日志和全部 red/green/设备失败证据。未改生产 BLE/Rust 关闭代码。

```text
.work/formal-android-managed-lifecycle/548f7c0a-cc91-45db-a1f0-466fa0d07ffe/
.work/formal-android-click-gesture-device.log
.work/formal-click-gesture-{red,green,composition}.log
.work/formal-close-legacy-shell-probe-1b9a3ff7-e078-4cec-b63f-ac209cae77a4.json
```

当时只读对照普通与 legacy `shell -x`，读取 boot_id 均 4 秒 timeout；没有为了消除故障扩大权限、
修改设备安全策略、kill 全局 adb server 或重启手机。随后又一次 **只读** readiness 读回恢复且
boot 与 `548f7c0a…` 原记录一致，才执行该 run 专用的恢复脚本；不是在 shell 不可用时猜身份清理。

```text
.work/formal-close-548-recovery-readiness-812fe583-473a-486d-8814-417432857d09.json
.work/formal-android-managed-recovery-9a73cf13-50cf-479a-b978-d704f9c06e02/
.work/formal-android-548f7c0a-retirement.log
```

结果 `restored / app_stopped / helper_restored / diagnostic_tag_restored / lease_archived=true`，
原 host/device DELETE 缺失回执与 failed/quarantined journal 均保留，没有资源归零或 graceful close 声称。
当前 active phone lock path 不存在。

撤回点击实验后的当前源码验证：

- `pixi run just --command python -m unittest tools.tests.react_native.test_bleng_ui tools.tests.react_native.test_bleng_scan_ui tools.tests.react_native.test_bleng_lifecycle_evidence tools.tests.react_native.test_bleng_android_close_diagnostics -q`：**139 passed / 4.140s**。
- `pixi run just fmt` / `pixi run just lint`：passed。
- `pixi run just docs-check-mermaid`：207 Markdown / 27 diagrams passed。
- `pixi run just react-native formal-host-lifecycle-verify .work/formal-android-managed-lifecycle/548f7c0a-cc91-45db-a1f0-466fa0d07ffe`：offline integrity passed，仍 failed/quarantined，不升级。

日志 `.work/formal-close-post-experiment-{regression,fmt,lint,docs}.log`、
`.work/formal-click-gesture-failed-replay.log`。

## 下一步

不要继续仅更换点击机制或扩大 timeout。需要在现有单次 UI 场景 **Close 之前** 受控保存页面 tree、
元素 bounds/窗口及截图，确认实际可点击目标与 UI 状态；失败后 automation 已经阻塞，无法依赖
届时才请求页面。该诊断应保持原身份/authority/deadline，不能重放不确定点击或借新 driver 接管旧 session。
同时区分 SDK/transport 故障，不把 HTTP timeout、日志缺失或一个 native 用例通过直接当作根因结论。
iOS、scan/RF、reload 与 legacy internal 的迁移条件均未因此完成。
