# RN continuous10：同一页面/runtime 真机 RF

日期：2026-10-14。范围：本轮 continuous10 实现与 candidate 证据；不是 release/ABI freeze 或全部功能完成声明。

## 后续当前入口

统一 evidence bundle 接入后，两端已在冻结安装快照上各自重新真实连续 10/10 通过：Android `e81103e7816a42c0829af6fdfab5e9ed`、iOS `f0c851c8e19b41e49dd83df7a042d5f8`，均 host passed、kept-open，manifest 离线完整性复核通过。最新候选指纹、证据入口、使用方式与限制统一见 [RF evidence bundle 报告](2026-10-14-rn-rf-evidence-bundle.md)。以下保留 bundle 接入前十项与 optional-localName 修复的具名证据，不回填其历史 attempt。

## Bundle 接入前结论

- **continuous10 已实现，Android 修复后 candidate 实际 REAL 10/10 PASSED**：attempt `1e7f537c50c34d25b19dbacf2b8ffc31`，同一个 RN RF page/JS runtime 自动执行 normal 九项 + watchdog，host 校验通过、App kept-open；不是 normal9 与独立 watchdog 的拼接。
- 用户本轮解锁后，**iOS 新版 deterministic normal9 已实际 REAL 9/9 PASSED**：`7fc0214a04fa47e091e4066dd8b62640`，含 physical cancellation，App kept-open。此前“新版 iOS cancellation 未通过”已是历史状态。
- **iOS continuous10 现已实际 REAL 10/10 PASSED，host 校验通过、App kept-open**：本轮解锁后最终 attempt `2a89c4d5b0ac42239510fbdf7c9e6753`，十项在同一页面/JS runtime/native PID 连续执行；不是 normal9 与独立 watchdog 的拼接。官方 process query 在 host detach 后确认原 PID `41640` 仍存活。
- **同一 optional-localName 修复后的 RN/host 测试实现现已在 Android/iOS 各自完整连续通过**：Android 重建 Release APK 后 `1e7f…`，iOS `2a89…`。Android `92d8…` 降为修复前历史证据；这不升级为完整 production fingerprint、ABI freeze 或所有同候选门禁通过声明。
- Host 直接控制运行，不要求人工逐项点击；成功保留结果页。使用既有 multifunction A/B 固件，无本轮 flash，也无 production ABI/backend、firmware、nrftest 修改或新 commit。

固件 declaration/profile/contract、A/B normalized/HEX hashes、显式 provision 与 dirty-source development provenance 见 [multifunction 历史报告](2026-10-14-multifunction-nrf-rn-rf.md)。A/B 是同实现、不同 compiled profile identity 的并行隔离 variants，不是 bit-identical HEX；普通 run 从不隐式刷机。本轮不升级为 clean locked release。

## Candidate 与原始证据

以下路径为本机 ignored artifacts；机器身份只在报告记录，不进入通用 recipe 默认值。

| Gate / artifact | 本轮结果与身份 |
|---|---|
| Android continuous10 | `1e7f537c50c34d25b19dbacf2b8ffc31`；REAL 10/10、host 校验通过、kept-open |
| Android APK SHA-256 | `e659814cde504fc2d4eb759c23cdb436967e752f62a3f11dce7d4d59040d7f58` |
| Android App PID / JS thread | `11545` / `11682` |
| Suite session / JS nonce | `f129fd7d-29cd-4e17-8ed7-0f7ef8a0ceb6` / `mv7yhri2-ygwusekm9h`，start/close 一致 |
| Fixture PID/PGID / resource | `23228` / `23228`；`jlink:1050252028`，两个 physical flow 与 live receipt 一致 |
| Installed image | `nrf-multifunction-rf-v1-a`；normalized `dd7dea99a325db32fa1fd66648d0a0814c8c7c442a77cf08998f2b91a1cc5b7a` |
| iOS deterministic normal9 | `7fc0214a04fa47e091e4066dd8b62640`；REAL 9/9、kept-open |
| iOS continuous10 当前 pass | `2a89c4d5b0ac42239510fbdf7c9e6753`；REAL 10/10、host 校验通过、kept-open |
| iOS 当前 `main.jsbundle` SHA-256 | `0681b8b25412b2f4b90820b6feec9a005aad5894437f134813a91f609e50812e`；compile/sign succeeded |
| iOS App PID / native boot | `41640` / `6c5f385c-456b-42cc-96a1-522a0d8c8091` |
| iOS suite session / JS nonce | `ce22315e-b4f3-4d14-8ef8-98aadf13576b` / `mv7y4f16-3oml3p2g5ml`，start/close 一致 |
| iOS fixture PID/PGID / resource | `18096` / `18096`；`jlink:1050252028`，两个 physical flow 与 live receipt 一致 |
| iOS 历史启动阻塞 | `13dd789b52b54d0da7b2e7fd7e3dc977`、`6932311b94ec473192d453e6c1788691`；startup Locked、未进入 RF |
| iOS 本轮 early failures | `5bc0a2f4f11241dd8301a0b9a7f9904c` 九项通过、watchdog 等待广播名超时；`710ddb8c855a476bbc7411ee33a6d739` RN 10/10 但 host 拒绝缺少广播名；两者均非最终 gate pass |

