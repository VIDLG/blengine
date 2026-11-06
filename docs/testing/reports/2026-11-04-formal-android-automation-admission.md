# Formal Android automation admission：本地静态审计

日期：2026-11-04。状态：当前 Android automation 路径不满足 formal prepared scan 准入要求；正式入口已 fail closed，未进入真机 RF。

## 范围与结论

- 本报告基于本地 driver、随附依赖源码及关键 `build/lib` JS 的只读审计，不是上游所有版本的结论。
- 未执行设备命令、真机 session、安装、权限调整、radio 操作或 RF；未联网，未修改 `~/.appium`。
- **`skipServerInstallation` + `skipDeviceInitialization` 不等于只读准备，也不保证无隐藏 repair/root/grant/settings mutation。**
- 这是 formal Android scan automation 的归属/收尾缺口，不是放弃 Appium、切换测试框架或修改 BleHub/BLeng 产品架构；下述副作用不能一概视为禁止动作，见后续 root 边界更正。
- 关联上下文：[automation ownership](2026-11-04-formal-automation-ownership.md)、[prepared scan](2026-11-04-formal-prepared-scan.md)。

## 用户澄清后的 root 边界更正

现有唯一 Android 手机**已经 root**，项目也已有 `adb push + su -c pm install` 安装器。安装器人工确认与自动化所有权是不同问题；此前将 `no-root` / 全程无任何 settings mutation 当作普遍前提过严，不是用户要求。源码中的 `su` 本身不构成架构阻塞，也不能据此决定 fork driver。

- 显式 provisioning 可复用现有 root-aware installer，包括其 installer UI 清理与 helper grants；不是要求用户手动安装，也不是为普通 scan 隐式增加 setup。
- Appium 必要的 helper/settings 动作按作用范围、BLE 场景影响和恢复结果评估。自动化内部 instrumentation retry 不自动等于重跑 BLE 场景。
- 尚未完成的是具体 platform ownership、未知旧 session/process 的拒绝、必要变更的基线/收尾读回；root 存在不能替代这些证据。目前正式 guard 保留，但错误原因已去掉 blanket no-root/root-fallback 阻塞。
- 优先接 stock driver 与 host 控制，不假设必须最小扩展或 fork；只有实践证明现有配置/host 边界无法满足时才另行评审。

继续实现中还发现本地 helper resolver 可在缺 server 时返回两个 APK，也会返回多个 server 版本；空文件或链接可能在其他 APK 已安装后才失败。现已在首条设备命令前要求唯一完整 triplet，拒绝缺失/多版本、空文件、symlink/hardlink 与非 regular 文件。这个局部检查不证明 package metadata/签名、installed hash 或 live session 归属，也不消除文件检查后外部替换的 race。

新增测试贯通原 `install_uiautomator2_support → android_sdk.install_apk → run_instrumentation.install_apk`，仅 mock 实际 subprocess 与 SDK executable 路径：验证 root-aware push/`su -c pm install -r -g`、本轮上传文件清理、失败无 nonroot fallback；ordinary driver attach 不调用 SDK provisioning。这里保留 installer 的现有 helper grant 语义，不变成 DUT BLE permission 授权。

## 本地版本与路径

下列路径别名仅用于展开本机源码位置，不是可移植工具配置：

- `D` = `/Users/vision/.appium/node_modules/appium-uiautomator2-driver`。
- `A` = `D/node_modules/appium-android-driver`；`B` = `D/node_modules/appium-adb`。
- `S` = `D/node_modules/io.appium.settings`；`P` = `D/node_modules/teen_process`。
- Driver **8.7.3**，Android driver **14.2.5**，ADB **16.1.2**，server **10.7.6**，Settings **8.4.5**。
- 版本依据：`D/package.json:3,41–50`、`A/package.json:3`、`B/package.json:3`、`D/node_modules/appium-uiautomator2-server/package.json:3`、`S/package.json:3`。
- 实际 driver 入口是 `build/lib/index.js`：`D/package.json:32–37`；关键风险分支已对照随附编译 JS。

| 对象 | Exact identity | 源码 |
|---|---|---|
| Server | `io.appium.uiautomator2.server` | `D/lib/uiautomator2-server/packages.ts:1` |
| Test | `io.appium.uiautomator2.server.test` | 同文件 `:2` |
| Settings | `io.appium.settings` | `S/lib/constants.ts:1–2` |
| Instrumentation | `io.appium.uiautomator2.server.test/androidx.test.runner.AndroidJUnitRunner` | `D/lib/uiautomator2-server/core.ts:21,382–403` |

