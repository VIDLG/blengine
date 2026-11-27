# Multifunction nRF 固件与 RN 真机 RF

日期：2026-10-14。范围：本轮固件身份、分次 RF 结果、失败边界与后续门禁；不修改 PLAN，不声明 release/ABI freeze。

## 当前入口与历史范围

本报告保留 multifunction 安装身份、分次 normal9/watchdog 和 early failure 的阶段性证据，不继续追加 continuous10 时间线。后续两端先在 optional-localName 修复后各自连续 10/10 通过，详见 [continuous10 报告](2026-10-14-rn-continuous10-rf.md)。**统一 evidence bundle 接入后又实际重跑：Android `e81103e7816a42c0829af6fdfab5e9ed`、iOS `f0c851c8e19b41e49dd83df7a042d5f8` 均连续真实 10/10、host passed、`app=kept-open`，manifest 离线完整性复核通过。**当前入口、冻结安装快照指纹与剩余 provenance 限制统一见 [RF evidence bundle 报告](2026-10-14-rn-rf-evidence-bundle.md)。不把较早 attempt 回填为新 bundle，也不声明 release 冻结、source-to-build attestation 或全门禁完成。下列阶段性结论/attempt/软件验证保留历史范围。

本轮仅测试侧修正 watchdog RN/host 将 localName 设为必填：iOS 广播含正确 service、但无 localName，改为与 normal9 一致的 **optional-but-validate-when-present**；service/GATT/attestation/peer/budget 未放宽。无 flash、production ABI/backend、firmware 或 nrftest 修改；此前 Locked、前九项通过但 watchdog 扫描超时的 `5bc0…`、RN 10/10 但 host 拒绝的 `710ddb…` 均不计本轮 pass，完整时间线由上述 continuous10 报告维护。

## 阶段结论（历史快照）

- 长期使用**同一个 multifunction 固件**覆盖 normal 与 watchdog；未 ARM 时普通 I/O，显式 ARM 后才 hold，不再为了两类 flow 换 image。`provision` 是独立显式操作，普通 `run` 不隐式刷机。
- A/B 均已 build、显式 provision/BTP installed-image attestation 通过；B 自 provision 后未换 image。两块板使用相同 code/settings/capabilities 的 compiled profile-identity variants 支持并行隔离，**不是 bit-identical HEX**。
- Android B、iOS B/A **独立 watchdog RF 均通过**。iOS B normal 连续 REAL 9/9 已在 Apple retention 修复后重跑，kept-open；但其旧 cancellation race 试探已 superseded。
- 当前 normal9 严格要求 live-attested multifunction 与 physical WAITING barrier。Android A **本阶段 exact-peer/BTP deadline/ACK deadline 加固 host `88ac2e14b14c4c62a473a58be8578859` 连续 REAL 9/9 PASSED、App kept-open**，该阶段 pass 以此为准；`59b476…` 保留为较早 host pass。
- **当时新版 iOS cancellation gate 未通过**：`ef321…` 已真实通过前八项、bootstrap identity verified，但 armed relay official process query 因非法 CLI `--timeout 1` 失败；修正后该阶段 `9d4e50db662b45b589a5b4c656fe16e2` 实际重跑 launch Locked，未进入 RF。独立 watchdog pass 不变，也不能继承给 cancellation。
- 链路为 `RN → Nitro → production BleHub/backend → phone radio → nRF`，没有 fake RF。**当时同一个 RN page/runtime 统一十项尚未实现**，该阶段分次结果不拼成 10/10，不声明 same-candidate 全门禁通过；后续实现与 Android/iOS 各自真实 10/10 见当前入口。

长期方向与分层规则见 [RN RF Lab 架构](../../research/react-native/react-native-rf-lab-test-architecture.md)。[2026-10-13 normal](2026-10-13-normal-attested-rn-rf-lab.md) 与 [watchdog 专用 image](2026-10-13-rn-native-watchdog-rf.md) 保留为历史证据，不是当前换 image 策略。

## 固件身份与显式安装

机器身份仅记录于本报告，不作为通用 recipe 默认值：

Board 均为 `nrf52840dk/nrf52840`。早期 A untouched normal A 的状态已被本次显式 provision 取代。

