# 正式 Android Maestro lifecycle：接线、首轮失败与离线表达式修正

日期：2026-11-09。延续 [idle 接入候选](2026-11-09-formal-android-maestro-idle-integration.md)，仅评估同一正式 App 的非 RF UI transport，不改变默认 Appium、native/RF gate 或 internal 删除条件。

## 1. 当前结论

- 单次 CLI/instrumentation 的 Start → 浏览 → Close → Evidence 软件路径已接入 named runner、CLI、Just 和严格离线证据回放。
- **首个真机 lifecycle attempt 失败于 Start 后的动态断言，尚未执行 Close。** 随后的修正候选已完成同一 CLI lifetime 的 Start → Close → final Evidence comparison pass。
- 已用锁定 Maestro 2.12.1 的原版 `Env` + `GraalJsEngine` 离线复现并修正表达式边界；软件回归和修正后的第二次真机 candidate 均通过。原失败 attempt 未重跑或改写。
- `host_gate_status=pending`、`rf=not_run` 保持不变；不证明 native resource-zero、scan/RF、Hermes reload、持续结果页可见性或 Phase exit。

## 2. 实现与职责

- [`MaestroAndroidOwner`](../../../tools/react_native/maestro_android.py) 的 `run_idle` / `run_lifecycle` 共享窄 `_run_flow`；每个 owner 只允许一次 CLI，不能 idle/lifecycle 连用。运行前后检查原始 `AndroidCandidateOwner` 身份，不重认领。
- [`runner`](../../../tools/react_native/maestro_idle_runner.py) 保留 idle API，新增 `run_maestro_lifecycle`；生命周期结果使用独立 `formal-android-maestro-lifecycle` scope 和 `startup_snapshot`。安装、lease、helper teardown、candidate finalization、phone finalization、prepare/seal 仍复用原 owner，不另造 registry 或 UI/ADB 协议。
- [`flow`](../../../tests/consumers/react-native-bleng/e2e/maestro/lifecycle-navigation.yaml) 使用官方 YAML：Tests/idle → Evidence/idle → Tests/idle → Start → Current run/started → Tests/started → Evidence/started → Current run/started → Close → Evidence/closed。三项启动快照必须逐项一致，最终保留 start/close 两项成功观察。八次 tap 均禁止 no-change retry。
- [`evidence`](../../../tools/react_native/maestro_idle_evidence.py) 检查 pinned flow、官方 commands export、evaluated assertions、JUnit、原 PID navigation 与四个 JS Close marker；它们不等于 native cleanup。idle/lifecycle scope 不得互换。
- preflight 补充拒绝 **helper 包不在 inventory 但同名或 `:worker` 进程仍在** 的状态；复用既有 process parser，拒绝在任何安装/UI mutation 之前发生。拒绝 sticky，不 force-stop 未知进程。

显式入口（与 idle 使用相同参数边界；这里只展示无设备的 help）：

```sh
pixi run just react-native formal-host-android-maestro-lifecycle --help
```

只读重放为 `pixi run just react-native formal-host-maestro-lifecycle-verify ATTEMPT`；`ATTEMPT` 是已存在的 lifecycle directory，不是设备授权。工具安装、APK 构建和普通 run 仍分开。

## 3. 精确退休与唯一真机 attempt

先只读验证 predecessor idle sealed bundle `fd6b95d9-5616-458f-b78d-df0f2d790473`，随后在独立新 lease 下 fresh 检查并显式退休其 PID **17666 / UID 10378 / start ticks 244973575**。退休 attempt：

`.work/maestro-maintained-acceptance/retirement-46d8b99c-aae8-4637-bf33-8cc752dbf462/`

退休 `status=passed`，包/UID 全进程缺席、权限未变、lease 已释放。完整日志 `.work/logs/maestro-lifecycle-retirement.log`。这是单独授权的已知身份退休，不是普通 runner 自动接管。

