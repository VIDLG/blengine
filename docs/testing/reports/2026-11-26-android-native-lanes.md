# Android native lanes：四条 lane、七次运行全部通过

日期：2026-11-26
Lane：`run_bleng_android_native_notification.py`、`run_bleng_android_adapter_transition.py`、
`run_bleng_android_permission_transition.py`、`run_bleng_android_multi_link.py`
设备：OnePlus PHB110（`3ac5cc09`，rooted，API 36）；nRF52840 DK-A（`jlink:1050252028`，v2-a）与
DK-B（`jlink:1050251479`，v2-b）
产物：App APK `a1fcb8efdf174c674854df182ee8fe136d021efd20a092aa38b39e8933ea1518`；测试 APK
`0578d9b2690398cd6dd17097b6929e9c27997903a7f9aeff801bfc65fbafc7ad`

本文只记录候选事实与本次修掉的缺陷，不宣布门禁通过、不冻结 ABI。四条 lane 共用同一份 App 与测试
APK；这也是本轮补跑的原因——`notification overflow` 的测试 APK 在上一轮发生了变化，另外两条正向
场景必须换到同一份产物上才成立。

## 运行结果

| Lane | 场景 | 结果 | 证据根 |
|---|---|---|---|
| native notification | single | `passed` | `.work/android-native-notification-single/2026-11-26-b/563780b2-bde2-4edb-86f8-52658cdbe76d` |
| native notification | sequence（8 值，无丢失/重复） | `passed` | `.work/android-native-notification-sequence/2026-11-26-b/e47171ce-a166-4c34-9443-4f48bcc6fd71` |
| native notification | overflow（96 值，慢消费者） | `passed` | `.work/android-native-notification-overflow/2026-11-26-b/89346afb-7027-450f-a01e-b5c60c2f64b3` |
| adapter transition | radio OFF → ON | `passed`，`radio=restored` | `.work/android-native-adapter-transition/2026-11-26-a/cb7230a4-7559-49c5-aabb-099fec82fa2f` |
| permission transition | denied → active → restored | `passed`，`hold=held`，`death_reason=8` | `.work/android-native-permission-transition/2026-11-26-a/a5ab345e-ae39-4fa1-9da3-69a3c6270c38` |
| multi-link | 2 peers，1 round | `passed`，24 stages | `.work/android-native-multi-link/2026-11-26-a/f6841c64-fb56-431b-930a-de02e1db10d7` |
| multi-link | 2 peers，2 rounds | `passed`，每轮各自 24 stages | `.work/android-native-multi-link/2026-11-26-b/510f7b58-6797-4dde-9ffd-24508646f3eb` |

每次运行：`status=passed`、`scenario_status=passed`、`fixture_status=passed`、
`cleanup_errors=[]`、`phone_cleanup_errors=[]`；`host_gate_status=pending`（这些 lane 不跑 host 侧
门禁，它只属于 managed 场景序列）。

fixture 事实：两条 DK 的 boot nonce 在本轮保持不变（DK-A `91a0f16bffc1d12fe30e5fee2e3ab9a4`、
DK-B `fc3da81101ef3b07471a216f57d62241`），每次运行结束后 `advertising_inactive=true`、
`resources_closed=true`，证明方式是 `btp-gap-controller-info-advertising-bit-clear`。没有烧写、
没有复位、没有换镜像。

### adapter transition

`adapter-off → adapter-stream-on → adapter-restored → scan-submitted → scan-started →
advertisement → scan-stopped → connected → gatt → io → done`。host 拥有 radio 的开关，App 只负责
产品事实：radio 关闭时的启动被拒绝，radio 回来后它自己重试成功。`radio=restored` 是 host 在收尾时
读到的实际状态，不是计划值。

这条 lane 是本次能安全重跑的关键：HANS 解析（
[`2026-11-26-hans-rescope.md`](2026-11-26-hans-rescope.md)）证明 App 只在后台期间持有
`connectedDevice` 前台服务，`freezer_cutoff_adj=900` 之下不会进入 cached-app freezer，因此
radio/权限切换不需要任何白名单或 ROM 改动。

