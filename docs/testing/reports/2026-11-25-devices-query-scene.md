# `devices-query` 场景：系统清单必须包含我正握着的这条链路

日期：2026-11-25

范围：第 17 个 managed 场景 `devices-query`——用 `beginDeviceQuery('connected', …)` 读平台
自己的清单，并证明**这次运行的 peer 就在清单里**。这是它的第一条真机证据；顺带修掉了上一场
（`descriptor-io`）暴露的一个**账本窗口**根因。状态：Android 单场景真机通过、`integrity=passed`；
**没有 iOS 设备证据**。

## 为什么是这一条

`beginQueryDevices` 是只读清单，返回 `BlengDeviceInfo[]`（`key/localName/connected/bonded`）。
关键是 bridge 的契约：**`key` 是 owner-scoped 的，同一 peer 与 `peripheral.key()` 可比**。所以场景
能问的比"命令被接受"更强的问题：

| 事实 | 含义 |
|---|---|
| `devices=done` | 查询落定 |
| `devicesCount>=1` | 清单非空（空清单不能冒充"没连设备"） |
| `devicesPeer=yes` | **本场正连着的 peer 出现在 `connected` 清单里** |

"回来一个列表"和"这个列表描述的是我这条链路"是两个claim，所以投影分成三个字段，而不是一个布尔。
Android 上 `inventoryDevices()` 复用同一份 peer 缓存，所以 key 天然相等；这也是首次真机就能过的原因。

平台差异：Android 三种 kind（`connected/bonded/companion`）都支持；Apple 只支持 `connected`
（且要求至少一个 service UUID），`bonded`/`companion` 如实 `not_supported`。本场景只用
`connected`，形状两端通用——但 **iOS 尚未跑**。

## App 面

- `useManualScan.queryDevices()`：走 `pendingOperation`（与其它 lane 同一个 await 层）；提交与
  wait 分开记账（`devices-query` / `devices-query-wait`）；终态 `done|unsupported|failed`。
- 投影新增 `devices` / `devicesCount` / `devicesPeer`（`yes|no|none`）。
- 控件 `devices-query` 放在 **scan 主行**（`Start scan / Pull next / Stop scan / Devices`）：这一行有
  横向余量，`flexGrow` 会把 4 个按钮压在同一行，**不增加 console 高度**。console 是固定预算，
  新加一行会把下面的控件挤出视口（Android 不渲染视口外元素），上一次的 `link-facts` 已经因此回归过。

## 顺带修掉的根因：`tapUntil` 的重复点击会顶穿账本窗口

验证本场景时，`descriptor-io` 在第二次重跑里失败了：

```text
omitted=1
2|begin-scan|succeeded|
...                              # 第 1 条 `start` 被 App 的有界窗口丢掉
```

原因是 `tapUntil`（带 `advance`）的循环是"检查 → 点击 → 再检查"，而读是异步的：点击后立刻重查时
计数还没前进，于是**又点了一次**（App 侧被 `descriptorPending` 忽略）。账本仍会记录这两次点击，
四次读就多出几条，最终 33 条 > `EVIDENCE_LIMIT=32`，最早的 `start` 被挤掉。

修法（两端 driver 同构）：带 `advance` 时，**每次点击后先等该计数严格前进再决定是否重试**。
`advance` 的 baseline 在步骤开始时固定（"比开始时新一次的完成"），不是"比上次轮询新"。于是：

- 一次读 = 一次点击（真机 `taps` 从 `2,2,2,2` 变成 `1,1,1,1`）；
- 只有**值真的过期**才会再点（这才是该有的重试）；
- 账本回到 25 条，`omitted=0`。

同时 `_require_ledger` 增加硬约束：**`omitted>0` 直接失败**，并说明"窗口丢了 N 条，本场记录不完整"。
单场景本就需要整段会话，不能拿一个后缀冒充全部记录；`recovery` 那种本来就跨窗口的场景用自己的
后缀 claim，不走这条路径。

## trace 与 ledger

```text
idle, started, scan:running, scan:advertisement-matched, scan:stopped, link:connected,
devices:listed, devices:peer-connected, closed
```

```text
start, begin-scan, scan-start-wait, scan-pull, scan-stop,
connect, connect-wait, devices-query, devices-query-wait, close
```

## 真机结果（Android，OnePlus PHB110 `3ac5cc09`）

| 项 | 值 |
|---|---|
| 运行 | `.work/android-native-devices-query/2026-11-25-b`，`status=passed`，trace 9/9，场景片 14 s |
| 投影 | `devices=done;devicesCount=1;devicesPeer=yes`，`failure=none` |
| 离线 | `integrity=passed`、`fixture_close=advertising_inactive` |
| App APK | `93f5e42c…`；test APK `82e09a78…` |
| ledger | 11 行全 succeeded，`omitted=0` |

同一次 APK 复核（driver 修复后）：`descriptor-io` 通过（25 条账本、四次读各 1 次点击）、
`notification` 通过（console 高度未变）。

## 守卫（不需要设备）

- `tools/tests/react_native/test_scene_semantics.py`（+5）：清单含本 peer 才通过；`devicesPeer=no`
  不能通过；空清单不能通过；查询未落定不能通过；账本缺 `devices-query-wait` 不能通过；
  另加一条"被窗口截断的账本不能当整段会话读"。
- `Devices.test.tsx`（+4）：清单与 peer 匹配、遗漏 peer 记为 `no`、平台拒绝记为 `unsupported`、
  无链路时控件禁用。
- `runStatus.test.ts` / `run_status.py`：键集与严格解码钉住 `devices/devicesCount/devicesPeer`。
- `test_scene_sequence.py`：顺序钉住 `descriptor-io < devices-query < connect-failure`。

## 尚未完成

- **iOS 无设备证据**：四条最新场景（`disconnect`/`connect-failure`/`link-facts`/`descriptor-io`）
  加上本条，都只在 Android 真机跑过。iOS 侧 `advance`/`awaitStatusAbove` 已实现但未跑。
- **全序列仍不能声称通过**：`background` 在序列里两次以不同症状中断（见
  [`2026-11-25-link-facts-scene.md`](2026-11-25-link-facts-scene.md)），单跑通过；在拿到可复现序列前
  不记成场景缺陷。
- 矩阵 C 表剩余：`adapter-control`（含 `radio-loss-live`，会动手机无线电，需明确 scope）、`bond`、
  `auto-connect`、`companion`、`adopt-peripheral`。
