# Formal Android：concrete session owner 与只读收尾

日期：2026-11-04。真实 Mac、本地 Pixi/Just、未提交工作树。**本轮没有真机、Appium/WDA 真实服务、安装/授权、radio 变更或 RF 执行。**

依据：[单一 Plan](../../../.plan/PLAN.md)、[host 职责](../../research/react-native/react-native-host-tooling-architecture.md)。此前的 [inventory](2026-11-04-formal-android-automation-inventory.md) 与 [root 边界更正](2026-11-04-formal-android-automation-admission.md) 保留原候选事实；本轮继续实现具体生命周期，不把历史软件通过继承为设备证据。

## 本轮实际接线

新增 [`AndroidAutomationOwner`](../../../tools/react_native/bleng_android_session.py)，并接入 [`ScanAutomationOwner`](../../../tools/react_native/bleng_automation.py)：

1. owner 自行调用同 lease 的 live inventory collector，得到**进程内 observations**，同时保存命令与报告。外部 JSON、即使复制到预期路径，也不能构造 authority；输出目录必须全新。
2. prepare 检查受支持的明确设置基线及独立 OS observation。
3. managed Appium child 固定本次 nonce endpoint 和显式 `APPIUM_HOME`。
4. `begin_session(server_url=...)` 在 Remote 前再跑完整 idle 检查，固定实际 owned Appium endpoint；之后进入 requesting，不可再用于另一个提交。
5. 实际 SDK 创建一个 attach-only session，使用明确 `systemPort` 和共享的 protected capability profile。
6. bind 通过独立 SDK identity、完整 AMS instrumentation、helper 进程、forward、device REST 和一次 host live proxy 请求，前后核对原 identity。
7. `quit()` 成功返回后，parent 才调用 `session_deleted(host_session_id)`；接着回收 local server，执行 concrete finalizer。只有本轮已知 DELETE 回执或明确从未请求 session 的 prepared 状态能进入成功收尾。
8. finalizer 重新采集 idle inventory/OS/settings，核对原基线和 frozen receipts。它不 stop/reset/root/grant/repair，不释放 phone；parent 的 `require_closed()` 还会复查 nested owner 的结果。

Android 的 callbacks 从这个 concrete owner 派生，不能同时传外部 guard/finalizer 来覆盖。iOS 仍使用其必填 platform callbacks。**managed Android 软件路径现在可以接真实 owner；裸 `formal-host-ui android` 仍没有此交接，继续拒绝。**没有添加绕过开关或 scan CLI。

## 受支持的 prepared profile

这不是一般性的“自动恢复任意手机设置”框架。初版要求显式准备：

- 三个 helper 的已审核 SHA256 与 installed path/UID/hash 一致，single user 0。
- 三个 hidden-API keys **实际不存在**。`settings get` 的 `null` 不能区分 absent 与字面字符串 `null`，所以额外用 `settings list global` 确认存在性；final 再核对不存在。
- Settings 的 mock-location AppOps 必须含 package-scope `allow`，存在的其他受支持 scope 也必须为 `allow`，且整个 scope 形状随后保持一致。
- `/data/local/tmp/mock_apps.json` 已存在、是无重复合法包名的列表并包含 Settings；后续比较原捕获字节。stock 的已审核分支因此无需 push store，不走 reset/deny 其他包的分支。
- 不存在未知 active instrumentation，也没有 Settings 当前/过渡 service 或 connection 记录。缓存的 exact Settings process 可保留，但要核对其独立 OS identity，不 stop 或认领它。
- IME 的当前值和 enabled 列表可读取并保持不变；owner 不修改它们。

stock driver 可执行已知的临时 `hidden keys absent → 1 → absent`，但必须被观测读回；不是 blanket no-root/no-mutation。root 已可用，显式 provisioning 继续复用既有安装器；本轮不自动补 AppOps/store 或改设备状态来满足前置条件。其他基线应返回明确失败，不能假装自动恢复。

完整 global 输出可能含无关设备标识，因此 receipt 只保存三个 policy rows 和完整**捕获 stdout 文本**的 SHA256，不持久化其他值。它不是原始 wire/数据库 attestation。

## session/transport 不能靠 capabilities 猜

