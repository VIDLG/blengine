# iOS 设备侧 automation-mode 阻塞（2026-11-26，候选事实）

日期：2026-11-26
设备：iPhone 14 Plus `00008110-000C61160C39401E`（iOS 27.2.1 / 24B156）
范围：在 iOS 上补最近五条 managed 场景（`devices-query` / `descriptor-io` / `link-facts` /
`disconnect` / `connect-failure`）的第一条设备证据。

## 结论

设备侧 XCUITest「UI 自动化」握手未生效，UI runner 在**初始化阶段**超时，未进入测试体。这台 Mac
读不到也设不了设备上的开关；按仓库既有流程，此时停止盲试，改要一个设备侧读数/动作。

```text
Testing failed:
  BleHubBlengHostUITests-Runner (… ) encountered an error
  (The test runner failed to initialize for UI testing.
   (Underlying Error: Timed out while enabling automation mode.))
** TEST EXECUTE FAILED **   # xcodebuild exit=65，约 63 s
```

## 已排除（都是读数，不是推断）

| 检查 | 结果 |
|---|---|
| 主机 automation 授权标记 `/var/db/com.apple.dt.automationmode/no-auth-required` | 存在；`xctest_unattended` 报 `host_unattended=yes` |
| 主机 `automationmodetool` 的 "Automation Mode is disabled" | 只是**当前会话**状态，XCTest 按需开启；不是前置条件（见 `xctest_unattended.py` 注释） |
| 手机锁屏 | `passcodeRequired=false`、`unlockedSinceBoot=true` |
| 手机 Developer Mode | enabled |
| 连接方式 | USB（`xcrun xcdevice list` → `interface: usb`，`available: true`） |
| 非 UI 设备测试会话（同一 xctestrun 的 `BleHubBlengHostDeviceTests`） | **4.4 s 通过** → DDI / testmanagerd / 签名 / 安装均正常 |
| iOS Swift（`advance` / `awaitStatusAbove`） | 随 `build-for-testing` **编译通过**；产物含 `BleHubBlengHostUITests-Runner.app`、`BleHubBlengHostUITests.swiftmodule` |

因此失败点唯一：**设备侧 UI 自动化那一层**，与 App 代码、签名、安装、DDI 无关。

## 已尝试的解除动作（都不改变结论）

- 重开 UI 自动化开关 + 重插 USB：仍 exit 65。
- 先跑非 UI 设备测（建立真实设备测试会话）再跑 UI：仍 exit 65。
- 重启 iPhone 后再跑：仍 exit 65。

## 根因：开关只是权限，系统还会单独要一次解锁凭据

设备日志（`.work/ios-xctest-init/diagnosis.*/device-filtered.log`）把这条链条读全了，结论是**读数**，
不是推断：

```text
testmanagerd: requesting automation mode
→ automationmode-writer 创建 LAContext
→ coreauthd: "No identities are enrolled" / "No companion device available"
→ Code=-1004 "User interaction is required"
→ "No previous credential exists"
→ 系统弹出 "Enable UI Automation" 认证框（LocalAuthentication.ui.presented）
→ 60 s 无人应答 → "Timed out while enabling automation mode"
```

也就是说：**设置里的 UI 自动化开关控制的是权限；设备仍需要在这张认证框上输入一次密码（或 Face ID），
才会被记下一份 "previous credential"**。此后每次 automation mode 握手都复用它，不再询问。

行动（一次，手机侧）：**在一次 run 期间，于弹出的 "Enable UI Automation" 认证框上输入设备密码**。
开关已经是开的就不要再关/开，也不要重启设备或重插 USB — 这些都不改变这条链条。

## 已就绪的产物（认证框应答后可立即跑）

- App（已含本轮全部改动）：`tests/consumers/react-native-bleng/ios/build/device-derived/Build/Products/Debug-iphoneos/BleHubBlengHost.app`
- App tree：`5327b8c83ab1c49a75a70fb9944f22484612636ce26505aaa04a6609ad89c0ac`
- xctestrun：`tests/consumers/react-native-bleng/ios/build/device-derived/Build/Products/BleHubBlengHost_iphoneos27.2-arm64.xctestrun`

运行入口（以 `devices-query` 为例，其余五条替换 `--scene`）：

```sh
pixi run just react-native::formal-host-ios-managed-devices-query \
  --device 00008110-000C61160C39401E \
  --app tests/consumers/react-native-bleng/ios/build/device-derived/Build/Products/Debug-iphoneos/BleHubBlengHost.app \
  --app-tree-sha256 5327b8c83ab1c49a75a70fb9944f22484612636ce26505aaa04a6609ad89c0ac \
  --xctestrun tests/consumers/react-native-bleng/ios/build/device-derived/Build/Products/BleHubBlengHost_iphoneos27.2-arm64.xctestrun \
  --fixture-contract tests/peripherals/nrf52840/firmware/contracts/multifunction-rf-v2-a.json \
  --fixture-profile tests/peripherals/nrf52840/firmware/profiles/multifunction-rf-v2-a.json \
  --fixture-port /dev/cu.usbmodem0010502520281 --fixture-resource-identity jlink:1050252028 \
  --output .work/ios-devices-query/2026-11-26-f \
  --authorize-install --authorize-fixture-start --release-before-install
```

## 不做成结论的两件事

- 不把这次失败归因到 Android 端任何改动（该设备上一轮 Android 17 条均通过）。
- 不声称 iOS 五条有任何通过证据；它们仍是 **Android-only**。
