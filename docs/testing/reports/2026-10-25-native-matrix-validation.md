# Native 测试矩阵：真实执行、采集修复与未关闭边界

日期：2026-10-25

## 范围与结论

继 [native 桥接与前置](./2026-10-25-native-perimeter-bridge.md)，本轮使用现有 Android 真机、真实 Mac 和 DK-A v2-A 执行最小 native 验证，并尝试进入 iPhone normal IO。没有刷写/reset 固件、切换系统蓝牙、撤权、重复 iOS CCC REJECT 或运行长期压力。

**最终结果：Android direct native 两种 write/notification smoke 通过；Mac native IO 和 notification 在修复后的同一 Mac App 候选上通过。首次 Mac IO 暴露的事件漏报已按现行 Plan 仅修平台 projection，没有删除 HIL 断言或改核心定义。iPhone 的 orphaned-held owner 已合规归档，native IO 和独立 notification 均已有真实通过；notification 首次运行暴露的 shared HIL 中途检查时序错误已修复并保留原失败。当前约定的外围工程与最小平台补项已完成，可以进入核心人工 review；这不是最终同候选验收、Phase exit 或 ABI freeze。**

两个核心 crate 的 67 个文件与本轮开始时逐文件 hash 一致。所有原始失败、成功和 cleanup 分开保存，没有合并为同一 session。

## 1. 分层矩阵：不靠增加场景数量补齐

| 层/路径 | 当前证据定位 | 本轮结果/是否继续补 |
|---|---|---|
| Rust、native deterministic、FFI、package/clean consumer | 既有 [host checkpoint](./2026-10-25-candidate-host-gates.md)、[device runtime](./2026-10-25-candidate-device-runtime.md) 各有 candidate 范围 | 不把旧通过升级为最终同候选；本轮仅跑受采集修复影响的回归，不重跑全部包/runtime |
| RN full-suite、REJECT、pending reload、permission/radio、multi-DK | 具名历史报告与 [RF Lab 架构](../../research/react-native/react-native-rf-lab-test-architecture.md) | 不因 native 新入口就重复整套；iOS CCC callback gap 保持 open，不重新确认 |
| Android direct Kotlin native | production AAR + Hub/backend/sink，真实手机 + DK-A | **Passed**：组合扫描、连接/GATT、两种 write 的 domain/terminal 与独立 readback、notification、disable、disconnect/close、host cleanup；不含长期压力 |
| Mac native notification | production Swift HIL + DK-A，独立 native boot/fixture session | **Passed**：真实 baseline value、disable 后 distinct stimulus 与有界静默、close/cleanup；不是 iOS 证据 |
| Mac native IO | 修复平台漏报后，重建 App 的独立 session | **Passed**：原有两种 write/domain/terminal、独立 readback 与严格 IO ledger 均通过；首次失败保留 |
| iPhone normal native IO | owner 恢复、用户关闭旧 App 后，以新签名 prebuilt 启动独立 session | **Passed**：组合扫描、连接/GATT、两种 write/domain/terminal 与独立 readback、disconnect/dispose 后静默、peer retirement 和 host cleanup |
| iPhone normal native notification | 修复 shared HIL 中途检查后重建的 App；与 IO 为不同 hash，独立 run/boot/fixture | **Passed**：真实 `ef` value、disable/order、post-disable distinct `ee` stimulus 与静默、disconnect/dispose、peer retirement 和 host cleanup；首次 HIL 失败及一次 App-occupied preflight 均保留 |
| 最终同候选与长稳 | [Plan §14](../../../.plan/PLAN.md) | 整体功能、人工 review 和稳定候选后再做，不增加第二档 Android、固定小时数或长 soak 框架 |

本轮具体协议/准入补项已闭环，不再为每层复制完整 RN 故障矩阵；已定界的 iOS CCC REJECT gap 与发布前同候选/长稳责任仍保留。

## 2. 本轮实际运行矩阵

均显式使用 Android `3ac5cc09` 或本机 Mac；fixture 为 J-Link `1050252028`、BTP `/dev/cu.usbmodem0010502520281`、canonical `multifunction-rf-v2-a` contract/profile，并由每次 owner 在物理租约内检查 live receipt。测试 runner 不 provision 固件。

