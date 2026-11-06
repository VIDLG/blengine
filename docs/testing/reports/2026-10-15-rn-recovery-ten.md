# RN strict recovery-ten：Android 与 iOS 各自真机通过

日期：2026-10-15。

## 结果与边界

- **Android：REAL RF 10/10，host ledger、physical cleanup、evidence bundle 均 passed，离线完整性复核通过，`app=kept-open`。**
- **iOS：解锁后修正 passive Result 断言，再重建签名并完成 REAL RF 10/10，host ledger、physical cleanup、bundle passed，离线复核通过，`app=kept-open`。**
- 本次不刷机，不换 image。DK-A 保持 `nrf-multifunction-rf-v1-a`，live receipt 仍要求当前安装 image 精确匹配 contract。
- 这是同一个 App / JS runtime 内的十个 fresh Probe/Hub 生命周期，不是一个 Hub 连续存活十轮，也不是 continuous10 的十项不同 flow。
- 资源计数证明被观察到的 native logical ownership；不证明 OS heap、系统线程或所有未知对象无泄漏。artifact fingerprint 是 host 安装快照，不是设备端 binary hash 或 source-to-build attestation。

两平台是同一个 `BleHubNitroHost` App 的独立 recovery-ten 场景入口，并非新 App；尚未与 continuous10 合为同一次启动的组合 suite。Android 通过先于本报告后续 Apple passive Result/shared RN 改动，不将旧 APK 重标为该改动后的 same-candidate rerun。此前 continuous10 与 pending reload 的通过记录属于各自候选。新增资源 SPI、Android snapshot barrier 和共享 RN 代码后的最终 same-candidate continuous10/package/native 全门禁尚未全部重跑，不能据本报告宣布测试系统完善或 ABI freeze。

## Android 证据

Attempt 根目录：

```text
tests/consumers/react-native-internal/android/build/rf-recovery-ten-77cd4b638aba4543a48a0ec03f6b63f5/
```

| 身份 | 值 |
|---|---|
| Android | OnePlus 11，`3ac5cc09` |
| DK-A USB / J-Link | `001050252028` / `1050252028` |
| VCOM | `/dev/cu.usbmodem0010502520281` |
| Bundle run UUID | `a5b1639c-db9a-4601-b6e3-a294eda85280` |
| Fixture/native/JS run UUID | `4f50d655-b80f-44a2-8dc3-a6de2688bf58` |
| App PID / native boot | `6338` / `2960b25d-edba-4059-89e7-973439b5b46f` |
| JS nonce | `mv8s51h9-stng9n24a1i-s8yebrishi` |
| APK SHA-256 | `58d30d2f1abb620df9d2e2ebab8dcd845ebb81f403541dd3cdb651d740f635fe` |
| Bundled JS SHA-256 | `408397d85bf2532ac859e942f63f6e2f2284a4d20feb4cb969d9eada7a2506e7` |

机器身份仅记录本次证据，不作为 runner 默认设备。

实测 ledger：一个 App runtime、十个 Hub/Probe 生命周期；100 个命令及 exactly-once terminals、260 个事件、10 个 `EF` Notification、10 次物理被动断连、10 次恢复后的远端 `00` read、50 个资源快照。物理记录有 20 次连接与 20 次断连；十个 closed 快照中全部 17 项为零。最终 cleanup 为 `peer_count=0`、`hold_inactive=true`、`advertising_stop_confirmed=true`。

主要文件：`evidence-bundle.json`、`recovery-ten-ledger.json`、`recovery-ten-runtime.log`、`.raw.log`、`.identity.json`、`.capture.json`、`coordination/fixture-session/fixture-receipt.json`、`coordination/recovery-ten-physical.json`。Host 在成功边界确认原 App 仍存活，结束的是本地日志捕获，不是手机 App。

## 每轮协议

实现：[RN](../../../tests/consumers/react-native-internal/src/rf_recovery_ten.tsx)、[provider](../../../tests/peripherals/nrf52840/recovery_ten.py)、[capture](../../../tools/react_native/recovery_ten_capture.py)、[semantic ledger](../../../tools/react_native/recovery_ten_ledger.py)、[runner](../../../tools/react_native/run_rf_recovery_ten.py)。

