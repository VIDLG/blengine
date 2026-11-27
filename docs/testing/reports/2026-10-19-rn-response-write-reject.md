# RN response-write REJECT：Android / iPhone 独立真机通过

日期：2026-10-19。范围：现有 Android/iPhone RN workbench 中独立、opt-in 的 `write-reject`，不是新 App，不加入 `full-suite`。本报告不宣告 Phase exit、ABI freeze、CCC 专项或双平台同候选 RF 全通过。

## 结论

- Android 真机 response-write REJECT **通过**：精确 HELD → REJECT，未执行 target callback/commit，独立 local 与 Central readback 仍为 `00`；同一 Probe/connection/GATT/characteristic 随后正常 write/read `22`；firmware 六类 refs、native 17 项、known peers 全零。
- iPhone 真机 response-write REJECT **通过**，精确映射为 `platform_apple / CBATTErrorDomain / code="14"`；同连接正常恢复及双层零资源成立。iOS device build、single-image 与 simulator 原生路由/生命周期测试另有通过证据，不与 RF 混算。
- 最新扫描规则修正后，iPhone 与 Android 各自使用独立 frozen artifact / session 再次完成专项。此前锁屏与扫描超时失败保留，不覆盖或拼接历史 pass。
- 未刷写、reset 或 unsecure；使用已安装 DK-B v2，DK-A 未操作。`IOS-RADIO-GATE` 继续 deferred；无密码采集或授权对话框重试。

## 实现与职责

沿用 [RN 页面架构](../../research/react-native/react-native-rf-lab-test-architecture.md) 与 [host tooling 所有权](../../research/react-native/react-native-host-tooling-architecture.md)：

- App：[`rf_write_reject.tsx`](../../../tests/consumers/react-native-internal/src/rf_write_reject.tsx)，唯一 workbench `Tests → Current run → Evidence`；host-only 场景，不在手机内操控 BTP。
- CLI/runner：[`run_rf_write_reject`](../../../tools/react_native/run_rf_write_reject.py) → 既有 `rf_scenario_runner`；冻结 artifact、租约内 installed-image receipt、精确 owner 收尾。
- Provider：[`write_reject.py`](../../../tests/peripherals/nrf52840/write_reject.py) 复用 typed `ArmRequest(Operation.RESPONSE_WRITE)` 与唯一 leased BTP session，不复制 codec、不新增 firmware 策略。
- Capture/ledger：[`write_reject_capture`](../../../tools/react_native/write_reject_capture.py)、[`write_reject_ledger`](../../../tools/react_native/write_reject_ledger.py)；native ACK 先于 Linking/JS ACK，原始日志、安装身份、控制信号、物理事实与 native 结果分别绑定。
- [`rf_evidence_bundle`](../../../tools/react_native/rf_evidence_bundle.py) 显式支持 `write-reject` 的 empty/partial/passed index、固定引用及 ledger 重放，绝不落入 continuous10 fallback。失败的截断 JSON 留存解析诊断，旧 failed/null-index attempt 仅在能复现其原错误时只读验证，不升级 verdict。

Control discriminator 为 `flow=10, mode=rf-write-reject`，不是 full-suite 第十一项。四阶段为：`arm/armed` → `write-dispatched/held` → `reject/rejected` → `recovery-done/close-allowed`。App 的结束顺序为 disconnect/terminal → Probe close → 17 项资源零 → close step → runtime close → passed。`Disconnecting` 可缺省、至多一次且不得晚于 `Disconnected`；最终精确 `Disconnected` 与成功 terminal 必须存在。

## Android 首次通过的真机证据

设备：`3ac5cc09`（PHB110）。Peripheral：DK-B，USB serial `001050251479`，BTP `/dev/cu.usbmodem0010502514791`。固件来源和安装历史见 [v2 DK-B 报告](2026-10-18-v2-dk-b-mobile-full-suite.md)；本次另有 lease 内即时 exact installed-image receipt。

| 身份 | 值 |
|---|---|
| image | `nrf-multifunction-rf-v2-b` |
| installed normalized image digest | `2066c7545e8ce2533288adbe980ce1c36ebc18dc9dc425f97c7bab868baa4799` |
| Release APK SHA-256 | `ee545d1ed347e06e89980a2f3b64f53526de2377bab47c6b4372042090231ac4` |
| bundle run ID | `90890725-7ab4-4468-ab36-b5fab47250e9` |
| fixture/App control session | `b24c80c9-a8ee-41b6-a70e-2be9f0530ffc` |
| service | `fd700000-0000-4000-8000-000000000000` |
| native connection / GATT / characteristic | `30004 / 30007 / 22` |
| firmware ticket sequence / actual ATT handle | `16 / 39` |
| runtime SHA-256 | `a5733370b8f06dba1944d420464d3264d78d30e76dfae494835d5d0c8f3e38a7` |

