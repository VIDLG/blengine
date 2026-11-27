# Managed `cancellation` 场景 + 一次阻塞即可观测的 host run（候选事实）

日期：2026-11-19。范围：RN formal consumer 的第五个 managed UI 场景 `cancellation`（fixture 在固件里
hold 住 active 读，App 排队第二条并取消），以及它逼出来的一条 host 观测/预算规则。Android 真机已通过；
iOS 尚未跑（隧道需人工 root 启动）。本文只记候选事实与已修缺陷，不改 gate、不冻结 ABI、不宣称 release
或 Phase exit。

## 结论

| 平台 | 入口 | run id | 结果 |
|---|---|---|---|
| Android (`3ac5cc09`, PHB110) | `formal-host-android-managed-cancellation` | `79616ffc-97d8-4234-91e6-f0621a03dae3`（收据读优化）、`f1f30fb6-1206-4ebf-ad38-5e1c55716782`（watchdog 30 s 后）、`96c52cd4-3f88-40a9-991e-4a35d9f20850`（首次通过） | 均 `scenario/session=passed`，13 项 trace，`cleanup_errors=[]` |
| iPhone (`00008110-000C61160C39401E`) | `formal-host-ios-managed-cancellation` | `99c23a6e-f25d-4957-98be-f228ebabd9d6` | `status/scenario/session/phone/fixture=passed`，`cleanup_errors=[]`，fixture `advertising_inactive`+`resources_closed` |

`host_gate=pending` 是设计状态。产物指纹：Android APK
`5afb1ab0457709f36ec94ecfc9b04e7623e3ec0b9d7e9c9e2fe12abbf5756dcd`；iOS App 树
`c10b906bd9a76ca923271abf8214c4e44e6efddad2f8bda25eb19a6658484355`。共享 UI 路径改动后已复跑：
iOS 序列 `scan`/`notification`/`indication`/`io` 全过（`6b163ccc`/`8389f4db`/`90f40129`/`49b8f76c`，
`scene_sequence=passed`），Android 金丝雀 `io` 过（`d6dfbc8a`）。

## 场景语义

```
idle → started → scan:running → scan:advertisement-matched → scan:stopped
     → link:connected → gatt:ready → read:held → read:queued
     → cancel:queued-accepted → read:active-completed → read:recovered → closed
```

Host 才是让顺序成立的一方：fixture 在**固件里**卡住第一条读，第二条才真的是 queued；取消排队那条后
必须恰好出现一个 `cancelled` 终态、且 cancelled 命令不产出值；释放被 hold 的读后它仍要正常完成；最后
同一条 lane 还要接受一次普通读。所有判定只看 App 自己发布的 phase 与它自己的回执。

## 两次真机失败与根因（按发现顺序）

1. **取消码写错平台一侧**（上一轮，App 侧）：App 比对 `'cancelled'`，而 supervisor 发布的是
   `bleng.cancelled`（`format!("bleng.{code}")`）。自己请求的取消被当成故障。离线 fake 用了同一个错
   的码，所以 220 项离线测试全绿却掩盖了它——修 App 的 `CANCELLED_CODE` 并同步 fake。
2. **关键路径撞上 backend 的 native watchdog**（本轮第一次真机）：错误是
   `operation/bleng.operation.timeout: Android GATT I/O operation 6 exceeded the native callback
   watchdog`。用 Appium 日志逐请求累加 + 场景分段计时定位到：从提交被 hold 的读到 `hold.release()`
   的 Appium 往返约 **10–11 s**（`read:held→read:queued` 4.1 s、`read:queued→cancel` 4.7 s、加上提交
   到第一个观察点），正好等于 `AndroidBleBackend` 的 10 000 ms watchdog。

   处置**不是**调大 watchdog：`docs/research/react-native/react-native-host-tooling-architecture.md`
   与 `PLAN.md` 都冻结了 10 s 默认值与「超时即停整条 lane」的语义，`nrf52840-fault-firmware-v2.md`
   也按 20 s firmware hold 容纳它。真正的缺陷在场景：该窗口里塞了三个 `ui.identity()` 检查点，而
   `hold.*` 的每个动作前后都已跑完整 host-authority guard、`ui.click()` 又在 dispatch 前做
   foreground+identity+guard。删掉这三个重复证明后
   `read:held→read:queued` 3.3 s、`read:queued→cancel` 4.2 s、`cancel→read:active-completed` 2.6 s，
   窗口回到 10 s 以内，同一次运行通过。

   **由此固化一条场景预算规则**：在「对端已被卡住 → 释放对端」之间，可花的 host 往返时间是 backend 的
   native watchdog（两端均 10 s、已冻结），不是场景自己的 timeout。

