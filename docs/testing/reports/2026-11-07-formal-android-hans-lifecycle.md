# Formal Android：单 UID HANS 豁免与原 lifecycle 链通过

接续[原进程 freezer 证据](2026-11-07-formal-android-idle-freezer.md)。用户明确授权真机、root、工具升级及必要软件修正。本轮没有修改 BleHub/BLeng、正常 UI 路径、BLE 权限、radio、固件、温控模块、厂商 XML 或全局 freezer。

**这台手机和本候选上的 idle 阻塞及 `Start → Close → Evidence → DELETE` 已取得通过证据。修复是显式、默认关闭、限定已审计 ROM 的 host 环境规避策略，不是所有 Android 的通用保证；`host_gate_status=pending`、`rf=not_run`，不是 RF、ABI 或 Phase exit。**

## 1. 从设备源码确定最小控制面

前轮原 UiAutomator2 server 全部线程的 `do_freezer_trap` 已直接解释“TCP 已 ACK、用户态不处理 HTTP”。标准 deviceidle 白名单和 AOSP `unfreeze --sticky` 均未解决本机 OEM 路径，不能继续把它们当修复。

从实际设备取回 ROM JAR，而不是根据网上其他机型猜测命令：

| 文件 | SHA-256 |
| --- | --- |
| `/system/framework/oplus-services.jar` | `889bdf044f565d94a4fb603402a0f7f3d394750f2d151c29eaa5122a25b09a24` |
| `/system/framework/oplus-framework.jar` | `f9a6951017ddc59260372d8406a1b2a56541c555deff6a73a86c59fe15489592` |

本地副本及定向反编译在 `.work/hans-framework-extract-10b7a6ab-d977-4004-abe4-a264e5567c4e/`。官方 JADX 1.5.6 release SHA-256 为 `545ea2be9c242511bc145755cf4bda2485ade42966e096f8b4d3da2a230e8974`；部分大方法反编译失败，不据此推断未还原的逻辑。

已核对 `OplusHansManager` / `HansSceneManager` 的 `addwl` 分支：

- `dumpsys activity hans addwl <uid> <package> 1` 将目标从管理分类移出，再加入 WHITE。
- 对应 `0` 恢复为 THIRD。flags 为 THIRD=1、WHITE=2、EX_WHITELIST=4；实际亮屏场景 target flags=5。
- 调试分支要求 `persist.sys.hans.dump=true`；只在短事务中临时设置并在 finally 恢复原值。
- 实际 helper `io.appium.uiautomator2.server` / UID 10255 的初始分类为 THIRD。

没有自制 Binder transaction、修改 UiAutomator2 APK 或 fork driver。该 OEM 命令不是 Android/Appium 通用 API，因此严格限制 ROM hash，不自动探测后套用到别的设备。

先在不启动 Appium 的独立 phone lease 下执行单 UID move/readback/restore：`.work/hans-single-uid-transaction-df3f4f2e-0d91-4e27-8a36-69b4e0cace57/`。目标 THIRD→WHITE→THIRD、其他 UID 分类及 debug property 恢复均有回执，cleanup errors 为空。这份小事务本身不证明 Appium 稳定性。

## 2. 代码责任和失败语义

- [`android_hans_policy.py`](../../../tools/react_native/android_hans_policy.py)：owner-local HANS 事务及独立补偿。
- [`bleng_android_session.py`](../../../tools/react_native/bleng_android_session.py)：fresh server/session 固定后、首次 live proxy 前 apply；finalize 先恢复 HANS，再检查 session/原 server absence。
- [`bleng_android_lifecycle.py`](../../../tools/react_native/bleng_android_lifecycle.py)：即使 automation authority 已失效，也在 cleanup 中独立尝试 HANS 恢复；不接管未知 DELETE。
- [`run_bleng_android_lifecycle.py`](../../../tools/react_native/run_bleng_android_lifecycle.py)：显式 `--authorize-server-hans-exemption`，默认不启用。
- [`bleng_lifecycle_evidence.py`](../../../tools/react_native/bleng_lifecycle_evidence.py)：离线检查 HANS selector、baseline、apply/restore、debug 和 command receipts，不能靠重封 manifest 把缺失恢复洗成通过。最后审查发现“删除 mutation receipts 后保留连续编号的读回”或“把 mutation 移到读回之后”仍可能通过，已用两个 red 用例复现并修正为按分类快照核对精确命令顺序；同时保留目标已被外部恢复时不发冗余 mutation 的合法边界。

