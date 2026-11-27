# HANS 重新界定：被卡住的进程已经不存在了

日期：2026-11-26
设备：OnePlus PHB110 `3ac5cc09`（rooted，API 36 / ColorOS，HANS 服务在跑）
范围：把"**HANS OFF active-session UiA2/ADB stall 未解决**"这条基线重新核对到今天的 lane 上。
状态：不是"修好了"，而是**依赖被删掉了 + 剩下的风险由产品面（前台服务）覆盖**，并且留下了
以后真出问题时的有界诊断路径。

## 1. 当时到底卡的是谁

[2026-11-09 的对照报告](2026-11-09-formal-android-appium-hans-start-hold.md)把主体写得很明确：
被分类成 **THIRD** 并被冻结的是 **Appium 的 UiA2 server**（`io.appium.uiautomator2.server`，
UID 10255），失败后它的 server 快照有 29 个 wait channel 停在 `do_freezer_trap`；把它临时提升到
**WHITE** 后，同 scope 的 `Start → hold → Close` 一次通过。那次提升是**单次 run 的单 UID 临时
豁免**，明确"不在普通 preflight 中永久加白、不全局关闭 HANS"。

## 2. 今天的 lane 里，那个进程已经不在了

2026-11-23 把 Appium（driver、toolchain、helper APK、HANS 策略与全部 recipe）整体删除。现在的
managed 场景驱动是 **App 自己的 instrumentation**（`BlengSceneDriverInstrumentedTest`），
没有任何 helper UID：

```python
# tools/react_native/bleng_scan_evidence.py
# An in-process instrumentation run has no Appium server, no helper APKs and no HANS
# exemption, so its request states the driver and the exact test APK instead of all of them.
```

证据层更进一步：`_require(not any(name.startswith("platform/server-hans-") ...))` —— bundle 里
**出现** HANS receipt 就会被判失败。也就是说，就算有人想给这条 lane 加白，也会被证据层拒绝。
原故障的三要素（独立 helper 进程 / 被分类 THIRD / 冻结后无人应答）在今天的 lane 里一个都不成立。

## 3. 剩下的唯一风险面，由产品面覆盖

现在的场景只需要**一个**进程活着：App/测试进程。它只在 `background` 场景里被主动送走过一个
声明窗口。两条事实叠起来：

- **产品面**：App 在 `AndroidManifest.xml` 声明 `FOREGROUND_SERVICE_CONNECTED_DEVICE`，并有一个
  空的 `BleHubKeepAliveService`（`foregroundServiceType="connectedDevice"`），**Activity 失焦时
  启动、回到前台时停止**，目的是"不给 OS 回收这个进程的机会"，明确不拥有任何 BLE 状态。
- **平台面**：本机 `dumpsys activity settings` 的 `freezer_cutoff_adj=900` —— AOSP 的 cached-app
  freezer 只考虑 `adj ≥ 900` 的**缓存**进程；带前台服务的进程不会落到那一档。

## 4. 实测（HANS 开启状态下）

先读只读事实（全程没有改任何 ROM/系统设置）：

```text
init.svc.hans                  = running
persist.vendor.enable.hans     = true
sys.hans.enable                = true
freezer_cutoff_adj             = 900
```

然后跑 `background`（对 `notify:subscribed → notify:backgrounded → notify:resumed →
notify:value-after-resume` 全链，55 s），同时从 host 每 7 s 采一次前台服务状态：

| 时刻 | 前台服务 | App 是否前台 |
|---|---|---|
| t+7 s | 无 | 否（正在安装/启动） |
| t+14 s | 无 | **是** |
| t+21 s | 无 | **是** |
| t+28 s | **有** | 否 |
| t+35 s | **有** | 否 |
| t+42 s | **有** | 否 |
| t+49 s | **有** | 否 |
| t+56 s | 无 | **是**（已回来） |

也就是说：**被送走的那段窗口里，前台服务确实在**；回到前台后它自己停了（没有常驻通知）。
场景：`.work/android-native-background/2026-11-26-hans-a`，`status=passed`、`integrity=passed`、
`cleanup_errors=[]`。同一天 `background` 在 20 条序列里也通过过一次。

## 5. 不能做什么（以及为什么）

- **不读 THIRD/WHITE**：本机 `dumpsys activity processes` 不输出冻结状态
  （`dumpsys activity processes -a` 里也没有 `freeze` 字段），分类 dump 需要临时 debug-property
  scope —— 那是一次 ROM 变更，`react-native-host-tooling-architecture.md` 早就写明"实际
  THIRD/WHITE 确认必须留到绑定的显式准备事务"，而不是只读 preflight。
- **不加白、不全局开关 HANS**：原策略就是单 run 单 UID 临时豁免；现在连要豁免的 UID 都不存在。
- **不把这条写成"已修复"**：真正的结论是"**依赖已删除**"。

## 6. 以后真出现"App 被冻住"时的有界诊断

顺序固定，每一步都是读数而不是猜测：

1. `getprop init.svc.hans` / `persist.vendor.enable.hans` / `sys.hans.enable` —— HANS 是否在跑。
2. `dumpsys activity services <app-id> | grep isForeground` —— 被送走的那段窗口里前台服务在不在。
   不在就是**产品缺陷**（keep-alive 没起来），与 HANS 无关。
3. 场景的 trace：`notify:backgrounded` 之后是否还有 `notify:resumed`。没有 resumed 是驱动/系统
   层面的问题；有 resumed 但值没到，才是"进程被挂住"的证据。
4. 只有在 1–3 都指向"进程被挂住"时，才申请一次**显式授权**的 debug-property scope 去读分类并
   按原策略做单 UID 临时豁免；绝不把豁免变成默认，也不全局关闭 HANS。

## 7. 基线措辞变更

- 旧：`The HANS OFF active-session UiA2/ADB stall remains unresolved.`
- 新：HANS 在本机**是开启的**，原始 stall 的主体（Appium UiA2 helper UID）已随 Appium lane 删除，
  证据层拒绝 HANS receipt；今天的 lane 只依赖一个带 `connectedDevice` 前台服务的进程，该服务在
  被送走的窗口里实测在运行，`background` 在该状态下通过。若将来观察到 App 被挂住，按 §6 的顺序
  诊断，不回到"给 helper 加白"的老办法。