Android 同一 attempt 的证据入口：

- [Continuous runtime log](../../../tests/consumers/react-native-internal/android/build/rf-lab-suite-1e7f537c50c34d25b19dbacf2b8ffc31/rf-lab-suite-continuous-runtime.log)、[pinned runtime identity](../../../tests/consumers/react-native-internal/android/build/rf-lab-suite-1e7f537c50c34d25b19dbacf2b8ffc31/rf-lab-suite-continuous-runtime.identity.json)
- [Flow 8 cancellation physical evidence](../../../tests/consumers/react-native-internal/android/build/rf-lab-suite-1e7f537c50c34d25b19dbacf2b8ffc31/coordination/flow-8-rf-cancellation/physical-evidence.json)
- [Flow 9 watchdog physical evidence](../../../tests/consumers/react-native-internal/android/build/rf-lab-suite-1e7f537c50c34d25b19dbacf2b8ffc31/coordination/flow-9-rf-watchdog/physical-evidence.json)
- [Frozen fixture request](../../../tests/consumers/react-native-internal/android/build/rf-lab-suite-1e7f537c50c34d25b19dbacf2b8ffc31/coordination/fixture-session/fixture-request.json)、[live attestation receipt](../../../tests/consumers/react-native-internal/android/build/rf-lab-suite-1e7f537c50c34d25b19dbacf2b8ffc31/coordination/fixture-session/fixture-receipt.json)

## 连续执行与物理边界

实现复用同一 provider owner/physical lease/BTP session/live attestation。RN App mount 一次，suite 通过 session + runtime nonce 证明连续性；flow 可创建/关闭各自 Probe/Hub 和连接，**不声称十项共用一个 Hub 或一条连接**。Flow 8 为 cancellation，flow 9 为 watchdog，host 下发同 session、不同 token 的 control bundle。

1. Flow 0–7：scan、connect、GATT、I/O、RSSI、Notification、strict Indication、passive disconnect。
2. Flow 8：真实 ARM/WAITING 后单次 active cancellation 拒绝、queued cancellation 等待 exact `Cancelled` terminal，再 release、active completion/recovery、physical close。
3. Flow 8 关闭 exact peer 后，在 live no-peer 边界恢复 canonical values，并对 readable values 做本地 ATT readback；每次 mutation/readback 前后检查无 peer，拒绝意外 GAP peer event。保留原 ATT handles，不 reset GATT、不 reopen BTP、不重刷固件。
4. 同一页面继续 flow 9；Android scan backoff 在 ARM 前完成，避免消耗 hold budget。复用 `Experiment.execute`，不调用 standalone `run` 的 App launch。
5. iOS 要求同一 native boot/PID，以及 cancellation 三组 + watchdog 四组共七组 native/JS 双 ACK；最终 `2a89…` 已实际全部验证。

源码入口：[App.tsx](../../../tests/consumers/react-native-internal/src/App.tsx)、[rf_signal.ts](../../../tests/consumers/react-native-internal/src/rf_signal.ts)、[rf_watchdog.tsx](../../../tests/consumers/react-native-internal/src/rf_watchdog.tsx)、[cancellation_control.py](../../../tests/peripherals/nrf52840/cancellation_control.py)、[continuous_watchdog.py](../../../tests/peripherals/nrf52840/continuous_watchdog.py)、[Experiment.execute](../../../tools/react_native/run_rf_watchdog.py)、[RF control relay](../../../tools/react_native/rf_control_relay.py)。

### Android 已验证 candidate 的 RF ledger

- Flow 8：hold sequence `16`、target handle `41`，physical `status=passed`；release budget `7.0s`，结束 hold inactive、peer count `0`、cleanup `[]`。
- Canonical restore：read-write、updates、indications 为 `00`，read-only 为 `426c65487562`；write-only 设置 `00`，因不可读而无本地 ATT readback，不将它记录为已读回。
- Flow 9：新 sequence `17`、同 target handle `41`；WAITING → active `21010` native Timeout、queued `21011` OperationAborted → **仍 WAITING** → explicit release/RELEASED → exact old-peer GAP disconnect。
- Host post-release old-route silence **1006.378417ms**，之后才允许 recovery；新连接读 `21024` 返回 `00`。13-command native terminal ledger 完整，physical cleanup 为 hold inactive、no peers、advertising stopped、errors `[]`。
- 最终日志十个 `flow-passed` 与同 nonce start/close，并输出：

