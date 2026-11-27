# Android 正式 session：补齐正常关闭的 owned worker 退役

日期：2026-11-10。用户要求回到产品测试主线。审计正式 scan/reload 的前置生命周期时，发现
Android session 自有 HandlerThread 未进入正常退役路径；本轮在原 owner 内修正，增加软件
回归及生产-session instrumentation 用例，并完成无设备构建。初始软件交付阶段没有运行
手机、BLE 或 RF。用户随后明确授权只安装新 APK 并运行 3 项无 BLE 初始化的 native 回归，
实际 **3/3 passed，phone cleanup passed**；后续证据与限制见 §5，不继承为完整 Supervisor、
Hermes 或 RF 通过。

## 1. 根因与分层

**源码已核实：**

- `AndroidPlatformSession.create` 创建并启动自己的 `HandlerThread`，backend 只接收 caller-owned
  Handler。`AndroidBleBackend.dispose` 不停止调用方线程，这是正确的 backend 职责边界。
- 原 session 的 `stopBackendThread` 仅由 `disposeUnassembled` 调用；backend 已转交 Bleng 的
  正常关闭链路没有调用它。
- Rust `OwnerRetirementEvidence.collect` 顺序为 `bleng.shutdown → wait_event_reader →
  resource_snapshot`，即使前一步失败也继续收集后续结果。原 Android `resourceSnapshot`
  只统计 backend 资源，因此正常关闭的自有线程没有被退出或纳入资源门禁。

这说明源代码存在确定的退休缺口；本轮**没有在旧手机候选上动态复现线程泄漏**，也没有
把它当作 [Appium active-session stall](2026-11-10-formal-android-appium4-device-comparison.md)
的根因。此前 UI pass 不证明 native worker/resource-zero。

## 2. 修复

### 保留原顺序，不新增 runtime 或 ABI

`integrations/react-native-bleng/android/src/main/kotlin/com/blehub/reactnative/bleng/AndroidPlatformSession.kt`
继续使用 Rust 原退役回调，不新增关闭 API、第二 native image、线程或 runtime：

```text
Bleng shutdown（原 backend dispose）
→ event reader drain（原回调）
→ 活跃 Handler 上读取 backend snapshot
→ 请求本 session 的 worker quitSafely
→ 原有 5 秒上限 join / 确认线程已退出
→ 返回包含 backendThreads 的资源证据
```

snapshot 必须先于 worker 退出：`AndroidBleBackend.snapshotRfResources()` 自己还需要向该
Handler 同步投递，不能在 `waitEventReader` 提前停线程。

`resourceSnapshot()` 现明确是 terminal retirement barrier，不是活跃 session 的任意诊断入口。
即使 backend snapshot 抛错或返回非法负数，也会尝试退出本 session 的 worker；snapshot 错误
保留为 primary，quit/join 错误作为 suppressed，任一失败不能形成成功资源证据。

### 狭窄的 worker 机制

相邻内部 `OwnedBackendThread` 只接收已存在的 worker 和 quit callback，不创建线程、backend
或新的状态机/注册表。它拥有有界 join、自线程 join 拒绝、原中断保留和 sticky stop 结果。
原 `AtomicBoolean` 在退出尝试前置 true 后，后续调用可能忽略首次 stop 失败；现在重复 stop
复用首次成功/失败，不重发 quit，也不把晚些自然退出重标为首次成功。

`disposeUnassembled()` 仍使用原 backend dispose + 独立 worker stop 的错误收集顺序，复用
同一机制，不在已转交 ownership 的正常路径再次 dispose backend。

### 资源词汇

`AndroidPlatformSessionFactory.REQUIRED_ZERO_RESOURCE_NAMES` 新增内部 `backendThreads`。
原 backend 11 项计数 + session 自有线程共 12 项；只在确认 worker 退出后报告线程为零。
不是新增 canonical BLE DTO 或 JS diagnostics surface，ABI 仍未冻结。

legacy `AndroidProbePlatformSessionFactory` 复用这个实际 session 与 required-name list，
没有复制修复或另建 thread owner；本次没有移除 legacy host，也没有把其既有 RF/Hermes
结果转给 formal host。

## 3. 测试和编译

### 实际执行的纯 JVM 回归

新增 module-neighbor `OwnedBackendThreadTest`，使用受控的真实 JVM Thread 和 latch，不依赖
Android runtime、native image、SDK/ADB 或蓝牙。覆盖 9 项：

1. 活跃线程上的 snapshot 先于 quit，重复 stop 不重发请求。
2. snapshot 失败仍退出线程，保留 primary。
3. snapshot 与 quit 双失败分别保留，失败不重试。
4. snapshot 成功但 quit 失败不能返回成功。
5. join 超时后，即使 worker 后续退出，首次失败仍保持。
6. snapshot 失败 + join 超时保留 primary/suppressed 两份证据。
7. interrupted join 保留原 caller 中断；缓存失败不错误中断另一个 caller。
8. 自线程 join 在发出 quit 前拒绝。
9. 零/负 join budget 在动作前拒绝，避免 `join(0)` 无限等待。

