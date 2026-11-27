# RN production native watchdog 真机 RF — 2026-10-13

## 结论与证据边界

- **Android 最终稳定 runner（格式化后）完整通过**：attempt `cbf82ed5-bff9-4cd8-a9f8-a251042ebfd8` 的原始 `evidence.json` 为 `status=passed`。真实 RN/Nitro → production BleHub → Android native backend → 手机 RF → 实体 DK 完成 active watchdog timeout、queued abort、独立 host post-release old-route silence、重连/重新 discovery、恢复读 `00`、disconnect/close 与 host physical cleanup。13 个 commands 的 terminal 均 exactly once，cleanup errors 均为空；成功路径保留 App 结果页。新增 `post-release-old-route-silence` 在 RELEASED + physical disconnect 之后、recovery advertise 之前，本轮 `observation_ms=1316.8694591149688`。
- **iOS watchdog 未通过、未进入 RF**：最新 signed build 已完成，但 attempt `c68207f8-8bb6-4e36-8824-de2db7ee2a6b` 在 device preflight 明确 `Locked`。evidence 为 `status=failed`，没有 ARM、advertising、native watchdog terminal 或恢复读 ledger。后续再次只读 ready check 仍 Locked，没有新增 RF 结果。不能用 signed build、live DK receipt、cleanup 成功或 normal iOS pass 代替 watchdog RF。
- 当前 DK-A 保持 normal A；DK-B 已独立安装 `watchdog-v1`（30s Peripheral hold）。run 不刷机。normal A 的 Android/iOS REAL 9/9 是独立历史 gate；normal iOS 解锁后的最终 controller attempt `ea3df60d219e44219b1388550dc8b8b6` 已通过，不再是 normal blocker，见 [normal-attested 报告](2026-10-13-normal-attested-rn-rf-lab.md)。normal B 只有历史 provision/readback，不能将本次 DK-B watchdog RF 归给 normal B。
- 本报告不证明 ABI freeze、BLE 认证、完整统一 run manifest、adapter/permission transitions、Android pending reload、所有 image/platform 的 current-candidate recovery，或 iOS/CoreBluetooth 晚 callback 已观察。

## 原始 evidence 与具名身份

以下路径均为 BleHub-relative 本机 ignored evidence，成功与失败均保留：

```text
.work/react-native/rf-watchdog/cbf82ed5-bff9-4cd8-a9f8-a251042ebfd8/evidence.json
.work/react-native/rf-watchdog/cbf82ed5-bff9-4cd8-a9f8-a251042ebfd8/runtime.log
.work/react-native/rf-watchdog/cbf82ed5-bff9-4cd8-a9f8-a251042ebfd8/android-runtime-identity.json
.work/react-native/rf-watchdog/cbf82ed5-bff9-4cd8-a9f8-a251042ebfd8/android-logcat-snapshots.log
.work/react-native/rf-watchdog/c68207f8-8bb6-4e36-8824-de2db7ee2a6b/evidence.json
```

| 身份 | 本轮 evidence |
|---|---|
| Android Central | OnePlus 11；ADB serial `3ac5cc09`；runtime log PID `11198` |
| iOS Central | iPhone 14 Plus；UDID `00008110-000C61160C39401E`；signed build team `FWVYQ287W5`；Locked preflight |
| DK-B USB probe serial / canonical resource | `001050251479` / `jlink:1050251479` |
| 本轮 DK-B BTP port | `/dev/cu.usbmodem0010502514791` |
| Board | `nrf52840dk/nrf52840` |
| Installed image / version | `nrf-att-hold-watchdog-v1` / `maintained` |
| Profile / local name | `blehub-nrf52840-watchdog-v1` / `BleHubWatchdogDK` |
| Service UUID | `fd300000-0000-4000-8000-000000000000` |
| Installed capabilities | `att-read-hold`, `att-read-hold-timeout-30000`, `diagnostic-btp`, `gatt-reset` |
| Android live receipt | session `8e65e613-429b-4e29-b42c-7e82043acfa9`；captured UTC `2026-10-13T15:59:07.757522+00:00`；fixture PID/PGID `21332`/`21262` |
| iOS live receipt | session `63f63017-7dc8-409c-b02f-07dd4d209e87`；captured UTC `2026-10-13T15:28:40.205896+00:00`；fixture PID/PGID `9334`/`7273` |
| Live proof method | 两轮均 `btp-vendor-live-under-device-lease` |

