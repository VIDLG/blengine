# iOS 原生 RF：Swift / host 文件协议

本协议对应 `AppleCoreBluetoothRfScenario.swift` 与 iOS `AppDelegate.swift`。Swift 侧已实现；Python host 编排由另一工作项实现。本文不是硬件通过报告，也不继承历史 IO run `fd83b497f53b4116ad4fca403ef1c89f` 的证据。

## 启动与文件归属

App 参数：

```text
--run-id <fresh-run-id>
--rf-service-uuid fff00000-0000-4000-8000-000000000000
--rf-scenario io|notification|indication|rssi|passive-disconnect|recovery
```

省略 `--rf-scenario` 时仍为 `io`。省略 RF service 和 scenario 参数则只运行 initialization/runtime。RF scenario 参数要求同时给出 service UUID。无目标 identifier 的 `--rf-unfiltered` 仅保留 IO 历史诊断扫描用途，不产生 scenario/overall RF pass。

- Bundle ID：`dev.blehub.ios-probe`。
- AppDelegate 通过 Foundation Documents URL 构造并传入**真实绝对路径**，不是宿主机路径：`Documents/blehub-rf-<run-id>/`。下文称 `R`。
- run ID 必须是新的、非空、非 `missing`，最多 128 UTF-8 字节，仅字母/数字/`-`/`_`。已存在的 run 目录直接失败，绝不清空旧 marker 或历史结果。
- **由 app 创建 `R`**，host 不预建、不覆盖整个目录、不复用上次 run ID。host 使用 `devicectl device copy from/to` 的 app data container，通过同一 run ID 的相对 Documents 路径逐文件读取/写入。不要把 Mac 文件路径作为 app 参数。
- App marker 使用 `signalProbeHost` 原子写入；host ACK 使用 `waitForProbeHost` 检查真实文件。普通 marker 内容为 `ready\n`，由 run 目录隔离；`cycle-ready` / `cycle-passed` 内容为 `<run-id>\n`。host ACK 建议也写 `<run-id>\n`，当前 Swift 只检查文件存在。
- **ACK 必须最后复制**，仅在对应 host 动作及其预期结果得到验证后写入；文件存在即视为动作已完成。不要提前写 ACK，也不要仅凭广播命令退出成功推定 fixture 的结果。
- 每次轮询都检查 `R/result.txt`；发现同 run ID 的失败立即终止流程并保留文件/日志。兼容输出仍写 `Documents/blehub-result.txt`，但旧文件/不同 run ID 不能作本轮证据。
- 不需要 macOS 的 `probe-pid`、`connected`、`fault-triggered` marker；本协议使用下表的 iOS marker。
- iPhone app 必须前台、设备解锁且已授权 Bluetooth。host 负责启动前 Android Peripheral 就绪；不要在 app 发出允许切换的 marker 前停止 fixture。

Fixture 来源：[android-peripheral.json](../../../conformance/scenarios/archive/android/android-peripheral.json)。`FFF2...` 是 read + notify + indicate；**indication-only 为 `FFF5...`，不是 `FFF3...`**（后者只读）。以上特征均使用 profile 的 128-bit UUID，例如 `fff50000-0000-4000-8000-000000000000`。

## IO / RSSI：没有 host 握手

- `io`：保留 scan → connect → 完整 fixture discovery → FFF1 初始 `00` → with-response 写 `10 20 30` / 精确读回 → without-response 本地提交 `AB CD` / 延迟精确读回 → disconnect。初始 fixture 必须 reset 到 profile 值。no-response terminal 仍只表示本地提交，不提升为远端确认。
- `rssi`：scan → connect → discovery → **独立 ReadRssi command 12** → disconnect。要求恰好一个同 command/connection 的 `RssiRead` 先于恰好一个成功 terminal。结果来自 connected RSSI，而非 advertisement RSSI；数值不设 RF 强度阈值。
- 两者均无 trigger/ACK 文件；host 只轮询 `R/result.txt`。完整场景还要通过 append-only ledger 和 dispose 后两秒静默。

## Notification / Indication

单次场景全部 marker 位于 `R/`。Recovery 使用每周期目录，见下一节。

