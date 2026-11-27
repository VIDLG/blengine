# 同日同批闭环：两端各 20 条 managed 场景、4 条 native lane、6 个 artifact consumer

日期：2026-11-26（序列跨到 2026-11-27 00:31）
证据根：`.work/android-native-sequence/2026-11-26-d`、`.work/ios-native-sequence/2026-11-26-f`

本文把 `abi-freeze-readiness.md` §4 第 2 步要求的「同一份候选上的完整闭环」重建并记录成一份。
它是候选事实的汇总，不是冻结公告，也不把 `host_gate=pending` 当成通过。

## 本批产物

| 平台 | 产物 | 指纹 |
|---|---|---|
| Android | App APK | `a1fcb8efdf174c674854df182ee8fe136d021efd20a092aa38b39e8933ea1518` |
| Android | 测试 APK | `0578d9b2690398cd6dd17097b6929e9c27997903a7f9aeff801bfc65fbafc7ad` |
| iOS | App 树 | `662451e7420f628e65f68152597e89b769a8571b842111a9ca599d7edad06f97` |
| iOS | xctestrun | `4bca358da3b431f54a388398eefe4902496789e225447530355321c7234ff3c9` |
| 双方 | Peripheral | nRF52840 DK-A `jlink:1050252028`（`multifunction-rf-v2-a`），boot nonce `91a0f16bffc1d12fe30e5fee2e3ab9a4` |
| 双方 | 设备 | OnePlus PHB110 `3ac5cc09`（API 36）；iPhone 14 Plus `00008110-000C61160C39401E`（iOS 27.2.1） |

iOS 的 App 树指纹是**构建产物**的指纹，不是源码版本：同一份源码两次构建会得到不同的哈希（本轮
`7e56a50d…` → `634f0575…` → `9136bacf…` → `662451e7…`），而 `main.jsbundle` 四次完全相同
（`89d3e861…`，3 348 188 字节）。所以跨构建比较要认 bundle，不认树哈希；本文的每条结论都绑定到
上表这一份树。

## 结果

| 门禁 | 命令 | 结果 |
|---|---|---|
| Android managed 20 条，一次运行 | `formal-host-android-managed-sequence` | **20/20 passed**，11.9 min（714.9 s），20 份 bundle 逐个离线 `integrity=passed`、`cleanup_errors=[]` |
| iOS managed 20 条，一次运行 | `run_bleng_ios_scenes` | **20/20 passed**，33.4 min（2 003.0 s），20 份 bundle 逐个离线 `integrity=passed`、`cleanup_errors=[]` |
| Android native lane（7 次运行） | `notification` single/sequence/overflow、`adapter-transition`、`permission-transition`、`multi-link` 1/2 轮 | 全部 passed（[报告](2026-11-26-android-native-lanes.md)），与本批同一份 App/测试 APK |
| 两端 artifact consumer（6 个） | `ffi android-backend-consumer(-build)`、`ffi android-bleng-build`、`apple consumer-ios`、`consumer-macos`、`consumer-bleng-macos`、`consumer-bleng-ios-unsigned` | 全部通过（[报告](2026-11-26-artifact-consumers.md)） |
| Android native contract | `formal-host-android-native-test` | 通过 |
| 包文档与示例契约 | `tools/tests/react_native/test_api_docs.py`、`react-native quickstart-typecheck` | 6 条契约通过 |
| 九份 API 快照 | `binding snapshot-check` / `binding bleng-snapshot-check` | `verified 3` / `verified 6` |

每份 iOS bundle 还带着 `phone_display`（`awake=true`，亮度 109–235）——这是本轮新增的
「屏幕亮着」观察，见下文。

### 每条场景的耗时

Android 的 `step_seconds_total` 是每条驱动记录的聚合值（每个 phase 都写同一个数），iOS 是分 phase
的，所以两张表的口径不同；序列总时长以 `progress.jsonl` 为准。