| 路径与 run ID | 结果 | 保留事实 |
|---|---|---|
| Android `95b9ef03-aebb-4666-baea-c6713c8fbcda` | failed | 初始化/组合扫描/连接/GATT 后，两字节 smoke 数据被拒绝，status 13；未进入 notification |
| Android `1b56a737-d841-409f-9881-cfc382375422` | failed | 改正数据后精确 readback、enable 通过；通知等待超时，主机触发握手被 4096-byte buffered read 延后 |
| Android `c600c19a-4ed8-45aa-b1d5-f198ddb2563f` | **passed** | 完整十个 host phase，包括 `notified`、`disconnected`、`closed`；Peripheral 无 peer，primary null、cleanup_errors 空 |
| iPhone `24aeb93a-3c9e-477b-a370-95829e3d0fa0` | preflight blocked | 新 RN 合作锁取得并释放；radio owner 冲突、native_identity null；没有 App/fixture 执行 |
| Mac IO `1f371e7e-866b-4e72-83f5-425fb4112e66` | failed | command 9 已成功 terminal，probe 等待旧 domain 条件超时；cleanup_errors 空，不改成 pass |
| Mac notification `d2bf44a6-a7dd-4333-8c08-161a60ab3e8a` | **passed（修复前 App）** | `ef` baseline 真实接收；disabled 后 stimulus `ee`、原生静默断言、close 与 peer retirement 通过；cleanup_errors 空 |
| Android `0bf8c3e8-98a4-4b23-9e4c-641cd569bd24` | **passed（修复后）** | 同一 smoke 验证 with/without-response 的 Written/terminal 和各自精确 readback，随后真实通知与完整 close/cleanup |
| Mac IO `b4ff6755-ae8c-49e3-96d0-4e5d0d37213e` | **passed（修复后）** | 不改原 probe 判据，原先缺失的 no-response domain 与原生 IO ledger 均通过 |
| Mac notification `b1db28a2-ef4a-4be9-9e16-d415f100575d` | **passed（修复后）** | 与上一 IO 使用同一 App hash，独立 session/boot/lease；baseline、distinct post-disable stimulus、静默、close、peer retirement 与 cleanup 均通过 |
| Android `f20edd7b-6c18-48b3-b6b8-5ed2ac29cca4` | **passed（最终 versioned smoke）** | 显式 `nativeHil=writes-v2`，避免旧单 write test APK 冒充覆盖；两种 write/readback、通知、close、peer retirement/cleanup 均通过 |
| iPhone IO `b84d623a-cfc5-4f8a-b68a-a2c9af1c82e5` | **passed** | 新签名 App 两种 write/readback、原生 IO event-order/ledger、disconnect/dispose 静默、Peripheral peer retirement 均通过；primary null、cleanup_errors 空，结果页先保留，后经明确授权关闭 |
| iPhone notification `05143154-865d-4ecf-8773-afa1ef967bdf` | failed | 旧 HIL App 已 enabled/收到 value，但在派发 disable 前错误要求 command 7 terminal；仅 baseline stimulus，未到 post-disable；cleanup_errors 空 |
| iPhone notification `8a3ae23f-7e9b-4bf6-abf9-29cc321cf106` | preflight blocked | 修复后 App candidate，但设备出现另一个既有 Native App PID；没有安装/启动新候选或接入 fixture，native_identity null |
| iPhone notification `466ffa0a-0b60-4199-b0ad-b177d222bf4e` | **passed（修复后）** | 真实 `ef`、正常 disable、distinct `ee` stimulus 后静默、ledger、disconnect/dispose、peer retirement/cleanup 均通过；primary null、cleanup_errors 空，最终成功页保留 |

原始目录分别位于 `.work/validation/android-native-hil/<run>/` 和 `.work/apple-native-hil/<run>/`。`.work/validation/2026-10-25-native-matrix-evidence-index.json` 记录十四个原始 result 的路径/hash与分类，未修改任何原 result。

修复后的 Android app APK SHA-256 为 `6c52af7317a3dfa13f70dc87c76a07e6c2e6833326995fd3b899b57e25ee50c1`，最终 versioned test APK 为 `43f1556ad4e6c3738d68025a83ee1f27504cf0ddd8a2c5cea50f6fcbc8279f43`（前一双 write test APK 为 `b5ee8da246e9999c3bf493c622985371e4dc7c4a0a5c092114d9210e00a8ddba`，结果另存）；两场修复后 Mac App 均为 `f992d4b76a6f6a23cda03b40114cbf84e2c73f43a214ee2857f42f43d5df8f19`。修复前 Mac App `5139d9…` 和早期 Android `34a040…/90e064…` 的独立结果保留，不改写为新产物通过。不同 test APK 的 Android 早期失败不继承为最终成功。`cleanup_errors=[]` 不等于 OS heap 无泄漏，也不补写失败场缺失的 native close/resource-zero。

