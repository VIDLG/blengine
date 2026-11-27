# Android 20 条 managed 场景：一次跑通

日期：2026-11-26
设备：OnePlus PHB110 `3ac5cc09`（rooted，API 36）；DK-A `jlink:1050252028`（v2-a）
范围：`pixi run just react-native::formal-android-managed-sequence`，`scene_sequence.ORDER` 全部 20 条。
状态：**`scene_sequence=passed`**，20 份 bundle 各自离线复验 `integrity=passed`、`cleanup_errors=[]`；
`host_gate=host pending`（仍是候选事实，不是 release/Phase 退出）。

## 结果

```text
scene_sequence=passed scenes=scan,notification,indication,background,io,link-facts,disconnect,
descriptor-io,devices-query,devices-kinds,adapter-control,connect-failure,auto-connect,
cancellation,write-reject,ccc-enable-reject,ccc-disable-reject,passive-disconnect,recovery,recovery-ten
```

| 场景 | 场景片 | 场景 | 场景片 |
|---|---|---|---|
| `scan` | 20.2 s | `adapter-control` | 19.0 s |
| `notification` | 25.7 s | `connect-failure` | 62.3 s |
| `indication` | 26.7 s | `auto-connect` | 27.7 s |
| `background` | 63.9 s | `cancellation` | 31.7 s |
| `io` | 28.0 s | `write-reject` | 32.0 s |
| `link-facts` | 26.6 s | `ccc-enable-reject` | 30.2 s |
| `disconnect` | 34.1 s | `ccc-disable-reject` | 43.9 s |
| `descriptor-io` | 29.4 s | `passive-disconnect` | 31.5 s |
| `devices-query` | 21.7 s | `recovery` | 40.5 s |
| `devices-kinds` | 19.3 s | `recovery-ten` | 103.1 s |

合计 717.5 s（12.0 min）。App APK `a1fcb8ef…`，test APK `0066f06b…`，
fixture contract/profile v2-a，run root `.work/android-native-sequence/2026-11-26-a`。

## 为什么这条值得单独记

之前的基线明确写着"**不要声称 19/19**"：2026-11-25 的序列跑在 `background` 上停了两次、症状还不同，
而 `background` 单独跑是通过的。今天同一份顺序 20 条一次过，**包括 `background`**。

这里要诚实：**没有证据表明那次失败被定位并修掉了**。本轮之前的确有几处真修复（键盘/页面声明/
完成信号/`not_supported` 词表），任何一处都可能顺带改变了时序，但没有一次实验把"`background`
在序列里为什么停"隔离出来。所以本报告只记录"这一次完整通过"，不声称该缺陷已根因定位；下一次再遇到，
应当按新失败重新取证，而不是引用本条说"它已经好了"。

## 离线复验（20/20 独立重放）

每份 bundle 都跑过 `python -m tools.react_native.bleng_scan_evidence <attempt>`：

| 结果 | 数量 |
|---|---|
| `status=passed` 且 `integrity_status=passed` | 20 |
| `phone_status=passed`、`fixture_status=passed` | 20 |
| `cleanup_errors=[]` | 20 |

## 覆盖面的边界（这条通过不代表什么）

- **只覆盖 Android**。iOS 侧的 20 条没有一次同跑；其中 8 条有 2026-11-26 的单独证据，其余 12 条
  最近一次是 2026-11-24（App 树不同，不继承）。
- `host_gate` 仍是 `pending`：这些是 UI 观测，不是 RF verdict、不是 resource-zero、不是 ABI freeze。
- 三条能力明确不列 S 档（`bond` 需固件 SMP、`companion` 关联需人工确认、`adopt-peripheral` 需
  Central 角色 peer）；5 条能力按设计只在 L 档（adapter-transition / permission-transition /
  multi-link / notification overflow / engine-reload），本轮没有重跑 L 档。
- 场景通过不证明真机上的 radio 状态、系统蓝牙开关或后台策略；这些各有自己的 lane。
