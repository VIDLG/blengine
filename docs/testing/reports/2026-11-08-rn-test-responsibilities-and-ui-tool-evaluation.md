# RN 测试职责整理与 Detox / Maestro 准入评估

日期：2026-11-08。范围是用户允许的职责优化与候选 UI 工具评估，不是第二份架构计划、正式工具迁移或新发布 gate。[Plan](../../../.plan/PLAN.md) 仍是唯一验收来源。

## 结论与本次执行范围

- **先落实职责与执行隔离**：dependency setup、组件 Jest、native bridge、host/controller、UI E2E、RF 各有 owner；不把原有跨层验收删成 smoke。
- **Detox：暂不正式采用**。有 Android 限定实验的源码可行性，但当前 RN 0.87.1 / React 19.3.0 / Nitro 0.37.1 的完整兼容性未证明，真实 iPhone 不在已核实支持范围。不能为了使用框架降级 RN、改 ABI 或 fork 生产桥。
- **Maestro：CLI 2.12.1 的受控 Android idle 实验已有同 session 的 UI、身份复核与完整 cleanup passed**（`1c3b6973-1fee-493c-882b-667ee0f1a243`），但 **`host_gate_status=not_evaluated / rf=not_run`**。七次 tap 禁用 no-change 重试，原 PID 有七组 request→commit，九项设置与 DUT 权限恢复/保持 baseline，App 按成功策略保留。此前预检失败与提前设置断言失败均原样保留；有限实验不证明优于 Appium、原 Close/Evidence BUG 已修复或正式 RF 全链通过。
- 现有 Appium 保持唯一 maintained UI driver；工具候选不接管 BLE 操作语义、firmware/权限/radio 策略、lease、原身份或最终 evidence verdict。
- 本次下载并隔离解包 CLI；先后记录首次只读预检失败、旧 App 退休后的 UI pass/host failed，以及用户再允许继续后的修正版本完整 idle 实验 pass。每次执行各有独立 attempt，显式安装/清理 reviewed helpers，不失败自动重试。最新实验从已确认 App 缺席的状态安装/launch 同一 APK。没有修改 DUT 蓝牙权限/radio、全局 OEM policy、签名/SDK或重启共享 ADB，未重跑 RF。原 [scan failed 与 idle control passed](2026-11-08-formal-android-managed-scan.md) 不改写、不合并。

后续证据：[Maestro maintained idle](2026-11-09-formal-android-maestro-idle-integration.md)、[Maestro lifecycle comparison pass](2026-11-09-formal-android-maestro-lifecycle.md)、[同 APK Appium failed 与原 quarantine 独立恢复](2026-11-09-formal-android-appium-comparison-recovery.md)。这些新候选不改写本报告的历史实验或把 Detox 源码评估升级为真机通过。

## 1. 已落实的执行边界

### 显式 setup，不把 npm ci 藏在日常检查里

[`formal-host-check`](../../../just/react-native/mod.just) 不再依赖 `formal-host-install`；Android/iOS build 仍依赖 check，保留 typecheck 与正式 autolinking 验证。初始 checkout、lock 变化和干净候选任务应显式执行 install，然后 check/test/build。没有删除 install 或改写依赖锁，也没有通过 `--no-deps` 跳过检查来掩盖问题。

```sh
pixi run just react-native formal-host-install
pixi run just react-native formal-host-check
pixi run just react-native formal-host-test
```

后两条用于已 setup 的日常软件回归，不 build、不准备手机。build 的 Gradle/CocoaPods 等既有解析语义不因此变成强制 offline；本改动只移除隐藏的 npm ci 前置。当前 lock 的正式 file dependency 为 `link:true`，本机 installed package 也是指向正式 integration 的 symlink；没有把本地 source consumer 改成使用一份陈旧复制包。干净 artifact consumer 仍走自己的 package 隔离检查。

调用方审计同步修正 `bleng_ios_consumer.py` 的两条缺 installed-package 提示：应指向显式 `formal-host-install`，不再提示一个已不安装依赖的 check。先改 admission 回归复现两种 missing 输入的错误提示不符，再改提示；原“在任何 subprocess/generation 前拒绝”断言保留。未增加 iOS 构建、安装或 signing 动作。

### 组件 discovery 不加载设备 E2E bootstrap

正式 [`jest.config.js`](../../../tests/consumers/react-native-bleng/jest.config.js) 只发现 `src/**/*.test.[jt]s?(x)`。未来 device E2E 必须有独立配置/入口，不能被组件 Jest 装载；当前没有新增 Detox Jest 配置、依赖或默认入口。

回归使用实际安装的 Jest discovery engine、临时 source 与 device-bootstrap 文件，执行 `listTests`，不执行 fixture 内容，也不启动 SDK/设备。原默认 discovery 会收进两个 device bootstrap；修正后只保留两个 source fixture。无 Node/明确安装的正式 Jest 时该检查会明确 skip，本机本次实际运行，未 skip。

### Synthetic support 不依赖 TestCase 模块