| 顺序 | 方向 / 文件 | 前置条件与 host 动作 / 必须验证的结果 |
|---|---|---|
| 1 | app → `notification-enabled` | enable 域事件与 terminal 恰好一次、顺序及完整 tuple 已验证。Notification：FFF2 `PlatformDefault` 实选 `Notification`。Indication：先对 FFF2 `RequireIndication` 得到唯一 `NotSupported` terminal 且无 state 域事件，再对 FFF5 `RequireIndication` 实选 `Indication`。 |
| 2 | host 动作，无 ACK | **仅触发一次** `DE AD BE EF`。Notification 用 `android-notify`（FFF2），Indication 用 `android-indicate`（FFF5）。必须确认 fixture 返回成功，不能失败后无条件重试形成重复值。 |
| 3 | app → `notification-disabled` | app 已收到恰好一个精确且同 connection/GATT/subscription/characteristic 的 `DE AD BE EF`，并完成 unsubscribe 的 Disabled 域 → 成功 terminal。 |
| 4 | host 动作 | 对同一特征触发 `CA FE BA BE`，**预期 fixture 拒绝**。与 macOS `_trigger_android_notification` 规则一致：必须是 `characteristic has no subscribed central`，不是 ADB 失败、进程失败或其他错误。 |
| 5 | host → `post-disable-triggered` | 仅在第 4 步预期拒绝确认后复制此 ACK。 |
| 6 | app 内部 | 从 disable terminal 起检查没有任何 notification 值；收到 ACK 后再观察 2 秒，随后 disconnect、检查旧连接/GATT 路由，最终 dispose 后再观察 2 秒并重查全部 ledger。 |
| 7 | app → `result.txt` | 按“最终判定”检查，不能以 enabled/disabled 或 runtime marker 判定整场成功。 |

现有 host 动作入口为 `pixi run just peripheral android-notify deadbeef` / `android-indicate deadbeef`，post-disable 使用同一 recipe 的 `cafebabe`。host 必须保留 fixture stdout/stderr 并校验实际结果。严格模式没有 fallback：FFF5 不存在、实选模式为 Notification/未知、FFF2 返回成功或错误类别不是 NotSupported，均失败。

## Passive disconnect：先有活跃订阅与精确值，再断 radio

此场景不是仅连接后断开，也不先 unsubscribe。全部 marker 位于 `R/`。

| 顺序 | 方向 / 文件 | host 动作 / 预期 |
|---|---|---|
| 1 | app → `notification-enabled` | FFF2 `PlatformDefault` 已实选 Notification；host 仅触发一次 `android-notify deadbeef` 并确认成功。 |
| 2 | app → `radio-off-ready` | app 已验证活跃 subscription 下的恰好一个精确载荷；**尚未 disable**。只有此时才允许 host 断 Android Bluetooth radio。 |
| 3 | host → `radio-off-confirmed` | 执行 `android-radio-disable` 并确认 Android `STATE_OFF`，然后复制 ACK。不得用 `BluetoothGattServer.cancelConnection()` 或仅停止 fixture 替代。 |
| 4 | app → `passive-disconnected` | 观察到同 connection、`commandId=nil` 的原生终态：`.ok(.disconnected)` 或 `.err(category: .connection)`。后者是 CoreBluetooth 非预期断连错误路径，不伪称为 `.ok`。 |
| 5 | host 动作 | 执行 `android-radio-enable`，确认 `STATE_ON`，再 `android-resume` 并确认 fixture 已恢复广告/就绪。对 FFF2 触发 `cafebabe`，确认 `characteristic has no subscribed central` 拒绝（旧 subscription 不应被恢复）。 |
| 6 | host → `radio-restored` | 仅在第 5 步全部确认后复制 ACK。app 不 reconnect；继续观察 2 秒并验证自无 command ID 断连边界起无旧 connection/GATT/subscription/value/RSSI 事件，再 dispose 和观察 2 秒。 |
| 7 | app → `result.txt` | 要求完整 passive-disconnect 场景 pass；没有 `notification-disabled` / `post-disable-triggered`。 |

无论哪一步失败，host 在 finally 中尽力恢复 Android radio 与 fixture，保留本 run 失败结果；不得因此补写成功 ACK 或将恢复成功算成 RF 场景通过。

## Recovery：同一个 Hub 的 3 个短周期

只 initialize 一次；不在周期之间 dispose/recreate Hub，不清空 event list。这个场景是三次逻辑连接重建，不宣称三次 destructive radio fault。

对 `N = 1, 2, 3` 顺序处理 `R/cycle-N/`：

