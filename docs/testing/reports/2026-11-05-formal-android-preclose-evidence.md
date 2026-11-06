# Formal Android：Close 前原始证据与 HTTP 隐式重试修复

日期：2026-11-05。真实 Mac、Android `3ac5cc09` / PHB110 / API 36。
前序：[原生隔离、失败恢复及 Close 诊断](2026-11-05-formal-android-close-diagnosis.md)。

**结论：已取得 Close 前真实 PNG/XML/bounds，修复一个实证确认的 HTTP 自动重试预算问题；正式 RN
UI lifecycle 仍 failed。前两轮、后续 Start 正控及 UiAutomator2 双端日志 attempt 均已完成原身份物理恢复及租约归档，当前无活动 phone lease。
没有修改 Rust/UniFFI/native 关闭语义，也没有 DUT BLE grant、radio、firmware 或 scan/RF。**

## 1. 单次、同元素的点击前取证

显式 `--close-diagnostics` 下，在原 `_WorkbenchUi.click()` 已定位 Close 元素并通过原 foreground/
authority/native identity 检查后，调用 host-only `before_close(element, deadline)`。普通 run 不启用。
该 hook 不查找第二个元素、滚动、点击或重试；使用同一元素 id/parent、session、executor/config。
每个只读 SDK 边界检查 phone/run、原 candidate identity bytes/hash 与 caller authority，临时收窄 HTTP
timeout，退出恢复原 config。取证后再核对原元素、页面/state、可见/可用性和 fresh native identity，
才允许原来的一次点击；hook 失败、漂移、超时均不再点击。

scope 最终读取顺序为 PNG → XML → element rect → window rect → element rect；明确不是原子快照。
文件独占发布、固定 hash/inode，原 bytes 不裁剪/变换：

- `platform/close-diagnostics/before-close.png`：PNG ≤8 MiB。
- `before-close.xml`：UTF-8 ≤1 MiB。
- `before-close.json`：同元素、前后 rect、window、SDK read 时间、原 identity hash 与 raw 引用。
- 失败时保留已经取得的原始文件及 `before-close-failed.json`；仍独立尝试 helper/tag 收尾。

截图/页面树可能包含敏感 UI，仅留 ignored 本地 evidence，不上传外部服务。verifier 检查可选 metadata
的 identity、稳定有限 rect、raw 路径/hash/size/PNG signature；允许负坐标/屏外事实，**不从图片或
metadata 推导点击、Close、BLE 或 RF 成功**。历史普通/旧 diagnostic bundle 不强求新增文件；partial
failed 允许未完成项缺失，但不能保留已有身份/raw 引用冲突。

源码：

- [UI hook](../../../tools/react_native/run_bleng_ui.py)
- [实际 runner 的显式接线](../../../tools/react_native/bleng_android_lifecycle.py)
- [具体诊断 scope](../../../tools/react_native/bleng_android_close_diagnostics.py)
- [bundle 一致性](../../../tools/react_native/bleng_lifecycle_evidence.py)

## 2. 第一轮证明：读取就会阻塞，并非已发出 Close

候选 APK 两轮均为 `079d5b9a8cb166f514d0323069df0f0f95ec08d912aabc6bd2d70b12e4dd4d4e`。
原 180 秒场景 / 20 秒 UI phase / 90 秒 cleanup 预算未扩大。

```text
run_id=aa1535c9-008c-4560-9365-91b55298fdfb
observations=[idle, started]
status=failed; host_gate_status=failed; phone_status=quarantined; rf=not_run
```

首版取证以 `element.rect.before` 开始；该 **GET** 即失败，未走到点击。
同一原元素 `/rect` 在真实 Appium wire log 中出现 **4 个请求**，Close `/click` 为 **0**。
单次 read timeout 5 秒，但 metadata 记录约 20.06 秒后才返回，超过原 phase budget。
原始失败、空 rawfiles 和未执行 Close 均保留，不把先前“Close 附近超时”的描述当作本轮 Rust 调用证据。

```text
.work/formal-android-managed-lifecycle/aa1535c9-008c-4560-9365-91b55298fdfb/
.work/formal-preclose-device.log
.work/formal-preclose-failed-replay.log
```

## 3. 实证根因修复：显式超时路径禁止 SDK HTTP 自动重试

本机 Appium Python **5.3.1** / Selenium **4.51.0** 源码确认：`AppiumClientConfig` 将参数传给 Selenium，
`RemoteConnection._get_connection_manager()` 从双层 `init_args_for_pool_manager` 读取 pool 配置。
旧配置继承 urllib3 默认 `retries.total=3`；有界本地 HTTP server 红测确认 GET 被发出 4 次。

[Android builder](../../../tools/android/android_appium.py) 的显式 `command_timeout` 分支现加入：

```python
init_args_for_pool_manager={
    "init_args_for_pool_manager": {"retries": Retry(total=0)}
}
```

使用现有 urllib3 依赖，无新包；未传显式 timeout 的路径不变，未修改第三方源码、iOS 或 archives。
可控的 UI helper polling 保留，但 SDK 不再隐式重发 GET/DELETE/POST，session 构造也没有 fallback。

新回归捕获实际 client config，构造真实 `AppiumConnection`/Selenium manager，检查有效 pool，而非仅
断言字典；本地 server 对 GET read timeout 和 POST timeout 均只观察 **1 次请求**，服务与 handler
线程被关闭/等待。target module **13 tests passed / 0.763s**。

该修复消除已证实的重试放大；**不声称 socket timeout 等同可硬中断的端到端总 deadline**，也不声称
它修复了设备/automation 阻塞。callbacks/HTTP 前后仍检查绝对预算，in-flight SDK 限制保持明确。

## 4. 第二轮取得 PNG/XML：按钮确实可见，但 UI gate 仍失败

取证顺序改为先保存 PNG/XML，保持同元素及全部 guard，不更改点击实现：

```text
run_id=ceb7314c-064b-4c08-b968-2bcaeceddea8
observations=[idle, started]
status=failed; host_gate_status=failed; phone_status=quarantined; rf=not_run
```

取证全部读取约 0.9 秒完成：

| 原始观察 | 值 |
|---|---|
| window | 1080 × 2412，origin (0, 0) |
| Close rect，前后相同 | x=48, y=1838, width=984, height=105 |
| 原元素 id | `00000000-0000-018b-ffff-ffff00000066` |
| PNG | 208,015 bytes；SHA256 `182feb79bfcaf9e55711980089240b7c78fe08b3213541eca29503b92a213f16` |
| XML | 25,534 bytes；SHA256 `69ac3306607dc68aebf628d3f58604b297fca60926658702ccc125251311e65c` |

原 PNG 目视可见正式 App、Current run、`started`、poweredOn/scan denied/connection denied 快照，
Close 按钮完整位于 viewport 内；该帧没有锁屏/键盘遮挡。XML/rect 亦被保留。
这只约束 **取证时刻**，不是对点击瞬间、之后画面或 device state 的原子保证。

取证后的 foreground/native identity/UI-state/原元素显示与启用检查通过；真实 wire log 确认 Close
**只点击 1 次**，HTTP **200 / 104ms**，之后读取 `session-state` 的查询 timeout（20 秒、单请求）。
DELETE 也只提交一次；本轮仍未确认 UI `closed`、Rust Close 结果或正常清理，不因 click HTTP 200 判通过。

```text
.work/formal-android-managed-lifecycle/ceb7314c-064b-4c08-b968-2bcaeceddea8/
.work/formal-preclose-no-retry-device.log
.work/formal-preclose-no-retry-failed-replay.log
.work/formal-preclose-wire-summary.json
```

wire summary 去除 ANSI 颜色后按原元素 id 计数：首轮 rect=4/click=0；第二轮 rect=2（两次显式稳定性读）/
click=1。没有以新增 SDK 自动读取重试实现“稳定性”。截图只临时复制到工作树根目录供本地查看，随后
已删除该临时副本；原 evidence 文件保持不变。

## 5. 物理恢复与当前状态

两轮失败后的 SDK/App/helper/log tag 清理均曾遇到设备 shell 超时，分别保留 quarantine；不在未知
身份下强制释放。之后先只读确认同 boot shell 恢复，再按各自原 server/App PID/start ticks/UID/hash
执行已记录身份的强制 retirement、精确 helper/hidden-policy/IME/forward 恢复和 log tag 原值读回，
最后归档 exact quarantine lock。原 failed bundle 不改写，缺失的 host/device DELETE 不补写成功；
graceful close/native resource-zero 不声称。

```text
AA recovery:
.work/formal-android-managed-recovery-d2c82693-bcd9-4762-8b38-6a5b3ae359cb/
.work/formal-preclose-aa1535c9-retirement.log

CEB recovery:
.work/formal-android-managed-recovery-a19eb4ca-3dd9-4c0a-8253-34e4d9c353cf/
.work/formal-preclose-ceb7314c-retirement.log
```

均为 `restored / app_stopped / helper_restored / diagnostic_tag_restored / lease_archived=true`。
当前无活动 phone lease。定向 event log 的 ANR tags 采集为空，不能据此证明无 ANR 或归因具体模块。

## 6. 软件验证

