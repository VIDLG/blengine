# Android / Apple GATT 生命周期加固与两端 RN full-suite 回归

日期：2026-10-18。承接 [首轮中断覆盖审查](2026-10-18-gatt-interruption-coverage.md)。本轮把已确认的生产边界做成可测试实现，并修复消费者测试清理；**两端各自新跑的 full-suite 均 passed、离线 integrity verified、App kept-open**。这不是故障专项全覆盖、最终同候选 native/ABI/package 矩阵齐全或 ABI freeze。`IOS-RADIO-GATE` 仍为 Deferred，未运行 Settings/XCUITest/Continuity 排查或系统开关。

## 1. 实现与责任边界

没有新增 App、RN runtime mode、host runner、工具注册框架、生产公开 API、依赖或固件能力；既有 dirty 工作保留。两端共用审查矩阵，但不伪造相同 native completion 语义。

### Android：测试执行生产写入分支，而非只操作 terminal flag

- [CharacteristicWriteLifecycle](../../../platforms/android/library/src/main/kotlin/dev/blehub/android/CharacteristicWriteLifecycle.kt) 是生产 backend 实际调用的 write-only 生命周期边界；[AndroidBleBackend](../../../platforms/android/library/src/main/kotlin/dev/blehub/android/AndroidBleBackend.kt) 保留 Framework native submission、legacy/API-33 调用差异、真实 Handler 和 route/GATT identity。
- Controller 直接使用原有 queue、`stateLock`、watchdog slot、terminal claim 和 retirement projection。没有第二个队列、调度器、executor 或测试专用 backend。
- No-response acceptance 只发布一次 local-success terminal，保留 native head/watchdog；callback success/error 都只能 drain 已终结命令，之后才提交下一项。With-response 仍等 callback，并保留 domain-before-terminal。
- Native submit、event publication、watchdog removal、queue drive 在锁外；owner checks/queue/terminal 变化在现有锁内。Disconnect-during-submit 与 dispose-winning 保持不同结算语义。
- [六项生产 lifecycle 回归](../../../platforms/android/library/src/test/kotlin/dev/blehub/android/CharacteristicWriteLifecycleTest.kt) 使用实际 write DTO/native identity、生产 queue/watchdog/retirement，控制 native port 返回与 callback 消费顺序；不再用 RSSI 手动 claim 来替代写入路径覆盖。

**回归有效性对照：** 临时仅将新 lifecycle 的 accepted no-response 分支恢复成旧的 `finish(pending, null)`，目标测试按预期失败：expected submitted `[1]`，actual `[1, 2]`，暴露 callback drain 前提交 replacement 的错误。恢复后源码 SHA-256 与实验前一致，目标六项和完整 JVM gate 通过。证据：`.work/android-write-lifecycle-premature-finish-mutation.{log,xml}`、`.work/android-write-lifecycle-mutation-proof.log`；未使用 Git reset/restore 或覆盖其他 dirty 改动。

### Android：CCCD 失败只补偿本地注册

[NotificationLifecycle](../../../platforms/android/library/src/main/kotlin/dev/blehub/android/NotificationLifecycle.kt) 由 backend 的 native configuration/callback 入口实际调用：

1. Local registration 成功后，再提交一次 descriptor write。
2. Descriptor submission rejection/handled exception 或 error callback：失败 enable 尝试本地 unregister，失败 disable 尝试本地 re-register。
3. 只尝试一次，**没有补偿 descriptor write、自动重试、自动断连或远端 rollback 承诺**。
4. 原始 command failure 保持为 terminal；补偿 false/exception 使用既有 `Log.w` 单独记录，带 command identity，不替换 primary error，不额外伪造 BLE terminal。
5. Failed enable 不创建已确认 subscription；failed disable 保留既有 logical subscription/value route。Success 仍只在正向 descriptor completion 后提交。
6. 已 stale/disconnecting/closing owner 不再被允许发起补偿；native 调用后重查 ownership，竞争中由 disconnect/dispose 负责退役，不能用 callback 重建旧 route。

