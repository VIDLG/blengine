# Android Appium transport-only：独立 scope、软件回归与设备边界

日期：2026-11-09。延续[同 APK Appium comparison failed / 独立恢复](2026-11-09-formal-android-appium-comparison-recovery.md)。目的是用固定只读查询隔离 transport，不重跑原 lifecycle，也不宣称定位了原 `socket hang up` 的根因。

## 1. 独立诊断与职责

- [`run_android_appium_transport.py`](../../../tools/react_native/run_android_appium_transport.py) 仅转发到现有 managed CLI 并强制 transport mode；Just 提供 `formal-host-android-appium-transport`，没有 build、helper install、SDK setup 依赖。
- [`bleng_android_lifecycle.py`](../../../tools/react_native/bleng_android_lifecycle.py) 复用冻结 APK、App-only lease、`AndroidManagedHost`、原 App/session binding、独立 cleanup、prepared evidence、phone finalization 和 sealing。不是另一套 installation/session/recovery framework。
- 新 scope 为 **`formal-android-appium-transport-diagnostic`**；成功 trace 仅 `observations=["transport"]`，`startup_snapshot={}`，`host_gate_status=pending`、`rf=not_run`。不能洗成 lifecycle/idle/start-hold pass。
- [`android_appium_transport.py`](../../../tools/react_native/android_appium_transport.py) 只执行四项固定诊断读：`get_settings` → `page_source` → 固定 `formal-host-title` find → `is_displayed`。不 polling、不 fallback、不 navigation、不 Start/Close/scan，不改 DUT BLE 权限或 radio。正常 session bind 本来已有独立的 `get_settings` 和 owner SDK checks；四项不代表整轮只有四次 SDK/HTTP I/O。
- 单项 query、前后 authority/original-identity 检查共享至多 **5 秒**的绝对预算；剩余 scenario budget 更小时使用更早 deadline。`AndroidAppBinding` 的 frozen copy 只缩短 execution deadline，不重选 candidate/identity。局部 timeout 不冒充 owner drift，独立 cleanup 仍需 fresh 原身份检查。
- `automation/transport-control.json` 保存每项顺序、时间、typed summary、失败及原 host session/candidate identity hash。`automation/transport-source.xml` 为敏感 host-only source，至多 **1 MiB**，拒绝 DTD/entity declarations 和非 Android hierarchy。不会写 screenshot 或把 raw source 上传外部服务。
- [`bleng_lifecycle_evidence.py`](../../../tools/react_native/bleng_lifecycle_evidence.py) 增加独立 scope/receipt 校验，保留原 lifecycle 规则；继续严格检查原 session、candidate、helper、cleanup 和 phone receipts。失败 record 也不能随意删除、改成 `not_run`、重排或隐藏失败；确实的 record 发布错误单独保留并阻止 lease release。即使发布失败，保留 raw source 的专属大小上限仍不变。
- transport mode 拒绝 Close diagnostics、device-local capture、HANS/freezer/deviceidle exemptions；App install 和 temporary helper-settings 仍需分别显式授权。既有 helper triplet/hash 需已具备，不隐式安装 helper。成功保留 App；失败不自动重跑，unknown session 不 blind DELETE，cleanup 不确定继续 quarantine。

安全发现与隔离：

```sh
pixi run just react-native formal-host-android-appium-transport --help
pixi run just react-native formal-host-lifecycle-verify ATTEMPT
```

`ATTEMPT` 是已存在的诊断目录，不是设备授权。普通入口不会构建 APK、安装开发工具、刷 firmware、切 radio 或修改默认 Appium/Maestro 选择。

## 2. Test-first 与独立审查修正

[`test_android_appium_transport.py`](../../../tools/tests/react_native/test_android_appium_transport.py) 使用真实 managed owners/lease/sealer，复用 owner-local synthetic harness，在真正 SDK/subprocess/network 边界隔离。没有把 owner 替换成全成功 mock，没有 synthetic identity 到达真实设备命令。

保留红测试与后续绿测试日志：