| 实际命令 | 结果 |
|---|---|
| `pixi run just --command python -m unittest tools.tests.react_native.test_bleng_ui.LifecycleTests.test_preclose_capture_uses_chosen_element_once_and_rechecks_before_click -q` | 缺 hook 时先 red，随后纳入组合 green |
| scope 专项 unittest（完整调用/输出见 scope-test、scope-order-test logs） | 初版 43、PNG/XML 优先版 44 项通过；原身份/配置/失败恢复边界保留 |
| `pixi run python -m unittest tools.tests.react_native.test_bleng_lifecycle_evidence -q` | 40 passed / 1.051s，before-close 字段/原始引用重封篡改先 red |
| `pixi run python -m unittest tools.tests.react_native.test_android_appium -v` | 13 passed / 0.763s，真实 pool/本地 HTTP 边界红绿 |
| `pixi run just --command python -m unittest tools.tests.react_native.test_android_appium tools.tests.react_native.test_bleng_ui tools.tests.react_native.test_bleng_scan_ui tools.tests.react_native.test_bleng_android_close_diagnostics tools.tests.react_native.test_bleng_lifecycle_evidence tools.tests.react_native.test_bleng_android_lifecycle.AndroidLifecycleTests.test_opt_in_diagnostics_capture_and_restore_before_phone_release -q` | **175 passed / 28.532s** |
| `pixi run just --no-deps react-native formal-host-check` / `pixi run just react-native formal-host-test` | typecheck/autolinking / 52 Jest passed |
| `pixi run just fmt` / `pixi run just lint` | 修正新增测试的 closure/style 诊断后通过 |
| `pixi run just docs-check-mermaid` | 208 Markdown / 27 diagrams passed |
| `git --no-pager diff --check`、authored Markdown UTF-8/whitespace/fence/conflict 检查 | passed；diff check 仅提示既有 CRLF normalization |

新 hook 初次合成测试还暴露了 synthetic Remote 替换 executor 后没有模拟 SDK `_client_config` accessor；
修复 owner-local fake 的实际 SDK 形状后 green，未弱化 live guard。未为捕获权限/错误添加全局成功 mock。

日志 `.work/formal-preclose-{hook-red,scope-test,scope-order-test,evidence-test,composition-green,no-retry-composition,fmt,lint,typecheck,jest}.log`，
`.work/formal-no-http-retry-test.log`。最终当前源码再次执行同一 175 项组合亦通过（28.352s），日志 `.work/formal-preclose-current-regression.log`；文档日志 `.work/formal-preclose-docs.log`。两轮 failed bundle 均独立 offline replay passed，仍 failed/quarantined。
没有全 tools-test、iOS runtime、ABI freeze、Hermes reload 或 RF 通过声明。

## 7. Start 正控已实测：本轮在 Close 之前的截图读取失败

在同一 gated native tag 中增加 `probe.start.*`、`probe.rustStart.*` 固定阶段，作为已知成功事件正控；
没有新增 JS API、记录 payload/identity 或改变 Start/Close 语义。未把新日志设为历史 bundle 的必填项，
也不从日志推导 host gate 成功。

新 release APK 为 43,358,253 bytes，SHA256
`9d2a8b1e85a2c9528b2de1bbba67eab7a78f3e75b80410d01347e694453f0cdc`。
离线检查确认 Hermes bundle 含 `BLEHUB_FORMAL_CLOSE`，DEX 含 Start 正控和 Close 标记。
使用 maintained runner、原 180/20/90 秒预算、相同 helper hashes 和显式诊断 scope：

```text
run_id=0f13db83-2d53-4002-baaa-c3e782de8bdf
boot_id=35df0f9f-6c54-42b0-800c-3c410469b6e7
app_pid=15347
observations=[idle, started]
status=failed; host_gate_status=failed; phone_status=quarantined; rf=not_run
```

原 PID 的 `platform/close-diagnostics/004-command.json` 记录了完整正控：

```text
13:45:46.163 probe.start.enter
13:45:46.166 probe.start.returnPromise
13:45:46.169 probe.start.action.enter
13:45:46.214 probe.rustStart.enter
13:45:46.231 probe.rustStart.return
13:45:46.231 probe.start.action.return
13:45:46.231 probe.start.promise.resolve.enter
13:45:46.231 probe.start.promise.resolve.return
```

**日志 tag/原 PID 采集通道有效，native Start 和 Promise resolve 已返回。** 此后 Appium 仍成功读取
`started`、初始化快照、页面切换以及 Close 元素的 displayed/enabled 和 foreground package。
但 `before-close-failed.json` 的唯一 SDK read 是 `driver.get_screenshot_as_png`：约 **5.038 秒**后
超时，`rawfiles={}`，没有 XML/bounds，也没有 Close click。wire log 只有 **1 次 GET /screenshot**；
异常类名 `MaxRetryError` 不代表本轮发生重复请求。日志缺少 Close phases 与本轮未发出 Close 一致，
不能用来推断此前不同 run 的 Close 是否执行。

清理中的 DELETE、App finalizer 和 tag readback 随后也超时；原 bundle 保留 failed/quarantined。
这不是 Rust Close deadlock 的证据，截图故障尚未定位到 Appium host、device UiAutomator2 或系统服务
的具体边界，不扩大 timeout、不换点击机制，也不通过调整 Rust runtime 线程数试图掩盖它。

### 恢复前只读观察与独立恢复

之后一次有界 boot shell 检查已经恢复，无本轮重插 USB、重启手机或 radio 操作。按原记录身份退休前，
只读保存了线程清单、`activity lastanr`、power、events 和原 App PID 全 tag 日志：

- App 线程清单共 38 条，均为 sleeping 状态；WCHAN 显示 0，未取得阻塞栈/CPU 时间差，不能据此
  排除早先瞬时阻塞或宣称 Rust 没有资源问题。
- `lastanr` 返回自 boot 无 ANR；power 当时为 Awake、stay-on，不能据此证明故障瞬间没有异常。
- 原 App 日志在 13:46:01.216 有 Fabric `SurfaceMountingManager:MissingViewState` 软异常：
  `Unable to find viewState for tag 230 for deleteView`。这是新线索，不是已确认根因；不将它解释成
  native Close 已执行，也没有据此升级/修改 RN 依赖。

这些是 **shell 恢复后** 的非原子观察，不是截图阻塞瞬间的线程快照。原始系统数据可能包含敏感信息，
仅留 ignored 本地文件，不上传，也不加入已 sealed 的原 bundle。

随后原 boot/App/instrumentation/hash/UID/start ticks 匹配，完成 exact instrumentation/App force-stop、
helper store/AppOp、hidden policy、IME、forward 和原空值 log tag 的精确恢复及读回；归档 exact lock。
没有补写成功 DELETE，没有宣称 graceful Close 或 native resource-zero；原 failed 文件保持不变。
初次直接执行 ignored recovery script 因项目 import path 缺失而未发出设备命令，改用项目根目录的
`runpy.run_path` 后实际恢复成功。

```text
.work/formal-android-managed-lifecycle/0f13db83-2d53-4002-baaa-c3e782de8bdf/
.work/formal-start-positive-device.log
.work/formal-start-positive-system-evidence.json
.work/formal-start-positive-retirement-execution.log
.work/formal-android-managed-recovery-59a41f6d-98bd-4ccd-bd81-6a7e9cd767a6/
```

恢复结果 `restored / app_stopped / helper_restored / diagnostic_tag_restored / lease_archived=true`。
原 bundle offline replay integrity passed，verdict 仍 failed/quarantined；当前无活动 phone lease。

本轮实际软件验证：`pixi run just react-native bleng-check`、
`pixi run just --no-deps react-native formal-host-android-build release`（外层 180 秒 timeout）、
`pixi run just --no-deps react-native formal-host-check`、`pixi run just react-native formal-host-test`
均通过，Jest 为 **52 tests / 2 suites**；`pixi run just fmt`、`pixi run just lint` 通过。
完整输出见 `.work/formal-start-positive-{codegen,build,typecheck,jest,fmt,lint}.log`；offline replay
为 `.work/formal-start-positive-offline-replay.log`。再次执行第 6 节同一 175 项 Android HTTP/UI/scan/
诊断/bundle/runner 组合，**175 passed / 30.936s**，日志 `.work/formal-start-positive-regression.log`；
文档与注释更新后 fmt/lint、docs-check-mermaid 和 diff --check 再次通过，日志
`.work/formal-start-positive-final-{fmt,lint}.log`、`.work/formal-start-positive-docs.log`、
`.work/formal-start-positive-diff-check.log`。这些不是 RN lifecycle/RF gate 通过。

## 8. 双端日志已接通：host 尝试转发，设备日志未见截图入口

### 最小软件改动与本地依据

复用 [`AppiumServer`](../../../tools/mobile_appium.py)，新增严格 bool、默认 false 的 `debug_logging`：
仅 opt-in 时传 `--log-level debug --log-timestamp --log-no-colors`；原普通 info 命令不变。
[`ScanAutomationOwner`](../../../tools/react_native/bleng_automation.py) 转交该设置，managed Android
runner 只从原 `close_diagnostics` 显式启用，不新增命令入口或修改 Android/iOS caps、超时和 SDK retry。
本机实际 Appium server 3.9.0 的 `server --help` 已确认这些参数，输出
`.work/formal-appium-server-help.log`；入口解析至本机现有 npm cache，不下载或修改第三方。

