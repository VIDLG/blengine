# Candidate device runtime：Android ART、iPhone artifact 与 RN clean consumer

日期：2026-10-25

## 范围和候选边界

继 [host/package checkpoint](./2026-10-25-candidate-host-gates.md) 后，执行当前工作树的非 RF 真机 runtime。实际安装、启动了测试 App；没有启动 Peripheral、刷固件、扫描/连接 DK、切换蓝牙或撤销权限，没有收集密码、操作 keychain unlock 或执行人工签名授权。

- Android：serial `3ac5cc09`，model `PHB110`，Android `16` / API `36`，physical device。
- iPhone：iPhone 14 Plus，iOS `26.6.1`，UDID `00008110-000C61160C39401E`；开发者模式、可用签名身份与 unlocked preflight 通过。
- Git 工作树仍含大量未提交改动。通过只适用于本次产物、设备和运行，不是完整最终候选/ABI freeze 或 Phase exit。
- 本轮 staging source-input fingerprint：
  - Apple：`bea41d255db866ef4d9f42ae45641653224e2d257a3cc354b08f24736cad3a5e`
  - Android：`402631b9de79930178be098e1ba9d430887f916f0eda2e1fd3c7a70baae2be80`
- 指纹范围由 `tools/artifact_fingerprint.py` 定义，不是整个工作树或全部测试的单一身份。
- 本轮两个 AAR、三个 native Android APK 的 SHA-256，以及 native Android/iPhone 原始回执副本保存在 `.work/validation/2026-10-25-device-runtime-evidence/`；`candidate-inputs.json` 记录指纹和产物 hash。

## 1. Android production ABI ART：通过

实际执行：

```text
BLEHUB_ANDROID_SERIAL=3ac5cc09 pixi run just ffi::android physical
```

- bindings instrumentation：`OK (10 tests)`。
- published-AAR consumer：`OK (4 tests)`。
- `tenThousandProductionHubLifecyclesReturnLocalCountersToBaseline` 实际在 ART 执行：10,000 次 Hub 创建、重复 dispose、destroy、sink release，检查 backend disposal 与本地计数归零。
- 事件 catalog、foreign callback/thread、assembly rollback、sink/close/drain/error 边界属于 production UniFFI + test backend/sink 的范围。
- published consumer 另验证 production Android backend 初始化 adapter snapshot、caller-owned Handler/lease 的释放与失败后重新取得 owner；没有进行 GATT/RF。
- 此证据只覆盖 API 36 physical device，不是 Android API-tier matrix 或生产 BLE 长期压力。

完整构建/runner 输出：`.work/validation/2026-10-25-android-production-art.log`。

原始 instrumentation 副本：

```text
.work/validation/2026-10-25-device-runtime-evidence/android-bindings.log
.work/validation/2026-10-25-device-runtime-evidence/android-consumer.log
```

### Just dependency 修复

运行前发现 bindings instrumentation APK 不存在，当前 `android-build` 却没有显式生成它。`just/ffi/mod.just` 已恢复 `:bindings:assembleDebugAndroidTest` 的前置构建，并让 `android-consumer-build` 依赖 `android-backend-publish`，保证 consumer 所需的 backend AAR 与 UniFFI 一起发布，而非依赖残留 AAR。未改变测试标准、device selector 或安装机制。

## 2. 原生 iPhone artifact-only runtime：通过

实际执行：

```text
pixi run just apple::consumer-ios-device
```

本机 Xcode 对 staged production Swift package 进行 development signing、安装和启动；只操作 `dev.blehub.fwvyq287w5.ios-artifact-consumer`。本次 run ID：

```text
bad3b65c-4b8b-4e23-ae6f-94fb1356406f
apple_ios_artifact_consumer=passed
```

`tests/consumers/apple/ios/Sources/AppDelegate.swift` 的 fake backend/sink 验证 production ABI 的 assembly、同步 ingress/domain-terminal 顺序、显式 dispose、retained ingress closed 与 close 后不交付。设备结果必须匹配本次 run ID；不是 CoreBluetooth、RN 或 iPhone 10,000 lifecycle。

完整日志：`.work/validation/2026-10-25-ios-native-artifact-device.log`。结果、install/launch JSON 副本在上述 `device-runtime-evidence/`。

## 3. iPhone RN signed clean-artifact runtime：通过

实际执行：

```text
pixi run just react-native::host-ios-artifact-device-runtime 00008110-000C61160C39401E FWVYQ287W5
```

独立目录：

```text
.work/react-native-ios-artifact-consumer/run-2a9f0c64c11f45908ac83111f70abd10/
```