## 启动、retry 与隐藏副作用

| 观察 | 条件、源码与准入影响 |
|---|---|
| Server skip 的边界 | `D/lib/uiautomator2-server/session.ts:306–333`：跳过 server 安装及 Doze whitelist 添加，不提供等价 artifact/version assertion；JS `D/build/lib/uiautomator2-server/session.js:267–278`。 |
| Device skip 的边界 | `A/lib/commands/device/common.ts:212–255`：跳过 Settings 安装分支，也跳过 `skipSettingsAppReinstall` 的已安装检查，但后续配置仍执行；JS `A/build/lib/commands/device/common.js:168–206`。 |
| 旧 session 接管 | `D/lib/uiautomator2-server/core.ts:174–175,417–450`：启动先查询并 DELETE 旧 sessions，再尝试 force-stop server/test，错误可被吞；两个 skip 均不阻止。 |
| 自动重试 | 同文件 `:183–236,451–455`：instrumentation 异常退出后 strict cleanup、等待 3 秒再启动，最多两次启动；HTTP status 等待超时分支直接抛错，不是所有失败都 retry。 |
| 条件式 root | strict cleanup 按名称杀 `uiautomator`；`B/lib/tools/process-commands.ts:122–172` 在权限不足时 privileged retry，`B/lib/tools/system-calls.ts:595–607` 可构造 `su root`。**这是 root 路径，不是必然执行 `adb root`；root 本身允许，按名称清理未知进程的 scope 仍须核对。** |
| Hidden-API 写入 | API ≥28 不受两个 skip 控制：`D/lib/uiautomator2-server/session.ts:123–143`；`B/lib/tools/device-settings.ts:14–18,307–315` 写三个 global hidden-API policy 键为 `1`。 |
| 忽略错误不是跳过 | **`ignoreHiddenApiPolicyError=true` 仍尝试写入/删除，只改变错误处理**；同上及 `B/lib/tools/device-settings.ts:327–335`。 |
| 实机 mock-location | `A/lib/commands/device/common.ts:245–255` 默认执行 AppOps allow；空字符串走 reset/deny，不是禁用。`A/lib/commands/geolocation.ts:163–190,199–223` 还可能 push `/data/local/tmp/mock_apps.json`。 |
| ADB repair | `A/lib/commands/device/common.ts:26–38` 调用设备重试枚举；`B/lib/tools/system-calls.ts:326–354,395–404,521–532` 可 reconnect、重启 ADB server、重试原命令。 |
| suppress 的边界 | **`suppressKillServer=true` 不能消除 repair**：仅抑制部分 server restart/start 路径，不禁止 reconnect/command retry；`B/lib/adb.ts:359–370` 及上一行源码。 |
| 条件式设备配置 | `gpsEnabled` 有值即操作（false 也会写）；`D/lib/uiautomator2-server/session.ts:144–150`、`B/lib/tools/device-settings.ts:272–278`。Locale/IME/timezone 仍在 skip 分支外，`hideKeyboard=false` 会 `ime reset`：`A/lib/commands/device/common.ts:257–301`。 |
| 独立 AUT/unlock 路径 | `D/lib/commands/aut.ts:34–85,104–124` 可安装、reset、stop/retry AUT；`D/lib/uiautomator2-server/session.ts:221–240` 可 unlock/改方向，不由两个 skip 统一禁止。 |

未发现普通启动必经路径自动切换 Bluetooth/Wi-Fi/airplane mode，但这不证明全会话无 radio mutation；GPS/location 已有条件式写入。
未跳过安装时，另有 Settings 安装重试与 grant、失败后完整重装、notification listener/PROJECT_MEDIA 授权和 Doze whitelist 修改：
`A/lib/commands/device/utils.ts:168–219`、`B/lib/tools/apk-utils.ts:401–407`、`S/lib/commands/notifications.ts:66–69`、`S/lib/commands/media-projection.ts:33–36`、`D/lib/uiautomator2-server/session.ts:326–331`。

## DELETE、资源证明与 Settings 持久边界