[`AndroidAutomationOwner.capture_server_logs`](../../../tools/react_native/bleng_android_session.py)
只在未 finalized 的 bound owner 下、原 receipt/hash/phone/run 守卫通过后，复用有界 `_command`：

```text
adb -s <original serial> logcat -d -v threadtime --pid <original server PID> -t 1000 appium:I *:S
```

它不 shell 刷新身份、不请求新 session、不调用 HTTP，也不改变 phase/DELETE/cleanup 事实。
失败同样消耗一次机会并保存原 read receipt；没有重复摘要文件或日志 verdict。
runner 在 DELETE 前独立尝试该读取和原 App PID 取证，一项失败不能跳过另一项或其他 cleanup。
后续静态复核发现极短 cleanup budget 下可选读取可能耗尽必要清理时间，已先用真实推进 fake clock
的 timeout 回归确认 red，再让两项取证共享至多一半 cleanup 时间的绝对子 deadline，为 DELETE/
helper/tag 恢复保留请求预算。未增加总预算；guard/SDK timeout 仍不等于可硬中断所有本地 I/O。
该预算收紧发生在下述 run 之后，仅有对应软件回归，不继承该 run 为收紧版本的真机通过。
verifier 对可选 server-log receipt 检查同 run bound server 的 serial/PID/固定 tag/命令形状；
failed bundle 的 selector 冲突也拒绝，重封 manifest 不能绕过，历史没有该文件仍可验证。

本地 driver 8.7.3 与已安装 server 10.7.6 APK 字节码表明：常规截图经 `jwproxy.command('/screenshot',
'GET')`；host 的 `Proxying` 日志在下游 request 前，不能单独证明设备已收到。设备正常有
`channel read: GET …/screenshot` 与 `CaptureScreenshot command` 入口日志。该版本设备截图可能走
`UiAutomation.executeShellCommand` 的 screencap/display 查询、FD 读取，也可能走 Bitmap 截图，
不能预先将阻塞点限定为 `UiAutomation.takeScreenshot()`。未修改这些第三方路径。

Debug 可包含 UI 文本、capabilities 和 Base64/payload 前缀，**不声称自动脱敏**；只留 ignored 本地
证据，不上传。原 `skipLogcatCapture=true` 保持，设备日志由本轮 concrete owner 单次读取。

### 同 APK 真机诊断

沿用第 7 节 APK、helper hashes 和 180/20/90 秒预算：

```text
run_id=9707b82f-8fbd-4a64-97c6-aad964d8086f
boot_id=35df0f9f-6c54-42b0-800c-3c410469b6e7
app_pid=22557; server_pid=23159
device_session_id=73e8798e-c635-4fb2-b442-95abc0d47c99
observations=[idle, started]
status=failed; host_gate_status=failed; phone_status=quarantined; rf=not_run
```

Start 正控再次全部返回。截图只发 **1 次**，约 **5.040 秒**超时，Close click **0 次**、rawfiles 为空：

| 边界 | 原始事实 |
|---|---|
| host `automation/appium.log` | 06:37:42.574 收到 screenshot、进入 getScreenshot、记录向原下游 session 的 Proxying；没有对应 Got response |
| device `platform/037-read.json` | 原 server PID logcat 成功，78,222 bytes / 615 lines；包括同一下游 session 的成功请求/响应，最后一条为 14:37:40.232 的 enabled 响应，未见截图入口 |
| 原 App PID 取证 | Start/Rust/Promise 正控有效，没有 Close phases；本轮没有提交 Close |

host 与 device 日志显示的小时数相差 8，不能把两套文本时钟当作同一 monotonic clock。
日志缺少截图入口意味着 **当前证据没有确认设备 handler 执行**，不等同于已证明请求未到达；
保留日志环形缓冲、生成与采集可见性的限制。host 尝试 proxy 也不证明 socket 写出或响应已生成。
原生/JS/Rust Close 不在本轮失败执行链中，未据此更改 Rust/UniFFI/native 语义。

### 只读 transport 差分与恢复

runner 退出后 shell 的 boot、线程表、forward 清单曾成功返回：分别约 0.055 / 0.296 / 0.014 秒，
原 server 线程仍在（单帧 sleeping 状态，不是 Java 阻塞栈）。原 serial 的唯一 forward 仍为
`tcp:8237 → tcp:6790`。在同 lease、原 bound receipt 固定且匹配该 forward 后，一次只读 `/status`
直读约 **2.003 秒** timeout；没有重发 screenshot、点击、新 session 或修改 forwarding。

紧接着原身份 recovery 首条 boot shell 也 timeout，没有发出 mutation，失败目录和输出保留。
这只是事件顺序，**不能将 shell 再次阻塞断言为该 status 请求导致**。后续另一 fresh boot 检查恢复，
才按原 boot/PID/UID/start ticks/hash 完成 exact instrumentation/App retirement、helper/driver/tag
恢复与读回并归档 exact lock。原缺失 DELETE 不补写；graceful Close/native resource-zero 不声称。

```text
.work/formal-android-managed-lifecycle/9707b82f-8fbd-4a64-97c6-aad964d8086f/
.work/formal-uiautomator-device.log
.work/formal-uiautomator-wire-summary.json
.work/formal-uiautomator-transport-evidence.json
.work/formal-uiautomator-retirement.log
.work/formal-uiautomator-retirement-recovered-shell.log
.work/formal-android-managed-recovery-7c0d2a39-bacc-4c4f-aaec-1ad8c42e7db0/
```

原 bundle offline integrity passed，verdict 仍 failed/quarantined；独立恢复为
`restored / app_stopped / helper_restored / diagnostic_tag_restored / lease_archived=true`。
当前无活动 phone lease。没有 DUT BLE permission、radio、firmware 或 RF 操作。

### 当前软件验证

先 red 确认缺失 debug plumbing、server-log API/selector 校验，再最小接入；新增测试均进入原有维护模块：

| 实际命令（共同前缀 `pixi run just --command python -m unittest`） | 结果 |
|---|---|
| `tools.tests.react_native.test_mobile_appium -v` | 35 passed / 0.598s |
| `tools.tests.react_native.test_bleng_android_session tools.tests.react_native.test_bleng_lifecycle_evidence`（此组使用 `pixi run python -m unittest`） | 77 passed / 43.471s |
| `tools.tests.react_native.test_bleng_automation` + 五个 lifecycle default/opt-in/server-log timeout/native-log failure/tag-restore failure 用例，`-v` | 28 passed / 83.252s |
| `tools.tests.react_native.test_android_appium tools.tests.react_native.test_bleng_ui tools.tests.react_native.test_bleng_scan_ui tools.tests.react_native.test_bleng_android_close_diagnostics tools.tests.react_native.test_run_bleng_android_lifecycle -v` | 142 passed / 6.438s |
| lifecycle `test_server_log_timeout_is_not_retried_or_allowed_to_skip_other_cleanup` / `test_opt_in_diagnostics_capture_and_restore_before_phone_release -v` | cleanup_seconds=1 的耗时预算先 red，收紧后两项 green，见 budget logs |
| `pixi run just --no-deps react-native formal-host-check` / `pixi run just react-native formal-host-test` | typecheck/autolinking / 52 Jest passed |
| `pixi run just react-native formal-host-lifecycle-verify .work/formal-android-managed-lifecycle/9707b82f-8fbd-4a64-97c6-aad964d8086f` | integrity passed；仍 failed/quarantined |

日志 `.work/formal-uiautomator-{mobile-test,composition-current,neighbor-tests,typecheck,jest,offline-replay}.log`
及 `.work/formal-uiautomator-server-{red,green}.log`、`.work/formal-uiautomator-budget-{red,green}.log`。
最终 fmt/lint、docs-check-mermaid、diff --check 均通过，输出
`.work/formal-uiautomator-final-{fmt,lint}.log`、`.work/formal-uiautomator-docs.log` 与
`.work/formal-uiautomator-diff-check.log`。首轮完整三个 module 的组合在外层 180 秒 timeout，
未获得完成结果；已检查无残留测试进程，拆分逐项输出后只验证上述有结果的范围，不声称完整
`test_bleng_android_lifecycle` 或全 tools-test 通过。拆分中修正两处新测试自身的 fake 假设：generic
attach caps 与 concrete prepared caps 不同；失败 SDK dispatch 要从实际 Mock 调用记录计数，不能
从仅记录成功边界的 harness trace 推断次数。未修改 live caps 或用全局成功 mock 掩盖失败。

## 9. Host ADB provenance：SDK client 实际连接了 QtScrcpy 的旧 server

后续未重跑 Appium 场景，而是只读检查 host daemon。发现 **固定 SDK client 路径并不证明
5037 上的共享 server 由同一 SDK 启动**：

| 观察 | 实际结果 |
|---|---|
| SDK client `adb version` | 37.0.1-15733141，`/Users/vision/Library/Android/sdk/platform-tools/adb` |
| 5037 listener | PID 2348，启动时间 2026-10-29 17:27:32，当前仍存活 |
| PID 2348 的 mapped executable | `lsof -a -p 2348 -d txt` 指向 `/Applications/QtScrcpy.app/Contents/MacOS/adb` |
| 该 binary 的 `adb version` | 33.0.2-8557947；与 server log 中 PID 2348 的启动版本/路径一致 |
| SDK client `adb server-status` | `error: unknown host service`；不是此命令成功证明 SDK server 在运行 |
| 其他 host owner | QtScrcpy PID 1795 仍在运行，且有其子进程 ADB；共享 daemon 有现存连接 |