Bundle：`tests/consumers/react-native-internal/android/build/rf-write-reject-df321f5548dd4bee83470f6e24e58233/evidence-bundle.json`。

物理与 native 事实：

1. 初值独立回读 `00`，ARM 后仅在 matching HELD 才允许排入同 route 的 read。
2. HELD receipt 为 `REQUEST_CAPTURED`，observed `11`，before/after `00`，callback/commit 尚未发生。
3. 单次 REJECT 后 `RETIRED / INJECTED_REJECT / REARMABLE`，`RESPONSE_QUEUED / TX_COMPLETED / ANSWER_ORDERED`；无 callback/commit flag，`normal_att_result=0`、`callback_result=0`。它们不是 Central 注入 ATT error 的 `14`。
4. command `30010` 只有一次失败 terminal，`category=gatt`，`platform_android / BluetoothGatt / code="14"`；无该命令的成功 `characteristicWritten`。
5. Central queued read `30011` 与独立 local ATT readback 均为 `00`；随后 write `30012` / read `30013` 正常得到 `22`，local readback 也为 `22`。
6. 原 ticket 的 retained receipt 在正常恢复和 disconnect/close 后保持不变，six refs 全零；native 17 项全零，精确 peer attach/detach，最终 peer_count=0、GET_LIMITS idle。
7. host finish、physical、ledger 与 bundle 均通过，cleanup/evidence errors 为空。成功 App 页面保持打开。

实际运行命令（artifact 已先构建并检查 single-image）：

```sh
BLEHUB_NRF52840_PORT=/dev/cu.usbmodem0010502514791 pixi run python -m tools.react_native.run_rf_write_reject --platform android --device 3ac5cc09 --artifact tests/consumers/react-native-internal/android/app/build/outputs/apk/release/app-release.apk --fixture-contract tests/peripherals/nrf52840/firmware/contracts/multifunction-rf-v2-b.json --fixture-profile tests/peripherals/nrf52840/firmware/profiles/multifunction-rf-v2-b.json
```

## 最新双平台独立 RF 复核

前次成功后新增的唯一场景行为修正：与既有 RecoveryTen 一致，扫描 observation 的 local name 缺省可接受，存在则必须精确匹配；目标 service UUID 仍必需。名称不是 installed-image attestation，physical lease/live receipt、唯一 peer、目标 GATT 和精确 ticket 的验证均未放宽。修复依据为实际 iPhone observation 与本地 [Apple mapper](../../../platforms/apple/Sources/BleHubApple/ScanController.swift)；未改 production backend 或固件。

两平台分别重建 App、通过 single-image 后串行运行同一专项：

| 项目 | iPhone | Android |
|---|---|---|
| attempt（各平台 `build/` 下） | `rf-write-reject-84c7d8b2b87d407d80e6ef5d99d61932` | `rf-write-reject-b3068c178ef1486f9d6804577d65c467` |
| bundle run ID | `a610cfbe-7340-4c17-9bf0-73be553edbe2` | `86c0fc57-a67b-4c9f-a9f4-ab197d22067b` |
| fixture/App session | `db83fe1c-4adc-440a-81de-3e24e15890dc` | `0adb590b-3ff9-4eba-b631-2be4e47d4295` |
| artifact fingerprint | app tree `e66787c97614d6ef3dca0fbd88ea592dce42e7e021a2b57735ba0dd4f4ca3306` | APK `475a130676226afd291979694749a45eab4ee85b27e2d3af7a1d1f57d894865f` |
| native error | `platform_apple / CBATTErrorDomain / 14`，`Unlikely error.` | `platform_android / BluetoothGatt / 14` |
| native connection / GATT / characteristic | `30004 / 30007 / 13` | `30004 / 30007 / 22` |
| firmware ticket sequence / ATT handle | `17 / 39` | `18 / 39` |
| unchanged / recovery readback | `00 / 22` | `00 / 22` |
| verdict / cleanup / evidence errors | passed / empty / empty | passed / empty / empty |

