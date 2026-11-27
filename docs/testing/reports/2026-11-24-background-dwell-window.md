# `background` 场景的声明窗口：候选事实与两端重挣

日期：2026-11-24

范围：`background` 这一个 managed 场景的时序语义。其它 11 个场景的语言没有被改动，但它们与本场景在
**同一个候选、同一次序列**上重跑，因此本报告同时是那次候选的双端 12 场景记录。

## 起点：窗口是瞬时的

`background` 上线时的 `away` 阶段只有一步 `{"op": "background"}`：驱动按下 Home、证明 App 不在前台，
然后就返回。App 真正离开前台的时间 ≈ 宿主 `publish` + 下一阶段测试进程启动，即 1–4 秒。

这不足以支撑场景的声明。iOS 在数秒内就把后台 App 挂起（`runningBackground` → suspended），
而 Android 靠 `BleHubKeepAliveService` 常驻进程；一个从未被挂起的 App 不可能丢掉订阅，
所以「订阅活过了后台窗口」在瞬时窗口下几乎是恒真的——场景看起来通过，实际什么都没证明。

这也是计时证据里的一处矛盾：Android `background` 只要 23 s，比 `ccc-disable-reject`（35 s）还短，
而它声明的事情（一次前后台往返）本该是最慢的之一。

## 改法：窗口由计划声明，驱动守住它

| 位置 | 内容 |
|---|---|
| `tools/react_native/scene_plans.py` | `BACKGROUND_DWELL_SECONDS = 30`；`background.away` = `{"op": "background", "for": 30}` |
| `BlengSceneDriverUITests.swift` | `background` 要求 `for ∈ (0, 60]`；先证明不在前台（15 s 宽限、被吞的 Home 补按一次），再按 1 Hz 采样同一事实直到窗口结束；提前回前台判失败 |
| `BlengSceneDriverInstrumentedTest.kt` | 同一语义：`pressHome()` → `currentPackageName != appPackage` → 按 1 Hz 守住窗口 |
| `bleng_scan_evidence.py` | `BACKGROUND_SCENE.min_seconds` 90 → 120，让场景预算覆盖窗口本身 |

设计要点（对应约定 C8 的另一半）：**要守住的窗口必须声明**。若驱动自己决定「多久算后台」，场景策略就跑进了
执行器；而瞬时离开又证明不了任何事。窗口现在同时出现在三处且互相对得上：计划步骤的 `for`、场景的预算下限、
以及 receipt 里的 `observed["background"]`（`not-foreground for=<实测>s`）。

窗口值 30 s 不是魔法数：iOS 挂起后台 App 在数秒量级，30 s 保证挂起已经发生；上限 60 s 由驱动强制，
避免一个笔误把一场场景变成十分钟。

## 守卫（无需设备的测试）

- `tools/tests/react_native/test_scene_semantics.py`（executor-free）：
  - `test_a_subscription_that_outlived_the_background_window_is_the_whole_trace`
  - `test_a_value_the_background_window_lost_cannot_pass`（回前台后只数到 1 个值 → 判丢事件）
  - `test_a_window_the_app_did_not_stay_away_for_cannot_pass`（`away` 阶段没通过就不得被叙述成通过）
  - `test_a_window_the_receipt_does_not_show_cannot_pass`（receipt 只报了 12 s → 判窗口没守住）
  - `test_a_background_step_without_a_sample_cannot_pass`（根本没有 `observed["background"]` → 判未被采样）
  - `test_the_background_step_declares_the_window_it_holds`
- `scene_native._require_dwell`：派生侧不信 `status`，而是从 receipt 里读实际停留时长，
  与计划声明的 `for` 对比（只宽恕最后一位小数）。这与 `passive-disconnect` 的 `_require_quiet_step`
  是同一条规则：**时序声明必须由证据采样过**。
- `tools/tests/react_native/test_ios_scene_driver.py`：
  - 已知 op 集合现在也覆盖**组合后的计划**（组合计划才是设备真正执行的东西），`background` 单独规定形状
    （恰好 `{"op", "for"}`，`for ∈ (0, 60]`）。
  - `test_the_background_plan_leaves_the_app_and_returns_to_it` 追加窗口断言，以及「场景预算 ≥ 窗口」。

`tools/tests`：**992 passed / 21 skipped / 2814 subtests**（`pixi run python -m pytest tools/tests -q`），
`pixi run just lint` 通过。

顺带修掉的一处守卫漏洞：`test_every_step_names_a_known_op` 之前只遍历静态 `lane.PLANS`，组合计划里的步骤
（如 `background`、`waitAny` 的分支）从未被它检查过。

## 候选与命令

同一个候选上跑了两端完整序列。

