# iOS 七条最新场景：四个真缺陷与一轮真机证据

日期：2026-11-26
设备：iPhone 14 Plus `00008110-000C61160C39401E`（iOS 27.2.1 / 24B156）；DK-A `jlink:1050252028`（v2-a）
范围：`disconnect` / `connect-failure` / `link-facts` / `descriptor-io` / `devices-query` /
`auto-connect` / `devices-kinds` 的**首份 iOS 设备证据**。状态：七条全部 `status=passed`、
`integrity=passed`、无 cleanup 错误；`host_gate` 仍为 `pending`（候选事实，不是 release/Phase 退出）。

## 结论先说

这一轮不是"把 plan 搬到 iOS 上跑一遍"，而是**用 iOS 真机把四个真缺陷逼了出来**——两个在 App/驱动，
两个在 Apple backend。四个都已按根因修掉，两端都重新取证：

| # | 缺陷 | 层 | 修法 |
|---|---|---|---|
| 1 | 键盘不收起，钉住的控件被盖住 | App + 驱动 | 标题成为「点一下收键盘」的确定面；`setText` 成功后驱动收键盘 |
| 2 | `linkEnd` 词表与包不一致（`operator` vs `requested`） | plan + 派生 + 文档 | 统一用包自己的 `BlengEndReason` 词 |
| 3 | CCCD 描述符值在 CoreBluetooth 里是 `NSNumber`，只做 `Data` 转换 → 必读失败 | Apple backend | 一个 `descriptorValueBytes` 解码三形状 |
| 4 | 直接写 CCCD 让 CoreBluetooth **断言崩溃**（不是报错） | Apple backend | 在 dispatch 边界类型化拒绝 |

## 1. 键盘才是"present but not hittable"的根因

`disconnect` 的 `value` 相在 iOS 上失败：

```text
scene step 2 ["op": "tap", "id": "pull-notification"] failed:
  timeout: pull-notification is present but never became hittable within 30.0s
  (frame=(12.0, 622.3, 98.0, 28.0), enabled=true, app=4)
```

帧在屏内、`enabled=true`、进程 `runningForeground`，却点不到。设备侧 SpringBoard 树给出了唯一答案：

```text
Other {{0.0, 578.0}, {428.0, 348.0}}, identifier: 'inputView'   ← 软键盘
```

`setText` 聚焦输入框后键盘一直不收起，占住 y 578–926；`pull-notification` 在 y 622 —— 完全在键盘
下面。2026-11-22 那两个两阶段场景能过，是因为当时这一行还在键盘上方；console 后来长了（`Probe` 页、
`session-controls` 上移、`run-status` 多了 `devicesKind`/`auto`），它才落进键盘区。

修法不是"往上挪"，而是让机器面**有确定的收键盘方式**：

- App：标题 `formal-host-title` 的 `onPress` 调 `Keyboard.dismiss()`。它在每一页都有，plan 从不点它，
  是驱动唯一按名认识的 App 标识（`App.tsx` 注释与
  `tools/tests/react_native/test_workbench_ids.py` 两侧钉住这条契约）。
- 驱动：`setText` 验证取值成功后，若 `app.keyboards` 存在就点它一次，并把 `keyboard=dismissed|
  still present|present, no dismiss surface` 写进该步回执——收不掉是可见事实，不是静默。

顺带两条同源的改进：

- `tap` 不再"一次不可点就失败"，而是在该步预算内轮询 `isHittable`，超时报出帧、`enabled` 与
  `app.state`。一帧转场不该等于一次场景失败。
- `activate` 不再无条件按 Home：只有 App 真的不在前面（或窗口不可点）时才按，随后要求
  App 自己的窗口可点。2026-11-24 那条"无条件按 Home"的决定建立在"切换器吞点击"的假设上；这一轮的
  证据是**键盘**，切换器卡并未出现。现在的失败模式仍然是响亮的（超时信息带状态与切换器卡是否存在），
  而不是 90 s 停在 `idle`。

## 2. `linkEnd` 词表：`operator` → `requested`

`disconnect` 的 `sever` 相在 iOS 上报回：

```text
saw … link=disconnected;linkEnd=requested;gatt=ready;notify=ended …
```