| 项目 | DK-A 当前身份 | DK-B 当前身份 |
|---|---|---|
| USB / J-Link | `001050252028` / `1050252028` | `001050251479` / `1050251479` |
| VCOM | `/dev/cu.usbmodem0010502520281` | `/dev/cu.usbmodem0010502514791` |
| Image | `nrf-multifunction-rf-v1-a` | `nrf-multifunction-rf-v1-b` |
| Normalized image SHA-256 | `dd7dea99a325db32fa1fd66648d0a0814c8c7c442a77cf08998f2b91a1cc5b7a` | `9937f35d591fcb9bff86492b4b82841c4050fdb5901b460974635ba64a367f2b` |
| Raw HEX SHA-256 | `bc34439421b5ea36b321d623c3acce79d988962edcaba94ab668f353236749bf` | `4ba778494638e1eff4e34f62ba6b0772e92a2ab4cd39abda8694e2401d23782f` |
| Profile | `blehub-nrf52840-multifunction-v1-a` | `blehub-nrf52840-multifunction-v1-b` |
| Service | `fd500000-0000-4000-8000-000000000000` | `fd400000-0000-4000-8000-000000000000` |
| Local name | `BleHubMultiRfDKA` | `BleHubMultiRfDK` |

Tracked 输入：

- Image declaration：[A](../../../tests/peripherals/nrf52840/firmware/images/multifunction-rf-v1-a.json) / [B](../../../tests/peripherals/nrf52840/firmware/images/multifunction-rf-v1-b.json)
- Canonical profile：[A](../../../tests/conformance/scenarios/blehub-nrf-multifunction-v1-a.json) / [B](../../../tests/conformance/scenarios/blehub-nrf-multifunction-v1-b.json)
- Fixture contract：[A](../../../tests/conformance/scenarios/fixture-contracts/multifunction-rf-v1-a.json) / [B](../../../tests/conformance/scenarios/fixture-contracts/multifunction-rf-v1-b.json)

ignored local config 已将 A/B profile 映射到各自 installed multifunction，保留 serial。profile identity 不同用于并行隔离，不是为了 normal/watchdog 换 image；普通 run 从不刷机。

安装与构建证据（nrftest 本机 ignored artifacts）：

- Build manifest：[A](../../../../nrftest/.work/build-attested/images/nrf-multifunction-rf-v1-a/nrftest-build-manifest.json) / [B](../../../../nrftest/.work/build-attested/images/nrf-multifunction-rf-v1-b/nrftest-build-manifest.json)
- Installed BTP proof：[A](../../../../nrftest/.work/reports/multifunction-v1-a-installed-2026-10-14.json) / [B](../../../../nrftest/.work/reports/multifunction-v1-b-installed-2026-10-14.json)
- Provision report：[A `20261014T031604`](../../../../nrftest/.work/reports/firmware-provision-dk/20261014T031604.890851Z-bd125373f3664f229ae9335bf6a636a7.json) / [B `20261014T011358`](../../../../nrftest/.work/reports/firmware-provision-dk/20261014T011358.548050Z-0e21053a9b564d4ba7c27527c4beb2eb.json)：均 `passed`，proof 为 post-flash installed-image attestation。

Normalized digest 与 raw HEX hash 不同；前者绑定固件回报身份，后者确认实际 flash artifact。本地 manifest 本身不是 installed proof；本轮有真实 BTP readback，RF session 另有 live receipt。

**Provenance 限制：**这是使用 `--allow-local-tester-changes` 的 dirty-source development 构建。Zephyr HEAD 为 `d31eebc4557c7a0a8a64e77b9642ee9a341661f4`，manifest 保存 Tester source snapshot/local changes；HEAD 不代表本轮未提交修改已发布。不是 clean locked release，没有新 commit，不能扩展为完整 release reproducibility 证明。

Declaration 同时声明 normal capabilities 与 `att-read-hold`、`att-read-hold-timeout-30000`、`att-read-hold-runtime`；`CONFIG_BTTESTER_GATT_READ_HOLD_RUNTIME=y`。Canonical profile 不请求 authorization，未 ARM 普通读 `00`；host 在 watchdog 和新版 cancellation flow 显式 ARM/release。Peripheral hold 为 30s，production native watchdog default 仍为 10s。

## 本阶段 RF 结果（continuous10 前）