具体硬件身份只属于本具名报告和 ignored local config/evidence；不作为通用 runner、构建或 README 示例的硬编码默认值。

### 精确 SHA-256

两轮 live receipt 的 image、contract/profile hash 一致；其证明来自当前 BTP session 的 installed readback，而非复用历史 installed JSON。

```text
installed normalized image_sha256:
f7ca814353ddcc6e68327c9ad94843859b389b5be44fdf63e2574bc600c3cbb5

contract_sha256:
db6df08c34ea90eb76415d27355c38422d0091bb66fc2b6b90df6ffbb0e3abbd

profile_sha256:
f9d848cfcc72bf4806898ee6f745c5461b61ab4396d7e47a2541e85bc7b66707

runner_sha256 (最终 Android cbf82ed5 evidence；历史 f068aac8 相同):
7e5652f6f1ac28233a7da90f71da979e76c679b0fb4c1fee9ab98b95890c0644

runner_sha256 (此前 Android 875537cb 和 iOS Locked c68207f8 evidence):
9e1a773ada959e6fdfe2909fda4e4b2add6240031cd7c4319b48acb0d6940c95

Android app-release.apk SHA-256:
4fe437496fbaf1392bb384c296922b0809a189e490005fd306478618d67208da
```

APK 路径为 `tests/consumers/react-native-internal/android/app/build/outputs/apk/release/app-release.apk`；最新通过 attempt 的 `replaced_running_app=true`，recipe 显式传 `--replace-app`，本轮在 live proof 后实际替换了既有已知 RN package。最终 `cbf82ed5` 与历史 `f068aac8` 的 APK 和 runner hash 均相同；更早 `875537cb` 使用不同 runner hash。每轮有独立 receipt、设备连接身份和时间边界，不将它们拼作同一个 run。iOS Locked evidence 没有 `app_artifact` fingerprint 或 native ledger；不能补造 iOS artifact hash。normalized image digest 不是 raw HEX SHA-256，也不是刷写确认参数；本报告没有从 RF evidence 推导 raw HEX hash。

## 实现与实验设计

实现入口：

- [host controller `run_rf_watchdog.py`](../../../tools/react_native/run_rf_watchdog.py)：共享物理租约、sole BTP session、live request/receipt、安装/启动、host boundaries、预算与 owned cleanup。
- [host ledger `watchdog_ledger.py`](../../../tools/react_native/watchdog_ledger.py)：13-command exactly-once、native 错误来源、domain/route/scan/peripheral correlation、old-event isolation、恢复值与 close 检查。
- [RN `src/rf_watchdog.tsx`](../../../tests/consumers/react-native-internal/src/rf_watchdog.tsx)：独立 `rf-watchdog` mode 和 fail-closed event stream，不混入 normal 九项 suite。
- [watchdog declaration](../../../tests/peripherals/nrf52840/firmware/images/watchdog-v1.json)、[contract](../../../tests/conformance/scenarios/fixture-contracts/watchdog-v1.json)、[profile](../../../tests/conformance/scenarios/blehub-nrf-watchdog-v1.json)。

Peripheral `CONFIG_BTTESTER_GATT_READ_HOLD_TIMEOUT_MS=30000`，production native watchdog 保持 **10,000 ms default**，不修改 native default、不注入缩短的测试值。host 从 ARM 起采用 24,000 ms release budget，早于 Peripheral 30s expiry，且反复检查同一 sequence/handle/peer 的 live WAITING。**不用 cancellation readiness probe**：active/queued 的证明来自诊断 callback 的 WAITING 与 native terminal，而不是取消操作能否被接受。

