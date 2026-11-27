# Android Appium HANS OFF 根因诊断：活动 session 后 UiA2/ADB service stall 复现

日期：2026-11-10。接续[HANS full lifecycle candidate](2026-11-09-formal-android-appium-hans-full-lifecycle.md)与[Appium 对照和恢复](2026-11-09-formal-android-appium-comparison-recovery.md)。本轮是用户明确授权的一次 HANS OFF 自动化根因诊断；不执行 RF、scan、firmware、DUT BLE permission/radio mutation，也不启用 freezer/power/HANS exemption。

## 1. 结论

**HANS OFF 条件下，原始活动 session stall 再现。** 本次 fresh candidate 完成 `idle → started`，随后 UI 请求和独立 UiA2/ADB 健康探针均失去响应；没有把该 run 重试或升级为 pass。

| 状态 | 结果 |
| --- | --- |
| `scenario_status` | `failed` |
| `session_status` | `failed` |
| `phone_status` | `quarantined` |
| `status` | `failed` |
| `host_gate_status` | `failed` |
| `rf` | `not_run` |
| cleanup | candidate 内未闭合；随后独立 recovery 已通过 |

表格保留 candidate 当时的 `phone_status=quarantined` 事实；独立 recovery 只改善当前物理清理状态，不改写该 candidate 的 failed verdict。

这次结果把问题进一步收窄到：**已建立 UiA2 活动 session 的后段，UiA2 server 不再响应，且 ADB shell service acceptance 同时无法完成。** 它仍不能单独证明 OEM freezer 是唯一因果，也不能把问题归因到 RN/Rust/Close。

## 2. Candidate 身份和执行边界

| 项目 | 实际观察 |
| --- | --- |
| run / scope | **`763dfcd3-f847-4a70-a25f-78c278a9e881` / `formal-android-managed-lifecycle`** |
| bundle | `.work/formal-android-root-cause-off/d7e2c0a5-5964-4f3b-bb6f-7b8dcad3f21e/763dfcd3-f847-4a70-a25f-78c278a9e881/` |
| device | `3ac5cc09` |
| APK | `c96fc9909ed056e33e1ce47927639da742614294a60114d8ae546ee49255a584` |
| App identity | PID **13479** / UID **10378** / start ticks **254928535** |
| UiA2 identity | PID **14305** / UID **10255** / start ticks **254929370** |
| boot | `35df0f9f-6c54-42b0-800c-3c410469b6e7` |
| helper | Appium 3.9.0 / UiAutomator2 driver 8.7.3 / server 10.7.6；三份 helper SHA256 与 reviewed candidate 一致 |
| launch flags | `--authorize-install --authorize-helper-settings --use-root --close-diagnostics`；无 `--authorize-server-hans-exemption`、无 freezer/power exemption |
| fixed budgets | total 180s / phase 20s / cleanup 90s；失败后没有延长或重试 |
| startup snapshot | `poweredOn` / scan `denied` / connection `denied` |
| sealed manifest | 254 files；manifest SHA256 `591305ffa03b921b71612038cbcf458fea733e7340b5eaea3883f287dfc24acd` |
| sealed result | result SHA256 `e90fcd19b38d0c6c9b9413a79e2a1e23bcaefb4dc9e40691372ccd422dca5e10` |

本次前置动作先对上一成功页面 PID **10977** / UID **10378** / start ticks **252346347** 做了独立 exact-identity retirement，结果 `status=passed`、errors 为空；证据目录为 `.work/appium-root-cause-retirement-b7f2f2c4-76b6-44d4-8c9e-6f5539ef3dc6/`。共享 ADB 5037 PID **16841** 未重启，5038/8237/8937 在新 candidate 启动前无 listener。

## 3. 失败时间线和分层证据

- Appium session 创建、安装、启动和初始 idle 通过。
- Start click 完成，`observations` 已记录 `idle`、`started`。
- Started 后的 UI 请求在 20 秒预算内没有返回；primary 为 `urllib3.exceptions.ReadTimeoutError`，不是 WebDriver `socket hang up` 的表面错误。
- cleanup 前 fresh downstream `http://127.0.0.1:8937/status` 以 2 秒探针 timeout。
- 独立 ADB transport probe 使用 3 秒预算：
  - `server_version`：返回 `OKAY00040029`；
  - cached device：返回 `OKAY0006device`；
  - transport select：返回 `OKAY`；
  - `service_accept`：request 已发送，等待 service response timeout。
- `platform/server-status.json` 保留 UiA2 PID **14305** 的 exact identity，但 `/status` 无 response。
- `platform/adb-transport.json` 保留 `service_accept` timeout；这不是 ADB daemon version drift 证明，也不是设备已断开的证明。
- close diagnostics 的 live logcat collector 在故障窗口启动并收到 bounded host-arrival bytes，但后续按 bound PID 采集 logcat、failure UI 和 Close tag restore 均被同一 ADB stall 阻断。
- Appium session DELETE 无法完成；本地 Appium server 被安全流程终止，但不能据此声称远端 UiA2/App native resource-zero。

这次将边界从“某一条截图/`displayed` 查询的 hangup”推进为：**UiA2 active session proxy、UiA2 `/status`、ADB `service_accept` 在同一故障窗口分别失效**。它比旧 candidate 更接近 transport/process-policy 根因，但仍没有故障瞬间的原子 kernel freezer 采样或 ROM 因果证明。

## 4. Cleanup 和 quarantine

本次 bundle 保留真实失败，不重封、不升级 verdict：

- `automation/close.json`：`session_closed=false`、`server_closed=false`、`status=failed`；
- candidate finalization 未完成；
- helper restore 未完成，`helpers/restore-failed.json` 标记 `status=quarantined`，原 AppOps 为 `deny`，本次准备曾尝试 `allow`，store 曾创建；
- candidate finalization 时 phone lease 未释放，保留 quarantine lock；
- 随后在 ADB shell service 恢复后完成独立 recovery：原 App/UiA2/test identities fresh readback 后各自 exact force-stop，删除本次 `8937 → 6790` forward，`io.appium.settings` AppOps 恢复为 `deny`，严格 pin 的 mock store 删除，Close tag 恢复为空；App/helper/test/forward/store 均 readback absent，phone lease 已释放；
- recovery 证据：`.work/formal-android-root-cause-off/recovery-20f1b0ec-17f7-465f-8be7-35d27955c2a8/recovery-final.json`，`status=passed`、errors 为空；
- cleanup 过程没有杀共享 ADB PID `16841`，没有重启 daemon 或改变 radio；
- 任何 force-stop/进程消失事实都不被解释为 native resource-zero。

## 5. 当前判断和下一步

当前最强证据链为：

```text
HANS OFF
  → Appium session/start 成功
  → Started 后 UI request timeout
  → UiA2 /status timeout
  → ADB protocol version/cache/select 成功
  → ADB service_accept timeout
  → DELETE/helper restore 无法闭合
```

因此：

- 这不是一次 RN/Rust 生产 Close bug 的证明；
- 不应通过增加 timeout、自动 retry、换点击 API 或修改 App 伪造通过；
- HANS ON 的 full lifecycle pass 仍是 ROM-specific workaround，不是根治；
- 当前仍没有证据允许把 HANS 变成 Android 通用默认策略；
- 该 candidate 的 recovery 已完成，当前 phone lease 为空；在没有新的明确授权前不执行下一次手机 candidate，也不进入 RF。

本报告记录的是一次 failed/quarantined root-cause candidate；独立 recovery 通过不升级 Appium、host gate、RF、native resource-zero、ABI 或 release verdict。