唯一新维护入口：

```sh
pixi run just react-native bleng-android-unit-test
```

入口为原 Gradle `testDebugUnitTest`，带 `--offline --no-daemon`；不添加安装、SDK setup、签名、
手机或 native-instrumentation prerequisite。JUnit `4.13.2` 复用项目已有测试依赖版本。
Just dry-run 回归固定其完整命令与无 setup/device 边界。

实际最终结果 **9 tests / 0 failures / 0 errors / 0 skipped**，0.061s；初版 8 项亦通过。
新 XML：

```text
.work/react-native-bleng-android/blehub-react-native-bleng-android/test-results/testDebugUnitTest/TEST-com.blehub.reactnative.bleng.OwnedBackendThreadTest.xml
```

该 XML 属于本次实际 task；同缓存根下历史 `library/` 的旧测试结果不计入本轮。
这些用例证明 worker 机制，不冒充 Android Looper、Rust Supervisor 或 Hermes 的完整 runtime。
没有声称执行过旧实现的设备 red。

### 生产-session instrumentation：已编译，未运行

唯一 formal consumer 新增 `AndroidPlatformSessionRetirementInstrumentedTest`，3 项测试：

- 实际 production session 经 backend dispose、reader wait、resource snapshot 后，精确 owned
  thread 已退出且 required resources 全零。
- 两次独立 session 退役不遗留上一代 worker。
- unassembled rollback 和重复 disposal 不遗留线程。

测试不构造 Bleng、不调用 initialize/start/scan，不替换整个 `ForeignPlatformOwnerSession`。
只读反射精确 session 的 worker，不扫全局线程或新增 public getter；断言在 finally 兜底
清理之前，防止清理把泄漏洗成 pass。构造 backend 只取得 Android service 引用，不是 BLE 初始化。

实际命令：

```sh
pixi run just react-native formal-host-android-build debug
```

formal typecheck、无 internal 泄漏的 autolinking、App debug APK 和 AndroidTest APK 构建通过。
完整构建日志 `.work/logs/formal-host-android-debug-build.log`。**没有安装或运行这些新 APK，
instrumentation 3 项 runtime 仍 pending。** 本地最终 artifact hashes 已记录在
`.work/android-owned-worker-retirement-build-2026-11-10/result.json`，并核对新 class 与三项方法
确实存在于 test APK DEX：

| Artifact | SHA256 |
| --- | --- |
| App debug APK | `4564b73535e5a2f7b104de95f2fc676428be6ca7ab327a81993f94c7561f5778` |
| AndroidTest APK | `9faf57a45b879c1aa889f89bb08e534ea3028c361cdc2b1e30d359be3a530c3e` |

这些仍是 build evidence，不是 installed identity。未来安装前必须重新核对 frozen bytes、
原 retained App 身份与 fresh lease。不自动运行原 native suite，其中含 production
初始化用例；不将 debug 编译计成 release-shaped candidate、ART/Hermes 或 RF pass。

### 其他实际检查

- `pixi run just react-native tools-test test_recipe_wiring`：最终 15 tests / OK，0.313s。
- `pixi run just react-native formal-host-test`：2 suites / 57 tests / OK。
- `pixi run just fmt`、`pixi run just lint`：通过；`pixi run just docs-check-mermaid`：通过，
  234 个 Markdown 中 27 个图。`git --no-pager diff --check` 及 authored Markdown UTF-8、
  fence、空白、冲突标记、本地链接检查通过；既有三个 CRLF→LF warning 未顺手改动。
- 本次 unit task 及 build 不下载依赖或更换 SDK/JDK；没有手机 lease、radio/permission mutation
  或共享 ADB 操作。既有已安装 App 和上一轮 sealed bundle 均未更改。

## 4. 初始软件交付时的待办与剩余门禁

下列第一项后来由 §5 的授权真机执行关闭，其余范围仍未关闭；保留原构建交付阶段的边界。

- 需要在明确授权的新 APK/phone lease 下，独立运行上述 3 项无 BLE 初始化的 Android native
  回归，验证实际 HandlerThread、Handler snapshot 和 join；本轮没有用编译代替执行。
- 即使该 native slice 通过，也不证明真实 event reader/Bleng shutdown/active GATT 清理、Hermes
  invalidation 或 pending reload。当前 instrumentation 只模拟 backend 已被 Bleng 退役的调用序列。
- unassembled backend dispose 失败后的重试/资源退休仍未获专项证据；原实现已在 dispose
  失败后尝试 stop，本次不扩大为允许在已停止 Handler 上重新提交 backend cleanup 的契约。