- `D/lib/uiautomator2-server/core.ts:250–274`：DELETE 无确认只警告；等待退出、停止本机 instrumentation subprocess、force-stop 两包，部分错误被吞。
- 同文件 `:465–484`：进程查询异常被视为已退出，5 秒超时仍继续；**DELETE 返回不是 process-zero 证明**。
- 同文件 `:406–414`、`B/lib/tools/system-calls.ts:620–624`、`P/lib/subprocess.ts:271–281`：停止保存的本机 ADB 子进程（默认 SIGTERM），不独立证明远端 instrumentation 已退出。
- `D/lib/driver.ts:537–544` 仅在 proxy active 时调用 server DELETE；active 在 `D/lib/uiautomator2-server/session.ts:213–217` 成功后才置位，启动中途失败不能假定已清理 instrumentation。
- `D/lib/driver.ts:589–602` 尝试释放 system/MJPEG forward，失败只警告；映射到 device 6790/7810，见 `D/lib/uiautomator2-server/session.ts:33–39,80–113`。不是全量清理，也没有按 owner 再验映射归属。
- **API ≥28 DELETE 删除三个 hidden-API policy 键，不恢复原值**：`D/lib/driver.ts:604–607`、`B/lib/tools/device-settings.ts:327–335`；属于额外 mutation。
- AUT 可被 stop/uninstall：`D/lib/driver.ts:557–575`；还会发现并停止 Settings recorder，未检查是否由本 session 创建：同文件 `:519–522,546–555`、`A/lib/commands/media-projection.ts:75–79,117–123`。
- 普通 DELETE 不普遍 force-stop Settings；`S/lib/client.ts:128–138` 以 foreground service 检查 helper 存活。可将批准基线中的 exact `io.appium.settings` 视为共享持久 helper，但不能通配放行进程后缀或包内所有服务。
- 将来只读 assert 应分别检查 installed identities、active instrumentation、PID/UID、forward、Settings services、AppOps/settings/radio 基线；失败为 failed/unknown，不自动 repair。逻辑 owner close、资源清理、成功页保留分别判定。

## 已落实的 admission guard 边界

1. [`android_appium.require_prepared_driver_contract`](../../../tools/android/android_appium.py) 明确拒绝当前未准入路径，不按版本号或 source substring 推测兼容；升级不能自动放行。
2. [`ScanAutomationOwner.start`](../../../tools/react_native/bleng_automation.py) 在 platform callback、local spawn 或 `webdriver.Remote` 之前调用 guard。失败仍记录 `start-failed.json`，publication 错误不覆盖准入主错误；caller 仍须 close，但没有已接管平台 scope，`require_closed()` 不会伪造平台收尾成功或释放 phone。
3. [`run_bleng_ui`](../../../tools/react_native/run_bleng_ui.py) 同样拒绝 Android，记录 `stage=driver-admission`、`scenario_executed=false` 并由 CLI 返回 2；不因只测 lifecycle 而绕过同一个有副作用 driver。
4. iOS 与 legacy 路径不变，不扩大到产品 BLE backend；不提供 `force` / `allow unsafe`，不修改 `~/.appium`，不通过 provision settings/grants 掩盖 driver 的隐藏动作。
5. 下游 fake-driver ownership/scenario 测试显式模拟准入，并隔离实际 SDK/HTTP/process 边界；新增拒绝测试保留真实 guard，确认未调用 callbacks/spawn/Remote。合成准入不是 installed driver 已通过，也不是已实现完整 Android prepared owner。

## 后续方向（需独立证据与 review）

优先复用 stock Appium，接入有界 host preflight、明确 helper provisioning、同 lease 的 platform ownership，以及必要 settings/AppOps 变更的基线和恢复读回。普通 run 不隐式 provision、不改 DUT BLE permission/radio/firmware，也不接管未知旧 owner；不要求所有 Appium 内部动作天然只读。
若实际验证发现不能由现有配置/host 控制满足的具体缺口，再显式 review 最小 driver 扩展的源码/测试/打包责任；**不直接 vendor/fork，也不切换 Detox**。
源码审计与本轮 helper 准备检查均不解除尚缺证据的 live admission，不是 root/setup 或真机 cleanup 已通过。

## 选定文件指纹（SHA-256）

本轮实际以 `shasum -a 256` 只读计算以下文件；路径按上述别名展开。
这些 hash 仅标识选定本地字节，**不是完整 resolver attestation**：未覆盖全部模块解析、依赖闭包、APK/签名、设备 installed image 或运行时身份，也不证明来自未修改上游发布包。

