# Formal Android：真实 managed Appium lifecycle 候选

日期：2026-11-04。真实 Mac、本地 Pixi/Just、未提交工作树。唯一 Android 手机 `3ac5cc09` / PHB110 / API 36；boot `35df0f9f-6c54-42b0-800c-3c410469b6e7`。依据：[Plan](../../../.plan/PLAN.md)、[前序 helper 语义恢复](2026-11-04-formal-android-helper-restoration.md)。本轮使用已授权安装及 helper 临时准备，不授予 DUT BLE 权限、不切换 radio、不注入定位、不操作固件、不执行 scan/RF。

## 最新实际结果

Run：`da9a7148-57cc-448d-8966-40fc04e7f74a`。

正式 release-shaped APK SHA-256：

```text
02ad94fef0a904d667930682a078093d12c75f11aae414fe472414483f71d0da
```

冻结 APK、全部 command/identity/session/helper/UI/cleanup evidence 在 ignored 目录：

```text
.work/formal-android-session-da9a7148-57cc-448d-8966-40fc04e7f74a/
```

| 边界 | 实际结果 |
|---|---|
| 显式 helper prepare、真实 platform owner 准备 | 通过 |
| frozen APK root-aware 安装、installed hash、COLD launch、原 OS identity | 通过 |
| 本轮 local Appium child、实际 WebDriver session 创建及 host/device/native 绑定 | 通过 |
| 正式产品 API 的 `idle → started → closed` | 通过 |
| Tests / Current run / Evidence 浏览不改变 session/初始化证据 | 通过 |
| 最终 Evidence 中 start/close 成功记录和初始化快照 | 通过 |
| 本轮 session DELETE、local server reap、平台 idle/settings finalizer | 通过 |
| App 成功保留、helper 临时文件删除、AppOps 恢复、phone lease 释放 | 通过；cleanup errors 为空 |

实际初始化快照：

```text
state=poweredOn
scan_authorization=denied
connection_authorization=denied
```

这三个事实独立；没有为了让结果好看而把 denied 改成 authorized。最终 UI tree 中 `formal-host-title` 正确、`session-state=closed`、`workbench-page=Evidence`，`lifecycle-evidence` 为 `1. start: succeeded` / `2. close: succeeded`。`success-evidence.xml/png` 在 DELETE 前采集，后续 native App finalizer 验证原身份并 retain，不宣称持续屏幕可见性或跨 reload 持久化。

顶层 `scenario_status=passed`、`phone_status=passed`、`automation_cleanup=passed`、`helper_restoration=passed`；**`host_gate_status=pending`、`rf=not_run` 保持不变**。这不是完整 RF bundle、native resource-zero、clean npm artifact consumer、Hermes reload、ABI/Phase exit 或 internal 删除依据。

## 本轮接入的具体 owners

- [`AndroidHelperPreparation`](../../../tools/react_native/bleng_android_preparation.py)：显式授权、实际同 run phone lease、真实 inventory，拥有单个 helper package AppOp 与本次 noclobber store；固定安装/boot/file identity，记录 intent 与原始 before/after，独立 cleanup budget、one-shot retirement、未知变更/publication 不当作成功。不释放 phone，也不成为通用设置框架。
- [`AndroidAutomationOwner.require_helper_restoration`](../../../tools/react_native/bleng_android_session.py)：helper rollback 只能接同 phone/run/home/hash/port 的实际 owner；允许已闭合或确定尚未提交 session 的准备失败，requesting/binding/unknown 状态仍拒绝。避免在从未调用 Remote 的失败路径上伪造 DELETE 或制造假 cleanup failure。
- [`AndroidCandidateOwner`](../../../tools/react_native/bleng_android_candidate.py)：显式 `use_root=True` 复用既有 SDK/installer 的 push + `su -c pm install`；不探测后 fallback、不二次安装、不加 `-g`。日志/receipt、frozen hash、UID/path/COLD/native identity 保留。安装启动成功后即使 driver 尚未 bind，已有原 native identity 也可在 fresh identity 核对下清理本次 App；未知/缺失 identity 仍拒绝。
- [`android_sdk.install_apk`](../../../tools/react_native/android_sdk.py) / [原 installer](../../../tools/android/run_instrumentation.py)：可选 absolute deadline / owning guard / cwd，使每条 install 与本次 upload cleanup 命令前后都检查原 lease/candidate；删除失败或未知结果不算安装成功。复用原 root-aware installer，不另建包管理器。
- [`exercise_lifecycle`](../../../tools/react_native/run_bleng_ui.py)：复用已有 `_WorkbenchUi` 与同一共享产品场景，接 cheap live authority、fresh App identity checkpoints 和总 deadline；不加第二套 Android-only UI 流程。

