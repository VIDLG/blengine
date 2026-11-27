# Android 同 APK Appium 对照失败与原 lease 精确恢复

日期：2026-11-09。延续 [Maestro lifecycle comparison](2026-11-09-formal-android-maestro-lifecycle.md)，记录已有 Appium attempt 的失败、恢复阻塞审计与独立恢复。没有再次执行 lifecycle、scan 或 RF，也没有更换默认 UI driver。

后续独立候选：[同 APK transport-only passed](2026-11-09-formal-android-appium-transport.md)。它没有执行 Start/Close/navigation，不改写本报告的 lifecycle failed 或旧 strict-layout failure；当前 retained App 身份以该后续报告为准。

## 1. 当前结论

| 方案 | 已有项目证据 | 本次结论边界 |
| --- | --- | --- |
| Appium（默认） | 历史 [HANS lifecycle candidate passed](2026-11-07-formal-android-hans-lifecycle.md)；本次同 APK candidate failed | Start 后 Evidence 查询出现 `socket hang up`，Close 尚未完成；不是 Rust/Close 根因证明 |
| Maestro 2.12.1（opt-in） | [idle 接入](2026-11-09-formal-android-maestro-idle-integration.md)及同 APK Start → Close → final Evidence comparison passed | 单设备、单 session 的非 RF UI 结果，不证明普遍优于 Appium，也不替代 native/RF gates |
| Detox | [本地固定证据评估](2026-11-08-rn-test-responsibilities-and-ui-tool-evaluation.md) | 仍只有源码可行性评估，没有本项目编译或真机 pass |

原 Appium 场景 verdict 保持 **failed**。本次独立恢复为 **`recovered_failed` / `phone_status=passed`**，不是补算原 session cleanup passed，更不是把失败的 lifecycle 升级为通过。原 quarantine lease 已释放，App/helper 进程缺席；没有自动重跑场景。

## 2. 已有同 APK 对照与 retained App 退休

Maestro 成功候选 `9ff9215d-665d-44c5-9d73-fddfcb51dd79` 的 retained App 已在独立授权退休中关闭：

- receipt：`.work/maestro-maintained-acceptance/retirement-221039f0-f449-4bdf-8a12-dc0a8806075e/result.json`。
- 原 identity：`com.blehubblenghost`，PID **16829** / UID **10378** / start ticks **246864984**。
- `status=passed`、permissions 未变、lease 已释放。不能再次按该 PID 退休，也不能继续称该成功页面仍 retained。

随后已有一次 managed Appium 执行，本次仅审计，不重复执行：

| 项目 | 原始观察 |
| --- | --- |
| run / scope | `353bf795-e38b-4a6f-9818-a99ed508080b` / `formal-android-managed-lifecycle` |
| directory | `.work/formal-android-appium-post-close/353bf795-e38b-4a6f-9818-a99ed508080b/` |
| APK SHA256 | `c96fc9909ed056e33e1ce47927639da742614294a60114d8ae546ee49255a584`，与 Maestro 成功候选相同 |
| App identity | PID **29219** / UID **10378** / start ticks **247283143**；boot `35df0f9f-6c54-42b0-800c-3c410469b6e7` |
| helper identity | UiAutomator2 server PID **29914** / UID **10255** / start ticks **247283892** |
| startup snapshot | `state=poweredOn`，scan/connection authorization 均 `denied` |
| UI | observations 为 `idle`, `started`；Evidence tap 已返回，后续 `elementDisplayed` 查询报 `Could not proxy command to the remote server. Original error: socket hang up` |
| verdict | `scenario_status=failed`、`session_status=failed`、`phone_status=quarantined`、`host_gate_status=failed`、`rf=not_run` |
| 原 cleanup | WebDriver DELETE 超时；local Appium TERM wait 超时后 SIGKILL；session/helper restoration 未证明；candidate finalization 的 current-user SDK 查询失败 |

本次 bundle 无 HANS / server-freezer / server-power mutation receipts；存在 `bound-session.json`。对照 [session binding 的执行顺序](../../../tools/react_native/bleng_android_session.py)，opt-in HANS/freezer apply 在 bound receipt 之前。不能把该 attempt 当作已启用历史 HANS workaround 的完全受控复现，也不能单凭同 APK 就推导 automation 根因。

## 3. 第一次恢复为何拒绝删除

原 `.work/recover_appium_attempt.py` 已完成 App/helper stop、精确 forward removal 和 helper mock-location AppOp `allow → deny`，随后拒绝 store 删除：

```text
RuntimeError: mock store identity/content changed; refusing deletion
```

