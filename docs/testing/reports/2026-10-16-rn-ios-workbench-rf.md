# RN iOS workbench：独立 RF 与后续原生 UI 验证

报告日期：2026-10-16；原生 UI 通过复核更新于 2026-10-17。下列时间除特别注明外均为 UTC。依据具名 artifact、manifest、ledger、physical evidence、xcresult 与执行日志核对；不转储 raw events，不以 App 页面代替 host verdict。

**当前结论：两次独立 iOS full-suite RF 通过记录保留；2026-10-17 在操作者启用 Mac developer policies 后，同一预编译 XCUITest 也实际通过（1 passed / 0 failed / 0 skipped）。** 下面的认证失败均是历史尝试，不再代表当前 UI gate 状态；UI smoke 导航与真实 full-suite RF 仍是不同证据。

## 结论与安装输入

**iOS 两次独立 full-suite 均真实 passed：各自一次启动完成基础 10 flows + strict recovery 10 cycles，bundle 离线 integrity verification 均通过。** 最后一次是在 XCUITest runner 初始化受阻后，为恢复 RF 结果页而新跑的独立场次；不是沿用首次结果，也不是 UI 导航通过记录。

- Xcode **27.1 / 27A9275**。标准 `devicectl` wrapper 起初 expected `651.13.5`、installed `642.16`，连 `--help` 都被 first-launch 检查阻塞；用户处理后当前版本为 `651.13.5`，与 expected 匹配。未自动重配对、安装工具或读取密码。
- Central：`00008110-000C61160C39401E`，官方设备 JSON 核对为 **vision’s iPhone / physical iPhone 14 Plus / iPhone14,8**，不是其它同列 iPhone。设备证据为 `/tmp/blehub-ios-installed-coredevice-devices.json`；当前工具版本由 `/tmp/blehub-ios-retry-standard-lock.json` 等记录。
- `host-ios-device-build … FWVYQ287W5` 成功，日志 `/tmp/blehub-ios-workbench-build.log`。输入是现有生产链路 App 的 **Debug device build**：`tests/consumers/react-native-internal/ios/build/device-derived/Build/Products/Debug-iphoneos/BleHubNitroHost.app`，不是 release；bundle ID 为 `org.reactjs.native.example.BleHubNitroHost`。
- 两场均从该 `device-derived` App 冻结安装输入；**完整 artifact fingerprint 相同**，不只是源路径相同。两份 manifest 的 `tree_sha256` 均为 **`c69989581089c41775123061b006428d850d7a2edd2865996dc86d54eb95af29`**，不是 UI-test derived App。
- Git provenance 均为 `75edda34b20b043ef282d7d94614c9a1169c6179`、`dirty=true`、`source_to_build_attestation=false`。冻结 tree 不是设备 installed binary byte attestation、签名信任链或 clean locked release。

## 两次独立 RF evidence

| 场次 / manifest | Bundle UUID | Runtime/fixture session / App PID | UTC 起止 |
|---|---|---|---|
| [首次 `8ea0bb…`][first] | `15368421-cde8-4388-bcee-46d6f9f67ac8` | `721e8611-18c1-422a-8493-34bd509903d9` / `58019` | `04:27:09.092999–04:30:01.972892` |
| [最终 `e3e217…`][final] | `1b6d4298-e620-4d0f-8af3-e2ded4247422` | `bb5d423b-cf50-4556-a0e5-a841d7a718fb` / `58080` | `04:35:31.245182–04:38:23.978669` |

两场各有独立 native boot、JS nonce、fixture session；每场内部基础段与恢复段共享一个 runtime/capture，不把两场拼成连续二十轮。Bundle UUID 不与 runtime/fixture session 混用。

