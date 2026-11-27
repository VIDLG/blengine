# iOS managed 车道重挣 + 场景耗时收敛（2026-11-24，候选事实）

日期：2026-11-24
范围：iOS 11 个 managed 场景、Android 11 个 managed 场景、两端 scene driver
设备：iPhone 14 Plus `00008110-000C61160C39401E`（iOS 27.2.1 / 24B156）；OnePlus PHB110 `3ac5cc09`；
DK-A `jlink:1050252028`（v2-a）、DK-B `jlink:1050251479`（v2-b）
结论：两端各 11/11 通过；iOS 首次完整真机通过；Android 场景时间 −37%，iOS −14%。过程中修掉三个真缺陷。

## 1. iOS 车道的阻塞与解除

上一轮 iPhone 侧一直停在 `Timed out while enabling automation mode`（xcodebuild exit 65）。解除前的
可读证据（这台 Mac 能读到的全部）：

| 检查 | 结果 |
|---|---|
| Mac 启用 automation mode 的免认证策略 / developer mode / keychain | `unattended`（`xctest_unattended` 报 `host_unattended=yes`） |
| 手机锁屏 | 未锁（`passcodeRequired: false`、`unlockedSinceBoot: true`） |
| 手机 Developer Mode | `enabled` |
| 开发者磁盘映像 | `isUsable: true`（含 `XCTest-25435`） |
| Xcode / iOS | 27.2 (27B48) / 27.2.1，配套 |

决定性实验：同一 xctestrun、同一台手机，

- `BleHubBlengHostDeviceTests`（有宿主 App、非 UI）**4.6 s 通过**；
- `BleHubBlengHostUITests` **63 s 后超时**。

所以设备测试通道、DDI、签名、安装都正常，坏的是「UI 自动化」那一层。

什么把它修好的**不确定**：那段时间同时发生了三件事——用户在手机上重新开关了 UI 自动化、重插了 USB、
我跑了一次非 UI 的 `BleHubBlengHostDeviceTests`（它本身就在设备上建立了一次真实的测试会话）。任一件都
可能起作用，所以本节**不**把成功归因于开关。设备属性里
`bootedFromSnapshot=true` / `bootedSnapshotName=com.apple.os.update-…` 只说明该次启动来自一个系统更新快照，
不提供时间，也不证明开关在哪个时刻被复位过。

**一条流程教训（已采纳）**：先保留 XCTest 原始日志，对照同一份 xctestrun 的 UI 与非 UI 结果。
手机上开关的实际状态只按操作者当时的屏幕观察记录，缺少读数就记为未知。

## 2. 修掉的三个真缺陷（都由真机失败暴露，不是推断）

### 2.1 诊断文件名带冒号，bundle 发布被拒

`recovery` 场景通过后，整个 bundle 失败：

```text
unsafe relative path: 'driver/diagnostic-scan:0-1-ios-scene-driver-app-hierarchy-0-…txt'
```

循环场景的阶段名是 `scan:0` / `announce:0`，而 iOS executor 用 `phase.name.replace(".", "-")`
拼诊断文件名，冒号漏了过去；`evidence_files` 随后拒绝该路径。Android executor 早就用 `_slug()`
（`[^a-z0-9]+ → -`），iOS 是唯一的例外。改为 `_slug(phase.name)`，并加离线测试
`test_a_repeating_phase_name_is_a_safe_file_name`。

### 2.2 释放后平台把 App 恢复 → `refusing takeover`

sequence 在每场前用 `release_app` 证明 App 不在运行，但 iOS 会把刚被终止的后台 App 作为「最后前台」
恢复，恢复可能发生在释放证明与安装之间：

```text
RuntimeError: formal iOS App already running; refusing takeover
```