1. 等待 app 的 `cycle-ready`（内容为 run ID）。仅当该 marker 存在后，host 才可确认/恢复 Android fixture 的广告就绪。必要时可执行 `android-resume`；不得干扰正在进行的前一周期。
2. host 验证 fixture ready 后复制 `fixture-ready` 到 **本 cycle** 目录。app 开始该周期 scan → stop → connect → discovery。
3. 在本 cycle 目录按 Notification 表执行 `notification-enabled` → host `deadbeef` 成功 → `notification-disabled` → host `cafebabe` 预期拒绝 → `post-disable-triggered`。
4. app 检查 2 秒 post-disable silence，disconnect、检查静默，并验证截至当前周期的**全部历史**。然后写本 cycle 的 `cycle-passed`（内容为 run ID）。
5. 前两周期进入下一目录；第三周期后 app 才 dispose，观察 2 秒并重查历史，写 `R/result.txt`。**三个 `cycle-passed` 不能代替最终 recovery pass**，因为后续晚到事件仍可使整体失败。

ID 规则：`command = (N - 1) * 100 + local`；scan/connection/GATT/subscription ID 均使用 N，各自属于独立 ID 类型。每周期 local 为 2 scan、3 stop、4 connect、5 discover、6 enable、7 disable、8 disconnect。即 cycle 1 使用 2–8，cycle 2 使用 102–108，cycle 3 使用 202–208。初始化 command 1 不重复。backend 生成的 service/characteristic/descriptor IDs 只保证 session 内 opaque 唯一，不要求跨 session 的裸 attribute ID 数字不同；关联检查包含新的 connection/GATT session。

## 超时与最终判定

Swift 各阶段上限：initialization 120 秒、authorization 120 秒、默认 scan 30 秒、connect/discovery 各 30 秒、enable/disable/strict rejection 各 10 秒、RSSI 15 秒。host trigger 后值等待 45 秒；`post-disable-triggered` / `radio-off-confirmed` ACK 等待各 60 秒；radio-off ACK 后 passive event 60 秒；`radio-restored` / 每 cycle 的 `fixture-ready` 等待 90 秒。host 在看到 marker 后应立即操作，不要等到接近截止时间；建议每 0.5–1 秒轮询并设置覆盖上述总预算的外层 timeout（recovery 可用 900 秒）。所有轮询同步检查同 run 的失败结果。

`R/result.txt` 为 UTF-8、原子写入，兼容副本为 `Documents/blehub-result.txt`。成功必须按**整行精确匹配**同时包含：

```text
apple_ios_probe_run_id=<requested-run-id>
apple_ios_rf_scenario=<requested-scenario>
apple_ios_rf_<requested-scenario>=passed
apple_ios_rf_ledger=passed
apple_ios_rf_post_dispose_silence=passed
apple_ios_corebluetooth_rf=passed
```

Recovery 还要全部包含 `apple_ios_rf_recovery_cycle_1=passed`、`..._2=passed`、`..._3=passed`。Indication 还要 `apple_ios_corebluetooth_indication_dual_property_not_supported=passed` 和 `apple_ios_corebluetooth_notification_actual_mode=indication`。RSSI 要有 `apple_ios_corebluetooth_rssi_value=<signed-decimal>`、`apple_ios_corebluetooth_rssi=passed`。Passive 要有 `apple_ios_corebluetooth_passive_disconnect_active_subscription=passed`、`apple_ios_corebluetooth_notification_value=deadbeef`、`apple_ios_corebluetooth_passive_disconnect_silence=passed`。

失败结果保留 run ID、scenario，并输出：

```text
apple_ios_probe_run_id=<requested-run-id>
apple_ios_rf_scenario=<requested-scenario>
apple_ios_rf_<requested-scenario>=failed
apple_ios_corebluetooth_rf=failed
apple_ios_corebluetooth_probe=failed: <descriptive error>
apple_ios_rf_failure_snapshot=<append-only event snapshot, when a Hub was created>
```

非法/重复 run ID 导致不能创建新目录时，只使用兼容结果文件中的同 run 失败，不覆盖已存在 `R/result.txt`。正常阶段失败写入本 run 的 result，不能因为曾初始化成功或出现 `runtime=passed` 而认作 RF 成功；Swift UI 也不再这样判断。

## 非 RF 回归验证

```text
pixi run -e apple-ci python tests/hil/apple/support/test_rf_scenario.py
```

此脚本在临时 Swift package 中编译真实 helper、当前 Apple Swift 源码及测试，消费已有 staged production 包，不重建 native production package，不触发 RF。覆盖默认 IO ledger、严格模式及双属性拒绝、RSSI 关联与顺序、订阅 enable gate/精确值/晚到值、被动断连、三周期历史与 dispose 后事件、run 目录与真实文件 marker。