而 plan/派生/报告都还写着 `linkEnd=operator`。包的类型是 `BlengEndReason`
（`requested|unrequested|failed|systemSuspended|connectionClosed|ownerClosed|detached|overflowed`），
App 也是原样发布 `BlengEndReason` —— `operator` 是 adoption 之前的旧词，Android 那份 2026-11-24
的证据取自旧 App。修法只有一条：**工作台不自造归属词汇**，全部改用 `requested`；trace 标记同时改为
`link:end-requested`。历史报告保留当时的 `operator`，不改写。

## 3. Apple backend：CCCD 的值是 `NSNumber`

`descriptor-io` 的第一次读就失败：`descriptor=failed`、`failure=operation/bleng.operation.gatt`。
根因在 backend：

```swift
value: descriptor.value.flatMap { $0 as? Data }   // CCCD 是 NSNumber → nil
```

`CBDescriptor.value` 的形状取决于描述符：特性值是 `Data`，**CCCD 是 `NSNumber`**，用户描述是
`String`。`Data`-only 转换让"这个工作台唯一寻址的描述符"永远没有值。新增
`descriptorValueBytes(_:)`（`platforms/apple/Sources/BleHubApple/DescriptorValueBytes.swift`）：
`Data` 原样、`NSNumber` 取 16 位小端（与 Android 报告的字节一致）、`String` 取 UTF-8，未知形状返回
`nil` 并由调用方报失败而不是猜字节；`BleHubAppleTests/DescriptorValueBytesTests.swift` 钉住四种形状。

## 4. 直接写 CCCD 会让 CoreBluetooth 断言崩溃

修好读之后，`descriptor-io` 的下一次写直接把 App 打崩（`Crash Log … .ips`，`SIGABRT`）：

```text
*** Terminating app due to uncaught exception 'NSInternalInconsistencyException',
reason: 'Client Characteristic Configuration descriptors must be configured using
setNotifyValue:forCharacteristic:'
```

这是 `-[CBPeripheral writeValue:forDescriptor:].cold.3` 里的断言：CoreBluetooth 不允许直接写 CCCD，
而且**崩溃而不是返回错误**。这是"这个能力在这台平台上不存在"，所以按项目一贯口径**类型化拒绝**，
放在 dispatch 边界，绝不把请求递给 core：

```swift
if let descriptor = target.descriptor as? CBDescriptor,
   descriptor.uuid == CBUUID(string: "2902") {
    throw CommandDispatchError.NotSupported(detail: "CoreBluetooth configures a client characteristic
        configuration descriptor through setNotifyValue(_:for:); a direct descriptor write is not permitted")
}
```

App 侧因此多一个终态：`descriptorWrite` 现在是 `none|done|unsupported|failed`，`unsupported` 只在失败
码正是 `bleng.dispatch_rejected.not_supported` 时出现（与 `mtu`/`priority`/`refresh`/`auto` 同一口径）。

## 5. 顺带：拒绝可以落在两个边界，账本要认两种形状

`auto-connect` 在 iOS 上第一次跑被**派生**判失败：

```text
the App's standing-request record failed for a reason other than a missing capability:
[('auto-connect-wait', 'operation/bleng.dispatch_rejected.not_supported: CoreBluetooth cannot request an auto-connect link')]
```

原因是同一个"平台拒绝"可能落在两个位置：引擎在**提交**时拒绝（没有 wait 行），或者提交被接受、
**操作终态**才是拒绝（`descriptor-write`/`auto-connect` 两行都在）。派生原来只认第一种，于是把正确的
平台答案判成坏记录。现在 `_require_auto_connect_ledger` 与 `_require_descriptor_io_ledger` 都接受两个
边界，并且只接受**恰好一次**、且码为 `not_supported` 的拒绝。

`descriptor-io` 的 trace 也顺势收敛成一个标记 `cccd:write-answered`（"写的问题被回答了"），
**哪个答案**由派生分别钉住：Android 必须 `done`，Apple 必须 `unsupported` + 平台码，且写后的读必须
仍是 `0000`。一条 trace 覆盖两个平台，不会为了对称造假场景。

## 真机证据（iOS，2026-11-26）

App 树 `cb09bdfc…`（Debug，含本节全部改动）；每份都跑过
`python -m tools.react_native.bleng_scan_evidence`，`integrity=passed`、`phone=passed`、
`fixture=passed`、`cleanup_errors=[]`：