- factory 与 owner 共用 [`prepared_driver_capabilities`](../../../tools/android/android_appium.py)，固定 attach/no-reset/no-grant、skip-unlock、抑制 ADB server restart、禁用 driver logcat、明确 helper mock app 等配置；省略会改 IME/GPS/locale/方向的参数。owner 拒绝额外这些 side-effect capabilities，包括有些传 `False` 仍会操作的选项。
- parent 与 child 均固定实际 `config.remote_server_addr`，不只比较 executor/config 的对象身份。真实 Selenium 会每次从该字段构造 URL；更改后恢复不能复活已撤销的 authority。
- helper server identity 复用原 [`read_app_identity`](../../../tools/react_native/android_sdk.py)，包含 OS boot、原 PID/start ticks、数字 UID/cmdline、原安装路径和 SHA256；proxy 前后只能核对原 identity，不能重新选择。
- host Appium ID 与 device server UUID 是两个字段。device 6790 的既有 forward 经检查后，通过共用 bounded loopback GET 读取 `/sessions` singleton；`driver.get_settings()` 是真实 host→device proxy 路径，不是 capability echo。
- 保存的 correlation 明确为 singleton REST + live host proxy + bracketed OS identity；`explicit_host_device_uuid_mapping=false`。现有公开 host API 没有显式返回它的内部 downstream UUID，不能伪造一个能力字段。
- `require_owner` 只查本地 lifetime/lease/driver/session/caps/transport，不在逐字段 UI polling 中重复完整 SDK/HTTP inventory。完整检查属于 prepare、提交、bind 与 finalization。

共用 [`read_loopback_json`](../../../tools/mobile_appium.py) 复用了原 readiness 的严格 HTTP/JSON parser：loopback、只读 GET、64 KiB 整体响应上限、同一 deadline、不 redirect、不接受重复 key/非有限 JSON。原 readiness 继续只接受明确 `value.ready=true`。

## Android OS observation

纯 parser 在 [`automation_observation.py`](../../../tools/android/automation_observation.py)，没有 SDK/进程调用。格式按固定 AOSP `android-16.0.0_r1` 窄范围源码核对：

