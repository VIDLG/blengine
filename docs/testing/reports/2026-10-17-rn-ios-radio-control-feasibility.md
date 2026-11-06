# iOS Settings 蓝牙控制可行性：OFF/ON 与同进程返回通过，radio RF 待验证

日期：2026-10-17；2026-10-18 补充只读诊断与独立 smoke；2026-10-19 无镜像对照先遇runner初始化超时，用户完成系统认证后的新attempt只读导航通过。结果：**重新明确授权后的control03实际完成OFF/ON读回与原App同PID/native boot返回，non-RF控制验证通过。旧failed保留；backend PoweredOff事件、radio-transition RF与恢复仍未验证。** 不改变 approved Plan、production API、fixture 协议或既有授权专项结果。

## 当前排期：non-RF 控制子门禁通过，backend/radio RF 待推进

用户已明确重新开放 **`IOS-RADIO-GATE`**，排在当前 firmware/CCC 工作之后；旧 Deferred 决定不再是当前排期。最新[通知观测与 RF 报告](2026-10-19-nrf-notification-observation.md)已记录当前 CCC 的明确阻塞边界，以及新候选 iPhone RecoveryTen 的独立失败，均未放宽为通过。

用户完成系统认证后，Settings导航恢复；control01导航识别失败、control02误选外层wrapper导致OFF读回失败，均保持原failed。最终locator限定到唯一设置行内的唯一真实子Switch，inspect-only04验证可访问且为ON；用户再次明确授权后，control03一次执行成功，真实树按ON/OFF/ON读回，返回检查时仍为原PID/native boot，原closed-Probe结果可见。**这只闭合Settings控制可行性，不证明live backend OFF、同Hub/JS engine持续存活或RF恢复。** 测试结束后目标App退出，未重新启动以伪装同进程或结果页持续可见。无RF、无密码收集或系统安全策略自动变更；不推定认证永久恢复或镜像是旧空树根因。

### 历史暂缓决定

2026-10-18 用户决定：本项统一命名为 **iOS 系统蓝牙开关门禁（`IOS-RADIO-GATE`）**，状态 **Deferred / 暂缓**。本记录继续保留实际 failed/blocked 结果，不改写为 pass。暂缓 Settings/XCUITest/Continuity 的进一步排查及依赖它的 iOS startup-off → ON / active-loss RF 自动化；现有 opt-in 用例/入口暂留，不加入默认 gate，也不因一般“继续”自动恢复。恢复需用户明确点名本专项，再按当时源码、设备状态和新 attempt 重新验证。

此决定不暂停 iOS/RN 常规测试、authorization-only、Android radio/permission、native/runtime/package 或 lifecycle 验证；不删除失败证据，不改变阶段退出条件或宣告 ABI freeze。正式排期决定见唯一 [Plan](../../../.plan/PLAN.md)。该段记录当时的暂缓边界；当前恢复顺序以上方更新与 Plan 为准。

## 范围与复用

沿用同一个 RN workbench、`BleHubNitroHostUITests` 和现有授权初始化/close audit，不新增 App、Python controller、capture/ledger framework 或 RF scenario。入口：

```text
pixi run just react-native host-ios-device-radio-control-test <udid> <team> <fresh-xcresult-path> [inspect-only=0]
```

结果路径相对于 consumer 的 `ios/`；拒绝覆盖已有 xcresult/log/summary。普通 UI discovery 跳过该 opt-in 控制用例。运行期间须独占手机，不与 RF、permission、其他 Settings 操作并发；此入口尚不是集成到 physical lease/evidence bundle 的 RF controller。

完整控制用例的预期顺序：

1. 原生 inspect 确认 App 已授权；不 reset authorization、不处理 consent。
2. 现有 fresh authorized audit 验证 production Probe `PoweredOn + Authorized` initialize/close 和 17-zero。
3. 仅通过 Settings accessibility UI 定位蓝牙页面和唯一开关，确认初始 ON。
4. OFF/readback → ON/readback；tap 前记录 mutation intent，失败时只尝试恢复 ON，不重跑 OFF。
5. 返回原 App，要求 native identity（包括 PID/boot ID）完全一致和原结果仍可见，不主动 relaunch。
6. 保存分离的 primary/cleanup errors、阶段耗时和截图/树。成功才保留原 App 结果页。

**这仍只是 non-RF control feasibility。** 切换期间没有 live Probe；Settings 的 OFF readback 不等于 backend `PoweredOff` 事件，原结果返回不证明 same-Hub、JS engine 持续存活或 fresh RF I/O。最终实现显式记录 `powered_off_backend_proven=false` 和 `rf_recovery_proven=false`。`inspect-only=1` 只激活 Settings、等待导航层级并保存只读附件，永不点击开关；其成功也不是控制验证通过。

## 本机与尝试

- iPhone 14 Plus：`00008110-000C61160C39401E`，iOS `26.6.1 / 23G83`。
- Team：`FWVYQ287W5`。
- 全部尝试通过 `run_quiet.py --no-tty`、`-collect-test-diagnostics never` 执行；没有终端密码输入。
- 没有启动 nRF provider、领取 fixture session、刷机、连接或执行 GATT。

以下路径均相对于 `tests/consumers/react-native-internal/ios/build/`：

| Attempt | XCTest 结果 | 可据以判断的事实 |
|---|---|---|
| `radio-control-20261017-01.xcresult` | 0 passed / 1 failed / 0 skipped | 授权/PoweredOn audit 完成，随后无法定位 Bluetooth Settings；开关 tap 之前失败 |
| `radio-control-inspect-20261017-01.xcresult` | 1 passed / 0 failed / 0 skipped | 首版只读快照采集成功；空树本身不代表 Settings 可控 |
| `radio-control-inspect-20261017-02.xcresult` | 0 passed / 1 failed / 0 skipped | 加入 15 秒导航等待后仍只有空窗口；明确拒绝把快照采集当导航 ready |

完整用例只执行一次；失败后没有自动重跑 destructive path。后两次都是只读诊断。各自 `.log`、xcresult 和导出的 `*-attachments/` 保留，不回写失败结果。最终增加了导航等待、只读模式的前台返回、失败快照、`mutation_attempted` 字段及严格 back-button 匹配；最终源码另行 build-for-testing 通过，但未重跑控制用例。

首轮 host verdict：

```json
{
  "run_id": "8b912d40-0e01-4ea7-817f-854e863621b5",
  "scope": "ios-settings-radio-control-feasibility-non-rf",
  "status": "failed",
  "restored": false,
  "same_native_boot": false,
  "powered_off_backend_proven": false,
  "rf_recovery_proven": false,
  "primary_error": "failed(\"Cannot find Bluetooth Settings through public UI\")",
  "cleanup_error": ""
}
```

该轮只有 `authorized-powered-on-probe-closed` observation（约 11.7 秒）；`restored=false` 不是恢复失败，因为尚未尝试切换 radio。该首轮 schema 尚无后加的 `mutation_attempted` 字段，不 backfill 原证据。native inspect PID 为 `67814`，authorized audit PID 为 `67815`；后者 boot ID `52965525-5886-4535-b717-cbf1bce166dc`。二者是预期的 fresh audit，不是 Settings 返回后同进程验证通过。

## 已观察到的阻塞与未知项

只读附件显示 `com.apple.Preferences` PID `39296`、label `设置`，但 accessibility subtree 仅 Application → Window → Other → Other，没有导航栏、cell 或 switch。截图全黑。增加等待仍无导航层级。

随后独立 `devicectl device info lockState` 读回：`passcodeRequired=false`、`unlockedSinceBoot=true`。因此不能把当前失败直接归因于手机未解锁；也没有证据确定 Settings 进程状态、系统 UI automation 限制或其他系统条件中的哪一个是根因。RN baseline audit 可运行，不表示其他系统 App 的 UI 自动化同样可用。

不采用私有 Bluetooth setter、Settings URL scheme、坐标盲点、Control Center 临时断开或自动重启 Settings/设备来绕过问题。下一次实际开关测试需要新的显式授权和新 result path；先由操作者确认手机上 Settings 是否能正常显示，再作只读验证。若系统 UI 路径仍不可用，应评审人工 host action + backend assertion，不能假造自动控制通过。

## 2026-10-18：runner 认证与 Continuity 场景分开诊断

操作者确认实体 Settings 蓝牙页面可以显示、顶部开关可操作；“其他设备”区域持续搜索但没有结果。该用户观察不是 XCUITest/扫描 RF 证据，也不把设备列表为空判作 backend 故障。

以下新增路径同样相对于 consumer `ios/build/`，原结果不覆盖：

| Attempt | 结果 | 范围 |
|---|---|---|
| `radio-control-inspect-20261018-01.xcresult` | runner 初始化认证失败 | `com.apple.sharing.authentication error 12`、等待响应超时；未执行 Settings 用例，不能算 UI assertion failure |
| `radio-control-inspect-20261018-02.xcresult` | runner 初始化认证失败 | 用户授权后的新尝试，错误相同；未执行 Settings 用例 |
| `ui-smoke-20261018-01.xcresult` | 1 passed / 0 failed / 0 skipped | `testContractLabCompletesAndRecreatesProbe` 实际执行，两轮 5/5 与 Tests → Evidence → Current run；不是 Settings、radio 或 RF pass |
| `radio-control-inspect-20261018-03.xcresult` | 0 passed / 1 failed / 0 skipped | runner 已启动；Settings 导航层级仍不可读 |
| `radio-control-inspect-20261018-04.xcresult` | 0 passed / 1 failed / 0 skipped | 增加 foreground 等待、状态/整屏/SpringBoard 证据；已达 foreground，导航层级仍不可读 |
| `radio-control-inspect-20261018-05.xcresult` | 0 passed / 1 failed / 0 skipped | 仅在诊断分支以公开 Home 按钮导航再 activate 的对照；结果未改善，未将其保留为自动 workaround |

各 `radio-control-inspect` 命令使用同一入口和显式末参数 `1`；没有重跑完整 OFF/ON mutation。Smoke 使用同一 workspace/scheme/UI target，`run_quiet.py --no-tty`、`-collect-test-diagnostics never`、`-only-testing:BleHubNitroHostUITests/BleHubNitroHostUITests/testContractLabCompletesAndRecreatesProbe` 与 fresh result path。认证成功只归属于该次已执行用例，不宣称系统认证永久修复。只读 `DevToolsSecurity -status` 为 enabled，`devicectl ... lockState` 当轮为 `passcodeRequired=false` / `unlockedSinceBoot=true`，未修改这些策略。

### 新证据与结论边界