- npm tarball、installed package/Pod dependency 与 archive provenance 通过。
- Signed device build、single-image、install 与 runtime checker 通过。
- runtime markers 包含 command-ID validation、ArrayBuffer round-trip、dispatch accepted、completion、listener removal 和 close resolved。
- 最终：`react_native_ios_artifact_consumer=device-runtime-passed`。
- 这是 RN/Nitro contract smoke，不是 lifecycle stress、XCUITest、reload、CoreBluetooth 或 RF。

顶层日志：`.work/validation/2026-10-25-rn-ios-artifact-device.log`。独立目录的 `logs/` 保留 signed build、single-image、install JSON、runtime/controller/check 输出。

## 4. Android RN clean-artifact lifecycle：首次失败，修复后通过

实际执行：

```text
pixi run just react-native::host-android-artifact-device-lifecycle 3ac5cc09
```

首次独立目录为 `.work/react-native-android-artifact-consumer/run-7b94f248552940a792530ba97d453d14/`。静态 package、debug/release APK、single-image 和安装均成功；App 输出 `stress:complete:passed`，但主机 checker 拒绝通过，缺少 `stress:start:8` 与 `stress:cycle:0:passed`。保留 `.work/validation/2026-10-25-rn-android-artifact-device.log`，不得用 App completion 替代 host verdict。

### Host capture 修复

`tools/react_native/run_android_runtime.py` 原来每秒读取 `logcat -d` 并覆盖证据文件。后续快照无法保证保留设备缓冲区中已滚动消失的早期记录；修改不涉及 App、bridge 或 checker：

- 清理旧日志后、启动 App 前，启动本次专属流式 logcat。
- 直接写本机文件，读取完整 UTF-8 行；超过既有 8 MiB budget 或采集子进程提前退出均 fail closed。
- terminal 后冻结采集文件，再运行原有 checker；不合并快照、不补造或合成缺失标记。
- 复用 `capture_io.stop_local` 回收本次子进程；失败清理仍停止 App，保留 primary error 和 cleanup note；成功不 force-stop，保持结果页可见。
- 回归覆盖 capture-before-launch/freeze-before-checker、早期记录保留、partial UTF-8、退出/overflow、timeout、reload、失败时子进程回收和 successful App retention。

新独立 run：

```text
.work/react-native-android-artifact-consumer/run-38aa7b0bce434ac58821c4eeaa9d2438/
react_native_android_artifact_consumer=device-lifecycle-passed
```

完整日志：`.work/validation/2026-10-25-rn-android-artifact-device-streamed.log`。debug/release single-image 和未修改的 lifecycle checker 均通过。

成功保留结果页的后续短验证先用 `pm path` + 手机上的 `sha256sum` 比对已安装 APK 与该 clean artifact，SHA-256 相同：

```text
daa9f837e7ad92fd00297018765f2a37252b584fcc29760bc3630b60d9b08e0a
```

再执行：

```text
pixi run python -m tools.react_native.run_android_runtime 3ac5cc09 --mode stress --log .work/validation/2026-10-25-rn-android-result-visible.log --timeout 30
```

原 checker 通过，capture 结束，App 未被成功清理路径停止。这个场景由 `STRESS_CYCLES=8` 定义，只是 bounded RN close/recreate、listener removal、late-callback/post-close rejection gate，不是 10,000 lifecycle 或长期生产压力。

## 软件回归与清理收尾

- `pixi run just react-native::tools-fixture-test`：1200 tests，214.590 秒，`OK`。完整日志 `.work/validation/2026-10-25-device-runtime-tools-fixture.log`。
- `pixi run just react-native::host-check`：通过；`pixi run npm --prefix tests/consumers/react-native-internal run test:ccc-disable-reject`：6 suites / 158 tests 通过。
- `pixi run just fmt`、`pixi run just lint`、`pixi run just docs-check-mermaid`、`git diff --check`：通过。Authored Markdown UTF-8、fences、空白和本地链接校验通过。
- Apple/Android source-input fingerprint 均为 fresh；没有本次 logcat、instrumentation 或 artifact controller 残留进程。
- 最后只读查询：Android 成功 App PID `26390`，`topResumedActivity` 为 `com.blehubnitrohost/.MainActivity`；结果页保持前台。

## 5. 同一已安装候选的 iPhone lifecycle / 双平台 runtime reload

后续继续复用上文的 clean artifacts，没有重新构建或安装。执行前核对：Android 手机上的 APK SHA-256 与 `run-38aa7b0bce434ac58821c4eeaa9d2438` 的 release APK 一致；iPhone 当前 App 的 installation URL 与 `run-2a9f0c64c11f45908ac83111f70abd10` 安装回执一致。记录见 `.work/validation/2026-10-25-lifecycle-reload-artifacts.json` 和 `2026-10-25-ios-lifecycle-installed-app.json`。

实际执行：