| Gate | 结果 | 原始证据 |
|---|---|---|
| Android B normal 九项（历史旧 gate） | REAL 9/9，kept-open；旧 cancellation superseded | [`e610bfd8` log](../../../tests/consumers/react-native-internal/android/build/rf-lab-suite-e610bfd8e12a4c509f8299a956b70cf1/rf-lab-suite-continuous-runtime.log) |
| iOS B normal 九项（Apple 修复后、旧 gate） | REAL 9/9，kept-open；旧 cancellation superseded | [`a37c9695` log](../../../tests/consumers/react-native-internal/ios/build/rf-lab-suite-a37c96951fb34714bd83dbb5ab8235a0/device-rf-lab-suite-continuous.log) |
| Android B 独立 watchdog | 完整通过 | [`8ad60add` evidence](../../../.work/react-native/rf-watchdog/8ad60add-2399-4389-a13d-bd58f3b406c2/evidence.json)、[runtime log](../../../.work/react-native/rf-watchdog/8ad60add-2399-4389-a13d-bd58f3b406c2/runtime.log) |
| iOS B 独立 watchdog（Apple 修复后） | 完整通过 | [`11ba2013` evidence](../../../.work/react-native/rf-watchdog/11ba2013-df47-4091-9fd5-8636fffdf83e/evidence.json)、[runtime log](../../../.work/react-native/rf-watchdog/11ba2013-df47-4091-9fd5-8636fffdf83e/runtime.log) |
| iOS A 独立 watchdog | 完整通过 | [`56cf3b78` evidence](../../../.work/react-native/rf-watchdog/56cf3b78-e9c3-4b89-a3f2-b6cf5d30d01c/evidence.json)、[runtime log](../../../.work/react-native/rf-watchdog/56cf3b78-e9c3-4b89-a3f2-b6cf5d30d01c/runtime.log) |
| Android A normal9 新物理 gate（本阶段 pass） | 最终加固 host `88ac…` 连续 REAL 9/9 PASSED、App kept-open | [Runtime log](../../../tests/consumers/react-native-internal/android/build/rf-lab-suite-88ac2e14b14c4c62a473a58be8578859/rf-lab-suite-continuous-runtime.log)、[physical evidence](../../../tests/consumers/react-native-internal/android/build/rf-lab-suite-88ac2e14b14c4c62a473a58be8578859/coordination/flow-8-rf-cancellation/physical-evidence.json) |
| Android A normal9 新物理 gate（较早 pass） | `59b476…` REAL 9/9，仅适用于最终加固前的 host | [Runtime log](../../../tests/consumers/react-native-internal/android/build/rf-lab-suite-59b476a33f84478f8ae7a476f5709d0c/rf-lab-suite-continuous-runtime.log)、[physical evidence](../../../tests/consumers/react-native-internal/android/build/rf-lab-suite-59b476a33f84478f8ae7a476f5709d0c/coordination/flow-8-rf-cancellation/physical-evidence.json) |
| iOS A normal9 新物理 gate（历史） | `ef321…` 前八项/bootstrap 已验证；当时最新 `9d4e…` launch Locked、未 RF；该阶段 NOT pass，后续已通过 | [当时 controller log](../../../tests/consumers/react-native-internal/ios/build/rf-lab-suite-9d4e50db662b45b589a5b4c656fe16e2/device-rf-lab-suite-controller.log) |
| 同 page/runtime 统一十项（当时） | 尚未实现、未执行；后续已实现 | 本表分次证据不相加为 10/10；后续真实连续 Android/iOS 10/10 见新报告 |

normal 九项为 scan、connect、GATT、I/O、RSSI、Notification、strict Indication、passive disconnect、cancellation；通过轮各含 `flows=9:passedCount=9:failureCount=0`。unarmed I/O 初始读 `00`、strict Indication 与 passive disconnect 有真实 marker，normal 路径不依赖切回 normal image。旧 cancellation 的单次通过保留为历史，但不能证明当前 physical gate；Apple 修复前 iOS [`228588b6` 9/9](../../../tests/consumers/react-native-internal/ios/build/rf-lab-suite-228588b6e88040c8b080ed8cfe8a6129/device-rf-lab-suite-continuous.log) 同样不替代新 gate。

### Android watchdog 通过边界

Attempt `8ad60add-2399-4389-a13d-bd58f3b406c2`：

1. Live proof 后配置目标；ARM `handle=41, sequence=3`，真实 ATT read 到 `WAITING`。
2. Active `21010` native `timeout`；queued `21011` 为 `operation_aborted`。host 复核 Peripheral **仍 WAITING**，不是 Peripheral 自己先超时。
3. 显式 release → `RELEASED` → old peer physical GAP disconnect。
4. host 在 release/物理断开后、recovery advertising 前独立观察旧路由 silence **1198.681 ms**，不是仅使用 release 前 RN silence marker。
5. 新连接恢复读 `21024` 返回 `00`；13-command ledger exactly-once，cleanup 无错误：hold inactive、advertising stopped、known peers 为零。