项目本来就有这条安全出口：`--release-before-install`（只终止 candidate 自己的 inventory 归因到本
bundle 的 PID、记录该动作、并在安装前证明生效）。此前 sequence 只加 `--release-retained-app`，没有
这一条。现在两者都加（`RECLAIM_FLAG`），守卫 `test_a_sequence_cannot_keep_the_app_of_the_scene_it_just_ran`
要求两个 flag 各出现一次。外部 App 仍然 fail closed。

### 2.3 答完的系统弹窗被记成 `absent`

`answerAlert` 在「答完一个、窗口已到」的路径上会退出循环并把 `observed` 覆盖成 `absent`
（`required` 时还会误报失败）。两端同时修：只有 `answered == 0` 才写 `absent`，`required` 的失败也
只在没答到时发生。

## 3. 耗时：先量，再改

一份完整 iOS 序列（run j，11 场）的逐步骤统计，回答「时间花在哪」：

| 步骤 | 次数 | 总耗时 | 平均 | 问题 |
|---|---:|---:|---:|---|
| `answerAlert` | 32 | **130.5 s** | **4.08 s** | 可选弹窗也要跑满 step 的 `timeout=4.0`，32 次全部「没弹窗」 |
| `tap` | 74 | 64.0 s | 0.86 s | XCUITest 合成触摸固有成本 |
| `setText` | 20 | 51.2 s | 2.56 s | 键盘输入 + 清空/校验循环 |
| `wait` | 129 | 37.2 s | 0.29 s | 真实事实等待，合理 |
| `read` | 103 | 32.8 s | 0.32 s | 复合节点读，合理 |

### 3.1 `answerAlert` 声明窗口（两端）

计划层新增字段 `grace`：可选弹窗只观察声明的窗口（防守位 0.5 s，动作后可能弹的位 2.0 s），
`required` 才用满 `timeout`。iOS 与 Android 驱动读同一个字段、同一个语义
（`watch = required ? timeout : min(timeout, grace)`）。

Android 另外补了一个**便宜的前置问题**：`device.currentPackageName == appPackage` 时直接返回
（一条查询代替 3 条规则 × 多个按钮的近二十次扫描）。

效果：iOS `answerAlert` 从 4.08 s/次降到 **1.33 s/次**；Android 从 4 s+/次降到 **0.46 s/次**。

### 3.2 Android `activate` 先问「是不是已经在前台」

Android 场景是单进程，App 全程在前台，而 `activate` 每次都做「按 Home → sleep 300 ms →
`am start -W` → 等前台」，1.32 s × 47 次。现在先看 `currentPackageName`：已经前台就直接记录
`already-foreground` 返回（这个事实正是该步骤要建立的状态），外部窗口才走完整路径。

> iOS 的 `activate` **不做**这个优化：它先按 Home 是刻意为之——presented 的 App Switcher 会吞掉
> 后续点击，而 switcher 的窗口在 SpringBoard 树里恒存在、检测不可靠。为省 40 s 重新引入那个坑
> 不划算。

### 3.3 `setText` 的耗时分解（埋点，不改行为）

每个 `setText` 把 `focus/clear/type/verify` 写进 receipt 的 `observed`。真机数据（26 次）：

```text
focus=0.61 clear=0.14 type=1.38 verify=0.16
focus=0.66 clear=0.16 type=1.08 verify=0.16
focus=0.93 clear=1.25 type=0.52 verify=0.17   ← 清空循环偶发重建
```

即：**键盘 `type` 占大头（1.0–1.4 s，36 字符 UUID）**，聚焦 ~0.8 s，清空与校验各 0.1–0.2 s。
再往下要动，只能把「场景参数」提成 App 自己的机器面（plan 声明 + `read` 证明），那会改产品表面；
按目前收益（iOS 约 57 s / 763 s）不值得动，记录为候选。

## 4. 结果（同一套 11 场景，同一 App/测试包）

### Android（`.work/android-native-sequence/2026-11-24-o`）