恢复读明确使用**同一 watchdog profile 中普通可读的 `updates`，initial `00`**。诊断 `read-write` 具有一次性 read authorization，每次 read 都需新的 ARM；不能当成普通恢复读。initial `00` 的恢复只证明新连接上的该可读 characteristic，不声称旧诊断 read 成功或旧 ATT response 被 Central 收到。

## Android host boundaries（UTC，按 evidence 顺序）

本轮 target native attribute handle `41`，permissions `67`（`0x43`），hold sequence `8`。旧 Central connection `21007`、GATT session `21009`、characteristic ID `23`；BTP peer `01d03151c5ef5d`。BTP handle 与 public characteristic ID 是不同命名空间，不能混用。

| Boundary | UTC（2026-10-13） | 事实 |
|---|---|---|
| `no-peer` | `15:59:12.838759` | `successful-gatt-reset-server-native-no-connection-guard` 建立 native no-connection baseline |
| `already-powered-compatible` | `15:59:36.023985` | `set powered` 返回 BTP `0x01`，核对 current settings `33307` 的 powered 状态后记录兼容边界，不把错误删除 |
| `configured` | `15:59:38.040881` | target handle `41`、permissions `67` |
| `armed` | `15:59:41.264544` | sequence `8`、handle `41` |
| `advertising` | `15:59:42.269600` | 初始广告 |
| `gap` connected | `15:59:44.994259` | opcode `130`（`0x82`）、上述 peer |
| `waiting` | `15:59:46.004507` | `WAITING`，同 sequence/handle/peer，characteristic `23` |
| `still-waiting` | `15:59:55.564898` | native `21010=timeout`、`21011=operation_aborted` 已证实，仍是同一 WAITING；`native_elapsed_ms=15310.613333946094` |
| `release-requested` | `15:59:56.573919` | 同 sequence `8` / handle `41` 的显式 RELEASE |
| `released` | `15:59:57.581439` | Peripheral `RELEASED`，同 sequence/handle |
| `gap` disconnected | `15:59:57.752763` | opcode `131`（`0x83`）、旧 peer |
| `old-peer-disconnected` | `15:59:57.752782` | host 记账旧 peer 已退出 |
| `post-release-old-route-silence` | `15:59:59.069656` | 两次 snapshot 均强校验 `21010/21011` 各一个 terminal、无 domain；`observation_ms=1316.8694591149688` |
| `advertising` | `16:00:00.078968` | post-release silence 成功后才恢复广告 |
| `gap` connected | `16:00:00.238001` | 同 peer 一次恢复连接 |
| `gap` disconnected | `16:00:01.011865` | 恢复连接关闭 |
| `cleanup-hold-inactive` | `16:00:03.513901` | 状态 `RELEASED`，无 active hold |
| `cleanup-peer-accounting` | `16:00:04.639003` | `known_peers=0`；`tracked-gap-disconnects-after-native-reset` |

`native_elapsed_ms` 是 host 从 ARM 到 still-waiting 的耗时，不是 native watchdog 本身的精确触发延迟。host 所证实的时序是 **WAITING → native terminals 后仍 WAITING → RELEASE → RELEASED → 旧 GAP disconnect → post-release-old-route-silence → advertise → 新连接 → 关闭**；不能用固定 sleep 或 firmware EXPIRED 代替这些边界。

## Android native terminals、隔离与恢复

`runtime.log` 初始 snapshot 为 `PoweredOn + Authorized`。初始/恢复扫描分别为 `21004`/`21014`，均观测 peripheral `1` 广告中含 watchdog service；RSSI 分别 `-52`/`-52`。两次 GATT summary 都为 services `4`、characteristics `22`、descriptors `3`，不将这些系统/fixture aggregate counts 当成单一 profile 计数。

13 个 commands 的实际 terminal：

| Command IDs | terminal |
|---|---|
| `21001`, `21002`, `21003`, `21005`, `21008` | 各一次 `commandCompleted`，无 error |
| `21010` | 一次 `commandCompleted`，`timeout` |
| `21011` | 一次 `commandCompleted`，`operation_aborted` |
| `21012`, `21013`, `21020`, `21022`, `21024`, `21025` | 各一次 `commandCompleted`，无 error |