| 项 | iOS | Android |
|---|---|---|
| App | tree sha `7a9711d461518e90dc01a6b9ace71cd3d947587ebe670c27bd2335ccae92542f` | APK `5191f9711453347db318366892d0504f271948125fdb144234b912d71b00bf32`（未变） |
| 驱动 | `BleHubBlengHost_iphoneos27.2-arm64.xctestrun`（重建，Swift 改动） | test APK `a615af544cbc5bae883e72f92eb82ae6a45fb713e25443df30668890d538bb3c`（重建） |
| 设备 | `00008110-000C61160C39401E`（iPhone 14 Plus） | `3ac5cc09`（OnePlus PHB110，root） |
| fixture | DK-B `jlink:1050251479`，profile v2-b | DK-A `jlink:1050252028`，profile v2-a |
| run root | `.work/ios-native-sequence/2026-11-24-dwell` | `.work/android-native-sequence/2026-11-24-dwell` |

```sh
pixi run just react-native formal-host-ios-device-test-build --device 00008110-000C61160C39401E
pixi run just react-native formal-host-android-build debug

pixi run just react-native formal-host-ios-managed-sequence \
  --output .work/ios-native-sequence/2026-11-24-dwell --device 00008110-000C61160C39401E \
  --app-tree-sha256 7a9711d4… --xctestrun …/BleHubBlengHost_iphoneos27.2-arm64.xctestrun \
  --fixture-contract …/multifunction-rf-v2-b.json --fixture-profile …/multifunction-rf-v2-b.json \
  --fixture-port /dev/cu.usbmodem0010502514791 --fixture-resource-identity jlink:1050251479 \
  --authorize-install --authorize-fixture-start

pixi run just react-native formal-host-android-managed-sequence \
  --output .work/android-native-sequence/2026-11-24-dwell --device 3ac5cc09 \
  --app-id com.blehubblenghost --activity com.blehubblenghost.MainActivity \
  --apk …/app-debug.apk --apk-sha256 5191f971… \
  --test-apk …/app-debug-androidTest.apk --test-apk-sha256 a615af54… \
  --fixture-contract …/multifunction-rf-v2-a.json --fixture-profile …/multifunction-rf-v2-a.json \
  --fixture-port /dev/cu.usbmodem0010502520281 --fixture-resource-identity jlink:1050252028 \
  --authorize-install --authorize-fixture-start --authorize-bluetooth-permissions --use-root
```

（命令块为了可读性折行并省略了很长的绝对前缀。）

## 结果

两端 **12/12 场景通过**，序列在依赖顺序上一次性走完。

| 场景 | Android | iOS |
|---|---|---|
| scan | 13 s | 23 s |
| notification | 18 s | 38 s |
| indication | 18 s | 38 s |
| **background** | **55 s** | **94 s** |
| io | 20 s | 39 s |
| cancellation | 23 s | 59 s |
| write-reject | 23 s | 53 s |
| ccc-enable-reject | 21 s | 56 s |
| ccc-disable-reject | 35 s | 78 s |
| passive-disconnect | 23 s | 48 s |
| recovery | 32 s | 84 s |
| recovery-ten | 87 s | 241 s |
| 场景合计 | 368 s | 851 s |
| 序列墙钟 | 467 s | 1082 s |

`background` 的窗口证据（`driver/receipt-away.json`）：

- Android：`not-foreground for=30.8s`（场景 23 s → 55 s）
- iOS：`not-foreground for=30.1s state=2`（场景 64 s → 94 s，state 2 = `runningBackground`）

其余场景相对上一轮（Android 314 s / iOS 763 s）的差异属于设备侧抖动，不是本次改动；本报告不据此推断
任何吞吐结论。

离线复验：对两个 run root 下的全部 24 个 attempt 执行
`pixi run python -m tools.react_native.bleng_scan_evidence <attempt>`，结果
**24/24 `integrity_status=passed` / `status=passed` / `host_gate_status=pending` / `cleanup_errors=[]`**。

新增的 `_require_dwell` 是派生侧的纯函数，所以可以直接拿**已发布的 phase 视图**回放：
把两端 `background` attempt 的 `driver/phase-*.json` 交给 `_observations`，两端都重新得出完整的
13 条 trace（Android `for=30.8s`、iOS `for=30.1s`），无需为这条守卫再跑一次设备。

## 仍然成立的边界

- `host_gate=pending` 是设计：managed 场景的 verdict 由 host gate 收尾，本报告只声明场景真机通过。
- 窗口证明的是「App 在后台期间订阅仍然送达」。它不证明冷启动恢复、进程被杀后的重建，也不覆盖
  iOS CoreBluetooth state restoration——那些是各自独立的场景。
- 30 s 是场景策略，不是产品常量；若要观察更长的系统后台策略（例如低电量模式、内存压力下的回收），
  应当新增场景而不是拉长本场景，否则「一次前后台往返」会变成「一次长时间驻留」。
- 本报告证明了这**一个**候选；ABI 仍未冻结，压力测试仍未开始。