- [`ProcessList.java` 4495+](https://github.com/aosp-mirror/platform_frameworks_base/blob/android-16.0.0_r1/services/core/java/com/android/server/am/ProcessList.java#L4495) 与 [`ActivityManagerService.java` 11194+/11422+](https://github.com/aosp-mirror/platform_frameworks_base/blob/android-16.0.0_r1/services/core/java/com/android/server/am/ActivityManagerService.java#L11194)：使用 `dumpsys activity processes`，不是未经支持的 `instrumentation` 子命令。要求 header 和正常尾字段 `mForceBackgroundCheck`；仅在受支持的完整 envelope 中缺 active section 才报告未观察到。
- [`ActiveInstrumentation.java` 94–140](https://github.com/aosp-mirror/platform_frameworks_base/blob/android-16.0.0_r1/services/core/java/com/android/server/am/ActiveInstrumentation.java#L94)：解析 component、finished、target package、唯一 target process 的数字 UID/PID；两个 permission 字段在 column 0，不能靠 dedent 结束 record。ProcessRecord 的 `mInstr=` 是回显，不能代替完整 record。
- [`ActiveServices.java` 7786–8155](https://github.com/aosp-mirror/platform_frameworks_base/blob/android-16.0.0_r1/services/core/java/com/android/server/am/ActiveServices.java#L7786)：使用 `dumpsys activity services io.appium.settings`，识别正常空结果及受支持 Last ANR 历史，拒绝 current/delayed/pending/restarting/destroying/connection 记录；历史 ANR 不误计为 live service。

running 必须只有一个 exact component、`finished=false`、target package 为 server，并与独立进程 PID/UID 一致。`.test` 是 instrumentation 包，运行的是 target `.server` 进程；不要求 `.test` PID。额外 helper UID/进程不放行。

这些文本没有强 framing/attestation。尤其 AOSP services 枚举异常可能只写 system log，后面仍输出 `(nothing)`；本轮只称**cleanup observations**，不是持续无服务保证或 resource-zero。OEM 实际输出、耗时和权限尚未执行验证。

## 失败和退休

- unknown Remote outcome 不枚举/接管/盲 DELETE；binding failure、未确认 DELETE、残留 process/instrumentation/forward、scope/IME/store/key 漂移都不被 idle readback 洗成成功。
- 任何 finalize 尝试后永久退休，失败后恢复 fake 字段也不能 prepare/begin/bind 或取得 authority。正常场景超时可用独立 cleanup budget，但不复活旧场景。
- SDK 主错误、post-boundary、publication、独立 server/platform cleanup 分开保留。终态 receipt 发布后再查 budget/authority；只撤回已知本轮 inode 且字节未改的成功文件，外部同字节 replacement 也不动。
- SIGTERM 只发向本轮 local process group，但 child shutdown handler 可能自己做远端收尾；local reap 绝不是 device cleanup proof。unknown 结果即使随后看似 idle 仍失败，不因 SIGTERM 猜成功。
- `require_closed()` 重验 frozen receipts；`host_gate_status=pending`、`resource_zero=not-claimed` 保持明确。成功 App 页保留不等于 OS 屏幕持续可见已验证。
- host 串行调用 owner 方法；该 controller 不是跨线程/跨进程仲裁框架。

## 软件验证

测试在 canonical `tools/tests/react_native/`，合成样本集中在其 `support/`，live owner 不 import synthetic/TestCase。新测试使用实际 inspector、identity reader、HTTP parser、ScanAutomationOwner 与 OS parser，隔离实际 subprocess/Popen/socket/SDK Remote 边界；不 patch concrete admission 为成功。旧 downstream callback 测试仍是隔离基线，不升级为真实 producer 证据。

```sh
pixi run just --command python -W error::ResourceWarning -m unittest \
  tools.tests.react_native.test_bleng_android_session \
  tools.tests.react_native.test_android_automation_observation \
  tools.tests.react_native.test_bleng_android_automation \
  tools.tests.react_native.test_bleng_automation \
  tools.tests.react_native.test_mobile_appium \
  tools.tests.react_native.test_capture_io \
  tools.tests.react_native.test_ios_appium \
  tools.tests.react_native.test_android_appium \
  tools.tests.react_native.test_bleng_scan_host \
  tools.tests.react_native.test_bleng_scan_ui \
  tools.tests.react_native.test_bleng_ui \
  tools.tests.react_native.test_android_sdk \
  tools.tests.react_native.test_android_instrumentation_capture -q
pixi run just --command python -W error::ResourceWarning -m unittest \
  tools.tests.react_native.test_bleng_android_candidate \
  tools.tests.react_native.test_bleng_ios_candidate -q
```

- 组合回归：**247 passed，52.724 秒**，包括新 owner 26 项与 parser 12 项。
- candidate/prepared runner 回归：**59 passed，107.272 秒**；其中旧 callback 场景不冒充 concrete owner 的全套 UI/RF 通过。
- `pixi run just react-native formal-host-check`：typecheck/autolinking/no-internal 通过。
- `pixi run just react-native formal-host-test`：**48 passed**，保留原 SafeAreaView deprecation warning。
- `pixi run just fmt`、`pixi run just lint`：通过，含 workspace/all-targets Clippy。
- `pixi run just docs-check-mermaid`：通过，200 份 Markdown 中 27 个图；本轮 7 份 authored Markdown 的 UTF-8、fences、尾随空白与 conflict 检查通过。
- 日志：`.work/formal-android-session-{targeted,review,regression,candidates,fmt,lint,host-check,host-test,mermaid}.log`。

一次子任务又执行 `pixi run just react-native tools-test`，在 **120 秒 timeout**，无全量 verdict；子任务检查未见对应残留进程。未因此删除失败测试、盲目延长或声称全量通过；此前 180 秒广域 timeout 与 legacy Jest timeout 也不升级为通过。

## 仍须闭合

完整实际 Appium/driver/config/plugin/dependency resolution 与来源、mobile runtime/OEM 输出及 deadline、Android rooted provisioning 的显式安装候选、iOS concrete WDA/forwarder owner、完整 outer host bundle 与其他 typed operation/reload/RF 迁移仍待推进。`APPIUM_HOME` 固定只是解析环境选择，不是完整 provenance。合作 lease 和前后观察不能消除外部竞争，不是原子 ownership attestation。

没有新 live scan CLI、没有设备/RF 结果、没有删除 internal，也没有 release/ABI/Phase exit 或 Git commit 结论。
