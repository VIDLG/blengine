# `devices-kinds` 场景：主线不需要的两种系统清单

日期：2026-11-26
设备：OnePlus PHB110 `3ac5cc09`（rooted）；DK-A `jlink:1050252028`（v2-a）
范围：第 19 个 managed 场景 `devices-kinds`（`beginQueryDevices('bonded' | 'companion')`）。
状态：Android 真机通过、`integrity=passed`；**无 iOS 设备证据**。

## 为什么是这一条

`beginQueryDevices()` 接受三种 kind，但主线只用到 `connected`：它的主张是"这份清单里有没有我正握着的
那条链路"。另外两种不是链路事实，而是**适配器事实**——本机配对过谁、哪些 companion association 存在：

```text
bonded    : Android 读 BluetoothDevice 的 bonded 集合；CoreBluetooth 无此清单
companion : Android 读 CompanionDeviceManager 的 association；CoreBluetooth 无此清单
```

所以这条场景**不需要对端、不需要扫描、不需要链路**，只证明每个答案都是平台自己的：要么是一份清单
（`done` + 数字），要么是平台自己的 `bleng.dispatch_rejected.not_supported`。超时、裸 `failed`、
或者答成另一种 kind，都不算答案。这也正是 Apple 上这条场景唯一合法的形状。

## App 面：探针记录归属"owner"，不归属 ScanRun

首版实现把探针塞进 `ScanRun`（`runRef.current`），于是有一个结构性 bug：**没有扫描就没有 run**，
`queryDevices()` 读到 `run === undefined` 直接返回。真机表现是"按钮 enabled、点了没反应"，
jest 里是 `beginDeviceQuery` 0 次调用。

修法不是给场景补一次扫描（那会让"这是适配器事实"的语义变成假的），而是承认两件事分属不同的归属：

| 请求 | 归属 | 失败栅栏 |
|---|---|---|
| `connected` | 活着的 `ScanRun`（要比对它自己那条链路） | `publish(run, …)` + observation 身份 |
| `bonded` / `companion` | **owner 本身**（`BlengCentral`） | `inventoryRef` 记录 + `centralRef.current` 身份 |

`useManualScan` 因此多了一份 owner 级记录：

```ts
const inventoryRef = useRef<{ central: BlengCentral; observation: object; pending: boolean }>()
```

它的三条不变量：

1. **一次一条**：`pending` 覆盖整个 wait，不只是提交（`pendingOperation` 提交成功不等于平台答完）。
2. **只由被问的 owner 发布**：`centralRef.current !== central` 或记录已被换掉 → 丢弃。
3. **关闭即退休**：`ownerClosing()` 清掉记录，尚未回来的答案不会再写入界面。

`canQueryInventory` 同时看这份记录和 run 的 `deviceQueryPending`，所以 bonded/companion 与
`connected` 互斥，不会在同一 owner 上叠两个清单请求。

页面归属不变：两个按钮在 `Probe` 页的 `probe-inventory-strip`（`CommandConsole` 仍是固定高度预算），
plan 用 `workbench-probe` + `workbench-page=Probe` 显式声明。

## plan 与账本

```text
初始 → start-session → tap workbench-probe → wait page=Probe
     → tap devices-bonded   → waitAny(devices=done|unsupported, devicesKind=bonded) → read
     → tap devices-companion→ waitAny(devices=done|unsupported, devicesKind=companion) → read
     → close-session
```

账本形状**由答案派生**，不再钉死一份常量（首跑就是死在这一点：plan 只声明了一次 query，App 记了两次；
两平台的形状又不同）：

| 平台答案 | 折叠后的账本名 |
|---|---|
| 两次都答完 | `start, devices-query, devices-query-wait, devices-query, devices-query-wait, close` |
| 两次都被拒 | `start, devices-query, close`（连续的 `devices-query` 折叠成一个） |
| 一答一拒 | 依次拼接，例如 `start, devices-query, devices-query-wait, devices-query, close` |

`_collapse` 只折叠**连续**重复，所以"两条 query 各带自己的 wait"是四个名字；而"提交即被拒"没有 wait 行，
两条拒绝对彼此连续、折成一个。`_require_devices_kinds_ledger` 先断言恰好两次 `devices-query`，
再按每条自己的 outcome 拼出期望序列，任何非 `not_supported` 的失败直接拒绝——这同时让"混合作答"
（老 Android 有 bonded、无 companion association）成为合法形状，而不是伪装成"两次都一样"。

## 真机结果（Android）

| 项 | 值 |
|---|---|
| 运行 | `.work/android-native-devices-kinds/2026-11-26-b`，`status=passed`，trace 5/5，场景片 11 s |
| bonded | `devices=done;devicesCount=0;devicesPeer=none;devicesKind=bonded;failure=none` |
| companion | `devices=done;devicesCount=0;devicesPeer=none;devicesKind=companion;failure=none` |
| 链路 | 全程 `link=none`、`scan=idle`（场景确实不依赖对端） |
| 离线 | `integrity=passed`、`cleanup_errors=[]`、fixture advertising bit = clear |
| App APK | `dce3f4f3…`；test APK `0066f06b…` |
| ledger | 6 行全 succeeded，`omitted=0` |

`devicesCount=0` 是这台手机的真实答案（该 App 没有配对设备、没有 companion association），
不是"没读到"：`done` 与数字都来自平台，`devicesPeer=none` 明确表示适配器清单没有可比的 peer。

首跑（`.work/android-native-devices-kinds/2026-11-26-a`）失败过一次，失败在**账本形状**而不是场景：
App 记录的两组 `devices-query`/`devices-query-wait` 与当时只声明一组的常量不符。第二次运行通过。

## 守卫（不需要设备）

- `test_scene_semantics.py`（+2）：一答一拒的混合账本可通过；`unsupported` 若不是平台码不能通过
  （既有 4 条仍覆盖：kind 必须自洽、`pending` 超时不能通过、两平台各自的形状、无 host 动作）。
- `Devices.test.tsx`（+1）：**没有链路**也能从 Probe 页问 bonded，`devicesKind=bonded`、
  `devicesCount=0`、`devicesPeer=none`；既有 4 条覆盖 connected 主线与 `unsupported` 分支。
- `test_scene_sequence.py`：顺序钉住 `devices-query < devices-kinds < cancellation`。
- `test_workbench_ids.py`：`devices-bonded` / `devices-companion` / `probe-inventory-strip` 进入词表。

## 尚未完成

- **iOS 设备证据已有**（2026-11-26）：这两种清单在 CoreBluetooth 下都以平台自己的
  `bleng.dispatch_rejected.not_supported` 结束（`devicesKind` 分别回 `bonded`/`companion`），
  见 [`2026-11-26-ios-seven-scenes.md`](2026-11-26-ios-seven-scenes.md)。
- 序列现为 19 条，仍**不声称连续通过**（`background` 在序列里失败、单独跑通过）。
- `Probe` 页后续成员：`adapter-control`（HANS OFF → UiA2/ADB 卡顿风险未解，需单独批准）、
  `bond`（固件无 SMP）、`companion` 配对（需人工确认）、`adopt-peripheral`（需 Central 角色 peer）。