两个 binary 均报告协议标识 `Android Debug Bridge version 1.0.41`。版本/来源不同是已确认的
环境事实，**不能仅凭此断定它导致超时或协议不兼容**；先前成功/失败 run 也不能据当前观察补写
历史 daemon provenance。当前发现使旧 host server 与外部 mirroring/control 的干扰成为值得隔离的
候选因素，不是 Rust、设备 UiAutomator2 或 ADB 任一方的已确认根因。

只读证据已保存：

```text
.work/formal-adb-host-provenance.json
.work/formal-adb-client-version.log
.work/formal-adb-running-binary-version.log
.work/formal-adb-server-status.log
.work/formal-adb-server-executable.log
.work/formal-adb-server-open-files.log
.work/formal-adb-server-listener.log
.work/formal-adb-host-features.log
.work/formal-adb-devices.log
.work/formal-adb-server-log-snapshot.log
```

provenance JSON 记录两份实际 binary 的 SHA256、版本、mapped path、process start 与 `mutations=[]`。
server log 约 9.8 MiB，仅保留有界开头/结尾，未将此快照当成完整日志或原 sealed bundle 的一部分。
日志及本机路径仅留 ignored 本地证据，不上传。

本步没有 acquire phone lease、安装、Appium session、daemon restart、USB reconnect、App 退出或
任何 DUT BLE/radio/firmware/RF 操作；当前无活动 phone lease。PID 2348/QtScrcpy 并非本轮拥有，
**没有擅自 kill-server 或关闭它们**。下一次隔离对照需用户明确允许关闭 QtScrcpy 的持续 ADB 客户端、
重启共享 host daemon 为 SDK 版本，并确认实际 listener/executable；这会中断现有 ADB/投屏/控制连接。
不安装/替换 SDK，不因消除外部因素而重标先前失败，也不承诺该对照会通过。

本步只改证据文档与安全说明，没有修改产品或工具执行逻辑；校验日志
`.work/formal-adb-boundary-{fmt,lint,docs,diff-check}.log`。

## 10. 经授权退出 QtScrcpy、切换 SDK server：同 APK 对照仍失败

用户随后明确批准第 9 节的退出/daemon 切换及同 APK non-RF 对照范围。操作前确认无活动 phone
lease，并 fresh 核对 QtScrcpy 主进程/子 ADB 与原 daemon 的 start time/路径。AppleScript 正常退出
请求先返回 `User canceled (-128)`，子 ADB 已退出；准备 TERM 前再次检查时主进程也已消失，
因此身份 guard 拒绝发送信号，**实际没有发送 TERM 或 KILL 给 QtScrcpy**。之后确认所有 QtScrcpy
进程均退出，再由 SDK client 单次 kill-server / start-server，未安装或替换任何工具。

新 daemon **PID 93206**，`lsof` mapped executable 和成功的 `adb server-status` 均为 SDK
`/Users/vision/Library/Android/sdk/platform-tools/adb`，版本 **37.0.1 / build 15733141**；
USB backend 为 NATIVE。运行前后 `server-status` 均保留，手机仍为同一 boot 且 online，未重插 USB。

```text
.work/formal-adb-sdk-switch-before.json
.work/formal-adb-sdk-switch-qt-quit.log
.work/formal-adb-sdk-switch-daemon-intent.json
.work/formal-adb-sdk-switch-stop.log
.work/formal-adb-sdk-switch-start.log
.work/formal-adb-sdk-switch-after.json
.work/formal-adb-sdk-switch-server-status-after-run.log
```

使用相同 43,358,253-byte APK，SHA256
`9d2a8b1e85a2c9528b2de1bbba67eab7a78f3e75b80410d01347e694453f0cdc`，相同 helper hashes、
180/20/90 秒预算和显式诊断 scope；包含第 8 节随后收紧的 diagnostic 子预算，没有重新构建 App：

```text
run_id=cff6c19f-82cf-4d9a-b49c-4c490e1b14ef
boot_id=35df0f9f-6c54-42b0-800c-3c410469b6e7
app_pid=20785; server_pid=21495
observations=[idle, started]
status=failed; host_gate_status=failed; phone_status=quarantined; rf=not_run
```

本轮仍在 Close 前的 screenshot 读取超时：约 **5.045 秒**，host GET/proxy attempt 均 **1 次**，
Close click **0 次**，rawfiles 为空。host 记录了向原下游 session 的截图 proxy 尝试，设备原 server
PID 日志仍停在前一个成功的 enabled 响应，未见截图 handler 入口。保留第 8 节的日志可见性限制，
不据此断言请求已写出或设备没有收到。

**QtScrcpy 退出且 SDK server 已实际接管后，故障仍可出现；旧 daemon/QtScrcpy 不是足以解释本次
失败的唯一条件。** 这不证明它们对历史故障毫无影响，也不证明问题属于 Rust、UiAutomator2 或 USB
的某个具体层；没有据此重标历史失败或启动第二次同条件对照。

### 清理与当前 host 状态

本轮与先前不同的已记录事实：`candidate/finalization.json` 为 passed/force-stop，原 App 已成功
终止；`platform/close-diagnostics/restored.json` 为原空值/verified=true，日志开关已恢复。
DELETE 与 owning server 的正常收尾仍 timeout，helper restoration 因未知/未关闭 session 被拒绝，
phone 正确保留 quarantine，未把其余成功清理合并成 session 成功。

随后同 boot shell 可用，独立恢复先固定原 finalization/tag receipts，确认 original App 已停且
installed path/hash 未变；不再尝试把旧 App PID 当作活进程或重复 force-stop。仅 fresh 核对并强制
退休本轮 instrumentation，精确恢复 helper/driver 配置，复验 tag 原值并归档 exact quarantine lock。
没有补写 device/host DELETE、graceful Close 或 native resource-zero。

```text
.work/formal-android-managed-lifecycle/cff6c19f-82cf-4d9a-b49c-4c490e1b14ef/
.work/formal-adb-sdk-switch-device.log
.work/formal-adb-sdk-switch-wire-summary.json
.work/formal-adb-sdk-switch-retirement.log
.work/formal-android-managed-recovery-a9a40fb4-d657-49dc-8d2b-2b047d25a2f6/
```

恢复为 `restored / app_stopped / helper_restored / diagnostic_tag_restored / lease_archived=true`。
原 bundle offline integrity passed，原 verdict 仍 failed/quarantined，输出
`.work/formal-adb-sdk-switch-offline-replay.log`。当前 **QtScrcpy 已退出，SDK daemon 保持运行，无活动
phone lease**；不自动重开 QtScrcpy 或恢复旧 daemon。不改 DUT BLE 权限、radio、firmware，未执行 RF。

本步没有修改产品或工具执行逻辑；fmt/lint、docs-check-mermaid 与 diff --check 的输出为
`.work/formal-adb-sdk-switch-{fmt,lint,docs,diff-check}.log`，未重新运行 native/Jest/Python suite。

## 11. 2026-11-06：socket 日志、DELETE 前 fresh status 与只读 ADB 对照

### 失败并非截图专属，也不是本次 Agent 等待容量

未重启 SDK daemon、未修改第三方或 native/Rust；只为一次受控运行增加 `NODE_DEBUG=http,net`，
原 Appium 子进程继承该设置并将输出留在 ignored log。先尝试仅限 lo0/8237 的被动抓包，BPF 被系统
拒绝，保留 `.work/formal-screenshot-bpf-permission.log`，**没有 sudo 或绕过授权**。
Node debug 可能含 UI/HTTP 内容，未向外部服务发送这些日志。

同 APK/预算的 `2900c168-2039-4eac-9573-43967b670575` 中，截图 **200 / 511ms**、XML、first rect、
window size 均成功；相同元素的 second rect 才超时，仍未点击 Close。失败 proxy 后已有
`have free socket`、`outgoing message end.`，没有 `wait for socket` 或相应 incoming response。
这排除了 **该次请求等待 Agent 容量**，但不把 outgoing end 当作 TCP/设备接收回执。
TERM 后的 `READ on FREE socket - destroying poisoned socket` 不能认定为根因：本机 Node
`v26.8.1` 的这一日志条件也包含 EOF/读错误，而且发生在基础设施已开始退休之后。

实际 proxy 依赖解析来自现有 npx Appium 的 base-driver/Axios，而不一定是 driver 邻近的同名包。
这是本地源码验证的路径差异，不是据版本号判故障。没有替换 Node/npm 包或调整池配置。

该轮 App finalizer/tag restore 均成功，session/helper 未正常闭合；后来按原 run receipt 精确退休
instrumentation、恢复 helper/driver、归档租约，原 failed verdict 保留：

```text
.work/formal-android-managed-lifecycle/2900c168-2039-4eac-9573-43967b670575/
.work/formal-screenshot-node-debug-device.log
.work/formal-screenshot-node-retirement.log
.work/formal-android-managed-recovery-900dfd90-3aeb-48df-b6b5-b824725173d1/
```

### 最小接线：失败后、DELETE 前一次 fresh/direct status

