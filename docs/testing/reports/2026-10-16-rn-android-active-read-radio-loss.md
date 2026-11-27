# Android RN：活动 read 期间真实 radio loss 与 fresh-Probe 恢复

报告日期：2026-10-16；执行时间使用 UTC。本文核对已生成的 bundle、ledger、physical evidence、radio journal 与现有验证日志，不代表文档更新时重新执行 RF。

## 首次功能 pass：结论与候选身份

OnePlus 11 → live-attested multifunction DK-A 的显式 `active-loss` gate **passed**：活动 ATT read 被 hold、另一 read 已排队后由 host 关闭手机 radio，旧连接结算并 teardown；ON 后以 **fresh Probe/Hub** 完成真实 GATT I/O。Host/physical/state journal/bundle 均通过，离线 integrity verification 已通过；这不是 same-Hub 恢复，也不是 startup-off 或 full-suite 的替代结果。

| 项目 | 已核对值 |
|---|---|
| Passing attempt | `rf-adapter-transition-9f5ae4d224f14337b5ddc33edec96a31` |
| Bundle UUID | `bbc0277d-fec4-4b29-991b-19b619a38679` |
| Runtime/fixture/control session | `ae78983a-fce1-40bf-87f8-aef04af40608` |
| UTC 起止 / 用时 | `01:26:03.472146–01:26:16.795356`，约 `13.323 s` |
| 手机 / App PID | OnePlus 11 / `3ac5cc09` / `16338` |
| Native boot / JS nonce | `101365f0-b500-404e-b46b-7bda839d0a9e` / `mvaadvic-mmexj482jql-mpv3lcp4xfe` |
| 冻结 APK SHA-256 / 大小 | `2fd59efb6b61778433ef96095292ab55a0eb39ad264d8028b6a91e5b38bd2636` / `38,133,550` bytes |
| Fixture resource / PID=PGID | `jlink:1050252028` / `45455` |
| Profile / image | `blehub-nrf52840-multifunction-v1-a` / `nrf-multifunction-rf-v1-a` |
| Installed image SHA-256 | `dd7dea99a325db32fa1fd66648d0a0814c8c7c442a77cf08998f2b91a1cc5b7a` |
| Root service | `fd500000-0000-4000-8000-000000000000` |
| Git provenance | `75edda34b20b043ef282d7d94614c9a1169c6179`，`dirty=true`，无 source-to-build attestation |

Bundle UUID 不等于 fixture/runtime session。安装输入由本轮冻结 APK 绑定；APK fingerprint 不是设备端 installed binary byte 回读或签名信任链。原件位于 [passing attempt][attempt]，由 [bundle][bundle] 的 inventory/hashrefs 绑定，不能把此结果继承给后续 source tree 或不同 APK。

## 故障与恢复的实际证据