这是对 [既有 Android best-effort rollback 约束](../../research/platform/platform-abstraction-and-compatibility.md#android) 的本地机制落实，不新增“BLE 事务回滚”语义。[九项生产 notification lifecycle 回归](../../../platforms/android/library/src/test/kotlin/dev/blehub/android/NotificationLifecycleTest.kt) 覆盖 enable/disable、两种 mode、local rejection、descriptor rejection/exception/callback error、补偿失败、primary error 保留、value routing、错误 native identity、ownership loss 与旧 route callback。

当前端口测试不执行 Android Binder/真实 Handler/OEM API；本地补偿请求返回 true 也不证明远端 CCCD 或实际 notification 连续性已恢复。

### Apple：发送容量等待有界，但不冒充 native submission

[Controller](../../../platforms/apple/Sources/BleHubApple/GattIoController.swift) 为 no-response capacity wait 保存独立状态；[现有 watchdog registry](../../../platforms/apple/Sources/BleHubApple/GattWatchdogRegistry.swift) 增加 exact-command capacity kind，由 [backend driver](../../../platforms/apple/Sources/BleHubApple/AppleBleBackend.swift) 使用原有 serial queue、配置时长和 fail-closed recovery。

- 首次 capacity wait arm；重复 drive/readiness 但容量仍不足时，不刷新 deadline。
- 等待阶段仍可 queued-cancel，不生成 native submission、callback barrier 或假 native-active 计数。
- Head cancellation 只取消该 command 的 capacity token；取消 sibling 不能取消 head 的 watchdog。
- Readiness 后真正提交前取消对应 capacity token；旧 expiry 不能退役替换后的 head/generation。
- Expiry 先核对 token、waiting command 与 connection identity；等待目标 `Timeout`、siblings `OperationAborted`，进入既有 connection fail-closed recovery，不盲目推进 FIFO。
- Disconnect/dispose 清除 owner/token，不因晚 readiness 重新提交。

这落实了 [Plan §16.1.7](../../../.plan/PLAN.md) 已有的有限 flow-control recovery 要求，不把它后置成纯 caller timeout。原有 11-case 中断 matrix 保留；另补四项 capacity 回归。**这些测试组合生产 controller、registry、connection gate 和 projection；未直接执行 backend 的真实 Dispatch timer wiring。**

### Android 干净消费者：版本和失败清理

[PublishedArtifactConsumerTest](../../../tests/consumers/android/app/src/androidTest/kotlin/dev/blehub/ffi/runtime/consumer/PublishedArtifactConsumerTest.kt)：

- 将旧硬编码版本 `5` 改为与 canonical `blehubContractVersion()` 比较，仍保留契约一致性断言，不为旧测试改生产版本。
- 嵌套 `AutoCloseable.use` 按顺序 dispose Hub、destroy wrapper、dispose backend，再停止并有界 join 调用者 HandlerThread；主断言失败时 cleanup failure 作为 suppressed error 保留。
- 新回归故意在 backend 创建后抛 assertion，再验证原错误不被替换、下一 owner 可取得真实 process lease。不是删除失败测试或只重跑一条初始化来绕过整组。
- 真机完整 class **4 tests / OK**。旧候选的 5-versus-6 和 lease 级联失败仍保存在首轮报告，不回写为 pass。新回归没有注入 dispose/destroy/thread shutdown 同时失败，不扩大这部分 runtime 证据。

## 2. 验证结果

| 门禁 | 实际结果 / 范围 |
|---|---|
| Android write lifecycle targeted | 6 passed；包含旧行为 mutation 的预期失败及恢复后新跑通过 |
| Android notification lifecycle targeted | 9 passed |
| Android canonical JVM discovery | **29 passed**，0 failure/error/skip；包含前两项，不相加计数 |
| Android production backend / clean consumer build | passed |
| Android physical clean consumer | **4 passed**，固定 serial `3ac5cc09` |
| Apple `just apple test` | passed，含 package staging 与 16 项 pure selection/configuration tests |
| Apple 全量 Swift deterministic | **84 passed**，其中 GATT controller targeted **39 passed**，不相加 |
| Apple generic iOS build | device + Simulator passed |
| Apple artifact-only iOS consumer | unsigned device + Simulator passed，不等于消费者真机运行 |
| RN Jest | **13 suites / 355 tests passed** |
| RN tooling + Peripheral/controller combined | **839 tests / OK** |
| RN private check / host typecheck | 串行执行通过 |
| Android RN full-suite | 新候选基础十项 + RecoveryTen，host/physical/cleanup/bundle passed，kept-open |
| iOS RN full-suite | 独立新候选基础十项 + RecoveryTen，host/physical/cleanup/bundle passed，kept-open |
| 两份新 bundle 离线复核 | integrity verified；不重新授权 RF |

Android canonical gate/build 使用已有 Pixi 环境：

```sh
PIXI_NO_INSTALL=true PIXI_FROZEN=true PIXI_OFFLINE=true BLEHUB_VERBOSE=true pixi run --as-is --offline just ffi android-backend-test
PIXI_NO_INSTALL=true PIXI_FROZEN=true PIXI_OFFLINE=true BLEHUB_VERBOSE=true pixi run --as-is --offline just ffi android-backend-build
PIXI_NO_INSTALL=true PIXI_FROZEN=true PIXI_OFFLINE=true BLEHUB_VERBOSE=true pixi run --as-is --offline just ffi android-backend-consumer-build
```

Apple 实际执行：

```sh
pixi run just apple test
pixi run just apple build-ios
pixi run just apple consumer-ios
pixi run -e apple-ci swift test --package-path platforms/apple --filter GattIoControllerTests
pixi run -e apple-ci swift test --package-path platforms/apple
```

日志：`.work/android-write-lifecycle-*`、`.work/android-notification-*`、`.work/apple-capacity-wait/*.log`、`.work/android-instrumentation/gatt-lifecycle-consumer-20261018-01.log`。父执行线 build/RN/Jest/tool/verify 输出存于 `/tmp/blehub-gatt-lifecycle-*`。

## 3. 两端 full-suite 的独立候选与 live proof

| 字段 | Android | iOS |
|---|---|---|
| Attempt | `rf-full-suite-0404dcc516e94ebba38ca45a5996ad73` | `rf-full-suite-ed7fe5c3f2214b2dbdb3b73c3999030b` |
| Bundle run ID | `67b1c9a6-0474-452c-bd4e-1a3363dd25f2` | `ab5501bd-1762-4ee7-95d0-a3a318165469` |
| Fixture session ID | `282e1ee6-c21d-4d99-8a46-131e9e4781ae` | `f214845f-ba6e-4c90-bb22-f35cd66bf770` |
| Phone | `3ac5cc09` | `00008110-000C61160C39401E` |
| Host finished UTC | `2026-10-18T01:22:08Z` | `2026-10-18T01:25:18Z` |
| continuous10 / recovery_ten | passed / passed | passed / passed |
| completed / primary error | true / null | true / null |
| cleanup / evidence errors | empty / empty | empty / empty |

Attempt 均位于各自 `tests/consumers/react-native-internal/{platform}/build/`，manifest 为 `evidence-bundle.json`。两端独立安装、runtime、fixture session 与 host verdict，不拼接为同一次跨平台运行。

同一物理 `jlink:1050252028` / `/dev/cu.usbmodem0010502520281`，由两端顺序领取 lease；每次 App 启动前都有新的 `btp-vendor-live-under-device-lease` receipt：

- Installed image：`nrf-multifunction-rf-v1-a`；digest `dd7dea99a325db32fa1fd66648d0a0814c8c7c442a77cf08998f2b91a1cc5b7a`。
- Profile：`blehub-nrf52840-multifunction-v1-a`，service `fd500000-0000-4000-8000-000000000000`，name `BleHubMultiRfDKA`。
- receipt 固定 contract/profile hashes、port、资源 identity、fixture PID/PGID 和 session；不是复用历史 receipt。
- 普通 run 未刷机或重启固件，未切系统蓝牙或 reset permissions；成功 App 保持打开。

### 候选身份

| 产物 | SHA-256 / tree fingerprint |
|---|---|
| Android RN release APK | `1eb61bf2d2f4df40ded9f4c7f11a4e136aee579fb672aeca3b7dffc7269a09e9` |
| iOS signed Debug App tree | `c8712902272c6d606295b09cb777a95d8d7bda2a2b24addd3f3627c8f98bd124` |
| Android backend release AAR | `42c4af062f131dcd133660fb9f8c8e61a91d83fae0f02d0857939cfcb2acb602` |
| Android native consumer APK | `28aa602f9ab212e92757a6606809e1a61d6abe2065d2a2f814146eb910ad5d79` |
| Android native instrumentation APK | `c610f3ccfbb850246ace899bba86d0ad28670772e41b263a36aad4ee9de041b6` |

Manifest 记录 HEAD `75edda34b20b043ef282d7d94614c9a1169c6179`、dirty=true、source_to_build_attestation=false。Fingerprint 是 host-submitted frozen snapshot，不是设备 installed binary byte attestation；也不把 dirty 工作树标作锁定 release。

实际 RF 入口（Android 调用前使用前述 Pixi no-install/frozen/offline 环境；iOS 复用已经构建好的 frozen candidate）：

```sh
pixi run just react-native host-android-rf-lab-suite 3ac5cc09 tests/conformance/scenarios/fixture-contracts/multifunction-rf-v1-a.json tests/conformance/scenarios/blehub-nrf-multifunction-v1-a.json
pixi run just react-native host-ios-device-build 00008110-000C61160C39401E FWVYQ287W5
pixi run python -m tools.react_native.run_rf_lab_full --platform ios --device 00008110-000C61160C39401E --artifact tests/consumers/react-native-internal/ios/build/device-derived/Build/Products/Debug-iphoneos/BleHubNitroHost.app --fixture-contract tests/conformance/scenarios/fixture-contracts/multifunction-rf-v1-a.json --fixture-profile tests/conformance/scenarios/blehub-nrf-multifunction-v1-a.json
```

两份 manifest 各自执行现有 `python -m tools.react_native.rf_evidence_bundle <manifest>`，结果均为 `integrity verified (not RF reauthorization)`；使用的路径就是上表 attempt 的 `evidence-bundle.json`。

## 4. 保留的失败与当前未证明项

- Android 第一次 full-suite **recipe** 调用在 private TypeScript check 失败：标准库和 React typings 文件暂时不可见；未启动 fixture、未生成 RF attempt。该次与 iOS build 的共享 npm install 并行，是依赖目录竞争的线索，而非已证明唯一根因。两条 build 结束后，串行 `pixi run just react-native check` 无源码/config 修补即通过，再串行 Android build+run 成功。保留 `/tmp/blehub-gatt-lifecycle-android-full-suite-20261018-01.log`，不把它回写为 RF pass；首次进入 RF 的是 `...-02.log` 对应的新 attempt。以后不要并行运行会改同一 npm/Nitrogen 输出目录的安装/生成步骤；两平台产物准备完后再按 fixture lease 执行。
- 初期 Android lifecycle 测试的编译/type-inference 和 fixture watchdog-token 重用问题已修正，原日志保留；最终 six-test targeted 和 29-test discovery 来自恢复后的源码，不计 mutation 的预期失败为产品失败或成功计数。
- 新 Android tests 执行的是生产 lifecycle 与注入 native ports，未证明 Binder、真实 Handler/timer/OEM 调度、API-tier 分支全覆盖或任意重复 callback 的 same-characteristic 消歧。
- Apple capacity tests 直接组合生产 controller/registry/projection，不直接调用 backend 的 Dispatch timer orchestration。真实永久 capacity stall、readiness/expiry 的系统调度竞态、远端断连完成仍缺具名故障证据。
- 两端 full-suite 是真实正常 I/O、现有 read-watchdog/cancellation 和 recovery 回归。它没有可控地制造 CCCD rejection/补偿失败、active write hold 或容量永久阻塞；现有 ATT **read** hold 不能改名充当这些证明。
- 本地补偿不保证远端配置回滚；逻辑资源归零不等于 OS heap 无泄漏。最终同候选 native ABI/runtime/package、10,000 lifecycle、长稳和 Android API-tier 仍按 Plan 独立验证，不用两份 full-suite 代替。
- 未恢复系统 radio/permission mutation，也没有因这轮通过取消 `IOS-RADIO-GATE` 的暂缓决定。

最终 `pixi run just fmt`、`pixi run just lint`、`pixi run just docs-check-mermaid`、`git --no-pager diff --check` 均通过；四份本轮 authored Markdown 的 UTF-8、fences、尾空白、冲突标记和相对文件链接检查通过。新 notification lifecycle 与消费者测试的编辑器 diagnostics 刷新后无 error/warning。静态检查不扩大上述 runtime/RF 证明范围。
