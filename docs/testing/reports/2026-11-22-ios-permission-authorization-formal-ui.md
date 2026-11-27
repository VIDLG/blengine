# iOS permission-authorization 正式 UI lane

本文件记录 C 类 #5（`permission-authorization`）从 `BleHubNitroHostUIOnly` 迁到正式
`BleHubBlengHostUITests` 的切片：主张、机制、两个共享组件缺陷和两次连续真机结果。
只记录本轮实际跑过的事实。

## 主张

App 必须如实报告 OS 记录下来的蓝牙授权，并且**永不**在没有授权时声称 `poweredOn`：

| 阶段 | 主机侧 | App 侧断言 |
|---|---|---|
| `deny` | 卸载 App → `xcodebuild` 重装 → 真系统弹窗答“不允许” | 新进程 `scan-authorization == denied`、`adapter-state != poweredOn` |
| `allow` | 再卸载 → 重装 → 弹窗答“允许” | 新进程 `scan-authorization == authorized`、`adapter-state == poweredOn` |

两次阶段都由 host 驱动，设备最终停在它开始的 authorized 状态。UI 自动化只做两件 OS 动作：
回答真实系统弹窗、判定 xcresult。无 radio、无 fixture、无 Probe、无 RF。

## 为什么必须卸载/重装

`XCUIApplication.resetAuthorizationStatus(for: .bluetooth)` 在这台设备/这个 iOS 上**不产生
notDetermined**：第一次真机尝试里 reset 之后仍没有弹窗，App 继续报它原来的授权。于是改为
**host 卸载 App + xcodebuild 重装**，用全新安装让 TCC 记录随之消失；这是唯一在真机上可复现
“首次询问”的方式，而且它本来就是 host 拥有的安装动作。

Runner 的每个阶段都会：`devicectl device uninstall app --device <udid> <bundle id>` →
用 `devicectl device info apps --json-output <file>` 回读确认已不在 → 再跑测试。
回读只认 JSON 文件（stdout 是人读表格）。

## 同一个新安装会先弹两个系统框

真机 hierarchy 证明：全新 Debug 安装**先**弹本机网络（`允许“BleHubBlengHost”查找本地网络中的
设备？`），**然后**才在创建 `CBCentralManager` 时弹蓝牙。测试按 label 识别并分别处理：

- 本机网络：允许（Debug 安装的连接需要；它不是被测权限），只认 label 含“本地网络”/“Local Network”。
- 蓝牙：按阶段答允许/不允许；label 不含“蓝牙”/“Bluetooth”时**拒绝作答**（不猜）。

该 iOS 上这类系统框的**子控件不进 accessibility 树**（hierarchy 里 Alert 下面只有 `Other`），
所以 `alert.buttons[...]` 查不到。测试先用 label 查询，失败再按 alert 自身的 frame 计算
底部按钮行的坐标点击（左=不允许，右=允许）。每次点击都留截图，写错会立刻被下一阶段的
授权断言抓住。

## xcresult 是唯一判定

每个阶段单独一个 xcresult，必须是 **1 passed / 0 failed / 0 skipped**；skip（例如 opt-in
环境变量缺失）绝不算通过。App 侧失败信息会带上真实读到的 `scan-authorization`，所以
“没弹窗”不会伪装成“通过”。

## 路上修掉的两个共享组件缺陷

1. **`ios_preflight._xctestrun_runner` 在 multi-target manifest 里选错条目**
   它取第一个带 `TestHostBundleIdentifier` 的对象，于是一个 *app-hosted 单元测试* 被当成 UI
   runner，去找不存在的 `...DeviceTests-Runner.app`。现在优先 `IsUITestBundle`，其次以
   `.xctrunner` 结尾；两者都没有就报“manifest 没有 UI runner”。
2. **`resolve_xctestrun` 只展开部分产品路径**
   它展开 `TestHostPath/TestBundlePath/DependentProductPaths`，却把 `UITargetAppPath` 里的
   `__TESTROOT__` 留下。controller 的 staged manifest 被 `xcodebuild` 解析到暂存目录，报
   `BleHubBlengHost.app couldn't be read`。现在递归展开整个 target；staged 文件里不再有
   `__TESTROOT__`。

两者都有离线回归测试（`test_ios_preflight.py`、`test_bleng_ios_native_common.py`）。

## 真机结果

| 项 | 值 |
|---|---|
| 设备 | `00008110-000C61160C39401E`（iPhone 14 Plus，iOS 27.2.1） |
| App bundle id | `FWVYQ287W5.bleng-rn-host`（设备 build 按 profile 派生） |
| xctestrun | `BleHubBlengHost_iphoneos27.2-arm64.xctestrun`（`BleHubBlengHostUITests` 条目） |
| 只跑 | `BleHubBlengHostUITests/BlengPermissionAuthorizationUITests/testBluetoothConsentPhase` |
| 第 1 次 | `.work/ios-permission-ui-r11/fdb57f05-…/result.json` · deny 1/0/0 · allow 1/0/0 · `status=passed` |
| 第 2 次 | `.work/ios-permission-ui-r12/f508480f-…/result.json` · deny 1/0/0 · allow 1/0/0 · `status=passed` |
| 每次阶段 | `uninstall.removed=true`（回读确认后再跑） |

`pixi run python -m pytest tools/tests/react_native/test_bleng_ios_permission_ui.py`
→ 12 passed / 18 subtests（无硬件，覆盖阶段顺序、每阶段卸载、环境注入、跳过/失败不算通过）。

## 对 C 类核对的影响

#5 的阻塞项关闭：OS 授权弹窗由正式 App 的 UI 测试目标证明，不再安装
`com.blehubnitrohost`。旧 recipe `host-ios-device-permission-test` 已删除。

## 已知边界

- 本机网络“允许”是环境动作，不是产品主张；如果未来 Debug 安装不再需要本地网络，这段可以删。
- 坐标回退只用于“控件不暴露”的系统框；它按 alert frame 计算，但按钮行位置仍可能随 iOS
  版本变化，写错会被授权断言抓住（不是静默通过）。
- 本 lane 不覆盖 Watch / macOS；`#5` 的主张只针对 iPhone 上的 App 授权。
