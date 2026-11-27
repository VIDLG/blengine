# 外围测试接线收口与 BLeng 人工 review 准备 checkpoint

日期：2026-10-25

## 范围与结论

本轮处理测试架构审查发现的入口接线、tarball 漏检、重复执行和日常/候选构建混用，并补现有 Android consumer 的最小 direct Kotlin native HIL 路径。用户批准的工作顺序已写入 [Plan §14](../../../.plan/PLAN.md)：先外围工程收口，再人工 review/打磨 `crates/blehub`、`crates/bleng`；长期压力后置到整体功能、人工 review 和稳定发布候选收敛后。

**本报告的初始接线 checkpoint 不等于当时已完成外围 RF、ABI freeze 或 Phase exit。** 用户明确选择先收外围，再开始核心人工 review，不并行推进。后续两个外围工作包及现有设备最小 native 验证已完成，最新事实以 [native 矩阵](2026-10-25-native-matrix-validation.md) 为准：Android direct native、Mac IO/notification、iPhone IO/notification 各有具名实证，可进入核心人工 review；不同 App hash/session 不合并为最终同候选通过。

- `crates/blehub`、`crates/bleng` 的 67 个文件在本轮前后逐文件 SHA-256 相同，未为外围测试改动核心语义。
- 本报告初始接线执行未操作物理 Android/iPhone、未启动 DK/RF、未刷写固件或修改 radio/权限；使用了已有 iOS Simulator。后续真实 native/RF 运行另存上述 native 矩阵报告，不回写初始阶段证据。
- 未 commit、push 或创建 branch；保留原工作树及本轮失败日志。

## 已完成的代码收口

### 1. 入口与维护中 discovery

- [RN recipes](../../../just/react-native/mod.just)：iOS basic runtime 的采集与 checker 统一使用 host-relative 日志路径，移除误导性的 `PYTHONPATH`。
- `host-test` / npm `test` 成为维护中 Jest 总入口，实际发现全部 17 个维护中 TS/TSX suites；具名 UI/REJECT scripts 保留为定向子集，不再串跑重叠脚本充当全量。
- `tools-test` 是 `tools-fixture-test` 子集，文档明确二选一；本轮只运行一次 combined，未在其后补跑整个 subset。
- iOS native lifecycle 入口从一个 class 扩大为整个 `BleHubNitroHostNativeTests` target，仍不选择 UI/radio target。
- 新增 SDK-free Just dry-run 接线回归，保护日志路径、native target、Jest discovery、增量/clean build 边界与 RF run 无隐式构建/provision fallback。
- 旧 `App.ledger.test.cjs` 未被接入或删除，不能因文件存在就算当前覆盖；它不在本轮维护中的 Jest discovery 范围。

### 2. Android artifact 漏检与构建分档

[Tarball 校验](../../../tools/react_native/android_artifact_consumer.py) 现在每个文本 member 只读取一次，再检查全部 forbidden tokens。回归分别验证无泄漏、首 token 和非首 token；后者保护本次 EOF 漏检根因，不需要设备或 npm 安装。

[Android FFI recipes](../../../just/ffi/mod.just)：

- 普通构建不再删除生成目录、执行 `:bindings:clean` 或 `--rerun-tasks`，保留 Cargo/Gradle 增量。
- 去除重复 backend debug 构建与重复 consumer build 定义。
- `android-backend-check` 继续作为已有 staged bindings 下的日常 backend 检查。
- `android-candidate-build` 显式清理所属 bindings/backend/consumer Gradle 输出及生成 Kotlin/JNI，再完成发布与 consumer build；没有 device/RF 步骤。本轮仅验证该命令接线，没有为证明接线再做一次 clean 重建。
- Android production fingerprint 只在 backend release AAR 发布后写入；仅 debug build 不再提前宣称完整 staged production 已 fresh。

### 3. 扩大 discovery 暴露的 iOS native 链接问题

第一次真实 Simulator 执行发现 `FullRecoveryBeginGateTests` 三项失败：hostless target 无法通过 `NSClassFromString` 找到只编入 AppDelegate 的 DEBUG probe。其余 24 项通过；Xcode 在失败诊断收尾超过 110 秒上限，本次结果包未完整，不能记为 gate 通过。失败日志和 incomplete result bundle 保留。

修复不是 skip 或放宽断言：将原来的 Foundation-only gate 与 DEBUG probe 从 `AppDelegate.swift` 原样抽到 [FullRecoveryBeginGate.swift](../../../tests/consumers/react-native-internal/ios/BleHubNitroHost/FullRecoveryBeginGate.swift)，由 App 与 native target 共同编译同一份实现；不依赖启动 RN App。NativeTests 内容不变。

修复后的单次 build/test 使用 `-only-testing:BleHubNitroHostNativeTests` 与 `-collect-test-diagnostics never`，完整 xcresult 核实：

| Native class | Passed / Total |
|---|---:|
| `BleHubNitroHostNativeTests`（含 extension） | 18 / 18 |
| `FullRecoveryBeginGateTests` | 3 / 3 |
| `PendingReloadNativeTests` | 6 / 6 |
| 总计 | **27 / 27，0 failed / 0 skipped** |

环境为已有 iPhone 18 Pro / iOS 27.0 **Simulator**。这不是物理 iPhone、CoreBluetooth RF 或 XCUITest 证据。既有 clean-consumer 白名单递归包含该新文件，实际 staging copy 核验通过；未完整重跑 iOS clean artifact build/device runtime。

### 4. Android 最小 native HIL 已接入，但未执行 RF

[Android native HIL 指南](../android.md) 描述实际入口、准入和证据边界：

