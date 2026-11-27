# Android Appium start-hold：Start 前超时、后失败 freezer 证据与原 lease 恢复

日期：2026-11-09。接续[同 APK transport-only passed](2026-11-09-formal-android-appium-transport.md)，复用已有 start-hold control，不新增场景或自动化框架，不修改生产 BLE/Rust、timeout/settings 或默认 driver。

## 1. 当前结论

- **唯一新 attempt 失败于 Start 之前的首个标题定位。** `observations=[]`、`startup_snapshot={}`、`hold_observation={}`；没有 Start、Close 或导航 click，不能把它算作已执行 start-hold/lifecycle。
- fresh UiA2 `/status` 超时；已有 ADB daemon 的 version/cached-state/transport-select 成功，但 `service_accept` 阶段超时。主错误是客户端 **20s read timeout**，与原 lifecycle 的 **HTTP 500 / socket hang up** 分别保留，不合并错误类型。
- 本地 Appium 已退出后，在独立恢复前 fresh 核对原 App/server 的完整身份，采集到原 server **29 个 thread wait-channel 读数全部为 `do_freezer_trap`**。这是直接的**后失败、post-local-reap 快照**，不是故障瞬间的原子采样；不能倒推冻结开始时刻或宣称全部 ADB stalls 都只有该原因。
- 原 attempt 保持 **failed / phone quarantined / host-gate failed / rf not_run**。原 lease 的独立恢复随后 **`recovered_failed` / `phone_status=passed`**、errors 为空、lease 已释放；不升级原 session DELETE 或原 gate。
- 没有自动重跑、启用 HANS/freezer/power exemption、执行 RF、修改 DUT BLE 权限/radio、刷 firmware、重启共享 ADB 或关闭其他客户端。目前没有 retained App。

## 2. 为什么选择已有 start-hold

先离线对照旧 failed `353bf795…` 和 passed transport `a4885be7…`：

- 旧 run 的 Start click 返回 200 / 138ms；其后首个失败之前仍有 **86 个成功 HTTP 请求**。Start 后的 `workbench-evidence` click 返回 200 / 108ms，之后 `session-state`、标题及 `workbench-page` find 均成功；首个失败是该 page marker 的 `displayed` 查询，HTTP 500 / `socket hang up` / 11263ms。不能称为 click 失败，也不能称目标页面已完整验证。
- transport 只有 **8 个 HTTP 请求**且全部 200；保留的 source 是 Tests/idle，无 Start 或导航。
- 三次 session 请求 capabilities JSON（再含本次新 run）完全相等，APK/helper identity 对应同一 reviewed candidate。**相同 capabilities、相同 settings response 长度，不证明 settings 值或整个运行环境相等。** 前两份 info 日志无逐行时间戳，也不能把 11263ms 当成“Start 后 11.263 秒”。

因此选择 [`exercise_lifecycle(..., browse=False)`](../../../tools/react_native/run_bleng_ui.py)：Start 一次 → 读取一个 startup snapshot → Current run 上静置五秒 → 原页 Close；不浏览 Tests/Evidence。该控制有独立 scope/record 和已存在的 strict verifier，不复制第二套 UI driver。

诊断结果没有走到 Start，故不能用它验证“Start 后不导航”假设；它仅新增了**不需要已执行 Start/导航，也可出现 automation stall**的候选事实。新 run 明确启用既有 Close diagnostics（本地 debug/timestamp/live logcat、固定 tag），与旧无诊断 run 不是完全单变量环境；不据此宣称导航、logging、USB、OEM 或 Appium 中任一项为唯一根因。

离线三候选摘要：`.work/logs/appium-start-hold-offline-diagnosis.{log,json}`。各 manifest-listed files 的 size/hash 均仍匹配：旧 failed **238**、transport **408**、本次 **254**；不把旧 failed 目录的已知 strict-layout failure 洗成通过。

## 3. 前驱 App 精确退休

先完成 start-hold targeted regression 与前驱 sealed bundle 只读复验，再在独立 lease 下 fresh 读取前驱 retained identity：

