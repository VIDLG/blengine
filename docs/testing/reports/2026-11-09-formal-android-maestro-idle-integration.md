# 正式 Android Maestro idle：具名 opt-in 接入与真机候选

日期：2026-11-09。范围：macOS controller → 唯一正式 RN App 的 Android idle 页面浏览；**新维护入口已有一个 fresh 真机 UI/身份/cleanup/sealed-directory 候选通过，不是默认框架采用批准、完整 host/RF gate 或 Phase exit**。

## 1. 结论与证据边界

已将此前 ignored Maestro idle 实验的受限能力接入锁定工具管理、具名 owner/runner、薄 CLI、维护 YAML 和独立 directory evidence bundle。Appium 仍是默认且维护中的 UI 路径，既有 lifecycle/scan gate 不变；没有 Detox、新 DUT npm dependency、第二 App、BLE API/ABI 改动或 RF 场景扩展。

| 层次 | 当前事实 | 不可推出的结论 |
| --- | --- | --- |
| 软件组合 | 两组定向回归通过，见 §4 | 全量工具测试或设备通过 |
| host-only 工具准备 | 从既有本地 ZIP 显式离线安装；只读 report 核对 202 个 pinned files | Java/Maestro 可运行、helper 已部署或设备 authority |
| 新 maintained runner 真机 | **`fd6b95d9-5616-458f-b78d-df0f2d790473` 同 session idle/UI、automation cleanup、phone 与 sealed directory passed，独立离线 verify 通过** | Start/Close、scan/RF、持续可见性、完整 host gate 或其他候选通过 |
| 旧物理实验 | 11-08 的独立 session `1c3b6973-1fee-493c-882b-667ee0f1a243` 曾完成 idle UI/identity/cleanup | 新 runner、sealed directory bundle 或 maintained gate 的设备通过 |
| 发布边界 | `host_gate_status=pending`、`rf=not_run` 是新 scope 的固定边界 | native HIL、scan/RF、resource-zero、ABI freeze 或阶段退出 |