两份 physical report 和 ledger 均证明 exact `INJECTED_REJECT`、无 target callback/commit、保留原 ticket、正常同连接恢复、firmware 六类 refs 与 native 17 项全零、known peers=0。完整原始 capture、四阶段双 ACK、安装身份与 live image receipt 在各自 bundle 内。两台手机结果页保持打开；未进行 permission/radio mutation 或固件 provisioning。

实际 iPhone 命令为上面的 runner 参数切换为 `--platform ios --device 00008110-000C61160C39401E --artifact tests/consumers/react-native-internal/ios/build/device-derived/Build/Products/Debug-iphoneos/BleHubNitroHost.app`，同一显式 DK-B port/contract/profile；Android 使用上文完整命令消费新 APK。完整日志为 `ios-rf-attempt-3.log` / `android-rf-attempt-3.log`。离线完整性复核见验证记录；这不是最终所有 gate 的 same-candidate exit。

## 保留的失败与修复

### Android 首次：GATT 日志截断，不是 REJECT 失败

Attempt：`android/build/rf-write-reject-f11820446bb441ec99d9722e8ad5318c`。完整 GATT JSON 超过 logcat 单条载荷，捕获行在约 4052 字符截断，parser 报 `malformed session JSON: Unterminated string`。失败发生在 ARM 前，cleanup 无错误。原始 bytes/failed manifest 保留，离线 integrity 已通过，仍为 failed。

修复：只投影目标服务的完整 GATT 数据，完整事件仍供 App topology validation；过滤前验证总 topology 数量。完整带 prefix 的事件按 UTF-8 字节计数，达到 4000 bytes 就显式失败，不截断错误/路由/值。新增真实规模 4-service/21-characteristic/3-descriptor 与非 ASCII 字节边界回归。重建后以上新 Android attempt 才通过。

### iOS 原生测试初次：test host 不加载 App probe

原生 standalone `xctest` 中 `NSClassFromString("BleHubWriteRejectGateProbe")` 为 nil，原结果 6 passed / 1 failed，四阶段断言未执行成功；另有 Xcode 600s diagnostic-collection timeout。原 `.xcresult`：`ios/build/native-test-derived/Logs/Test/Test-BleHubNitroHost-2026.10.19_08-57-40-+0800.xcresult`。

根因修复：将现有 Foundation-only owner 提取至 `BleHubNitroHost/WriteRejectControlGate.swift`，同一源码加入 App 与 native-test target；测试直接驱动该 owner，不改为启动 App、不放宽断言。修复后 canonical test **7 passed / 0 failed / 0 skipped**，device candidate 重建且 single-image 再通过。新 `.xcresult` 时间为 `09-12-48-+0800`。Pods 的既有 `Podfile.lock` / PrivacyInfo 未被覆盖。

Device build 更早一次前置 typecheck 报 `tsc: command not found`，随后工具已存在、直接 check 及未修改 recipe 的重跑通过；首次原因未确认，日志保留，不归因于签名。

### iPhone 专项：锁屏预检拒绝

设备 `00008110-000C61160C39401E`，team `FWVYQ287W5`。Attempt：`ios/build/rf-write-reject-8daaf3a3137641b3a3435c1b251f5a90`；bundle run ID `a9642064-1f76-4477-87d6-d4439daa49cb`。

`IosDevicePreconditionError: ... is locked; unlock it before starting the RN gate`。在 fixture startup/install/launch 前退出，无 runtime/ledger；failed bundle 已离线验证。后续使用新独立 attempt，而非续写此 failed bundle；该次不提供任何 iPhone RF 证明。最新通过见上节。

### iPhone 解锁后首次：目标广告缺 local name

Attempt：`ios/build/rf-write-reject-4afb0e30d0c84e92976fd3b1bcb832df`。初始化与 scanStarted 成功；收到 `peripheralId="0"`、正确 `fd700000-...` service UUID，但 observation 不含 localName。新场景原先要求名称必填且精确相等，故扫描等待超时，App 记录 `JS wait expired; no native outcome inferred`，native 同时记录 `write-reject-runtime-or-readiness`；host 最终主错为 `write-reject: native/App failure`。发生在连接/ARM 前，physical 仅有 no-peer/cleanup，owned cleanup 无错误，native 17 项全零；failed bundle 离线验证通过。