`RELEASED` 只证明 Peripheral callback 已释放，不证明 Android/CoreBluetooth 晚 callback 已上达；隔离结论来自 post-release window 与持续 ledger 的无 old-route event、exact terminals 和新连接恢复。

### iOS watchdog：历史失败 → 修复 → RF 通过

历史 attempt [`7bc25e96-6412-492d-a152-e191c5eb157e`](../../../.work/react-native/rf-watchdog/7bc25e96-6412-492d-a152-e191c5eb157e/evidence.json) 实际经历 WAITING、native timeout、queued abort、RELEASED 与 reconnect，但 recovery `21024` 同步拒绝：

```text
rf-watchdog:failed:dispatch rejected: 21024:invalid_state:a retired CoreBluetooth callback route has not reached a safe reuse boundary
```

源码核对发现 `CallbackBarrier` 之前只 retain peripheral，没有 retain key 对应的 characteristic；`ObjectIdentifier` 的唯一性只在对象存活期间成立。Apple 已改为有界 retain exact characteristic，保留既有 fail-closed capacity 约束，相关 deterministic tests 共 78 项通过。源码：[GattIoController.swift](../../../platforms/apple/Sources/BleHubApple/GattIoController.swift)；测试：[GattIoControllerTests.swift](../../../platforms/apple/Tests/BleHubAppleTests/GattIoControllerTests.swift)。

**证据分级：**characteristic lifetime/retention 缺口是源码确认的问题；该次 RF 是否确实发生地址复用仍是推断。原始 RF 日志没有 object addresses；后续修复产物 RF 通过也不能反推原始故障已观察到地址复用。

重建后的历史 attempt [`ceb121f6`](../../../.work/react-native/rf-watchdog/ceb121f6-1aa0-4afb-b1af-4765a5d0b0a3/evidence.json) 曾被 Locked 拒绝，未 ARM/进入 RF。随后 **B watchdog `11ba2013-df47-4091-9fd5-8636fffdf83e` PASSED**：ARM sequence `4`、target handle `41`，native active Timeout/queued OperationAborted 后仍 WAITING，release/物理 GAP disconnect 后 old-route silence **1003.346625ms**；13-command native terminal ledger、恢复读 `00`、`cleanup_errors=[]` 和 `physical_cleanup_errors=[]`。修复后的 App bundle SHA-256 为 `176e840e0ea3e8c2af4f782793e3a9159751effb31b2f82bcb8839c2061cc638`。

B normal `a37c96951fb34714bd83dbb5ab8235a0` 在 Apple 修复后也完成 9/9、kept-open，但旧 cancellation race probing 已 superseded。A installed proof 后，独立 watchdog `56cf3b78-e9c3-4b89-a3f2-b6cf5d30d01c` 同样 PASSED，恢复读 `00`、cleanup 无错误。这些是分次 watchdog/normal 证据，不是新版 iOS cancellation 或统一十项结果。

## 其他 blocker 与修复边界

- Permission blocker [`bc42fcb9`](../../../.work/react-native/rf-watchdog/bc42fcb9-9b15-4be1-9c7a-b8f351fdfdd4/evidence.json)：GET_ATTRIBUTES 回报 `0x43`，旧 checker 错按 ADD_CHARACTERISTIC 输入 `0x03` 比较。Zephyr native `PREPARE_WRITE=0x40` 与 BTP authorization input `0x40` 不是同一语义；GET_ATTRIBUTES 暴露 native `attr->perm`，不能据此推断 authorization。已按显式输入/native mapping 修正，保留 profile/permission/UUID 严格检查，没有放宽为任意 permission；见 [gatt_commands.py](../../../tests/peripherals/nrf52840/gatt_commands.py)。
- Android 早期失败 [`c85cdf2e`](../../../.work/react-native/rf-watchdog/c85cdf2e-7fef-4647-9671-27bb52a868b8/evidence.json)：ring-log 覆盖两个 marker，不能判为完整通过。runner 改为 pinned-PID 连续 logcat capture、保留 raw stream 并检查 PID/prefix/marker 单调性，软件测试后 `8ad60add` 实机复测通过。
- iOS `7bc25e96` 另有 app cleanup 错误：旧 terminate 调用缺少 `--pid`，不掩盖 primary RF failure；fixture physical cleanup 无错误。已改为从 launch JSON 固定 PID、核对 owned process，再用 PID terminate；**软件测试通过，修复后的物理 failure-cleanup 路径尚未验证**。Locked 重试没有启动 RN App，不能代替该验证。runner 与测试：[run_rf_watchdog.py](../../../tools/react_native/run_rf_watchdog.py)、[test_rf_watchdog.py](../../../tools/react_native/test_rf_watchdog.py)。