| 核对项 | 两场各自的实际结果 |
|---|---|
| Manifest / host | `status=passed`、`completed=true`、`primary_error=null`，cleanup/evidence errors 均为空；主日志均为 `flows=10 cycles=10 app=kept-open`。 |
| Full-suite ledger | `app_runtimes=1/base_flows=10/capture_children=1/recovery_cycles=10/recovery_terminals=100`；基础段含 scan/connect/GATT/I/O/RSSI/Notification/Indication/passive-disconnect/cancellation/watchdog。 |
| Recovery ledger | 10 fresh Probe/Hub lifecycles、100 commands/terminals、250 events、10 notifications/passive disconnects/remote reads；50 resource snapshots、10 closed-zero snapshots，closed totals 的 **17 项逻辑资源计数全零**。不是同一个 Hub 十次恢复或 OS heap/CoreBluetooth 内部资源证明。 |
| Handoff / physical | 基础段后 canonical values restored、hold inactive、peer count 0、停止广播确认，再经 native/JS begin 双 ACK 进入恢复；恢复 final groups 为 20 connections / 20 disconnections / 10 closed snapshots。最终 cleanup 为 `advertising_stop_confirmed=true/hold_inactive=true/peer_count=0`，physical errors/cleanup errors 均为空，full-suite/recovery physical 均 passed。 |
| Live fixture identity | DK-A `jlink:1050252028`，profile `blehub-nrf52840-multifunction-v1-a`，service `fd500000-0000-4000-8000-000000000000`；两场 live receipt 的 image SHA-256 均为 `dd7dea99a325db32fa1fd66648d0a0814c8c7c442a77cf08998f2b91a1cc5b7a`，proof method 为 `btp-vendor-live-under-device-lease`。 |

最终场次的 [full-suite ledger][ledger]、[recovery ledger][recovery]、[full-suite physical][physical]、[recovery physical][recovery-physical] 保留详细身份与收尾。执行日志分别为 `/tmp/blehub-ios-workbench-full-rf.log`、`/tmp/blehub-ios-workbench-final-full-rf.log`；首次离线日志 `/tmp/blehub-ios-workbench-full-verify.log`。本次文档核对又以现有 `verify_bundle` 对两份原件只读复核，均通过；**offline integrity 不重做 BLE 语义断言或重新授权 RF**。

## 历史 XCUITest 尝试：未初始化，不判 UI 断言 pass/fail

首次 RF 后，主执行线增强现有 [`BleHubNitroHostUITests.swift`][ui-source] 的 `testContractLabCompletesAndRecreatesProbe`：增加 **Tests → Evidence → Current run** 切换，返回后继续断言 **Run 2 / 5/5 / contract passed**；没有新增 App 或测试框架。该用例以 `--blehub-nitro-basic` 运行 ABI smoke，本身也不是 full-suite RF 导航用例。

实际执行现有 `host-ios-device-ui-test` 返回 **65**：`BleHubNitroHostUITests-Runner` PID **58059** 未能初始化，底层为 **`com.apple.sharing.authentication error 12` / `Timed out waiting for response`**。**XCTest 断言未运行**：这是 UI runner 基础设施阻塞，不能称 UI 断言失败或通过，不是 RF 失败，也没有证据认定锁屏是原因。

- 多次官方 `lockState` 曾出现 `passcodeRequired=true`，但 post-build 的官方 `require_ready_unlocked_device` 已通过（`/tmp/blehub-ios-postbuild-preflight.log`）；runner 阻塞后官方检查为 `passcodeRequired=false/unlockedSinceBoot=true`（`/tmp/blehub-ios-after-ui-lock.json`）。不从这些不同时刻的快照臆断失败根因。
- 完整日志：`/tmp/blehub-ios-workbench-ui-test.log`、[`ios/build/device-ui-test.log`][ui-log]。
- 日志所载实际结果目录：[`Test-BleHubNitroHost-2026.10.16_12-32-34-+0800.xcresult`][xcresult]；其中 `Staging/1_Test/Diagnostics/devicectl_diagnostics.zip` 为 partial diagnose bundle，不是 UI assertions pass。
- 随后用原 `device-derived` App 新跑最终 RF 场次，留下 `app=kept-open` 结果页；这不补齐未运行的 native UI 导航断言。

## UI runner 认证复核（12:52–12:53 本地时间）

