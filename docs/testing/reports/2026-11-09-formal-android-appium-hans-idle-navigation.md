# Android Appium HANS idle navigation：同 APK 导航控制通过

日期：2026-11-09。接续 [HANS start-hold pass](2026-11-09-formal-android-appium-hans-start-hold.md)，本轮只增加已有的 idle navigation control，不执行 Start、Close、scan 或 RF。

## 1. 结果

同一 reviewed APK、同一已审计 ROM/UiA2 helper、同一 Appium home/ports，在 HANS 单 UID exemption 下完成：

```text
Tests/idle → Evidence/idle → Current run/idle
→ Tests/idle → Evidence/idle → Current run/idle
→ final Evidence/idle
```

结果：

- run：**`6b10d9f3-e77c-4375-8f55-4b76a6e3b8ac`**
- scope：`formal-android-managed-idle-control`
- APK：`c96fc9909ed056e33e1ce47927639da742614294a60114d8ae546ee49255a584`
- `observations=["idle"]`
- `status=passed`
- `phone_status=passed`
- `host_gate_status=pending`
- `rf=not_run`
- cleanup errors 为空，phone lease 已释放
- HANS apply/restore：通过；`other_classifications_changed=false`；`debug_restored=true`

Bundle：`.work/formal-android-appium-hans-idle/6b10d9f3-e77c-4375-8f55-4b76a6e3b8ac/`。维护入口日志：`.work/logs/appium-hans-idle-device.log`。独立 offline replay 使用既有 `formal-host-lifecycle-verify`，不访问设备。

## 2. 诊断意义

目前同 APK 的受控对照为：

| 条件 | 控制 | 结果 |
| --- | --- | --- |
| 无 HANS | transport-only | 4 个固定只读查询通过 |
| 无 HANS | start-hold | Start 前首次标题查询阻塞；post-failure UiA2 线程快照为 `do_freezer_trap` |
| HANS | start-hold | `idle → started → 5s hold → closed` 通过 |
| HANS | idle navigation | Tests/Evidence/Current run 往返导航通过 |

这支持一个**受范围限制的工程判断**：本机审计 ROM 的 HANS 分类是当前 UiA2 automation stall 的高价值候选规避因素；它同时覆盖了 Start/hold 和 idle navigation 两类控制。

仍不能宣称：

- HANS 是所有历史 `socket hang up` / `service_accept timeout` 的唯一根因；
- 完整 Start → Tests/Evidence → Close → final Evidence lifecycle 已通过；
- HANS 规避应成为所有 Android 的默认策略；
- native Close/resource-zero、scan/RF、ABI 或 release gate 已通过。

旧 Appium full lifecycle failed、Maestro lifecycle passed、transport pass、无 HANS start-hold failed 均保持原始 scope/verdict，未合并或重封。

## 3. 具体 HANS 事实

- baseline：UiA2 server UID 10255 / package `io.appium.uiautomator2.server` 为 THIRD；目标 apply 后为 WHITE；restore 后为 THIRD。
- ROM SHA256：`889bdf044f565d94a4fb603402a0f7f3d394750f2d151c29eaa5122a25b09a24`。
- Appium server：PID **31470** / UID **10255** / start ticks **252190476**。
- retained App：PID **30788** / UID **10378** / start ticks **252189644**；boot `35df0f9f-6c54-42b0-800c-3c410469b6e7`。
- startup state：`poweredOn`，scan/connection authorization 均 `denied`。
- HANS `original_debug_value` 为空，已恢复；其他 UID 分类未改变。
- 5037 仍是共享 ADB PID **16841**；5038/8237/8937 无 listener；lease 已释放。

当前成功 App 按规则保留，普通 run 不得接管。后续如果要执行完整 lifecycle，必须新授权、先精确退休 PID `30788`，并保留 HANS 是环境变量这一事实；不能把这次 idle pass 当成完整 lifecycle pass。

## 4. 变更与验证范围

本轮没有修改生产 BLE/Rust、Appium settings、DUT BLE permission/radio、firmware 或 HANS policy 实现。使用已有 start-hold/idle-control 与 HANS evidence verifier；软件回归和最终文档检查此前已通过。新的设备证据只增加本候选 bundle 与本报告，不污染旧 sealed bundles。