实际组合顺序为 helper prepare → platform prepare → candidate install/launch → owned Appium/session → candidate bind → shared lifecycle UI → session/server/platform close → owned App finalization → helper restore → evidence → phone release。

本轮组合仍由 ignored one-shot host attempt 驱动并调用正式 owners，不是新增 maintained scan CLI 或完整 outer bundle runner；没有 live runner import synthetic/TestCase。新的 preparation tests 自动包含在既有 `tools-test` 的 canonical discovery，不增加重复入口。

## 实证驱动的三个根因修复

### Android argv NUL padding

旧 `read_app_identity` 要求 `/proc/<pid>/cmdline` 等于包名加恰好一个 NUL。真机读回是正确包名后多个 NUL（记录长度 99 字节），导致第一次安装/COLD launch 后误拒绝 identity。

现在只允许 **exact package + 一个或多个尾部 NUL**，规范化结果仍是原包名；缺终止符、worker、前后缀、空白、额外非 NUL 参数全部拒绝。原 PID/start ticks/UID/boot/hash 与 reverse sweep 不变；没有重新选 PID 的 fallback。

### Android edge-to-edge 标题被系统栏遮挡

旧 release 的 RN 正常运行，但 UI tree 中导航/idle/content 可见，最顶部标题缺失。实际布局使用 RN 核心 `SafeAreaView`，Android 未得到 system-bar insets，标题落在状态栏覆盖区域。没有删标题断言或延长 timeout。

[正式 consumer App](../../../tests/consumers/react-native-bleng/src/App.tsx) 现用精确锁定的 `react-native-safe-area-context@5.11.1`，稳定的 provider/initial metrics 与四边 native SafeAreaView；Jest 用库官方 native mock，补非零顶部/底部及侧边 inset 接线、pending owner 跨页面保持回归。owner/scan 状态仍位于页面切换之上。

这是 consumer UI 依赖，不是生产 BLE backend 的新抽象。新增 native UI 依赖后，**iOS 仍须刷新 Pods 并重新 build/runtime 验证**；本轮没有改 iOS Pod lock 或将 Android 结果计入 iOS。

### Nitro 库加载不等于 HybridObject 注册

安全区修复后真实点击 Start 暴露 `BlengSessionOwner` 未注册、registry `[]`。本机 Nitro Modules/Nitrogen 0.37.1 源码和原 APK ELF 检查确认：RN PackageList 已实例化正式 package，Kotlin OnLoad 已加载 shared library，但 `.so` 没有调用生成注册函数的 `JNI_OnLoad`。

[`cpp-adapter.cpp`](../../../integrations/react-native-bleng/android/src/main/cpp/cpp-adapter.cpp) / [`CMakeLists.txt`](../../../integrations/react-native-bleng/android/CMakeLists.txt) 补最小 `JNI_OnLoad → fbjni initialize → registerAllNatives()`。**这只是 Nitro 要求的 JSI/JNI 注册 glue，不恢复自制 C++→Rust ABI；业务仍是 Kotlin → UniFFI → Rust Supervisor/BLeng。**

原 APK 缺入口为符号 red，新 APK 两个 ABI slices 导出入口并有正确 objdump 调用路径为静态 green；本次真机正式 `createBlengSessionOwner/start/close` 又验证了实际注册与产品通路。consumer instrumentation 新增的真实 JNI backing 回归已编译，**未执行 instrumentation**，不将该构建本身当运行证据。

## 失败候选全部保留

| Run / APK | 结果与后续处理 |
|---|---|
| `8a3122d2-ce6a-4685-a301-0ee3c201aaa4` / `e759…` | 安装/COLD launch 成功，cmdline parser 拒绝 NUL padding；无 Appium session。helper 恢复成功，因缺完整 App identity 而 quarantine。 |
| recovery `51d5086f-0383-4e3f-a258-462170c645c0` | 以本次 COLD receipt、保存的 post-failure PID/start ticks/UID/cmdline 原始诊断及修复后的 fresh expected-identity 核对，只停止该已知 App；验证 helper 已恢复，归档释放 exact 旧 lease。原失败文件不改写。不是任意未知 App/PID adoption。 |
| `e516da31-2a37-45d4-b880-c74c568ad941` / `e759…` | Appium host/device/native binding 通过；标题等待失败。DELETE/platform/App/helper 清理全通过，phone 正常释放，主结果仍 failed。 |
| `cd8be78b-587d-4353-b51e-952153fe893c` / `e759…` | 为定位问题新鲜保存 guarded UI tree/screenshot，确认非定位器/JS 启动问题；同样标题阶段失败，cleanup 全通过。 |
| `f0ca3d36-4c7a-414b-b049-c008618a6e20` / `5bc5…` | 标题修复、idle 通过；Start 报真实 Nitro registry 为空。保留错误，无 snapshot，cleanup 全通过。 |
| `da9a7148-57cc-448d-8966-40fc04e7f74a` / `02ad…` | 最新完整 lifecycle/automation/helper/phone scoped pass。 |