| 场景 | Android (s) | iOS (s) |
|---|---|---|
| `scan` | 10.3 | 17.6 |
| `notification` | 15.6 | 28.0 |
| `indication` | 16.2 | 27.6 |
| `background` | 52.8 | 68.1 |
| `io` | 18.3 | 33.2 |
| `link-facts` | 15.6 | 25.6 |
| `disconnect` | 22.8 | 37.3 |
| `descriptor-io` | 19.3 | 35.1 |
| `devices-query` | 11.0 | 20.6 |
| `devices-kinds` | 8.5 | **553.7** |
| `adapter-control` | 9.2 | 13.1 |
| `connect-failure` | 48.5 | 61.6 |
| `auto-connect` | 16.8 | 23.7 |
| `cancellation` | 18.9 | 34.8 |
| `write-reject` | 20.0 | 35.7 |
| `ccc-enable-reject` | 19.2 | 34.5 |
| `ccc-disable-reject` | 32.4 | 48.3 |
| `passive-disconnect` | 20.9 | 35.7 |
| `recovery` | 31.1 | 44.0 |
| `recovery-ten` | 88.8 | 104.5 |

iOS 的 `devices-kinds` 是 553.7 s：同一份计划在别的运行里是 13 s。原因见下面「自动化通道降级」。

## 本轮为此修掉的两处缺陷（都会造成假失败）

### 1. `background`：pull 的等待被上一轮的值满足（假见证）

序列 `.work/android-native-sequence/2026-11-26-b` 在 `background` 失败：

```text
RuntimeError: the value emitted while the App was away never arrived;
the subscription did not outlive the background window
```

逐帧看：`drained` 读到 `notifyTotal=1`，而 `closed` 读到 `notifyTotal=2` —— 后台窗口里发出的那个值
**晚到了一点**。计划里 `pull-notification` 的等待条件是「`notify-latest` 含 `ef`」，而离开前屏幕上
已经有这个值，所以等待瞬间成立、`drained` 提前读。这与 `descriptor-io` 那次 `tapUntil` 命中旧值
是同一类错误：**用一个可能已被上一轮解释的帧当证据**。

修法：`background.back` 在 `pull` 之后、`drained` 之前，等 App 自己的计数器
（`wait run-status status={"notifyTotal": "2"}`）。真机上 `background` 单独跑通过，这一步耗时
0.62 s —— 正是之前被跳过的那段等待。回归测试
`test_returning_waits_for_the_away_value_instead_of_reading_the_old_one` 固定顺序。

### 2. `auto-connect`：fixture 的恢复证明与本场景的主张相矛盾

序列 `.work/android-native-sequence/2026-11-26-c` 在 `auto-connect` 失败：

```text
RuntimeError: advertising resume adopted a peer
```

`auto-connect` 的主张是「一个常驻的『peer 出现即连接』请求」：host 先撤走 peer（`withdraw`），
再恢复广播（`resume`），链路只能因为平台一直等着才出现。而 `DirectFixture.resume_advertising`
在恢复后的读回里要求**没有** peer —— 于是「App 立刻连上」这个**本场景要证明的答案**被当成失败。
它是不是出现，取决于 App 的重连定时器与 host 1 s 读回谁先到，所以同一场景今天先通过、下一步失败。

修法：把判断变成场景声明，而不是 fixture 猜测 ——
`require_resume_adoption_allowed(peer_present, expect_adoption=...)`，默认仍然严格；只有
`auto-connect` 用 `expect_adoption=True`（`tools/react_native/scene_native.py`）。

同一条修正还有另一半：peer 一旦采纳，控制器**会停掉可连接广播**，所以「恢复后广播位必须为 1」在
「已采纳」这条路径上本身就是错的 —— 下一次序列运行时它就以
`BTP controller is not advertising after resume` 失败（`.work/android-native-sequence/2026-11-27-a`）。
现在采纳了就直接返回：链路本身就是广播被接受的证据（代码注释与
`require_resume_adoption_allowed` 同时说明了两种答案）。两端单独重跑 + 两端 20/20 均通过。

## iOS 自动化通道的降级：已量化，不再造成假失败

本轮出现过三种症状，全部在 XCTest/设备侧，不在 App、计划或 fixture：

1. `activate` 报 `state=4, switcher=card-present`；
2. `setText` 30 s 内读回空串；
3. 单次按压 `press=120.6 s`（`hittable` 仍只有 0.1 s）。

量化证据（`.work/ios-native-sequence/2026-11-26-f/devices-kinds`）：