## 3. 已修复的两个外围问题

### Smoke 数据不能假设 fixture 支持任意长度

初稿 `NativeHilSmokeTest` 固定写 `5a31`，canonical read-write 初值为一字节 `00`，真实返回 Android `GATT_INVALID_ATTRIBUTE_LENGTH = 0x0d`（[本地 Framework evidence](../../platform-api-reference/android/BluetoothGatt.java)）。现由 host 从冻结 profile 初值派生**等长且不同**的 `writeHex`，交给 Kotlin；Kotlin 校验长度/差异并继续精确远端 readback。with-response 为 `00 → 01`；随后为 no-response 派生另一个等长且不同的 `02`，分别执行独立 readback。最终增加 `nativeHil=writes-v2` 的双向 opt-in：旧 `true` runner 不启动新测试，旧 test APK 也不能接受新 mode 而冒充两种 write 覆盖；现有 instrumentation parser 将 skip 判为 gate 失败。

此处只改 consumer 测试数据与 host 参数，未改 firmware 或 Rust contract；另有下节明确记录的平台 projection 修复。原失败保留，不通过放宽 readback 来变绿。

### 小量 instrumentation 状态必须在子进程退出前交付

[run_instrumentation.py](../../../tools/android/run_instrumentation.py) 原 `_read_output` 使用 `BufferedReader.read(4096)`。child 在输出短 `subscribed` 后等待主机触发时，这个读取可能等满块或 EOF；主机到 JUnit 超时后才消费状态并触发通知。

新增 [本地子进程回归](../../../tools/tests/react_native/test_android_instrumentation_capture.py)：child 输出短状态并 flush，然后等待 host ACK 文件；原实现 2 秒内确定失败。改用可用的 `read1` 后，该回归与真实 native notification 均通过。仍保留原队列/字节上限、UTF-8 decoder、parser、timeout 和 attempt-owned teardown，没有延长 BLE deadline，也不补造状态/事件。

这证明此前只给 synthetic child 一次性输出全部成功文本，会漏掉真正的实时握手边界；补这一条高价值回归比无区别增加 RF 次数更合适。

## 4. 已按现行 Plan 修复：no-response 平台事件漏报

[Plan §8.7](../../../.plan/PLAN.md) 第 6 点定义 no-response 成功仅为本地发送尝试，不是 remote ACK；第 9 点同时仍要求成功顺序 `CharacteristicWritten → CommandCompleted`。

首次诊断时的实现与测试（以下描述修复前状态，原失败保留）：

- [Apple projection](../../../platforms/apple/Sources/BleHubApple/AppleBackendEventProjection.swift) 只为 with-response 产生 `CharacteristicWritten`，no-response 只有成功 terminal；其 deterministic 测试明确保护这一选择。
- [Android write lifecycle](../../../platforms/android/library/src/main/kotlin/dev/blehub/android/CharacteristicWriteLifecycle.kt) 同样在 without-response 本地 acceptance 时完成 command，只有 with-response 生成 `CharacteristicWritten`；native lane retirement 另行等待 native callback/watchdog。
- [Mac probe](../../../tests/hil/apple/macos/Sources/BleHubAppleMacOSProbe/main.swift) 和 [shared Swift IO ledger](../../../tests/hil/apple/support/AppleCoreBluetoothRfScenario.swift) 仍依 Plan 为 no-response 等待域事件和对应顺序；16 项 synthetic ledger 样本也主动构造了该事件，所以其通过不能裁定 production 一致。

**“不是 remote ACK”不自动意味着应删 domain event。** 最终以已批准 Plan 为准修平台漏报：