- **授权故障前**：同一 live fixture receipt/lease 下完成真实 scan/connect/discovery；JS route 为 connection `32005`、GATT session `32007`、characteristic `23`。Provider 的 ARM、WAITING、fresh OFF authorization 绑定同一 hold **sequence `43` / ATT handle `41`**，两条 read `32008` / `32009` 已 accepted。Characteristic instance ID 不是 ATT handle。
- **结算 policy**：ledger 的 `active_loss.policy=disconnect-first`。按 ledger 顺序（零基 event index `12 / 13 / 14`），`32008` 和 `32009` 各有且仅有一次 `commandCompleted(error.category=connection)`，保留 `platform_android / BluetoothGatt` details；随后 `connectionStateChanged(Disconnected, connectionId=32005, commandId=null)`，无 error。这里的 `disconnect-first` 是 backend 结算分类，不表示 passive event 先于两个 terminal。
- **close 前 retirement**：`activeScans/connections/gattAttributes/subscriptions/pendingIo/watchdogs/callbackRoutes/handoffPending` 均为 `0`。`hubRoots/listeners/ingressRoots/adapterDelegates/backendLeases/handoffRoots/lifecycleOwners/peerCacheEntries` 仍各为 `1`，`workerThreads=2`，不能把此快照写成整 Probe 已关闭或所有资源归零。另有旧 route 的一秒 silence marker。
- **close 与 fresh recovery**：ledger 记录一个 App runtime、两个 Probe/Hub lifecycle；旧 Probe 与恢复 Probe 的 `closed_resources.off/on` **各 17 项全零**。ON 后 fresh Probe 使用 connection `9007` / GATT session `9009`：初始 read `00`；write-with-response `10` 后另行 readback `10`；write-without-response `AB` 的 terminal 仍只是 local submission，随后另行 readback `AB`。不是在原 Hub 上恢复，也不把无响应写 terminal 当远端 ACK。
- **物理与收尾**：physical evidence 记录两对 GAP connected/disconnected，分别覆盖旧 route 和 fresh recovery；hold 最终 `RELEASED`。Cleanup 为 hold inactive、peer count `0`、advertising stopped。Bundle `completed=true/status=passed/primary_error=null`，cleanup/evidence errors 均为空。`after-logcat-detach` process receipt 仍确认 PID `16338` 与原 executable，支持采样时 App kept-open，不保证无限存活。

逻辑资源计数、合作式 host/provider 记录不证明 OS heap/framework 内部对象全销毁，也不是独立空口抓包或实际晚 native callback 的完整 replay。

## OFF 预算：真实 deadline，不是原子同刻断言

[`adapter_transition_capture.py`](../../../tools/react_native/adapter_transition_capture.py) 先取得 request-bound fresh WAITING revalidation ACK，核对 owner/fixture 并拒绝注入前已经到达的 terminal，再调用 `radio_off(timeout=4.0)`；[`state_control.py`](../../../tools/android/state_control.py) 把递减 remaining deadline 传到实际 ADB subprocess，并拒绝超时后返回的状态。不是仅在动作结束后测量耗时；production native watchdog 保持原 10s，watchdog-first 不可判为本场通过。

本场 capture 的 OFF 调用区间为 host monotonic `1351323.056319375–1351324.491288041`（约 `1.435 s`）；journal OFF action/readback 约 `1.429 s`。Provider 在 `1351323.047480708` 重核 WAITING 并授权 OFF。**这说明有序、有界的跨进程握手，不说明 WAITING 检查与 radio OFF 在原子同一时刻发生。** Journal 四个 action 为 initial `ON` / off `OFF` / on `ON` / final `ON`，receipt 数 `1 / 9 / 5 / 1`；额外 receipt 是状态轮询，不是重复 OFF/ON mutation。

## 首次失败与有界 GATT 审计修复

首个 [failed attempt][failed] `80c79088783a4f869ffc849137ee4cb1` 保留 `status=failed/completed=false`，primary error 是 `FixtureSessionError: malformed session JSON: Unterminated string…`。Aggregate GATT JSON 在 logcat 4096 字节边界被截断，不能用后续成功回写首轮。

修复 [`rf_adapter_transition.tsx`](../../../tests/consumers/react-native-internal/src/rf_adapter_transition.tsx) 的 audit-only 输出，显式 schema **`active-gatt-canonical-v1`**：本场 runtime database 的总计为 **4 services / 22 characteristics / 3 descriptors**；compact audit 保留完整 canonical subtree **1 / 5 / 2**（instance IDs、属性与 descriptors），另用 `gattSummary` 明示全库总数，省略其他 services 的详细列表。它不修改原 runtime event，也不是完整数据库 replay。

含 `BLEHUB_NITRO_RUNTIME` 前缀的 active event 预算为 **3500 UTF-8 bytes**，超限 fail closed，不输出截断 JSON。本场 compact GATT record 实际为 **1440 bytes**；provider/ledger 校验 explicit schema、canonical topology 与全库 counts。修复后重新构建并安装上述 APK，才得到本场新 passing attempt。