随后经维护入口执行一次新 attempt，无 rebuild、下载、固件、radio 或 DUT BLE permission grant/revoke：

| 项目 | 实际观察 |
| --- | --- |
| attempt | `e3cbd283-14c9-4abf-99c1-c90a800cbc95` |
| directory | `.work/formal-android-maestro-lifecycle/e3cbd283-14c9-4abf-99c1-c90a800cbc95/` |
| 候选 | APK `cf10697246fd4a7d141182a295dd378865637ac16c9c06d25d540cd3b1814a9c`；managed Maestro **2.12.1**；既有 Pixi JDK |
| 原 App | `com.blehubblenghost`，PID **30619**、UID **10378**、start ticks **245456599**；boot `35df0f9f-6c54-42b0-800c-3c410469b6e7` |
| UI | 初始标题/idle、Evidence/Tests 浏览、Start、Current run/started、合法 adapter-state、copyText 和赋值均完成；sequence **17** 的首个 captured-state 断言失败 |
| 现场 | 官方 hierarchy 中 `adapter-state` 的 text 为 **poweredOn**；失败断言的 `evaluatedCommand` 却仍是 `${'^'+output.initialState+'$'}` 字面串 |
| CLI/JUnit | CLI **exit 1 / 35.068s**；JUnit **1 failure / 31.581s**，testcase `ERROR` |
| verdict | `scenario_status=failed`、`automation_status=failed`、`phone_status=failed`、`status=failed`；primary 为 `RuntimeError: Maestro CLI exited 1`；root identity/snapshot 为空，不能把 before receipt 冒充完成后的证据 |
| cleanup | automation `close.status=passed`、CLI reaped、helpers/instrumentation 缺席、forward/settings/permissions 恢复；candidate 原身份 finalization `force-stop` 成功；两组 cleanup errors 均为空，phone lease 已释放 |
| 主机 | 最后 host 只读 listener 检查：5037 仍 **PID 16841**；5038/8937 无 listener；未重启共享 ADB 或关闭其他客户端 |

原始设备输出 `.work/logs/maestro-lifecycle-device.log`。`phone_status=failed` 保存场景 primary，不等于 cleanup 失败或 quarantine；清理回执需单独读。成功 idle 的旧页面已经在上述独立退休中关闭；两次失败 lifecycle App 也已清理。后续修正 candidate 在 §5 完成时按规则保留成功页面；该 PID **16829** 随后已独立精确退休，见[同 APK Appium 对照与恢复](2026-11-09-formal-android-appium-comparison-recovery.md)，不能再作为 retained App 接管或重复退休。

## 4. 已定位的软件原因与修正

本地 pinned JAR 字节码确认：`ElementSelector.evaluateScripts` 调用 `maestro.orchestra.util.Env.evaluateScripts(String, JsEngine)`。其插值识别正则为：

```text
(?<!\\)\$\{([^$]*)}
```

因此 `${'^'+output.initialState+'$'}` 内部的 `$` 使整个插值不匹配，原字符串被保留。不是 BLE 初始化失败，也不是 `copyTextFrom` 调用了系统 clipboard；该版本从 UI tree 读取 text 到内部 `maestro.copiedText`。

修正为：

```yaml
text: ^${output.initialState}$
```

三个变量共 **12 处断言**同步修正，完整正则锚定不变，不放宽 snapshot continuity。当前 flow SHA256：`57316f4a5ded966b78b7d1f6976474cb2f2e37d724dd3cd2c28279887c2c82bc`。

离线 probe 直接实例化原版 `GraalJsEngine`，经原版 `Env` 比较旧/新表达式，对九个枚举字符串和正式三个变量路径均检查旧式保持 literal、新式展开为完整 `^value$`。另确认 `EvalScriptCommand.evaluateScripts` 保留 command/scriptString，赋值由 `Env` 完成；真实 sequence 16 的 metadata 与此一致。没有复制 JS 解释器，也没有改本机第三方依赖。

