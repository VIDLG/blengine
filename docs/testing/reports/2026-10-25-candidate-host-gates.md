# Apple / Android candidate：无设备回归与 artifact 边界检查

日期：2026-10-25

**当前状态：**下文记录的首次 synthetic SDK 隔离和 iOS RN checkout header 泄漏已在同日修复。标准组合回归 1197 项通过，iOS clean tarball consumer 的 unsigned device/Simulator build 与 single-image 检查通过；首次失败与超时日志保留，详见“同日修复与复验”。这些结果仍不是手机 runtime、RF 或 ABI freeze。

## 范围与身份

本轮针对当前 dirty worktree 执行无设备构建、production ABI runtime、确定性测试和 artifact-only consumer。没有刷固件、重跑 RF、安装手机 App、切换系统蓝牙或进行签名授权。下述 synthetic 测试存在一次未隔离的真实安装命令尝试，被系统拒绝，不能把本轮描述为所有测试均无外部 SDK 调用。

- Git HEAD：`75edda34b20b043ef282d7d94614c9a1169c6179`，**不是完整 worktree candidate identity**；既有未提交改动保持不变，不声明最终发布候选或 ABI freeze。
- 本轮完成后的 source-input fingerprint：
  - Apple production：`80423aea68e23d682834c50b07a3b1553bde271d816a79ef3d51705433da784c`
  - Android production：`8287d58404adc5ed6fd62f4cd0b178f65f27102cde559f34d8d933a83442f6b3`
- fingerprint 由 `tools/artifact_fingerprint.py` 的输入范围定义，只证明对应 staging 输入 freshness，不是全部测试、二进制内容或 RF 的统一证明。
- Apple 环境：Xcode `27.1`，Swift `6.4`，真实 Mac 本机执行。
- 日志根目录：`.work/validation/`；本机忽略的日志不能用报告中的通过文字替代。

## 已通过

下表命令均以 `pixi run` 执行。Just 使用明确的 module recipe 路径。

| 命令 | 本轮结果 | 日志 / 边界 |
|---|---|---|
| `just ffi::apple-package` | 五个 Rust Apple targets、三 slice XCFramework、component symbols、staging fingerprint 通过 | 后续 Apple consumer/backend recipe 也重建了相同 package；RN iOS recipe 最后重新写入 fingerprint |
| `just ffi::android-backend-test` | Kotlin binding/native image/AAR 构建成功 | `2026-10-25-android-backend-fixed.log`；第一次单测命中缓存，另有强制重跑 |
| `just ffi::android-backend-check --rerun-tasks` | 34 tests，0 failures/errors/skipped，debug/release build 通过 | `2026-10-25-android-backend-rerun.log`；从本次 JUnit XML 汇总 7 suites |
| `just ffi::swift-macos` | `swift_uniffi_production_runtime=passed` | `2026-10-25-swift-production-runtime.log`；production ABI fake backend/sink，不是 CoreBluetooth RF |
| `just apple::consumer-macos apple::consumer-ios` | macOS artifact consumer runtime、generic iOS device 和 Simulator unsigned consumer build 通过 | `2026-10-25-apple-consumers.log`；iOS 为编译/链接，不是真机运行 |
| `just binding::snapshot-check` | Rust/Kotlin/Swift 三份当前 reviewed API snapshots 校验通过 | `2026-10-25-api-snapshot.log`；未更新 snapshot，也不声明 freeze |
| `just apple::test` | backend macOS 确定性测试通过；纯 Swift HIL selection/ledger/config 共 16 tests 通过 | `2026-10-25-apple-backend.log`；不运行真实 Peripheral |
| `just ffi::android-backend-consumer-build` | 发布 AAR 与独立 native Android consumer APK/test APK 编译通过 | `2026-10-25-android-consumer-build.log`；未安装、未运行 ART instrumentation |
| `just test::all` | 260 tests，16 suites 通过 | `2026-10-25-core-workspace.log`；Rust workspace，不替代 foreign runtime |
| `just react-native::host-check` | TypeScript typecheck 通过 | 当前工作树，不是 artifact-only 证明 |
| `npm --prefix tests/consumers/react-native-internal run test:ccc-disable-reject` | 6 suites / 158 Jest tests 通过 | CCC enable/disable、signal、catalog/session/workbench；不是 RF |
| `just react-native::host-android-artifact-consumer` | clean npm tarball + Maven artifacts 静态 consumer 通过 | `2026-10-25-rn-android-artifact.log`；single-image 检查为 2 UniFFI ABI slices + 2 Nitro slices |
| `just fmt`、`just lint` | 通过 | 包含 Cargo formatting/Clippy、Ruff、Just 与 profile isolation；formatter 另格式化了一份已有的 `ios_adapter_startup_evidence.py` |