| 文件 | SHA-256 |
|---|---|
| `D/package.json` | `998e15e52e86757f906a621091d55c9876f940625bcfb86dfece1a6e605a979f` |
| `A/package.json` | `b97a16f7be69b90d1f85d56016d5f548ab56a8caa22136893f2a9ed47f159448` |
| `B/package.json` | `47b8b4a1e017379430123c43ddff0196aa4c2d49b2b81392254b17d096edd2cf` |
| `D/node_modules/appium-uiautomator2-server/package.json` | `9dde9135739a1086ee70f9569d0fba7b02350bff8a2555dbdefdfbfe9eafe0db` |
| `S/package.json` | `cd3e26ea2ca08ed8ccb5dd8b687a9d60fb4c219fdf4da32526e633d5b8e81b02` |
| `D/build/lib/uiautomator2-server/core.js` | `9beb2228a89d02d78c5f92622a47a30b45d95c26ad7967550396281f4a21ba47` |
| `D/build/lib/uiautomator2-server/session.js` | `5912ddf044863340ba87d96e5fdf9dc4b21c79d81f0cd63fe90906640cadc25a` |
| `D/build/lib/driver.js` | `543e409a6db597cd621116cbcd75adf81699844695922d8419f1c22d43a6223d` |
| `A/build/lib/commands/device/common.js` | `d4cf9ba83dbfcedaecab4723c0552c08f12ef7321f7f06c5e42c7ba7d68ee026` |
| `A/build/lib/commands/geolocation.js` | `f197a4ec5c7921ab21a761d2ec065e6d65582d2806cc9f33fdcf33b408e744f1` |
| `B/build/lib/tools/process-commands.js` | `74af1039cebf9dfe681de502b8fc9c9759e0c2711ce2ff4efd8ddf2921236d67` |
| `B/build/lib/tools/system-calls.js` | `c3d9f971ee762ba2e3887fc333f96f5a015c439593719778df72ef29a07584fa` |

## 验证与未完成项

新增两条准入测试先 red：原代码会创建 Android automation / 返回 lifecycle passed。最小 guard 后 green，并增加 publication failure 保留主错误的回归。完整输出在忽略目录 `.work/formal-android-admission-{red,targeted,regression,candidates}.log`。

```sh
pixi run just --command python -W error::ResourceWarning -m unittest \
  tools.tests.react_native.test_bleng_automation \
  tools.tests.react_native.test_mobile_appium \
  tools.tests.react_native.test_capture_io \
  tools.tests.react_native.test_ios_appium \
  tools.tests.react_native.test_android_appium \
  tools.tests.react_native.test_bleng_scan_host \
  tools.tests.react_native.test_bleng_scan_ui \
  tools.tests.react_native.test_bleng_ui -q
pixi run just --command python -W error::ResourceWarning -m unittest \
  tools.tests.react_native.test_bleng_android_candidate \
  tools.tests.react_native.test_bleng_ios_candidate -q
```

- 工具/UI/ownership 回归：**161 passed，11.016 秒**。
- 两端 candidate/prepared handoff 回归：**59 passed，100.982 秒**；仍是实际 owner/临时 lease 与隔离 SDK，不是真机。
- `pixi run just fmt`、`pixi run just lint`：通过；lint 包括 workspace/all-targets Clippy。
- `pixi run just react-native formal-host-check`：typecheck/autolinking/no-internal 通过。
- `pixi run just react-native formal-host-test`：**48 passed**；保留现有 SafeAreaView deprecation warning，不把警告写成失败或另行修改 UI。
- `pixi run just docs-check-mermaid`：通过，检查 198 份 Markdown 中 27 个图；本轮 authored Markdown 另查 UTF-8、fences、尾随空白与 conflict markers。
- 日志另见 `.work/formal-android-admission-{fmt,lint,host-check,host-test,mermaid}.log`。既有广域 tools-test timeout 和 legacy Jest timeout 不升级为通过。
- 未运行真机/安装/RF gates，未获得完整 prepared lifecycle 或 resolver attestation；没有删除 internal 或提交 Git。

### Root 边界更正后的本轮验证

本地 triplet 检查先 red：缺 server、空文件与链接仍可能进入安装。原 root-aware 安装及失败清理测试在改动前已通过，说明不需要为 rooted phone 另造安装方案。修正 resolver 后执行：

```sh
pixi run just --command python -W error::ResourceWarning -m unittest \
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
```

- **188 passed，23.109 秒**，包括五个新增准备边界测试及三个 helper 各自的缺失/空文件/链接/目录输入。
- `pixi run just react-native formal-host-check`：typecheck/autolinking/no-internal 通过。
- `pixi run just react-native formal-host-test`：**48 passed**。
- `pixi run just fmt`、`pixi run just lint`、`pixi run just docs-check-mermaid`：通过。
- 日志：`.work/formal-android-root-preparation-{red,green,regression,fmt,lint,host-check,host-test,mermaid}.log`。
- 所有 synthetic 设备命令在真实 subprocess 边界被隔离；没有执行 `su`、安装、读取或修改真机设置。历史 candidate 59 tests 结果保留为前轮事实，本轮未再次运行它们，也未将广域 timeout 改为通过。
