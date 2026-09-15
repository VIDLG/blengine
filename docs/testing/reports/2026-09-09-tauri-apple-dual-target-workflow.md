# 2026-09-09 Apple / Tauri 双端工作流收尾

## Candidate 与边界

执行环境为项目真实 Mac mini，Git 共同基线为 `ce0c5d54dcd7a337c0c688bfcff7b08b44d89590`，叠加本报告列出的未提交工作区变更。本次没有 commit 或 push。Mac 已接入完整远端历史，`main` 跟踪 `origin/main`；迁移记录见 [Git 工作流](../../git-workflow.md)。

本轮区分原生 Apple HIL、Tauri 确定性测试、最终链接、开发签名和真实 RF。无签名 iOS 构建不是 iPhone 运行证据，macOS 通过也不证明 iOS；Tauri 仍为可选 downstream 验证面，不替代生产 ABI/backend gate。

## 当前状态（按实际执行 candidate 区分）

| 验证面 | 最新已观察结果 | 不代表 |
|---|---|---|
| macOS Tauri TCC / native E2E | 用户允许Bluetooth后2 passed / 4 skipped；TCC blocker已关闭 | 本次E2E不是RF |
| 首次完整macOS Tauri RF | fixture编排修复后6 passing / 0 skipped，约1m35；app PID清理及Android `STATE_ON`/advertising ready确认 | 不继承给后续诊断candidate，不是长时soak |
| 最终UI诊断candidate | 19项component测试通过；实际RF重跑6 passing / 0 skipped，1m34.6s（WDIO总1m36）；app PID退出及Peripheral `STATE_ON`/ready确认 | 独立重跑而非继承首次pass；不证明长时soak或iOS |
| 原生macOS → Android Peripheral | `all` RF再次通过，与Tauri gate独立 | 不证明iOS |
| 原生iOS真机HIL | 最新串行`runtime-ios-android` retry仍`codesign errSecInternalComponent`，未RF | 历史iOS I/O RF不算当前全矩阵 |
| iOS Tauri | 既有两slice完整无签名构建通过；运行/开发签名/RF未跑 | 无签名build不证明真机运行 |
| Windows / 新增长时soak | 本轮未测 / 未跑 | 不从其他平台或短RF继承通过 |

以下保留先前失败和恢复顺序；状态以本表及末尾candidate边界为准。执行结果由协调主线程提供，本次文档更新仅运行文档检查。

## 最终结构与修正

- `platforms/apple` 为 macOS/iOS 共享的 Swift/CoreBluetooth backend。
- plugin 使用同一个 `assembly/apple-assembly` package、generated `attach_backend` 和 Rust `BackendAssembly`，不建立逐 BLE operation 的第二套 mobile bridge。
- iOS 由官方 `ios_path` 独占 Swift 链接；macOS 由 plugin build script 的 `SwiftLinker` 独占链接。目标配置统一为 macOS 13 / iOS 16。
- Tauri 官方 CLI `2.11.4` 移到 example 根目录的独立 npm manifest/lock，通过 `npm run tauri -- ...` 调用。UI 子项目不再拥有 CLI，仓库根文档工具依赖不受影响。
- 修复从 UI 目录直接执行 Node CLI 导致生成的 Xcode Rust build phase 调用 `node tauri`、找不到模块的问题；保留旧生成目录后用官方 init 重新生成，不手改 generated Xcode 工程。
- 注册遗漏的 Swift test target，测试目录/import 统一为 `BleHubTauriAppleAssemblyTests`。两个 XCTest 真正运行，而不是仅构建了生产 target。
- macOS WDIO已从直接spawn `.app` executable改为`NSWorkspace`/LaunchServices owner启动实际bundle并保留`NSRunningApplication`；`services: []`下普通WebDriver Classic连接同Rust embedded server，先校验临时loopback port、精确app PID listener和`/status` ready。stdin EOF（含Node parent被SIGKILL）触发精确实例terminate/有界forceTerminate。Windows/Linux保留原service与Cargo binary路径。不使用`osascript`或按进程名批量kill，不改协议或绕过TCC。
- macOS bundle 添加 Bluetooth usage description。`macos-authorize` 仅供人通过 LaunchServices 打开测试 bundle、点击 Initialize 并处理授权；它不是自动化测试，也不证明授权成功。
- `.gitignore` 排除官方 mobile builder 生成的 `assembly/.tauri/`；生成的 `gen/apple/`、归档、工具环境和本机签名配置不入库。
- iOS HIL 的精简失败日志现在保留 `errSec` / CodeSign 原因，并提示用户在本机终端执行钥匙串 recipe；不接收密码参数或将密码交给 agent。