在组件版本已匹配、目标 iPhone 官方回读 `passcodeRequired=false`、`developerModeStatus=enabled`、`ddiServicesAvailable=true` 后，重新执行同一 UI recipe，仍返回 65。Runner PID `58190` 在测试断言之前再次报告 `com.apple.sharing.authentication Code=12`。日志 `/tmp/blehub-xctest-auth-recheck.log`，独立结果为 [第二次 xcresult][xcresult-recheck]；不重写第一次失败或原 RF bundle，也不把重复尝试算作新 UI 覆盖。

对首次 xcresult 用官方 `xcresulttool export diagnostics` 导出的记录（`.work/xctest-auth/`）进一步核对：`testmanagerd.remote` 已连接，runner session 已建立；失败发生在 `enabling automation mode`，错误链为 `com.apple.LocalAuthentication Code=-1` → sharing authentication timeout。Runner 的 team/application entitlements 正确、`get-task-allow=true`，provisioning profile 包含该设备且未过期；因此没有依据把这次失败归为构建签名、普通安装或 RN 页面断言问题。日志不能单独证明系统为何没有完成身份认证，也不据此修改配对或关闭安全检查。

已通过 LaunchServices 打开现有 Xcode workspace，供操作者在 Mac 图形界面查看系统认证提示并手动运行该具名用例。代理不读取密码、不自动重新配对、不更改认证设置；在完成这项人工认证边界前，UI gate 保持 blocked。

## 2026-10-17：无终端执行与 Mac 开发工具授权检查

07:32 的 `test-without-building` 由直接终端调用启动。Runner 在 07:32:53 先报 automation-mode LocalAuthentication 失败，Mac unified log 随后在 07:32:54 记录内部 `sudo` 进程；终端出现 `Password:` 后由操作者停止，没有输入或保存密码。这个时间顺序支持“失败后的额外诊断收集触发密码请求”的判断，但并未取得该 sudo 的完整 argv，不能把推断写成已确认的具体命令。

18:23 再次确认手机当时已解锁，改用现有 `run_quiet.py --no-tty`（关闭 stdin、独立 session/process group），并按当前 Xcode 帮助使用 `-collect-test-diagnostics never`。这次没有终端密码请求，约 20 秒后仍因相同 LocalAuthentication/sharing error 12 返回 65；runner PID `65539` 的断言未执行。日志 `/tmp/blehub-xctest-no-tty-test.log`，结果为 [无终端场次][xcresult-no-tty]。现有 UI recipe 已固定该诊断参数；它不跳过实际测试或系统认证，也不改写历史失败结果。

只读 `/usr/sbin/DevToolsSecurity -status` 另显示 **Mac developer policies disabled**，当前用户属于 `_developer`；与此同时目标 iPhone 的 Developer Mode 为 enabled。两者不是同一配置。Mac 开发工具策略是需操作者明确授权的环境前置项，尚未启用，也未证明它是本次 UI automation 失败的唯一根因。若操作者选择启用，必须在自己控制的系统界面或终端执行；普通 build/test 不自动执行 `sudo`、`DevToolsSecurity -enable` 或接受密码。

## 2026-10-17：启用 Mac 开发工具授权后的原生 UI pass

操作者自行启用 Mac developer policies 后，只读 `DevToolsSecurity -status` 返回 enabled；同场官方 lockState 为 `passcodeRequired=false/unlockedSinceBoot=true`。代理没有执行 sudo、读取密码、重新配对或修改任何测试断言，也没有重新 build/install Pods。

复用已有 `device-ui-derived` 预编译测试，运行 `test-without-building`，继续使用 `run_quiet.py --no-tty` 与 `-collect-test-diagnostics never`。执行返回 0，无终端密码提示。官方 `xcresulttool get test-results summary` 对 [本场 xcresult][xcresult-enabled] 确认为 **result=Passed / totalTestCount=1 / passedTests=1 / failedTests=0 / skippedTests=0 / expectedFailures=0**，UTC `10:58:04.891–10:58:47.604`，约 42.713 秒；设备为同一 iPhone 14 Plus / iOS 26.6.1 23G83 / arm64。