## 入口与首次功能 pass 时的软件记录

既有 recipe 末尾可选 mode；省略仍为 `startup-off`，本场显式选择 `active-loss`：

```text
pixi run just react-native host-android-rf-adapter-transition <device> <fixture_contract> <fixture_profile> active-loss
```

该 recipe 是复现入口，不把 recipe 中每一步都补记为本次文档工作执行过。首次功能 pass 时，主执行线的 `/tmp/blehub-rf-hierarchy-active-verify.log` 记录该 bundle 的 `integrity verified (not RF reauthorization)`；当时 Jest 日志记录 **12 suites / 301 tests passed，33.337 s**，Python 联合检查尚待更新。此为当时快照：该 Jest 日志后来被最终执行覆盖，当前 **301/12 suites、34.133 s** 与 **combined 734** 见下节最终报告，不将两轮数量相加。本次文档工作未代跑 build/RF/Jest/Python gates。

## 最终 same-candidate 三门禁

上述 `9f5ae4…` / APK `2fd59e…` 是修复后的首次功能 pass，不是最终同 APK 矩阵。后续冻结 APK **`3cff6dcc8da044b5e76eef12032ea86a44cb9798fc3d3d0ceed605f618769676`**（`38,133,774` bytes）重新执行 active-loss、startup-off、full-suite，三者均 passed 且 offline integrity verified；完整 summary、三 manifest 和软件检查见 [RF Lab workbench 最终报告](2026-10-16-rn-rf-lab-workbench.md)。

最终 [active-loss manifest](../../../tests/consumers/react-native-internal/android/build/rf-adapter-transition-33c1dcddd0a8478b95d22054ded373f6/evidence-bundle.json) 的 bundle 为 `7f5a86a8-0836-4985-8620-c12f69863703`，session `40cdcd39-30c7-438f-9881-2cad0d0c4a22`，PID `19035`；UTC `01:34:39.915198–01:34:52.879885`。仍为 `disconnect-first`，两条 read 各一次 `connection` terminal 后才有 passive disconnect，两个 closed snapshots 各 17 项全零。Manifest `completed=true/primary_error=null`，cleanup/evidence errors 为空。该段是**新 attempt 的复核**，不覆盖前文首次 pass 的 PID、session、hold sequence 或耗时。

三门禁各自独立 App/session，不能说三者共用一个 runtime。真实设备 UI 导航/截图检查仍待主执行线补充，不因最终 RF pass 冒称完成。

## 不外推的边界

- 仅 Android held-read + queued-read loss、旧 route retirement/close 与 fresh-Probe RF recovery；不证明 same-Hub recovery、in-flight writes/CCCD loss、permission revoke/restore 或 iOS transitions。
- 恢复阶段正常 writes/readbacks 不等于 write/CCCD **执行中**注入 radio loss 的覆盖。
- 最终同 APK startup-off/full-suite 由上节与 [最终报告](2026-10-16-rn-rf-lab-workbench.md) 的新 manifest 证明，不继承首次功能 pass；UI 截图仍未计入已完成结果。旧 startup-off 结果见 [独立报告](2026-10-16-rn-android-adapter-transition.md)。
- 不是最终同候选 native/package/RN/Android/iOS 全矩阵、clean locked release 或 ABI freeze；App checks 不替代 host physical/cleanup/bundle verdict。

[attempt]: ../../../tests/consumers/react-native-internal/android/build/rf-adapter-transition-9f5ae4d224f14337b5ddc33edec96a31/
[bundle]: ../../../tests/consumers/react-native-internal/android/build/rf-adapter-transition-9f5ae4d224f14337b5ddc33edec96a31/evidence-bundle.json
[failed]: ../../../tests/consumers/react-native-internal/android/build/rf-adapter-transition-80c79088783a4f869ffc849137ee4cb1/evidence-bundle.json