旧实验的完整事实、原始失败与采用限制仍以[11-08 工具评估](2026-11-08-rn-test-responsibilities-and-ui-tool-evaluation.md#当前完整-idle-候选ui公开身份-checkpoint-与-cleanup-同-session-通过)为准，不改写历史报告，不用新 verifier 追溯封装旧实验为新 scope。

## 2. 实现归属与安全契约

| Owner / 文件 | 本候选职责 |
| --- | --- |
| [`tools/maestro_toolchain.py`](../../../tools/maestro_toolchain.py) / [`tools/maestro/lock.json`](../../../tools/maestro/lock.json) | 显式本地 archive 校验/提取、lock-specific home、helper hashes、只读 inventory；没有下载、Java 执行或设备探测 |
| [`maestro_android.py`](../../../tools/react_native/maestro_android.py) | 独占 CLI/helper 生命周期、固定 endpoint/ADB daemon 观察、原身份 checkpoint、安装及精确 teardown |
| [`maestro_idle_runner.py`](../../../tools/react_native/maestro_idle_runner.py) | 仅 idle 的 named non-RF 编排：冻结 APK、phone lease、candidate、automation、收尾与发布 |
| [`run_maestro_idle.py`](../../../tools/react_native/run_maestro_idle.py) | 薄 `run` / `verify` CLI，不复制 SDK、身份或场景逻辑 |
| [`maestro_idle_evidence.py`](../../../tools/react_native/maestro_idle_evidence.py) | strict 官方 artifact reader、预释放 evidence 准备、sealed **directory** bundle 与离线完整性重放 |
| [`idle-navigation.yaml`](../../../tests/consumers/react-native-bleng/e2e/maestro/idle-navigation.yaml) | 唯一维护 flow：固定 testID、页面/idle/无 run 断言、七次点击禁止 retry |

运行期身份使用既有 `AndroidCandidateOwner.inspect_running_identity(record_command=...)`；可选 callback 只被动保存共享 SDK read hook 的命令审计。没有私有接口访问、第二份 live identity codec、重新认领已有进程或 WebDriver 绑定；该 host-tool checkpoint 不改变生产 BLE ABI，也不证明 foreground、UI 或 native terminal。

准入要求单一 user 0、明确物理 USB serial、reviewed APK/hash、既有 Pixi JDK、实际共享 ADB daemon PID，以及独立 App/helper 安装授权。拒绝旧运行 App、预存 Maestro helper、未知 instrumentation/lease、共享 UID、冲突 listener/forward；不自动退休旧 owner、重启 ADB、关闭外部客户端、reset、grant DUT BLE、改 radio/firmware、重跑场景或使用 cloud。CLI 使用本轮 HOME、受限 PATH、固定本地 endpoint，关闭 analytics/update check；helper 由 host 显式安装，CLI 使用 `--reinstall-driver=false`。

### UI 证据与 verdict 分层

flow 从 Tests/idle 开始，执行 **Evidence → Current run → Tests** 两遍，再到 **Evidence**；七次 page/idle 断言和最终 `No run has been requested.` 必须通过，不调用 Start、Close、scan 或 BLE。reader 不只信 CLI exit：核对 reviewed YAML、官方 commands 的完整顺序/COMPLETED/禁止 retry、精确状态断言、单 flow JUnit，以及官方 device logcat 中**原 App PID 的 14 条、七组 request→commit**。commit 只是 React effect 的 JS commit，不是 native frame/accessibility/RF ACK；页面断言不可省略，各时钟也不能混作同一时基。

`scenario_status`（逻辑 UI）、`automation_status`、cleanup errors、`phone_status` 与总 `status` 分开，host gate 保持 pending、RF 保持 not_run。成功保留本轮原 App/Evidence；automation 或 phone 收尾失败不能因此擅自关闭成功页面。失败场景的 App 退休仍须原 candidate 的 fresh identity proof；unknown identity 不做猜测性清理，保留 quarantine。

### Teardown、错误与 directory bundle

按 automation → candidate App → prepared evidence → phone terminal journal/release → directory seal 顺序收尾。两个 helper 在 scoped force-stop/uninstall 前重新核对本轮 exact APK/path/hash/UID 和 boot；随后检查包/UID 进程缺席、instrumentation、forward/listener 以及设置/权限基线。旧实验曾观察 active 末期 `accessibility_enabled=1`，owned helper 全部退出后回到原值 0：这说明恢复判定须在完整 teardown 后，不说明可省略恢复检查，也不授权 settings 写入或全局 reset。

DUT 权限基线采于本轮显式 App 安装之后、helper 安装之前；恢复判定不冒充安装前后权限等值证明。`primary_error` 与完整 `primary_traceback` 保留 SDK 原因链；nested cleanup errors、phone cleanup errors 与 interruptions 独立保存，不用 cleanup 异常覆盖 primary。prepare callback 中的 `KeyboardInterrupt` 也须在 quarantine/证据发布后重新抛出，不能因 phone owner 将异常 journal 化而吞掉。

`prepared-result.json` 在 phone release 前冻结当时的非 phone 证据；运行中的 `phone.json` 是明确允许终态替换的 mutable journal，最终 manifest 再封存它。`result.json` / `manifest.json` 及目录 inventory、文件 hash、SDK 原始命令事实共同重放。manifest/inventory 和 SDK receipts 都不是当前设备 authority。离线 verifier 可返回 `integrity=verified` 同时保留已记录的 `status=failed`，不因此升级 verdict；不套用 Appium DELETE verifier，也不把旧 ignored 实验追溯 seal 为新维护 scope。

## 3. 维护入口与使用边界

具体参数表见[正式 consumer 使用说明](../../../tests/consumers/react-native-bleng/README.md#opt-in-android-maestro-idle)。四个入口均由 [`just/react-native/mod.just`](../../../just/react-native/mod.just) 维护：

| Recipe | 范围 |
| --- | --- |
| `pixi run just react-native maestro-install ARCHIVE` | **显式 LOCAL offline extraction only**。`ARCHIVE` 是说明性参数名，不是可直接执行的设备命令；必须替换为已存在且锁定 hash 匹配的本地 ZIP。绝不下载或接触手机 |
| `pixi run just react-native maestro-report` | 只读 installed inventory，不执行 Java/Maestro；缺失/漂移要求显式 setup，不自动修复 |
| `pixi run just react-native formal-host-android-maestro-idle --help` | 仅查看新 opt-in runner 参数；真正 `run` 另需 fresh 授权和真实身份，不能使用占位值 |
| `pixi run just react-native formal-host-maestro-idle-verify ATTEMPT` | `ATTEMPT` 是已存在的新 scope attempt 目录；纯离线完整性，不访问设备、不将 failed 改 passed |

工具 root 为 `BLEHUB_MAESTRO_ROOT` → ignored `[toolchains].maestro_root` → `~/.cache/blehub/toolchains/maestro`；实际 home 为 lock-specific child。install 拒绝任何已存在的 home，包括相同版本；已有候选用 report 验证，partial/foreign home 不自动覆盖或修复。`--java-home` 必须指向已有 Pixi JDK，不安装 Java/SDK。默认 driver port 8937、scenario timeout 180 秒（最大 300）、cleanup 90 秒（最大 120）。没有新 DUT npm dependency；Jest 仅发现 `src/**/*.test.[jt]s?(x)`，不加载 `e2e/maestro/*.yaml`。

本候选 lock：CLI **2.12.1**，commit **`67d8a677029261fa424a45a10d08eacd6aa0deed`**，archive **349832427 bytes**，SHA256 **`54dbb0716425626604973cbb374c585ca30272375d9ce15177a1152765ddd9d7`**。URL 仅为锁定 provenance，不授权自动下载。

## 4. 本地已见验证与原始 red

以下为本次实际执行并保存的日志；定向组合不等于全量 discovery。两组回归的实际命令分别为：

```sh
pixi run --locked python -W error::ResourceWarning -m unittest \
  tools.tests.react_native.test_maestro_toolchain \
  tools.tests.react_native.test_maestro_idle_evidence \
  tools.tests.react_native.test_maestro_android \
  tools.tests.react_native.test_android_sdk \
  tools.tests.react_native.test_recipe_wiring \
  tools.tests.react_native.test_layout -q

pixi run --locked python -W error::ResourceWarning -m unittest \
  tools.tests.react_native.test_bleng_android_candidate \
  tools.tests.react_native.test_android_sdk \
  tools.tests.react_native.test_bleng_android_prepared_scan \
  tools.tests.react_native.test_android_managed_host -q
```

| 日志 | 实读结果 / 范围 |
| --- | --- |
| `.work/maestro-managed-software-tests.log` | **110 tests / 18.242s / OK**；toolchain/evidence/owner/SDK/recipe/layout 组合 |
| `.work/maestro-managed-existing-regression.log` | **112 tests / 197.177s / OK**；candidate/SDK/prepared scan/managed host 回归 |
| `.work/maestro-managed-recipes-red.log` | **1 test / 0.018s / FAILED (errors=1)**；缺少 `maestro-install` recipe，Just dry-run 非零 |
| `.work/maestro-managed-interruption-red.log` | **1 test / 0.990s / FAILED (failures=1)**；`test_pre_release_interruption_is_rethrown_after_quarantine_and_publication` 复现 `KeyboardInterrupt not raised`，随后修正 prepare callback 的 interruption capture |
| `.work/maestro-managed-offline-install.log` | 从既有 archive 离线安装到 `.work/managed-maestro/cli-2.12.1-ec9482df28e3f174`，202 个 pinned files |
| `.work/maestro-managed-static-report.log` | 相同 home、202 个文件；`verification=static-inventory-only; no execution or device attestation` |

安装/report 通过 `BLEHUB_MAESTRO_ROOT` override 完成，复用原始已下载 ZIP，没有新网络下载、Java/Maestro 执行或设备命令。上述组合测试有重叠，不相加充当覆盖率。

集成测试还修正了这些具体边界：不受支持的 instrumentation 查询改用共享 observer 的实际可解析输出；candidate install receipt 按真实字段处理；原始 cmdline 的多 NUL padding 沿用 SDK 接受规则，不能误拒合法身份；phone journal 的终态替换单独允许并复验。原失败保留，没有放宽 UI、原 identity、lease、cleanup 或恢复契约。

## 5. Maintained runner 首个 fresh 真机与独立离线重放

用户继续授权后，先用 App-only lease **`87c9da8a-41d1-4962-a5ac-44c7458667cf`** fresh 核对旧 retained App（PID 28598、UID 10378、start ticks 244323303、原 boot/installed path/APK hash），检查单 user 0、非 shared UID、无 Maestro helper/instrumentation/forward 后，仅提交一次该已知 package/user 0 的 force-stop。原包/UID 进程缺席、DUT 权限未变及 phone lease 释放均已验证。该显式退休与普通 runner 分开，未将旧 App/session 认领为新 run，也不把进程退休计为 Close/resource-zero。

退休证据：`.work/maestro-maintained-acceptance/retirement-87c9da8a-41d1-4962-a5ac-44c7458667cf/`；完整输出 `.work/maestro-maintained-retirement.log`。随后通过现有 `formal-host-android-maestro-idle` Just/CLI 执行**一次**新 attempt，无失败重试、无 APK rebuild、无固件操作或新下载。

| 观察 | 当前实际结果 |
| --- | --- |
| attempt / scope | **`fd6b95d9-5616-458f-b78d-df0f2d790473` / `formal-android-maestro-idle`** |
| 候选 | 同一 reviewed APK **`cf10697246fd4a7d141182a295dd378865637ac16c9c06d25d540cd3b1814a9c`**；managed Maestro **2.12.1**，既有 Pixi Java home；没有新构建或 host tool 安装 |
| 原 App 身份 | `com.blehubblenghost`，PID **17666**、UID **10378**、start ticks **244973575**、boot **`35df0f9f-6c54-42b0-800c-3c410469b6e7`**。before/after CLI 与 finalization 均检查本轮原身份，无重认领 |
| UI / CLI | CLI **exit 0 / 29.774s**；JUnit **1 SUCCESS / 26.173s**。七次指定导航和全部 page/idle/无 run 断言完成，七次 tap 禁止 no-change retry，原 PID **14 条、七组 request→commit** |
| 恢复 | 记录 `accessibility_enabled` **0 → 1 → 0**；九项观察设置恢复原值，DUT Bluetooth permission/UID readbacks 与本轮安装后基线一致，没有 settings 写入、DUT BLE grant/revoke 或 radio 操作 |
| automation | CLI 已 reaped，两个 exact helper 包/UID 进程缺席，instrumentation 空、原空 forward baseline 恢复，所需退休/恢复 flags 均为 true，`no_mutations_dispatched=false`、errors 为空 |
| verdict | `scenario_status/automation_status/phone_status/status=passed`，`primary_error/primary_traceback=null`，两组 cleanup errors 为空；**`host_gate_status=pending / rf=not_run`** |
| sealed directory | **184 files**，prepared evidence 先于 phone release，最终 phone journal/result/manifest 重放一致；没有往封存目录里补写摘要或改写历史失败 |
| 主机/phone | 5037 listener 仍为原 **PID 16841**，5038/8937 无 listener，活动 phone lease 为零；没有重启共享 ADB 或关闭其他客户端 |
| 成功 App | candidate finalization 为 **retain/passed**，保留本轮原进程；flow 最终 Evidence 可见通过。cleanup 后持续页面可见性未另行 UI 验证，不用进程存活冒充该证明 |

真实目录：`.work/formal-android-maestro-idle/fd6b95d9-5616-458f-b78d-df0f2d790473/`；执行日志 `.work/maestro-maintained-device.log`，只读提取摘要 `.work/maestro-maintained-device-summary.json` 保存在封存目录之外。源 manifest SHA256 为 **`02dd4322b0de444e3a130bde0cb7fdae7664afd307e1c14ce19e8f64054a2532`**，result SHA256 为 **`908c5572e2e71e1d2eba1307bc7305c609ba1ce7c25ea207f4fd05f262e5b44e`**。

独立执行以下实际命令，**integrity verified / 原 passed verdict 保持**；不是另一次设备运行，也未升级 pending host gate。完整输出为 `.work/maestro-maintained-offline-verify.log`：

```sh
pixi run --locked just react-native formal-host-maestro-idle-verify \
  /Users/vision/projects/blehub/.work/formal-android-maestro-idle/fd6b95d9-5616-458f-b78d-df0f2d790473
```

设备候选与文档更新后实际执行 `pixi run --locked just fmt`、`pixi run --locked just lint`、`pixi run --locked just docs-check-mermaid`、`git diff --check`，以及相关 authored Markdown 的 UTF-8/fences/whitespace 检查，均通过；日志 `.work/maestro-maintained-device-{fmt,lint,docs}.log`。本轮没有修改 maintained runtime/controller 实现或测试，未重复执行 §4 的软件测试组合；也没有在封存目录内补写文件。

这次证明的是**新维护入口**的同 session idle 范围、受控生命周期和证据发布/重放，不是旧 ignored 实验 pass 的继承。它未执行 Start/Close/scan，没有复现或宣称修复原 `Close returned → Evidence blocked → ADB service_accept timeout` 根因；没有 RF、native HIL、resource-zero、reload 或 ABI exit 证明。后续应回到功能场景迁移与正式 scan/lifecycle 闭环，不以反复 idle 计数替代缺失覆盖。

## 6. 当前保留现场与剩余边界

**后续现场更新：** 下述 PID 17666 是本 idle 候选结束时的历史现场，已在后续独立授权 lease 中 fresh 核对并退休。后续 lifecycle 首轮 Start 后断言失败，修正后的新候选最终完成 Start → Close → Evidence comparison pass；最新报告记录的 retained App 为 PID **16829 / UID 10378 / start ticks 246864984**，不是本次实时设备核验或后续接管授权。原 idle 和 lifecycle 失败证据不变，完整身份及候选边界以 [lifecycle 报告 §5](2026-11-09-formal-android-maestro-lifecycle.md#5-修正候选的完整-lifecycle-comparison-pass) 为准。

本 idle 候选结束时 retained App 是 §5 新候选的 **PID 17666 / UID 10378 / start ticks 244973575**，原 boot/APK 不变；phone lease 已释放。旧 PID 28598 已在独立授权退休中确认缺席。新值仍不是后续运行的自动授权，普通 runner 必须拒绝接管它。

任何下一次设备运行仍须遵守明确授权、fresh 身份核对和精确退休边界，以新 lease、新 attempt 和 reviewed 输入执行；普通入口不自动 retirement、adopt 或重跑。后续成功 App 按对应候选的保留策略处理，不能凭本报告关闭或接管。

相关 authored Markdown 的 UTF-8、围栏平衡、尾随空白、冲突标记、末尾换行与链接目标均须保持有效；本次没有新增 Mermaid 图。这些文档检查不是设备验证。

最终实际执行：

- `pixi run --locked python -W error::ResourceWarning -m unittest discover -s tools/tests/react_native -t . -p 'test_maestro*.py' -q`：**62 passed / 17.255s**。这是 canonical discovery 下的新 Maestro 子集，不是整个 `tools-test`；与 §4 组合有重叠，不相加。
- `pixi run --locked just react-native formal-host-check`：typecheck/autolinking/no-internal 通过；`pixi run --locked just react-native formal-host-test`：**57 passed / 2 suites**。
- `pixi run --locked just fmt`、`pixi run --locked just lint`、`pixi run --locked just docs-check-mermaid`、`git diff --check` 与相关 authored Markdown UTF-8/fences/whitespace 检查通过。diff check 只有既有文件的 CRLF→LF 提示，无 whitespace error。
- 软件接入阶段的 `pixi run --locked just react-native formal-host-android-maestro-idle --help` 正常显示参数，没有设备命令；该阶段没有 fresh 读取或接管 retained App。后续明确授权的实际退休与设备执行独立记录于 §5。

最终软件日志为 `.work/maestro-managed-final-{discovery,host-check,host-test,fmt,lint,docs}.log`。本次未执行全量 `tools-test/tools-fixture-test`、native build/runtime、Hermes reload 或任何 RF，不把定向结果称为全仓通过。

新 runner 的当前 CLI/JDK、同 session idle UI/identity/helper teardown/phone release 与真实 directory bundle 已有 §5 的限定设备候选。成功页面持续可见性、Start/Close 与 scan/RF 场景、Hermes reload、native/runtime/ABI 及阶段退出覆盖仍未由此证明；现有 Appium lifecycle/scan 默认与 internal 能力迁移/删除条件不变。