关键约束：

1. 同一 phone lease、boot、helper UID/APK、已审计 ROM；apply 还要求原 PID/start/session selector 不变。
2. 不采用 shared UID、不采用未知分类、不移除原本已在 WHITE 的 helper。
3. membership dispatch 前消耗一次性提交资格；未知结果只允许独立补偿，不重放 apply。恢复失败是 sticky，不能反复运行到成功。
4. 恢复不依赖旧 server 仍存活，但必须重新核验 installed identity、boot、ROM 和 lease。
5. 事务中发现非目标 UID 分类变化即失败，保留 before/after，不猜测原因，也不重放或恢复别人的 map。这一边界先用两个测试确认 red，再做最小实现取得 green。
6. debug 恢复与 membership 恢复独立记录。原 session unknown/cleanup failure 不能因 HANS 恢复成功而放租。
7. 恢复的是 classification 语义；`addwl` 会重建 map object，不宣称还原原瞬态状态、timestamp 或原子系统快照。

默认关闭的 deviceidle/sticky 两个诊断入口仍保留各自安全回归，不与本次 HANS 策略合并。没有增大既有 timeout 或添加 UI/解冻重试。

## 3. 两个真实通过候选

共同条件：手机 `3ac5cc09`；boot `35df0f9f-6c54-42b0-800c-3c410469b6e7`；App `com.blehubblenghost`；APK SHA-256 `51711779e5e9efd60105d1f312b90e65df0d85f7f8996c1d70e200485e6d9fef`。Appium 3.9.0 / Python Client 6.1.5 / UiAutomator2 driver 8.7.3 / server APK 10.7.6，沿用锁定的 managed 安装。

只启用 HANS exemption；power exemption 和 sticky exemption 都为 false。场景 180 秒、单 phase 20 秒、cleanup 90 秒未改变；root device-local TCP 6790/原 server log collector 开启。控制器 request 和软件 SHA-256 在各自 `.work/device-local-hans-{idle,lifecycle}-controller-<run>/` 中，bundle 在 `.work/formal-android-managed-lifecycle/<run>/`。

| run | 场景 | 真实结果 |
| --- | --- | --- |
| `d46993af-397a-4232-88a8-cf12027660b3` | idle 两轮 Tests/Evidence/Current run 浏览，最终 Evidence/idle；无业务 Start/Close | scenario/session/phone/status 全 passed；无 primary/cleanup error |
| `09fe78b8-034f-44c2-8968-985ac24af65d` | idle 浏览→Start→snapshot→started 浏览→Close→Evidence/closed | observations=`[idle, started, closed]`；scenario/session/phone/status 全 passed；无 primary/cleanup error |

二者 `verify_lifecycle_bundle` 均实际成功返回；加入最终 mutation/readback 顺序校验后再次离线通过，未重跑真机，日志 `.work/hans-final-bundle-integrity.log`。两次 HANS apply/restore 都为 passed、`debug_restored=true`、`other_classifications_changed=false`；初始 THIRD、运行 WHITE、结束 THIRD。原 server 分别为 PID 19820、27425；正常 finalizer 验证原进程缺席，helper/Appium/forward/tag 恢复和 phone release 没有因豁免放宽。

### Appium 同一 host 日志中的时间链

以下仅比较各 run 的 Appium 日志时间，不与设备 PCAP/monotonic 混算：

- idle：13:32:25.963 已有 proxy 配置，13:33:45.087 最后一条 Evidence click 返回 200；13:34:19.021 DELETE 返回 200 / 578ms。正常 UI 应答覆盖了此前约 50 秒的冻结窗口，不只是短时启动探针。
- lifecycle：Close element `…00000066` 的 click 于 13:39:33.189 提交，13:39:33.277 返回 200 / 88ms。
- Close 后 Evidence element `…0000000f` 的 click 于 13:39:41.179 提交，13:39:41.260 返回 200 / 81ms；随后页面/closed/evidence 读取继续 200，最后一次 text 在 13:39:44.112 返回。
- 13:40:06.054 DELETE 提交，13:40:06.567 返回 200 / 513ms。

真实 startup snapshot 为 `state=poweredOn`、`scan_authorization=denied`、`connection_authorization=denied`。原样保留，不授予权限、不把 lifecycle 通过说成 scan readiness。