## 本轮同时修掉的效率缺陷

现场诊断一度靠 `sleep N; tail` 轮询日志，单次真机 run 要盲等 10 分钟以上。查清后与「大文件」无关
（该 run 的 `appium.log` 只有 65 KB，60 MB 里 45 MB 是 APK）：真实成本是**轮询量化**——`sleep 300` 是
纯空转，且每次轮询都要重开进程、把整段输出拉进上下文；根因是**生产者全程不报进度**，managed lane 只在
最后 `print` 一次。

处置是把已有但没接线的 `ProgressReporter` 接到 managed lane 上，改成「一次阻塞调用 + 边跑边叙事」：

- `ProgressReporter` 增加 `phase()` 阶段上下文（开始/通过/失败三态，失败带异常类型）与可选守护线程
  心跳（阶段打开期间每 15 s 重复一次），因此再长的准备阶段也不再像卡死。
- 新增 `ReportedObservations`：场景的观察日志本身会报边界，写入时附带
  `since_previous=<n>s`，一个接缝覆盖所有场景，场景代码不需要第二个 progress 参数。
- `run_android_scan` / `run_ios_scan` 接 `progress=` 并给出 `candidate.freeze`、`phone.acquire`、
  `fixture.lease`、`fixture.prepare`、`fixture.start`、`platform.prepare`、`wpa.*`、`app.install-launch`、
  `automation.session`、`scene` 与 `cleanup.*` 阶段；两个 CLI 入口用 `auto_heartbeat=True` 构造并托管
  生命周期。
- `_emit` 只吞 `OSError`/`ValueError`：进度是观测而非证据，输出流坏掉不能改变 run 的判定。

效果：一次 Android run 从「盲等 10 min」变成 2 分钟内跑完、全程可见，且失败点直接落在输出里。

### iOS 通过前一共有三道障碍，第三道是点击本身

iOS 依次遇到：设备侧 UI Automation 授权（`Timed out while enabling automation mode`）、
固件 HELD 20 s（`CBATTErrorDomain: 14`，ARM 的 `hold_max_ms` 被用满）、以及最终的根因——**每次 UI 点击
内要付两道身份证明**。

测量工具是新增的 `BLEHUB_UI_TIMING=1`（只写 stderr，不进证据、不改判定）：每个 UI wait 报
「轮询次数 + 耗时」，每个 `click` 报各阶段耗时。第一次上手就推翻了两个猜测：

- `held read submission` / `queued read submission` 都是 **polls=1、0.6 s**——等待循环不是瓶颈；
- 所有 `observe:*` 都是 `polls=1`——**没有重试风暴**。

真机单次点击 ≈ **9.4 s** 的精确切口（iPhone 17 Pro / iOS 27.2）：

| 阶段 | 耗时 |
|---|---|
| `status`（读 `run-status`） | 0.5 s |
| `reveal`（解析目标控件） | 0.45 s |
| prechecks + final-status（含 **pinned** identity） | 2.1 s |
| dispatch-observe | 0.47 s |
| dispatch | 0.64 s |
| **post-identity（完整档 identity）** | **4.7 s** |

即：点击边界同时付了 1.6 s 的 pinned 档和 4.7 s 的完整档。按 PLAN 的两频率原则，完整档属于观察边界，
点击只需要证明「要派发进去的进程还是我们的」：改后两次点击证明都用 pinned 档（Android 上就是原来的本地
检查，行为不变）；对 peer-held 窗口里的三次点击再用 `verify_after=False` 跳过派发后那次（前派发证明仍
保留，窗口后的观察边界带完整挑战）。

结果：窗口从 ~30 s 降到 **~14 s**（固件 HELD 是 20 s），iOS `cancellation` 通过，且每个 iOS 场景都快了
一截（`io` 段 53 s、`cancellation` 场景 165 s）。代价是共享路径，所以四个已通过场景全部复跑（见上表）。

## 仍未完成

### iOS：场景能跑到 `read:held`，但窗口装不进 10 s

设备授权（Settings → Developer → UI Automation）过了之后，iOS lane 能完整跑：安装/启动、WDA、
automation session、扫描、连接、发现都到 `read:held`，然后失败在
`operation/bleng.operation.timeout: Apple GATT operation watchdog expired: characteristic read`。

真机定量（`cProfile` + Appium 日志，不是推断）：

| 环节 | 实测 |
|---|---|
| 一次 `ui.click()` | **9.4 s**（9 个 Appium 请求 ≈ 1.4 s + identity + reveal/settle） |
| 完整 native identity（6 条 CoreDevice 命令） | 4.45 s |
| pinned identity（1 条命令） | ~0.6 s |
| 整个 scene | 144–177 s，其中 Appium 只有 44–62 s |