实际执行的 `testContractLabCompletesAndRecreatesProbe` 完成两轮 ABI contract smoke，每轮 5/5，显式 recreate 后为 Run 2；再进入 Tests 核对会话名与 App Passed，进入 Evidence 核对 host verdict 边界与 basic mode，返回 Current run 再确认 Run 2 / 5/5 / contract passed。这证明了该具名 smoke 会话的原生页面导航与结果保留，不升级为运行中 full-suite 导航、radio/permission transition 或 RF 重跑。用例自身的 `defer app.terminate()` 清理规则不变，不声称本 UI 测试结束后仍保留先前 RF 结果页。

日志 `/tmp/blehub-xctest-enabled-test.log`，官方 summary 保存于 `.work/xctest-auth/enabled-summary.json`。Summary 另记录一条内部 QoS priority-inversion runtime warning，本轮未归因或修复；它未产生失败断言，不宣称零告警。结果与环境前后对照支持 Mac 开发工具授权是此次解除阻塞的关键前置条件，但不概括成所有 LocalAuthentication error 12 的统一根因。

## 验证与边界

共享 UI 逻辑的软件验证不能代替 iOS native 导航。本次 native 导航结论仅来自上述 2026-10-17 XCUITest 实际 pass；没有 JS 修改，不新增或推测 Jest 数量。iOS RF 仍是 2026-10-16 两份具名 artifact/session 的独立 pass，不从本次 ABI smoke UI 导航继承新的 RF 结论。

未执行 iOS radio/permission mutations，不覆盖这些 transition/fault；不是 ABI freeze，也不是完整同候选 RN/Apple/Android/native/package 矩阵。文档前本轮 fmt/lint/diff 已通过，日志为 `/tmp/blehub-ios-workbench-fmt.log`、`/tmp/blehub-ios-workbench-lint.log`、`/tmp/blehub-ios-workbench-diff.log`；文档后的 fmt/lint 由主执行线另行重跑。本次只写报告与架构证据索引，不改 App、测试、工具或既有 evidence，不 pull/commit/reset。

[first]: ../../../tests/consumers/react-native-internal/ios/build/rf-full-suite-8ea0bb0c925c4190badd0d8e147adbdc/evidence-bundle.json
[final]: ../../../tests/consumers/react-native-internal/ios/build/rf-full-suite-e3e2178c0a4749f7b4cf33d31a23ce54/evidence-bundle.json
[ledger]: ../../../tests/consumers/react-native-internal/ios/build/rf-full-suite-e3e2178c0a4749f7b4cf33d31a23ce54/full-suite-ledger.json
[recovery]: ../../../tests/consumers/react-native-internal/ios/build/rf-full-suite-e3e2178c0a4749f7b4cf33d31a23ce54/recovery-ten-ledger.json
[physical]: ../../../tests/consumers/react-native-internal/ios/build/rf-full-suite-e3e2178c0a4749f7b4cf33d31a23ce54/coordination/full-suite-physical.json
[recovery-physical]: ../../../tests/consumers/react-native-internal/ios/build/rf-full-suite-e3e2178c0a4749f7b4cf33d31a23ce54/coordination/recovery/recovery-ten-physical.json
[ui-source]: ../../../tests/consumers/react-native-internal/ios/BleHubNitroHostUITests/BleHubNitroHostUITests.swift
[ui-log]: ../../../tests/consumers/react-native-internal/ios/build/device-ui-test.log
[xcresult]: ../../../tests/consumers/react-native-internal/ios/build/device-ui-derived/Logs/Test/Test-BleHubNitroHost-2026.10.16_12-32-34-+0800.xcresult
[xcresult-recheck]: ../../../tests/consumers/react-native-internal/ios/build/device-ui-derived/Logs/Test/Test-BleHubNitroHost-2026.10.16_12-52-23-+0800.xcresult
[xcresult-no-tty]: ../../../tests/consumers/react-native-internal/ios/build/device-ui-derived/Logs/Test/Test-BleHubNitroHost-2026.10.17_18-23-38-+0800.xcresult
[xcresult-enabled]: ../../../tests/consumers/react-native-internal/ios/build/device-ui-derived/Logs/Test/Test-BleHubNitroHost-2026.10.17_18-58-04-+0800.xcresult