- Apple 两种 write 的成功 `.write` completion 都投影原 IDs 的 `CharacteristicWritten → CommandCompleted`；no-response 证据仍仅为已发生的本地提交，没有合成 CoreBluetooth `didWrite` callback。
- Android 本地 acceptance 与 callback-first completion 都通过 `claimCommandTerminal()` 胜出者发布同一成功双事件；late callback 不产生重复 domain/terminal。保留 pending native head、watchdog、串行 drain、失败/取消/退役语义，未把平台取消当远端 rollback。
- 加强 Apple/Android 现有 deterministic 回归，验证 IDs、顺序、exactly-once 以及 callback-before-return 的成功/失败/返回拒绝或异常竞态。未删除原关闭/资源隔离断言。
- 原 Mac/shared HIL probe 判据未变。重建 App 后 Mac IO 通过；Android 现有 smoke 增加 no-response domain/terminal 和独立 readback 后通过。
- RN `App.tsx` 同样要求目标 IDs 全匹配的 Written 与 terminal，再独立 readback；三个 mock 的两种 write 成功序列对齐规范。维护中 Jest 17 suites / 621 tests 通过，含两个新增的真实执行器用例，防止错误 IDs 的 Written 加成功 terminal 被当成通过；typecheck 通过。旧 CJS 只修 mock，未接入或修 loader。

没有改 Plan 语义、Rust types、公开 ABI 或 firmware。Apple 41 项、Android 17 项受影响 native 回归及 generic iOS builds 通过。先前 Mac IO 失败不被回写，新候选的 actual RF 另存上表。

新的 iPhone development-signed prebuilt 是 `.work/apple-ios-runtime/NoresponseSignedDerivedData/Build/Products/Debug-iphoneos/BleHubAppleIOSProbe.app`，完整 App hash `d1e1b5dd76f3b3c37d6f0b1e247170a5c4422aef36ec679fc5aaeb83e8eeaf80`；签名/缓存 profile 校验通过，后续独立 iPhone IO session 已实际运行通过（见下节）；不是修复前的 `90826a…` App，构建本身仍不等于运行通过。

## 5. iPhone owner 合规归档与 native IO

手机 `00008110-000C61160C39401E` 的原 owner：

- run `acf1e1f8-c304-442a-9333-7765ff3c897d`，scope `ios-adapter-startup-phone-owner`，host PID `96490`；只读查询该 PID 与 capture PID `96608` 均不存在。
- owner 路径 `.work/ios-radio-leases/40a87b583c2d6aa6e4b9e2948ca22535483c07db1148069d7ef33272bd91d60b/owner.json`。
- 原 attempt：`tests/consumers/react-native-internal/ios/build/ios-adapter-startup-a66fb80cce9d49b6b5cb5e8dad517707/`。
- `ios-radio-lease.json` 为 **held / restoration=unverified / error=null**；bundle/capture 为 running，finished_at null。
- 原 xcresult 的一个 XCTest 和 control attachment 确有历史恢复 ON，时间 `2026-10-24T16:33:05.510+0800`；不能代替今天的 fresh ON proof、host 完整 cleanup 或 owner 释放。

本次恢复实现之前，`restore_quarantined_phone` 只处理已发布的 `quarantined`，不接受本次 `held`；不能修改原 journal 以迎合 API，也不能拿 PID 消失当释放证据。

原 owner SHA-256 `92497faab6962e34c77b73ce34b4849e93fd93aafec6710efce3f33c31253e79`、journal SHA-256 `4499a11689f0fae899514cd1187a5d48b8166cb0d30daae14ca9c6b24a12cfdc` 本轮未改。原 journal/verdict 保留，owner 目录在下述成功恢复中原样归档，而非删除。

### 证据保全恢复机制已接入

在原 [`ios_radio_lease`](../../../tools/react_native/ios_radio_lease.py) 中收敛为 `archive_phone_owner`，显式选择原始 `quarantined` / `held`，复用同一 mutex、原始 hash/inode/bytes guard、pending blocker、原子归档和失败 rollback。没有新建清锁工具或普通 runner fallback，原 quarantine 的 error/restoration 约束没有放宽。

Held 额外绑定同设备/run 的原 `running` capture 路径/hash，检查原 host PID、capture PID 及独立 process group 均已退出；任何存活/不可查询都保留占用。安全复核发现，单靠“原文件 mtime 早 + 提交时进程已死”无法证明旧执行者没有在 ON inspection 后再次修改 radio。因此必须先通过同一入口的 `--prepare-orphaned-held` 保存独立 preflight，再执行新鲜 ON inspection；归档检查 inspection 的开始晚于该 preflight，且提交前重新校验进程/进程组和全部源记录。