- 复用现有 `tests/consumers/android`，opt-in `NativeHilSmokeTest` 直接调用 production Hub、Android backend 与 Kotlin sink；不经过 RN/BLeng，不另建 App/native image。
- 一条 bounded smoke 覆盖初始化、service UUID + profile local-name prefix 组合扫描、连接、发现、read/response-write/readback、notify、disable、disconnect 与 dispose/destroy/Handler join。
- Peripheral/GATT IDs 来自真实事件；profile 值来自 immutable session 输入，没有硬编码地址、ATT handle 或机器端口。
- Host 复用 leased `DirectFixture`、live receipt、现有安装/instrumentation owner 和 phone observation；每次 install/launch 前验证 owner/receipt。权限不足、运行中 consumer、身份漂移均 fail closed，不自动切换 radio、grant/revoke、flash/reset 或重试。
- 默认普通 consumer instrumentation 不启用 RF；`ffi android-native-hil` 只运行预先构建的 artifacts。订阅后的 BTP trigger、native 真实通知、完整 close、Peripheral 无 peer 及 cleanup 共同决定 host 结果。

Host-only synthetic tests 和 APK 编译已通过，**真实 Android native RF 尚未执行**，不能据此标为通过或关闭 native HIL 验收。

## 本轮实际验证

| 命令/范围 | 结果与证据 |
|---|---|
| `pixi run just react-native host-test` | 17 suites / **619 tests passed**；`.work/validation/recipe-wiring-jest.log` |
| `pixi run just react-native host-check` | 通过；`.work/validation/recipe-wiring-host-check.log` |
| iOS native target build/test | 27 passed，完整 xcresult；精确命令、candidate inputs 与结果在 `.work/validation/full-recovery-shared-fix-final-summary.json` 及对应 build/test status/log 中 |
| `pixi run just react-native tools-fixture-test` | **1214 tests，219.243 秒，OK**；`.work/validation/2026-10-25-peripheral-readiness-tools-fixture.log` |
| `pixi run just ffi android-backend-consumer-build` | 增量 bindings/backend 发布、consumer APK/test APK 均成功；`.work/validation/2026-10-25-peripheral-readiness-android-build.log` |
| 最后 profile name-prefix 接线后的定向 Python 回归 | 4 个模块共 **14 tests passed**；`.work/validation/2026-10-25-peripheral-readiness-final-targeted.log`；未为这个窄变更重复整个 combined |
| 最后 Kotlin smoke APK 离线编译 | `:app:assembleDebugAndroidTest --offline --no-daemon -Pandroid.builder.sdkDownload=false`，**BUILD SUCCESSFUL**；`.work/validation/2026-10-25-peripheral-readiness-final-kotlin.log` |
| `pixi run just fmt` / `pixi run just lint` | 通过；`.work/validation/2026-10-25-peripheral-readiness-final-{fmt,lint}.log` |
| `pixi run just docs-check-mermaid` / Markdown 检查 | 27 个 Mermaid diagrams 通过；主导航/分层/native 指南及本报告的 UTF-8、围栏、空白与 83 个相对链接目标通过；失效的 Windows 指南链接已指向保留的 archive |

初次 Just 空行解析错误和新增 Python 两项 lint 问题已修复，原失败日志保留；不算生产/RF 故障。Android staged fingerprint 在本轮发布后为 fresh。Apple fingerprint 因其输入包含本轮修改的共享 FFI recipe 而为 stale；未仅为 recipe 元数据改变重建五 target Apple package，亦未写新 stamp 或宣称最终同候选通过。

编辑器还报告 standalone HIL 源文件不能解析 `BleHubUniFfi`（如 `SwiftProbeHarness.swift:1`）；这些文件需要其 staged-package 构建上下文，不等于本轮真实 Xcode target 编译失败。本轮没有为清编辑器全局诊断修改无关 HIL/外部仓库。

## 后续工作包收口与发布边界

| 工作包 | 当前状态 | 与人工 review 的关系 |
|---|---|---|
| Android direct native RF | 已完成接入及现有手机上的真实双 write/readback、notification、close/cleanup；具名 runs 见 native 矩阵 | 当前最小补项已完成，不要求第二台 Android或并行于核心 review 补测 |
| Apple 常规 native v2 host 准入/清理桥接 | 已完成 immutable session/live receipt/identity-bound cleanup；Mac IO/notification、iPhone IO/notification 各有真实通过。iPhone owner 恢复、失败及 HIL 时序修复均保留 | 当前最小补项已完成；候选 hash 与场景分别记录，不能升级为全部原生 RF 故障矩阵或最终同候选通过 |
| 已知 iOS CCC disable callback gap | RN + Native HIL 已定界，失败保持 open | 不为重确认而重复 RF，不合成事件/改固件/放宽判据；不要求用户等此平台边界“变绿”才能读 BLeng |
| 稳定同候选终验、最终 ABI/package/RF、extended lifecycle 与长期 stress | 发布/冻结前按 Plan 核对；长期压力已明确后置 | 不作为开始人工 review 的前置，不为当前外围工作建设长 soak 框架 |

初始 checkpoint 后按用户选择串行完成了这两个外围工作包及适用的最小真机验证，没有扩张到第二档 Android、重复 standalone 或多小时压力。现在可以进入用户主控的 `crates/blehub`、`crates/bleng` 人工 review/打磨；这表示本轮约定的外围 review 前置已完成，不表示所有发布验收或已知平台缺口已经关闭。后续以核心改动选择定向回归，稳定候选阶段再执行发布/冻结门禁。