本机 Xcode 的公开 `XCUIAutomation.framework/Headers/XCUIApplication.h` 说明：`activate()` 不终止已有进程，但返回时可能是 runningBackground 或 runningForeground；`state` 是异步观察值，不能单靠 activation 返回或单个状态证明可操作 UI。现行用例增加 `wait(for: .runningForeground, timeout: 10)`，仍另外要求真实导航栏；没有降低 ready 条件。附件使用公开 `XCUIScreen.main.screenshot()` 和 App/SpringBoard 的状态及 accessibility snapshot。API 还核对了 Apple [XCTest attachments 文档](https://developer.apple.com/documentation/xctest/adding-attachments-to-tests-activities-and-issues)，不调用私有 selector。

`...-04`、`...-05` 读回：

```text
settings_state=4; target_state=1; springboard_state=4
Settings PID: 39296
SpringBoard:
  SBSwitcherWindow:Continuity.main[2-567]+<continuity>
  AppSwitcherContentView
  card:com.apple.Preferences:sceneID:com.apple.Preferences-default
```

`4` 为 runningForeground，`1` 为 notRunning；这些数值按 SDK enum 解释。只读诊断未主动启动 RN App，因此 target_state=1 不证明旧 RF/Probe 在执行中死亡。Settings 虽报 foreground，树仍只有空窗口；`...-04` 的 App 和整屏截图均黑。Mac 只读进程列表同时发现 PID `484` 的 `/System/Applications/iPhone Mirroring.app/Contents/MacOS/iPhone Mirroring`。这是 **Continuity/镜像显示场景可能干扰自动化的具体线索**，不是已经证明的唯一根因。Home 导航对照后，同一场景标记和空树仍在；因此不将 Home 操作写入正常控制路径，不以它替代 readback 或恢复。

Apple [iPhone Mirroring 官方说明](https://support.apple.com/en-us/120421) 说明镜像使用时实体 iPhone 保持锁定，并可通过退出镜像 App 或实体手机解锁停止镜像。这一官方使用约束不直接证明本轮测试失败的因果关系；本轮 lock-state readback 也不能单独排除残留/不同显示场景。

未来明确恢复本专项时，下一对照要求操作者退出 Mac 上的 iPhone 镜像，在实体手机解锁后打开 Settings；不擅自关闭用户镜像窗口，不改为自动认证、不绕过系统授权。随后只读重跑同一入口，新 result path；若排除镜像后仍为空树，继续保持 blocked，不宣告系统 Settings 不支持 XCUITest。未进行该无镜像对照前，不新增硬编码进程存在即拒绝的全局 guard（进程存在并不等于正在镜像），也不修改 AGENTS 为已确定根因。

## 2026-10-19：无镜像、已解锁，runner 初始化超时

用户完成退出镜像与实体解锁后，先后两轮只读预检确认：Mirroring进程不存在、iPhone available且`passcodeRequired=false / unlockedSinceBoot=true`、没有其他xcodebuild/xctest或nRF provider占用。没有把这几个前置条件视为Settings导航或radio控制通过。

为避免重新build/signing触发无关授权，复用`device-ui-derived/Build/Products/BleHubNitroHost_iphoneos27.1-arm64.xctestrun`及既有signed test artifacts。仅在同一ignored Products目录生成fresh `ios-radio-inspect-20261019-01.xctestrun`，向UI target显式写入`BLEHUB_IOS_RADIO_CONTROL_GATE=1`、`BLEHUB_IOS_RADIO_CONTROL_INSPECT_ONLY=1`和唯一test identifier；原xctestrun未修改，前后hash保持一致。没有新增App、controller或修改测试源。

从BleHub根目录实际执行（外层600s；stdin关闭、独立process group）：

```sh
pixi run --frozen --no-install python tools/react_native/run_quiet.py --no-tty .work/ios-radio-control-resume-20261019-01/inspection.log -- pixi run --frozen --no-install -e apple-ci xcodebuild -quiet test-without-building -xctestrun tests/consumers/react-native-internal/ios/build/device-ui-derived/Build/Products/ios-radio-inspect-20261019-01.xctestrun -destination id=00008110-000C61160C39401E -resultBundlePath tests/consumers/react-native-internal/ios/build/radio-control-inspect-20261019-01.xcresult -parallel-testing-enabled NO -collect-test-diagnostics never -only-testing:BleHubNitroHostUITests/BleHubNitroHostUITests/testBluetoothSettingsOffAndRestore
```

结果exit65，约62.67秒结束；不是外层超时。runner PID85036在初始化阶段记录：`XCTHTestOperationCoordinatorErrorDomain/8`，underlying `com.apple.dt.XCTest.XCTFuture/1000: Timed out while enabling automation mode.`，内部future预算60秒。

xcresult summary报告`total=1 / passed=0 / failed=1 / skipped=0`，failure target为`BleHubNitroHostUITests-Runner`，**这是runner失败计数，不是一个Settings assertion实际执行失败**。Session log没有test-case start，导出attachments manifest为空，未生成Settings树/截图或host verdict；未执行test body、授权audit、OFF/ON mutation或RF，不推断失败后的旧Probe/native close-zero。

本地diagnostic进一步证明：testmanagerd control/session握手完成，对PID85036的authorization reply为`1: (null)`；随后runner加载系统XCTAutomationSupport，等待automation mode超时。本次未记录旧attempt中的`com.apple.sharing.authentication error12`，但这不证明自动化服务健康或该旧问题已修复。当前iOS `26.6.1 / 23G83`、Xcode `27.1 / 27A9275`，device developerMode enabled、DDI services available，Mac `DevToolsSecurity -status`也为enabled。不能再把当前失败直接归因于没退出镜像、设备锁屏或developer mode未开；具体automation-mode阻塞原因仍未确定。

证据：consumer `ios/build/radio-control-inspect-20261019-01.xcresult`；ignored `.work/ios-radio-control-resume-20261019-01/`保存`input-receipt.json`、`result.json`、完整运行log、xcresult summary、只读device/Xcode/security信息、postconditions和导出的testmanagerd/runner/session/scheduling logs。导出仅消费已有xcresult，不启动新的设备诊断采集。没有重新build/sign、设置系统授权、启动RF、重启设备/服务、清除pairing或盲目重试。

当前下一步是补充操作者可见的系统提示/自动化服务证据，或评审一次独立runner smoke/人工host action对照；不能从当前信息猜测并修改BLE代码，不重复OFF动作或要求在agent terminal输入密码。若存在系统确认，仍由操作者在系统UI中处理。本次不把phone-live检查或文档验证算作UI/radio/RF pass。

## 2026-10-19：系统认证后只读导航成功

在attempt01失败后，用户明确要求再试一次并观察弹窗。attempt02复用相同既有signed artifacts及inspect-only staging，独立结果为`radio-control-inspect-20261019-02.xcresult`：exit65，约62.397s，同样`Timed out while enabling automation mode`，runner PID85108，未进入Settings test body。xcresult仍将runner故障计为1 failed，不能称为Settings assertion失败。

用户随后明确报告系统要求输入密码，且已在系统界面完成。没有请求、记录或读取密码；该用户观察不补写为此前未捕获的具体认证机制或错误码。新attempt03在相同无镜像/已解锁/无并发条件下，以`ios-radio-inspect-20261019-03.xctestrun`显式设置inspect-only=1，运行相同`test-without-building`与唯一test identifier，结果保存为`radio-control-inspect-20261019-03.xcresult`。

| 项目 | 实际结果 |
|---|---|
| xcodebuild / XCTest | exit0，约5.758s；1 passed / 0 failed / 0 skipped |
| 用例执行范围 | `testBluetoothSettingsOffAndRestore`的inspect-only分支；未进入完整control分支 |
| Settings状态/页面 | runningForeground；NavigationBar identifier=`通用`，有真实cell/button树，不再是空窗口 |
| Attachments | 5份：Settings screenshot、整屏screenshot、accessibility tree、SpringBoard context、inspection-only scope note |
| 其他现场事实 | context含`SBSwitcherWindow:Main`；target_state=2、springboard_state=4，不据此推导旧Probe close或稳定的显示场景因果 |
| 蓝牙开关 / RF | 没有导航到蓝牙页面，没有开关定位或ON/OFF readback，没有radio mutation、live Probe或RF |

附件明确scope为`settings-ui-inspection-only; radio-control-gate-not-run`。截图附件名以bluetooth-settings开头是既有通用命名，不代表已处于蓝牙页面。前后再次验证原/新xctestrun hash不变、手机available/unlocked、无Mirroring/xcodebuild残留及provider marker。

新证据位于`.work/ios-radio-control-resume-20261019-01/`：`retry-02-input.json`、`inspection-02.log`、`xcresult-summary-02.json`，以及`retry-03-input.json`、`retry-03-result.json`、`inspection-03.log`、`xcresult-summary-03.json`、`attachments-03/`。旧attempt01/02不覆盖，不因03成功升级；未修改App/测试源、重新build/sign、重试OFF动作、安装工具或commit/push。

下一步是另行明确执行一次完整Settings OFF/readback → ON/readback与原App identity恢复的non-RF控制验证。既有报告要求新授权与fresh result path；只读成功不能自动授权实际开关动作，也不替代后续独立live-attested radio RF gates。此前runner超时的具体因果仍不超过“用户完成系统认证后，本次已成功”的观察范围。

## 2026-10-19：授权后的完整控制失败与严格 toggle 定位

用户明确确认执行一次完整OFF/readback→ON/readback验证。复用既有signed artifacts，UI target显式`BLEHUB_IOS_RADIO_CONTROL_GATE=1`、`BLEHUB_IOS_RADIO_CONTROL_INSPECT_ONLY=0`，独占手机、无Mirroring/xcodebuild/nRF provider并发。没有启动RF或重刷设备。

| Attempt（consumer `ios/build/`） | 实际结果 |
|---|---|
| `radio-control-20261019-01.xcresult` | exit65，约26.319s；1 failed。授权与PoweredOn初始化/close audit通过，导航耗尽；`mutation_attempted=false`，未点击radio |
| `radio-control-20261019-02.xcresult` | exit65，约31.929s；1 failed。初始ON，OFF读回超时；`mutation_attempted=true / restored=true / cleanup_error=""`；没有OFF retry |
| `radio-control-inspect-20261019-04.xcresult` | 最新严格inner-toggle locator；exit0，约7.724s；1 passed / 0 failed / 0 skipped，真实子Switch读回`1`；不点击radio |

### 两个已观察到的定位问题

control01实际已进入蓝牙页：树中NavigationBar identifier为SwiftUI通用`_TtGC7SwiftUI32NavigationStackHosting`，而非“蓝牙”；旧page-title判断未识别到达，继续返回/进入，最终耗尽六步预算。只修改有界页面识别：可由唯一`BLUETOOTH`设置行和唯一带标识Switch证明到达，继续要求hittable、二值状态和歧义拒绝，不使用坐标或URL scheme。signed build-for-testing通过后才运行control02；前次没有OFF，因此不是重试已发生的OFF动作。

control02又暴露外层wrapper与内层toggle区别。initial-on、primary-failure、cleanup-on-readback三份完整树均为：

```text
Cell identifier=BLUETOOTH
  Switch identifier=BLUETOOTH label=蓝牙 frame={{40,310},{348,28}} value=1
    Button identifier=BLUETOOTH label=蓝牙
    Switch frame={{327,310},{63,28}} value=1
```

原identifier/label predicate只选中外层wrapper。`attachments-02/71A7616D-FC47-43EF-9255-17BB628D073E`为实际`XCSynthesizedEventRecord`，两个pointer event均在`(214,324)`，Settings PID39296，screen428×926。Session log也记录该hit point及SpringBoard/Settings的TouchEventsCompleted和合成完成回调。**该点在内层toggle矩形之外；合成事件完成不代表状态切换。** 没有证据证明它是唯一失败原因，亦未假定修正后必然控制成功。

10次等待期间的wrapper debug值均1；三份完整树中的内层值也均1。无OFF observation，不能由缺省value或采样间隙推导OFF。cleanup的ON predicate成功且活动树没有第二个Tap：**这证明确认仍为ON，不证明cleanup重新打开蓝牙或已发生OFF→ON转换。** `restored=true`按现有verdict字段保留，其含义不得扩大；same-native-boot、backend powered-off、RF recovery均未证明。

### 最终修正与非破坏验证

仅修改 owning [`BleHubNitroHostUITests.swift`](../../../tests/consumers/react-native-internal/ios/BleHubNitroHostUITests/BleHubNitroHostUITests.swift)：

- 保留页面导航识别修正；在唯一`BLUETOOTH` Cell内要求唯一带标识wrapper，再要求**唯一直接子Switch**，返回该子控件用于未来control与readback。
- 继续要求hittable与`0/1`状态；缺失/歧义/未知状态fail closed。不退回外层、不匹配全局无标签Switch、不硬编码触点、不增加OFF retry。
- 现有inspect-only分支在通用导航诊断后，进一步有界进入蓝牙页，只定位/读取同一真实toggle，保存额外树/截图及`scope=settings-bluetooth-switch-inspection-only; switch_value=1; radio-control-gate-not-run`。因此**新版inspect-only会做页面导航，但不会点击蓝牙开关**；旧03的General-only通过保持原范围。

最终signed build-for-testing通过，然后仅运行inspect-only04；已经尝试过一次OFF，所以未自动重跑完整control。两次build复用既有Pods/工具，未pod install/npm install或允许provisioning updates，no-tty/collect-test-diagnostics never保持。23份既有package/lock/privacy文件构建前后逐字节hash一致；UI源另存前后candidate副本。所有改动保持未提交，未push，不修改BLE/backend/App业务语义。

full attempts、build、最终只读inspection及完整input/artifact hash receipts在`.work/ios-radio-control-full-20261019-01/`：`input-receipt.json`、`input-02-receipt.json`、`control{,-02}.log`、`attachments{,-02}/`、`readonly-audit-02/`、`navigation-fix-build.log`、`leaf-selector-build.log`、`leaf-inspection-{input,summary}.json`、`leaf-inspection-attachments/`、`final-result.json`和build preservation文件。原failed xcresults与verdict未覆盖；后续只读ON不补写为此前成功OFF。

最终软件检查：`pixi run --frozen --no-install just react-native tools-fixture-test` **1012 tests OK / 216.114s**；`just react-native host-check`通过；Pixi下host `test:ccc-disable-reject` **6 suites / 157 tests passed**。这些不是控制或RF证明。该失败时点仍需新的明确授权与fresh result path；后续用户确认的control03结果见下节；不重启设备/服务、重置pairing、修改系统安全策略或要求在agent terminal输入密码。

## 2026-10-19：新授权 control03 完成 non-RF OFF/ON 与原 App 返回

用户在inner-toggle只读检查通过后，再次明确确认完整控制。使用**相同已构建UI源码**，SHA-256 `ac206168da9b7b9b60925e35486de3cc236b4181fdad637df7fa33426b987e17`；没有重新build/sign、修改源码、额外OFF retry或启动nRF。fresh staging为`ios-radio-control-20261019-03.xctestrun`，UI target明确gate=1、inspect-only=0、仅选择原用例；actual command沿用上述`run_quiet.py --no-tty`＋`test-without-building`，新result为consumer `ios/build/radio-control-20261019-03.xcresult`，外层600s。

**实际exit0，约27.405s；1 passed / 0 failed / 0 skipped。** host verdict run `2af3811d-6c8d-432e-82c7-763370bb89fe`，scope=`ios-settings-radio-control-feasibility-non-rf`，status=passed、mutation_attempted=true、restored=true、same_native_boot=true，primary/cleanup error均为空。

| 相对开始时间 | 实际 observation / 附件事实 |
|---|---|
| 12.054448s | App授权、PoweredOn初始化及Probe close audit完成 |
| 14.487011s | 初始Settings ON；wrapper与实际子Switch的value均1 |
| 18.313507s | OFF读回成功；wrapper与实际子Switch的value均0，保存独立树/截图 |
| 21.745970s | ON读回成功；两者value均1，保存独立树/截图 |
| 24.562820s | 不主动relaunch，返回原App；PID/native boot与baseline逐字段一致，原closed-Probe结果再次可见 |

baseline authorized App PID **85605**、boot **`6a081432-fb3f-40e4-b4c9-6e15a5fa6d7c`**；与初始inspect阶段预期fresh process分开，不以两次不同phase launch证明same-App。成功返回证明只适用于本次control后的检查时点。

### 明确的测试结束边界

Session log在24.58s进入Tear Down，suite/testBundle随后完成；之后记录token2、PID85605 exit status0和automation connection interrupted。独立post-run `devicectl device info processes`也未找到该PID或BleHubNitroHost。日志未给出明确的最终kill/request发起者，因此不把因果归于某一组件，也不将exit0推导为所有native资源零。

**不能宣称成功结果页在测试退出后仍持续可见。** 没有重新launch目标App来伪装原PID保留；返回时的同进程/结果截图、测试结束后的进程退出分别保留。这不反向改写用例内已证明的same-App返回，但属于后续UI runner/展示收尾需要处理的边界。

### 成功范围与收尾

- `powered_off_backend_proven=false / rf_recovery_proven=false`按原verdict保留：切换时已没有live Probe，Settings的OFF读回不是backend事件，也不证明同Hub/JS engine或fresh RF I/O。
- 原xctestrun、新staging及App/UITest runner完整文件inventory前后不变；23份package/lock/privacy输入hash不变。收尾只读确认iPhone available/unlocked，无Mirroring/xcodebuild或nRF provider占用，未触碰固件/现有CCC证据。
- `.work/ios-radio-control-full-20261019-01/`保存`input-03-receipt.json`、`control-03{,-console}.log`、`xcresult-summary-03.json`、`attachments-03/`、`control-03-result.json`、`post-control-03-processes.json`和`diagnostics-03/`；失败01/02与只读04原文件不覆盖。
- 本次未修改UI/App/backend代码、运行普通full-suite、重刷固件或commit/push；之前源码修正的软件回归仍属于上节执行记录，未冒充本次重跑。

下一步可基于这次控制可行性证据推进独立startup-off→ON、active-loss门禁：仍须明确平台语义、live backend OFF/拒绝/失效观察、close-zero、ON后fresh-Probe/live-attested Peripheral RF与最终恢复；不自动再切换一次radio，不以本次non-RF成功补齐CCC continuity、RecoveryTen或阶段退出缺口。

## 验证

本轮实际执行：

- `pixi run just react-native host-check`：通过。
- `pixi run just react-native tools-fixture-test`：839 tests，OK。
- 三次上述 device XCTest：结果见表，不合并为 pass。
- 最终 signed build-for-testing：通过；完整日志 `ios/build/radio-control-final-build.log`。它不执行或证明控制路径。执行目录为 consumer `ios/`，命令如下。
- 文档完成后 `pixi run just fmt`、`pixi run just lint`、`pixi run just docs-check-mermaid`、`git --no-pager diff --check`：均通过。

```sh
pixi run python ../../../../tools/react_native/run_quiet.py --no-tty build/radio-control-final-build.log -- pixi run -e apple-ci xcodebuild -quiet -workspace BleHubNitroHost.xcworkspace -scheme BleHubNitroHost -configuration Debug -destination generic/platform=iOS -derivedDataPath build/device-ui-derived DEVELOPMENT_TEAM=FWVYQ287W5 CODE_SIGN_STYLE=Automatic CODE_SIGN_IDENTITY='Apple Development' ONLY_ACTIVE_ARCH=YES build-for-testing
```

RN JS、production backend、native ABI、provider 与 bundle schema 均未修改；10-17 和 10-18 的上述诊断未重跑 Jest、native HIL、package、full-suite 或 RF。

2026-10-18 最终诊断源码验证：`pixi run just react-native tools-fixture-test` 再次 839 tests / OK；`pixi run just react-native host-check` 通过；`pixi run just fmt`、`pixi run just lint`、`pixi run just docs-check-mermaid`、`git --no-pager diff --check` 通过。最终 signed build-for-testing 仍使用上述命令，仅换新日志名 `build/radio-control-diagnostics-build-20261018-01.log`，通过。编译不等于 Settings 控制通过；`...-04` 的真实执行保留了现行前台等待和诊断附件的失败断言，不因最终编译成功回写为 pass。

后续 live-attested startup-off gate 必须同时绑定 iOS artifact/install/PID/native identity、fixture receipt/readiness、真实 backend OFF/scan rejection、两个 close-zero、ON 后 fresh-Probe RF I/O 和最终恢复；现有 Android ledger/provider/bundle 的 platform restrictions 不能简单删除。先验证控制路径，不预建未使用的跨平台框架。既有 [authorization-only pass](2026-10-17-rn-ios-permission-authorization.md) 和 [host 工具边界](../../research/react-native/react-native-host-tooling-architecture.md) 不因此改变。

## 2026-10-20：startup-off 软件 foundation 与 Swift→Python journal 交叉验证

**本节没有操作实体手机、切换蓝牙、启动 fixture 或执行 RF。** non-RF control03 的范围不扩大；iOS live startup-off、active-loss 与最终 host bundle 仍未通过。所有 BleHub 改动保持未提交、未 push。

### 已实现的窄边界

- 同一 RN App 的 `rf_adapter_transition.tsx` 支持 iOS `startup-off` candidate，仍拒绝 iOS `active-loss`。native identity 与 JS boot nonce 独立绑定；OFF 事件/close/17-zero 完成后等待 host ready，只有实际 Linking ready 回调才启用 UI ON request。按钮请求 native URL，不直接构造第二 Probe 或伪造 Linking callback。
- `BleHubNitroHost/AdapterStartupGate.swift` 校验 launch/URL/阶段，journal 以 fresh run UUID 独占创建、完整 envelope atomic replace。native acceptance 必须先持久提交再转发 Linking；不跨 session lock 调用 framework，以免跨队列同步回调死锁。真实 ON reports 同时包含 `rf:` 和 `rf-io:`，不是只接纳最终 pass family；nonce 为三段 lowercase base36，不是 UUID。
- [`ios_adapter_startup_journal.py`](../../../tools/react_native/ios_adapter_startup_journal.py) 严格读取 schema、unique JSON keys、canonical attempt、UTF-8、256 records、1 MiB envelope、32768-byte record 和 bounded error。保留原始 bytes/hash，LF projection 单独计算；partial/failed envelope 保留诊断，不升级为成功。
- [`ios_adapter_startup_ledger.py`](../../../tools/react_native/ios_adapter_startup_ledger.py) 仅重放 cooperative transcript：同 native PID/boot/JS nonce、两个 exact URL/native commit 顺序、真实形状 OFF DTO、两次精确 17-zero、完整 ON RF I/O reports、close 后 overall terminal；unknown/duplicate/foreign/late failure 拒绝。它复用 `check_rf_scan`，只返回 `runtime_status` 和明确 scope，不返回 host/RF verdict。
- 交叉验证发现共享 checker 的子串字段匹配会接受 `value=10ff` 或不同 characteristic route。已在其 I/O 路径收紧 exact tokens/重复字段、PoweredOn+Authorized、scan identity、同一 characteristic、精确值与 local-submission/close 标记；不将 native submission 当远端 delivery。local-name 与启动声明关联，保留合法冒号和 Unicode JSON 字符，不用 `splitlines()` 把 U+2028/U+2029 误切成日志边界。

### 原始 Swift crossing 样本与实际验证

cross-layer [`ios_adapter_startup_journal.json`](../../../tests/support/rf_lab/ios_adapter_startup_journal.json) 原样复制自 simulator NativeTests 的 `ios-adapter-journal-crossing` XCTAttachment，不由 Python 重建或修补。**它的事件和 RF reports 是合成数据**，只证明 Swift writer/native state gate 与 Python reader/ledger 协议相容。

- simulator：iPhone 18 Pro / iOS 27.0，`D8098F99-2E1D-4E40-963F-D2F6F4942EEA`。
- 原始附件：7597 bytes / 39 records，SHA-256 `bc6411ef6486613d785342697fa1eaeed35a922d4a855d1ab7c6245af9d61bc7`。
- 首版附件仅含简化 ON reports，不满足完整 RF checker。它留在 ignored logs 中，不放宽 checker；补全 Swift **测试样本**后重新 build/test/export，tracked fixture 使用新导出的完整原始字节。
- `pixi run -e apple-ci xcrun swiftc -typecheck tests/consumers/react-native-internal/ios/BleHubNitroHost/AdapterStartupGate.swift`：exit 0。
- simulator `xcodebuild … build-for-testing`、指定 AdapterStartup/Journal/NarrowRejectControl 的 `test-without-building`：exit 0，**12 passed / 0 failed / 0 skipped**；没有运行已知独立 `FullRecoveryBegin` 失败用例。补全 crossing 样本后同样 12 项再次通过。
- `xcresulttool export attachments` 后将 ON 投影直接交给现有 `check_rf_scan(..., connect=True, gatt=True, io=True, expected_local_name="BleHubNrf52840")`：通过。
- 完整实际命令、所选 tests、日志、xcresult 与原始附件索引：`.work/ios-adapter-foundation/validation/validation-{75faac5a,43978d72}.json`。构建保留依赖 warnings、Xcode `invalidDigitCount` 诊断；首轮另有 SwiftCompile 的 exit-code-0/no-output 诊断，build 整体 exit 0 且随后测试实际通过，未顺手修改依赖。

### 本节软件回归结果

以下命令均在本机实际执行；完整日志保存在 `.work/ios-adapter-foundation/validation/`，不输出完整构建或测试流到对话：

| 实际命令 / 范围 | 结果 |
|---|---|
| `pixi run python -m unittest tools.tests.react_native.test_ios_adapter_startup_journal tools.tests.react_native.test_ios_adapter_startup_ledger tools.tests.react_native.test_check_runtime_log tools.tests.react_native.test_adapter_transition_ledger -q` | 72 tests / OK |
| `pixi run just react-native tools-test` | 585 tests / OK / 189.837s |
| `pixi run just react-native tools-fixture-test` | 1036 tests / OK / 210.718s |
| `pixi run just react-native host-check` | 通过 |
| 在 consumer 目录下 `pixi run ./node_modules/.bin/jest --runInBand src/rf_adapter_transition.test.tsx src/rf_lab_catalog.test.ts src/rf_lab_workbench.test.tsx src/rf_lab_session.test.ts` | 4 suites / 138 tests passed |
| 在 consumer 目录下 `pixi run ./node_modules/.bin/jest --runInBand` | 17 suites / 618 tests passed |
| `pixi run just fmt`、`pixi run just lint`、`pixi run just docs-check-mermaid`、`git --no-pager diff --check` | 通过 |

首次 Jest 命令从仓库根经 `npm --prefix … exec` 启动，未加载 consumer 的 RN/Babel 配置，出现 Haste 同名 package 和 TypeScript `import type` parse error，0 tests 执行；日志 `jest.log` 保留，不算产品测试失败或通过。改为在真实 consumer 工作目录执行现有本地 Jest，无改动依赖或测试配置，随后定向与全套分别通过。一次早期 agent 的 canonical tools-test 120s 外层预算不足而超时，未算通过；上表为后来 600s 预算内完整重跑结果。

该 foundation 时点仍待闭合：在**单次存活的 XCUITest 窗口**中完成 initial ON → OFF readback → OFF backend/first close-zero → ON readback → 同 App/JS ready/confirm → live-attested DK 的 fresh-Probe I/O → second close-zero，并绑定 phone/fixture lease、原始 journal、UI 控制记录、最终 ON readback 和 bundle verdict。不能拼接两次用例、在 runner 退出后 relaunch 冒充连续性，或仅凭这份合成样本开放 maintained iOS RF 入口。

## 2026-10-20：单次 UI 窗口与 host capture candidate

**本轮仍未操作实体手机或 DK，未执行 radio mutation/RF，也未 commit/push。** 在原 `BleHubNitroHostUITests` 增加 opt-in `testBluetoothStartupOffAndFreshProbeRecovery`，不是新 App 或第二套通用 controller。全部输入先验证，authorization inspect 不 reset/回答权限；审计的 adapter App 只 launch 一次。OFF 不重试，ON confirm 只点一次；每次 `activate()` 前要求原 App 仍在后台，随后逐字段绑定 native PID/boot 与 JS nonce。返回最终结果页只证明用例内时点，不声称 runner 结束后页面持续可见。

### 分层接口与安全边界

- UI 复用严格 child Switch 和 Settings readback helper，成功记录固定十二步：authorization verified → initial ON → OFF → OFF Probe closed → ON → 原 session 返回 → 请求 host window → ready → ON request → App runtime passed → 独立 final ON → 原结果返回。每步保留 elapsed、actual UI identity 或 switch value；primary/cleanup 分离，失败后最多一次有界 ON restoration 尝试，系统/XCTest 中止仍可能阻止它。
- 唯一 `BLEHUB_IOS_ADAPTER_WINDOW_READY` 输出绑定 run、fixture receipt hash 与实际 UI identity。XCTest 不调用 `openURL`；host 需先验证 ownership/readiness/no-peer 和 official PID，再通过原 CoreDevice payload-url 机制投递一次 ready。该输出不自行证明 receipt 的真实性或 radio state。
- 新 [`ios_adapter_startup_capture.py`](../../../tools/react_native/ios_adapter_startup_capture.py) 复用 `CaptureTransport`/`PrefixStream` 的有界 SDK 泵送、install/process/payload receipts 与 local child teardown，不继承 recovery 状态。只运行指定 test、单 iteration、禁止并行；没有 retry flag、终端输入、App `--terminate-existing` 或 fallback launch。post-window 下载限定 exact-run App container journal，不重启 App。
- `test-without-building` **仍可能安装 App/runner**。查本机 `xcodebuild.xctestrun(5)` 后，staging 同时把 `UITargetAppPath` 与原 App 在 `DependentProductPaths` 中的精确项/子路径绑定至 frozen candidate；不把无显式 `devicectl install` 误说成没有安装副作用。未来 parent 必须在 phone/artifact ownership 内处理它，并重新核对实际 install/executable identity。
- Ledger 新 `verify_window_ready` / `verify_control` 严格绑定 schema、run/receipt、十二步顺序、相同身份、Switch 值和 finite/non-bool/nonnegative/nondecreasing elapsed。它们仅审计 cooperative observations，不证明独立进程 provenance、物理 RF、跨时钟 deadline 或 cleanup 保证。

### 无设备回归发现并修正的问题

1. runner 在 `ready_guard` 内退出后仍可能投递 URL：授权后、投递前再次检查存活；不靠重启修复连续性。
2. deadline 已过才在子进程提交后报错：新 capture 在 preflight/runner/ready 提交前检查预算；共享 `ios_device.require_ready_unlocked_device(timeout=60)` 的两个只读查询共用整体剩余预算；共享 `CaptureTransport._command` 也在 `Popen` 前拒绝已过期预算。SDK 迟到成功不恢复授权。
3. final publication/close 覆盖 primary，或 normalized log 写失败后旧 receipt 留为 completed：分别聚合 stop/close/publication 错误，保留原异常与 notes，并独立 best-effort 保存 failed receipt。持续不可写的文件系统无法保证落盘，不能靠旧 completed 文件推导 host pass。
4. host 接纳 `--` 开头 local-name 而 UI/native 拒绝：在 staging 前统一拒绝，避免无效输入启动 SDK。

失败 assertions 和早期 120s tooling timeout 的日志保留在 `.work/ios-adapter-foundation/validation/`，未标为 expected failure 或放宽断言。当前没有 public live runner/Just recipe、phone lease owner 或最终 iOS bundle admission；还需闭合这些与 provider physical/GAP、cleanup、UI 附件和 journal 的绑定，才可在新明确授权下运行真机。

### 本轮实际验证

| 命令 / 范围 | 结果 |
|---|---|
| Simulator `build-for-testing`，包括新增 UI method | 通过；没有执行 mutation 路径 |
| 最新 `BleHubNitroHost_iphonesimulator27.1-arm64-x86_64.xctestrun`，原 12 个 native selections + 新 UI method，所有 gate 环境变量均未设置 | **12 passed / 1 skipped / 0 failed**；新增 method 实际被发现并在 opt-in 检查处 skip |
| `pixi run python -m unittest tools.tests.react_native.test_ios_adapter_startup_capture tools.tests.react_native.test_ios_adapter_startup_ledger tools.tests.react_native.test_ios_adapter_startup_journal tools.tests.react_native.test_capture_io tools.tests.react_native.test_ios_device -q` | **68 tests / OK** |
| `pixi run just react-native tools-fixture-test` | **1080 tests / OK / 216.625s**，包含 RN tooling、Peripheral 和 Android controller，不是 RF |
| `pixi run just react-native host-check` | 通过 |
| consumer 目录下 `pixi run ./node_modules/.bin/jest --runInBand` | **17 suites / 618 tests passed** |
| `pixi run just fmt`、`pixi run just lint`、`pixi run just docs-check-mermaid`、`git --no-pager diff --check` | 通过 |

默认 skip 的 UI activity 只有 Start/Set Up/Tear Down，没有 App launch、Settings 导航、toggle 或 window-ready 输出；这是默认安全分支的模拟器证明，不算实际 UI 控制/RF 通过。实际命令、环境/manifest 检查、产物 hash 不变验证、summary 与 fresh xcresult 索引在 `.work/ios-adapter-foundation/validation/startup-default-skip-626a082a-85a9-4d72-b292-17b18b9d33f4.validation.json`。本轮 build 日志为 `startup-ui-build-for-testing-final.log`，定向/组合/Jest/格式检查日志使用 `startup-host-*` 前缀。首次 `fmt` 命中 cleanup aggregation 的 BLE001，已补明保留 primary 的意图后完整重跑通过；没有删除 cleanup 或中断处理来消除检查。

## 2026-10-20：外层 runner、phone lease、provider owner 与 bundle candidate 接通

**仍未执行真机 SDK/Settings/RF、固件操作或 commit/push。** 本节完成代码连接与无设备验证，不把 earlier simulator/non-RF pass 升级，也不宣称完整 runner 的真实 SDK 正向已跑通。新增 candidate 入口存在不等于 phase exit、ABI freeze 或双平台 RF 完成。

### 实现与归属

- [`run_ios_adapter_startup.py`](../../../tools/react_native/run_ios_adapter_startup.py) 拥有命名编排：显式 `--authorize-radio` 在任何设备操作前校验，输入必须是已有 App/xctestrun、contract/profile 与物理 UDID；无 build、Pods、signing/provisioning 或固件 fallback。phone owner 在 fixture/安装/App 前获取；bundle 与 physical session 使用同一 canonical run UUID。`fixture_session.prepare_session` 新增可选 `session_id`，旧 caller 仍默认生成新 UUID；非法显式 UUID 在任何目录/USB 解析前拒绝。
- [`IosRadioLease`](../../../tools/react_native/ios_radio_lease.py) 校验 lock/owner inode 与原始 bytes，拒绝 stale/foreign owner、symlink/hardlink；失败不能证明最终 ON 时保存 quarantine 并保留占用，无自动 clear。现代/旧式 UDID 的 canonical case 由 `ios_device.canonical_physical_udid` 统一用于 SDK、bundle 与锁 hash。锁只协调合作 runner，不是系统级 radio 排他，仍禁止与别的 Settings/RF 操作并发。
- provider-local [`IosAdapterStartup`](../../../tests/peripherals/nrf52840/ios_adapter_startup.py) 在 parent 进程持有唯一 `DirectFixture`、physical lease/BTP session，共用 `FaultController.require_idle` 与已有 exact GAP tracker/owned-peer cleanup；不创建第二个串口或写全局 provider PID file。`start` 先 live attestation；`open_on` 前排空旧 GAP、确认 idle/no-peer；成功必须在 cleanup 前已有同一 peer 的恰好一次连接/断连。cleanup 补出的 disconnect、timeout 或 unknown peer 均不能制造 pass。
- ready callback 在存活的单次 XCTest 窗口内复制 exact-run OFF journal，`verify_off_journal` 与完整 runtime 共用同一 parser，要求恰好八条 OFF prefix、实际 DTO 与精确 17-zero。与实际 UI identity 匹配后，provider 才开放 ON，capture 再投递一次 ready。它不把 Settings ON 或 native submission 直接当远端 RF 证明。
- [`ios_adapter_startup_evidence.py`](../../../tools/react_native/ios_adapter_startup_evidence.py) 负责独立 `ios-adapter-startup` bundle scenario；**原 `adapter-transition`/`permission-transition` 的 Android-only restrictions 保持**。Passed 必须重放 runtime/control/physical，绑定原始 OFF/full journal、XCTest window raw/projection、官方 install/PID/URL/copy receipts、staged test-run、同 candidate、1/1/0/0 XCTest summary、唯一 exact-method/device 的原始 control attachment，以及已释放/on-readback 的 phone journal。原 XCTAttachment bytes 不重序列化，hash/range/inventory 仍归原 EvidenceBundle。

失败收尾分别尝试 control/summary/journal 只读收集、physical finish、phone finish 和 bundle finish。回归发现并修正：unexpected physical finish 与 collector 中的 `KeyboardInterrupt` 不得覆盖 primary 或跳过后续 owner；同一 UDID 的大小写别名不得导致 phone journal 与 bundle drift。持续不可写或系统中止仍可能使恢复/发布不完整，不以旧 running/completed 文件推断通过。

### 入口与执行边界

```text
pixi run just react-native host-ios-rf-adapter-startup-candidate <udid> <prebuilt-app> <prebuilt-xctestrun> <fixture-contract> <fixture-profile> authorize-radio
```

末参数是明确允许一次 radio OFF→ON 的执行授权，不是普通无设备测试参数；本节未运行该命令。Xcode 本身可能安装 App/runner，执行前仍需构建/核对同一签名 device candidate、新鲜 readiness/installed-image receipt、手机解锁与独占状态。当前 App catalog 不因 candidate recipe 存在而标为 maintained RF pass，iOS active-loss 继续拒绝。

### 实际软件验证

| 实际命令 / 范围 | 结果 |
|---|---|
| `pixi run python -m unittest tools.tests.react_native.test_run_ios_adapter_startup tools.tests.react_native.test_rf_evidence_ios_adapter_startup tools.tests.react_native.test_ios_radio_lease tools.tests.react_native.test_ios_adapter_startup_capture tools.tests.react_native.test_ios_adapter_startup_ledger tests.peripherals.nrf52840.test_ios_adapter_startup tests.peripherals.nrf52840.test_fixture_session -q` | **151 tests / OK / 4.367s** |
| `pixi run just react-native tools-fixture-test` | **1147 tests / OK / 220.142s** |
| `pixi run just react-native host-check` | 通过 |
| consumer 目录下 `pixi run ./node_modules/.bin/jest --runInBand` | **17 suites / 618 tests passed** |
| `pixi run just fmt`、`pixi run just lint`、`pixi run just docs-check-mermaid`、`git --no-pager diff --check` | 通过 |
| `pixi run python -m tools.react_native.run_ios_adapter_startup --help` | 通过；不执行设备操作 |

新 bundle 的完整 synthetic 正向、搬迁及篡改/缺件/quarantine 回归实际通过；runner 的 collection/orchestration、真实 lease/session/journal/failed-bundle 路径与 collector＋真实完整 bundle 重放也通过。**这不是 complete runner 的实际 SDK/BTP 正向集成或 RF 通过。** 本轮没有改 Swift/TS，未重复声称 native simulator 或 signed device build 已验证新 runner。初始 import owner 错误、finish/中断失败断言、早期 agent 的 120s 组合测试 timeout 均保留日志，修复后最终 canonical 在 600s 预算内完整通过。

所有完整输出在 `.work/ios-adapter-foundation/validation/`：最终本轮日志使用 `parent-*`，原失败和各 owner 定向日志不覆盖。下一次真机 mutation 仍需明确授权和 fresh attempt；若缺最终 ON 证明，不自动解除 quarantine 或再跑一次 OFF。

## 2026-10-20：新增测试的硬编码、冗余与 oracle 审查

按用户要求仅整理本轮 RN startup 测试/support 和职责文档；不修改 production runner/ledger/provider、Swift/TS、固件或 golden attachment，不操作手机/RF，不 commit/push。本次不是对全仓测试已完成全面审计的声明。

两处实际问题已修正：

1. 完整 synthetic bundle 曾用 production `ledger._CONTROL_STEPS` 生成“正确 control”；这会让输入和 verifier 同步漂移。新增 RN-only [`ios_adapter_startup_samples.py`](../../../tools/tests/react_native/support/ios_adapter_startup_samples.py)，保存独立编写的 UI source sequence，bundle、control-ledger、lease 和 runner support 复用数据而非复用被测判断。回归在 verifier table 被更改后再构造样本，要求真实校验拒绝，防止重新引入同源 oracle。
2. runner 的失败路径 Harness 曾先构造完整 `StartupBundle`，额外生成 unrelated candidate/receipt/成功 ledger 再取少数输入。现在只创建最小 fake App、输入 test-run、source journal/control/summary；完整 bundle 只留在 evidence/collector 的端到端离线重放中，没有把不同层的 mock/物理策略并成万能 Harness。

示例设备改用 synthetic physical-shaped ID；完整 bundle 新增两组独立 PID、boot、JS nonce、service UUID 的正向重放，尤其让 boot UUID 与 service UUID 不同，避免只对一个 magic 样本通过。Swift 原始 golden 的 bytes/hash 继续单独验证，SHA-256 仍为 `bc6411ef6486613d785342697fa1eaeed35a922d4a855d1ab7c6245af9d61bc7`。

保留固定 command IDs、wire/schema、十二步顺序、精确 17-zero、原始 hash/字节绑定、错误保留、quarantine 和 deadline assertions。这些是独立契约或独特回归，不因“去硬编码”而从 production 常量动态生成，也不为降低测试数删除。`test_layout` 的现有依赖保护已涵盖新 iOS runner/shared owners，继续禁止 live→synthetic、shared→CLI 和 non-recovery capture→recovery state。

实际验证（完整输出为 `.work/ios-adapter-foundation/validation/test-review-*`）：

| 命令 / 范围 | 结果 |
|---|---|
| `pixi run python -m unittest tools.tests.react_native.test_ios_adapter_startup_ledger tools.tests.react_native.test_ios_radio_lease tools.tests.react_native.test_rf_evidence_ios_adapter_startup tools.tests.react_native.test_run_ios_adapter_startup tools.tests.react_native.test_layout -q` | **79 tests / OK / 4.128s** |
| `pixi run just react-native tools-fixture-test` | **1149 tests / OK / 214.798s** |
| `pixi run just react-native host-check` | 通过 |
| consumer 目录下 `pixi run ./node_modules/.bin/jest --runInBand` | **17 suites / 618 tests passed** |
| `pixi run just fmt`、`pixi run just lint`、`pixi run just docs-check-mermaid`、`git --no-pager diff --check` | 通过 |

没有据此补写 complete runner 的真实 SDK/BTP/RF pass，也没有重跑 native 或签名 device build。测试/support 的小范围整理完成，之前明确的真机缺口不变。

## 2026-10-20：签名 device candidate 构建与只读预检通过，未安装/执行

用户继续后，本轮只构建/核对 candidate，不把一般“继续”解释为新的系统蓝牙 mutation 授权。没有安装或启动 App、执行 XCTest、切换蓝牙、打开 fixture/串口、刷固件、重置授权或 commit/push，也没有运行 keychain unlock、收集密码或允许 provisioning updates。

### 实际输入与只读状态

- 本机选定 Xcode **27.1 / 27A9275**，Apple Swift **6.4**，通过本地 Pixi Apple environment 执行。
- CoreDevice 新查询确认目标物理 iPhone `00008110-000C61160C39401E` 已配对、wired，Developer Mode enabled，iOS `26.6.1`；lock-state 当次返回 `passcodeRequired=false / unlockedSinceBoot=true`。初始 device-list 的 tunnel 为 disconnected，后续 lock-state 查询成功；不推导 automation 认证或 RF readiness。
- `security find-identity -v -p codesigning` 可见一个 Apple Development identity。仅身份可见不证明私钥可访问；随后实际 signed build 成功才闭合本次构建的签名访问，未执行任何交互式解锁操作。
- `pixi run just react-native host-check` 和 `pixi run python -m tools.artifact_fingerprint check apple-production` 通过；复用既有 npm/Pods，`stage-apple-production` 仅刷新既有 production staging，没有 toolchain/npm/Pods 安装。

### 实际构建与产物验证

工作目录为 BleHub root，完整输出保存至 `.work/ios-adapter-foundation/device-candidate-20261020/`。构建命令如下，仅 `build-for-testing`，没有 `test`/`test-without-building`：

```sh
pixi run python tools/react_native/run_quiet.py --no-tty --progress-seconds 60 .work/ios-adapter-foundation/device-candidate-20261020/build-for-testing.log -- pixi run -e apple-ci xcodebuild -quiet -workspace tests/consumers/react-native-internal/ios/BleHubNitroHost.xcworkspace -scheme BleHubNitroHost -configuration Debug -sdk iphoneos -destination 'generic/platform=iOS' -derivedDataPath tests/consumers/react-native-internal/ios/build/device-ui-derived DEVELOPMENT_TEAM=FWVYQ287W5 CODE_SIGN_STYLE=Automatic CODE_SIGN_IDENTITY='Apple Development' ONLY_ACTIVE_ARCH=YES build-for-testing
```

实际 exit 0；`codesign --verify --deep --strict --verbose=2` 对 App 和 UI runner 均通过，`check_single_image.py ios …/BleHubNitroHost.debug.dylib` 通过。App 为 `org.reactjs.native.example.BleHubNitroHost`，签名 Team **FWVYQ287W5**，CDHash `cd8c3580b23d2531d672bb5ed65988fcf8866f62`。App/UI runner 的 provisioning profile 均包含目标 UDID，Team 匹配，本次读取时未过期（到期 `2027-09-18T07:19:54Z`）；这不构成后续认证永久有效的承诺。

| 产物 / 检查 | 本次事实 |
|---|---|
| App | `tests/consumers/react-native-internal/ios/build/device-ui-derived/Build/Products/Debug-iphoneos/BleHubNitroHost.app` |
| Device test-run | 同一 `Build/Products/BleHubNitroHost_iphoneos27.1-arm64.xctestrun`；SHA-256 `e22ceea6a53ce3e65e07613dfdd55800436d2aab07d2bd1ec8b361ed49f02ef0` |
| App tree fingerprint | `ab872b1ea5f806ed21966d155fabb93d6a96e413189f3403decdedfdae61badd` |
| `main.jsbundle` | 4529484 bytes；SHA-256 `a31788617830835b0325ea8fac9c688867edde47651d520f011bfc53f6d955f4` |
| 记录的源码输入 | tracked/untracked 非 ignored 的 223 个文件，构建前后 hash 不变；不宣称覆盖所有编译环境或 source-to-build attestation |
| 产物入口检查 | UI binary 包含新增 startup-off test method/window marker；JS 包含 ON confirm/native identity properties；Info.plist 注册 private control scheme |
| Test-run 默认环境 | 三类环境均无 BLEHUB opt-in flags；没有把历史 control/inspect staging 当作新 candidate |

`prepare_test_run` 用这份真实 device xctestrun 做了一次**纯临时目录 shape check**：只保留 UI target、指定 method、candidate App/dependency 路径；使用明确的 shape-only 参数及无证明作用的零 receipt hash。临时 staging 未执行且已随目录移除，不留可误用的 mutation-enabled test-run，不冒充 live receipt。

DK-B 的 contract/profile 仅做 offline preflight：`nrf-multifunction-rf-v2-b`、service `fd700000-0000-4000-8000-000000000000`、12 capabilities，相互匹配；**没有重新读取实际安装固件，不证明 live installed-image/idle/no-peer**。

`candidate-receipt.json`、source before/after inventories、App/UI runner 共 125-file inventory、artifact fingerprint、签名/profile/single-image 日志和 offline fixture-input check 均保存在上述 ignored 目录。源码/产物 marker presence、签名和 profile 验证只是构建/包证据，不证明 native/JS 执行、系统 Settings 控制、结果页可见或 RF。

本轮未改 production/test code，未重跑 Jest、模拟器 runtime 或真实 RF。文档完成后执行 `pixi run just fmt`、`pixi run just lint`、`pixi run just docs-check-mermaid` 与 `git --no-pager diff --check`。首次真机 startup-off RF 仍须对这台 iPhone 的一次系统蓝牙 OFF→ON 获得明确授权，随后在真实 lease 下重新取得 fresh installed-image receipt/readiness，并核对 candidate hash 未漂移。

## 2026-10-20：首次授权 startup-off 尝试在 test body 前失败，phone lease 保持隔离

### 授权尝试与实际执行边界

用户明确授权目标 iPhone 的一次系统蓝牙 OFF→ON 后，使用上述 signed candidate，显式以进程级 `BLEHUB_NRF52840_PORT=/dev/cu.usbmodem0010502514791` 选择 DK-B；未修改默认 DK-A 配置，未刷固件或 reset。执行入口为 `tools.react_native.run_ios_adapter_startup`，参数包含目标 UDID、上述 App/xctestrun、v2-B contract/profile 和 `--authorize-radio`。

- Run ID：`3fd0100b-b1e6-4056-b99a-2002b5902b1b`。
- 原始 attempt 目录：`tests/consumers/react-native-internal/ios/build/ios-adapter-startup-d3d4a7c750684b7096e58eb1c021e858/`；完整入口日志为 `.work/ios-adapter-foundation/validation/rf-authorized-attempt-01.log`。
- Xcode 在执行测试前拒绝 `-test-iterations 1`：`Must specify -test-iterations with more than 1 iteration.`。结果摘要 `totalTestCount=0`；未执行 test body、未打开 Settings、未切换系统蓝牙。
- 没有 `BLEHUB_IOS_ADAPTER_WINDOW_READY`，capture 未交付 guarded readiness。bundle 保持 failed，primary error 为 `startup-off XCTest failed/exited without guarded readiness`；没有 backend OFF、Probe close/17-zero、同 JS session 或 RF recovery 证据。

DK-B 在 App 测试前完成 live attestation：resource `jlink:1050251479`、image `nrf-multifunction-rf-v2-b`，installed digest 为 `d57953806f0195dff0b175d80b09279d64e5b2d82b15198de77bd9ebd54e75f7`。provider 最终记录 `connections=0 / disconnections=0`，`advertising_stopped=true / hold_inactive=true / peer_count=0 / engine_flags=0`，`cleanup_errors=[]`。这证明本次记录的 attestation 与无连接 cleanup，不是 RF pass。failed bundle 的离线 integrity check 通过，不构成重新授权。

### 命令修正与防重复执行回归

已从 [capture](../../../tools/react_native/ios_adapter_startup_capture.py)、[bundle verifier](../../../tools/react_native/ios_adapter_startup_evidence.py) 和对应 synthetic command 样本移除 `-test-iterations 1`；单次 XCTest 使用默认执行，不传 repetition flags。没有以迭代次数 2 绕过参数检查。

[capture command 断言](../../../tools/tests/react_native/test_ios_adapter_startup_capture.py) 与 [bundle mutation 回归](../../../tools/tests/react_native/test_rf_evidence_ios_adapter_startup.py) 拒绝 `-test-iterations 1/2`、`-retry-tests-on-failure`、`-run-tests-until-failure`，防止把重复 radio mutation 包装为一个授权窗口。修正后的 targeted 回归实际 **63 tests OK**，当轮 fmt/lint/host-check 通过。

真实 Xcode option parser 的无设备执行诊断不再报 iteration 参数错误，而停在刻意缺少 test bundle 的 `There are no test bundles available to test.`。最初诊断脚本因误要求输出包含缺失文件名而 exit 1；原日志未覆盖，后续 assessment 单独记录。证据为 `.work/ios-adapter-foundation/validation/rf-command-sdk-parser{.log,.json,-assessment.json}`；此检查不是 XCTest 执行或 RF pass。

### Phone quarantine 与只读恢复检查失败

由于本次没有 control attachment/final Settings ON readback，phone journal 保持 `status=quarantined / restoration=unverified / released_monotonic=null`，错误为 `missing or malformed source control SHA-256`。lease 目录为 `.work/ios-radio-leases/40a87b583c2d6aa6e4b9e2948ca22535483c07db1148069d7ef33272bd91d60b`。不能凭“test body 未运行，因此蓝牙应未改变”推断恢复证明并删除 lease。

随后曾执行一次现有 inspect-only 分支，只读取 Settings，不点击开关、不重发 OFF、不清理 quarantine。其结果为 **runner 初始化失败**：

```text
The test runner failed to initialize for UI testing.
Underlying Error: Timed out while enabling automation mode.
```

- 独立结果包：`.work/ios-adapter-foundation/validation/radio-recovery-inspect-ee5a2db7e7f54f28aa024b1b9313c614.xcresult`；完整日志 `rf-recovery-inspect.log` 位于同目录。
- 历时约 62.6 秒；`xcresulttool get test-results summary/tests` 显示唯一失败节点位于 `System Failures`，名称为 `BleHubNitroHostUITests-Runner (90395) encountered an error`。摘要中的 `totalTestCount=1 / failedTests=1` 是系统 runner 错误节点，不能称业务测试方法执行一次。
- 未进入 inspect-only test body，无 Settings tree/ON readback，也无新的 OFF→ON。不能把初始化超时归因为已证实的镜像冲突、手机未解锁或 RN UI bug。

后续只读检查再次取得 `passcodeRequired=false / unlockedSinceBoot=true`；Mac 进程快照未发现运行中的 `xcodebuild`、`XCTRunner` 或 `iPhone Mirroring`/`ScreenContinuity`。手机进程快照包含 RN host PID `90368`、Settings 和系统 `testmanagerd`，未见该 UI runner；还可见历史 native probe/artifact consumer 进程。该快照不证明前台 UI、业务会话连续性、并发 BLE 活动或 automation 授权已恢复，也没有终止这些进程。device process 原始记录保存为 `.work/ios-adapter-foundation/validation/rf-readonly-processes-followup.json`。

本次失败保持原样，不自动重跑 inspect-only 或 startup-off，不调用系统认证，不解除 quarantine。继续真机前需要明确的只读恢复检查授权与 fresh ON readback；若引入 restoration-only release，必须绑定原 quarantine owner/run/device、保留新恢复证据与原 failed verdict，不能直接删锁或将恢复冒充 RF pass。新的 OFF→ON 仍需另行明确授权；`active-loss` 继续不开放。

### 后续软件验证

未修改 App/Swift 或 production ABI，也未重新执行手机 UI/RF。本轮重新运行 canonical `pixi run just react-native tools-fixture-test`（**1150 tests OK**）与 `pixi run just react-native host-check`，均通过；完整日志为 `.work/ios-adapter-foundation/validation/rf-followup-{fixture-tests,host-check}.log`。`pixi run just fmt`、`pixi run just lint`、`pixi run just docs-check-mermaid`、`git --no-pager diff --check` 以及报告 UTF-8/空白/fence 检查均通过。这些软件结果不解除 phone quarantine，也不补齐 RF 门禁。

## 2026-10-20：再次明确授权的单次只读恢复检查仍未进入 test body

用户在“只读取 Settings 蓝牙开关、不点击开关、不跑 RF、不自动清除隔离”的明确范围下回复继续。本次仅执行一次 inspect-only，没有把该授权扩展为 OFF→ON。执行前重新取得目标 iPhone 的 `passcodeRequired=false / unlockedSinceBoot=true`；Mac 进程检查未发现运行中的 Xcode UI test 或 iPhone Mirroring 相关进程。这些预检仍不证明 automation mode 可用。

复用已核对 SHA-256 的 inspect-only staging，创建独立 `radio-inspect-only-0d6cb1b73a87478481084774959c3ff3.xctestrun`；只保留 `testBluetoothSettingsOffAndRestore`，明确设置 `BLEHUB_IOS_RADIO_CONTROL_GATE=1` 与 `BLEHUB_IOS_RADIO_CONTROL_INSPECT_ONLY=1`，未启用 startup-off 或其他 opt-in。唯一配置调整是保留系统 attachments。执行使用 `run_quiet.py --no-tty`，关闭 stdin、脱离 terminal session，无 repetition/retry 参数；未构建、刷固件或打开 fixture。

命令 exit **65**，约 **62.7 秒**后再次报告 `Timed out while enabling automation mode`。新结果包的唯一失败节点为 `System Failures / BleHubNitroHostUITests-Runner (90813) encountered an error`，不是 inspect-only 方法内的断言；没有 Settings 开关读数，也没有蓝牙 mutation。到此停止，没有自动重试。

本次证据均位于 `.work/ios-adapter-foundation/validation/`：

- 结果包：`radio-recovery-inspect-0d6cb1b73a87478481084774959c3ff3.xcresult`。
- 完整日志：`rf-recovery-inspect-0d6cb1b73a87478481084774959c3ff3.log`。
- 输入及原 lease hash：`rf-recovery-inspect-input-0d6cb1b73a87478481084774959c3ff3.json`。
- `xcresulttool` summary/tests 导出与 assessment：`rf-recovery-inspect-0d6cb1b73a87478481084774959c3ff3-{summary,tests,assessment}.json`。

执行后对比确认 staged test-run、原 quarantine owner 与原失败 journal 的 SHA-256 均未改变，原 lease 仍为 `quarantined`。本次既不构成 ON 恢复证明，也不构成 RF pass；不能仅凭同一 timeout 就确认认证、镜像、手机锁屏或 RN 页面是根因。未修改 production/test code；本节为新尝试的事实补充，不重标历史 verdict。

## 2026-10-20：用户再次授权观察弹窗，单次 inspect-only 取得 fresh Settings ON

用户明确要求“再尝试一次我看看有没有弹窗”，范围继续限定为一次 Settings 只读检查，不点击蓝牙开关、不跑 RF、不解除 quarantine。本次预检确认 iPhone 已解锁、未发现运行中的竞争 Xcode UI test/Mirroring 相关进程；复用前一份 hash 已核对的 inspect-only test-run 内容，另存新 staging/result，未覆盖之前失败证据。

`run_quiet.py --no-tty` 下的 `xcodebuild test-without-building` 本次 exit **0**。`xcresulttool` summary 为 **1 passed / 0 failed / 0 skipped**，总窗口约 **14.5 秒**；test tree 确认 `testBluetoothSettingsOffAndRestore()` 实际执行约 **5.2 秒**。虽然 method 名称含 Off/Restore，环境明确启用 `BLEHUB_IOS_RADIO_CONTROL_INSPECT_ONLY=1`，实际执行的是提前返回的只读分支，不是 OFF→ON 控制流程。

导出的 `ios-radio-inspection-only` attachment 原文为：

```text
scope=settings-bluetooth-switch-inspection-only; switch_value=1; radio-control-gate-not-run
```

同次 accessibility tree 中，`BLUETOOTH` cell 的 labelled switch 和其 child switch 均为 `value: 1`，并保留 Settings 截图。因此取得本次 **fresh Settings ON readback**，不是根据历史状态、App backend 状态或人工口述推断。没有点击开关、执行 RF、打开 fixture，也没有通过 terminal 收集认证信息。本次成功仅证明这一窗口的 UI automation 可用，不能确认前几次 automation-mode timeout 的根因或宣称认证永久恢复；是否出现系统弹窗仍以用户观察为准。

独立证据位于 `.work/ios-adapter-foundation/validation/`：

- 结果包：`radio-recovery-inspect-8ac69242dbec4102b4c57f72131da2e9.xcresult`。
- 完整日志：`rf-recovery-inspect-8ac69242dbec4102b4c57f72131da2e9.log`。
- 输入记录：`rf-recovery-inspect-input-8ac69242dbec4102b4c57f72131da2e9.json`。
- summary/tests、原 lease 对比和 assessment：`rf-recovery-inspect-8ac69242dbec4102b4c57f72131da2e9-{summary,tests,lease-checks,assessment}.json`。
- 原始导出：`radio-inspect-8ac69242dbec4102b4c57f72131da2e9-attachments/`，含 manifest、10 个 attachments；note SHA-256 `ebe709ecc5d5f85768477ecc360a76ab456102364b593c9f84486ae1e334e96f`，toggle accessibility tree SHA-256 `1a618712e5492ee7b726fad865ce7c51a346f752524b7a76059e6d60469a3061`。

执行后再次确认 staging、原 owner 和原 failed journal 的 SHA-256 未改变；原 phone lease 仍为 `quarantined`，首次 RF attempt 仍为 failed。新 ON 观察不补写原 attempt 的 control attachment，也不自动释放 lease。后续仍须明确的、绑定原 owner/run/device 和本次恢复证据的 restoration-only release 流程；新的 OFF→ON 必须另行授权。未新增工具/App、未修改 production/test code，也未进行新的 RF 尝试。

## 2026-10-20：显式恢复性释放与第二次授权 RF 尝试，控制通道问题已定位

### 恢复性释放：保留原失败 owner，不直接删锁

用户确认上次 inspect-only 成功前在系统弹窗内亲自完成了密码授权；未向 agent 提供密码。随后用户明确同意当前 iPhone 一次自动 OFF→ON + DK-B RF 测试，并回复开始。本轮先实现并验证所需的 restoration-only 前置，不把自动化认证或只读 ON 观察本身当作 radio mutation 授权。

- [原 phone owner 模块](../../../tools/react_native/ios_radio_lease.py) 增加显式 `restore_quarantined_phone` / `python -m tools.react_native.ios_radio_lease` 入口，不在普通 RF runner 内自动调用，不采纳 stale owner。
- [只读恢复证据校验](../../../tools/react_native/ios_radio_restoration.py) 从原 xcresult 重新导出 summary/tests/attachments，绑定原 owner/journal hash、设备、唯一 inspect-only 方法、精确 opt-in/命令、原始 ON note 与真实 switch/child tree。源/result/候选/导出数据在归档提交前再次复验，ON 检查须晚于 quarantine 且未超过一小时。
- 恢复必须确认原 owner PID 不存在；与 acquire 共用 lock-root mutex。原 owner/journal 副本和 intent 先落盘，再原子归档旧 quarantine 目录，不修改原 failed journal/bundle。
- `restoration-release.json` 状态为 `verified-on-awaiting-archive-commit`，仅文件存在不是 release；必须同时验证 archive inode 与原 owner bytes。`.restoration-pending` blocker 在 commit/rollback 失败时保留，普通 acquire 拒绝，不因原 active 名称为空而放行。
- 回归覆盖缺授权、错 run/device/hash、活 PID、并发 acquire/recovery、源/导出/目录/inode/原始 bytes 漂移、证据过期、写盘/归档失败、归档落盘失败回滚，以及回滚失败后新 acquire 仍被 blocker 拒绝。最后一次恢复/runner 定向验证 **68 tests OK**，随后 canonical 工具/fixture 回归通过。

本次使用 `8ac69242…` 的原始 ON xcresult，先做真实本地 collector/verifier 预检，再显式恢复原 run `3fd0100b-b1e6-4056-b99a-2002b5902b1b`。恢复输出在 `.work/ios-adapter-foundation/restoration-release-20261020/`；命令完整日志 `validation/restoration-release-20261020.log`。执行后复验 archive inode/owner、原 journal hash、active lock 名称和 pending blocker：**归档提交成功**，原 failed journal 未变，没有 pending blocker。只解除旧 phone 占用，不将原 RF attempt 改为 passed。

本次绑定的原 owner SHA-256 为 `800dbd211564b5a53f704c52da18a10b90e82c640e891b3bc9c6ee6d9ed104f6`，原 journal SHA-256 为 `4add54bc80171d5a0a1a9ee0198e3f4939878190707b503b0f16b5d1977b343d`。新恢复证据及其 intent 独立保存，不回填旧 control attachment。

### 第二次授权尝试的实际边界

恢复之后，重新核对原 signed App/UI runner **125 个文件 hash**与原 xctestrun SHA-256 均未漂移；目标 iPhone 已解锁，没有运行中的竞争 Xcode UI test/Mirroring 相关进程或 single-DK provider marker。显式 `BLEHUB_NRF52840_PORT=/dev/cu.usbmodem0010502514791` 选择 DK-B，运行原 `run_ios_adapter_startup --authorize-radio` **一次**，没有刷固件/reset，也没有失败后自动重试。

| 项目 | 本次事实 |
|---|---|
| Attempt | `tests/consumers/react-native-internal/ios/build/ios-adapter-startup-e4cd52fe39854b96b09c80d266d22e30/` |
| Run ID | `17132656-b299-443b-b9e3-cb825a5a4be8` |
| 入口日志 | `.work/ios-adapter-foundation/validation/rf-authorized-attempt-02.log`；entry exit **2** |
| XCTest | 方法实际执行，summary **1 failed**，总窗口约 **50.8 秒** |
| Settings | 原始 accessibility attachments 的 labelled switch 与 child 均证明 **ON(1) → OFF(0) → ON(1)** |
| OFF 分支 | 失败 journal 中含真实 `PoweredOff + Authorized`，scan command `31002` 以 `adapter_unavailable` 结束、17 resource counters 均为零、`off:passed` |
| Native identity | PID `91173`，boot `4459cefb-ee86-4175-a073-d9d96c19cec6`；不得将随后结果合并为另一 session 的连续性证据 |
| Host ready | `ready_delivered=false / window=null`，没有完成 guarded readiness，不是 readiness guard 已拒绝的证据 |
| RF / GAP | `connections=0 / disconnections=0`，没有 fresh-Probe RF recovery |
| Physical cleanup | `advertising_stopped=true / hold_inactive=true / peer_count=0 / engine_flags=0 / cleanup_errors=[]` |
| Phone | 无最终 control attachment / final ON proof，新 run 再次 `quarantined / restoration=unverified`；未解除 |

上述 OFF 内容属于原始 failed journal 的部分 checkpoint；其 envelope 最终为 `unknown-stale-or-failed-marker`，末尾含 `on signal timed out after 30000ms`，不能称完整 runtime ledger 通过。中间 ON readback 也不能代替最终恢复证明。DK-B 在本次 physical lease 内重新 attestation，receipt hash `269c811a4211cc36e545567f556cc31878fdb3c574a22fa00d1d08781dc1374a`；安装 digest 仍为已记录 v2-B candidate。failed bundle 的 `host-rf-evidence-check` 离线 integrity 通过，不授权新 RF。

### 主失败与诊断失败分离：不是 BLE 恢复逻辑的已证实缺陷

实时 raw log 没有 `BLEHUB_IOS_ADAPTER_WINDOW_READY`，但同一结果包的 **action log / testDetails.emittedOutput** 含完整请求，身份与原 journal 一致；原 `verify_window_ready` 可接受该原始请求。因此 XCTest 已发出消息，不是没有走到请求步骤。控制 capture 只读取重定向的 xcodebuild stdout/stderr；当次命令带 `-quiet`，本机 Xcode help 明确其只输出 warning/error。请求仅在事后 action log 找回，host 没有在窗口内收到并投递 ready。

活动树显示约 t=24 秒开始 host-ready 等待，约 t=39 秒结束；保存的 App signal 仍为 `Awaiting on URL (30s)`，未记录确认 tap。随后 catch 对**后台** Preferences 调用 `settings.screenshot()`，快照重试至约 t=47 秒后报 `Failed to get matching snapshot: Error getting main window kAXErrorServerNotFound`。这个二次诊断失败阻断 cleanup/control attachment 发布，最终 XCTest failure text 因而掩盖了原握手超时。不能只凭该 final text 归因为 RN 页面、系统蓝牙或镜像故障。

原始活动/action 导出保存为 `.work/ios-adapter-foundation/validation/rf-attempt-02-{activities,action}.json`；原 App journal、66 个 attachments 及 manifest 保存在 attempt 目录，不改写或补造最终 control。

### 已修正的软件路径与新构建，未重新执行手机测试

1. [startup capture](../../../tools/react_native/ios_adapter_startup_capture.py) 的实时控制命令移除 `-quiet`；严格 evidence command 与 owner-local 样本同步更新。仍将完整输出写 ignored raw log，只向对话显示有界摘要；没有删除其他只读 inspect 历史命令的 `-quiet`。
2. 增加回归，确保 capture 命令不带 `-quiet`，且重新插入该参数、重算 inventory hash 也不能骗过 bundle checker。capture/evidence/runner 定向验证 **64 tests OK**。
3. 原 UI target 的 startup-off catch/cleanup 路径不再先对 Settings 截图，避免不可可靠捕获的 XCTest snapshot failure 阻断恢复和 primary/control 发布；正常 ON/OFF readback 的截图不变。未放宽 watchdog、identity、17-zero、guard、deadline 或 RF verdict。

最终 canonical `pixi run just react-native tools-fixture-test` **1184 tests OK**；`pixi run just react-native host-check` 通过。旧 failed bundle integrity、fmt/lint 通过，完整日志位于同一 `validation/` 下的 `handshake-*` / `restoration-*` 文件。纯工具源码之外只改了原 Swift UI test 的失败诊断路径，未改 RN/JS/生产 ABI，未重跑 Jest。

在独立 `ios/build/device-ui-handshake-derived/` 执行同一 workspace/scheme 的 signed `build-for-testing`，保留旧 derived candidate 与原 RF 证据不变。构建 exit **0**，新 App/UI runner 的 `codesign --verify --deep --strict` 和 iOS single-image 检查通过。新候选的 125-file inventory / xctestrun receipt 保存为 `.work/ios-adapter-foundation/validation/handshake-candidate-receipt.json`，完整构建日志 `handshake-build-for-testing.log`。这只是新构建证据：**未安装、未启动 App、未执行新 XCTest，也未再次 OFF→ON**。

去掉 `-quiet` 消除了已证实的抑制选项，但还不能保证真实窗口内及时转发；后续新增了独立 `ios-adapter-handshake` 非 RF 预检：同一签名 RN/UI runner 只运行 `testAdapterReadyTransportWithoutRadio`，不打开 Settings、不启动 fixture/Probe、不操作蓝牙，host 只验证 marker 在 XCTest 退出前进入 raw stdout。新的 RF mutation 必须再次明确授权；iOS active-loss 仍不开放，本轮没有 RF pass。

### 2026-10-20：非 RF 实时 stdout handshake 预检通过

为避免再次用 RF 窗口诊断传输问题，新增 [ios_adapter_handshake](../../../tools/react_native/ios_adapter_handshake.py) 与 UI target 的 `testAdapterReadyTransportWithoutRadio`。它要求独立环境 gate 和 canonical run ID，输出 exact schema 的 `BLEHUB_IOS_ADAPTER_HANDSHAKE_READY`，然后正常结束；没有 Settings、CoreBluetooth、Probe、Peripheral、phone lease 或 RF controller 路径。staging 仅保留该 UI method、清空 launch arguments，并拒绝 repetition/retry 选项。

新候选在 `tests/consumers/react-native-internal/ios/build/device-ui-handshake-derived-02/` 执行 signed `build-for-testing` 通过；staged xctestrun 和 runner marker 离线检查通过，未在构建阶段安装/执行。随后使用该候选在目标 iPhone 上执行一次非 RF `test-without-building`，无 `-quiet`，完整 host 输出保存在 `.work/ios-adapter-foundation/validation/handshake-transport-device.log` 与同名 raw/result/capture 文件。

实际 capture：

- scope `ios-adapter-handshake`，status `passed`；目标 UDID `00008110-000C61160C39401E`。
- run ID `2f770fc0-aadf-4acd-b597-ecc3a020f2c5`，test-process PID `93304`，marker nonce `edfee077-252c-432e-bb4a-a41a1293b468`。
- raw stdout 中 marker **恰好 1 次**，在 XCTest 退出前被 host 捕获；xcresult summary 为单项成功。
- `radio_mutated=false / fixture_started=false / probe_started=false`；没有 DK-B 连接、没有手机 lease、没有 Settings 操作。

这只闭合了 **XCTest→xcodebuild raw stdout→host PrefixStream 的实时传输**，不证明 startup-off 的 RF 业务路径、Settings OFF/ON、backend OFF、same-JS、fresh Probe、GAP 或 cleanup。新增 handshake staging 的 3 项硬件无关回归通过；随后 canonical `tools-fixture-test` **1182 tests OK**（一次全量运行出现既有 iOS `sdk-receipt` capture 波动，单测与完整重跑均通过，未改其断言）。本节后续已对 attempt-02 做 fresh ON 检查并完成显式 restoration-only 释放；新的 OFF→ON RF 尝试仍需单独授权，不能把 handshake pass 当作 RF pass。

### 2026-10-20：attempt-02 fresh ON 检查与 restoration-only 释放完成

在不切换蓝牙、不启动 DK/Probe 的范围内，使用当前 handshake 修复后的 signed candidate staging 执行一次 inspect-only：

- 输入 receipt：`.work/ios-adapter-foundation/validation/restoration-attempt02-inspect-f00c9f7777cb404b95f0ec1c2a675dc4-input.json`。
- xctestrun：`tests/consumers/react-native-internal/ios/build/device-ui-handshake-derived-02/Build/Products/restoration-attempt02-inspect-f00c9f7777cb404b95f0ec1c2a675dc4.xctestrun`；result：`.work/ios-adapter-foundation/validation/restoration-attempt02-inspect-f00c9f7777cb404b95f0ec1c2a675dc4.xcresult`；完整日志为同名 `.log`。
- `testBluetoothSettingsOffAndRestore()` summary 为单项通过；真实 Settings labelled Bluetooth switch 与 child 均为 `1`。collector/verifier 最终 proof 保存在 `restoration-attempt02-inspect-f00c9f7777cb404b95f0ec1c2a675dc4-proof.json`，独立 fresh ON 证据生成约 460 秒前。
- 首次离线导出过程中出现一次 source/candidate inventory drift；没有重跑手机 XCTest，只对同一个已完成 xcresult 做本地诊断与重导出，第二次 collector/verifier 成功。候选 App/runner bytes 未发现实际差异，过程证据保存在 `attempt02-inspection-inventory-diagnostic{,2}.json`。

随后用原 attempt-02 run/device、原 owner SHA-256 `42311c009744e6b46fa364d28bcc04bc4e8ea2d6c8974487a17e1221f6b6b674` 和原 journal SHA-256 `6ea4ebd2e632b0692d1b2b7cf2fe6a8ee9c578238a293fff8b9c75910cfea417` 执行 `restore_quarantined_phone --authorize-restoration-only`。结果位于 `.work/ios-adapter-foundation/restoration-release-attempt02-20261020/`：archive inode/owner 匹配、active lock 名称已归档、`.restoration-pending` 不存在，原失败 `ios-radio-lease.json` 内容/hash 保持不变。此操作只释放 phone 占用，不修改原失败 verdict，也不授权新的 radio mutation。

当前可继续的唯一 destructive step 是新的 startup-off `Settings ON→OFF→ON + RF` 尝试；需要用户对这一次系统蓝牙 mutation 重新明确授权。

### 2026-10-20：attempt-03 identity false rejection，已修正并完成隔离恢复

用户继续后执行了一次新的 startup-off OFF→ON + DK-B RF gate（attempt-03），使用当时已通过 handshake 的 candidate；这是本轮唯一新的 destructive RF 授权窗口，没有自动重试。

- Attempt：`tests/consumers/react-native-internal/ios/build/ios-adapter-startup-525ad96d1ca04899af7d3b2b8852edf4/`；run ID `df732e76-cd8d-499e-b881-a9d4c71e90a7`。
- Settings 与 backend OFF 已实际执行：journal 含 `PoweredOff + Authorized`、scan `adapter_unavailable`、OFF `passed` 和 17 项资源归零；Settings ON readback 也完成。
- XCTest emitted window marker 已进入 host raw log，但 capture 在 ready guard 前拒绝：`startup-off identity differs from official installed executable`。实际 App journal executable 使用 container UUID `BC4B22AA-...`，而 parent 的 `devicectl install` receipt 使用了另一个安装 UUID；bundle ID、`BleHubNitroHost.app` 和 `BleHubNitroHost` executable 均一致。这是 iOS XCTest 重装路径造成的稳定 UUID 差异，不是 foreign App/PID 或 BLE identity 证据。
- 因未完成 guarded ready、没有 control attachment/final restoration proof，attempt-03 phone lease 进入 quarantine；physical cleanup 为 `connections=0 / disconnections=0 / advertising_stopped=true / hold_inactive=true / peer_count=0 / cleanup_errors=[]`，没有 fresh-Probe RF/GAP。

根修复位于 [capture transport](../../../tools/react_native/capture_transport.py) 与 [startup capture](../../../tools/react_native/ios_adapter_startup_capture.py)：iOS executable 比较现在严格要求同一 container App 路径尾部（bundle App 名称和 executable 名称），但允许 `/private/var/containers/Bundle/Application/<UUID>/` 的重装 UUID 改变；不同 App、不同 executable、非法 URI 仍拒绝。新增回归覆盖 UUID 重装接受和 foreign identity 拒绝，相关 startup capture 定向 **50 tests OK**。没有放宽 PID、bundle ID、boot/nonce、run identity、watchdog 或 RF ledger。

修复候选在 `tests/consumers/react-native-internal/ios/build/device-ui-startup-fixed-derived/` signed `build-for-testing` 通过。attempt-03 随后使用 fresh inspect-only ON（读数 1）完成 restoration-only 归档：输出 `.work/ios-adapter-foundation/restoration-release-attempt03-20261020/`，原失败 journal/hash 保持不变，active lock 已归档且 pending blocker 清除。fresh inspect 的首次本地导出出现同一 source/candidate inventory 瞬时失败，未再次操作手机；对已完成 xcresult 的离线重导出成功，最终 proof 位于 `validation/restoration-attempt03-inspect-6900a8e15e424f8ebaf774330272428b-proof.json`。

attempt-03 不重标为通过。下一次须使用 `device-ui-startup-fixed-derived` 的精确 App/xctestrun，并重新取得一次明确的 OFF→ON + RF authorization；本次 identity 修复本身没有形成 RF pass。

### 2026-10-20：attempt-04 越过 ready identity，payload receipt 仍有同类 UUID 比较，已修正

使用 `device-ui-startup-fixed-derived` 执行一次新的、明确授权的 startup-off 窗口（attempt-04，run ID `ecaf19bc-487f-4fd6-a954-ae0633ef5ac8`）。本次实际完成了：

- Settings ON→OFF→ON；OFF journal 的 `PoweredOff + Authorized`、`adapter_unavailable`、17 项资源归零和 `off:passed` 均存在。
- ready window 已成功交付，physical boundary 记录 `on-window-ready`；说明上一轮 executable container UUID false rejection 已被修正。
- 仍未进入 fresh Probe/RF/GAP。发送 host payload URL 时，`capture_transport._relay` 的第二处 executable 字节比较拒绝了官方 launch receipt：错误为 `official payload launch receipt changed device/PID/URL`。install receipt 的 container UUID 与 payload launch/process receipt 不同，但 bundle/executable 尾部一致。
- physical cleanup 为 `connections=0 / disconnections=0 / advertising_stopped=true / hold_inactive=true / peer_count=0 / cleanup_errors=[]`；phone lease 因无最终 control attachment 再次 quarantine。

已将 [capture transport](../../../tools/react_native/capture_transport.py) 的 payload receipt 检查统一改用同一 UUID-aware executable matcher，并保留 device identifier、PID、payload URL、bundle argument、禁止 terminate/console 等严格检查。startup capture 定向回归继续 **50 tests OK**，未放宽 RF/identity/lease 语义。attempt-04 的 fresh inspect-only ON proof 读数为 1，之后 restoration-only archive 位于 `.work/ios-adapter-foundation/restoration-release-attempt04-20261020/`；原失败 journal/hash 未改写，active lock 已归档且 pending blocker 清除。

attempt-04 仍是 failed，不能重标为 RF pass。下一次新的 destructive RF authorization 将使用包含 payload receipt 修复的候选；本轮已消耗的 OFF→ON 授权不自动延伸。

### 2026-10-20：fixed2 候选完成 build-only 验收

将 executable UUID matcher 同步应用到 payload launch receipt 后，新的候选位于 `tests/consumers/react-native-internal/ios/build/device-ui-startup-fixed2-derived/`。App/UI runner `codesign --verify --deep --strict` 通过，iOS single-image 通过，runner handshake marker 存在；App 114 files、UI runner 11 files，xctestrun SHA-256 `e22ceea6a53ce3e65e07613dfdd55800436d2aab07d2bd1ec8b361ed49f02ef0`。本轮仅 build/离线检查，未安装、未执行 XCTest、未切换蓝牙；receipt 为 `.work/ios-adapter-foundation/validation/startup-fixed2-candidate-receipt.json`。

下一次真机 RF 需使用该 fixed2 App/xctestrun，并重新取得明确 OFF→ON 授权。

### 2026-10-20：startup RF runner 增加 automation handshake 硬前置

针对多次 automation mode 初始化失败，`run_ios_adapter_startup` 现在在 `preflight_fixture` 的输入校验之后、fixture/provider 和 `IosRadioLease` 之前调用非 RF `run_handshake`。因此：

- automation preflight 失败只留下 handshake capture/attempt 目录；不启动 DK、不创建 phone lease、不进入 radio mutation。
- contract/profile 输入本身无效时，在 preflight/handshake 前拒绝，不创建 attempt。
- handshake 通过后才进入原有 fixture attestation、phone owner、install 和 startup-off RF window。

新增 runner 回归证明 automation failure 时 `events=[] / commands=[] / phones=[] / phone-locks` 不存在；canonical `tools-fixture-test` 本轮为 **1184 tests OK**。这一步改变的是失败隔离顺序，不放宽 RF 或 automation 判定。fixed2 当前仍未取得 RF pass；attempt-05 的 automation quarantine 仍需系统授权恢复后只读释放。

### 2026-10-20：attempt-05 在 automation mode 初始化前失败

使用 fixed2 candidate 执行了一次新的授权窗口（run ID `09564585-6a41-48ec-93eb-60ec1eb0a5ef`），但 XCUITest runner 在 test body 前再次失败：

```text
The test runner failed to initialize for UI testing.
Underlying Error: Timed out while enabling automation mode.
```

结果 summary 为 `1 failed / 0 passed` 的 runner system failure，未生成 startup journal、ready marker 或 control attachment；physical provider 只完成 attested startup/cleanup，`connections=0 / disconnections=0 / advertising_stopped=true / hold_inactive=true / peer_count=0`。因此本次没有执行 Settings OFF/ON，也没有 RF mutation，不能称 fixed2 candidate 或 RF 逻辑失败。

Attempt 目录为 `tests/consumers/react-native-internal/ios/build/ios-adapter-startup-6223047bdb0d46d48b1fe815ec0bab9d/`，完整日志为 `.work/ios-adapter-foundation/validation/rf-authorized-attempt-05.log`。由于没有最终 ON/control proof，phone lease 按设计保持 quarantine；不要凭 test body 未运行推断并清除。后续需先由用户在系统窗口恢复 XCUITest automation authorization，再执行一次只读 Settings ON 检查和 restoration-only release；在此之前不再自动重试 UI 或 RF。

随后用户继续后，针对 attempt-05 又执行了一次单独 inspect-only recovery（不切蓝牙、不启动 DK/RF），结果仍为约 62.7 秒后的 `Timed out while enabling automation mode`，没有进入 inspect test body、没有 Settings readback；结果包为 `.work/ios-adapter-foundation/validation/restoration-attempt05-inspect-b6be371216ec1c2a675dc4.xcresult`，完整日志为同目录对应 `.log`。因此 attempt-05 quarantine 继续保留，不能用历史 ON 观察或“test body 未运行”的推断释放。需要用户先在系统 UI 完成/恢复 automation authorization，再进行一次 inspect-only；在此之前不再重试。用户随后要求继续并观察弹窗；第一次因复用已有 result 被 Xcode 拒绝覆盖（exit 64，无测试执行），随后使用全新 staging/result 执行 inspect-only，仍在约 62.6 秒后报告 `Timed out while enabling automation mode`（exit 65），没有进入 test body、没有 Settings readback 或 radio mutation。新结果为 `.work/ios-adapter-foundation/validation/restoration-attempt05-inspect-retry-41dc5b9b3d5c4cc4b6e2cf28a9571601.xcresult`，完整日志为同名 `.log`。

用户确认 Mac mini 上已将 Zed 加入 Automation、将 Terminal 加入 Developer Tools 和 Accessibility，但仍未看到系统授权弹窗。随后再次使用新的唯一 staging/result 运行 inspect-only：`automation-authorized-inspect-2cd528c3546545eca1f0838ed2c3139f.xcresult`，runner PID `97414`，仍在约 62.5 秒报告 `Timed out while enabling automation mode`，未进入 test body。由此可见这是 XCUITest/testmanagerd 与设备 automation session 的运行时握手问题，不是当前权限列表中简单漏掉 Terminal；不应继续通过重复 RF/inspect 命令消耗尝试。

### 2026-10-21：用户完成授权后，non-RF handshake 仍在 automation mode 失败

用户确认已完成 Mac mini 本地授权后，按安全顺序只运行一次新的 non-RF `ios-adapter-handshake`，使用 fixed2 App/xctestrun，不启动 Settings、fixture、Probe 或 RF。执行前 `devicectl device info lockState` 仍为 `passcodeRequired=false / unlockedSinceBoot=true`。

- capture：`.work/ios-adapter-foundation/validation/handshake-after-authorization-20261021-01.capture.json`
- result：`.work/ios-adapter-foundation/validation/handshake-after-authorization-20261021-01.xcresult`
- raw log：同名 `.raw.log`
- runner PID：`1397`
- result：XCTest 启动后约 60 秒报告 `Timed out while enabling automation mode`，未进入 test body、未产生 handshake marker、未产生 Settings readback。
- `radio_mutated=false / fixture_started=false / probe_started=false`。

因此授权列表更新没有闭合设备 automation session；attempt-05 quarantine 继续保留，不能释放，也不应继续重复 inspect/RF。下一步需要处理 Mac/XCTest/testmanagerd 与该物理设备的运行时 session 问题，可能需要用户明确授权的本地环境恢复动作；本次没有终止现有 App 进程、重启设备或修改蓝牙状态。

### 2026-10-21：关闭 iPhone Mirroring 后 non-RF handshake 通过

用户重启 Mac mini 后关闭了正在运行的 `iPhone Mirroring.app`。只读预检确认没有 `iPhone Mirroring`、`xcodebuild`、`XCTRunner` 或 `testmanagerd` 残留进程；目标 iPhone 仍为 `passcodeRequired=false / unlockedSinceBoot=true`。随后使用 fixed2 App/xctestrun 执行一次新的 non-RF handshake：

- capture：`.work/ios-adapter-foundation/validation/handshake-after-mirroring-closed-20261021-01.capture.json`
- result：`.work/ios-adapter-foundation/validation/handshake-after-mirroring-closed-20261021-01.xcresult`
- runner test-process PID：`1917`
- status：`passed`，`passedTests=1 / failedTests=0 / skippedTests=0`
- marker：恰好一次，`radio_mutated=false / fixture_started=false / probe_started=false`

这证明关闭 iPhone Mirroring 后，XCTest→testmanagerd→物理 iPhone 的 non-RF automation transport 已恢复；仍不证明 Settings OFF/ON、backend OFF、fresh Probe、GAP 或 RF。下一步若获明确的“只读 Settings ON 检查”授权，可为 attempt-05 取得 fresh ON proof 并执行 restoration-only release；该步骤不点击开关、不启动 DK/Probe、不运行 RF。

### 2026-10-21：attempt-05 fresh ON 与 restoration-only release 完成

用户明确继续后，执行一次只读 Settings inspection：fixed2 inspect-only staging/result，唯一测试为 `testBluetoothSettingsOffAndRestore` 的 inspect-only 分支；没有点击蓝牙开关、没有启动 DK/Probe、没有 RF mutation。

- result：`.work/ios-adapter-foundation/validation/radio-recovery-inspect-after-mirroring-20261021-01.xcresult`
- input：`.work/ios-adapter-foundation/validation/rf-recovery-inspect-input-after-mirroring-20261021-01.json`
- restoration output：`.work/ios-adapter-foundation/validation/restoration-release-attempt05-20261021-02/`
- inspection：`inspected_switch_value=1`，summary `1 passed / 0 failed / 0 skipped`，窗口约 8 秒。

第一次离线 collection 在 summary 导出后遇到 candidate inventory drift，未提交 archive；quarantine 未改变。使用同一已完成 xcresult 和全新空 output 目录重试，完整 source/candidate/result/output integrity 检查通过，`restore_quarantined_phone --authorize-restoration-only` 成功归档 quarantine。原 owner hash `520306dc...ff589a7`、原 journal hash `2038e9ec...f4c4df` 已绑定，原 `ios-radio-lease.json` 内容和 failed verdict 未改写，active quarantine lock 已归档，`rf_gate_passed=false`。

这一步只释放 attempt-05 的 phone quarantine，不授权新的 radio mutation。下一次 startup-off 的 Settings OFF→ON + DK-B RF 需要用户单独明确授权；授权前不运行 `IOS-RADIO-GATE`。

### 2026-10-21：attempt-06 越过 automation/ready，连接阶段暴露 JS advertisement 消费 bug

用户继续后执行了一次新的 fixed2 `run_ios_adapter_startup --authorize-radio`，明确使用 DK-B `/dev/cu.usbmodem0010502514791` 与 v2-B contract/profile。此次已通过：

- non-RF handshake；
- startup App/native identity；
- Settings OFF→ON；
- OFF journal、17 项资源归零；
- `BLEHUB_IOS_ADAPTER_WINDOW_READY`；
- DK-B physical readiness；
- iPhone scan start、正确 service UUID advertisement、scan stop 和 2 秒 silence。

Attempt 目录：`tests/consumers/react-native-internal/ios/build/ios-adapter-startup-8f56d6e15ffb4228a34a5c16cf5598c7/`，run ID `bcec9f9d-2acb-43a4-ab05-b5aff5f0ff3a`。最终失败为：

```text
expected connectionStateChanged:9005:9007:Connecting,
got advertisementObserved:missing:missing:missing:no error
```

Physical cleanup 为 `connections=0 / disconnections=0 / advertising_stopped=true / hold_inactive=true / peer_count=0`，没有完成 Probe GATT/RF pass。lease 因本次 failed bundle 缺少 source control hash 保持 quarantine；没有自动释放。

根因已由独立 synthetic replay 定位：RN `createRfEventStream` 在 connect 前只消费了第一个 advertisement；扫描期间第二条合法 advertisement 留在未消费队列，随后 `waitForNext` 将其误认为 `Connecting` 回调。修复位于 `tests/consumers/react-native-internal/src/App.tsx`：增加显式 `consumeAll`，在 scan history audit 后消费剩余同一 scan session 的 advertisement。新增回归覆盖重复 advertisement，软件验证 `App.rf-lab.test.tsx` **72 tests passed**，TypeScript typecheck 通过。

Attempt-06 保持 failed，不重标为 RF pass；修复后的真实 RF 重试需要新的明确授权，不自动延用本次已消耗的 OFF→ON 授权。