```text
rf-lab-suite:passed:flows=10:passedCount=10:failureCount=0
```

成功要求相同 hold generation 的严格 RELEASED，不以 ABORTED/EXPIRED 充当正常 release；release-request→RELEASED **严格小于 3s**，ARM→RELEASED **严格小于 24s**。本地证据分别约 `0.003218s`、`10.381096s`，都满足预算。`RELEASED` 只证明 Peripheral callback 已释放；隔离依据是 release 后物理断开、host silence、持续 native ledger 和恢复读，不声称已观察到平台晚 callback。

首次 Android continuous10 `6d1e80b2ac564ba4bcb9ff32e09d9274` 与后续 canonical/release 加固的 `92d8bdad9c0a4e9f8cecb8f2337e577d` 保留为历史 candidate。**当前 Android 主 pass 为 optional-localName 修复后的 `1e7f…`**。Host run 后 `adb pidof com.blehubnitrohost` 仍为 pinned PID `11545`，并通过 Android single-image gate（2 UniFFI ABI slices / 2 Nitro slices）；不把重新启动 App 当作 kept-open。

## iOS：同一页面/runtime continuous10 完整通过

最终 [`2a89…` runtime log](../../../tests/consumers/react-native-internal/ios/build/rf-lab-suite-2a89c4d5b0ac42239510fbdf7c9e6753/device-rf-lab-suite-continuous.log) 记录顺序十项各一次通过、相同 session/nonce 的 start/close，以及 `rf-lab-suite:passed:flows=10:passedCount=10:failureCount=0`。Host runner exit `0`、`ios-rf-lab-suite:passed`、`app=kept-open`，fixture 已停止。

- [Native identity](../../../tests/consumers/react-native-internal/ios/build/rf-lab-suite-2a89c4d5b0ac42239510fbdf7c9e6753/device-rf-lab-suite-continuous.attempt.rf-host-identity.json)：`status=passed`，同一 PID `41640` / boot，七组 native/JS ACK 均完成，无 failure。
- [Cancellation physical evidence](../../../tests/consumers/react-native-internal/ios/build/rf-lab-suite-2a89c4d5b0ac42239510fbdf7c9e6753/coordination/flow-8-rf-cancellation/physical-evidence.json)：sequence `14`、handle `41`；ARM/WAITING → active `operation_not_cancellable` rejection → queued exact `Cancelled` terminal → still WAITING → explicit RELEASED → active completion/recovery；close 后 hold inactive、peer count `0`、cleanup `[]`。
- [Watchdog physical evidence](../../../tests/consumers/react-native-internal/ios/build/rf-lab-suite-2a89c4d5b0ac42239510fbdf7c9e6753/coordination/flow-9-rf-watchdog/physical-evidence.json)：同一 fixture session/PID/resource，下一 sequence `15`、handle `41`；active `21010` native Timeout、queued `21011` OperationAborted 后 DK 仍 WAITING，显式 RELEASED 后 exact old-peer GAP disconnect、host silence **1011.253708ms**，之后才恢复广告/连接，`21024` read `00`。13-command terminal ledger 完整，hold inactive、no peers、advertising stopped、cleanup errors `[]`。
- Watchdog release-request→RELEASED 约 **0.003130s**、ARM→RELEASED 约 **11.002846s**，分别严格小于 3s/24s，未放宽预算。与 Android 一样，不把 RELEASED 或静默窗口解释为已观察到平台晚 callback。
- [Console cleanup](../../../tests/consumers/react-native-internal/ios/build/rf-lab-suite-2a89c4d5b0ac42239510fbdf7c9e6753/device-rf-lab-suite-continuous.attempt.console-cleanup.json) 为 `action=detached`；[suite 后官方 process query](../../../tests/consumers/react-native-internal/ios/build/rf-lab-suite-2a89c4d5b0ac42239510fbdf7c9e6753/post-suite-processes.json) 确认同一 PID/executable 仍存活，不是重新启动结果页。

### 本轮测试侧修复与早期失败

解锁后 `5bc0…` 已真实通过前九项（含 cancellation），watchdog 收到正确 provider service UUID，但 `localName` 缺失，被 RN 必填名称谓词挡住。修复 RN 初始/恢复 scan 为 **optional-but-validate-when-present** 后，`710ddb…` 的 RN 十项及物理 watchdog 闭环全部完成，但 host physical checker 仍把广播名当必填，因此 exit nonzero 并触发 failure cleanup；**该次不计完整 gate pass**。