### permission transition

三个阶段各自是独立进程，host 拥有全部 OS 动作：

- `active`：`pid=18921`、`start_ticks=398038144`，建立链接并 armed；随后被权限撤销杀死，
  因此 `clean_terminal=false` 是这条 lane 的**期望值**，不是失败；
- `denied`：`pid=18826`，两个权限都为 `false`，App 只记录被拒绝的启动；
- `restored`：`pid=19082`，权限恢复，重新扫描、连接、读写并正常收尾。

`permission_death.reason=8`（`PERMISSION CHANGE`），`uid=10378`，与 `package_uid` 一致；
`active_link_ended=true` 说明被杀死的那条链接确实终止了，而不是留了个假活着的链路。

### multi-link

两个 peer 由命令行声明（`port` + `contract` + `profile` + `identity`），runner 里没有设备名、端口
或镜像常量——声明本身就是拓扑。DK-A 是 v2-a（`BleHubMultiRfV2DKA`），DK-B 是 v2-b
（`BleHubMultiRfV2DKB`），`service_uuid` 分别为 `fd600000…` / `fd700000…`，所以两条链路的
发现与读写是各自独立的。

场景结束第 0 条链接（`drop-requested:0 → dropped:0`），同时要求第 1 条存活
（`survivor-requested:1 → survivor:1`），最后 `disconnected:1`。两轮运行里每轮都只报告自己的 24
个 stage；这正是上一轮报告担心的跨轮 marker 污染，这里是反证。

## 修掉的缺陷：`overflow` 的终止语义

失败的那次运行（`.work/android-native-notification-overflow/2026-11-26-a/…`，旧测试 APK
`0066f06b…`）停在 `values-requested`，`primary_error` 是 stage 契约不匹配：期望
`overflow-observed` 等 stage，实际只到 `values-requested`。

根因在测试，不在产品：supervisor 把「消费者跟不上」表达为 `ended(reason = overflowed)` 且
`failure == null`——这与 scan lane 的形状一致，语义是「这条观察 lane 终止，原生订阅和其它操作继续
运行，不是运行时级故障」。测试仍在要求 `failure.code = delivery.value_overflow`，于是一个本应
合法的终止被判成错误，`nextEvent` 拿到 `Second` 后直接报错，`overflow-observed` 永远不会发出。

修法是把断言改成契约本身：`Second.ended.failure` 必须为 `null`，`ended.reason` 必须是
`BlengEndReason.OVERFLOWED`；`First`（还收到值）和 `Third`（整体操作失败）都按名字拒绝。测试注释
写明了这是 lane 的 terminal 而非 fault，以及旧断言为什么不再成立
（`tests/consumers/react-native-bleng/android/app/src/androidTest/kotlin/com/blehubblenghost/BlengNotificationInstrumentedTest.kt`）。

包文档同步：`integrations/react-native-bleng/README.md` 与 `README.zh-CN.md` 明确写「流的结尾是
`reason: 'overflowed'`」。

## 离线验证

| 命令 | 结果 |
|---|---|
| `pixi run python -m pytest tools/tests -q` | `1074 passed, 21 skipped, 2984 subtests passed`（45.3 s） |
| `pixi run just --fmt --check` | 通过 |
| `pixi run just docs-check-mermaid` | `Checked 28 Mermaid diagrams in 300 Markdown files` |

## 还没做的

- **iOS 没有对应 lane**：`adapter-transition`、`permission-transition`、`multi-link` 只覆盖
  Android 原生契约。iOS 侧的等价事实目前只在 managed 场景（`adapter-control`）里有证据。
- **`host_gate_status=pending`**：这四条 lane 不参与 host 侧门禁，不要把这里的 `passed` 当成
  managed 序列的 `host_gate`。
- 压力/长稳仍后置；`multi-link --rounds` 是场景级重复，不是长稳证据。