- predecessor：`a4885be7-627e-4f03-b2ba-7d85486a06d0`。
- 原 App：`com.blehubblenghost`，PID **23923** / UID **10378** / start ticks **251037215**，原 boot/APK/hash 全匹配后，仅一次 user-0 package force-stop。
- retirement：`.work/retire-transport-before-start-hold-7580a4fe-2a86-4499-b6db-5a385bf48753/`。
- `status=passed`、整包/UID 进程缺席、BLE permissions 未变、errors 为空、lease 已释放；日志 `.work/logs/appium-start-hold-retirement.log`。

不是普通 runner 接管旧进程，不应再按 PID 23923 或更早 Maestro PID 16829 退休。原 transport result/manifest 不改写。

## 4. 唯一新 attempt 与准确失败点

| 项目 | 实际观察 |
| --- | --- |
| run / scope | **`77621426-a1d6-4928-9e61-10e7373d2768` / `formal-android-managed-start-hold-control`** |
| bundle | `.work/formal-android-appium-start-hold/77621426-a1d6-4928-9e61-10e7373d2768/` |
| candidate | APK **`c96fc9909ed056e33e1ce47927639da742614294a60114d8ae546ee49255a584`**，未 rebuild；现有 Appium **3.9.0** / UiA2 **8.7.3** / server **10.7.6**，精确三个 helper hashes 不变 |
| App | PID **455** / UID **10378** / start ticks **251264495**；boot `35df0f9f-6c54-42b0-800c-3c410469b6e7` |
| UiA2 server | PID **2133** / UID **10255** / start ticks **251265312**，原 installed path/hash/boot 一致 |
| session | host `da965590-636d-484a-ad65-aa04c82d6543`；device `41ef211a-13fe-45a5-84c4-d7f56f0aaa95`；保持独立身份 |
| primary | `urllib3.exceptions.ReadTimeoutError: ... Read timed out. (read timeout=20.0)` |
| progress | observations、snapshot、hold 均为空；没有 Start/Close/导航 click |
| verdict | `scenario_status=failed`、`session_status=failed`、`phone_status=quarantined`、`status=failed`、`host_gate_status=failed`、`rf=not_run` |

维护入口为 `formal-host-android-managed-lifecycle`，显式选择 `--automation-start-hold-control --close-diagnostics`，同一 serial/App/activity/APK/helper hashes、现有 managed home/Node、ports **8237/8937**，`--timeout 180 --phase-timeout 20 --cleanup-seconds 90 --authorize-install --authorize-helper-settings --use-root`。这些是在执行前固定的既有 diagnostic budgets，不是在失败后延长重跑。没有 exemption 或 device-local PCAP flag。

完整输出 `.work/logs/appium-start-hold-device.log`，由 `run_quiet --no-tty --progress-seconds 20` 留存 progress；命令正常 **exit 2**，不是 outer timeout 杀掉。

同一份 timestamped Appium host log 内的链路：

| host-log 时间 | 请求/结果 |
| --- | --- |
| 10:36:41.517 | session 创建 200 / 1716ms |
| 10:36:43.682 | bind 的 `get_settings` 200 / 9ms |
| 10:36:48.149 | `mobile: getCurrentPackage` 200 / 57ms |
| 10:36:48.178 | 首次 `formal-host-title` find 提交；无成功响应 |
| 10:37:08.181 | 客户端断开，HTTP response 无有效状态码；primary 为 20s read timeout |
| 10:37:23.325 | DELETE 提交 |
| 10:37:28.328 | DELETE 客户端 5s read timeout；随后 local Appium TERM，最终需 SIGKILL/reap |

本次 **6 个 HTTP 请求**，四个先行请求为 200，title find 与 DELETE 无有效成功响应。不要把 cleanup 阶段 instrumentation client 的 SIGTERM / driver 的通用 “probably crashed” 文字当成 device server 已退出的证明；后续 fresh OS identity 仍发现原 PID 2133 存活。

## 5. 新的诊断证据与限制