扩大现有 [`test_layout`](../../../tools/tests/react_native/test_layout.py) 时，原规则发现九处跨测试模块/fixture 依赖。没有放宽 AST 检查；按既有 owner 提取已有样本并改引用：

| 原责任 | 归属 | 保留的边界 |
| --- | --- | --- |
| Android helper preparation harness 与 command samples | `tools/tests/react_native/support/android_preparation.py` | 仍从实际 SDK/subprocess 边界隔离，保留原 scope/rollback assertions |
| 设备本地 capture emulator、有限 pipe 与样本 | `tools/tests/react_native/support/android_device_capture.py` | Device 仍使用注入的 case assertions，不创建 TestCase 或真实设备命令 |
| lifecycle bundle 的文件样本 builders | `tools/tests/react_native/support/lifecycle_evidence.py` | 显式 caller-owned root；不借另一个 TestCase.setUp/cleanup 构建样本 |
| 只读 Just recipe expansion | `tools/tests/react_native/support/recipes.py` | 只调用 bounded `just --dry-run`，不执行 recipe 或 placeholder SDK 参数 |
| Peripheral ServiceWire | 既有 `tests/peripherals/nrf52840/support/direct_fixture_resources.py` | 不搬进 RN support，不复制/改变 BTP 字节或 live provider |