| 场景 | 基线 | `grace` | `grace` + activate |
|---|---:|---:|---:|
| scan | 27 | 12 | 11 |
| notification | 32 | 18 | 18 |
| indication | 32 | 17 | 17 |
| io | 33 | 22 | 19 |
| cancellation | 39 | 25 | 23 |
| write-reject | 38 | 24 | 23 |
| ccc-enable-reject | 38 | 25 | 23 |
| ccc-disable-reject | 53 | 40 | 36 |
| passive-disconnect | 38 | 25 | 23 |
| recovery | 52 | 35 | 33 |
| recovery-ten | 120 | 99 | 88 |
| **合计** | **502 s** | **342 s** | **314 s（−37%）** |

测试包 sha：`c12aa3bf6f12e824e2c9988fbe444ced1d1b7d3a1efe3e59addb5d89742a3f6e`；
App APK 未变：`fb914530573210670c60825f1fff6cbeed9f06233755e893857be58f18339274`。

### iOS（`.work/ios-native-sequence/2026-11-24-n`）

| 场景 | 基线 | `grace` |
|---|---:|---:|
| scan | 35 | 23 |
| notification | 50 | 39 |
| indication | 50 | 39 |
| io | 51 | 39 |
| cancellation | 70 | 59 |
| write-reject | 64 | 54 |
| ccc-enable-reject | 68 | 57 |
| ccc-disable-reject | 89 | 78 |
| passive-disconnect | 59 | 48 |
| recovery | 96 | 85 |
| recovery-ten | 254 | 242 |
| **合计** | **886 s** | **763 s（−14%）** |

App 树 sha：`0f389cb5540d5afa2aaad215489414338a3150c33a3b867c9b0aa88bd91aef7f`
（`b67288e1…` 与 `d5f5e2f3…` 是本次迭代中的中间构建，均已作废）。

两端 bundle 逐目录离线复验：22/22 `integrity=passed`、`host_gate=pending`、`cleanup=[]`。

## 5. 一个运行时事实：人工操作手机不是测试失败

一次运行中场景耗时从 89 s 涨到 240 s 以上。阶段的 `xcresult` 显示**测试本身通过**，但
`activate` 花掉 61 s、`tap` 花掉 121 s。附件（hierarchy）给出原因：

```text
Window … identifier: 'SBSwitcherWindow:Main'
  … identifier: 'AppSwitcherContentView'
    … identifier: 'card:FWVYQ287W5.bleng-rn-host:…'
```

手机停在多任务切换器（人在拿手机看弹窗），XCTest 必须先把它关掉并把 App 拉回前台。这不是缺陷，
也不是测试判定问题——但它说明**运行期间手机必须无人触碰**，否则耗时会失去意义。

## 6. 并行执行

两条 lane 用两台手机，因此可以各用一台 DK 并行（Android ↔ DK-A/v2-a，iOS ↔ DK-B/v2-b）。
隔离由 `expected_local_name` 与 profile 的 service UUID 保证（`BleHubMultiRfV2DKA` /
`fd600000…` vs `BleHubMultiRfV2DKB` / `fd700000…`），每条 bundle 记录自己的 fixture 身份。
本节的两端结果就是这样一次并行运行得到的，墙钟从约 20 分钟降到约 10 分钟。

## 7. 限制

- 每个 lane 各一次运行；并行运行期间 2.4 GHz 共存未做专项测量。
- iOS 仍未在最终构建上重复整轮（`0f389cb5…` 这一轮是最终构建，run n 11/11）。
- 没有 ABI、Phase 退出或压力结论；`host_gate=pending` 是离线复验的正确值。

## 8. 后续（同一日）：后台能力 + 两个 lane 缺陷

用户批准“尽可能保持连接”并接受 Android 常驻通知后，加了两项产品能力：

| 平台 | 改动 | 验证 |
|---|---|---|
| iOS | `Info.plist` 加 `UIBackgroundModes: bluetooth-central` | 构建产物 `Info.plist` 里读到该值 |
| Android | 新增 `BleHubKeepAliveService`（`foregroundServiceType=connectedDevice`），**只在 `onStop`→`onStart` 之间运行**，所以通知只在后台期间存在 | 真机 `dumpsys activity services`：后台时 `isForeground=true`、`types=0x00000010`、通知已挂；回前台后服务消失 |