- `platform/server-status.json`：直接 fresh downstream `/status` 两秒预算超时，没有 status response。
- `platform/adb-transport.json`：`server_version=0029`、cached `device`、exact transport select 均成功；`service_accept` 的请求已经 send，之后等待 `OKAY` 超时。没有进入 boot-ID output 阶段。这不是 ADB daemon 版本漂移归因，也不是设备掉线证明。
- 因 fresh `/status` 失败，既有安全边界拒绝排入额外 PNG/XML UI 诊断，`failure-ui-failed.json` 保存空 reads/rawfiles 和真实原因；没有用 blind retry/dismissal 伪造现场。
- owned live logcat 正常停止/reap，raw 只有 **156 bytes**，内容是一个 ActivityManager unbind warning。它不证明该 warning 导致 stall，也不证明没有其他 crash/kill。没有完整 failure-window instrumentation/exit-cause 证据。
- 原 cleanup 的 server/App logcat reads、DELETE、candidate finalize 和 Close tag restore 均有失败回执，不能因后续 shell 恢复可读而删掉。

随后原 lease 下的只读 readiness 成功：boot 一致，原 App/helper 仍在，原 forward 仍在；证据 `.work/logs/appium-start-hold-readiness.{log,json}`。独立恢复前再 fresh 验证完整 App/server identity，并以 root **只读**采集这两个确切 PID 的 stat/status/wchan 与 server task wchan。原 server 的 29 个采样 wait channels 均为 `do_freezer_trap`，主进程状态仍为 sleeping；原 App main wchan 为 `__arm64_sys_epoll_pwait`。

该快照保存在 recovery result 的 `post_failure_proc_snapshot`，采样发生于 **local Appium reap 之后、device force-stop 之前**。这支持本次原 server 的 post-failure freezer 状态，且与[历史本机 freezer/HANS 证据](2026-11-07-formal-android-idle-freezer.md)相容；不证明冻结开始于哪个请求，不单独证明 OEM 分类决策，也不把与之同窗的 ADB service-accept stall 当作已完成因果归因。既有 [reviewed-ROM HANS workaround](2026-11-07-formal-android-hans-lifecycle.md) 仍默认关闭，本轮未重复 apply。

## 6. 独立恢复，不污染 sealed bundle

恢复目录：`.work/appium-start-hold-recovery-b1f34578-4d7e-4dee-b1c6-c46843c3b0a9/`；日志 `.work/logs/appium-start-hold-recovery.log`。这是原 run / 原 owner bytes / 原 lock inode 下的独立恢复，不是新场景，也不把旧 owner journal 改成 passed。

- 先证明原 host PID 与 owned local live-capture PID 缺席、源 result/manifest/关键 receipts 未变、shared daemon 仍 PID **16841**；SDK/command receipts 共 **107** 条。
- fresh 确认 exact App/server PID/UID/start/APK/boot 后各仅一次 user-0 force-stop，拒绝未知 worker/UID/其他 automation owner；不按 PID 消失推断 native close/resource-zero。
- 只删除原 `3ac5cc09 tcp:8937 → tcp:6790` forward，不 reset 全局 forwards。
- 原 store pin **`65099:20477:8180:1:23:2000:2000`** 与成功创建回执和 live 全部 bytes 严格一致；删除前再次拒绝 symlink/nonregular、比较 stat 与 `cmp` 内容，之后确认 absent。没有采用“内容相同但未 pin”的文件。
- 仅将可归因的 Settings package-scope mock-location `allow → deny`；仅恢复原本 absent 的三个 hidden-API keys；仅把本次设置的 Close tag `DEBUG → 原空值`，逐项 readback。
- 前后 DUT BLE permissions 与前驱退休的最新 baseline 一致；IME 一致；最终 App/helper/instrumentation/Settings service/forward 缺席，store absent，lease 才释放。
- HANS debug 为空、cached freezer setting 为 `null`、deviceidle whitelist 无 Appium helper；这些是只读观察，不声称 HANS 分类恢复证明，原 run 没有这些 mutation。
- 恢复 **150s 总预算**、普通 command 至多 **6s**、单次完整 OS identity 至多 **12s**；这是独立 recovery budget，不扩展旧 run，也不授权重复场景。
- 最终 `status=recovered_failed`、`phone_status=passed`、`primary_error=null`、`cleanup_errors=[]`、`lease_released=true`。5037 仍 PID **16841**，5038/8237/8937 无 listener，phone lease 为空。