```text
pixi run python -m tools.react_native.run_ios_device_runtime 00008110-000C61160C39401E .work/validation/2026-10-25-ios-lifecycle-followup.log --seconds 35 --stress --keep-app
pixi run python -m tools.react_native.check_runtime_log .work/validation/2026-10-25-ios-lifecycle-followup.log --stress

pixi run python -m tools.react_native.run_android_runtime 3ac5cc09 --mode reload --log .work/validation/2026-10-25-android-reload-followup.log --timeout 60

pixi run python -m tools.react_native.run_ios_device_runtime 00008110-000C61160C39401E .work/validation/2026-10-25-ios-reload-followup.log --seconds 40 --reload --keep-app
pixi run python -m tools.react_native.check_runtime_log .work/validation/2026-10-25-ios-reload-followup.log --reload
```

三项均通过原有 checker：

- iPhone lifecycle：`stress:start:8`、close-during-callback、cycle 0—7、recreate-close、late-callback、post-close-dispatch 和 complete 均 passed。
- Android reload：同一 logcat PID `30087` 的两次 `reload:ready`，中间 `reload:cleanup-started`，经 runner 请求原生 `ReactDelegate.reload()`；checker 通过，App 保留。
- iPhone reload：两次 start/round-trip/dispatch/completion/ready，中间 `reload:native-triggered`、cleanup，最后 `reload:complete:passed`；checker 通过。
- 各 controller 顶层输出保存在对应 `*.host.log`，不与原始 runtime log 混用。

### iOS `--keep-app` 非 RF 行为

原 parser/cleanup 只对部分 controlled RF mode 支持此选项。现在明确允许 basic/stress/reload，并在非 RF 成功时仅 detach 本机 console；失败继续按原路径清理。既有 RF mode 的 admission/cleanup 逻辑保持不变，没有修改 App、Native image、checker 或 BLE 语义。相关 native-host Just runtime/lifecycle/reload 入口已传入 `--keep-app`。

定向回归 33 tests 通过，新增 basic/stress/reload × success/failure 六个真实本地子进程子场景，分别断言 detached 与 signal-forwarded、子进程回收及原始日志保留，不调用 iPhone SDK。

本轮最终标准 `pixi run just react-native::tools-fixture-test`：1201 tests，217.127 秒，`OK`，日志 `.work/validation/2026-10-25-lifecycle-reload-tools-fixture.log`。RN typecheck、158 项 Jest、fmt/lint、Mermaid 与 Markdown 检查也通过；Apple/Android staging fingerprints fresh。controller diagnostics 无错误；无本次采集残留。Android reload 成功 App PID `30087` 在清理检查时仍存在。

两个真机 console cleanup 回执均为 `action=detached`：

```text
.work/validation/2026-10-25-ios-lifecycle-followup.console-cleanup.json
.work/validation/2026-10-25-ios-reload-followup.console-cleanup.json
```

注意：detach 时本次 `*.launch.json` 未落盘；cleanup receipt 中的 console/helper PID 是本机进程身份，不是设备 App PID。不能据此宣称 iPhone native boot identity、旧 Probe 资源归零或 pending-ATT engine reload。此处只是已验证的非 RF JS/Nitro lifecycle/reload scope，不替代那些更强的独立门禁。

## 余下边界

- macOS Swift 和 Android ART 各自已有 production ABI / fake backend 10,000 lifecycle 证据；不推导 iPhone、RN 或生产 BLE 的同等压力覆盖。
- 本报告已补齐 bounded iPhone lifecycle 和双平台非 RF reload；仍需核对 pending native owner/resource-zero、长期压力、同候选 RF 和最终 ABI/package freeze 条件。这些通过不直接关闭 Phase。
- Plan Phase 5 要求长扫描、高频 notification、反复重连及可追踪 stress；当前 8-cycle RN gate、10,000 fake-backend lifecycle 和历史 10/20-cycle RF 不等于连续多小时压力。用户随后已批准将长期压力后置到整体功能、人工 review 与稳定 release-shaped candidate 收敛后（Plan §14），不作为外围工程收口或开始 BLeng review 的前置。负载/速率、持续时间、资源 baseline/增长阈值、失败预算和 cleanup 判据届时评审；当前不建设长 soak runner，不临时规定固定小时数。
- 后续范围调整（2026-10-25，用户批准）：因当前只有一台 Android 真机，[Plan §11.2/§15](../../../.plan/PLAN.md) 已移除两档 API/多版本真机 matrix 的当前强制门槛，不再等待第二台设备。原始执行事实不变：本轮只有 API 36 真机证据，其他版本/OEM 未验证；不标为通过，也不用模拟器或历史报告伪装补齐。现有手机的适用 native HIL/RF 门槛保留。
- iOS CCC disable continuity 保持 open；未修改固件、合成 `E1`、放宽 continuity 或重复已知 RF 失败场景。
- 原始失败保留；不提交、不推送或创建 branch。