两个 collector 都正常 finished，含三次 tree quiet 检查及 owned payload/token 清理。lifecycle PCAP 为 219871 bytes，SHA-256 `972ed9a01d756984f8cadf45cefabe48671762fb4a6fe42a883adde0f613bd62`；这里不新增未做的包级分析或 resource-zero 结论。

## 4. 失败尝试和场景交接不被洗白

- 首次本地 controller 因未设置项目 import path 而 `ModuleNotFoundError: tools`，在任何设备动作前退出；修正为 `PYTHONPATH=.` 后才执行 idle 候选。
- 中间 run `67cb52dc-9ce8-4924-87e6-395bca4ff2f7` 在业务启动前拒绝：上一轮成功保留的 idle App 仍在运行，runner 不接管它。其 bundle 保留 failed / scenario、session not_run；不是 HANS 失败或冻结复现，清理错误为空且 lease 已释放。
- 为执行已授权的下一场景，独立 lease 下重新核对上一轮完整 PID/UID/start/APK/boot identity，仅退休该已知 idle App，验证整个 UID 进程缺席后释放。证据 `.work/retire-hans-idle-owned-b77b68f7-615b-4870-9b20-d42e51b07d11/`，cleanup errors 为空。不是修改前一份 passing bundle，也不是重试原失败 owner。
- 最后的 lifecycle 候选使用 fresh run/session；成功后的 Evidence/closed 页继续保留，没有停止此 App。

原先五份 failed/quarantined 冻结 bundle 仍保持原 verdict，独立恢复记录不改写历史结果。共享 SDK ADB daemon PID 16841 没有重启；交付前 4737/8237 无 listener、phone lease 目录为空。

## 5. 软件验证和范围

实际执行：

- `pixi run --locked python -W error::ResourceWarning -m unittest -v tools.tests.react_native.test_android_hans_policy`，并在同一 focused 命令中加入 lifecycle 的四个 `test_server_hans_*` 和完整 `test_run_bleng_android_lifecycle`：**36 tests / 23.720s，passed**；`.work/hans-focused-green.log`。
- `pixi run --locked python -W error::ResourceWarning -m unittest -v tools.tests.react_native.test_bleng_hans_evidence tools.tests.react_native.test_bleng_lifecycle_evidence tools.tests.react_native.test_bleng_device_local_evidence`：最初 **96 tests / 5.097s，passed**；补齐命令顺序和外部恢复边界后，同一命令最终 **99 tests / 5.312s，passed**；`.work/hans-evidence-final-order.log`。
- 最终把上述 focused 36 项与 evidence 99 项合并为同一 unittest 调用：**135 tests / 29.036s，passed**；完整测试名、输出及结果见 `.work/hans-final-combined.log`。
- session 七个 `test_server_freezer_*` 用 unittest 标准 loader 按现有测试名前缀组成定向回归，**7 passed**；`.work/hans-freezer-regression.log`。
- `pixi run --locked just react-native formal-host-test`：**2 suites / 52 tests，passed**；`pixi run --locked npm --prefix tests/consumers/react-native-bleng run typecheck` 和 `pixi run --locked just react-native host-check` 通过。
- `pixi run --locked just fmt`、`pixi run --locked just lint`、`pixi run --locked just docs-check-mermaid`、`git diff --check` 通过，日志 `.work/hans-final-{fmt,lint,docs,diff-check}.log`；后者只有已有文件的 CRLF 提示，没有 whitespace error。

两项新增 non-target drift 用例的 red 日志 `.work/hans-nontarget-red.log`，及两个离线 mutation 命令顺序用例的 red 日志 `.work/hans-command-order-red.log`，均明确为“应抛错但未抛错”，不是环境失败；实现后纳入上述 green。

四个完整 policy/session/lifecycle/CLI 模块的组合在 120 秒预算内没有完成，停于已有 session freezer 用例；日志保留在 `.work/hans-regression.log`，未发现该调用残留进程。另一次 canonical `pixi run just react-native tools-test` 也在 120 秒内未完成。不能计为完整回归通过；本轮没有盲目放大预算或反复跑全 discovery。

结论仅限：已审计 ROM 上，以受控 HANS 环境策略消除了本候选已复现的自动化冻结阻塞，且原始非 RF lifecycle 链和恢复已通过。没有证明所有历史 `service_accept` timeout 只有一个成因；没有新做 RF、reload、长时稳定性、其他系统版本或完整 native/package/ABI 验证。正常 UI 仍使用官方 Appium SDK/stock driver，不需要替换自动化框架来绕开本机 OEM freezer。