审计发现是一次性脚本的比较错误，不是已证实的 store 漂移：

- `helpers/007-command.json` 是成功 `noclobber + umask 077` 创建回执；`helpers/prepared.json` 与 `helpers/010-command.json` 再次确认同一对象。
- pin 均为 **`65099:86998:8180:1:23:2000:2000`**；完整内容为 `["io.appium.settings"]` 加实际末尾换行。
- 原 `recovery-result.json` 的 events 8 / 16 仍为完全相同的 stat 与 bytes。
- 脚本比较用了 `STORE[0] + "\\n" + STORE[1]`，即字面量反斜线加 `n`，而 SDK 输出 stat/content 的分隔符是实际换行。离线已同时重现正确比较通过、旧比较拒绝。
- 使用生产 [`AndroidHelperPreparation._store_pin`](../../../tools/react_native/bleng_android_preparation.py) 解析创建与恢复输出，结果严格一致；没有按“内容看起来相同”收养未知文件，也没有放宽任何 pin 字段。

第一次恢复的 `recovery-result.json` / `recovery-phone.json` 和原 failed verdict 全部保留。原失败不是已验证的第三方 Appium store 改写证据。

## 4. 独立恢复及真实终态

新的 one-shot recovery 使用原 run 的 quarantine lease，核对 owner bytes、目录 inode、historical host PID 缺席及原 receipt 哈希；未创建新设备场景或抢占其他 lease。

- evidence directory：`.work/appium-post-close-verified-recovery-d7abde70-e4bc-4444-af5a-6b319f2d025e/`。
- 脚本：`.work/recover_appium_store_verified.py`；日志：`.work/logs/appium-post-close-verified-recovery-execution.log`。
- **55** 个有界 SDK command receipts，整体 deadline **140s**、单命令至多 **7s**；每次命令前后检查原 lease、关键 receipt 和 host listeners。
- 先 fresh 确认相同 boot/user、App 与全部 Appium/Maestro helper 进程缺席、instrumentation/Settings service 缺席、forward 为空；核对已安装 DUT/helper APK 哈希与原路径，IME 仍匹配原始快照。
- DUT `BLUETOOTH_SCAN` / `BLUETOOTH_CONNECT` 的完整解析结果与 Maestro retained retirement 的最后权限回执一致，均未 grant/revoke。
- store 的完整 live stat/bytes 必须与成功创建回执一致；删除命令内再次拒绝 symlink/nonregular，精确比较全部 stat 字段及 `cmp` 内容后，只删除该对象，再确认 `absent`。
- 发现 Appium session 留下 `hidden_api_policy`、`hidden_api_policy_p_apps`、`hidden_api_policy_pre_p_apps` 三个值为 `1` 的 key；原 before/admission inventory 证明这些 key 缺席。逐项 fresh 确认后仅删除这三项，并以最终 global key inventory 确认恢复缺席；不 reset 全局设置。
- helper AppOp 保持原 package-scope `deny`；无需再次 mutation。
- deviceidle whitelist 未含 Appium helper；`persist.sys.hans.dump` 为 **空**，`cached_apps_freezer` 为 **`null`**。未设置 debug property、未更改 HANS 分类或 freezer/power 策略。debug 关闭时 `hans scene` 只有时间头，**不冒充 HANS 分类 readback**；原 attempt 无这些 mutation，亦无完整 pre-run 基线，不能额外宣称完成了 HANS 分类恢复验证。
- `phone_status=passed`、`cleanup_errors=[]`、`lease_released=true`；最终 `.work/android-radio-leases/` 无 owner。原 App 不再运行。
- 5037 listener 始终为原 **PID 16841**；5038/8237/8937 无 listener。没有重启/杀死共享 ADB、关闭其他客户端、读取/改变 radio、刷固件或重新启动 App。

执行入口首次因 Python script import path 缺少项目根而在设备命令前退出（`.work/logs/appium-post-close-verified-recovery.log`）；显式 `PYTHONPATH=.` 后才进入上述独立恢复。不是重跑 lifecycle，也不是设备 mutation timeout 后重试。

## 5. 证据完整性与离线回放限制

原 manifest 所列 **238 files** 的大小/SHA256 在恢复前后全部匹配，未修改原 result、manifest 或旧 recovery receipts：