- `.work/logs/appium-transport-red.log`：新 transport mode 尚未实现时的拒绝。
- `.work/logs/appium-transport-review-red.log`：独立审查发现身份 SDK 仍可能用总 scenario deadline、失败 trace 可被删除/改为 not-run；回归先复现，再以窄 deadline 透传和 strict partial-record 检查修正。
- `.work/logs/appium-transport-source-bound-red.log`：record 发布失败路径的 raw XML 超限回归，先复现，再将专属 read bound 移到 record 是否存在的分支之外。
- fixture 首轮的 visibility mock `side_effect`/not-run record 预期错误单独修正，没有把 `False` 放宽为 pass。生产 visibility 要求实际值严格为 `True`。

覆盖重点为正常原身份保留与 teardown、missing title 单请求、坏 settings/source、source timeout、可见性 false、未知 Remote、post-query identity loss、局部预算到期与 fresh cleanup、interrupt 保留、record 发布失败 quarantine，以及一致重写 manifest 后仍不能洗掉 scope/identity/query order/raw source 的语义错误。

本地 SDK 源码核对使用已锁定的 Appium Python Client **6.1.5** / Selenium **4.51.0**：find 是单次 `FIND_ELEMENT`，Appium WebElement 的 displayed override 是 `IS_ELEMENT_DISPLAYED`，不是浏览器 JS atom。没有下载新版本来替换本地行为证据。

## 3. 实际软件验证

已执行以下有重叠的定向命令，不能相加为覆盖率或全量通过：

```sh
pixi run just react-native tools-test-appium
pixi run python -m unittest -v tools.tests.react_native.test_bleng_scan_host tools.tests.react_native.test_bleng_lifecycle_evidence tools.tests.react_native.test_run_bleng_android_lifecycle tools.tests.react_native.test_recipe_wiring tools.tests.react_native.test_bleng_android_lifecycle.AndroidLifecycleTests.test_complete_fake_sdk_flow_preserves_evidence_app_and_owner_receipts
pixi run python -m unittest -v tools.tests.react_native.test_bleng_android_candidate
```

| 命令组 | 实际结果 | 日志 |
| --- | --- | --- |
| canonical Appium slice | **86 tests / 81.477s / OK** | `.work/logs/appium-transport-appium-slice.log` |
| shared binding / evidence / CLI / recipe / 原 lifecycle 成功路径 | **117 tests / 32.853s / OK** | `.work/logs/appium-transport-shared-regression.log` |
| candidate 原身份与 cleanup 回归 | **63 tests / 107.900s / OK** | `.work/logs/appium-transport-candidate-regression.log` |

raw-source failure-path 上限修正后的 `pixi run just react-native tools-test test_android_appium_transport`：**18 tests / 72.291s / OK**，包括实际 SDK boundary 的 transport 回归及匹配的 recipe wiring；日志 `.work/logs/appium-transport-final-tests.log`。最终 evidence/CLI/recipe 组合 **103 tests / OK**，日志 `.work/logs/appium-transport-final-evidence-cli.log`。

`pixi run just fmt` 和 `pixi run just lint` 最终通过，后者包含 Clippy/Ruff/profile isolation；日志 `.work/logs/appium-transport-final-{fmt,lint}.log`。`formal-host-check` 与 `formal-host-test` 通过（**57 tests / 2 suites**），日志 `.work/logs/appium-transport-formal-{check,test}.log`。Mermaid、authored Markdown UTF-8/fences/links 与 diff checks 作为文档检查，不是设备 gate。

## 4. 同 APK 的一次真机 transport candidate passed

软件检查通过后，使用用户已有 Android install/helper-settings/root 授权执行独立新 attempt，没有重跑原 failed lifecycle。先只读预检原 lease 为空、App/helper/instrumentation/forward/store 缺席、AppOp deny/hidden-API keys 缺席、DUT BLE 权限匹配基线及原 ADB listener；日志与完整 receipts 为 `.work/logs/appium-transport-preflight.{log,json}`。