并行重跑两端时暴露两个真缺陷（均已修）：

1. **cleanup 预算被场景吃光**。场景预算同时是 `finalize` 的截止时间，一个用满预算的场景让 cleanup 只剩几秒，而
   `devicectl` 拒绝任何低于 5 秒的 `--timeout`——于是 **已经通过**的 `recovery` 在 `candidate.finalize` 报
   `iOS identity SDK failed (exit=64)`。现在 scenario 预算在 sequence 里预留 `CLEANUP_SECONDS`，且 SDK 超时
   的下限写成显式常量。
2. **native identity 等待没有自己的上限**。若 App 是**上一轮遗留的实例**（`launch` 会复用运行中的进程），它
   永远不会写本轮 run id 的 identity 文件，循环就一直重试到场景截止——实测空转 18 分钟、5207 次拷贝。现在该
   等待独立限时 30 秒，并直接命名最可能的原因（“很可能由上一轮启动，安装前先释放”）。

同一轮 Android 在带前台服务的新 APK（`5191f971…`）上 **11/11 通过**，说明产品能力没有回归。

**iOS 当时一度阻塞，根因已查明并修复（见 §9）**：加完后台模式后的构建（树 `381559df…`）在 run p 上
已跑到 10/11，之后几轮在 native identity 握手处失败。当时我把 boot id 的变化误读成“手机重启、
开关复位”——**这是错的**，见 §9 的更正。

## 9. native identity 握手：根因与修复

### 9.1 更正：“boot id 变化 = 手机重启”是错的

把今天所有 run 的 identity 文件拉平对照后发现：

```text
run j → 12 个场景，12 个不同 boot_id
run n → 12 个场景，12 个不同 boot_id
run p → 11 个场景，11 个不同 boot_id
```

`boot_id` 是**每次 App 启动**生成一次（`NativeProcessIdentityContext.makeBootId()`），不是设备启动 id。
因此它变化不构成重启证据；**今天没有任何重启证据，也没有任何证据表明 UI 自动化开关被复位过**。
之前的建议（“去手机上确认开关”）是错误归因。

### 9.2 真正断在哪

失败发生在 `bind`，在任一 UI 测试开始之前：

- 宿主 `devicectl device copy` 把 `identity-request.json`（含 challenge）写进 App 容器；
- `devicectl device notification post` 回执明确写 “Posted 'dev.blehub.formal-rn.<run>.<boot>.inspect'”；
- 但 App 回写的 `native-inspection.json` 里 `challenge` 恒为 `null`（run r 132 次、run s 272 次拷贝）；
- 给 responder 加了一个把诊断写进容器的日志后，该日志**根本不存在** → `respond()` 从未被调用。

即：**Darwin 通知没有被投递给 App**，不是某一守卫拒绝了请求。

### 9.3 修复：把证明和传输分开

挑战-响应才是“活进程”的证明，Darwin 通知只是传输。现在 responder 除了听通知，还**自己看请求文件**：

- 首次应答前 100 ms 一次；应答后降为 1 Hz（每次只是一次文件读，不下发任何设备命令），
  这样 cleanup 阶段的二次 identity 复查也能被应答；
- 两条路径汇入同一个幂等的 `respond()`；
- 每个守卫的静默 `return` 现在写一行 `identity-responder.log`（限 8 KB），拒绝原因可读。

修复后单场景端到端通过（含 `finalize` 3 s），随后 **iOS 11/11 通过**（`.work/ios-native-sequence/2026-11-24-w`，
App 树 `cefd194e198a5198958c651d1b3f048f597913dfa587632317297c20a34c166c`，场景合计 746 s），
11 个 bundle 离线复验 `integrity=passed` / `host_gate=pending` / `cleanup=[]`。