RN Android clean consumer 本次独立目录：

```text
.work/react-native-android-artifact-consumer/run-20645fc6089b49e2bc3b576827902f6e/
```

### 10,000 lifecycle 的实际边界

当前 `tests/ffi/runtime/swift-host/main.swift` 的 `runLifecycleStressTest()` 执行 10,000 次 Hub 创建、重复 dispose 和释放，并断言 backend dispose 总数恰为 10,000；`ffi::swift-macos` 会调用该测试。因此，本轮已取得 **macOS Swift / production UniFFI / fake backend 的 10,000 lifecycle 通过**。

这不是 iPhone 或 Android ART 的 10,000 lifecycle，不是生产 CoreBluetooth 持续 RF、RN reload 或长期资源压力测试，也不能替代最终同候选冻结后的重跑。

## 修复：Android toolchain verify 长等待

第一次 bounded build 在 300 秒上限仍停于 `python -m tools.android_toolchain verify`，没有进入 Gradle。进程观察显示版本探测父命令已结束，但同一进程组仍有 `android-cli upload-metrics`，Python 仍在等待输出收集。日志：`2026-10-25-android-backend.log`。

`tools/android_toolchain.py::_run_output` 原来用无内部超时的 PIPE capture。现改为：

- stdout/stderr 使用独立临时文件，避免后台子进程继承 PIPE 导致 EOF 等待；
- stdin 关闭，不等待终端输入；
- 默认 30 秒 deadline；
- 保留退出码和两路输出，超时给出明确 command/context，不绕过版本验证。

新回归 `tools/tests/react_native/test_android_toolchain_output.py` 三项通过：后台子进程仍持有输出时父探测正常返回、非零退出码/两路输出/stdin EOF、挂起探测的有界失败。它已纳入现有 RN host tooling discovery。随后完整 Android gate 在几十秒内通过，未安装或更换 SDK/NDK，也未修改 CLI telemetry 配置。

另执行：

```text
pixi run python -m unittest tools.tests.react_native.test_android_toolchain_output tools.tests.react_native.test_ccc_disable_reject_ledger tools.tests.react_native.test_run_quiet -q
Ran 16 tests — OK
```

## 首次失败与未完成边界（保留原始结果）

### 1. 合并工具/provider 回归未通过

`pixi run just react-native::tools-fixture-test` 在 180 秒上限未完成，日志中已出现失败标记。保留 `2026-10-25-tools-fixture.log`，结束本次 attempt-owned process group，不能标为通过。

随后 fail-fast 诊断：

```text
pixi run python -m unittest discover -s tools/tests/react_native -t . -p 'test_*.py' -v -f
Ran 224 tests in 67.222s
FAILED (errors=1)
```

首个错误为 `IosPendingRunnerTests.test_frozen_app_live_fixture_single_install_native_capture_and_cleanup_bundle`。synthetic 测试的 install 路径没有隔离 `ios_sdk.run_command`，实际调用了：

```text
xcrun devicectl device install app --device physical-device ...
```

系统返回 `CoreDeviceError 1000: The specified device was not found`。未成功安装，但这是 host 测试隔离缺口；不能称为硬件故障，更不能在缺少 mock 的状态下继续当作纯软件通过。日志：`2026-10-25-tools-failfast.log`。本轮没有修改 pending-reload runner/test，也没有弱化判据；其余失败尚未完整枚举。

### 2. iOS RN clean artifact consumer 被 checkout leakage gate 拦截

`pixi run just react-native::host-ios-artifact-consumer` 退出码 2，在进入 Xcode consumer build 前失败：

```text
checkout path leakage in host/ios/BleHubNitroHost/BleHubPendingReloadLifecycle.mm
```

当前 host 文件包含指向 `integrations/react-native-internal/nitrogen/generated/ios/c++/HybridBleHubProbeSpecSwift.hpp` 的 checkout-relative include。clean tarball consumer 必须解析 installed artifact / Pod 提供的 header，不能依赖源 checkout；不允许禁用泄漏检查来过门禁。