原始错误保留为：

```text
21010:
category=timeout
message=Android GATT I/O operation 21010 exceeded the native callback watchdog
platformDetails.platform=platform_android
platformDetails.name=AndroidBleBackend

21011:
category=operation_aborted
message=Android GATT connection recovery retired a pending operation
platformDetails.platform=platform_android
platformDetails.name=AndroidBleBackend
```

这不是 JS outer timeout。旧连接另有无 `commandId` 的 `connectionStateChanged` 错误：`category=connection`、`message=Android GATT operation watchdog retired the connection`、platform/name 为 `platform_android`/`BluetoothGatt`；该投影没有 `connectionState` 字段，不把它改写成日志未给出的 `Disconnected` domain。其后有 `rf-watchdog:connection-retired:connectionId=21007` marker，physical disconnect 则由上述 BTP GAP 边界独立证明。

原始 RN threadtime 记录（手机日志时钟，不是上表 UTC；本轮跨越手机本地午夜）：

- `10-13 23:59:44.506` dispatch active `21010` / queued `21011`，旧 route `21007/21009/23`。
- `10-13 23:59:54.541` active native timeout；`10-13 23:59:54.542` queued abort 与旧连接错误。
- `10-13 23:59:54.572` active-timeout、queued-aborted、awaiting-release、connection-retired markers。
- `10-13 23:59:55.583` RN `late-silence:observationMs=1000`；随后 recovery scan `21012`。该 RN marker 仍在 RELEASE 前；host post-release 窗口另见 boundary table，不混为一条 marker。
- 新连接 `21021`、GATT session `21023`；read `21024` 的 characteristic `53` 为普通可读 updates，值 `[0]`、byte length `1`、`valueTruncated=false`，terminal 无 error。
- `10-14 00:00:01.041` `recovery-read:passed:commandId=21024:connectionId=21021:gattSessionId=21023:characteristicId=53:value=00`。
- `21025` 有 `Disconnecting`、`Disconnected` 和无 error terminal；`10-14 00:00:02.137` close passed 与 watchdog passed。

host 检查全部 commands 的 exactly-once、无失败 read 的 domain、无旧 route/重复/unknown/越界事件、相同 peripheral recovery、精确 `00`，并在最终 pass 后再取一次快照复核；不只信任 UI `passed` marker。checker 不强加跨 JS delivery 的 domain-before-terminal 保证。

**关键语义：**Peripheral `RELEASED` 只证明诊断 callback 已释放，不能等同于“Central 晚 callback 已观察”，尤其本轮没有任何 iOS RF，不能声称 CoreBluetooth 晚 callback 已观察。Central isolation 的证据是记录的 RN/native 投影无 old event、旧 command terminals exactly once，以及新 route 的恢复读。该 stream 不是平台 callback tracing。RN 1,000 ms silence marker 仍在 host RELEASE **之前**，但最新 host 已另加独立 post-release 窗口，不再仅凭持续 ledger 推断：在 RELEASED 和旧 peer physical disconnect 后，recovery advertising 仍关闭时取得两次 snapshot，间隔至少 1,000 ms；两次 `release_gate` 均要求 `21010/21011` 各只有一个正确 native terminal、没有任何 domain，并检查没有新 GAP peer。本轮 boundary 为 `post-release-old-route-silence`，commands 为 `["21010", "21011"]`，`observation_ms=1316.8694591149688`。该字段按实现从 isolation start 到边界记录计时，包含 snapshot/检查开销；不要称为 native callback 触发延迟。通过后才 recovery advertise，ledger 继续覆盖恢复、close 与最终复核。新增测试拒绝 post-release old event，失败不得恢复广告。

## Cleanup、keep-app 与 iOS preflight

Android `cleanup_errors=[]`、`physical_cleanup_errors=[]`，physical cleanup 精确为：

```json
{
  "advertising_stopped": true,
  "hold_inactive": true,
  "known_peers_remaining": [],
  "peer_state": "no-known-peers-after-native-reset-baseline",
  "radio_power": "unchanged"
}
```

