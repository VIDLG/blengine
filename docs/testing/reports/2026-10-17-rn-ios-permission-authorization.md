# iOS RN Bluetooth authorization：完整非 RF 门禁通过与历史恢复复核

日期：2026-10-17。

## 结论

本轮在原有 **Tests → Current run → Evidence** 工作台和现有 XCUITest target 中增加 iOS **authorization-only / non-RF** 场景，没有新 App、Python runner、通用控制框架或生产 BLE API。

**完整 authorized baseline → denied → restored 非 RF 门禁已由新的 `permission-authorization-20261017-05` 显式 attempt 通过：1 passed / 0 failed / 0 skipped。** XCUITest 自动完成拒绝及允许，native 读取绑定同 run、不同进程/boot，确认 Authorized → NotDetermined → Denied → NotDetermined → Authorized，baseline/denied/restored 三个 audit Probes 均完成检查及 close/17-zero。最终 host verdict 为 passed、restored=true、primary/cleanup error 均为空；结果 App 保持打开。

此前完整尝试和恢复诊断原样保留，不能因新通过而升级旧 verdict。用户反馈部分历史测试有人工点击，具体哪些动作受影响未知；在要求不人工点击的新尝试中也复现过旧 App UI timeout，不能把旧问题只归因于人工操作。当前通过使用明确分离 consent staging / fresh audit 的新版本，不复用旧失败。

较早的**恢复专用复核也各有独立通过**，但它们只证明对应 run 的授权恢复与 initialize/close，不替代上面的完整新门禁。当前完整通过仍不覆盖 RF 恢复、活动连接撤权、same-Hub 恢复、radio transition、consent-staging process 的 close/zero 或 ABI freeze。

## 完整新通过的身份与证据

实际命令（默认末尾参数为 0，即完整流程）：

```text
pixi run just react-native host-ios-device-permission-test 00008110-000C61160C39401E FWVYQ287W5 build/permission-authorization-20261017-05.xcresult
```

- Run ID：`95997b1b-fb4c-4940-b044-1789b5a50458`。
- 结果：`1 passed / 0 failed / 0 skipped`，host attachment scope 为 `ios-authorization-only-non-rf`，不是 restoration-only；primary/cleanup 均为空。
- XCTest summary 与完整附件位于 `tests/consumers/react-native-internal/ios/build/permission-authorization-20261017-05.xcresult` 及 `.summary.json`；导出副本在忽略目录 `.work/ios-permission-passed-05-attachments/`。
- `permission-consent-decision` 分别记录 `decision=deny`、`decision=allow`；存在对应系统弹窗截图/层级和阶段完成截图。

| 审计步骤 | Native PID | authorization-at-launch |
|---|---:|---|
| 初始 inspect | 67720 | authorized |
| baseline initialize/close | 67721 | authorized |
| 第一次 reset 后 inspect | 67722 | not_determined |
| consent deny 后 fresh audit | 67725 | denied |
| 独立 denied inspect | 67727 | denied |
| 恢复 reset 后 inspect | 67728 | not_determined |
| consent allow 后 fresh audit | 67731 | authorized |

七份 native identity 的 run/bundle/executable 匹配、boot UUID 均不同。Baseline 和 restored audit 要求 PoweredOn + Authorized 与成功 initialize；denied audit 要求 initialize 成功、Denied snapshot、scan 恰一次 Permission terminal，不能出现 scan domain/广告。三份 audit Probe close 均校验 17 项零。

最终 restored boot 为 `fa663c31-f143-4e6c-bf07-37433bc5cec5`，native 授权为 authorized；App 保持打开。两个 consent-staging process 仅用于引出并处理系统提示，**没有其 close/zero 断言**；这不是验证九个 clean lifecycle，也不是活动请求撤权结算或旧进程资源释放证据。

## 实现与所有权

- [`rf_permission_transition.tsx`](../../../tests/consumers/react-native-internal/src/rf_permission_transition.tsx) 复用 phase audit、Probe close、17 项零校验与有界 UI；iOS 只接受 `denied` 和 `authorized`，拒绝 Android 的 active/process-death 与 restored RF I/O 阶段。`inspect` 只显示 native 授权身份，不创建 Probe、listener 或 BLE command。
- [`App.tsx`](../../../tests/consumers/react-native-internal/src/App.tsx)、[`rf_lab_catalog.ts`](../../../tests/consumers/react-native-internal/src/rf_lab_catalog.ts)、[`rf_lab_session.tsx`](../../../tests/consumers/react-native-internal/src/rf_lab_session.tsx) 使用独立 `permission-authorization` runtime identity，fixture 为 none。摘要不接纳 Android active/death/RF success，App 检查不是 host gate。
- [`AppDelegate.swift`](../../../tests/consumers/react-native-internal/ios/BleHubNitroHost/AppDelegate.swift) 只读取公开 `CBManager.authorization`，记录 run/phase、PID、native boot、bundle/executable、authorization-at-launch。普通启动仍是无 Probe 的 workbench；没有调用私有 TCC 或 Bluetooth setter。
- [`BleHubNitroHostUITests.swift`](../../../tests/consumers/react-native-internal/ios/BleHubNitroHostUITests/BleHubNitroHostUITests.swift) 拥有 app-specific authorization reset、明确的 Bluetooth consent UI、恢复和 XCTest evidence。执行必须显式 opt-in；默认 all-tests 不隐式重置授权。
- [`just/react-native/mod.just`](../../../just/react-native/mod.just) 复用 Pods/build、unlocked-device preflight、`run_quiet --no-tty` 和同一 UI target。拒绝覆盖 result/log/summary；关闭并行，无自动测试重试；summary 必须正好一项 passed、无 failed/skipped，skip 不算通过。