| 项目 | 本次实际观察 |
| --- | --- |
| run / scope | **`a4885be7-627e-4f03-b2ba-7d85486a06d0` / `formal-android-appium-transport-diagnostic`** |
| bundle | `.work/formal-android-appium-transport/a4885be7-627e-4f03-b2ba-7d85486a06d0/` |
| APK | **`c96fc9909ed056e33e1ce47927639da742614294a60114d8ae546ee49255a584`**，与 Maestro comparison 和原 failed Appium candidate 相同，没有 rebuild |
| tools | 既有 managed Appium **3.9.0** / UiA2 driver **8.7.3**；helper server **10.7.6**、Settings **8.4.5**；三项 APK hashes 与原 reviewed inventory 一致，没有安装/替换 helper |
| 原 App | `com.blehubblenghost`，PID **23923** / UID **10378** / start ticks **251037215**；boot `35df0f9f-6c54-42b0-800c-3c410469b6e7` |
| 原 server | PID **24561** / UID **10255** / start ticks **251038023**，与 pinned helper APK/boot 一致 |
| session | host `053beaa9-799f-493f-b511-49ceea69df27`；device `968cb315-6178-43e8-a6f4-babc4034bba8`；不宣称两 UUID 相同或可互换 |
| diagnostic reads | `get_settings` **4.349s** → `page_source` **4.498s** → `find_title` **4.461s** → `title_displayed=true` **4.306s**；时间包含各项原身份复核，均在独立 5 秒预算内，没有重试 |
| raw source | **16684 bytes**；SHA256 `5fbb7762e11519c725a903fe1d07fd6ae970ae8a085084ff39197024af4b7098`；只读 local XML，非 screenshot/RF observation |
| verdict | `scenario_status=passed`、`session_status=passed`、`phone_status=passed`、`status=passed`；primary 为 null，两组 cleanup errors 为空 |
| cleanup | 正常 WebDriver DELETE、local server reaped、platform/helper restoration 全通过；lease 已释放，未 force-stop 成功 App |
| host/RF | **`host_gate_status=pending` / `rf=not_run`**；没有 Start/Close/scan、permission grant/revoke、radio read/change、firmware 或 policy exemption |
| daemon | 5037 始终 **PID 16841**；运行后 5038/8237/8937 无 listener；没有重启/杀死共享 ADB 或关闭其他客户端 |
| sealed evidence | **408 files**；result SHA256 `77ed16aed4ea9f8821230ef0ec021f759abfef08b60381ab271a0bc45582d1d0`；manifest SHA256 `90528b3abfa0a0cd5fdad20fe9ad0bef25ec3f188c3e18aae9505a0c7913ec65` |

实际命令经维护 recipe 执行，参数为上述明确 serial/App/APK/helper hashes、现有 managed home/Node、ports **8237/8937**、`--timeout 120 --phase-timeout 5 --cleanup-seconds 60 --authorize-install --authorize-helper-settings --use-root`。没有任何 exemption 或 Close diagnostics flag。完整输出 `.work/logs/appium-transport-device.log`，`run_quiet --no-tty --progress-seconds 20` 保留 bounded progress，没有 silent timeout/retry。

独立 `formal-host-lifecycle-verify` 回放通过，日志 `.work/logs/appium-transport-device-offline-verify.log`。运行后另做只读观察：DUT BLE 权限与 preflight 一致，成功 App 完整 identity 仍与 finalization receipt 匹配，原 failed comparison 的 manifest-listed 238 files 与 result bytes 未变；证据 `.work/logs/appium-transport-post-observation.{log,json}`。这是 post-observation，不是下一轮的 mutation authority。

## 5. 保留的限制与当前手机状态

该候选完成时保留了 **PID 23923 / UID 10378 / start ticks 251037215** 的 idle workbench；随后在独立 lease 下已 fresh 验证并精确退休。后续 start-hold attempt 失败于 Start 之前，现已独立恢复、无 retained App，见[后续诊断及 recovery](2026-11-09-formal-android-appium-start-hold.md)。不能重复退休 PID 23923 或旧 Maestro PID 16829，也不能将后续失败改写为本次 transport pass。

这次通过证明同一 reviewed APK 在本次真实 Android/Appium session 上能完成固定 transport probes 与既有 cleanup。它**不证明** Start 后 Evidence navigation 稳定、原 `socket hang up` 根因已消失、RN/native Close/resource-zero、scan/RF、Hermes reload、ABI 或 release gate，也不宣称优于 Maestro。约 4.3–4.5 秒的 query 实测包含严格身份成本；不能为缩短命令而删掉原身份或 cleanup 检查。

原 comparison 的 failed result 和第一次恢复失败记录继续保留；旧 sealed 目录追加 recovery files 导致的 strict replay layout failure 没有被改成通过。本次复用 verifier 不允许这些额外文件，新 attempt 不向旧目录写入。
