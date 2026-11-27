# Managed `io` 场景：read / write / read-back / RSSI 真机通过（候选事实）

日期：2026-11-18。范围：RN formal consumer 的第四个 managed UI 场景 `io`，在真机 iPhone 与真机
Android 上各通过一次。本文只记候选事实与已修缺陷，不改 gate、不冻结 ABI、不宣称 release 或 Phase exit。

## 结论

| 平台 | 入口 | run id | 结果 |
|---|---|---|---|
| iPhone (`00008110-000C61160C39401E`) | `formal-host-ios-managed-io` | `36b0f0e0-62bc-4d88-bb70-713b715e165c` | `scenario/session=passed`，`cleanup_errors=[]`，fixture `advertising_inactive`+`resources_closed` |
| Android (`3ac5cc09`, PHB110) | `formal-host-android-managed-io` | `5a5abb87-e8db-4085-84a6-d31731f16716` | 同上；同一 `observations` 12 项 trace |

`host_gate=pending` 是设计状态：host 级 requalification 不是本场景能自证的结论，ABI 仍未冻结。

产物指纹：iOS App 树 `313401e0663412de555ba2ae609582a06a3753bf6c2f1a4bd93d08f69d11bdd5`；
Android APK `c394c9a827e782b6ba462b940d0657f11cae9025699f137d470f1227d45f2f97`
（两者都包含本次的 pinned outcome 布局改动）。

## 场景语义

`io` 是同一个固定命令面上的 12 项 trace：

```
idle → started → scan:running → scan:advertisement-matched → scan:stopped
     → link:connected → gatt:ready → io:read-initial → io:written → io:readback
     → rssi:observed → closed
```

断言原则：

- 特征值只按 **UUID 声明**，由 App 解析；host 不读拓扑位置。
- **不断言 peer 的初始值**。profile 的 `initial_value_hex` 描述它写就时的镜像，安装镜像当前持有什么
  只能被观测，不能被声明。
- **写回执不是存储值**：写完必须再读一次，且该读数与写入前的读数不同。
- RSSI 必须是连接链路上的真实读数（负值），不是屏幕的 idle 占位。
- 全程零手势（`_require_no_gesture`）：场景自己的命令与它自己的回执都在 pinned 面内。

## 三次真机失败与根因（按发现顺序）

1. **把 profile 的初始值当断言**（上一轮 `ufd9mzk2`、`aveb87c7`）：场景要求首次读到的值等于
   profile 的 `initial_value_hex`。实测镜像给出的不是那个值，于是轮询到超时。修法：场景只要求
   "拿到合法 payload"，写后再读必须变化。
2. **值正则不接受长 payload**（本轮离线回归发现）：`_await_gatt_value` 只匹配 `N bytes [xx …]`，
   而 App 对 >16 字节的值渲染成 `62 bytes [.. .. …]`（省略号在方括号内）。真机上任何长值都会被
   判成"没有值"。修法：单条 `_GATT_VALUE_TEXT` 正则，允许截断省略号。这一条**没有被真机发现**，
   是新写的离线场景测试在提交前抓到的。
3. **机器读的结果落在可滚动内容区**（本轮 `ios-managed-io-r2`）：`primary_error` 只写
   `observed None`，Appium 日志给出了真正原因——`gatt-result` 被找到、`text` 有内容，但
   `is_displayed` 一直是 `false`（84 次 `/displayed` 响应里 21 次是 `{"value":false}`，全部是
   `gatt-result`）。值存在、有文本、但屏幕上看不到，所以场景永远读不到。这与仓库自己的设计相悖：
   `run-status` 之所以存在，就是因为"机器读的值必须 pinned"。修法：把 `gatt-result` /
   `gatt-error` / `rssi-value` / `rssi-error` 从 `GattPanel`（可滚动内容）移进 `CommandConsole`
   （pinned），App 侧加了一条测试断言它们不在 `workbench-content` 内、且与产生它的命令在一起。
4. **写入长度由 fixture 决定**（本轮 `ios-managed-io-r3`）：`write failed: … CBATTErrorDomain:
   The value's length is invalid.`。根因在 firmware 侧：BTP tester 的 `write_value` 在
   `offset + len > value->len` 时返回 `BT_ATT_ERR_INVALID_ATTRIBUTE_LEN`，即**写不能长于该
   attribute 当前值的长度**。这个长度是 fixture provision 出来的（`profile.initial_value_hex`
   的长度）。修法：删掉 runner 里的硬编码 `IO_WRITE_PAYLOAD = b"01"`，改由 `bleng_scan_host`
   从 profile 派生：长度等于 provision 的长度，字节取一个**与当前值不同**的可打印 ASCII
   （`_io_write_payload`）。场景不再自己发明长度。

## 新增的离线覆盖

真机前 `io` 场景没有任何测试，两次真机运行只是学到一个 profile 事实。现在：

- `tools/tests/react_native/test_bleng_io_ui.py`（11 项）：完整 trace、写未落盘的失败、写类型回执、
  不可读/不可写目标先于任何 I/O 被拒、读到别的特征值不算、缺 RSSI / 非负 RSSI、**值被渲染但不在屏内
  时点名布局缺陷**、payload 必须是可打印 ASCII、以及"peer 值与 profile 不同不算失败"的回归用例。
- `tools/tests/react_native/test_bleng_scan_host.py::OwnedIoTests`（4 项）：profile 派生目标与合法
  payload、seam 只调用一次且参数来自 profile、未知 role 不触达 UI、payload 保持 provision 长度
  且永不等于当前值。
- `CommandConsole` / `GattPanel`：`Gatt.test.tsx` 新增一条 pinned 断言（结果与命令同处
  `command-console`，且不在 `workbench-content`）。