```text
10 setText scan-service-filter   63.6 s
     timing: focus=0.98 clear=0.16 type=1.13 verify=0.16   ← 步内自计只有 2.4 s
12 tap workbench-probe          121.0 s   hittable=0.11 press=120.65
14 tap devices-bonded           120.8 s   hittable=0.08 press=120.55
17 tap devices-companion        120.7 s   hittable=0.08 press=120.56
20 tap close-session            120.7 s   hittable=0.07 press=120.57
```

`setText` 的 63.6 s 里只有 2.4 s 落在它自己计时的段内，其余约 61 s 在**取元素的那一刻**（一次
snapshot 查询）；按压的 120.6 s 恰好是两次 60 s。也就是说这台设备上的 XCTest↔device 自动化通道会
成片地卡在 60 s 单位上，然后自己恢复。它与当天早些时候的
`Timed out while enabling automation mode`（[报告](2026-11-26-ios-automation-mode-block.md)）是同一类
现象，**没有代码层的根因可修**。

我们做的是三件「让它只花时间、不产生假结论」的事：

1. `tap` 记录 `hittable=`/`press=`，慢的地方一眼可辨（本轮就是靠它定位的）；
2. 一个阶段步时合计 ≥120 s 时，即使通过也保留 XCTest 控制台尾部（`xcodebuild_output`，16 KiB 上限），
   因为 idle/animation 警告只出现在那份日志里；普通阶段不携带它，bundle 不变大；
3. 计划不再依赖瞬态：`background` 等的是 App 自己的计数器，`auto-connect` 把「立刻连上」声明成
   期望答案，`ccc-enable-reject` 的 hold 阶段改看 App 自己的 `notify-enable-wait|` 行（不再要求读到
   瞬态 `notify=enabling`；若 waits 已结束则尾部变为 `succeeded` 并被如实拒绝）。这条改完后四个 ccc
   场景在两端各重跑一次，均通过（`.work/{android,ios}-native-ccc-*/2026-11-27-b`）。本轮同一次降级只
   让 `devices-kinds` 慢了 9 分钟，20 条仍全绿。

外加上一轮的屏幕门：`run_ios_scan` 在 lease 之前截图读亮度（`phone.display`），熄屏直接拒绝并点名
「自动锁定 → 永不」。屏幕熄灭时 `lockState` 仍报未锁屏，是所有「按钮点了没反应」的根源。

## 诚实边界

- **序列证据绑定在 23:40 构建的那一份产物上**（App 树 `662451e7…`，`main.jsbundle` `89d3e861…`）。
  此后 `integrations/react-native-bleng/src/` 在 05:46 被另一路工作改过（新增 `AbortSignalLike`
  导出）；消费 App 不引用该模块，所以行为未变，但包面变了 —— 冻结日重跑前要先让那部分落地并重新构建。
- `ccc-*-reject` 的 hold 计划在上述序列之后改过一次（改为以 ledger 为见证），四个 ccc 场景已在
  两端各重跑（`2026-11-27-a` / `-b`）；两条完整序列本身没有再重跑，因为包面当时正在被改。
- 每条场景都是 `host_gate=pending`：managed 场景 lane 不跑 host 侧门禁，20/20 不等于 host_gate 通过。
- 没有压力/长稳；`recovery-ten` 的 10 轮是场景级重复，不是 soak。
- 只有一台非一加/非三星设备；`expected_local_name` 与服务 UUID 已与机型解耦，但没有第二台 OEM 证据。
- iOS 证据绑定 App 树 `662451e7…`；重新构建会得到新树，证据需要按新树重跑。
- Android 的 `step_seconds_total` 是聚合值，不能按 phase 相加。

## 验证（离线）

| 命令 | 结果 |
|---|---|
| 20 份 iOS bundle + 20 份 Android bundle 逐个 `bleng_scan_evidence` | 全部 `integrity=passed`、`cleanup_errors=[]` |
| `pixi run python -m pytest tools/tests tests/peripherals/nrf52840/test_direct_fixture.py -q` | `1116 passed, 21 skipped, 3051 subtests passed` |
| `pixi run just --fmt --check` | 通过 |
| `pixi run just docs-check-mermaid` | 通过 |
| `pixi run just binding snapshot-check` / `bleng-snapshot-check` | `verified 3` / `verified 6` |