## Normal9 cancellation root fix 与历史阻塞

Android A 初轮 [`0687f17ad68649729bfa1307fd6dd137`](../../../tests/consumers/react-native-internal/android/build/rf-lab-suite-0687f17ad68649729bfa1307fd6dd137/rf-lab-suite-continuous-runtime.log) 实际为 8/9：`active cancellation returned accepted`。旧 flow 用调度/试探推断 native active，read 仍 queued 时 cancel accepted 是合法 race，不是 multifunction 固件失败。

当前 gate 严格仅接受 live-attested multifunction；共享 [cancellation_control.py](../../../tests/peripherals/nrf52840/cancellation_control.py)、[rf_signal.ts](../../../tests/consumers/react-native-internal/src/rf_signal.ts) 与 [RF control relay](../../../tools/react_native/rf_control_relay.py) 在同一 lease/BTP session 上执行：ARM → exact sequence/target/peer WAITING physical-ready → **单次** active cancel rejection → queued cancel accepted 并等待 exact `Cancelled` terminal → still WAITING → release/RELEASED → active completion/recovery。control signal 只证明边界，不伪造 BLE event；保存 `coordination/flow-8-rf-cancellation/physical-evidence.json`，拒绝旧 probing fallback。

此前 Android A `59b476a33f84478f8ae7a476f5709d0c` 较早 host 新 gate 9/9 通过：physical sequence `2`、handle `41`、ARM/WAITING/still-WAITING/release/closed 顺序完整，hold inactive、peer count `0`、cleanup `[]`。串口 root fix 为 first-byte blocking + available-byte nonblocking drain，消除小 BTP frame 的 `read(4096)` 约 1s 延迟；见 [serial_transport.py](../../../tests/peripherals/nrf52840/serial_transport.py)。

新版 iOS gate 时间线（早期尝试合并保留，不逐次扩展列表）：[`f9c4faa5`](../../../tests/consumers/react-native-internal/ios/build/rf-lab-suite-f9c4faa5ba89407994e4956cb62c584e/device-rf-lab-suite-controller.log) 的 console-attached/launch-JSON 缺失促成 scoped native bootstrap、官方 process-before-ARM 与 native/JS 双 ACK；[`ee4489a0`](../../../tests/consumers/react-native-internal/ios/build/rf-lab-suite-ee4489a0600f49c5ab22f94c988ffd91/device-rf-lab-suite-controller.log) 的 10s startup window 改为 launch-anchored 有界 45s；[`443029…`](../../../tests/consumers/react-native-internal/ios/build/rf-lab-suite-44302941fac742e49d5a4fdadf37ce21/device-rf-lab-suite-controller.log) 是早期 Locked，已不是 latest。

- 后续预检曾为 Unlocked，[`ef32144f8c2c4f18b23fb22ed3165d24`](../../../tests/consumers/react-native-internal/ios/build/rf-lab-suite-ef32144f8c2c4f18b23fb22ed3165d24/device-rf-lab-suite-controller.log) 真正执行并通过前八项，scoped bootstrap identity 已与官方 process 验证；首次 armed relay 的 process query 却以 `devicectl --timeout 1` 返回 exit `64`，尚未取得 cancellation pass。本机确认该 CLI minimum 为 5s；[Python root fix](../../../tools/react_native/rf_control_relay.py) 使用 CLI `max(5, budget)`，phase query/relay 仍由外层 subprocess **1.5s** 限时，**ARM release 7s / native-JS ACK 2s 均未放宽**。CLI 参数下限不是 host 等待预算。
- **当时实际重跑 [`9d4e50db662b45b589a5b4c656fe16e2`](../../../tests/consumers/react-native-internal/ios/build/rf-lab-suite-9d4e50db662b45b589a5b4c656fe16e2/device-rf-lab-suite-controller.log)**：root fix 后系统 launch Locked，未进入 RF。该阶段新版 iOS cancellation 未通过；后续解锁后的真实 pass 见新报告。Unlocked 预检不是后续 launch 保证，独立 watchdog A/B pass 不变。