| 文件 | SHA256 |
| --- | --- |
| 原 `result.json` | `1439a67f5c3334f16125202354d4e39deae97d7393e7c0557cd63f3035f8cd17` |
| 原 `manifest.json` | `db033b6522434b5fadb6b435aaeaae15d86d5f700da324c46b60134b1d9a8fa6` |
| 原 `recovery-result.json` | `24dc1d5a5fa0540d92a5dcd21403123e37801da38c0167fc967bac48ec80ce64` |
| 新独立恢复 `result.json` | `c5c4c2eb68743a9ec2e1357c49aa73fbe9f4dd1cbbf99ed9dd96c00cc039e501` |

但是原目录已经包含第一次恢复追加的两个 root files，严格 `formal-host-lifecycle-verify` 本次 **exit 2：`unexpected evidence layout`**。这与原 manifest-listed bytes 未变是不同事实，**不能声称整个 bundle 离线重放通过**。不删除原恢复失败记录、不重写 manifest、不放宽 verifier；本次新的恢复证据因此放在独立 sibling directory。日志 `.work/logs/appium-post-close-original-offline-verify.log`。

## 6. 测试重构收尾与实际验证

`pixi run just react-native tools-test` 现在保留 canonical discovery；同时增加四个同样硬件无关、只改变 `unittest -k` 过滤器的职责入口：

- `tools-test-android-safety`
- `tools-test-appium`
- `tools-test-maestro`
- `tools-test-lifecycle`

它们不是互斥 suite，不能把计数相加成覆盖率。初次整组运行显示 `android_` 与 `lifecycle` 范围过宽，在 180 秒 bounded deadline 内未完成，最后停在长耗时 device-capture/lifecycle case；没有失败断言，也没有遗留进程。随后改按模块级 deadline 运行，定位并验证如下：

| Slice | Result |
| --- | --- |
| Appium | **68 tests / 2.418s / OK** |
| Maestro | **103 tests / 40.931s / OK** |
| Android session owner | **68 tests / 107.172s / OK** |
| Android automation + observation lease | **34 tests / 9.242s / OK** |
| Lifecycle evidence | **69 tests / 3.280s / OK** |
| Lifecycle runner | **16 tests / 0.061s / OK** |
| Recipe wiring | **13 tests / 0.205s / OK** |

[`test_bleng_android_preparation.py`](../../../tools/tests/react_native/test_bleng_android_preparation.py) 将原分散的 store identity/bytes 漂移断言合为独立 fixture 的矩阵：device、inode、mode、file type、nlink、size、UID、GID、内容、缺失/字面量末尾换行，以及缺失/字面量 stat-content 分隔换行。每项保留“不删除、只执行一次可归因 revoke、不能 restored、lease 保留”断言；成功 prepare/restore 同时保护真实换行。没有修改 production helper、安全 watchdog 或 Rust/Close。

已实际执行：

- `pixi run just react-native tools-test test_bleng_android_preparation`：**58 tests / 38.596s / OK**，包括 13 个 drift subtests；日志 `.work/logs/appium-post-close-helper-final-regression.log`。
- 上述模块级 slices 均按独立日志保存于 `.work/logs/next-step-{android-session,android-automation-lease,lifecycle-evidence,lifecycle-runner,appium,maestro}.log`；包括 extra/missing/layout drift 严格拒绝，不为旧 recovery 文件开例外。
- 新增 recipe wiring 验证日志 `.work/logs/next-step-recipe-wiring-fixed.log`；四个 slice 的 `just --dry-run` 输出也已分别保存，均保持 canonical discovery root。
- `pixi run python -m unittest -v tools.tests.react_native.test_bleng_lifecycle_evidence tools.tests.react_native.test_recipe_wiring`：**81 tests / 3.290s / OK**；日志 `.work/logs/appium-post-close-evidence-wiring-regression.log`。
- `pixi run just react-native formal-host-check`：typecheck/autolinking/no-internal 通过。
- `pixi run just react-native formal-host-test`：**57 tests / 2 suites / passed**。
- `pixi run just fmt`、`pixi run just lint`：最终通过，日志 `.work/logs/appium-post-close-recovery-verified-{fmt,lint}.log`。首次 fmt 的新测试 nested-with `SIM117` 已合并上下文修正，未删除安全断言；原失败日志保留。
- `pixi run just docs-check-mermaid`、相关 authored Markdown UTF-8/fences/whitespace/local-link 检查及 `git diff --check` 通过；未新增 Mermaid 图。

上述均为定向软件验证，不是全量 tools discovery、native cleanup/resource-zero、RF 或 release gate。Appium 默认、Maestro opt-in、Detox 仅评估的状态不变；本次不继续设备 run，不把历史 HANS candidate 或 Maestro pass 合并到当前失败 session。