| 文件 | SHA256 |
| --- | --- |
| 原 failed `result.json` | `d60902a4a6425459ffaf09532eaa1f1e4f839ddb42b1c7509752db890e24e29d` |
| 原 `manifest.json` | `08947448be8a5a22bfce5db07ed1b1163dc288f64cab11e76c7bc3a582cf40b2` |
| 独立 recovery `result.json` | `7760662414d4e43ad19922e529f3040f9f87b27955d521a9b36899c2af0d6efc` |

恢复前后 `formal-host-lifecycle-verify` 均通过 **integrity replay**，返回的原 scenario/session/status 仍 failed、phone 仍原 quarantined、host gate 仍 failed；日志 `.work/logs/appium-start-hold-{failed,post-recovery}-offline-verify.log`。独立 recovery 改善的是当前物理清理状态，不改写当时的失败判定。新 recovery files 全部在 sibling directory，原 254 files 和严格 layout 未被污染。

## 7. 实际验证与下一边界

实际执行的 targeted unittest：

```sh
pixi run python -m unittest -v \
  tools.tests.react_native.test_bleng_android_lifecycle.AndroidLifecycleTests.test_start_hold_control_reuses_managed_owners_and_preclose_diagnostics \
  tools.tests.react_native.test_bleng_android_lifecycle.AndroidLifecycleTests.test_start_hold_insufficient_budget_keeps_failure_and_normal_cleanup \
  tools.tests.react_native.test_bleng_android_lifecycle.AndroidLifecycleTests.test_start_hold_record_failure_preserves_ui_success_but_quarantines_cleanup \
  tools.tests.react_native.test_bleng_android_lifecycle.AndroidLifecycleTests.test_start_hold_failure_and_interrupt_keep_scope_and_fresh_probe_before_delete \
  tools.tests.react_native.test_run_bleng_android_lifecycle \
  tools.tests.react_native.test_bleng_lifecycle_evidence.LifecycleEvidenceTests.test_start_hold_requires_its_own_record_and_exact_observations \
  tools.tests.react_native.test_bleng_lifecycle_evidence.LifecycleEvidenceTests.test_failed_start_hold_still_rejects_scope_washing_and_invalid_progress
```

结果 **26 tests / 34.015s / OK**，日志 `.work/logs/appium-start-hold-software.log`。收尾实际执行的 `pixi run just fmt`、`pixi run just lint`、`formal-host-check`、`formal-host-test`（**57 tests / 2 suites**）、`docs-check-mermaid`、`git diff --check` 和相关 authored Markdown UTF-8/fences/local links 检查均通过；日志 `.work/logs/appium-start-hold-final-{fmt,lint,host-check,host-test,docs,diff,authored-docs}.log`。未计为全量 tooling/provider discovery 或 native/RF gate。

本轮没有已证实的 production software bug，因此没有为 UI 失败修改 core、watchdog、native Close 或 Appium settings，也没有添加重复 test/framework 来“修绿”环境。

目前精确状态是：同 APK transport pass；新 start-hold 在 Start 前 failed，未证明静置/Close；post-failure 原 UiA2 freezer 状态有直接观察，独立 recovery passed。默认 Appium、Maestro opt-in、Detox 仅可行性评估均不变。

后续如评估同 APK 的 reviewed-ROM 单 UID HANS 对照，需要明确保留新的环境策略差异及 restore evidence，不能自动重试本 failed owner、默认启用豁免或宣称已有 lifecycle/root-cause pass。本轮到安全恢复和证据整理为止，不继续第二次设备场景。