Authorized phase 只 initialize/close，**不启动 scan、连接或 GATT**。Denied phase 请求 scan 只为验证权限拒绝，若出现 scan domain、广告、成功 terminal 或其他事件则 fail-closed。其 UUID 是非 RF sentinel filter，不指代已安装的 Peripheral profile；本轮没有 fixture lease、广播或 RF bundle，不能拿 XCTest summary 代替 nRF RF evidence。

## 公开 API 与机器前提

真实 Mac mini 的 selected Xcode 为 `/Applications/Xcode.app/Contents/Developer`，iPhoneOS SDK 27.1；iPhone `00008110-000C61160C39401E`，iOS 26.6.1 / 23G83，team `FWVYQ287W5`。只读确认设备 available、unlocked，Mac DevToolsSecurity 已启用；未请求密码、sudo 或授权绕过。

本地 SDK 的 `XCUIAutomation.framework/Headers/XCUIProtectedResource.h` 定义 `XCUIProtectedResourceBluetooth`；`XCUIApplication.h` 定义 `resetAuthorizationStatusForResource:`，说明下次访问会显示授权提示，且部分资源重置可能终止 App。Swift `resetAuthorizationStatus(for: .bluetooth)` 已通过本机 iOS SDK typecheck，并用于本轮 XCTest。`XCTest.framework` 的旧 header 只是指向 XCUIAutomation 的 forwarding header。

CoreBluetooth 授权与 manager state 是不同维度；本地 [CBManager snapshot](../../platform-api-reference/apple/cbmanager.md) 及 [AppleEventMapper](../../../platforms/apple/Sources/BleHubApple/AppleEventMapper.swift) 是映射依据。没有把 iOS 套成 Android `ApplicationExitInfo` reason 8，也没有把控制中心临时断连当作 PoweredOff。

## 执行与证据

所有 xcresult 位于忽略目录 `tests/consumers/react-native-internal/ios/build/`：

| 路径 | 结果与范围 |
|---|---|
| `permission-authorization-20261017-01.xcresult.log` | CLI exit 64：Xcode 不接受 `-test-iterations 1`；尚未执行 XCTest 或授权重置。参数已移除，保留原日志。 |
| `permission-authorization-20261017-02.xcresult` | 完整流程：0 passed / 1 failed / 0 skipped。Inspect 与 authorized baseline 完成；授权 reset 后，App identity 被系统弹窗遮住，主错误及恢复错误均为 identity UI timeout。 |
| `permission-restore-20261017-01.xcresult` | 仅恢复：严格拒绝未能读取标题的 alert，没有盲点按钮；failed。 |
| `permission-restore-20261017-02.xcresult` | 仅恢复：已保存正确的 Bluetooth alert hierarchy；递归读取动态地图/全部 descendants 时 query snapshot 失效；failed。 |
| `permission-restore-20261017-03.xcresult` | 仅恢复：明确的“允许”已点击，截图/层级保留；随后 App identity UI timeout，仍 failed，不宣称恢复完成。 |
| `permission-restore-20261017-04.xcresult` | **仅恢复复核 passed**：先 native inspect 确认已 Authorized，再 fresh authorized initialize/close；未再次 reset。1 passed / 0 failed / 0 skipped。 |
| `permission-authorization-20261017-03.xcresult` | 显式完整新尝试；0 passed / 1 failed / 0 skipped，alert query snapshot 失效。未升级为通过；用户后续反馈部分历史测试有人工点击。 |
| `permission-authorization-20261017-04.xcresult` | 用户要求重测、并明确提示不人工点击后的完整新尝试；0 passed / 1 failed / 0 skipped。脚本记录定位并 tap“不允许”，随后 App identity UI timeout；同场恢复也 timeout，原 verdict 保留 failed/restored=false。 |
| `permission-restore-20261017-05.xcresult` | **独立恢复复核 passed**：1 passed / 0 failed / 0 skipped；native inspect 为 Authorized，新 authorized Probe initialize/close 完成，未再次 reset。 |
| `permission-authorization-20261017-05.xcresult` | **完整授权专项 passed**：1 passed / 0 failed / 0 skipped；自动 deny/allow、独立 reset/实际授权读回、三个 fresh audit Probes 完成检查及 close/zero，host restored=true、无 primary/cleanup error。 |

完整流程的原始 verdict attachment：

- run ID `5466141c-83bb-447d-875f-9a8d0c1e48e2`；
- scope `ios-authorization-only-non-rf`，status failed，restored false；
- baseline native inspect PID `67424`，authorized baseline PID `67426`，不同 native boot；两者 launch authorization 为 authorized。