新增 [`AndroidAutomationOwner.probe_server_status`](../../../tools/react_native/bleng_android_session.py)，
只在未 finalized 的原 bound owner/selector/receipt 守卫下，一次性调用现有
`read_loopback_json('http://127.0.0.1:<original system port>', '/status', …)`：

- 新 TCP socket、`Connection: close`、不走环境代理或 Appium 4737、不重试、不刷新身份。
- 最多 2 秒，受原 scenario deadline 和原 half-cleanup 诊断子预算共同约束，不增加总预算。
- 固定原 server/session/port 与起止时间，在 `platform/server-status.json` 保存 response/error；
  不从 ready 推导关闭或 gate 成功，不改变 owner phase/retirement 事实。
- runner 仅在 **显式 diagnostics、session 已绑定且 UI 有 primary error** 时，于 DELETE 前调用；
  probe 失败不覆盖 UI primary，也不跳过其他 cleanup。成功 UI 不额外探测。
- verifier 对可选 receipt 的 endpoint/port、server 与 host/device session 做同原绑定/准备回执的
  一致性校验，失败 bundle 也不能重封绕过；历史无该 receipt 仍允许。

复核还发现 HTTP 中断可能被 post-read guard 或 publication 普通异常覆盖，已另做 test-first
修复：`KeyboardInterrupt`/其他非 Exception 中断保持原对象传播至 runner，普通 ownership 错误
仍优先于普通 HTTP 错误，所有 note 含明确类型；不为诊断吞掉 caller 中断。

### 真机分流结果：新连接也不可用，且发生在 DELETE 之前

同 APK/预算、同 SDK daemon、仍带 Node debug 的新 run：

```text
run_id=0793a5ed-9bca-4791-825c-d5d609f88cb3
app_pid=22384; server_pid=23050
observations=[idle, started]
status=failed; host_gate_status=failed; phone_status=quarantined; rf=not_run
```

故障比前一轮更早：Current run 导航 click 已返回 200，随后定位 `session-state` 的普通请求在
20 秒上游预算内未返回；没有进入 Close，也没有执行 pre-close 截图。**第一次 DELETE 之前**，
原 8237 端口上的 fresh `/status` 约 **2.010 秒** timeout。该事实不再受“DELETE/host kill 已改变
server 状态”的混淆，故不能只用旧 pooled socket 解释此次所有现象。

紧接着两份 logcat、App finalizer 与 tag readback 的 ADB 命令也 timeout；host `get-state` 仍为
`device`，不能据此断言设备命令健康。对实际 SDK daemon 的一次无需提权 `sample` 成功：main 为
poll、USB read 为 IOKit ReadPipe、writer 为条件等待。**单帧采样不能证明 host deadlock 或定位 USB
哪一端有错**；没有据此杀 daemon、切换 USB backend、root 调试系统进程或重启设备。

之后 fresh boot shell 自行恢复，才按原 App/instrumentation identity 完成强制 retirement、精确
helper/driver/tag 恢复及 exact lock 归档。缺失 DELETE 和原 failed/quarantined 不补写成功：

```text
.work/formal-android-managed-lifecycle/0793a5ed-9bca-4791-825c-d5d609f88cb3/
.work/formal-fresh-status-device.log
.work/formal-fresh-status-adb-sample.txt
.work/formal-fresh-status-adb-cached-state.log
.work/formal-fresh-status-retirement.log
.work/formal-android-managed-recovery-aed1bac5-5557-4d12-83ef-dd1f73c7c421/
```

### 独立 ADB-only 控制：不启动自动化、UI 或 Rust owner

恢复后，在新 physical phone lease 中做一次最长 25 秒的有界只读命令对照：开始/结束核对同 boot，
开始确认目标 App/helper 无进程，串行执行 **200 次 `adb shell true`**；每条至多 2 秒，任何失败即
停止，不循环重试、不改变 settings。**200/200 成功、cleanup_errors=[]、phone_status=passed**，
结束 fresh boot 检查后释放租约。它是短期定位控制，不是产品 stress/长稳 gate，也没有增加框架。

```text
run_id=caf85302-3df5-473c-a758-321954d21f10
scope=bounded-adb-only-read-control
appium=not_started; rust_owner=not_created; ui=not_launched; rf=not_run
.work/formal-adb-readonly-repro.log
.work/formal-adb-readonly-repro-caf85302-3df5-473c-a758-321954d21f10/
```

这证明本次无自动化活动的 ADB-only 样本可完成，不证明任意负载/时刻的 ADB 都健康，也尚未将
UiAutomator2 与 Rust Start 分离。当前只能将解释收敛为 **自动化会话活动期间，新旧 HTTP 连接与
设备命令可能一起停顿**，不是已证的截图函数或 Agent 容量单点故障。

### 软件与证据验证

- 新 owner/verifier 接线先 red 后 green；对应两个完整模块最初 **86 passed**。
- 当前 owner/verifier、mobile Appium、Android SDK、UI/scan/诊断/CLI 的组合 **263 passed / 59.926s**，
  日志 `.work/formal-fresh-status-combined-tests.log`；该组合先于随后中断优先级修复。
- 修复中断优先级后完整 `test_bleng_android_session` **43 passed / 54.595s**，red/green 见
  `.work/formal-fresh-status-interrupt-{red,green}.log`。
- 两个新增 runner 分流/短清理预算和两个既有诊断成功/timeout 用例 **4 passed / 59.381s**，
  `.work/formal-fresh-status-composition-{red,green}.log`。超时 fake 推进真实模拟预算，不是瞬时抛错。
- `pixi run just --no-deps react-native formal-host-check`、`pixi run just react-native formal-host-test`
  通过，Jest **52 passed / 2 suites**；`.work/formal-fresh-status-{typecheck,jest}.log`。
- 两个 failed bundle 均通过 `formal-host-lifecycle-verify`，仍 failed/quarantined；日志
  `.work/formal-screenshot-node-offline-replay.log`、`.work/formal-fresh-status-offline-replay.log`。
- 当前 fmt/lint、docs-check-mermaid、diff --check 输出
  `.work/formal-fresh-status-final-{fmt,lint,docs,diff-check}.log`；未运行 full tools-test、iOS 或 RF。

两轮独立恢复均为 `restored / app_stopped / helper_restored / diagnostic_tag_restored /
lease_archived=true`。当前无活动 phone lease，QtScrcpy 仍关闭，SDK daemon 保持运行。
未改产品 Rust/Swift/Kotlin、BLE 权限、radio 或 firmware；原 raw PNG/XML/debug logs 仅保留本地。

## 12. 2026-11-06：idle UI control 真机通过，Start 后的故障仍待定位

为分开自动化活动与会话启动，复用 maintained managed entry，新增显式
`--automation-idle-control --close-diagnostics`；没有另建 universal runner、设备 registry 或第二 App。
flag 类型/组合在任何设备命令前校验。所有 APK/helper review、phone lease、OS/session identity、
绝对预算、DELETE/恢复/归档/保留成功页面仍由原 owners 负责。

共享 UI 模块新增 `exercise_idle_control()`：验证 Tests/idle，两轮固定
Tests → Evidence → Current run → Tests，最终停 Evidence/idle。只调用既有 guarded 导航操作，
不点击 Start/Close/scan、不读取初始化快照，也不触发 pre-Close 截图 hook。
App 源码仅在显式 Start 路径调用 `createBlengSessionOwner()`；该对照绕开这个创建/启动路径，
不是用 mock 替代 native 结果。

证据 **单独使用 `formal-android-managed-idle-control` scope**，成功 trace 精确为 `["idle"]`、
snapshot 为 `{}`。verifier 不接受其带上 started/closed 或初始化快照，也不能只改 scope 冒充
lifecycle；prepared/final scope 仍必须一致。CLI 的独立 key 为
`formal_android_managed_idle_control`，并核对 replayed scope 等于请求场景；修正了初版仍使用
lifecycle summary key 的复核问题。旧 lifecycle scope、完整 trace 和 readiness 检查没有放宽。

### 真实运行与清理

```text
run_id=d0833fa7-a346-44f4-aa5b-09f4d2f9d2e1
scope=formal-android-managed-idle-control
APK SHA256=9d2a8b1e85a2c9528b2de1bbba67eab7a78f3e75b80410d01347e694453f0cdc
boot_id=35df0f9f-6c54-42b0-800c-3c410469b6e7
app_pid=28314
observations=[idle]; startup_snapshot={}
scenario_status=passed; session_status=passed; phone_status=passed
status=passed; host_gate_status=pending; rf=not_run
cleanup_errors=[]; phone_cleanup_errors=[]
```

复用同一手机、SDK ADB daemon、UiAutomator2/helper hashes 与 180/20/90 秒预算，带原 Node debug。
wire 摘要确认 **7 次点击全部为三个 workbench 导航按钮**，Start/Close/scan 点击为 0。原 App PID
的 gated native phase 日志为空；空日志不单独充当证明，场景白名单、App lazy-create 源码和原始
wire 一起限定本次实际路径。

host/device session DELETE、local server reap、concrete platform finalizer、helper AppOp/store
恢复、tag 恢复及 phone release 均通过。App finalizer 为 retain，保留 Evidence/idle 页面。
这不是 Started owner lifecycle、native resource-zero、Hermes reload、scan/RF 或 ABI gate pass。

