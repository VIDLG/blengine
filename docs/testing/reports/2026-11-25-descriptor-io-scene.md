# `descriptor-io` 场景：把对端的 CCCD 当成订阅的第二个证人

日期：2026-11-25

范围：第 16 个 managed 场景 `descriptor-io`——同一份订阅的生命周期里，对端自己存的 CCCD
被读四次（关、开、清、写后不变）。这是它的第一条真机证据，也修掉了一个会让"读"根本不发生的
**假证人**。状态：Android 单场景真机通过、`integrity=passed`；**没有 iOS 设备证据**；全序列另见末节。

## 为什么是这一条

订阅状态过去只有一个证人：App 自己的 `notify` 相位。它会说 `subscribed`，而这条链路是否真的
在对端使能了 CCCD，App 无从证明。CCCD（`00002902-…`）就是第二个证人：

| 时点 | App 说 | 对端 CCCD 应说 |
|---|---|---|
| 订阅前 | `notify=none` | `0000` |
| 订阅后 | `notify=subscribed;notifyMode=<mode>` | `<mode 对应的位>`（notification=`0100`，indication=`0200`） |
| 关闭后 | `notify=ended` | `0000`（由 **disable** 清，不是我们的写） |
| 同值写后 | `notify=ended` | 仍是 `0000` |

所以顺序是有语义的，不是随便排的：

```text
disable → 读(0000) → 同值写(0000) → 读(0000)
```

**先 disable、读、再写**：只有"清空发生在我们的直接写之前"，`0000` 才能归因于 disable。
真机实测：禁用后直接写 `0100` → CCCD 读回 `0100` **且** `notifyPhase` 变回 `subscribed`
——对 CCCD 的直接写会重新使能对端。因此直接写只允许写"对端刚报告的字节"。

## 抓到的真缺陷：条件已满足，读就根本不发生

第一版把每次读都写成 `tapUntil`（点击直到状态满足）：

```text
[id=descriptor-read] until {descriptorValue: "0000"}
```

前三次读没问题（当前值与期望不同，一定会点）。**第四次（写后"不变"）坏在这里**：期望值是
`0000`，而第三次"清空"读已经把机器面停在 `descriptorValue=0000`。`tapUntil` 是
"already satisfied → 不点"，于是这一拍被**完全跳过**：

```text
...|descriptor-write|succeeded|
...|descriptor-write-wait|succeeded|
...|close|succeeded|
```

`cccd-unchanged` 读到的其实是上一拍遗留的 `0000`，而不是写之后的一次新读。这是一个**假证人**：
报告会说"写后不变"，但根本没有再碰对端。（同时它也解释了旧账本对不上的次要原因。）

### 修法：单调计数 + 计划的 `advance`

机器面新增 `descriptorReadTotal`（已结算的描述符读次数，成功/失败都算），和已有的
`gattReadTotal` 同一套语义。`tapUntil` 新增可选键 `advance`：

```json
{"op": "tapUntil", "id": "descriptor-read",
 "until": {"id": "run-status", "status": {"descriptorValue": "0000"}, "exists": true},
 "advance": "descriptorReadTotal", "maxTaps": 5}
```

语义：步骤开始时记下该计数的当前值，**只有计数严格变大且 `until` 成立**才算满足。于是
"值相等"不再能冒充"刚读过"；即使第一次读回缓存旧值，计数没有前进，仍会继续点。

这不是把 App 内部重试藏起来：重试次数仍是计划声明的 `maxTaps`，计数本身是 App 如实发布的
机器事实（`Descriptor.test.tsx` 钉住：同值重读也 +1，失败读也 +1）。

这个 primitive 是通用的：任何"命令效果可能等于屏幕上旧值"的控件都可以用它，而不是给某个场景
写死。

## trace 与 ledger

```text
idle, started, scan:running, scan:advertisement-matched, scan:stopped, link:connected, gatt:ready,
cccd:initial, notify:subscribed, cccd:enabled, notify:disabled, cccd:cleared, cccd:written,
cccd:unchanged, closed
```

`DESCRIPTOR_IO_LEDGER` 是第二证人（顺序即计划顺序）：

```text
start, begin-scan, scan-start-wait, scan-pull, scan-stop, connect, connect-wait,
gatt-discover, gatt-discover-wait,
descriptor-read, descriptor-read-wait,
notify-enable, notify-enable-wait,
descriptor-read, descriptor-read-wait,
notify-disable, notify-disable-wait,
descriptor-read, descriptor-read-wait,
descriptor-write, descriptor-write-wait,
descriptor-read, descriptor-read-wait,
close
```

账本校验新增一条**只折叠重试对**的规则（`_collapse_retries`）：`tapUntil` 未满足时会重复点，
App 就会连续记录 `X, X-wait, X, X-wait`。那是**一条**命令观察到落定，不是两条命令；只折叠
"同名且紧跟其 `-wait`"的相邻重复，别的同名词永远不会被合并。语义测试钉住：把模板里每次读都
写两遍仍然通过；把最后那次读整段删掉**必须失败**（`test_an_unchanged_read_that_never_happened_cannot_pass`）。

## 真机结果（Android，OnePlus PHB110 `3ac5cc09`）

| 项 | 值 |
|---|---|
| 运行 | `.work/android-native-descriptor-io/2026-11-25-f`，`status=passed`，trace 15/15，场景片 23 s |
| 离线 | `integrity=passed`、`fixture_close=advertising_inactive`（BTP controller-info advertising bit clear） |
| App APK | `7405e5dc…`；test APK `4888b97c…` |
| 每次读的 taps | `1, 1, 1, 2`——最后一次是**真的点了**（`advance` 生效）。随后的 driver 修复（每次点击后先等计数前进）把重复点击消掉，复跑 `2026-11-25-i` 为 `1, 1, 1, 1` |
| ledger | 首次 27 行（最后一次读点了两次，被折叠）；修复后 25 行、`omitted=0` |

速度也顺带改善：`advance` 去掉了"第一拍已满足就跳过"和一次多余的 `wait descriptor=done`，
单场景从 40–50 s 级降到 23 s。

## 守卫（不需要设备）

- `tools/tests/react_native/test_scene_semantics.py`（+2）：重试对折叠后仍是同一场景；缺最后一次
  读的账本不能通过。
- `Descriptor.test.tsx`（+1，失败用例 +1 断言）：`descriptorReadTotal` 同值重读也 +1、失败读也 +1。
- `runStatus.test.ts`：键集与顺序钉住 `descriptorReadTotal`。
- `tools/react_native/run_status.py`：严格解码要求该字段存在（漂移即失败）。
- `test_scene_sequence.py`：顺序钉住 `link-facts < disconnect < descriptor-io < connect-failure`。

## 尚未完成

- **iOS 无设备证据**：`disconnect` / `connect-failure` / `link-facts` / `descriptor-io` / `devices-query`
  五条都只在 Android 真机跑过。iOS 侧 `tapUntil` 的 `advance` 与 `awaitStatusAbove` 已实现，但没有
  在真机上跑过。
- **全序列（17 场景）**：`background` 在序列里两次以不同症状失败而单跑通过（见
  [`2026-11-25-link-facts-scene.md`](2026-11-25-link-facts-scene.md) 末节），在拿到可复现序列前
  不记成场景缺陷，也不声称 17/17。
- **账本窗口**：本次修复后 `descriptor-io` 回到 25 条（窗口 32），但场景再长或重试再多仍可能撞上
  `EVIDENCE_LIMIT`。现在 `_require_ledger` 会明确拒绝被截断的窗口，而不是拿后缀冒充整段会话。