恢复复核的身份：

- run ID `8a3558e9-d63b-46b5-9623-15a1aa69b179`；
- inspect PID `67514`，boot `e7e66cf5-33b7-43b7-bb89-a3c0e336755f`；
- authorized PID `67516`，boot `8a2812de-7517-4a64-ae39-d9e71ba579f9`；
- 两者 native authorization-at-launch 均为 authorized；
- attachment 明确写 `scope=authorization-restoration-only; full-gate-not-run`，recipe 也输出 `ios-permission-authorization=restoration-only`。

恢复复核实际命令：

```text
pixi run just react-native host-ios-device-permission-test 00008110-000C61160C39401E FWVYQ287W5 build/permission-restore-20261017-04.xcresult 1
```

末尾 `1` 是显式 restoration-only，不是完整流程 pass。默认末尾参数为 `0`，执行完整门禁；每次必须给新 xcresult 路径。恢复路径先只读 inspect，若已 authorized 就不再 reset；否则只做恢复并验证新 Probe。主错误与恢复错误分别保留。

## 观察到的问题与限定修复

1. 初版先处理 consent 再 activate 原 App 仍不足：`permission-authorization-20261017-04` 的活动树记录了正确目标 alert 和“不允许”按钮、tap/event synthesis，以及 XCTest interruption-query 警告，随后原 App UI 不可读。不能据此证明进程死亡、CoreBluetooth 故障或成功 deny；UI 等待超时不是 BLE terminal。
2. consent 只接受包含本 App 名及 Bluetooth/蓝牙的 alert，按钮采用明确的中英文标签；不按位置、不点通用“第一个按钮”。当前真机 alert 标题为 `允许“BleHubNitroHost”查找蓝牙设备？`，按钮为“不允许”/“允许”。
3. 不枚举系统 consent 内的所有 descendants：该版本提示包含动态地图，跨多次 snapshot 的 index 会变化。现改为 alert 自身标题和精准 button query，保留窄范围 accessibility evidence。
4. XCTest 的 restoration-only 路径不会把完整 gate 重跑；最终用新的独立 native inspect + initialize/close 证明恢复，而不是回填原失败 verdict。

### 当前修复：consent staging 与 fresh audit 分开

在上述新失败之后，XCTest 流程调整为：reset 后先用无 Probe 的 native inspect 证明 NotDetermined；另启动 consent-staging App 并自动选按钮；结束该 staging App 后，用新进程读取实际 Denied/Authorized，并检查相应 Probe initialize/scan-denied/close。原 App UI 能否继续可读不再是该 authorization-only 测试的前提。

**staging process 的终止不证明 close、17 项零或旧请求结算。** 只对 fresh audit Probes 要求并记录 close/zero；该变化不升级为 same-Hub、live revoke、process-death cleanup 或 RF 证据。Native run/phase/boot、实际授权、事件及 terminal/zero 校验保留。正常完整流程的恢复仍要求 reset/consent/新授权审计；失败 cleanup 和显式 restoration-only 则先读当前授权，已 authorized 就不再 reset，避免无必要地更改隐私状态。

这个 fresh-audit 版本随后由独立 `permission-authorization-20261017-05` 完整验证通过，详情见顶部身份与证据；此前只做构建/软件检查的阶段不当作硬件通过。无需操作者手动修改全局 radio；未调用私有系统控制、未请求终端密码。

最近一次恢复的原始身份：run `376d96a5-8673-45cf-916c-7aa08670ed11`，inspect PID `67636` / boot `e60f3942-cc1c-4f70-a5a7-f71fa101c4cc`，authorized PID `67637` / boot `9647fc57-ba47-4b1b-8720-1834191cbc99`；两个 native launch authorization 均为 authorized，attachment 明确 `authorization-restoration-only; full-gate-not-run`。对应完整失败 run 为 `0c3c33d7-421e-44ac-affc-ea4c28a728b0`，其 primary/cleanup timeout 及 restored=false 保留，不回填。

## 验证与剩余工作

- `pixi run just react-native host-check` 通过；RN 全量 Jest **13 suites / 355 tests passed**，包含平台 phase 隔离、native metadata 不替代真实 snapshot、inspect 无 Probe、授权摘要不接纳 Android death/RF markers。
- `pixi run just react-native tools-fixture-test`：**839 tests / 54.411s / OK**；它不包含真机 consent 操作，不构成 RF。
- unsigned iOS `build-for-testing` 与真机签名构建通过；真实 restoration-only XCTest 通过。存在既有 Pods/SDK 警告，不宣称 warning-free。
- `pixi run just fmt`、`pixi run just lint`、`pixi run just docs-check-mermaid`（26 diagrams / 140 Markdown files）、UTF-8/fences/whitespace/conflict 与 `git diff --check` 均通过。

本次 authorization-only gate 已有完整新通过，不能扩大为 live RF 或计划阶段退出。下一步仍需独立设计并验证 live-attested iOS RF recovery、radio startup-off/active-loss 与 active write/CCCD loss；不增加私有系统控制接口，也不修改 production ABI 或计划退出条件。
