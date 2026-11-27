# iOS 原生驱动第四个场景：`io` 真机通过（候选事实）

日期：2026-11-23。范围：把 managed `io` 场景从 Appium 迁到进程内 XCTest 驱动，并在真机 iPhone
对真机 nRF52840 DK 跑通。本文只记候选事实与已修缺陷：不改 gate、不冻结 ABI、不宣称 release 或
Phase exit；`host_gate` 仍为 `pending`（场景通过 ≠ 发布判定）。

## 结论

| 项 | 值 |
|---|---|
| 入口 | `formal-host-ios-managed-io`（现在自动走原生驱动） |
| run id | `f4ef1c28-46bf-4376-ad2f-b5000c8086e9` |
| 结果 | `status=passed`、`host_gate=pending`、`rf=io-ui-observation` |
| 驱动 | `ios-xctest-in-process`，plan `io`，单阶段 44 步 **44.6 s**，`xcodebuild` rc 0 |
| 设备 | `00008110-000C61160C39401E`（iPhone 14 Plus，iOS 27.2.1） |
| App 树 | `5cfabfac16fa188bea05eedf1be70814cfcc4817d94734fad4f97aeaa6e415fa` |
| fixture | `jlink:1050252028`，contract/profile `multifunction-rf-v2-a`，收尾 `stopped` |

12 项 trace 全齐：

```
idle → started → scan:running → scan:advertisement-matched → scan:stopped
     → link:connected → gatt:ready → io:read-initial → io:written → io:readback
     → rssi:observed → closed
```

App 自己发布的读数（回执原值）：

| 事实 | 回执 |
|---|---|
| 写前读 | `fff10000-…: 1 bytes [00]` |
| 写回执 | `fff10000-…: wrote 1 bytes (withResponse)` |
| 写后读 | `fff10000-…: 1 bytes [37]` |
| RSSI | `-52 dBm` |

动作账本 18 条、全部 `succeeded`，顺序与场景声明一致：写入前后各一次 `gatt-read` /
`gatt-read-wait`，中间是 `gatt-write` / `gatt-write-wait`，最后 `rssi`。

## 场景语义（与 Appium 版本一致）

- 特征值只按 **UUID 声明**，由 App 在已发现的拓扑上解析；host 不读拓扑位置。
- **不断言 peer 的初始值**：profile 的 `initial_value_hex` 描述镜像写就时的值，安装镜像当前持有
  什么只能被观测。
- **写回执不是存储值**：写完必须再读一次，且该读数与写前读数不同（`io-readback != io-initial`）。
- RSSI 必须是连接链路上的真实读数（负值），不是屏幕的 idle 占位。
- 全程零手势：场景自己的命令与它自己的回执都在 pinned 面内。

## 这一轮新增的三个决定

1. **写入载荷由 host 从 profile 派生，两条 lane 共用一份。**
   `bleng_scan_host.io_target()` = `read_write_target()` + `_io_write_payload()`：长度等于 fixture
   provision 的值长度（BTP tester 对更长的写返回 `INVALID_ATTRIBUTE_LEN`），字节取一个与当前值不同
   的可打印 ASCII。Appium 版 `exercise_owned_io` 改用同一函数，native 版只把结果声明进 plan，
   因此两个驱动不可能写出不同的 payload。
2. **一次读的归属靠“渲染出来的东西”，不靠阶段、也不靠存在性。**
   App 在派发命令时清空 `gatt-result`，只在命令结算后重新渲染它。于是：
   - 等 `gattRead=done` 只能证明**第一次**读完（第二次读时它本来就是 `done`）；
   - 等元素存在会命中**写回执**；
   - 值渲染 `N bytes [..]` 与写回执 `wrote N bytes (…)` 互不为子串，所以「等到值是值」是唯一
     能区分「我刚请求的那次读」的条件。两个形状常量在 `run_ios_scene_driver` 里各只有一处，
     并由 plan 形状测试钉住顺序（值 → 回执 → 值）。
