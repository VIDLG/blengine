# `link-facts` 场景：平台能做的照做，不能做的要说出来

日期：2026-11-25

范围：第 15 个 managed 场景 `link-facts`（MTU 查询/请求、连接优先级、GATT 缓存失效），
它的第一条真机证据，以及一次由**控件条高度**引起的真实回归。状态：Android 单场景真机通过、
`integrity=passed`；**全序列当前在 `background` 处失败**（见末节），所以本报告不声称 15/15。

## 为什么是这一条

RN 包新增了一大片能力（见 [`api-coverage-matrix.md`](../../research/react-native/api-coverage-matrix.md)），
其中 MTU / 优先级 / 缓存失效这三类有一个共同点：**平台可能合法地没有这个能力**。
CoreBluetooth 明确拒绝 `requestMtu`（"does not support explicit MTU requests"）、拒绝
`setConnectionPriority`、拒绝 `refreshGatt`；Android 三者都接受。场景要证的不是"都成功"，而是
**每个答案都以自己的形状出现**：

| 终态 | 含义 | 允许作为答案 |
|---|---|---|
| `done` | 平台照做了 | ✅ |
| `unsupported` | 平台说自己没这个能力（带平台自己的拒绝码） | ✅ |
| `failed` | 命令失败了 | ❌ 不是能力缺失 |
| `pending` | 提交了、没人回答 | ❌ 这正是超时会伪装成的东西 |

`unsupported` 的判据是失败码 `bleng.dispatch_rejected.not_supported`
（`crates/bleng-supervisor/src/ffi/failure.rs`），App 在 `useManualScan` 里读它；场景再要求
**投影里的 `failure` 也必须是这个码**——一个"unsupported"若来自超时，两个证人会对不上。

## trace 与 ledger

```text
idle, started, scan:running, scan:advertisement-matched, scan:stopped, link:connected,
mtu:observed, mtu:request-answered, mtu:re-queried, priority:answered, cache:refresh-answered, closed
```

trace 只说"每个问题都被回答过"，不说"四个都成功"——否则同一条场景在 iOS 上必然失败，
而失败原因是平台事实，不是 App 的缺陷。

`LINK_FACTS_LEDGER` 是第二证人：`mtu-query` / `mtu-request` / `mtu-query`(重查) / `priority` /
`refresh` 各一对 submission+wait，且**允许存在的失败只有一类**：detail 里带
`bleng.dispatch_rejected.not_supported` 的 wait。任何别的失败（例如 `observation/bleng.timeout`）
都会让场景失败。

## 关键设计：请求之后要重查

第一版只做到"请求被接受"。真机上 Android 报 `20/20`（默认 ATT MTU 23 → 20 字节载荷），
请求 247 之后也仍然是 `20/20`——因为**没有再问一次**。于是加了一步重查：

```text
mtu      : mtu=done writes=20/20  req=none
mtuAfter : mtu=done writes=248/248 req=done
```

- 请求 `done` → 重查的上下限必须**不小于**请求前（缩小即失败）；
- 请求 `unsupported` → 重查必须**等于**请求前（"被拒绝却变了"同样是失败）。

这一条把"回执"变成了"效果"：248/248 说明对端真的把 MTU 抬到了 251。

## 一次由 UI 引起的回归（重要）

第一版把这 4 个按钮 + 4 行结果文本**加进** `CommandConsole`。结果 `notification` 场景立刻在真机失败：

```text
scene step 36 {"id":"action-ledger","labelContains":"notify-disable-wait|"} failed: … saw absent
```

原因不是 App 逻辑：console 是固定的 pinned 机器面，加高之后 `disable-notifications` 被挤出视口，
而 **Android 对不在视口内的元素根本不渲染**，于是那一下 `tap` 点在了空气上。

改法：4 个按钮并入已有的 `Ids.link.controls` wrapping 行（标题缩短为 `MTU` / `MTU req` /
`Priority` / `Refresh`），**结果事实不再新增文本行**——它们本来就是 `run-status` 的
`mtu` / `mtuWrites` / `mtuRequest` / `priority` / `refresh` 标量。console 高度基本回到原状，
`notification` 与 `link-facts` 都重跑通过。

这条教训写进了 coverage matrix 的维护规则：**console 是固定预算，不是随便加的容器**。

## 真机结果（Android）

| 项 | 值 |
|---|---|
| 单场景 | `.work/android-native-check/2026-11-25-link-facts`、`.work/android-native-link-facts/2026-11-25-d`，`status=passed`，trace 12/12，场景片 17–18 s |
| 离线 | `integrity=passed`、`fixture_close=advertising_inactive` |
| App APK | `81403ef5…`（首次通过）/ `9d468672…`（console 修好后） |
| 平台答案 | MTU 查询 `20/20`；请求 247 `done` → 重查 `248/248`；优先级 `done`；缓存刷新 `done` |
| ledger | 19 行全 succeeded |

## 全序列的现状（未解决，如实记录）

两次全量序列都在 `background` 处停下，而 `background` **单跑通过**（55 s）：

| 序列 | `background` 的失败 |
|---|---|
| `2026-11-25-b` | 234 s 后 `scene driver deadline expired before instrumentation`（instrumentation 从未启动） |
| `2026-11-25-c`（`--timeout 900`） | 83 s 处 `tap pull-notification` 超时；结束时的投影是 `link=disconnected;linkEnd=unrequested;notify=ended;notifyTotal=1`，即**链路在场景中途被对端结束** |

两者症状不同，都不是 `link-facts` 的语义问题，也都无法在单跑里复现。`background` 是唯一会让 App
在真机上**真的进后台 30 s** 的场景，因此它最可能暴露手机侧的进程/前台服务状态差异；在拿到可复现序列
之前，不把它记成场景缺陷，也不声称 15/15。

## 守卫（不需要设备）

- `tools/tests/react_native/test_scene_semantics.py`（+10）：done 分支与 unsupported 分支都得到同一 trace、
  unsupported 必须带平台码、`failed`/`pending` 不能通过、MTU 上下限缺失或低于 ATT 下限不能通过、
  请求 `done` 后重查变小 / 请求 `unsupported` 后重查变化都不能通过、ledger 里非"能力缺失"的失败不能通过。
- `LinkFacts.test.tsx`（+6）：查询终态与 ledger、`unsupported` 与 `failed` 互不冒充、优先级与刷新互不串台、
  无链路时四个控件都禁用、只提交未落定时投影停在 `pending`。
- `test_scene_sequence.py`：顺序钉住 `io < link-facts < disconnect`。