修复只改变上述 optional local-name admission，沿用既有 RecoveryTen 语义。新增 8 项双平台回归：缺名/正确名成功、错误存在名称/错误 service 拒绝且不 connect/write，包含合法 opaque ID `0`。未通过启用 duplicates、修改 native callback 或增加重试掩盖问题。

## 验证记录与可发现入口

实际执行：

- `pixi run just react-native tools-fixture-test`：契约对齐后 877 passed；随后增加 failed-bundle 回归，最终组合结果见本报告末尾的软件复核。
- `pixi run just react-native tools-test`：failed-bundle 修复后 495 passed。
- `pixi run npm --prefix tests/consumers/react-native-internal run test:write-reject`：GATT 投影修复后 5 suites / 74 tests passed。
- host 全 Jest discovery：投影修复前 15 suites / 408 tests passed；最终结果见软件复核。
- `pixi run just react-native host-check`：通过。
- `pixi run just react-native android-test 3ac5cc09`：17 instrumentation tests 通过（含 3 项 write-reject routing），非 RF。
- Android Release build 与 `check_single_image.py android ...app-release.apk`：通过。
- `pixi run just react-native host-ios-native-lifecycle-test D8098F99-2E1D-4E40-963F-D2F6F4942EEA`：修复后 7 项通过，iOS 27 simulator，不替代 iPhone。
- `pixi run just react-native host-ios-device-build 00008110-000C61160C39401E FWVYQ287W5` 与 `check_single_image.py ios ...BleHubNitroHost.debug.dylib`：最新 App 投影版本通过。
- `pixi run just fmt`、`pixi run just lint`：通过；文档完成后的复核另记于下。
- Android passed、Android first failed、iPhone locked failed 三份 bundle 的 `host-rf-evidence-check`：通过；均保持原 verdict，不重新授权 RF。

新增维护入口：`host-android-rf-write-reject <device> <contract> <profile>` 与 `host-ios-device-rf-write-reject <udid> <team> <contract> <profile>`。两个 recipe 构建现有 App，runner 只消费冻结产物，不 provision。

完整构建/SDK/test 输出保存在 ignored `.work/rn-write-reject-2026-10-19/`，包括 `native-probe-fix/` 与 `gatt-log-projection/`；RF 原始证据保存在各 attempt。命令错误也保留：一次误用不存在的 npm `test` script、一次重定向目录层数错误，之后从 host 目录直接运行现有 Jest executable；不把命令启动失败算测试失败或通过。

### 最终软件复核

GATT 日志投影修复与首轮文档完成后执行：`tools-fixture-test` **880 passed**；完整 host Jest **15 suites / 415 tests passed**；`fmt`、`lint`、`host-check` 均通过；`docs-check-mermaid` 检查 **151 Markdown files / 27 diagrams** 通过。日志为上述 ignored 目录中的 `final-*.log`。这些软件结果不补写 iPhone RF，也不重标原失败。

扫描名称修正及双平台 RF 后再次复核：`test:write-reject` **5 suites / 82 tests passed**，完整 host Jest **15 suites / 423 tests passed**，`host-check` 通过；`tools-fixture-test` **880 passed**。两平台重新构建和 single-image 通过；新 Android/iPhone passed bundles 及 iPhone 缺名称 failed bundle 均离线 integrity 通过。文档完成后的 `fmt`、`lint`、`docs-check-mermaid` 再通过。日志为 `scan-name-*.log` / `dual-pass-*.log`。未重跑 native 17/7 项测试，因为本次只修改 JS 扫描匹配，不修改 native gate。

## 既有 baseline 与剩余缺口

重构后 iPhone full-suite 首次 `rf-full-suite-723a87fc8e034e499c4188df5715db93` 的 RecoveryTen cycle 8 连接后缺 marker、absolute timeout 仍未定位。后续独立 session `rf-full-suite-91f9ddab18e4418aa1b6e98549a0c513` 完整 10 flows / 10 cycles 通过，日志为 `.work/rn-write-reject-2026-10-19/baseline-ios-independent.log`。独立通过不解释或删除首次失败，也不证明最新 write-reject candidate 的 full-suite。

response-write REJECT 已取得上述独立双手机证据；接下来按顺序实施独立 CCC enable REJECT、CCC disable REJECT。最终同候选 native/runtime/ABI/package/RF、10,000 lifecycle、long stress、Android API tiers 与尚未证明的 firmware 内部调度/资源失败仍是独立缺口。