3. **回执的读数名字必须在一个 plan 内唯一。**
   回执是一张扁平的 `as → 文本` 表，重名会静默覆盖。首轮真机就撞上了：io plan 的第一次 GATT
   读也起名 `initial`，覆盖了 `_prefix` 的 run-status 前置读，host 于是拿
   `fff10000-…: 1 bytes [00]` 去按 `run-status` 解码，运行在 50 s 时报
   `Invalid run-status field`。修法是按来源命名（`io-initial` / `io-written` / `io-readback` /
   `io-rssi`），并加一条单测遍历所有 plan（含按场景组合出来的 `io` 与订阅计划）要求 `as` 唯一。

## 唯一 codec：`GATT_VALUE_TEXT`

App 的字节渲染（`src/useManualScan.ts` 的 `binary()`）形如 `N bytes [xx xx …]`。该形状此前只写在
Appium lane 里；现在归 `bleng_scan_evidence.GATT_VALUE_TEXT`，两条 lane 共用：Appium 版把它包成
`AppiumError`，原生版用它拒绝「渲染了但不是值」的读数（例如把写回执当成值）。Appium lane 删除后，
这个 codec 不会随之消失。

## 离线覆盖

- `test_scene_ios_native.py`：io 需要 profile 目标；目标上声明不可读/不可写要在任何 I/O 之前被拒；
  写回执必须点名该特征与字节数（写类型来自 `gattProps`）；写后读数未变化即失败；读数来自别的特征
  即失败；把写回执当值即失败；RSSI 非负或未读即失败；账本必须显示「读→写→读」的顺序。
- `test_ios_scene_driver.py`：io plan 用同一套 op 词汇、只驱动 pinned 面、以 `close-session` 收尾；
  三次 `gatt-result` 等待的顺序与形状；**所有 plan 的读数名字唯一**。
- `tools-test` 全绿：1755 passed / 21 skipped / 5359 subtests。

## 之后的 App 变化与重跑

随后迁 `cancellation` 时 App 改了两处（`run-status` 新增 `gattReadTotal`；pinned 账本改回真实字段名，
此前一直丢弃失败细节），因此用最终树 `0fe5920d…` 把 `scan → notification → indication → io` 整条重跑：
io 仍 passed（run `bf295476-2aa7-48c5-bbf7-2eb6d142d7ad`，44 步 44.7 s），四场景
`scene_sequence=passed`。本次 io 的候选事实以那次重跑为准，本文上面的数值来自 `5cfabfac…` 树的首次通过，
两者结论一致。

## 边界

- 一次真机通过是**候选事实**：不迁移任何 gate，`host_gate` 仍 `pending`。
- 读回内容只断言「与写前不同」，仍不断言等于写入的字节：peer 是否回显是 fixture 行为而非场景声明。
- Android 仍走 Appium 的 `io` 分支（`AppiumSceneDriver` 按 `scene.kind` 分派，Android 未迁移前该
  分支不可删）；两端同一套 plan 词汇的接缝是 `SceneDriver`。

## 复现命令（iPhone）

```sh
pixi run just react-native formal-host-ios-scene-driver \
  --device 00008110-000C61160C39401E \
  --xctestrun tests/consumers/react-native-bleng/ios/build/device-derived/Build/Products/BleHubBlengHost_iphoneos27.2-arm64.xctestrun \
  --plan probe --timeout 240

pixi run just react-native formal-host-ios-managed-io \
  --device 00008110-000C61160C39401E \
  --app-tree-sha256 5cfabfac16fa188bea05eedf1be70814cfcc4817d94734fad4f97aeaa6e415fa \
  --xctestrun tests/consumers/react-native-bleng/ios/build/device-derived/Build/Products/BleHubBlengHost_iphoneos27.2-arm64.xctestrun \
  --fixture-contract tests/peripherals/nrf52840/firmware/contracts/multifunction-rf-v2-a.json \
  --fixture-profile tests/peripherals/nrf52840/firmware/profiles/multifunction-rf-v2-a.json \
  --fixture-port /dev/cu.usbmodem0010502520281 \
  --fixture-resource-identity jlink:1050252028 \
  --output .work/ios-native-io \
  --authorize-install --authorize-fixture-start --release-before-install --timeout 900
```