原用例、故障注入、SDK mock、wire 字节和独有安全断言保留；没有为了计数减少测试。support 不导入 `test_*`，不继承/实例化 TestCase；live code 不导入 synthetic support。完整职责表维护在[分层指南](../../research/cross-cutting/testing-layer-responsibilities.md#21-正式-rn-consumer-的具体责任落点)与 [consumer ownership](../../../tests/consumers/README.md)，本报告不另建通用 runner/driver/cleanup 框架。

## 2. Detox：Android 有实验路径，尚无正式采用资格

### 固定证据与兼容性

本次官方 latest-release API 返回 [Detox 20.51.3](https://github.com/wix/Detox/releases/tag/20.51.3)；窄源码审计固定该 tag，不下载/克隆完整仓库。此前读取的官方 README 列出 New Architecture 已验证 RN 0.77–0.84，更新版本未充分验证。tag 中开发依赖和版本识别分支不能替代完整测试矩阵。

- [`ReactApplicationExt.kt`](https://github.com/wix/Detox/blob/20.51.3/detox/android/detox/src/full/java/com/wix/detox/reactnative/ReactApplicationExt.kt)、[`ReactNativeInfo.kt`](https://github.com/wix/Detox/blob/20.51.3/detox/android/detox/src/full/java/com/wix/detox/reactnative/ReactNativeInfo.kt) 有 Fabric/reactHost 和 RN >=0.84 new-architecture-only 路径。这证明实现考虑该架构，不证明当前 RN 0.87.1 + Nitro 组合可运行。
- [`detox/package.json`](https://github.com/wix/Detox/blob/20.51.3/detox/package.json) 的开发 RN 为 0.85.2；manifest 自身版本含 `20.51.3-smoke.0`。本次审计的是 release tag 源码，未核对实际 npm/Maven 发布物，不将 tag/version 名当产物同一性证明。
- 已核实的 [iOS setup](https://github.com/wix/Detox/blob/20.51.3/docs/introduction/partials/_project-setup-devices-ios.mdx) 使用 `ios.simulator`/simctl；官方 README 的 physical iOS 限制仍在。不能替换本项目真实 iPhone BLE gate，也不新增 Simulator 必跑门槛。

### Android 接入与同步成本

[官方 Android setup](https://github.com/wix/Detox/blob/20.51.3/docs/introduction/partials/_project-setup-apps-android.mdx) 需要现有 App APK + instrumentation APK/Detox bootstrap。当前正式 consumer 已用 AndroidJUnitRunner，没有从 runner 类型上直接冲突；若试验，应分别精确选择 bootstrap class，保留现有 `HybridBlengSessionOwnerInstrumentedTest` 的 class filter，不混跑、不增加第二份 production image。

[`Instrumentation.js`](https://github.com/wix/Detox/blob/20.51.3/detox/src/devices/common/drivers/android/tools/Instrumentation.js) 和 [`instrumentationArgs.js`](https://github.com/wix/Detox/blob/20.51.3/detox/src/devices/common/drivers/android/tools/instrumentationArgs.js) 支持这一入口隔离方向，但未在本项目执行验证。不能复制文档中的动态 `com.wix:detox:+` 或旧 SDK 示例。

[`AndroidDriver.js`](https://github.com/wix/Detox/blob/20.51.3/detox/src/devices/runtime/drivers/android/AndroidDriver.js) 包含安装、instrumentation、reverse 和 reset/permission 机制，不能直接充当现有 attach-only owner。真实采用仍需证明不隐式清 DUT 数据/grant BLE、拒绝未知 App、收尾无泄漏且成功 Evidence 页保留。

[`Detox.java`](https://github.com/wix/Detox/blob/20.51.3/detox/android/detox/src/full/java/com/wix/detox/Detox.java) 对非标准异步工作要求 IdlingResource；[`FabricDetoxIdlingResourceFactoryStrategy.kt`](https://github.com/wix/Detox/blob/20.51.3/detox/android/detox/src/full/java/com/wix/detox/reactnative/idlingresources/factory/FabricDetoxIdlingResourceFactoryStrategy.kt) 的同步不等于 Nitro/Rust actor/BLE operation terminal。没有查到当前精确组合的通过证据，不宣称 Nitro 不兼容，也不宣称所有 native Promise 会自动被跟踪。不能把长时间 scan 注册成阻止所有 UI 操作的“busy”来替代取消/stop 语义。

**采用前提**：固定实际发布物且当前依赖不降级；独立 bootstrap/组件 discovery；原 native contract 仍通过；真实 Android 正常与失败路径有界；无隐式 reset/grant；owner/cleanup 与成功页保留成立；无需长期 fork/全面关闭同步且有可测维护收益。满足这些也只允许讨论 Android 限定采用，不补 iPhone/RF 缺口。

## 3. Maestro：值得小试，不应直接裸 CLI 接管手机

### 固定版本与实际 setup/teardown

本次 [official release API](https://api.github.com/repos/mobile-dev-inc/maestro/releases/latest) 返回 [CLI 2.12.1](https://github.com/mobile-dev-inc/Maestro/releases/tag/cli-2.12.1)，tag 指向 commit **`67d8a677029261fa424a45a10d08eacd6aa0deed`**。以下核对固定该 commit：

- [`AndroidDriver.kt`](https://github.com/mobile-dev-inc/maestro/blob/67d8a677029261fa424a45a10d08eacd6aa0deed/maestro-client/src/main/java/maestro/drivers/AndroidDriver.kt)：`open()` 安装 `dev.mobile.maestro` 和 `dev.mobile.maestro.test`，启动指定 AndroidJUnitRunner instrumentation 与 gRPC 服务；不是仅连接 DUT 的纯 host API。
- 默认 `reinstallDriver=true`，安装前可能卸载同名包；`close()` 也卸载 helper、关闭 instrumentation/connection。`bestEffortUninstall` 可在 operation failure 后记日志而不向外重抛最终卸载失败，不能仅以 CLI exit 0 充当 exact cleanup 证明。
- location/proxy/orientation 等命令另有 grant/AppOps/settings 副作用；这些源码分支不代表导航小试必然执行，也不能因此将全工具一概拒绝。小试必须禁止无关命令并记录实际 setup/恢复。
- 初始 [`TestCommand.kt`](https://github.com/mobile-dev-inc/maestro/blob/67d8a677029261fa424a45a10d08eacd6aa0deed/maestro-cli/src/main/java/maestro/cli/command/TestCommand.kt) 与[官方 CLI reference](https://docs.maestro.dev/maestro-cli/maestro-cli-commands-and-options.md) 未核实到 offline `validate`/`test --dry-run`；随后实际发布物提供了独立 `check-syntax`。已用它验证本次 flow（`.work/maestro-flow-syntax.log`），不将语法通过称为设备 dry-run 或兼容性通过。
- [launchApp 文档](https://docs.maestro.dev/reference/commands-available/launchapp.md) 的默认 stop/permission 语义不适合原样搬入 current-run 测试；即使 `stopApp:false` 也不能自动证明权限不变。小试不使用 launch/reset/permission 命令。
- [iOS 文档](https://docs.maestro.dev/get-started/supported-platform/ios.md) 的已核实执行范围是 Xcode Simulator，不作为真实 iPhone 支持证明。framework-agnostic 与零 DUT npm 依赖也不等于没有 companion driver 或 OEM 风险。

release asset `maestro.zip` 为 **349,832,427 bytes**，本地完整 SHA256 已匹配 API 的 **`54dbb0716425626604973cbb374c585ca30272375d9ce15177a1152765ddd9d7`**；隔离解包后 `--version` 返回 **2.12.1**。首段下载超时的 partial 与日志保留，后续 Range 续传后校验完整文件；未将 partial 或 metadata 计作通过。产物与 host home 均留在 ignored `.work/`，未加入 maintained toolchain 或 DUT dependencies。

实际 JAR 内两个 helper APK 已提取、校验 manifest 与 SHA256；后续单次 idle 实验使用项目 root-aware installer 显式安装（`grant=False`），最终按 exact 安装身份清理：

| helper | bytes | SHA256 |
| --- | --- | --- |
| `dev.mobile.maestro` | 11,736,549 | `a7f12bbd6211825e743ef0ed729ce5d484a3c713210f28e21ab638413ee1f0b9` |
| `dev.mobile.maestro.test` | 886,083 | `c8bc0a3a299eca612291fb0b14db68de97965126c65cb8afdf4f0d946131aebf` |

静态审计确认：`--reinstall-driver=false` 在 helper 缺失时仍会安装，结束时保留 helper；因此不能靠这个选项宣称零安装或清理完成。实验 flow 禁用 `tapOn.retryTapIfNoChange`，无 launch/reset/permission/BLE 命令。实际 CLI 环境禁用 analytics/update check，隔离 Java home/prefs，移除 SDK 环境变量及 PATH 内 `adb`，指定 `--host 127.0.0.1`；本次运行的 guard 未观察到共享 5037 daemon 身份变化或 5038 fallback listener，不把轮询观察宣称为瞬时变化的绝对排除。

### 值得测的最小问题

如果继续实验，先比较 **同一正式 Android App 的 idle 页面导航**，不同时迁移 BLE 场景和 ledger：Tests → Evidence → Current run → Tests，每步核对 idle/page；记录真实点击/日志/UI 结果和 cleanup，保持候选与 session 独立。不需要第二 App、云服务、第二手机或新 production test API。

实验前必须有明确、审计过的 CLI/helper 产物和 setup/teardown scope，唯一 phone lease、无未知自动化 owner，按原 receipt 显式处理 retained App；不能 `maestro test` 裸接管已有成功页。安装与设备动作是独立显式步骤，不隐藏在 ordinary check/build。若需要重启共享 ADB、修改全局 OEM policy、隐式 DUT grants/reset 或构建新的通用 owner 框架才能运行，先停止评估收益，不为“用了成熟工具”扩大副作用。

**停止条件**：当前候选无法被支持；helper/设置不能收尾验证；App/runtime 被意外替换；无法保存真实 primary/cleanup 错误；靠 retry/reset 掩盖单次投递问题。一个导航 pass 不证明 BLE/RF 或能解除 HANS。先比较部署成本、稳定性、故障定位和维护收益，再决定是否替换 UI 层；不同时长期维护三套默认框架。

### 单次预检失败与受控软件修正

实验 attempt 为 **`09166ceb-1e18-4bc0-9517-361886562962`**，证据位于 `.work/maestro-idle-poc/09166ceb-1e18-4bc0-9517-361886562962/`。预定 flow 为七次 idle 跨页导航，最终停在 Evidence；不是 RF 或正式 host gate。

- **真实 primary**：实验脚本自行使用了不受此 ROM 支持的 `dumpsys activity instrumentation`，SDK exit 0 但 stdout 为 `Unknown command: instrumentation` / `Bad activity command`。脚本拒绝该响应，在 baseline 完成、helper 安装、旧 App retirement、新 App launch 和 Maestro CLI 启动之前停止。
- **真实 cleanup**：同一错误查询再次导致 final check 失败，原 `phone.json` 为 `quarantined`；`result.json` 为 failed。两者均保留，不改写为 passed。不存在 Maestro 导航耗时、点击结果或 JUnit 通过记录。
- **显式恢复**：确认 attempt host PID 已退出、七份 SDK receipts 只有只读命令、无 helper/candidate/CLI request，读取设备支持的 `activity -h` 与 `activity processes`，核对原 App 身份、无 Maestro helper/forward、5037 daemon 仍 PID **16841**、5038/8937 无 listener。随后仅将该 attempt 的 exact lease 归档至 `quarantined-lease-retired/`，活动 lease 清空；`operator-recovery*.json` 独立记录恢复，不覆盖原失败。
- **修正边界**：没有新增通用 ADB/解析框架或修改 production core。ignored 实验脚本改用既有 [`parse_active_instrumentation`](../../../tools/android/automation_observation.py) 与既有 `MAX_PROCESSES_OUTPUT_BYTES`，只对 exact `activity processes` 查询使用原 owner 的独立上限；删去重复的 header-only 解析。该修正不等于原 Appium Close/Evidence 问题的根因修复。
- **离线验证**：真实保存的 **1,412,999-byte** processes dump 被原观察器解析为零条 instrumentation；原 unsupported-command 响应及三个截断样本均拒绝。`offline-preflight-regression.json` 记录零新增设备命令。实验脚本 `py_compile` 通过，失败版本另存 `run.failed.py`；本轮没有自动重跑。

首次只读预检结束时，retained App 仍为原 idle-control 的 `com.blehubblenghost`、PID **25327**、UID **10378**、start ticks **243249699**、boot **`35df0f9f-6c54-42b0-800c-3c410469b6e7`**、APK **`cf10697246fd4a7d141182a295dd378865637ac16c9c06d25d540cd3b1814a9c`**。该次只读身份核对不重新证明页面可见性，原 Evidence 页面未被该次操作改变。后续单次 idle 实验已按该 receipt 显式退休旧 App，见下一节；不再把它当作当前 retained App。当时执行的 ignored 实验使用了现有 candidate 的私有身份 checkpoint，没有伪造 WebDriver 或扩大 production API；后续软件修正已改为公开的 host-tool checkpoint，见下文，未将该新版本称为已上机。

### 修正预检后的单次 Android idle 实验

用户再次允许继续后执行 attempt **`e5ee09fb-4da8-4af9-b0cd-db5f03f42eb6`**；完整输出为 `.work/maestro-idle-poc-corrected-host.log`，产物位于 `.work/maestro-idle-poc/e5ee09fb-4da8-4af9-b0cd-db5f03f42eb6/`。该次失败没有自动重试；后续用户再次允许继续后，另立当前完整 idle 候选。

| 层次 | 实际结果 |
| --- | --- |
| CLI / flow | CLI exit **0**，进程耗时 **32.800s**；JUnit **1 flow passed / 27.539s**，failures **0** |
| UI 导航 | `Evidence → Current run → Tests → Evidence → Current run → Tests → Evidence`，每步 page/idle 断言通过；最后 `No run has been requested.` 可见 |
| SDK 官方产物 | `commands.json` 七条 tap 全部 COMPLETED，`retryIfNoChange=false`；device logcat 中原 PID 的 **14 条、七组 request→commit** 顺序匹配。React commit 不是 native frame/accessibility ACK，页面可见性由 flow 自己断言 |
| host primary | CLI 返回后、helper 完整退场前的设置比较失败：`secure/accessibility_enabled` 为 **1**，baseline 为 **0**。原 `result.json` 保持 failed；原 primary 仅记录空消息 `AssertionError`，由保存的 receipts 与执行脚本离线定位，不伪造原 traceback |
| cleanup | `cleanup_errors=[]`、`phone_cleanup_errors=[]`；两个 helper 被 exact force-stop/uninstall，UID 进程消失，最终 instrumentation 空、forward 空，九项设置恢复 baseline；五份 DUT UID/Bluetooth-permission 快照语义一致 |
| App / phone | 原 PID 25327 已明确退休；本次 App PID **21158**、UID **10378**、start ticks **244137424**、相同 boot/APK。因 primary failed，candidate finalization 按原身份 force-stop 并验证缺席，**没有保留成功页面**；phone journal 为 failed（非 cleanup quarantine），活动 lease 已释放 |
| BLE / gate | **RF not_run / host_gate not_evaluated**。未 Start/scan/Close，不是原 Close→Evidence 或正式 scan 全链 pass |

官方证据为 `artifacts/2026-11-08_224846/idle-navigation/{commands.json,logs/device-logcat.txt}` 和 `junit.xml`。独立的 `posthoc-observations.json` 保留离线提取、哈希与原 failed，不覆盖原 verdict。设置的真实观测为 **0 → 1 → 0**；与 UiAutomation 生命周期有关是当前推断，本次没有在中间时间点保存 active-instrumentation snapshot，不能把因果关系写成已验证平台事实。未执行 `settings put/delete` 或 blanket reset 来恢复。

设备 logcat 同时保存了 `waitForWindowUpdate` 的 **500ms timeout** 和 `Could not detect idle state` warnings；不删去或称没有 warnings。它们出现于 helper 的窗口观察路径，本次 flow 仍实际通过；不是原 ADB `service_accept` timeout，也不据此宣称 UI 框架消除了所有 idle/OEM 风险。

#### 提前设置断言后的实验脚本修正

- 保存 CLI 后设置快照；恢复 verdict 放在 owned helper/instrumentation 完整 teardown 后。任何最终设置漂移仍保留 cleanup error/quarantine，不将 active 期快照或 CLI exit 0 当清理证明；不新增 settings 写入或重试。
- 导航事实复用 Maestro 官方 commands/logcat，而不是再发一套 `logcat` 命令。只接受一份 export、七个指定目标、no-change retry 全关闭、原 PID 七组且时间递增的 request/commit；不要求 CLI 开始捕获前的初始 Tests commit。
- 后续异常保存完整 traceback。已执行版本另存 `run.executed.py`，原空消息 primary 不事后改写。
- 离线 reader 先遇到 Python tuple 与 JSON array 的表示差异，已按 JSON 的 `[uid, permissions]` 语义比较；五份保存快照的 UID/grants 均一致。原分析失败日志 `.work/maestro-idle-artifact-offline.log` 保留。
- 修正后的真实产物回放与故障注入 **8 tests passed / 0.015s**，覆盖真实 export、未完成命令、启用 retry、错目标、缺 commit、foreign PID、时间乱序、重复 export。日志 `.work/maestro-idle-artifact-offline-green.log`；零设备命令，仅隔离执行实验脚本的纯 reader，**不是新 runner 的真机通过**。

**该次执行后的采用结论**：Maestro 在当前 Android/RN release 上已有受限 UI 可行性证据，值得下一步评估受控集成；尚不提升为 maintained 默认入口。现有 Appium idle-control 也已通过，不能用两次不同 session 排名稳定性。若继续集成，优先复用官方 CLI、flow、commands/logcat/JUnit，host 仍只负责 exact App/phone/helper 生命周期与独立 verdict；后续公开 host-tool 身份 checkpoint 与当前完整 idle 候选见下文；不能把当时私有调用或 ignored 脚本直接当正式架构，也不将后续 pass 回填为该次 failed 的 pass。Detox 本轮未运行，真实 iPhone 仍不能由此实验替代；internal 能力迁移和 consumers 清理条件不变。

### 运行期身份 checkpoint：解除 WebDriver 耦合的软件边界

[`AndroidCandidateOwner.inspect_running_identity`](../../../tools/react_native/bleng_android_candidate.py) 新增一个窄的只读 host-tool 接口，复用原 SDK/lease/frozen receipts/original launch identity，不新增 driver registry、ADB 协议、UI 场景、public BLE ABI 或第二套清理实现。它拒绝 launch 前/retire 后、延长 deadline、重启/PID reuse、失权与 frozen provenance 变化；不修改 bind 状态，不签发 automation/session authority，也不认领未知进程。原 Appium `bind` 与 finalizer 保持不变。

- 仅 checkpoint 预算到期允许独立 cleanup deadline 下重新验证原启动身份；提前 transport timeout、非 timeout transport failure、identity/receipt/lease 失败均粘性失权。不能恢复文件或等待设备重新可达后自行恢复 authority。
- Maestro ignored 脚本已改为调用该公开接口，移除私有 `_read_identity` 依赖；执行过的 `run.executed.py` 保持原样。该阶段新脚本 `py_compile` 通过，未运行设备实验；之后上机的版本与独立 attempt 见下一节。
- 新测试位于既有 [`test_bleng_android_candidate.py`](../../../tools/tests/react_native/test_bleng_android_candidate.py)，沿用真实临时 lease、mock 实际 `android_sdk.subprocess.run` 的隔离边界；未接触真实设备或用 always-success SDK mock 代替命令校验。canonical `tools-test` discovery 自动包含这些测试。
- 先 red：**7 tests / 11 errors**，均因缺少新方法（包含 deadline subcases），日志 `.work/android-independent-identity-red.log`。最小实现后 **7 passed / 2.056s**（`-k driver_independent`，`.work/android-independent-identity-green.log`）。
- 最终定向新增边界 **9 passed / 2.517s**：fresh 13-command snapshot、无自动化绑定/设备 mutation、拒绝重启与重认领、命令中途失权、窄预算超时后的独立清理、提前 timeout 与非 timeout transport failure 的 sticky authority、deadline 不延长、frozen APK 和 install log 篡改。日志 `.work/android-independent-identity-final-targeted.log`；测试未更改原独有回归。
- 同一实现的相关组合 **100 passed / 197.589s**，实际命令如下，日志 `.work/android-independent-identity-regression.log`。此组合包含最初七个新测试，最终又增加的 transport/install-log 两个测试包含在上述九项定向结果中；数量不相加冒充覆盖率，也不宣称整个 tools-test 已通过。

```sh
pixi run --locked python -W error::ResourceWarning -m unittest \
  tools.tests.react_native.test_bleng_android_candidate \
  tools.tests.react_native.test_android_sdk \
  tools.tests.react_native.test_bleng_android_prepared_scan \
  tools.tests.react_native.test_android_managed_host -q
```

本次还实际执行 `pixi run --locked just react-native formal-host-check`（typecheck/autolinking/no-internal 通过）和 `pixi run --locked just react-native formal-host-test`（**57 passed / 2 suites**）；日志 `.work/android-independent-identity-host-{check,test}.log`。`pixi run --locked just fmt`、`pixi run --locked just lint`、`pixi run --locked just docs-check-mermaid`、`git diff --check` 与两份 authored Markdown 的 UTF-8/fences/whitespace 检查通过；日志 `.work/android-independent-identity-{final-fmt,lint,docs}.log`。没有新增工具安装、构建或设备命令，以上不是 native/runtime/RF 结果。

独立只读 review 提出的 transport 与 receipt 定向覆盖已补齐。timeout 分支不清除既有 sticky failure；预算先耗尽而未执行 ownership 检查，不等于已观察到 ownership failure，也不宣称原子 attestation。

### 当前完整 idle 候选：UI、公开身份 checkpoint 与 cleanup 同 session 通过

用户再次允许继续后，执行一次修正版本：**`1c3b6973-1fee-493c-882b-667ee0f1a243`**。完整输出 `.work/maestro-idle-complete-host.log`，证据目录 `.work/maestro-idle-poc/1c3b6973-1fee-493c-882b-667ee0f1a243/`；执行脚本保存为该目录的 `run.executed.py`。没有失败后自动重试，也没有重建 APK。

开始前读取前一 failed attempt 的 result/phone/finalization receipts，确认其原身份 force-stop 已验证、清理错误为空；在新 phone lease 内再验证原 boot、包/UID 无进程、已安装 APK/path/hash、无未知 instrumentation/helper。只将其当作 predecessor 清理事实，不把已关闭的 PID 21158 或更旧 PID 25327 当运行 App，也不因旧记录而获得新 App/session authority。随后显式安装/launch 同一 reviewed APK，使用公开 `inspect_running_identity()` 读取本轮原启动身份。

| 层次 | 当前同 session 结果 |
| --- | --- |
| CLI | **exit 0 / 30.931s**，取 `cli-result.json`；`result.json.cli_elapsed_seconds=33.909s` 还包含 CLI 后身份/设置/产物检查，不混称进程耗时 |
| JUnit / UI | **1 flow passed / 27.506s**，七次 `Evidence → Current run → Tests → Evidence → Current run → Tests → Evidence`，每步 page/idle 与最终 `No run has been requested.` 断言通过 |
| 官方证据 | commands 中七次 tap 均 COMPLETED、`retryIfNoChange=false`；官方 device logcat 中本轮 PID 的 **14 条、七组 request→commit** 严格匹配，不补造初始 commit |
| 身份 | APK **`cf10697246fd4a7d141182a295dd378865637ac16c9c06d25d540cd3b1814a9c`**；`com.blehubblenghost`，PID **28598**、UID **10378**、start ticks **244323303**、boot **`35df0f9f-6c54-42b0-800c-3c410469b6e7`**；公开 checkpoint 与末次 candidate finalizer 核对原身份，无 rebind/relaunch |
| 设置/权限 | 保存的 active 末期 `accessibility_enabled=1`，完整 helper teardown 后为原值 **0**；九项观察设置等于 baseline，五份保存的 DUT UID/Bluetooth-permission 快照语义一致。没有 settings 写入/blanket reset |
| cleanup | 两个 exact helper force-stop/uninstall 后包/UID 进程缺席；最终 active instrumentation 空、forward 等于原空 baseline、8937 无 listener；共享 5037 仍 PID **16841**，5038 无 listener |
| host 实验 verdict | `status/scenario_status/ui_flow_status/phone_status=passed`；`primary_error=null`、两组 cleanup errors 为空；phone lease 已释放 |
| 成功 App | candidate finalization 为 **retain/passed**，原进程保留。flow 最终 Evidence 可见通过；退场后的持续页面可见性未另加 UI 检查，不把进程存活等同于可见性证明 |
| 未执行 | **`host_gate_status=not_evaluated / rf=not_run`**；没有 Start/scan/Stop/Close 或固件/permission/radio gate，不是正式 scan 全链、native HIL、ABI 或 Phase exit |

离线重放使用**实际保存的执行脚本**中的纯 artifact reader，核对 commands/logcat SHA256、身份/finalization、权限/设置、helper/instrumentation/forward 和结果层次，保存 `offline-replay.json`；零新增设备命令，日志 `.work/maestro-idle-complete-offline-replay.log`。官方产物哈希为：

- commands：`f935d154535a05df5e3fa00d93108175b0e2bbea8602600c6018e9c2726bc369`
- device logcat：`95c7e35f39c5e4a402e41cb831c8dadba6e45aab35d8338a0bca708b53dadb87`

上述重放是实验 scope 的一致性检查，**不是正式 sealed lifecycle/RF bundle verifier 通过**。本次修正证明：设置恢复 verdict 应在 owned helper 完整退场后判定，不能用 CLI exit 或 active 末期快照替代；不进一步宣称已验证 Android 内部设置变化的全部因果链。

该次交付实际复验：`pixi run --locked python -W error::ResourceWarning -m unittest tools.tests.react_native.test_bleng_android_candidate -k driver_independent -q` **9 passed / 2.345s**；相同 unittest 命令不带 `-k`、模块换为 `tools.tests.react_native.test_android_automation_observation`，**16 passed / 0.006s**。`pixi run --locked just react-native formal-host-check` 与 `pixi run --locked just react-native formal-host-test`（**57 passed / 2 suites**）通过。`pixi run --locked just fmt`、`pixi run --locked just lint`、`pixi run --locked just docs-check-mermaid`、`git diff --check` 以及报告 UTF-8/fences/whitespace 检查通过；日志统一为 `.work/maestro-idle-complete-{identity-tests,observation-tests,host-check,host-test,fmt,lint,docs}.log`。这些离线软件检查不扩大设备实验 scope。

**当前采用边界**：Android idle 的公开身份 checkpoint、官方 CLI/UI 证据与 host cleanup 已闭合一个真实 session，后续可进入受控接入评审，无需反复重测 idle 来堆次数。Maestro 仍是 ignored 实验，不新增第三套默认入口；正式工具管理、具名 runner/测试/证据接入尚未完成。Appium 仍为 maintained driver，真实 iPhone/Detox 支持结论不变；不能以此删除 internal 的未迁移能力或弱化正式 scan 的原身份、一次投递、最终 Evidence 与恢复要求。

## 4. 验证与未证明项

新边界回归先 red：**2 tests / 3 subcase failures**，分别确认隐藏 npm ci 和默认 Jest 会发现 device bootstrap（`.work/rn-test-responsibilities-red.log`）。之后改 recipe/config；实际执行 typecheck/autolinking 与原组件 Jest，未安装依赖或访问设备。

扩大 layout 检查最初 **16 tests / 9 subcase failures**（`.work/rn-test-responsibilities-green.log`）；这些是现存 fixture 依赖方向错误，原日志保留，不计通过。迁出 support 并更新 callers 后，recipe/topology/layout/managed CLI **32 tests passed / 1.520s**（`.work/rn-test-responsibilities-boundaries.log`），没有降低规则。

各次定向结果不重复相加为覆盖率：

| 实际验证范围 | 结果 | ignored 日志 |
| --- | --- | --- |
| lifecycle / device-local / HANS offline evidence 三模块 | 99 passed / 5.238s | `.work/support-evidence-tests.log` |
| Android helper preparation | 59 passed / 31.503s | `.work/support-preparation-tests.log` |
| Android device-local capture | 63 passed / 20.270s | `.work/support-device-capture-tests.log` |
| direct fixture / passive close | 79 passed / 0.656s | `.work/support-direct-fixture-tests.log` |
| managed host / managed scan producer | 26 passed / 161.009s | `.work/rn-test-responsibilities-managed-callers.log` |

交付组合实际执行既有 `run_quiet.py --progress-seconds 20` 包围的以下命令，**332 tests passed / 69.759s**（`.work/rn-test-responsibilities-regression.log`），与表中单模块结果重叠，不合算覆盖率：

```sh
pixi run --locked python -W error::ResourceWarning -m unittest \
  tools.tests.react_native.test_recipe_wiring \
  tools.tests.react_native.test_consumer_topology \
  tools.tests.react_native.test_layout \
  tools.tests.react_native.test_run_bleng_android_lifecycle \
  tools.tests.react_native.test_bleng_lifecycle_evidence \
  tools.tests.react_native.test_bleng_device_local_evidence \
  tools.tests.react_native.test_bleng_hans_evidence \
  tools.tests.react_native.test_bleng_android_preparation \
  tools.tests.react_native.test_bleng_android_device_capture \
  tests.peripherals.nrf52840.test_direct_fixture \
  tests.peripherals.nrf52840.test_direct_fixture_passive_close -q
```

另验证：

- iOS consumer admission/生成隔离 + recipe/topology/layout：**23 passed / 1.331s**（`.work/rn-test-responsibilities-ios-setup-green.log`）；此前 missing-package 提示 red 保留在 `.work/rn-test-responsibilities-ios-setup-red.log`。输出的 iOS product 路径来自 synthetic `_run` 边界，不是真实 Xcode build。
- managed lifecycle 的 idle-only、opt-in capture/restore、failed UI-before-DELETE、ADB-probe failure 独立 cleanup 四个具名用例：**4 passed / 47.989s**（`.work/rn-test-responsibilities-lifecycle-final.log`）。没有把该子集计作全部 lifecycle。
- `pixi run --locked just react-native formal-host-check`：typecheck/autolinking/no-internal 通过，未运行 npm ci（`.work/rn-test-responsibilities-host-check-final.log`）。
- `pixi run --locked just react-native formal-host-test`：**57 passed / 2 suites**（`.work/rn-test-responsibilities-jest-final.log`）。
- `pixi run --locked just fmt`、`pixi run --locked just lint`、`pixi run --locked just docs-check-mermaid`、`git diff --check` 通过（`.work/rn-test-responsibilities-{fmt-final,lint,docs,diff-check}.log`）。首次 fmt 的新增 subprocess capture 规范错误已修正，未使用 unsafe fixes。

本次没有重跑此前未完成的全量 tools/lifecycle discovery，也不称所有 tooling tests 已绿；用本次实际定向/组合结果描述该职责整理。`tools-test`/`tools-fixture-test` 的 canonical discovery 保留，新回归在现有目录被自动发现；没有新增只靠手工执行的 test runner。

Maestro 预检修正后另外实际执行：

- `pixi run --locked python -W error::ResourceWarning -m unittest tools.tests.react_native.test_android_automation_observation -q`：**16 tests passed / 0.007s**，日志 `.work/maestro-preflight-parser-tests.log`。覆盖完整/截断/未知/重复 instrumentation、OEM time envelope 等既有安全边界；不是新增设备测试或整体 tools-test 通过。
- `pixi run --locked just fmt`、`pixi run --locked just lint`、`pixi run --locked just docs-check-mermaid` 与 `git diff --check` 通过；日志 `.work/maestro-poc-{fmt,lint,docs}.log`。diff check 另有既有文件的 CRLF→LF 提示，无 whitespace error。

单次 idle 实验及离线结果处理修正后再次实际执行：既有 instrumentation 观察器 **16 tests passed / 0.006s**（相同 unittest 命令，日志 `.work/maestro-idle-completed-parser-tests.log`）；`pixi run --locked just fmt`、`pixi run --locked just lint`、`pixi run --locked just docs-check-mermaid`、`git diff --check` 均通过（`.work/maestro-idle-completed-{fmt,lint,docs}.log`）。本地逐端口检查确认 5037 仍为 PID 16841、5038/8937 无 listener、活动 phone lease 为零；离线分析与后续检查未再次访问设备。

本次没有 Detox 编译、Maestro 正式 host gate 或 BLE/RF 兼容性通过；只有上述单候选 Android idle 完整实验通过，不能由 `check-syntax`、离线回放或该实验结果扩大解释。没有移除正式 scan 的跨页/原 identity/一次投递/最终 Evidence 检查，未修复或重分类原 RF failed。新增工具评估不解除 internal host 的能力/测试/evidence 迁移条件，未进入 core review、ABI freeze 或 Phase exit。
