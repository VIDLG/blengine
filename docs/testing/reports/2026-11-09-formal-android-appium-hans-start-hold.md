# Android Appium HANS start-hold 对照：单 UID 规避后通过

日期：2026-11-09。接续[start-hold 无 HANS 的 Start 前失败](2026-11-09-formal-android-appium-start-hold.md)与[同 APK transport-only pass](2026-11-09-formal-android-appium-transport.md)。本轮只增加已审计 ROM 的单 UID HANS exemption；不启用 deviceidle/freezer/power exemption，不改 Appium settings、DUT BLE permission/radio、firmware 或生产代码。

## 1. 结论

**HANS 条件下的独立 start-hold control 通过。** 同一 reviewed APK、同一 managed Appium/UiAutomator2 helper hashes、同一 serial/ports 和同一 start-hold scope 完成：

```text
idle → started → Current run 静置约 5 秒 → closed
```

- `scenario_status=passed`
- `session_status=passed`
- `phone_status=passed`
- `status=passed`
- `host_gate_status=pending`
- `rf=not_run`
- `cleanup_errors=[]`
- `phone_cleanup_errors=[]`

这说明在本机审计 ROM 上，单 UID HANS 分类规避能让这一次 **Start → hold → Close** 完成；它**不证明**完整 Tests/Evidence navigation pass、不证明旧 `Evidence displayed → socket hang up` 已修复、不证明 HANS 是所有历史 timeout 的唯一原因，也不构成 RF/native resource-zero/ABI/release gate。

## 2. 执行与身份

先完成只读 preflight：无 phone lease、5037 仍 PID **16841**、5038/8237/8937 无 listener、旧 App/helper/instrumentation/forward 缺席、boot 相同、AppOp deny、HANS debug 为空。日志 `.work/logs/appium-hans-start-hold-preflight.log`。

随后通过维护入口执行一次新 attempt：

| 项目 | 实际观察 |
| --- | --- |
| run / scope | **`656f6d15-8182-4466-a88f-3a5c98c0980a` / `formal-android-managed-start-hold-control`** |
| bundle | `.work/formal-android-appium-hans-start-hold/656f6d15-8182-4466-a88f-3a5c98c0980a/` |
| APK | `c96fc9909ed056e33e1ce47927639da742614294a60114d8ae546ee49255a584` |
| helper | Appium 3.9.0 / UiA2 driver 8.7.3 / server 10.7.6；三个 helper hashes 与前两个候选一致 |
| ROM | `/system/framework/oplus-services.jar` SHA256 `889bdf044f565d94a4fb603402a0f7f3d394750f2d151c29eaa5122a25b09a24` |
| App | PID **24421** / UID **10378** / start ticks **252092340**；boot `35df0f9f-6c54-42b0-800c-3c410469b6e7` |
| UiA2 server | PID **25108** / UID **10255** / start ticks **252093155**；same installed path/hash/boot |
| HANS selector | host session `636fdc11-44c3-453f-abec-00ba606d52e5`、device session `e1e194f8-cf92-49d8-801b-ffd9cc029782`、system port 8937；apply 只绑定该 fresh selector |
| startup snapshot | `state=poweredOn`、scan authorization `denied`、connection authorization `denied` |
| output | `.work/logs/appium-hans-start-hold-device.log`；维护 runner exit 0 |

命令显式包含 `--authorize-server-hans-exemption`，但没有 `--authorize-server-freezer-exemption`、`--authorize-server-power-exemption` 或 device-local capture。总预算 `180s`、phase `20s`、cleanup `90s`，未在失败后延长或重试。

## 3. HANS 事务事实

`platform/server-hans-*.json` 保留完整 command/intent/result receipts；离线 verifier 实际通过。摘要：

- baseline classification：UiA2 server UID 10255 / package `io.appium.uiautomator2.server` 为 **THIRD**；其他 UID map 保留。
- apply：审计 ROM/hash、installed helper path/hash、boot、完整 package/UID inventory 与原 fresh selector 全部匹配；目标从 **THIRD → WHITE**。
- runtime：在 HANS WHITE 下完成 `Start → Current run`、5 秒 hold 和 `Close`；没有 Tests/Evidence navigation。
- restore：目标从 **WHITE → THIRD**；`debug_restored=true`；`other_classifications_changed=false`。
- Close/App/helper/phone cleanup：均 passed；HANS restore 在 session finalization 之前独立完成；lease 已释放。
- `server-hans-intent.json` 的初始 `status=failed` 是一次性 mutation intent 的未完成初始记录形状，不是最终 verdict；对应 `server-hans-result.json` 与 `server-hans-restore-result.json` 为成功 readback，整体 bundle verifier 通过。不能删除 intent 或用重封改变该 receipt 历史。

HANS 只恢复 classification 语义，不声称恢复原始瞬态 map/timestamp；不读取或改变 BLE radio。HANS 命令顺序与其他分类不变由现有 evidence verifier 检查，不由本报告文字推断。

## 4. 与无 HANS start-hold 的对照

| Candidate | 结果 | 首要边界 |
| --- | --- | --- |
| 无 HANS：`77621426-a1d6-4928-9e61-10e7373d2768` | failed/quarantined，后独立恢复 | Start 前标题查询 timeout；fresh status 和 ADB service_accept 也超时；后失败 server 快照 29 个 wait channels 为 `do_freezer_trap` |
| HANS：`656f6d15-8182-4466-a88f-3a5c98c0980a` | start-hold passed | 同一 scope 在 HANS WHITE 条件下完成 Start/hold/Close；无 navigation/Evidence 证据 |

这是有区分度的候选差异，但不是严格单变量证明：两次是不同 session/进程生命周期，HANS 本身会改变 OEM process policy；不能把它写成“所有 Appium 断连由 HANS 修复”，也不能把 HANS exemption 变成默认策略。旧 Appium full lifecycle failed、Maestro lifecycle passed、transport-only passed 的 verdict 全部保留。

## 5. 当前设备和后续边界

成功页面按默认规则保留。当前 retained App：PID **24421** / UID **10378** / start ticks **252092340**。普通 run 不得接管；后续如需设备 mutation，必须先在独立授权下 fresh exact-identity retirement，不能重复退休此前的 PID 23923 或 Maestro PID 16829。

本轮软件/证据回归沿用前述 start-hold 26 tests、Appium/owner/evidence/candidate suites；正式 App 57 Jest、typecheck、fmt、lint、Mermaid、Markdown/link checks 均已通过。完整 Appium lifecycle、scan/RF、长时稳定性、resource-zero、ABI 和 Phase exit 仍 pending。