1. 创建 fresh Probe/Hub，先注册 listener，再 initialize，读取 baseline roots。
2. 扫描冻结 profile 的 service，停止扫描并观察两秒静默。
3. 连接、发现 GATT、开启 Notification，检查 active positive-control roots。
4. DK 发送 `EF`；收到真实 value 后，host 通过 GAP 注入被动断连，检查旧 route 静默和 passive roots。
5. Provider 确认物理状态并恢复广告，再发布本 run/cycle readiness；host 通过 payload URL 交付，要求 native/JS 同身份 ACK。没有固定 sleep 冒充 fixture readiness。
6. 重新连接、fresh GATT discovery、远端读取 canonical `00`；不通过 host SET_VALUE 修补 read 结果。
7. 逻辑 disconnect、静默、资源取样、真实 native close；17 项全部归零后才允许下一轮。
8. 十轮完成后关联物理与语义证据；cleanup 和 bundle finalization 也成功才报告 passed。

## 本轮发现并修复的问题

### Android 扫描静默限流

失败 attempt `cb2d8f0b04a145a08936bff37e0672e9` 第六次扫描有系统日志 `status=6` / `registration failed because app is scanning too frequently`，随后广告等待超时。不是 fixture 无广告的猜测。[本地 Android Framework 源码](../../platform-api-reference/android/BluetoothLeScanner.java) 明确存在扫描过频时不通知 App 的分支。

测试层现在显式执行 conservative scan-budget policy：首次 scan 前 30s quiet window，后续从已验证 native start/terminal 后计时至少 7s。等待保持取消及 stream-health 检查，记录 policy/wait/dispatch/started 单调时间 marker，host 检查顺序和时间下限；不重试失败 scan，不给 production backend 增加定时器。这是测试策略，不是所有 Android 厂商的频率保证，JS 时间也不是独立 HCI 计时。

### Native delivery 与 snapshot 的竞态

失败 attempt `16f9e058f8a746f88472fca085d7f8a5` 在 baseline 观察到 `handoffPending=1`。已用 native instrumentation 在旧实现确定性复现：listener/JS 可先收到 terminal，而 executing native delivery job 尚未进入 finally 递减。

[Android Probe](../../../integrations/react-native-internal/android/src/main/java/com/margelo/nitro/blehubreactnative/HybridBleHubProbe.kt) 改为 backend owner 取 native roots，再经过 delivery barrier 读取真实 Probe/handoff counts；delivery 队列不反向等待 backend。Promise 在不占 owner 队列、不持 Probe lock 的诊断线程结算，close race 拒绝快照。未伪造零，也未用 sleep 轮询。OnePlus resource accounting instrumentation 8/8 passed，涵盖 listener 重入、Promise listener 等 close、close race、十轮 production/deterministic ownership 清理；不是 RF 替代物。

### 真实广告形状与错误归类

- `8266a7476e0342b491b8e6559480d412`：App/DK 完成十轮，但 ledger 未允许广告合法 `rssi`，最终失败。已允许 optional i16 并覆盖 bool/浮点/字符串/越界拒绝。
- `5c52e4cf84b846fdaee881710b18a61e`：App/DK 与 semantic ledger 完成十轮，但 bundle 将预期被动断连的 `error=connection` 误当错误 marker。现仅对 exact successful passive-disconnect marker 识别 domain category，未知 error/failure 仍 fail closed。
- iOS `3d24fb70cbdd4f6c8ee8c75927572c9d`：收到正确 service、`peripheralId=0`、缺 local name/RSSI 的广告，旧 RN name predicate 却等待超时。RN 允许缺失名称、显式 wrong name 仍拒绝；host 严格遵守冻结 profile 的 `require_local_name`。true 时缺失拒绝，false 时缺失允许；配置必须为 bool。
- Capture 拒绝清理阶段乱序 marker 时，现在保留同批拒绝行和随后真实 failure，标为未验证诊断数据，不能进入已验证 facts 或转成 pass。
- 前序 Fabric mounting crash 的稳定行 UI 修复保留：高频 event/resource console 仍完整输出，但不逐条触发 UI 挂载。

上述失败 attempt 全部保留原结果，未回写为 passed。修复后的新 Android/iOS attempt 分别构成各自通过证据。

## iOS 解锁后重跑