相关入口见 [Just Tauri recipes](../../../just/tauri/mod.just)、[example README](../../../examples/tauri-blehub-internal/README.md) 与 [assembly 调研](../../research/tauri-apple-assembly-ecosystem.md)。

## `project.yml` 的两个 owner

| 文件 | Owner / 维护方式 | 验证目的 |
|---|---|---|
| `tests/hil/apple/ios/project.yml` | 人工维护、入库，由 XcodeGen 生成工程 | 独立 Swift HIL 消费生产 API/package，验证 Apple backend |
| `examples/tauri-blehub-internal/src-tauri/gen/apple/project.yml` | 官方 Tauri CLI 生成、ignored，不手改 | 同一 Tauri example 的 iOS host，验证 WebView/plugin/Hub/backend 完整链 |

不新增手写 Tauri XcodeGen 模板。官方 init 会保留已有生成模板；修改最低系统版本后，本轮先备份旧 `gen/apple` 再重新生成，避免配置已是 16、实际产物仍为 14。

## 已执行并通过

| 命令 / 检查 | 结果 |
|---|---|
| `pixi run just apple test` | Apple backend 确定性测试与 4 个 RF selection 测试通过 |
| `pixi run just apple build-ios` | 原生 Apple backend generic simulator/device 无签名构建通过 |
| `pixi run just apple rf-macos-android` | 现有 `all` 场景通过：scan、connection、GATT、I/O、Notification、strict-Indication 场景、RSSI、passive disconnect；Android Peripheral 恢复流程完成 |
| `pixi run just tauri ios-init` | 官方 Xcode 项目生成通过，未使用 rustup 安装 targets |
| `pixi run just tauri ios-build aarch64-sim` | 完整 simulator app archive / 最终链接通过 |
| `pixi run just tauri ios-build aarch64` | 完整 device app archive / 最终链接通过，未开发签名 |
| `pixi run just tauri tauri` | UI component/flow、Playwright、plugin Rust、demo Rust、2 个 Swift assembly XCTest、native host build 通过 |
| `BLEHUB_TAURI_DEMO=1 pixi run just tauri tauri-native-e2e` | 真实 macOS WebView embedded automation：3 passed、3 RF-only skipped；仅 demo，不是 RF |
| `pixi run just tauri macos-bundle` | native debug/instrumented `.app` 构建通过 |
| `pixi run just tauri tauri-native-e2e`（用户授权后） | 2 passed / 4 skipped，无RF；TCC blocker关闭 |
| `pixi run just tauri tauri-native-rf`（fixture编排首次修复后） | 6 passing / 0 skipped，约1m35；完整可见flow、重复/循环及故障恢复通过 |
| 后续UI-only诊断candidate component gate | 19项全过；与随后RF重跑分开记录 |
| `pixi run just tauri tauri-native-rf`（最终UI诊断candidate重跑） | 6 passing / 0 skipped，1m34.6s（WDIO总1m36）；session和PID见末节 |
| `pixi run just fmt` / `pixi run just lint` / `pixi run just tauri lint` | 通过 |

早期使用npm service的WDIO demo teardown曾输出upstream mock-store cleanup warning，但该测试命令退出成功；保留为历史提示，不将其当作最新owner路径、RF或production cleanup的额外证据。

### iOS 最终产物检查

| 检查 | Simulator | Device |
|---|---|---|
| Mach-O platform | IOSSIMULATOR / arm64 | IOS / arm64 |
| `MinimumOSVersion` | 16.0 | 16.0 |
| `LC_BUILD_VERSION minos` | 16.0 | 16.0 |
| BleHub UniFFI 定义 | 123 个，各唯一 | 123 个，各唯一 |
| `blehub_tauri_apple_assemble` | 1 个 | 1 个 |
| 旧 test-harness 符号 | 无 | 无 |
| 额外内嵌 dylib/framework | 无 | 无 |