```text
.work/formal-android-managed-lifecycle/d0833fa7-a346-44f4-aa5b-09f4d2f9d2e1/
.work/formal-idle-control-device.log
.work/formal-idle-control-wire-summary.json
.work/formal-idle-control-offline-replay.log
.work/formal-idle-control-server-status.log
```

offline integrity passed，保留 idle-control/pending/not_run。当前无活动 phone lease；QtScrcpy
仍关闭，SDK daemon 保持运行，保留本轮身份明确的成功 App 页面，未自动停止它。

### 证据现在能与不能区分的内容

- ADB-only 控制通过；现在 **UiAutomator2 + RN idle 导航 + 完整 cleanup 也有真实通过样本**。
- 与此前 Started 路径失败构成了有价值的差分，但只是一轮 control，不证明底层自动化/USB 永远
  健康，也不能把 Start 增量中的 Rust、Kotlin/backend 或 JS/Fabric 某一层直接判为根因。
- Start 同时引入 native owner/runtime 工作、初始化证据与不同页面树。App/scan UI 源码未发现
  ownerReady 变化会自动发 scan 或启动轮询 effect；这仍不是完整运行时排除证明。
- 对 current-tree Android session 的只读审查另发现两项独立生命周期缺口：长期 reader 将正常
  300 秒 `nextEvent` timeout 当成退出性错误，正常 assembled retirement 未调用专属
  `HandlerThread` 的退出路径。见
  [`AndroidPlatformSession.kt`](../../../integrations/react-native-bleng/android/src/main/kotlin/com/blehub/reactnative/bleng/AndroidPlatformSession.kt)。
  它们需后续针对性 red→green 及 native 验证；本轮未修，也不拿它们解释不足 300 秒、尚未 Close
  的首次 Start 故障。没有为诊断先调整 Rust worker 数或削弱 native watchdog/retirement。

### 本轮实际验证

- 新 scene、runner/scope/verifier/CLI 的缺失先 red，再最小接线；
  `.work/formal-idle-control-ui-{red,green}.log`、`.work/formal-idle-control-composition-{red,green}.log`。
- 新 idle success/failure、原 lifecycle success、完整 evidence/CLI 组合 **58 passed / 34.000s**，
  在随后 CLI summary/scope 复核修正之前；修正后完整 CLI **10 passed / 0.048s**。
- 当前 UI/scan UI、完整 evidence 与 CLI 邻近组合 **138 passed / 4.376s**，
  `.work/formal-idle-control-neighbor-tests.log`；输入/授权校验另有定向输出
  `.work/formal-idle-control-input-validation.log`。
- `formal-host-check` 与 `formal-host-test` 通过，**52 Jest passed / 2 suites**，
  `.work/formal-idle-control-{typecheck,jest}.log`；本轮无产品/native 源码变更，没有重建 App。
- 全 tools-test 曾在外层 120 秒达到 deadline、没有最终汇总，已确认无残留进程，**未计为通过**。
  fmt/lint、docs-check-mermaid、diff --check 输出
  `.work/formal-idle-control-final-{fmt,lint,docs,diff-check}.log`。

## 13. 2026-11-06：Start 后不切页控制通过，取得完整 Close 正控

离线先核查当前 release 的真实依赖，而不是从缓存目录猜版本：
`android/app/build/outputs/sdk-dependencies/release/sdkDependencies.txt` 和 release lint package
模型均记录 `androidx.collection:collection-jvm:1.4.2`。生成 Nitro Kotlin 的四个
`CxxPart.initHybrid()` 仍有 `@FastNative`，release 编译 jar 的 `javap` 也确认注解；不能把它
推广为所有业务调用。App/autolinking/依赖源码未找到第二个 JSI runtime 的实际创建引用，库支持
跨 runtime 不等于 App 使用它，因此 Nitro 多 runtime issue 不构成已证实的前提。
本地核查详情：`.work/2026-11-06-offline-android-collection-nitro-audit.md`。

新增显式 `--automation-start-hold-control --close-diagnostics`，与 idle control 互斥，复用原
owners 和 `exercise_lifecycle`，没有另建 App/runner 框架。仅执行 idle → Start 一次 → started →
一次初始化快照 → Current run 两次 bounded 读取，中间静置五秒 → 同页 Close → closed；不点击
导航/scan。独立 scope 为 `formal-android-managed-start-hold-control`，CLI key 为
`formal_android_managed_start_hold_control`；sealed `automation/start-hold-control.json` 绑定
scope/quiet window，verifier 拒绝缺失/不完整窗口或改标成 lifecycle navigation。

首个 attempt `b2615393…` 在启动前因上一轮 retained idle App 仍运行被拒绝，observations=[]、
scenario/session=not_run，正常恢复 helper/tag 并释放 lease；没有接管旧 App。随后在独立 App-only
lease 中 fresh 核对 `d0833fa7…` 的原 boot/PID/start ticks/UID/install hash，才退出该已知 App，
不把 force-stop 当作 graceful close/resource-zero。

同原 APK `9d2a8b1e…` 的真实控制：

```text
run_id=8cb21309-6953-414e-b0b4-173414add55a
scope=formal-android-managed-start-hold-control
boot_id=35df0f9f-6c54-42b0-800c-3c410469b6e7; app_pid=11695
observations=[idle, started, closed]
startup_snapshot=poweredOn / scan denied / connection denied
quiet-window observed span=5.896946125s
scenario_status=passed; session_status=passed; phone_status=passed
cleanup_errors=[]; phone_cleanup_errors=[]
status=passed; host_gate_status=pending; rf=not_run
```

wire 确认仅两次点击：Start 和 Close，没有导航/scan。pre-Close PNG/XML、bounds/window 读取通过；
原 App 日志取得完整 Start 和 JS Close → Hybrid → Rust closeResult → reader drain/resource
snapshot → Promise resolve → JS receipt/finally 链。两个时钟域不互减；native 自身日志中
`rust.closeResult.enter` 至 `return` 约 14.6ms。这是该样本的成功返回，不证明所有 native thread
已退出，亦未覆盖先前单独发现的 HandlerThread 退休缺口。

成功保留 Current run/closed；session DELETE、local server、helper/tag/phone 收尾均通过。
完整本地证据：

```text
.work/formal-android-managed-lifecycle/8cb21309-6953-414e-b0b4-173414add55a/
.work/start-hold-device-retry.log
.work/start-hold-retained-idle-retirement.log
```

本次证明“不切页且短时静止”的 Started owner 可正常完成查询、取证与 Close；它与旧 Started
浏览失败形成差分，但一轮通过不证明 Rust/native/USB 永远健康，也不将 UI 树确定为根因。

## 14. 2026-11-06：AndroidX 1.4.4 修正未消除完整浏览停顿