各 run 在 `.work/formal-android-session-<run>/`；App recovery 在 `.work/formal-android-app-recovery-51d5086f-0383-4e3f-a258-462170c645c0/`。每个失败原因先采证、修根因或添加诊断后才开始 fresh attempt；没有延长静默等待、自动重跑破坏性门禁或把失败候选覆盖成成功。

## 工具与证据限制

实际 host 使用本机解析的 Appium 3.9.0、UiAutomator2 driver 8.7.3，三个 helper exact hashes 沿用[前序清单](2026-11-04-formal-android-device-preparation.md)。`APPIUM_HOME`、owned nonce route、host/device 独立 session ID、original server OS identity、一次 live host proxy 与前后 readback 均被记录；不声称公开 API 有显式 host/device UUID mapping，也不声称完整 plugin/dependency/config provenance 已闭合。

成功 run 明确观察到 stock driver 的临时 hidden-API policy 生命周期并在 finalizer 核对原 absent baseline；host 没有自行 root 改写这些 keys。helper 只显式 root 改一个 package AppOp，其他 scope 不合并。`su` 与安装不是架构阻塞；实际 native safety/identity guard 也没有因 root 而取消。

每个物理 attempt 的 scenario 预算 150 秒、独立 cleanup 90 秒，外层 270 秒有界并每 15 秒报告进度；没有外层 timeout。UI phase timeout 20 秒，点击不重试。包级 force-stop 与 OS identity 读回不是原子系统操作，合作 lease 不是强制排他或 attestation；继续保留这些限制。

## 本轮软件/构建验证

- helper scope 初版 **26 passed**，review 后移除真实 serial 硬编码、增加未提交 session 的恢复 handoff；最终 targeted **30 passed**。
- root-aware candidate/SDK/installer/Appium combined：**90 passed**；补未 bind driver 的 App cleanup 后 candidate **49 passed**；argv padding 的 SDK **26 passed**。
- managed lifecycle guard + helper preparation + session combined：**96 passed，51.512 秒**。
- 正式 App safe-area 新回归旧实现 red，修复后 Jest **51 passed**；typecheck/autolinking 通过。
- 正式 Android release（安全区候选及 JNI 候选）均 build 成功；JNI 候选 debug + AndroidTest build 成功。均使用既有 Pixi/Just recipe 和限时日志 runner，未安装 SDK/NDK 工具链。
- 以上为实际执行过的分组，存在重叠，**不可相加为独立覆盖数**。日志在 `.work/formal-managed-lifecycle-{red,green}.log`、`.work/formal-android-helper-scope-combined.log`、`.work/logs/formal-safe-area-*.log`、`.work/logs/formal-nitro-*.log` 与 Just build logs。

最终同工作树组合复验：

```sh
pixi run just --command python -W error::ResourceWarning -m unittest \
  tools.tests.react_native.test_bleng_android_candidate \
  tools.tests.react_native.test_android_sdk \
  tools.android.test_run_instrumentation \
  tools.tests.react_native.test_android_appium -q
pixi run just --command python -W error::ResourceWarning -m unittest \
  tools.tests.react_native.test_bleng_android_preparation \
  tools.tests.react_native.test_bleng_android_session \
  tools.tests.react_native.test_bleng_ui \
  tools.tests.react_native.test_bleng_android_automation \
  tools.tests.react_native.test_android_automation_observation -q
```

- installation/SDK/controller 组 **100 passed，102.238 秒**；owner/UI/parser 组 **135 passed，66.026 秒**。这是有针对性的维护模块组合，不声称全量 `tools-test` 已通过。
- `pixi run just fmt`、`pixi run just lint` 全量通过，含 workspace/all-targets Clippy；Ruff format 报告 301 文件未变。
- `pixi run just --no-deps react-native formal-host-check` 的 typecheck/autolinking 通过；`pixi run just react-native formal-host-test` **51 passed**。
- `pixi run just docs-check-mermaid`：204 份 Markdown、27 个图通过；四份本轮 authored/updated Markdown 的 UTF-8、围栏、尾随空白、冲突标记与本地链接检查通过。
- 最终日志：`.work/formal-android-managed-final-{fmt,install-tests,owner-tests,lint,host-check,host-test,mermaid}.log`。最终 phone lock absent；没有遗留 quarantine，也没有新增 Git commit。

当前没有授权 BLE permission provisioning 或 RF；下一步先补 iOS consumer 依赖/构建验证、可维护 host composition/bundle 和正式 typed scan 的准备交接，再按明确权限与 Peripheral installed-image receipt 执行相应设备/RF 门禁。其余 operations、reload、provider/host ledger 的迁移未完成，**不能删除 internal**。