归档 receipt 使用 `ios-radio-orphaned-held-restoration-only` 独立 scope，保存原 owner/journal/capture/preflight 副本；明确 `original_attempt_cleanup_proven=false`、`rf_gate_passed=false`。原 journal 仍是 held，原 bundle/capture 的 running verdict 不补写。操作步骤与职责见 [host ownership](../../research/react-native/react-native-host-tooling-architecture.md)。

实现阶段只完成代码与 synthetic 回归，未触设备。后续现场执行需单独授权及独占窗口；只观察 ON，发现 OFF 则停止，不隐式切换。原 owner/journal hash 不因恢复代码而改写。

### 授权后一次现场检查：UI automation 初始化失败，保留占用

用户随后明确授权保存 preflight、单次只读 Settings ON 检查及成功后的归档和两场 native smoke。本次证据在 `.work/ios-owner-recovery/2026-10-25-authorized-01/`：

- `--prepare-orphaned-held` 成功；原 host PID、capture PID 和 capture process group 均不存在。preflight SHA-256 为 `5d6f8fde55de2cfb0d081057f2cab31b848890bcce633c4d90819bad0a36fa6d`。此步骤没有释放 owner。
- 手机 available/paired/unlocked 预检通过；主机未发现并发 xcodebuild/UI runner/Mirroring。复用既有 signed artifacts，仅 staging 现有 `testBluetoothSettingsOffAndRestore`，精确设置 `BLEHUB_IOS_RADIO_CONTROL_GATE=1`、`BLEHUB_IOS_RADIO_CONTROL_INSPECT_ONLY=1`，移除 startup/RF opt-ins，未重新构建。
- 通过 `run_quiet.py --no-tty --progress-seconds 30` 运行 `pixi run --frozen --no-install -e apple-ci xcodebuild -quiet … test-without-building`，约 **62.9 秒，exit 65**。`inspection-input.json` 保存完整命令、staging 路径/hash及原 owner/journal hash；`test-without-building` 仍可能安装 test artifacts，不宣称无安装副作用。
- 原 `inspection.xcresult` 的 summary/tests 是 **System Failures / BleHubNitroHostUITests-Runner (2449)**：`The test runner failed to initialize for UI testing. (Underlying Error: Timed out while enabling automation mode.)`。summary 为 0 passed / 1 failed / 0 skipped；这个 failed 是 runner 初始化失败，不是 Settings/ON 断言失败，未进入目标方法、没有 fresh ON proof。
- 到此停止：没有调用归档、没有切 radio、没有取得 fixture、没有执行 iPhone IO/notification，也没有自动 retry。`inspection-export/` 仍空；原 owner/journal hash 再次匹配，active owner 保留、无 restoration-pending。
- 后续只读进程快照未发现残留主机测试控制进程。手机仍有既有 `BleHubAppleIOSProbe` PID `1952` 和 RN host PID `2315`；没有终止、接管或宣称它们的 close/resource-zero。即使后续完成 owner 恢复，native runner 也会拒绝覆盖仍运行的既有 HIL App，应先由用户关闭该 App。

本次 `result.json`、原始 log/xcresult、导出的 summary/tests 和只读主机/设备进程快照均保留。阻塞属于 UI automation 基础设施，不能据此诊断 BLE backend 或延长 BLE deadline；手机可用/解锁也不等于 automation 已启用。该次执行到此停止，未自动重试；不收集密码、不绕过系统授权。用户随后明确要求继续，新的单次检查与归档如下。

### 用户再次授权后的只读检查与归档通过，未启动 native RF

新证据位于 `.work/ios-owner-recovery/2026-10-25-authorized-02/`，没有覆盖前一次失败：