两 slice 的 BleHub UniFFI 符号集合一致。两者均含 CoreBluetooth 依赖和 Bluetooth usage description。Simulator 的 linker ad-hoc signature 不表示 Apple Development 签名通过。

本地日志与产物（ignored，不是可移植测试证据）：

- `.work/tauri-ios16-{init,sim-build,device-build}.log`
- `.work/tauri-ios16-final-images.log`
- `.work/tauri-ios16-simulator.xcarchive`
- `examples/tauri-blehub-internal/src-tauri/gen/apple/build/blehub-tauri-internal_iOS.xcarchive`

## 阻塞、历史失败与未执行

### iPhone 原生 HIL

`pixi run just apple runtime-ios-android` 已重试，连接的 iPhone 14 可见、Developer Mode enabled，但 Xcode 最终签名失败：

```text
BleHubAppleIOSProbe.debug.dylib: errSecInternalComponent
Command CodeSign failed with a nonzero exit code
```

尚未安装/启动新 app，没有进入 RF。此前 iOS Android RF 历史通过仍只属于 [2026-09-08 报告](2026-09-08-apple-ios-android-rf-pass.md)，不能 relabel 为本轮全场景重测。用户需在本机交互式终端执行 `pixi run just apple unlock-login-keychain` 后再重试；密码仅交给系统工具，不发送给 agent。

### macOS Tauri native：早期失败，TCC现已解决

早期`tauri-native-e2e`能建立embedded WebDriver session，但真实snapshot为 `Unavailable + NotDetermined`，初始化 readiness 断言失败。`tauri-native-rf` 尝试也因 `CoreBluetooth scan authorization is not allowed` 停止，未完成扫描/GATT。之后移除了无效 preflight，保留用户授权边界，没有放宽断言或把 demo 成功当作 native 成功。

当时pinned npm service的embedded provider直接spawn bundle executable，没有官方attach既有LaunchServices实例的入口。这是旧进程owner限制，不是WebDriver协议不能连接；后续已用标准LaunchServices owner解决，详见下文。仅写入Info.plist不证明TCC已授权；实际用户授权后的native E2E通过才关闭本轮TCC blocker。

### 仍待完成

1. 人工钥匙串授权后重跑 iPhone 原生 HIL，再记录实际 RF 场景。
2. iOS Tauri 开发签名、安装、真机 WebView/plugin 生命周期与 RF；现有 macOS embedded 测试不自动覆盖 iOS。
3. Windows native provider 构建、运行和受影响测试在 Windows 独立执行。

macOS TCC授权、native E2E及最终UI诊断candidate的Tauri RF重跑均已完成，不再列为待办。

本轮未使用 nRF、macOS BLESS、Mac QEMU 或 UB500，也未执行新的一轮长时 soak。

## 后续定位：SSH responsible process 与 LaunchServices owner

上述直接 spawn 的 TCC 失败现已找到系统级原因：2026-09-09 15:27 的 `tccd` 日志显示应用的 responsible process 为 `com.apple.sshd-keygen-wrapper`，并明确输出 `Policy disallows prompt`。这不是用户漏点允许，也不是 CoreBluetooth RF 故障。

维护中的 macOS 自动化已改为：

1. `launch-app.swift` 通过公开 `NSWorkspace` API/LaunchServices 启动真正的 app bundle，持有返回的 `NSRunningApplication`。
2. `macos-owner.js` 分配临时 loopback 端口，并要求新 app 的精确 PID 实际监听该端口、`/status.value.ready` 为真，才将连接交给 WDIO。
3. macOS WDIO 使用普通 WebDriver Classic HTTP 连接到同一 `tauri-plugin-wdio-webdriver` embedded server，不使用 npm service 的直接 spawn owner；Windows/Linux 保持原 service。
4. stdin EOF、父 Node 被结束、正常完成、启动失败或晚到完成均走实例级清理。先 terminate，再有界 forceTerminate；无法确认时明确失败，不按进程名批量结束其他实例。

这改变的是测试应用的启动/清理 ownership，不增加生产 BLE API、不手改 TCC、不绕过用户授权。原 npm provider 缺少 attach 入口不妨碍使用标准 WebDriver 协议连接公开的 embedded endpoint。