最终同步 host checker 的广播名策略：正确 service UUID 仍必需；广播名称若出现，必须与 frozen profile 匹配；恢复 scan 仍要求同一 peripheral。此策略与 normal9 一致，且并不放宽 BTP installed-image attestation 中必填的 local name、profile/hash/capability identity、GATT topology、peer、hold generation 或 release budget。RN 回归覆盖缺少名称在初始/恢复扫描通过，显式错误名称在两处拒绝；host 回归同样拒绝任一广播的显式错误名称。修复仅在测试层，本轮无 production backend/ABI 或固件修改，未重新刷机。

### 历史 normal9 与 Locked 阻塞

用户解锁后，[`7fc021…` normal9 runtime log](../../../tests/consumers/react-native-internal/ios/build/rf-lab-suite-7fc0214a04fa47e091e4066dd8b62640/device-rf-lab-suite-continuous.log) 实际通过九项，含新版 cancellation；[flow 8 physical evidence](../../../tests/consumers/react-native-internal/ios/build/rf-lab-suite-7fc0214a04fa47e091e4066dd8b62640/coordination/flow-8-rf-cancellation/physical-evidence.json) 为 passed，hold sequence `6`、target `41`、physical close hold inactive/peer count `0`/cleanup `[]`。[Native host identity](../../../tests/consumers/react-native-internal/ios/build/rf-lab-suite-7fc0214a04fa47e091e4066dd8b62640/device-rf-lab-suite-continuous.attempt.rf-host-identity.json) 记录同一 bootstrap PID/boot 与 armed/held/released 三组 native/JS ACK，`status=passed`。这解决此前新版 iOS normal9 的缺失 gate，但不是 continuous10。

随后 continuous10 App compile/sign 成功，[`13dd789…` controller log](../../../tests/consumers/react-native-internal/ios/build/rf-lab-suite-13dd789b52b54d0da7b2e7fd7e3dc977/device-rf-lab-suite-controller.log) 记录系统 launch Locked；[host identity](../../../tests/consumers/react-native-internal/ios/build/rf-lab-suite-13dd789b52b54d0da7b2e7fd7e3dc977/device-rf-lab-suite-continuous.attempt.rf-host-identity.json) 为 failed、bootstrap null、无 signal ACK，未进入 RF。**该历史 attempt NOT pass**，不借用 normal9 或独立 watchdog A/B pass 补成 10/10；当前十项 pass 来自上述独立完整重跑 `2a89…`。

随后历史重试 [`693231…` launch log](../../../tests/consumers/react-native-internal/ios/build/rf-lab-suite-6932311b94ec473192d453e6c1788691/device-rf-lab-suite-continuous.attempt.log) 同样记录 `FBSOpenApplicationErrorDomain` 的 `Locked`（CoreDevice error `10002`）；host runner exit `2`，runtime controller exit `1`，fixture cleanup 输出 `nrf52840_fixture=stopped`。这是启动前置条件阻塞，不是 RF assertion failure，不触发刷机或修改测试通过条件。

## 软件验证与未完成项

本次 optional-localName 修复后实际重跑：RN Jest **96 tests passed**（两套，包含 watchdog 69 项）、RN typecheck、`tools/react_native` **221 tests passed**、focused Python **77 tests passed**、`pixi run just peripheral nrf52840-test`、`pixi run just fmt`、`pixi run just lint` 均通过。iOS physical-device Debug App（包含重新生成的 Metro/Hermes bundle）编译/签名与完整 continuous10 RF host-run 通过。随后 Android 在同一修复后的 RN/host 实现上重建 Release APK，完整 continuous10 host-run 与 single-image gate 通过；APK/hash/physical evidence 以上述 `1e7f…` 为当前证据。最终文档检查实际通过：`pixi run just docs-check-mermaid`（128 个 Markdown 文件中的 24 个图）、`git diff --check`、三个相关文档的 UTF-8/代码围栏/尾随空白/冲突标记检查。两份改动的 Peripheral Python 文件额外通过 Ruff check/format check；首次 format check 指出测试文件一个 `with` 语句的换行格式，已格式化并重跑 checker unit tests（13 项通过）。

iOS 同 page/runtime、boot/PID、七组 native/JS ACK、两个 physical flow 与最终 summary/cleanup 门禁已由 `2a89…` 完整补齐。Android 修复后的重建/完整 RF 重跑现已由 `1e7f…` 补齐。该阶段仍待 adapter/permission transitions、Android pending-operation reload、完整统一 production fingerprint/逐 flow provenance、适用 native/package 门禁、clean locked firmware release 与 ABI freeze；统一 bundle 后续已实现，具体剩余 provenance 边界以新报告为准；两端同一修复后 RN/host 测试代码各有真实 continuous10 通过证据，不代表所有功能、完整 production provenance 或 same-candidate 全门禁完成。