peer-held 窗口最少需要 submit#1 → submit#2 → cancel → release 三次点击，即使 identity
全部换成 pinned 档也要 18 s 以上，而当时两端 backend native watchdog 都是 10 s。两端的
失败形态分别为 `operation/bleng.operation.timeout: Android GATT I/O operation N exceeded the
native callback watchdog`（Android，连带退役整条 lane，queue 中的 op 得
`operation_aborted`）与 `operation/bleng.operation.timeout: Apple GATT operation watchdog
expired: characteristic read`（Apple；更早一轮还出现过
`operation/bleng.not_ready.connection_not_active`，即连接已被退役）。

### 裁决：watchdog 对齐 ATT 事务上界（30 s）

选定的是「重审 watchdog 语义」：这个计时器防的是 native 回调丢失，不是对端延迟。ATT 事务允许最长
30 s（Core spec Vol 3 Part F 3.3.3），AOSP 也用同一上界退役 GATT 事务；比平台更短的 app 级计时
不会增加安全，只会把「对端合法地慢」变成「连接被退役」，而 caller 无法区分两者。因此两端默认值
改为 **30 s**（`AndroidBleBackend.DEFAULT_ACTIVE_OPERATION_WATCHDOG_MS`、
`AppleBleBackend.init(gattIoWatchdogMilliseconds:)`），注释里写明了理由；四处文档里「冻结 10 s /
不得调大」的表述同步更新，但保留「场景仍要把窗口做短」的约束（不把 30 s 当成可随意拖到上界的预算）。
firmware v2 的 10 s ARM / 20 s HELD 不变，所以 20 s HELD 现在会先于 backend watchdog 到期。

新默认值下的 Android 复验：APK `5afb1ab0…`（需要先 `ffi android-backend-publish` 重发 AAR，否则
RN consumer 仍然链旧字节）run `f1f30fb6`，13 项 trace、`scenario/session=passed`、
`cleanup_errors=[]`，scene 68 s，窗口仍为 3.1 s / 3.7 s / 2.4 s 三段。

iOS 尚未拿到同结论：Apple backend 改动后 RN 设备包必须重建，而本轮重建卡在 `codesign`
（login 钥匙串授权，human-only；`pixi run just apple unlock-login-keychain` 是既有入口），
所以 iOS 的 `cancellation` 用的是旧 10 s 字节。预期的窗口为 ~21 s（三次点击 × 9.4 s + 等待），
新上界下应有余量，但需真机复跑后才能记成候选事实。

本轮已落地的 iOS 修复（都已离线回归，且 R9/R10 真机验证到 `read:held`）：

- **identity 分两档**：`read_app_identity(pinned_only=True)` 用一条 inventory + 已钉 receipt 代替
  六条命令，`ui.click()` 走 `require_click_identity`，观察边界仍走完整档。
- **guard 分两频率**：`ScanAutomationOwner.require_attached` 只查本地所有权（仍含自有子进程存活），
  设备侧 `/status` 属于检查点；PLAN 本来就要求这样，之前是逐边界都在跑完整快照。
- **`--release-before-install`**：本次 run 可以终止自己 bundle 的运行副本（按精确 PID + 安装目录归属，
  落回执并复验），否则上一次尝试留下的副本会让之后 every run 都拒接。
- **`_wait_gatt_reads` 失败必须点名**：同时报 `gattRead` 与该 read 的 App 错误，并在 read 已失败时
  立即失败——下一轮才看得出是“被退役”还是“投影错”。

### 其他

- `read:recovered` 段已从 52.6 s 降到 **19.5 s**（scene 104 s → 71 s）：原先 7 次 `_receipts()` 每次
  都要「Evidence → 读 trace → Current run」两次导航，现改为每个收据边界只读**一次** trace、在 host 里
  断言全部 action/result 对。新增回归 `test_both_receipt_boundaries_read_the_evidence_trace_once`
  按导航次数钉住它（回退会变成 7 次）。
- A 类剩下的三项 fault reject（`rf-write-reject`、`rf-ccc-enable-reject`、`rf-ccc-disable-reject`）。
- 已知命名瑕疵（未改）：scene 的 `scope` 字符串跨平台共用，于是 iOS bundle 也写
  `formal-android-managed-*`；bundle 自己的 `platform` 字段可区分，因此没有歧义，但重命名会动已发布
  证据的字符串，留到下一次 bundle schema 变更时一并处理。
- 离线回归：`tools/tests/react_native` 全目录 **2167 passed / 21 skipped / 0 failed**。
- 离线回归：`tools/tests/react_native` 全目录 **2163 passed / 21 skipped / 0 failed**（本轮改动后重跑）。