- 手机 ready/unlocked、无并发主机测试控制进程；重新保存同一原 owner/capture 的 preflight，SHA-256 `c5583e930ea3056d3ce77216a73a8cbca1d62660c96d796735e1582e0a1be860`。
- 使用相同既有 artifacts 和经验证的 inspect-only staging 内容，仅换新 staging/result 路径。一次 `test-without-building` **exit 0，1 passed / 0 failed / 0 skipped**；xcresult summary 起止 `1792925705.473 → 1792925717.888`，约 **12.4 秒**。
- 第一次离线归档导出在 summary 后报 `inspection source/candidate drift`，未释放 owner；部分导出与失败事实分别保存在 `inspection-export/`、`archive-export-01-failure.json`。随后对原 result、UI runner 和 App 连续读取 inventory 均稳定，改用新的空目录 `inspection-export-02/` 对**同一个 xcresult**离线重新导出成功。未重复手机检查、未放宽 inventory 规则，也未将瞬时 drift 的根因推断为已证实。
- 最终校验同设备唯一 inspect-only 方法、成功 summary/tests、原始 ON note 及真实 switch/child 均为 `1`，inspection 晚于本次 preflight；提交前复验原 bytes/inode/hash、原 PID/进程组、inspection freshness。`restoration-release.json` 的 archive inode/原 owner hash 与实际目录匹配；active 名称和 `.restoration-pending` 都不存在，归档提交已核实。
- 原 owner 目录为 `.work/ios-radio-leases/restored-40a87b583c2d6aa6e4b9e2948ca22535483c07db1148069d7ef33272bd91d60b-f45a0fb2ed6b40c0aaa3809d8a2d2d2d/`；原 journal SHA-256 未变，仍记录原 held 状态。恢复 receipt 明确 `original_attempt_cleanup_proven=false / rf_gate_passed=false`，不补写旧 attempt 成功。
- 归档后只读进程快照仍见 `BleHubAppleIOSProbe` PID `1952` 及 RN host PID `2315`。没有终止或接管这两个非本次创建的进程，没有启动 DK/fixture 或新的 native App；native runner 的拒绝覆盖规则不变。需用户先关闭既有 HIL App，再各运行一次 IO/notification。
- 新 native prebuilt hash 再次核对为 `d1e1b5dd76f3b3c37d6f0b1e247170a5c4422aef36ec679fc5aaeb83e8eeaf80`，本轮仍未安装/启动该 native 候选。授权后的 `result.json` 仅归类为 restoration-only archived，不是 native RF pass。

### 用户关闭旧 App 后：iPhone native IO 实证通过

用户明确报告已关闭旧 App 后，只读快照 `.work/validation/2026-10-25-pre-native-04-processes.json` 不再有 `BleHubAppleIOSProbe`；active radio owner/pending 均不存在。随后运行：

```sh
pixi run just apple rf-native-smoke ios 00008110-000C61160C39401E /dev/cu.usbmodem0010502520281 .work/apple-native-hil/b4ff6755-ae8c-49e3-96d0-4e5d0d37213e/fixture-session/contract.json .work/apple-native-hil/b4ff6755-ae8c-49e3-96d0-4e5d0d37213e/fixture-session/profile.json .work/apple-ios-runtime/NoresponseSignedDerivedData/Build/Products/Debug-iphoneos/BleHubAppleIOSProbe.app io
```

命令通过 `run_quiet.py --no-tty --progress-seconds 30` 保存完整日志至 `.work/validation/2026-10-25-iphone-native-io.log`。输入是既有 immutable contract/profile 快照，hash 分别为 `1b193b3b1acfaa29f5d93aae21a9a6d8e3dd682bfa231ebd5127f0c781863dca` / `78896fa89181502f2daa8967d4ae2d1f2e645adde8ff5f36969081b50f2d338f`；新 session 在其物理租约内重新验证 live installed-image receipt，未复用旧 RF receipt 或 provision 固件。

新 run `b84d623a-cfc5-4f8a-b68a-a2c9af1c82e5` 的原始 `result.json` / `result.txt` 位于 `.work/apple-native-hil/<run>/`；App hash 为上述 `d1e1…`，native PID `2461`、boot `fc3152e9-c871-4d7b-bb0c-ed8c22147b52`。真实服务 UUID + name prefix 组合扫描、连接/GATT、初始 `00`、with-response `10` readback、no-response 本地提交后 `ab` readback、原生 domain/terminal 顺序和 IO ledger、disconnect/post-disconnect、dispose/post-dispose silence 全部通过。No-response 的远端接收依据独立 readback，不把本地提交当 remote ACK。

Host 同时通过 fresh live-boot cleanup challenge、Peripheral 无 peer、同一 firmware boot、fixture retirement 和共享 RN lock 释放；`primary_error=null / cleanup_errors=[]`。该次成功 App 先保留结果页，没有自动终止。用户随后明确授权代为关闭；独立 notification 与新候选的后续事实见下节。

## 6. Notification：授权关闭、HIL 根因修复与真实通过

### 授权关闭与两个非成功 attempt

用户明确授权代为关闭 App。第一份独立 transition 证据 `.work/apple-native-transitions/ef28807f-7c53-4607-9bdf-1fe5beb62ff8/` 绑定 IO 原 `result.json` hash、PID `2461`、完整可执行路径及 fresh boot challenge，在共享 RN phone exclusion 下单次终止并确认 App 已退出；原 IO result 未改，不清数据、不切 radio。