- `tools/react_native/run_bleng_ios_scenes.py` 的 `SEQUENCE` 扩到
  `scan → notification → indication → io`，入口测试同步更新（Android 侧仍是逐场景入口）。

同时顺手收敛一处诊断：`_wait_rssi` 补上 `_PRECONDITION_SETTLE_SECONDS` 上界，并新增
`_WorkbenchUi.off_screen()`，让"值渲染了但不在屏内"在失败消息里被点名，而不是伪装成 peer 没有值。

## 复现命令（iPhone）

```sh
pixi run just react-native::appium-ios-tunnel-script   # 人工：sudo 启动隧道 registry
pixi run just react-native::formal-host-ios-release-app --device 00008110-000C61160C39401E \
  --output .work/ios-release-pre-io --timeout 120
pixi run just react-native::formal-host-ios-managed-io \
  --device 00008110-000C61160C39401E \
  --app-tree-sha256 313401e0663412de555ba2ae609582a06a3753bf6c2f1a4bd93d08f69d11bdd5 \
  --appium-port 4723 \
  --fixture-contract tests/peripherals/nrf52840/firmware/contracts/multifunction-rf-v2-a.json \
  --fixture-profile tests/peripherals/nrf52840/firmware/profiles/multifunction-rf-v2-a.json \
  --fixture-port /dev/cu.usbmodem0010502520281 \
  --fixture-resource-identity jlink:1050252028 \
  --output .work/ios-managed-io --timeout 600 --cleanup-seconds 120 \
  --authorize-install --authorize-wda-build --authorize-fixture-start --release-retained-app
```

四个场景连跑：`formal-host-ios-managed-sequence`（同一组参数，去掉 `--scene`）。

## 回归验证（同一 App/APK，四场景往返）

布局改动会移动四个场景共用的 pinned 面，因此此后每次改 App 都重跑两端全部场景。三轮实测：

| 轮次 | App 树 / APK | iPhone（scan / notification / indication / io） | Android（scan / notification / indication / io） |
|---|---|---|---|
| 首次 io 通过 | `313401e0…` / `c394c9a8…` | `e570a1ef` / `b9e5e632` / `3449607e` / `3f4219e7` 全部 passed | `d6fb64cf` / `ca758966` / `2dc389a9` / `5a5abb87` 全部 passed |
| 读取取消面（契约版） | `b7fd3487…` / `e2dcd6ff…` | `c6717441` / `251c27d5` / `27817127` / `5bd2fa18` 全部 passed | `7d3d59f4` / `27088c63` / `948ecbd2` / `45ca458c` 全部 passed |
| 最终产物（`reads` 队列记账后重建） | `91a702ed…` / `ccd0247a…` | `io` 金丝雀 `468c28a3` passed | `io` 金丝雀 `53563b67` passed |

iPhone 前两轮都是整条序列（`scene_sequence=passed scenes=scan,notification,indication,io`），每个场景
`cleanup_errors=[]`、fixture 均到达 `advertising_inactive`+`resources_closed`。第三轮只跑 `io`：相对
`b7fd3487` 的源码差异只在 App 内部的“未完成读数”记账（`reads` 队列，同一契约、同一 pinned 面，
由 107 项 App 测试覆盖），因此用单场景金丝雀证明重建产物仍会说同一套契约，而不是重跑 33 分钟矩阵。
下一轮的 `cancellation` 场景会重新跑满两端矩阵。

## 读取取消面（为 `rf-cancellation` 做的第一步）

App 之前根本没有读取取消面：`read()` 内联 `await wait()` 且不保留句柄，所以一个未完成的读永远
无法从 UI 取消。本轮补上，但仍刻意小：

- `BlengRead.cancel()` 是独立的取消通道，不是回滚；`gatt-read-wait` 的 receipt 依旧如实结算（它是
  `failed`，因为这次等待确实没成功），只有“本次运行请求了取消且原生失败码是 `cancelled`”才发布
  `gattRead=cancelled`；未经请求的 `cancelled` 一律当故障。
- 新 pinned 控件 `cancel-gatt-read`（在 `command-console` 内，与 Read/Write/Subscribe 同排），
  canceled 终态以 pinned `gatt-cancel` 展示；`run-status` 新增标量键 `gattRead`
  （`none|waiting|cancelled|done|failed`），因为“被取消的终态”必须能与“还没结果”区分。
- 顺手修掉一个真实缺陷：`gatt-subscribe` 的 `onPress` 写成了 **直接调用** async handler
  （`onPress={scan.enableNotifications(...)}`），在渲染期就发命令并 setState，订阅成功后的下一次
  render 会进入无限更新。现已改为回调；TypeScript 早就在这一行报 `Promise<void>` 不能赋给
  `() => void`，只是测试当时靠早退守卫没踩到。

App 侧测试：新增 4 项（取消→cancelled 终态→同特征再读成功；**取消队列中的那一个而活动读照常完成**；
未请求的 cancelled 算失败；无未完成读时按钮 disabled），全套 107 项通过。真正的 `cancellation` 场景仍需：
fixture ARM 一个 read hold、host 侧 coordination、新的 scene kind 与 trace——这是下一步。

## 边界

- 一次真机通过是**候选事实**：不迁移任何 gate，不把 `io` 计入 release 或 stress 结论。
- 写入长度依赖 firmware 的 tester 语义（`write_value` 的 `INVALID_ATTRIBUTE_LEN`）。若将来镜像改成
  定长写入或更长 provision 值，`_io_write_payload` 会随之给出新长度，但**读回内容仍不被断言**。
- `host_gate=pending`、`stress` 与 ABI 冻结都仍未开始。