| 场景 | 运行 | 场景片 | trace |
|---|---|---|---|
| `devices-kinds` | `.work/ios-native-devices-kinds/2026-11-26-b` | 17 s | 5/5（两种清单都 `unsupported` + `bleng.dispatch_rejected.not_supported`） |
| `disconnect` | `.work/ios-native-disconnect/2026-11-26-a` | 50 s | 15/15（含 `link:end-requested`） |
| `connect-failure` | `.work/ios-native-connect-failure/2026-11-26-a` | 71 s | 全 marker |
| `link-facts` | `.work/ios-native-link-facts/2026-11-26-a` | 30 s | MTU 查询 `done`，三者 `unsupported` |
| `descriptor-io` | `.work/ios-native-descriptor-io/2026-11-26-a` | 39 s | 14/14（`cccd:write-answered` = 平台拒绝） |
| `devices-query` | `.work/ios-native-devices-query/2026-11-26-a` | 25 s | 9/9 |
| `auto-connect` | `.work/ios-native-auto-connect/2026-11-26-a` | 37 s | 8/8（`auto:answered` = 平台拒绝） |

Android 侧因为词表、plan 与 App 都变了，重新取证了受影响的三条（同一 App APK `a1ed2d68…`）：

| 场景 | 运行 | 结果 |
|---|---|---|
| `disconnect` | `.work/android-native-disconnect/2026-11-26-a` | `passed`，15/15，场景片 25 s |
| `descriptor-io` | `.work/android-native-descriptor-io/2026-11-26-a` | `passed`，写仍为 `done`，场景片 21 s |
| `auto-connect` | `.work/android-native-auto-connect/2026-11-26-a` | `passed`，场景片 19 s |

其余 Android 场景的 App 侧行为未变（收键盘只在 `setText` 后由驱动触发；`descriptorWrite` 只多了
一个平台拒绝时才会出现的终态），因此不重复取证，也不把旧证据升级。

## automation mode：这次能用了，但不要据此下结论

同一天早间同一个 lane 仍连续 6 次 `Timed out while enabling automation mode`（每次 60 s），
到 14:23 再跑同样的探针就通过了，之后七条场景连续跑通。**期间没有任何代码或 host 侧改动能解释这个
变化**，设备端开关状态无法从 Mac 读取，所以这里只记录"当时可用"，不声称根因、也不把
[`2026-11-26-ios-automation-mode-block.md`](2026-11-26-ios-automation-mode-block.md) 里的认证面结论
升级或撤销。下一次遇到同样的超时，先看那份报告的认证框证据，不要再让操作员重启或重插。

## 守卫（不需要设备）

- `test_scene_semantics.py`：`descriptor-io` +3（提交级拒绝、操作级拒绝、`unsupported` 必须带平台码）、
  `auto-connect` +1（操作级拒绝）；`linkEnd` 的负例改为"归给没人"。
- `Descriptor.test.tsx` +1：平台拒绝写 → `descriptorWrite=unsupported`、`failure` 为平台码、字节不变。
- `test_workbench_ids.py` +1：标题的 `onPress={Keyboard.dismiss()}` 与驱动里的 `formal-host-title`
  必须同时存在，否则两边一起红。
- `test_ios_scene_driver.py` 40 条仍全过（plan 形状、禁止滚动、回执契约）。
- Apple：`swift test`（`apple_backend_macos_tests=passed`），含新的 `DescriptorValueBytesTests`。

## 尚未完成

- 序列现在是 19 条，仍**不声称连续通过**：`background` 在序列里失败、单独跑通过，本轮没有重查。
- iOS 只新增这七条的设备证据；其余十二条最近一次 iOS 证据仍是 2026-11-24 那一轮，App 树不同，
  不继承本次结果。
- `tests/hil/apple/support` 里三个 fake backend 未跟上 `BleBackend` 新增成员，
  `just apple::test` 的第二段（rf-scenario 支持包）编译失败——与本轮改动无关，未修。
- 后续：`adapter-control` 已在同日完成并两端取证，见
  [`2026-11-26-adapter-control-scene.md`](2026-11-26-adapter-control-scene.md)。