`871ce58373214821a385667470a3a011` 的锁屏 preflight 拒绝保持原结果。随后解锁运行 `407154a6e1e34da99819de49a55ec0c3` 已进入真实 RF，但旧测试只接受无 error 的 `Disconnected`。实际 DK 注入后有精确 GAP disconnected，Apple backend 报告 `commandId=None + Result.err(connection)`，`CBErrorDomain/code=7`，未提供成功 state；这是生产契约的合法错误分支，不应该补造 `Disconnected`。已核对 `platforms/apple/Sources/BleHubApple/AppleBleBackend.swift` 的被动断连映射与 `crates/blehub/src/contract/events.rs` 的互斥 Result 定义，修正 RN/parser/ledger 接受合法无错 state 或 connection error，marker 按实际分支生成；其他 error 类别、error+state 混合、错路由/command 与 marker 不一致仍拒绝，不改 native/backend。

修复后重新签名构建并独立执行的新 attempt：

```text
tests/consumers/react-native-internal/ios/build/rf-recovery-ten-0d2c06cd477b45d498f97029398cbd81/
```

- iPhone：`00008110-000C61160C39401E`。
- Bundle UUID：`476969a8-a898-4fc5-9c22-9e83d30130a5`。
- Fixture/native/JS run UUID：`390a4392-ce69-4d61-b677-f88a0541cb10`。
- PID/native boot：`47167` / `479bd3fa-7318-40e2-9e54-ac1588ff8ae0`。
- JS nonce：`mv8sneaf-0q6x330pvedj-exf4xq99t9m`。
- App tree SHA-256：`17ec7542b55bf6c5e1cd2e0956a8f6239c2c9e7ac1abeecc5b6632dc96488efd`。

实测：一个 App runtime、十个 fresh Hub/Probe 生命周期、100 commands/terminals、250 events、10 notifications、10 passive disconnects、10 remote reads、50 resource snapshots，十个 closed 快照全部 17 项为零。物理 cleanup 为无 peer、hold inactive、advertising stop confirmed，runner 确认 App kept-open，bundle 离线复核通过。事件数量不要求跨平台相同；命令/domain/物理关联严格按各自实际契约验证。

## 合并到同一 RF Lab 页面的下一步

后续已实现并由 Android 实测通过，详见 [full-suite 增量报告](2026-10-15-rn-full-suite.md)；下述是本独立场景完成时的设计记录，不表示组合入口仍未实现。

目标是一次 App launch/mount/JS boot，固定两组 UI 行：基础十项（含 cancellation/watchdog）→ physical-clean handoff → strict 十轮恢复 → 一个总结果。当前仅完成这两个独立场景，各自通过不能冒充组合 pass。

组合实现必须复用同一 fixture lease/BTP/session，watchdog 后先核对 owned hold、GAP 无 peer、停止广告，再在原 handles 复原 canonical values 并读回。恢复 cycles 内仍不得 SET_VALUE 修补 RW read。Native/JS 阶段路由需拒绝旧信号，使用同一 boot/nonce；host 复用一个持续捕获 owner，按阶段派发给现有 checker，保留各场景物理证据与总索引。不能通过串行调用两个会重启 App 的 runner 实现“合并”。第一段失败或 close/handoff 失败不得进入 recovery；第二段失败保留第一段结果，但总结果失败。

## 正式入口与检查

[Just recipes](../../../just/react-native/mod.just) 已新增 Android 并将旧 iOS recovery-ten 入口升级为严格 runner；iOS 现在额外要求 contract/profile，不再调用历史 delay-based gate：

```text
pixi run just react-native host-android-rf-recovery-ten <android-serial> <fixture-contract> <fixture-profile>
pixi run just react-native host-ios-device-rf-recovery-ten <udid> <team> <fixture-contract> <fixture-profile>
pixi run just react-native host-rf-evidence-check <attempt>/evidence-bundle.json
```

VCOM 通过 ignored local config 或 `BLEHUB_NRF52840_PORT` 显式选择。普通入口不隐式 provision。

本轮已运行：Android Release / signed iOS physical Debug build；初轮 RN full Jest 187 tests，Apple passive Result 修复后 focused Jest 66 tests、typecheck；最新 React Native host Python discovery 364 tests；nRF provider Python discovery 259 tests；Android native instrumentation 8 tests；Android/iOS bundle offline integrity check。不同分组不相加冒充 RF 数量。`pixi run just fmt`、`pixi run just lint`（含 workspace strict Clippy）、`pixi run just docs-check-mermaid`、两端新 recipe dry-run、`git diff --check` 及 authored Markdown UTF-8/围栏/空白/冲突标记检查均通过。