这是以成功 native reset 的 no-connection guard 为 baseline、结合本轮 tracked GAP disconnect 的 no-known-peers accounting，不是声称 BLE controller 有额外的通用 all-peer enumeration。cleanup 不关闭/改变 native radio power；lease/session 清理和 App close marker 成功后，成功路径保持 RN App 打开显示结果，不把 keep-app 当成未清理 fixture。

iOS 最新 evidence 的错误原文为：

```text
IosDevicePreconditionError: iOS device 00008110-000C61160C39401E is locked; unlock it before starting the RN gate
```

它只到 live proof、no-peer baseline、already-powered-compatible 和 configured（handle `41`、permissions `67`），之后进入 cleanup；无 armed/advertising/waiting/native ledger。其 `cleanup_errors=[]`、`physical_cleanup_errors=[]`，physical cleanup 字段同上，hold inactive 状态为 `RELEASED`，known peers `0`。**这是 preflight 失败后的安全清理，不是 iOS watchdog pass，也不是 iOS keep-app/RF runtime 成功证据。**后续再次只读 ready check 仍为 Locked；这不是新 RF attempt 或成功证据。解锁需人手完成，再独立重跑。

## 本轮修复与保留的 historical attempts

实现修复包括：

1. Android 使用 `am start -W` 同步启动再 pin PID，避免 launch 返回时 App 尚未建立而 `pidof` 失败。
2. compact event projection 保留 `advertisedServices`，使 host ledger 能验证初始/恢复扫描的 provider service，不用本机猜测替代广告证据。
3. [`BtpSession.poll_event`](../../../tests/peripherals/nrf52840/btp_session.py) 消费 queued 或 newly read event：返回新 frame 后从 dispatcher queue 取走它，避免“已返回、又留队列”产生重复 GAP accounting。
4. 移除 cancellation readiness probe；恢复改读同 profile 的普通 updates，而不是需要每次 ARM 的诊断 read-write。
5. 新增 host `post-release-old-route-silence`，独立锚定 RELEASED + physical disconnect 之后、recovery advertise 之前的两次 snapshot（至少 1,000 ms 间隔）。两次均以 `release_gate` 强校验 `21010/21011` exactly-once terminal 且无 domain；[`test_rf_watchdog.py`](../../../tools/react_native/test_rf_watchdog.py) 加入 `post-release-old-event` 拒绝场景并检查不会开始 recovery advertising。

历史 Android attempt `f068aac8-d92c-4939-9b4c-fa0dbbed1d32` 的完整 pass 及 `.work/react-native/rf-watchdog/f068aac8-d92c-4939-9b4c-fa0dbbed1d32/` 原始 evidence/log 均保留：它已包含 post-release boundary，`observation_ms=1321.3156249839813`，runner/APK hash 与最终轮相同，但不是最终 `cbf82ed5` 的 receipt 或时间边界。以下最终结论以格式化后的稳定 runner `cbf82ed5` 为准，不删除或重标任何历史失败。

此前 Android attempt `875537cb-51e5-4932-988a-cd2b85ef4c84` 的 `status=passed` 与原始目录 `.work/react-native/rf-watchdog/875537cb-51e5-4932-988a-cd2b85ef4c84/` 保留为 historical pass：它有 RN pre-release silence 与后续持续 ledger，但没有新增 host post-release boundary，不能用它证明加强后的 gate。它的 `replaced_running_app=false`、旧 runner hash 与本轮分开记录；历史失败也继续保留如下。

以下原始目录均位于 `.work/react-native/rf-watchdog/<attempt>/`，保留 failed status，不删除、覆盖或重标为最终 pass；它们不是本轮通过的替代证据：