Review 随后进一步加固 ARM-peer 与 exact disconnect correlation、[BTP absolute request deadline](../../../tests/peripherals/nrf52840/btp_session.py)，以及 [native/JS signal hard 2s ACK deadline](../../../tools/react_native/rf_control_relay.py)，并有软件测试。**`59b476…` 只适用于这些最终加固前的 host**。

最终加固 host Android A attempt **`88ac2e14b14c4c62a473a58be8578859` 连续 REAL 9/9 PASSED、App kept-open**。本地 [physical evidence](../../../tests/consumers/react-native-internal/android/build/rf-lab-suite-88ac2e14b14c4c62a473a58be8578859/coordination/flow-8-rf-cancellation/physical-evidence.json) 经 terminal 精确核对：hold generation/sequence 为 `4`、target handle `41`，ARM/WAITING/still-WAITING/RELEASED 的 peer 均为 `58f46182e66b`、address type `1`；声明 `release_budget_seconds=7.0`，release 在该预算内。边界依序为 armed → waiting → still-waiting → release-requested → released → closed，最终 `hold_inactive=true`、`peer_count=0`、`cleanup_errors=[]`、`status=passed`。[Runtime log](../../../tests/consumers/react-native-internal/android/build/rf-lab-suite-88ac2e14b14c4c62a473a58be8578859/rf-lab-suite-continuous-runtime.log) 确认单次 active rejection、queued `Cancelled` terminal、active completion/recovery，以及 `flows=9:passedCount=9:failureCount=0`。这成为该阶段 Android normal9 的主 pass；当时 iOS 新 gate 尚未通过。该阶段软件验证另列如下，不与 RF 结果合并为统一十项通过；后续 iOS normal9 与 Android/iOS continuous10 pass 在新报告分别记账。

## 本阶段软件验证与候选边界

主执行线已在本阶段 CLI 修复后、continuous10 开发前完成下列软件验证；本次文档编辑未重新执行。Zephyr、nrftest 与 Apple 行保留各自分阶段证据，计数不相加；软件通过不等于 RF same-candidate 全门禁或统一 10/10：

| 范围 | 已报告结果 |
|---|---|
| BleHub（最终） | `pixi run just peripheral nrf52840-test`、`pixi run just fmt`、`pixi run just lint` 均 passed；Peripheral 不填写未提供的总计数 |
| RN（本阶段） | Jest 83 tests passed、typecheck passed；本阶段验证时 UI 未再改，后续 continuous10 candidate 另见新报告 |
| RN host tooling / Python（最终） | 最终 CLI 修复后 `tools/react_native` 全量 217 tests、0 failures；替代此前 full 215 / focused 84 的阶段性状态 |
| 文档与 diff（最终） | `git diff --check` passed；Mermaid 24 diagrams / 127 files；docs UTF-8、fences、whitespace、conflict checks 均 passed |
| Zephyr | 初轮 3 个 read-hold 配置的 sanitizer 测试通过 |
| nrftest | full tests 666 passed、1 skipped、4 subtests passed；lint、A contract check 通过 |
| Apple retention 修复 | deterministic 78 项通过（Apple agent 执行）；后续独立 watchdog RF 另行记录 |

## 后续进展入口

本阶段之后 iOS 新版 normal9 已通过；continuous10 已实现，同一 optional-localName 修复后的 RN/host 测试代码在 Android 当前 `1e7f…` 与 iOS 当前 `2a89…` 各自真实连续 10/10、host 校验通过、App kept-open。iOS 同一 session/nonce、PID/boot、七组 native/JS ACK 及同一 fixture BTP/lease/live receipt 下两个 physical flow 已完整通过；Android 修复后重跑的两个 physical flow 均 passed、cleanup 无 error，post-suite ADB 确认同一 PID，single-image 的 2 个 UniFFI / 2 个 Nitro slices 检查 passed。期间没有 RN/host 实现修改、无 flash；不是分次证据拼接，不扩展为全门禁通过。Android `92d8…` 保留为修复前历史候选，修复后 Android 重跑不再列为未完成项。

后续动作与 candidate 证据以 [continuous10 RF 报告](2026-10-14-rn-continuous10-rf.md) 和 [研究文档当前状态](../../research/react-native/react-native-rf-lab-test-architecture.md) 为准；transitions、Android pending-operation reload、完整统一 provenance、clean locked release 与 ABI freeze 仍未完成。固件安装与历史失败留在本报告，不把分次或旧 candidate 结果合并为新 gate pass。