- 字节码：`.work/logs/maestro-lifecycle-{env,selector}-bytecode.log`。
- 原 probe：`.work/maestro-lifecycle-expression/{MaestroExpressionProbe.java,run.log}`。
- 精确变量、有断言的补充 probe：`.work/maestro-lifecycle-expression/{ExactSnapshotExpressionProbe.java,exact-compile.log,exact-run.log}`，用既有 Pixi JDK 的 `javac -cp` / `java -cp` 和锁定发行包 JAR 执行，均 exit 0，无设备命令。

旧 failed flow digest `f52ada1bb73773079cd4d408f6621f3bbfb6420858cb87cc1e690c7aed4f805f` 仅允许 **已经封存、仍 failed、无 UI/automation pass** 的离线重放，且 captured flow bytes 必须匹配；不能用于新 prepare/publish 或升级 verdict。

真实失败 bundle 的 **170 个文件 SHA256 全部未变**，修复前后只读 verify 输出一致。manifest SHA256 `a14e99067b9776f2adbc644c3347b0c5b351f6194d7c21ef14419f7a9e57ec11`；result SHA256 `a660b9bb279bcb70ddd803689ac795ea4e5b2e3e0d9c822bb2ed4a563647dc57`。原失败、截图/hierarchy、缺少 Close marker 的事实均保留。

## 5. 修正候选的完整 lifecycle comparison pass

在前一轮 UI 失败后，没有接管或重跑旧 attempt；先把 Close 操作条置于快照前，再把四项初始化快照改成紧凑 label/value 行，确保小屏 ScrollView 视口同时包含 Close 和三项连续性字段。重新跑正式 RN tests/typecheck、Maestro owner/runner/evidence tests，并用新 release APK 执行一次 fresh candidate：

| 项目 | 实际观察 |
| --- | --- |
| attempt / scope | **`9ff9215d-665d-44c5-9d73-fddfcb51dd79` / `formal-android-maestro-lifecycle`** |
| APK | 新 release APK SHA256 **`c96fc9909ed056e33e1ce47927639da742614294a60114d8ae546ee49255a584`** |
| 原 App identity | `com.blehubblenghost`，PID **16829**、UID **10378**、start ticks **246864984**、boot `35df0f9f-6c54-42b0-800c-3c410469b6e7`；before/after identity 保持一致 |
| startup snapshot | `state=poweredOn`、`scan_authorization=denied`、`connection_authorization=denied`；完整字段连续性通过 |
| UI chain | 初始 idle 浏览、Start、started snapshot、Tests/Evidence 浏览、Current run 同一 snapshot、Close、closed、最终 Evidence 全部通过；无 tap retry |
| Close markers | `js.close.call` → `js.close.promise` → `js.close.receipt` → `js.close.finally` 全部出现且顺序/单调时间通过 |
| result | `scenario_status=passed`、`automation_status=passed`、`phone_status=passed`、`status=passed`；`primary_error=null`、cleanup errors 为空 |
| cleanup | CLI reaped；helper/instrumentation 缺席；forward、settings、BLE permissions 恢复；phone lease 为空 |
| host/RF | `host_gate_status=pending`、`rf=not_run`；5037 仍 PID **16841**，5038/8937 无 listener；没有重启共享 ADB、刷固件、切 radio 或执行 BLE scan |
| sealed bundle | **188 files**；manifest SHA256 `352c153eb075c3db61c920bddb441fce45a4a1722f2c1da8b75cf6b80bf6b8c5`；result SHA256 `a7a33a66dacf8254ecb0cee682fcf12040a29bb07f4277b8b54d73e645199ded`；独立 offline replay 通过 |