| Historical Android attempt | evidence/log 所记录的失败 |
|---|---|
| `0786ee54-2c53-4217-803b-96d2c9790cde` | `GAP set powered failed with BTP status 0x01`；physical/host cleanup 同时记录该错误 |
| `1ed38cd9-18d8-46b0-bfd8-0a6e9444bfdf` | 拒绝接管 pre-existing Android RN process；cleanup errors 空 |
| `6af6ad88-39e1-43f6-9006-6e91ffd98a87` | launch 后 `pidof` exit 1；cleanup 为 `no pinned Android PID; refusing unowned app termination`，不能称 cleanup success |
| `ab4aac30-7861-46b0-ab70-6baefce1e150` | `Android log snapshot truncated or changed during attempt` |
| `da8f9691-1d96-49fd-be1d-4ae4a937d5ba` | RN `active cancellation must never be accepted: accepted`；不是 native watchdog pass |
| `9c610f65-8183-4520-869b-96d36924c1bd` | `duplicate or concurrent GAP peer`；保留为修复事件重复消费前的历史失败 |
| `e5d01b52-f8bc-4a29-bb42-679721c144f1` | RN `unexpected terminal: 21024:gatt`，close 也 failed；原诊断 read-write 不能普通恢复读 |

除表中显式列出的 cleanup 错误外，这些 historical evidence 的两类 cleanup errors 为空；这也不使 failed RF attempt 成为 pass。iOS 最新 Locked attempt 独立保留在前述目录。

## 可复现 recipes 与未完成项

从 BleHub root 运行，显式选择 phone、contract/profile 和本机 port（port 可省略而使用本机配置）：

```text
pixi run just react-native host-android-rf-watchdog <device> tests/conformance/scenarios/fixture-contracts/watchdog-v1.json tests/conformance/scenarios/blehub-nrf-watchdog-v1.json <port>
pixi run just react-native host-ios-device-rf-watchdog <udid> <team> tests/conformance/scenarios/fixture-contracts/watchdog-v1.json tests/conformance/scenarios/blehub-nrf-watchdog-v1.json <port>
```

Android recipe 构建 release APK，显式传 `--replace-app`，只在 live fixture proof 后允许替换已知 RN package；最新通过 attempt 的实际 replacement 为 true。iOS recipe 先 signed build，再在 runner 中做 device preflight。两者都不刷 DK，不发现“第一块板”作为替代，不修改 production native watchdog default。

最终软件验证由主执行线/另一测试 agent 报告，本次文档 agent 未重新执行这些软件或 RF 门禁：

- BleHub `pixi run just peripheral nrf52840-test`、`pixi run just fmt`、`pixi run just lint`、Mermaid 全通过；nRF 测试不补造未提供的计数。
- nrftest lint/test 通过：**542 passed、1 skipped、4 subtests passed**；normal 轮较早的 471/1/4 记录保留在 normal 报告，不冒充最终结果。
- 全 RN Jest **43/43**，**34.9s**，在 **90s limit** 内完成；typecheck 通过。
- 此前 focused **watchdog 26/26、RN UI 30/30、transport + watchdog 31/31** 保留为独立已通过记录，包含 post-release old-event 拒绝场景，不与全 RN 计数相加。

旧全 RN Jest 在 120s 外层限制下超时，原因是纯 mock suite 累计真实 timer 窗口。测试 fixture 改用 fake timers，**不改 production、不改 timeout**；后续 43/43 是独立完成结果，不删除旧超时记录，也不把软件 timer 改动称为 RF 改动。上述结果不证明所有 native/package 门禁、iOS watchdog RF 或 ABI freeze。

仍未完成：

- iOS watchdog 解锁后的独立实际 RF gate；macOS/Android/normal iOS 结果不能代替。
- adapter/permission transitions、Android pending-operation reload。
- 完整统一 production-artifact + per-flow run manifest；本次 evidence 的 runner/APK hash 和 live receipt 是局部 provenance，不等于所有 gates 的统一 manifest。
- attested current-candidate repeated recovery/multi-DK gates、最终同候选 native/RN/package 验证及 ABI freeze。

本次仅更新指定文档，未改变实现、`.plan`、硬件或 commit。通用操作边界见 [fixture README](../../../tests/peripherals/nrf52840/README.md)，架构状态见 [RF Lab 架构与差距评估](../../research/react-native/react-native-rf-lab-test-architecture.md)。