- 正式 scan → stop → close → Evidence 全链、actual Hermes migration、RF、host gate、ABI freeze
  与 stress 保持各自 pending；本轮修复只是其生命周期前置，不升级此前候选结果。
- HandlerThread `quitSafely` 和 join 行为通过 Context7 官方 Android reference 核对：quit
  请求不是线程已退出证明，因此继续要求 bounded join 和实际 `isAlive` readback。
  参考：[HandlerThread](https://developer.android.com/reference/android/os/HandlerThread)。

## 5. 授权真机执行：3 项通过与清理

用户在上述安装/执行范围说明后明确回复“继续”。本轮只安装 §3 的同一对 reviewed APK，
仅运行 `AndroidPlatformSessionRetirementInstrumentedTest`，不运行含初始化用例的整个 suite。
没有 rebuild、真实 BLE initialize、Start/scan/connect、BLE permission grant/revoke、radio
切换、HANS 策略变更、Appium/Maestro 启动、firmware mutation 或共享 ADB 重启。

| 项目 | 本次实际结果 |
| --- | --- |
| run | `e58b844d-ba10-4699-a32a-c46261fdba2c` |
| local evidence | `.work/android-owned-worker-retirement-device-2026-11-10/e58b844d-ba10-4699-a32a-c46261fdba2c/` |
| scope | `formal-android-owned-worker-native-retirement`；不是 UI/RF/Hermes gate |
| App APK | `4564b73535e5a2f7b104de95f2fc676428be6ca7ab327a81993f94c7561f5778`，109,046,934 bytes |
| test APK | `9faf57a45b879c1aa889f89bb08e534ea3028c361cdc2b1e30d359be3a530c3e`，431,290 bytes |
| native result | **OK (3 tests)**，每项 `INSTRUMENTATION_STATUS_CODE: 0`，最终 code `-1` |
| time | instrumentation 报告 `0.07s`；host bracket 约 `1.046s`，不是完整安装/清理耗时 |
| scenario/phone | passed / passed；两组 cleanup errors 均为空 |
| host/RF | `host_gate_status=pending`、`rf=not_run` |

实际 3 项为：

```text
transferredBackendRetirementStopsOwnedWorkerAndReportsZeroResources
disposeUnassembledStopsOwnedWorkerAndRepeatedDisposalLeavesItStopped
independentSessionsRetireWithoutLeavingPreviousWorkersAlive
```

**得到的设备证据：**实际 Android HandlerThread、Handler 上的 backend snapshot 与 join 路径
执行通过。正常退役断言 worker 已退出、12 个 required resource counts 全零；两代独立 session
不残留旧 worker；unassembled 成功 rollback 的重复关闭不留线程。断言在测试 finally 清理
之前执行，不能由测试兜底把泄漏伪装成通过。

**未得到的证据：**测试没有构造 Bleng，没有真实 event reader 或 ownership assembly，不能
证明完整 Rust Supervisor shutdown、活跃 GATT/scan 资源归零、Hermes invalidation、reload、
UI 或 RF。这里的“12 项全零”仅属于本次未初始化 BLE 的 production session 用例，不是整个
App/系统或其他 candidate 的 native resource-zero。

### 安装、执行与设备收尾

- 本地输入先固定 hash、regular/nlink/size 和 copy 前后 identity；按现有 native-HIL 字节
  快照方式取得私有 App/test APK，并再次核对 source/copy hash。不是 UI lifecycle 的 bundled-JS
  APK gate；没有放宽那个 gate 让 debug/native APK 混入 UI 流程。
- fresh phone lease 下核对原 boot、user 0、无 active instrumentation/forward/helper process。
  前一 UI candidate `be441256…` 的 retained App 先按完整原 OS identity fresh 校验，再只退休
  该进程，不接管或停止未知 App。
- 两份 APK 通过原 `android_sdk.install_apk` 的显式 root-aware 路径安装，`grant=False`，
  test APK 显式 `test_only=True`；逐命令 lease/deadline guard。安装后 path/hash/独立 UID 以及
  target instrumentation 注册均核对，App UID 保持不变。
- 复用 `tools.android.run_instrumentation._run_instrumentation` 和原输出 parser，只提交一次
  `am instrument -w -r -e class com.blehubblenghost.AndroidPlatformSessionRetirementInstrumentedTest
  com.blehubblenghost.test/androidx.test.runner.AndroidJUnitRunner`，不依赖宽泛 discovery。
  场景总预算 240s、instrumentation 不超过剩余预算/120s、独立 cleanup 90s。
- 既有 parser 检查正常完成回执后，再核对精确 class、`numtests=3`、三项 method 的各自成功
  status；不是仅以 shell exit 0 判断通过。没有重跑该测试 class。
- 测试退出后 App/test process 已缺席，不需要绑定/force-stop 一个新的残余进程。两轮 fresh
  finalizer 检查无 active instrumentation、helper service/process 或 forwarding。
- App 两项 BLE permission 始终为 denied，前后 UID/permission tuple 相同。三项 hidden-API
  设置、HANS debug property 和 Close log tag 与 baseline 相同，无恢复 mutation。
  phone lease 已释放，5037 listener 保持原 PID `16841`。

当前手机安装的是本次 debug/native 测试候选，而不是前一 release-shaped UI APK；原成功
结果页已在已知身份退休时关闭，没有自动恢复或启动旧 App。后续 UI/RF candidate 必须按其
自己的 reviewed APK、live identity 与 lease 准入。

### 取证完整性与保留的本地拒绝

本轮一次性编排只复用既有 controller、installer、instrumentation parser 和 publication，
没有新增 maintained runner、App 或 registry。实际脚本、frozen APK、install/native 日志、
execution/phone/result 回执留在新 run 目录；其本地 consistency manifest **8 files** 的
path/size/SHA256 与完整文件集合独立复核通过。该 manifest 不是 canonical UI/RF bundle，
不使用 lifecycle verifier 冒充 native admission，也不是远端签名证明。

正式设备执行前，有两次纯本地准备拒绝，均在取得 phone lease 和首条设备命令之前，未算
native test failure，也未改写为成功：

- `b3701c79-2862-4fa0-99cc-0e29b2e57bcb`：误用 UI freezer 的非固定 destination 名，被拒绝。
- `14ec8efe-581f-46a7-acd7-8dba4615cdfe`：固定名后，UI APK gate 正确拒绝无 bundled JS 的
  debug APK。随后改用既有 native-HIL snapshot pattern，而非削弱 UI 校验。

两份 `preflight-failed.json` 及已创建的本地文件保留。设备只执行了后续独立 run 的一次测试。
本轮未修改可执行源码或测试断言，没有借修改测试把运行结果变绿。
收尾 `pixi run just fmt`、`pixi run just lint`、`pixi run just docs-check-mermaid` 均通过；
Mermaid 检查 234 个 Markdown 中 27 个图。两份更新文档的 UTF-8、fence、空白、冲突标记和
本地链接，以及 `git --no-pager diff --check` 通过；既有三个 CRLF→LF warning 未顺手改动。
本轮没有重跑 JVM/Jest suite；新增的是精确 3 项真实 instrumentation 和本地 evidence
consistency 复核，不把旧软件通过当作新的设备门禁。

## 6. 后续软件验证：Supervisor 必须等待两道 foreign retirement 屏障

继续产品主线时，在既有 `crates/bleng-supervisor/tests/ffi/interruption.rs` 补充
`close_waits_for_reader_and_snapshot_barriers_before_publishing_retirement`。复用该文件原有
`InterruptedSession`、Notify barrier、FakeBackend 与 FFI factory，不新增 platform adapter、
runner、公开 API、ABI 或 production 行为。

同一测试分别把 foreign event-reader drain、resource snapshot 保持在显式未释放屏障上，
执行实际 Rust `Supervisor.start()` 与 `shutdown_view()`，验证：

- 到达任一 foreign barrier 前，Bleng 已完成 backend disposal（一次）。
- reader barrier 未完成前，resource snapshot 调用次数仍为零。
- reader/snapshot 任一 barrier 未释放时，close future 明确仍 Pending，不能提前成功；
  并发 close view 为 `owner_closed=false / supervisor_busy`，没有可读取的退休资源证据。
- 只有 release 后才发布成功 close view 与零资源快照；后续读取保留该终态，不重复
  backend dispose、reader barrier 或 snapshot。

使用 Notify 和一次显式 poll，而非 sleep 或依赖机器速度的“等一会看没完成”。每个流程
由原有 3 秒 timeout 包围，测试不靠丢弃/取消未完成 future 来制造成功。

实际 `pixi run just test supervisor`：**124 tests passed / 6 suites**，包含新用例和原有
close failure、interruption、projection、startup/recovery 等相关回归。

此结果证明实际 Rust Supervisor/Bleng 与 foreign callback adapter 的 await/发布屏障语义。
平台 callback 仍是合成样本，并没有启动实际 Kotlin/Swift event reader、JNI/Hermes 或 BLE；
它与 §5 的 production Android session 退役是两层独立证据，不能拼成同一次完整移动 runtime
或 Hermes reload 通过。本轮不访问手机/ADB、不安装 APK、不运行新的 UI/RF 对照。

Maestro 维持既定显式 opt-in 小型 UI 对照，不扩建第二套完整测试；本轮没有新增或重跑
Maestro flow，也没有将其既有通过升级为上述生命周期或 RF 证据。