随后 `05143154-…` notification attempt 已真实启用并收到 value，但 shared [runNotifications](../../../tests/hil/apple/support/AppleCoreBluetoothRfScenario.swift) 在 value 阶段调用 `validateNotificationEventLedger(... expectDisable: !leaveActive)`。普通场景的 `leaveActive=false`，因而在 command 7 尚未派发时就要求其 terminal，立即报 `command 7 produced 0 terminals or a failed terminal`。这是 HIL 时序错误，不是 backend disable 失败，更不是 CCC disable REJECT continuity 的重现。Host 仅 ACK baseline `ef`，未进入 disable/post-disable；该 attempt-owned native/fixture 清理成功，原失败保留。

重建修复候选后的第一次准入 `8a3ae23f-…` 发现同一旧安装路径的新进程 PID `2466`；先前失败 cleanup 的最终快照已无 Native App，不能把新 PID 说成原 cleanup 遗漏，也不推断它重新出现的原因。Runner 在 fixture/安装/launch 前拒绝覆盖。用户授权关闭该 App 的范围内，第二份独立 transition `.work/apple-native-transitions/7aaec18e-e074-48cd-85f4-0ba0c1d02ae1/` 两次查询当前 SDK PID/完整安装路径后单次终止并确认退出；明确 `boot_continuity_proven=false / old_probe_close_or_resource_zero_proven=false`，不冒充原 session cleanup。

### 最小修复、回归与产物边界

仅将 value 阶段改为 `expectDisable: false`，其时点尚未派发 disable。**正常流程仍实际派发 command 7，要求正确 disabled domain 在唯一成功 terminal 前；完整结束时的严格 ledger、distinct post-disable stimulus 和静默判据均未放宽。** 生产 backend、Rust core、firmware 和已知 REJECT gate 不变。

在既有 [Swift support XCTest](../../../tests/hil/apple/support/AppleCoreBluetoothRfScenarioTests.swift) 增加真实执行 `runNotifications` 的无设备回归：使用 production ABI 的窄 notification backend，host worker 观察实际 enabled marker 后才送 value，观察 disabled marker 后才释放后续 marker。非 notification 命令拒绝，不用一次性成功事件列表替代 orchestration。旧代码精确复现同一 command 7 错误；修复后 **17 tests / 0 failed**，仍运行全部原 ledger/tuple/duplicate/late-event 回归。

重建、签名并通过 `codesign --verify --deep --strict` 的 App 是 `.work/apple-ios-runtime/NotificationGateSignedDerivedData/Build/Products/Debug-iphoneos/BleHubAppleIOSProbe.app`，hash **`c0c53e6cf80e84f7cca63bca1a1e88984e679a3e82274fb07ac174638024fe3c`**。旧 `d1e1…` App 与 IO 原证据保留；不宣称 IO/notification 在同一 App hash 上通过，也不为 HIL 单点修复重跑未受影响的整个 RN/full-suite/package 矩阵。

### 修复后的 notification 真机证据

沿用本报告 IO 命令的显式 phone/port/contract/profile，将 App 改为上述新路径、scenario 改为 `notification`。完整日志 `.work/validation/2026-10-25-iphone-native-notification-fixed-after-close.log`，原始证据 `.work/apple-native-hil/466ffa0a-0b60-4199-b0ad-b177d222bf4e/`。

- native PID `2468`、boot `4d10ff45-7f9f-4241-b665-8677aeb05d52`，与 IO 独立；每次 fixture lease 内验证 live receipt 与 pinned firmware boot。
- 组合扫描、连接/GATT、enable、真实接收 **`ef`**、正常 disable 顺序、post-disable 静默、disconnect、dispose/post-dispose ledger 均通过。
- Host 在 enabled 时提交 baseline `ef`，disabled 后提交 distinct **`ee`**；两个 `SET_VALUE` 均取得 BTP ACK，随后才释放 post-disable marker。ACK 仍不是空口 TX/delivery 证明；静默判据来自真实 Native event transcript。
- Host fresh boot challenge、Peripheral peer retirement、fixture 清理和共享 RN lock 释放通过；`primary_error=null / cleanup_errors=[] / peripheral_disconnected=true / notification_marker_released=true`。
- 成功结果页保留，没有继续关闭它或启动额外 RF；正常 disable 通过不关闭已知 iOS disable-REJECT continuity gap。