- 日志：`2026-10-25-rn-ios-artifact.log`
- 独立目录：`.work/react-native-ios-artifact-consumer/run-55f3379580f44cc29d091bc24454e54e/`
- 本轮保留失败，不把 native Swift iOS consumer build 通过替换为 RN clean consumer 通过。

### 3. 本轮没有新设备/RF证据

未执行 Android ART、iPhone signed artifact runtime、新 RF、长时间生产压力或 Android API-tier matrix。iOS CCC disable continuity 继续保持 open，见 [Native HIL Flow 12](./2026-10-24-ios-native-hil-ccc-disable-reject.md)。既有 `full-suite` 是十个 flow 加 RecoveryTen，不应写作每个 flow 各运行十次的完整交叉矩阵。

## 同日修复与复验

### Synthetic SDK 隔离

`test_ios_pending_reload.py` 将安装 mock 接到 runner 实际使用的 `ios_sdk.run_command`，不再错误地挂在 Android SDK 边界。模拟安装 receipt 绑定实际命令、冻结 artifact 和输出路径；断言安装前后 receipt guard、capture 顺序与参数，并覆盖安装失败、安装后 receipt 失败、capture 失败和 cleanup 同时失败。没有改 production runner 或放宽 ledger。

完整审计运行随后只剩三处 CLI mock 参数断言未包含已有的 `port=None`；`test_run_rf_{write,ccc_enable,ccc_disable}_reject.py` 已同步实际参数签名，不改变生产分派。最终标准入口：

```text
pixi run just react-native::tools-fixture-test
Ran 1197 tests in 275.876s
OK
```

- 标准入口日志：`.work/validation/2026-10-25-tools-fixture-fixed.log`
- 修正最后三处断言之前的 SDK 审计日志：`.work/validation/2026-10-25-tools-fixture-audited-agent-r3.log`，未拦截到真实设备/提权/fixture 命令；它不是最终通过日志。
- 定向包装 + pending-reload 测试：44 tests 通过。

### iOS RN artifact-only consumer

`BleHubPendingReloadLifecycle.mm` 现在使用 `<BleHubReactNative/HybridBleHubProbeSpecSwift.hpp>`。Nitrogen 已声明该 Pod private header，host 已配置 Pod header search paths；没有修改生成文件、增加 checkout 搜索路径或关闭 leakage 检查。新增回归直接复制实际 native host，确认其保持字节内容且可以通过 clean staging，而不是只检查不含实际 include 的 dummy fixture。

首次修复后尝试已越过 leakage、npm install、TypeScript 和 autolinking，但在 CocoaPods 的 Maven URL 探测处达到 300 秒上限；进程观察显示其 `curl` 等待数分钟。结束了本次 runner 和 Pod 专属进程组，保留 `.work/validation/2026-10-25-rn-ios-artifact-fixed.log`。随后有界 URL 检查确认官方 URL 经重定向返回 HTTP 200；未改 vendor 脚本、版本、源地址或依赖策略。

后续独立运行：

```text
pixi run just react-native::host-ios-artifact-consumer
react_native_ios_artifact_consumer=static-passed
```

- 最终日志：`.work/validation/2026-10-25-rn-ios-artifact-retry.log`
- 独立 artifact run：`.work/react-native-ios-artifact-consumer/run-6de6052e5c6949ccb766f8759e4857eb/`
- npm tarball provenance、installed-package resolution、Pod archive provenance、unsigned Simulator/device build 和两侧 single-image 检查通过。
- 未安装、签名或运行 iPhone App；静态 consumer 通过不替代真机 runtime。

## 下一步

1. 当前两个软件阻塞已关闭，后续不应继续把它们描述为 open；保留首次失败用于审计。
2. 后续具名 [device runtime 验证](./2026-10-25-candidate-device-runtime.md) 已覆盖 Android ART、iPhone signed artifact runtime 和两平台 RN clean-artifact runtime。仍按 Plan 核对其各自覆盖边界、长期压力和独立 RF；不由主机/静态 consumer 结果推导设备通过。用户随后已批准移除多版本/两档 API 真机的当前强制门槛（Plan §11.2/§15），使用现有一台 Android 手机继续推进，其他版本保持未验证。
3. iOS CCC disable continuity 仍 open，不为重复确认已知 CoreBluetooth 边界而自动重跑 RF。不要提前宣告 Phase exit 或 ABI freeze。

本轮不提交、不推送、不创建 branch，也不修改 Plan 的阶段状态。