这证明了当前 reviewed APK 在该一次 Android/Maestro 2.12.1 session 上可以完成 UI lifecycle 和 host cleanup。它**不**证明原 Appium `Evidence click → ADB service_accept timeout` 根因已经消失，也不证明 Appium lifecycle、native resource-zero、Hermes reload、scan/RF、ABI 或 release gate。该次成功页面按规则保留；后续已完成独立 exact-identity retirement，并执行了一次失败的同 APK Appium 对照，原失败与独立恢复见[后续报告](2026-11-09-formal-android-appium-comparison-recovery.md)。Maestro 的 pass 不升级该 Appium verdict。

## 6. 实际验证与剩余边界

已按 test-first 留下 owner/lifecycle、helper orphan 和 expression 边界 red，再修正转绿；synthetic SDK/CLI 在实际 subprocess 边界隔离，artifact inspector、prepare/publish/verify 保持真实执行。此前只做 YAML parse/serialization 不能证明表达式实际求值，这次以原版 JVM probe 补证，不把 synthetic evaluatedCommand 当外部库行为证明。

最终定向命令：

```sh
pixi run python -m unittest tools.tests.react_native.test_maestro_android tools.tests.react_native.test_maestro_idle_runner tools.tests.react_native.test_maestro_idle_evidence tools.tests.react_native.test_maestro_lifecycle_evidence tools.tests.react_native.test_maestro_toolchain tools.tests.react_native.test_run_maestro_idle tools.tests.react_native.test_recipe_wiring -q
pixi run python -m unittest tools.tests.react_native.test_bleng_android_candidate tools.tests.react_native.test_android_sdk -q
```

结果分别 **112 tests / 34.189s / OK**、**99 tests / 106.739s / OK**，是有重叠依赖的定向组合，不相加为功能覆盖率。日志 `.work/logs/maestro-lifecycle-{final-regression,candidate-sdk-regression}.log`。

- `pixi run just react-native formal-host-check`：typecheck/autolinking/no-internal 通过。
- `pixi run just react-native formal-host-test`：**57 tests / 2 suites** 通过；legacy `host-test`：**621 tests / 17 suites** 通过，不能据此删除 legacy。
- `pixi run just react-native tools-test` 的全量尝试分别在 120s/240s 上限终止；quiet 日志未定位具体测试，**不计全量通过，也不归因本次 Maestro**。已检查/清理本次测试进程组，没有延长重跑；单独四个相关模块 verbose 47 tests / 30.361s 通过仍非全量结果。
- 收尾 `pixi run just fmt`、`pixi run just lint`、`pixi run just docs-check-mermaid`、`git diff --check` 及相关 authored Markdown UTF-8/fences/whitespace/本地链接检查通过。新报告纳入 Mermaid 文档扫描（本次未新增图）；这些不是设备 gate。日志为 `.work/logs/maestro-lifecycle-final-{fmt,lint,docs,diff,authored-docs}.log`。
- 收尾 `formal-host-maestro-lifecycle-verify` 再次独立通过 integrity 回放，结果仍为 failed；日志 `.work/logs/maestro-lifecycle-final-failed-verify.log`。

修正后的 Maestro lifecycle 已由 §5 的新候选独立完成，不再列为待验证；首轮 Start 局部观察及原 failed bundle 不升级为 pass。后续已在同一 `c96fc990…` APK 上完成一次 managed Appium 对照，失败于 Start 后 Evidence 查询，且已独立恢复原 quarantine lease；详见[对照与恢复报告](2026-11-09-formal-android-appium-comparison-recovery.md)。不再将该已执行对照列为待运行，不重复退休 §5 的原 App 或自动重跑失败场景；Maestro 的通过不转移到 Appium。正式 scan 的 `scan → Stop → Close → final Evidence` 同候选全链仍 pending；Maestro 最多替代 UI transport，fixture/permission/lease/ledger/host verdict 继续由既有 owners 持有。生产 Rust/Close/watchdog、public API、默认 Appium 和 internal 删除条件均未变。