RN 0.87.1 的 [issue #58388](https://github.com/react/react-native/issues/58388) 与
[upstream fix #58389](https://github.com/react/react-native/pull/58389) 描述 Fabric
`MutableIntObjectMap` 在 collection 1.4.0–1.4.2 中丢失条目，并将依赖调整到 1.4.4。
当前已观测的 `MissingViewState` 与真实打包 1.4.2 满足适用性线索，但该资料不证明 ADB
shell/fresh UiAutomator2 status 同时停顿的原因。

仅在正式 consumer `android/app/build.gradle` 增加 `collection:1.4.4`，附原因；不改本机第三方
源码、不升级 RN/Nitro/Appium，不对 BLE library 做 global force。通过 managed release build
（约 60–80 秒）；`releaseRuntimeClasspath` dependencyInsight 和 release metadata 确认
`collection`/`collection-jvm` 均为 1.4.4。新 APK 43,374,637 bytes，SHA256：

```text
51711779e5e9efd60105d1f312b90e65df0d85f7f8996c1d70e200485e6d9fef
```

fresh 核对上一轮 retained App 原身份并退出后，新候选执行原完整 lifecycle，预算和自动化参数
未变，无 BLE permission/radio/firmware/RF：

```text
run_id=92b093b1-0d2f-472f-a47e-21ab483cb171
scope=formal-android-managed-lifecycle
app_pid=16565; server_pid=17342
observations=[idle, started]
startup_snapshot=poweredOn / scan denied / connection denied
status=failed; host_gate_status=failed; phone_status=quarantined
```

Start/初始化快照及 Started 下 Tests、Evidence 查询均已成功。最后一次 click 是
`workbench-current`，于 01:47:34.782Z 返回 200；01:47:37.119Z 的 foreground query 仍成功。
紧接着 01:47:37.150Z 的 `session-state` findElement 超时，尚未进入 Close 或 pre-Close capture。
DELETE 前 fresh/direct status 约 2.011 秒 timeout，随后日志/shell/DELETE/helper/tag 清理也超时。
因此 **1.4.4 未解决整体停顿**，不得把依赖修正、Start-hold pass 或上游 issue 升格成根因证明，
也不能据此确认该 run 已消除所有 ViewState 异常。

之后 shell 自行恢复，首次 recovery 的只读前置核查发现原 `8237→6790` forward 仍保留；因与
旧 recovery 假定的空表不符而停止，未发出 mutation。随后以原 bound session/server identity
确认该 exact forward，才删除该映射、强制退休原 instrumentation/App、恢复 helper store/AppOp、
hidden-policy/IME 和原空值 log tag，并复验后归档 exact quarantine lock。没有重启 daemon、
USB reset、手机重启或 radio 操作。原 failed bundle 与缺失 DELETE 不改写，未声称 graceful
BLeng Close 或 native resource-zero。当前无活动 phone lease，失败 App 已退出。

```text
.work/collection144-{release-build,dependency-insight,lifecycle-device}.log
.work/formal-android-managed-lifecycle/92b093b1-0d2f-472f-a47e-21ab483cb171/
.work/formal-android-managed-recovery-829d2976-89a0-4518-9826-7940adb01f5a/
.work/formal-android-managed-recovery-ff2afec3-82c7-4172-beb5-5682559a6001/
.work/collection144-original-session-restored.log
```

### 实际软件验证与未完成项

- UI/scan UI/evidence/CLI 邻近组合：145 passed / 4.741s。
- managed runner 模块：36 passed / 275.154s；随后有实际 I/O 拒绝护栏的定向复核：
  13 passed / 64.478s、`REAL_IO_VIOLATIONS=[]`。周期采样主要落在本地 receipt hashing/authority
  链，不是设备/网络等待证据，也不足以断言完整 discovery 的慢因。
- `formal-host-check`、`formal-host-test` 通过：52 Jest passed / 2 suites。
- `formal-host-android-build release`、explicit release dependencyInsight 通过。
- canonical `pixi run just react-native tools-test` 达到 240 秒外层 deadline，没有汇总，
  **不计通过**；结束后无该测试进程残留，不盲目继续延长。
- 日志：`.work/start-hold-{ui-evidence-tests,managed-tests,targeted-recheck,full-tools-test}.log`。

## 15. 2026-11-06：前置 live logcat 证明旧 stream 并非立即完全停传

只读联合审查确认 App 导航只 `setPage`，owner/scan controller 位于页面条件分支上方；切页不
调用 Start/Close/scan，也没有 mount effect 发出 BLE 命令。但 Current run 的 snapshot、按钮、
ManualScan/TextInput 确实卸载/重挂载，因此仍有 Fabric/native UI 与 accessibility 工作。
现有每次 `text` 均重新 UiSelector find，再读 displayed/text。仅缓存 WebDriver element 也不保证
绕过底层 accessibility 查询，不能当作独立 transport 活性证明。

原诊断仅在故障后开新 ADB stream 抓日志，新 stream 不可用时证据也丢失。为补这个缺口，仅在
既有 `--close-diagnostics` 中新增 `start_live`/`stop_live`，不增加另一套 runner/CLI 场景：

- 原 bound App identity/receipt 固定后、任何 UI Start 前，打开一条 tag 白名单 logcat stream。
- `main,system,crash,events` buffers、`-T 1`、固定 Appium/RN/system tags、`*:S`；不清 buffer、
  不启用 Appium 内部 capture，也不增加设备 settings mutation。白名单并不保证覆盖所有变体 tag。
- 原 scenario deadline 与 300 秒取较早者，raw 最多 8 MiB，保留首个 arrival 及至多 64 个最近的
  byte-offset/host-monotonic arrival 记录。stdout/stderr 合并保存，不将 stderr bytes 当作设备活性。
- failure fresh-status probe 后、DELETE 前停止。非阻塞 drain 与有限 wait/join，只回收保存的本次
  host `Popen`，不等继承 pipe EOF，不向 App、daemon 或其他进程组发信号。
- 不论是否成功绑定/启动，都走 local stop；未确认停止阻止 release。Popen 拒绝但确认无 child、
  原 tag 已恢复的失败 run 不因单纯诊断失败被误报为 property restoration uncertain。
- original-PID 事后日志与 live stream 的 offline selector/raw/identity/bounds 校验独立，不把
  live intent 当作 tag mutation intent；re-sealed 错误 tag、raw hash/size、arrival order 仍拒绝。

### 实际诊断 run

```text
run_id=330ff546-c0dc-4bd3-8f6c-cdb42858a13d
APK SHA256=51711779e5e9efd60105d1f312b90e65df0d85f7f8996c1d70e200485e6d9fef
app_pid=27281; server_pid=27815; live-host-child=51282
observations=[idle, started]
status=failed; host_gate_status=failed; rf=not_run
live raw=62,274 bytes; error=null; stop reason=stop-requested; child reaped(-15)
```

预算、完整 UI 路径和 APK 均未改变，只增加前置被动诊断。再次在 Started 的 Evidence→Current
导航后超时，尚未调用 Close。设备 live 日志确认 Click handler 与 response，最后一条正常
Appium response 为该 Current click；随后没有观测到目标 `session-state` FindElement 入口。
这仍不是“请求没有到设备”的证明。

关键新增的 host arrival 事实：前一 chunk 于 `671817.338895791` 收到，随后在
`671838.548748041` 又收到 126 bytes，内容为设备 `11:37:52.274` 的 ActivityManager warning；
fresh status probe 在 `671839.7070955` 开始，约 2.009 秒后 timeout。该系统记录及 host arrival
发生在本次 UI 超时附近，说明旧 stream 没有在最后一次正常 Appium response 后立即完全停传。
但不能仅用这一条记录排除缓存、USB/daemon/adbd 的部分 stream 问题，warning 也不是因果证据。
不同设备/host 时钟域不直接相减，raw 记录的设备时间不替代 host arrival 时间。

独立 host-local `adb server-status` 约 **16.4ms** 返回，核对仍是 SDK 37.0.1 daemon PID 93206、
`usb_backend=NATIVE`。随后同用户一秒 `sample` 显示 main 在 poll、USB reader 在 ReadPipe 等待；
这可以记录当时栈，但正常等待也可能有同形状，不能称为已证死锁或 USB 故障。未重启 daemon、
切 backend、重置 USB、重启手机或新增 radio/permission/RF 操作。

原 cleanup 的新 logcat/shell/DELETE/helper/tag 仍超时，保留 quarantine。首次独立 recovery 在
boot shell 前置检查就超时，没有 mutation；稍后 shell 恢复，fresh 核对原 boot/App/server/hash/
UID/start ticks 与 exact forward，完成原 instrumentation/App 退休、helper/driver/tag 恢复及
exact lock 归档。原 failed verdict、缺失 DELETE/graceful close/resource-zero 不补写。
当前无活动 phone lease，无 live capture child 遗留。

```text
.work/formal-android-managed-lifecycle/330ff546-c0dc-4bd3-8f6c-cdb42858a13d/
.work/live-logcat-lifecycle-device.log
.work/live-logcat-adb-{server-status,listener}.log
.work/live-logcat-adb-daemon-{sample.txt,existing-log.txt}
.work/live-logcat-original-session-{recovery,restored}.log
.work/formal-android-managed-recovery-1f686d6d-ca48-4bec-920d-fd1a9acaf931/
```

### 本轮软件验证

- live owner 的本地 pipe/child 测试与已有诊断回归：57 passed / 3.612s；实际 Popen 边界只允许
  本地 Python fixture，不使用真实 ADB。composition 新增测试先 red，再接线/修正 verifier。
- 当前 managed composition 4 项 + 完整 evidence 模块：53 passed / 49.079s；诊断/UI/CLI 邻近：
  112 passed / 4.218s。原有完整 discovery 的超时不计为通过，不盲目增加预算。
- `.work/live-logcat-{composition-final-green,neighbors}.log` 保存完整汇总。

## 16. 2026-11-06：按实际 UiAutomator2 版本核对使用方式

本轮仅核查本机包、Context7 官方文档索引与对应 server revision 源码，未改自动化配置、第三方
源码或设备状态，未执行新的真机 run。本机 driver **8.7.3**、server **10.7.6**；server package
`gitHead` 为 `31de0505539af35602cbe62519d95702702b8195`。Context7 默认索引可能比当前版本新，
因此实际选项以已安装 README 和该 revision 源码交叉核对，不用 latest 文档推定当前实现。

- 本地 `wait_find(..., 0.1)` 只限制外层 polling，不会取消已进入的 HTTP/device 调用。
  `waitForIdleTimeout` 与 `waitForSelectorTimeout` 都是 server Settings，默认各 10000ms；后者
  针对 `-android uiautomator`。不能机械相加为本次故障的 20 秒根因。Python HTTP timeout、driver
  `uiautomator2ServerReadTimeout`（默认 240000ms）是另外两层，客户端 timeout 不等于远端取消。
- `ElementLocationHelpers.findElement` 先 `resetAccessibilityCache()`，其中仍会 `Device.waitForIdle`。
  所以标准 id 查找也不是绕开 accessibility/idle 的独立活性通道。把 idle 设置为 0 会改变交互
  稳定性，官方也提示可能点错位置；不能全局设置后把故障消失算作修复。
- 仅按 resource-id 找控件，可使用标准 `AppiumBy.ID`：server 走 `By.res`/BySelector/UiObject2；
  当前 UiSelector 字符串走另一路 UiObject。RN 的 bare `testID` 应同时显式设置并读取确认
  `disableIdLocatorAutocompletion=true`，否则默认会补 `<appPackage>:id/`，不再是同一查询。
  当前 UiSelector 方案不是非法用法，但不能仅为简单 id 查找长期保留不必要的字符串构造。
  两种路径都不是经 runtime 验证的故障修复，本轮未切换。
- `HttpServer` 使用一个 boss event loop 和默认大小的 worker group，不是全局一个请求线程；
  单 channel 的 handler 同步执行，但 fresh TCP connection 不保证分配到不同 worker。
  `AppiumServlet` 路由与 `SafeRequestHandler` 未见包围所有请求的全局串行锁。
- `Status` 为 no-session handler，构造 build/ready response，不查询 UI 树、也不执行 find 的 idle
  等待。因此 fresh `/status` 也失败，不能简单解释为“一个元素找不到堵住同一个全局线程”。
  它仍共享 server process、网络栈和 ADB forwarding，不是独立的设备健康证明。
- `ServerHandler.channelRead` 在 dispatch 前记 `channel read`；`SafeRequestHandler.handle` 又在
  session 校验/业务 handler 前记 command。前置 live log 有助于观察两道入口，但缺日志仍不是
  请求未到达证明。`shutdownOnPowerDisconnect` 默认 true，属于需实际 power/stop 日志才能
  判断的另一机制，不能据此猜测当前发生了 USB/power 断连或直接修改它。

版本依据：本机 driver README 的 Settings/locator/capability 表，以及对应 revision 的
[HttpServer](https://github.com/appium/appium-android/blob/31de0505539af35602cbe62519d95702702b8195/packages/uiautomator2-server/app/src/main/java/io/appium/uiautomator2/http/HttpServer.java)、
[ServerHandler](https://github.com/appium/appium-android/blob/31de0505539af35602cbe62519d95702702b8195/packages/uiautomator2-server/app/src/main/java/io/appium/uiautomator2/http/ServerHandler.java)、
[Status](https://github.com/appium/appium-android/blob/31de0505539af35602cbe62519d95702702b8195/packages/uiautomator2-server/app/src/main/java/io/appium/uiautomator2/handler/Status.java)、
[AppiumServlet](https://github.com/appium/appium-android/blob/31de0505539af35602cbe62519d95702702b8195/packages/uiautomator2-server/app/src/main/java/io/appium/uiautomator2/server/AppiumServlet.java)、
[ElementLocationHelpers](https://github.com/appium/appium-android/blob/31de0505539af35602cbe62519d95702702b8195/packages/uiautomator2-server/app/src/main/java/io/appium/uiautomator2/utils/ElementLocationHelpers.java) 与
[AXWindowHelpers](https://github.com/appium/appium-android/blob/31de0505539af35602cbe62519d95702702b8195/packages/uiautomator2-server/app/src/main/java/io/appium/uiautomator2/utils/AXWindowHelpers.java)。
本地查询摘要在 `.work/uia2-{local-version-settings,docs-library,docs-settings}.log`；树索引在
`.work/uia2-source-tree.json`。这轮文档/源码核对不新增任何运行时通过结论。

## 17. 2026-11-06：故障窗口能选中 transport，但新 shell 服务未收到接受回执

为了在同一故障窗口区分 host daemon、缓存状态与实际设备服务，显式失败诊断增加一次
`AndroidAutomationOwner.probe_adb_transport()`。先用隔离测试证明缺失接线，再接到 fresh
UiAutomator2 status 后、live capture 停止及 DELETE 前；没有新增 App、CLI 场景或设备配置。

原本优先考虑 SDK CLI，但官方 `adb_client.cpp` 的 `adb_check_server_version` 会在 daemon 缺失
时启动它，在协议版本不匹配时 kill/restart；仅指定 SDK executable 或 `-H` 不足以保证只读。
所以这个探针只用固定已有的 `127.0.0.1:5037` smart socket 与四条白名单请求，不做通用 ADB
client，不添加依赖或 daemon 管理能力：

1. fresh socket 查询 `host:version`；返回的是内部协议版本，不是 SDK 工具发行版本。
2. fresh socket 查询 `host-serial:<owned serial>:get-state`；明确仅为缓存状态。
3. fresh socket 请求 `host:transport:<owned serial>`，然后提交
   `shell:cat /proc/sys/kernel/random/boot_id`，分别记录选择、服务接受和输出。

总 I/O 预算取 remaining deadline 与 3 秒较小者，所有发送/读取更新剩余 timeout，处理分片、
EOF、FAIL 和长度上限；无 retry。失败即停止依赖阶段，保存各 stage 的原始响应/时间/错误。
不修改主错误、owner phase 或关闭结论，不把 cached `device`、transport OKAY、boot ID 比对
当作 native/session ownership/resource-zero 证明。probe 与其他可选诊断共享原 half-cleanup
窗口，不扩大 scenario/cleanup 预算。官方 framing/service 依据保存在
`.work/adb-probe-official-{client.cpp,services.md}`。

### 同 APK 真实失败观测

```text
run_id=6bb87911-5717-4945-976e-6faf80f7fc79
APK SHA256=51711779e5e9efd60105d1f312b90e65df0d85f7f8996c1d70e200485e6d9fef
app_pid=15246; server_pid=16072
observations=[idle, started]
status=failed; host_gate_status=failed; rf=not_run
```

又在 Started 的 Evidence→Current click 200 后，下一个 `session-state` find 超时，未提交 Close。
fresh UiAutomator2 status 同样 timeout；随后只读 ADB probe 的真实阶段结果为：

| 阶段 | 结果 |
| --- | --- |
| server_version | `OKAY00040029`，正常返回 |
| cached_state | `OKAY0006device`，正常返回 |
| transport_select | `OKAY`，正常返回 |
| service_accept | 约 2.984 秒等待后 timeout，未收到任何接受响应 bytes |
| boot_id | 未进入，`boot_id_matches_expected=null` |

三条 fresh socket 总预算 3 秒。超时观测实际结束可能略超 deadline；verifier 保留真实迟返回的
失败记录，而不是把它当不合法证据丢弃，也不扩大网络预算或允许迟完成冒充成功。

这是比 UiAutomator2 元素查询更底层的差分：host daemon 和 transport 选择可应答，但新设备
shell 服务的接受回执不可用。**不能仅用 UiSelector/idle 等待解释全部现象**。仍不能据此定位
为 host USB backend、USB 物理层、adbd、设备调度/服务创建中的某一项，也不能把没有回执等同
请求未被发送或设备未收到。

原 run quarantine 和 missing DELETE 保留。随后 shell 自行恢复，按原 boot/PID/start ticks/UID/
installed hash、original forward 校验完成 instrumentation/App 退休与 helper/driver/tag 恢复，
归档 exact quarantine lock；不重启 daemon/手机、不 reset USB、不改 radio/permission。

### 恢复后的同协议只读正常对照

独立 App-only phone lease 下，四个相同白名单请求、相同 serial/daemon endpoint、同 3 秒预算，
不启动 Appium/UI/Rust owner：

```text
run_id=9f7293c2-e2cf-442c-8fc9-f1729879b073
scope=bounded-adb-smart-socket-read-control
service_accept=OKAY / 20.488ms
boot_id=matches expected / 23.309ms
total=46.715ms; status=passed; phone_status=passed; cleanup_errors=[]
host_gate_status=pending; rf=not_run
```

因此健康窗口的实际设备确认该固定协议能工作，不仅是 fake 的通过；但它只是短对照，不是
RN lifecycle、RF、ABI 或长期 transport 稳定性通过。当前无活动 phone lease，原失败 App 已退出。

```text
.work/formal-android-managed-lifecycle/6bb87911-5717-4945-976e-6faf80f7fc79/
.work/adb-probe-{lifecycle-device,original-session-recovery,healthy-control}.log
.work/formal-android-managed-recovery-667d841e-703f-42fe-aeee-a76be4373392/
.work/adb-smart-read-control-9f7293c2-e2cf-442c-8fc9-f1729879b073/
```

### 软件证据

- socket 边界测试先 red，fixed-stage probe green 7 项；session/preparation 完整模块回归
  94 passed / 103.222s，无真实设备命令，5037 fake 与 UiA HTTP 显式区分。
- managed 接线先 red，再绿：4 passed / 51.993s，覆盖只在显式 UI failure 后执行、主错误保留、
  fresh status→ADB probe→live stop→DELETE 顺序及独立清理；普通成功流不调用新探针。
- re-sealing 原 selectors、stage prefix/output/value/错误与预算校验；迟返回失败回归之后，完整
  evidence 模块 54 passed / 1.888s。另 evidence/CLI/diagnostics 邻近组合 123 passed / 5.627s。
- 日志：`.work/logs/adb-{transport-session-regression,evidence-late-timeout-green}.log`、
  `.work/adb-probe-{composition-green,neighbors}.log`。全 tools discovery 既有超时未升级为通过。

## 下一步边界

下一项优先区分 **新设备服务接受阶段中的 host USB/transport 与 adbd/设备服务建立**，
同时保留 Started 回到 Current run 的 mount/accessibility 关联。不能再把新连接超时直接概括成
整条 ADB 链路完全停传，也不能凭 host daemon 栈或 native USB backend 名称定因。
不重复无新增证据的完整 lifecycle、再换点击机制或扩大超时；不为诊断改 Rust owner/watchdog，
不擅自切换共享 daemon/backend、USB reset/手机重启。独立 reader timeout/HandlerThread 缺口仍需
各自 test-first/native 验证；完整 lifecycle/reload/RF/ABI 与 internal 删除条件仍未满足。