实际验证：新路径下 demo 为 3 passed / 3 skipped，应用退出已确认；owner 生命周期场景覆盖精确实例隔离、Node SIGKILL 后 EOF 清理、启动中 EOF、启动失败及无响应 app 强制退出并通过。独立入口为 `pixi run just tauri macos-owner-test`，不需要先通过 native E2E，也不初始化 Bluetooth。

新owner下首次native E2E在30秒Initialize观察窗口超时，但**2026-09-09 15:38:30**系统日志已确认responsible是实际app，并出现`AUTHREQ_PROMPTING` / `display_prompt` for `kTCCServiceBluetoothAlways`。该次只证明SSH启动归属问题修复、进入真实弹框流程，测试退出时owned app已清理。用户随后允许Bluetooth，native E2E实际2 passed / 4 skipped（无RF）；至此macOS Tauri TCC blocker关闭，不再记录为当前未授权。

一次同时运行两个 Apple prepare 流程导致 `.work/staged/apple-production` 正在重建而暂不可见，iPhone Xcode 因此 package resolution 失败。改为串行重试后依赖解析恢复，最终仍为 `errSecInternalComponent`。当前共享 staging/build 工作流必须串行执行，不能把编排竞争当作 iOS backend 故障。所有需要钥匙串密码的操作仍由用户本机终端完成。

## 授权后的扫描失败与首次完整Tauri RF通过

授权后的首次Tauri scan仍失败，截图已有`scan_started`和成功`command_completed`，但没有广告。这说明扫描已开始并完成start command，不说明发现了匹配fixture。定位发现E2E `before`在第一次scan前没有执行`android-ensure`；原生RF HIL退出会stop Android Peripheral，因此不能依赖上一轮留下的advertising状态。该失败不是新的TCC问题，也没有证据要求修改backend。

协调主线程先重跑`pixi run just apple rf-macos-android`，现有`all` RF再次通过；随后串行`pixi run just apple runtime-ios-android` retry仍在CodeSign以`errSecInternalComponent`失败，未进入iPhone RF。

E2E编排修复限定为：

- RF-only `before`执行一次`android-ensure`并等待advertising ready，再进入首扫。
- `after`恢复`android-radio-enable`与`android-resume`，包括部分准备失败后的恢复。
- 可见flow等待及时传播早期UI错误；故障测试保留`allowError`，不会把预期native fault改成普通成功或提前误判。

首次修复后执行：

```text
pixi run just tauri tauri-native-rf
```

**实际6 passing / 0 skipped，约1m35。** 覆盖首次完整可见flow、2次fresh-ID repeat、10cycle、discovery radio loss、active-subscription passive loss/recovery以及dispose。结束时确认owned app PID已清理，Android `STATE_ON`且advertising ready。此次未改backend、未放宽通过条件；这是首次完整Tauri RF通过，而非demo、原生HIL结果转记或长时soak。

## 后续UI诊断candidate：component通过后，最终RF重跑通过

首次RF通过后，主线程仅增强UI timeout诊断：错误包含缺少的evidence、`command_id`及scope；scan明确报告`matching fixture advertisement_observed`缺失，不再只给出泛化timeout。此改动不将scan start/terminal提升为扫描业务成功。

新增回归输入`scan_started`与`command_completed`，但不给有效广告，只提供旧session或明确`connectable: false`的观察；要求timeout且不调用connect。UI component **19项全部通过**。

在component通过时，前一编排修复candidate的RF证据未继承给诊断candidate，后者当时仍待重跑。协调主线程随后实际执行`pixi run just tauri tauri-native-rf`，最终UI诊断candidate **6 passing / 0 skipped**，测试耗时 **1m34.6s**，WDIO总耗时 **1m36**。

| 最终重跑关联项 | 实际记录 |
|---|---|
| WebDriver session | `c08a7a47-e9f8-4060-97e7-95954b147566` |
| Owner app PID | `94302`，确认退出 |
| Peripheral收尾 | Android `STATE_ON`，advertising ready |

最终candidate的RF待验证项由这次真实重跑关闭，不是对首次pass的外推。此前TCC、缺少广告和首次修复通过的历史顺序保留。该短RF重跑不证明长时soak；iOS真机签名blocker仍在，iOS Tauri运行/开发签名/RF与Windows验证未执行，这些边界不变。