## 7. 实际验证与后续范围

| 命令/检查 | 结果 |
|---|---|
| Android consumer `:app:assembleDebugAndroidTest --offline --no-daemon -Pandroid.builder.sdkDownload=false` | 通过，`.work/validation/2026-10-25-android-native-value-build.log` |
| 新握手回归 + native host 定向回归 | 7 passed；原握手实现失败日志保留，`.work/validation/2026-10-25-instrumentation-handoff-{before,fixed}.log` |
| `tools.android.test_run_instrumentation` | **10 passed**；`.work/validation/2026-10-25-native-matrix-instrumentation-owner.log` |
| Swift RF ledger baseline | **16 passed**；仅重放 synthetic，不能关闭 no-response 契约冲突 |
| `pixi run just react-native tools-fixture-test`（native bridge 收尾） | **1239 tests，228.640 秒，OK**；`.work/validation/2026-10-25-native-matrix-final-tools-fixture.log` |
| `pixi run python -m unittest tools.tests.react_native.test_ios_radio_quarantine_restore tools.tests.react_native.test_ios_radio_lease -q` | **31 tests，OK**；held 原始状态/hash/capture 身份、父进程/组存活阻止、preflight 先后顺序、提交前漂移，以及原 quarantine/mutex/rollback 回归；`.work/validation/2026-10-25-orphaned-held-targeted.log` |
| `pixi run just react-native tools-fixture-test`（恢复机制收尾） | **1245 tests，229.887 秒，OK**；通过 `run_quiet.py --no-tty --progress-seconds 60` 保存完整输出与有界进度，未额外重跑 subset；`.work/validation/2026-10-25-orphaned-held-tools-fixture.log` |
| 恢复机制静态检查 | `pixi run just fmt`、`pixi run just lint`、`pixi run just docs-check-mermaid`、UTF-8/fences/whitespace/conflicts、`git --no-pager diff --check` 通过；Mermaid 为 27 diagrams / 170 Markdown files；日志 `.work/validation/2026-10-25-orphaned-held-{fmt,lint,mermaid}.log`；核心 67 文件与原基线一致，原 phone owner/journal hash 未变 |
| `pixi run just fmt` / `pixi run just lint` | 最终通过；`.work/validation/2026-10-25-native-matrix-final-{fmt,lint}.log` |
| 平台事件投影回归 | Apple 41 passed；Android 17 passed；generic iOS Simulator/device unsigned builds 通过；`.work/apple-native-hil/noresponse-{apple-green,android-green,apple-ios-build}.log` |
| RN `host-test` / `host-check` | 17 suites / 621 tests passed，typecheck 通过；`.work/validation/rn-write-canonical-host-{test,check-final}.log` |
| 最终 versioned native host + instrumentation 回归 | 17 passed；`.work/validation/2026-10-25-native-writes-v2-host.log` |
| `pixi run -e apple-ci python tests/hil/apple/support/test_rf_scenario.py` | 新 orchestration 回归旧代码复现失败，修复后 **17 passed**；`.work/validation/2026-10-25-native-notification-runner-{red,green}.log` |
| notification HIL candidate build/signature | Xcode development-signed build 与 `codesign --verify --deep --strict` 通过；`.work/validation/2026-10-25-native-notification-signed-build.log`、`2026-10-25-native-notification-candidate.json` |
| iPhone native IO / notification | 各自真实 session **passed**，原始 result/ledger、失败和候选边界如上；此次不重跑无实现变更的 Python combined/Jest |

本轮不重跑未改动的 Jest/clean consumer/full-suite，不增加 API-tier 或多小时负载，不把 Mac pass 转成 iOS pass。no-response 平台漏报现已按 Plan 修复并实际验证。授权后的新鲜 ON inspection 与 owner 合规归档现已通过，不再需要重复该恢复步骤。iPhone native IO 与独立 notification 现也各有真实通过，本轮约定的外围最小补项已完成，可进入 `crates/blehub`、`crates/bleng` 人工 review。后续按核心改动定向复验；最终同候选 native/runtime/ABI/package/RF、extended lifecycle 与长稳仍按 Plan 在稳定候选阶段核对。已定界的 iOS CCC REJECT gap 保持 open，不再为此增加新故障矩阵。
