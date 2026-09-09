# Windows 测试执行指南

本文档是 [测试执行指南](README.md) 的 Windows 平台补充，整理 [`.plan/PLAN.md`](../../.plan/PLAN.md) 的 Windows-first 测试要求。它定义 fixture、native seam、覆盖矩阵、HIL 执行和报告方式，不是第二份架构 Plan，也不改变 Plan 的规范性 acceptance 或阶段退出条件。

Windows-first 的目标是尽早验证 BLE 事件和状态机，不是把公共 API 设计成 Windows API 的翻版。

## 适用阶段与 lane

| 阶段/条件 | 执行内容 | Lane |
|---|---|---|
| Phase 0-1 | 运行平台无关 contract/bridge、`FakeBackend` 和共享 conformance；Windows 不拥有这些语义的私有副本 | `core-primary`；可移植性由 `core-portability` 复核 |
| Phase 2，Android项目外设可用 | private Windows mapping fixtures、按需窄native adapter、scripted conformance、package，以及真实scan/GATT/notification开发smoke | `tests/peripherals/android`、`windows`；开发RF smoke不作为BLESS release HIL通过 |
| Phase 2，USB adapter与managed peripheral环境可用后 | 自动RF smoke、fault injection、资源清理和至少10次scan/connect soak | `windows-hil` |
| Release candidate | 执行并保存 Windows HIL 报告 | `windows-hil`，独占 runner |

`windows-hil`的最终计划环境仍是Windows Central + QEMU Linux Peripheral VM + 2 BLE controllers，使用BLESS managed peripheral。HIL不阻塞普通PR，也不承担全部状态组合；确定性组合应在较低测试层覆盖。

Windows RF runner位于standalone `tests/hil/windows` Cargo crate，只通过Just显式调用。`pixi run just windows::check`会在不访问RF硬件的情况下编译这些runner；普通`pixi run just test::all`不会构建或执行它们。该crate属于项目测试基础设施，不是`tests/consumers/windows` artifact-only packaging consumer，也不是面向用户的example。

### 当前物理外设：项目Android Peripheral

开发期使用Android真机（当前为OnePlus 11，ADB serial `3ac5cc09`）运行[`tests/peripherals/android`](../../tests/peripherals/android/)中的项目Kotlin GATT Server。它直接消费fixture `blehub-android-peripheral-v1`，在同一 GATT 数据库中提供普通 `updates` 和严格 Indication 用的 `indication-updates`，可显式发送 notification/indication并通过Android CLI/ADB控制；构建和操作见[Android开发外设指南](android-peripheral.md)。QEMU/BLESS消费语义对齐但广播service不同的`blehub-qemu-basic-gatt-v1`，因此两者并行运行时也不能被同一个service filter混淆。LightBlue保留为项目fixture不可用时的手工fallback。

2026-08-29已从Windows通过LightBlue和项目recipe实际验证共享fixture的以下基础路径：

- 扫描到service UUID `FFF00000-0000-4000-8000-000000000000`；
- 连接和GATT service/characteristic discovery；
- read、write with response、write without response；
- subscribe/unsubscribe；
- 初始值和characteristic properties。

运行入口：

```sh
pixi run just peripheral android-start
pixi run just ble-scan --service-uuid FFF00000-0000-4000-8000-000000000000 --timeout 20 --require-match
pixi run just test fixture --notification-timeout 60
# 等待 notification_subscription=ready 后，在另一终端执行：
pixi run just peripheral android-notify deadbeef
```

当前Bleak路径直接验证Windows OS/RF/项目Android fixture，不经过`WindowsBackend`或BleHub event ingress。`WindowsBackend`现已实现adapter初始化、scan/stop、logical connect/disconnect、公开GATT discovery、characteristic read/write和notification/indication源码路径，并通过非RF编译、测试、共享conformance与项目Android fixture开发RF smoke。

2026-08-31已使用HONOR 90 GT项目fixture完成真实`WindowsBackend -> BackendEventIngressHandle -> Rust BleEventSink`扫描闭环，覆盖`PoweredOn`初始化、`ScanStarted`、共享service predicate、真实`AdvertisementObserved`和requested `ScanStopped`。完整环境、首次失败及修复后事件序列见[`2026-08-31 Windows backend + Android Peripheral scan smoke`](reports/2026-08-31-windows-android-scan-smoke.md)。同日继续通过真实连接、作为连接证据的私有uncached GATT service probe和主动logical disconnect开发RF smoke，事件与边界见[`2026-08-31 Windows backend + Android Peripheral connect smoke`](reports/2026-08-31-windows-android-connect-smoke.md)；随后公开`DiscoverGattCommand`通过uncached service/characteristic/descriptor flat traversal，并准确映射fixture的四个characteristic、properties与CCCD，见[`2026-08-31 Windows backend + Android Peripheral GATT discovery smoke`](reports/2026-08-31-windows-android-gatt-discovery-smoke.md)；public characteristic read、write with response和write without response继续通过确定payload回读，见[`2026-08-31 Windows backend + Android Peripheral read/write smoke`](reports/2026-08-31-windows-android-read-write-smoke.md)。provider profile隔离后，两个fixture并行广播时的`FFF0...`/`FFE0...` service-filter选择通过；`PlatformDefault` Notification与严格Indication进一步通过enable、确定`DE AD BE EF` value delivery、disable和post-disable stale-route silence，见[`2026-08-31 Windows backend + Android Peripheral notification/indication smoke`](reports/2026-08-31-windows-android-notification-smoke.md)。随后同一candidate通过QEMU/BLESS `FFE0...` provider的scan/connect/public GATT discovery、确定性read/write回读、`PlatformDefault` Notification、严格Indication、disable后stale-route silence和connected RSSI `NotSupported` terminal；UB500 detach产生`command_id=None`的被动disconnect，reattach、guest doctor与fixture readiness恢复也通过，见[`2026-08-31 Windows backend + QEMU/BLESS full smoke`](reports/2026-08-31-windows-qemu-bless-full-smoke.md)。仅停止BLESS service未触发Windows disconnect，因此不作为backend证据。当前通过仍不替代active subscription/in-flight callback race、并发或高频FIFO RF、BlueZ/guest/host reboot、重复fault recovery与soak gate；本轮Android真机未连接ADB，未形成当前candidate的Android复跑证据。旧共享service首次选中QEMU的尝试仍不是notification通过或失败证据。

2026-09-01 RF-tested candidate在拆分私有`initialization`、`scan`与顶层`gatt`模块后通过28项Windows测试、103项workspace测试及完整静态检查，并在已连接Android真机复跑上述功能矩阵。六个项目专用RF runner随后迁入standalone `tests/hil/windows` crate并共享`src/support`，production crate不再承载fixture-specific examples。Android应用级`BluetoothGattServer.cancelConnection()`在connect-only与public discovery保留native GATT objects两种情况下均只产生Android server侧`status=0 state=0`，没有WinRT device-level disconnect；关闭整机Bluetooth radio并等待`STATE_OFF`则立即产生`command_id=None`的被动`Disconnected`，随后radio/fixture恢复通过。前者不是device-loss backend证据，后者是一次真实设备级被动断连证据；见[`2026-09-01 Android RF报告`](reports/2026-09-01-windows-android-current-candidate-smoke.md)。

后续non-RF hardened candidate把connection state machine提取到`connection.rs`，把GATT拆为父协调模块、`gatt/discovery.rs`、`gatt/io.rs`和`gatt/tests.rs`，并统一两阶段cleanup：先在`delivery -> state`内invalidate correlation、detach ownership和设置cleanup gate，再在锁外执行WinRT `Cancel`/`Remove*`/`Close`，随后重新进入delivery边界尝试prepared events并按disconnect优先drain deferred callback。caller-owned GATT/subscription ID不再保留历史tombstone，只检查live collision并使用private generation隔离stale callback；默认日志不输出原始Bluetooth address。notification callback撤销失败由`DisabledPendingRevoke`保留cleanup ownership并允许后续disable仅重试revoke，不把已关闭CCCD的route伪装为active。该candidate通过34项Windows非RF测试、109项workspace测试及完整strict lint；随后使用QEMU/BLESS `FFE0...` provider重跑并通过非破坏性scan/connect/public GATT/read/write、`PlatformDefault` Notification、严格Indication、disable后stale-route silence及connected RSSI `NotSupported`矩阵，见[`2026-09-01 hardened-cleanup candidate QEMU RF报告`](reports/2026-09-01-windows-hardened-cleanup-qemu-rf.md)。本轮没有执行USB detach等被动断连fault、重复恢复或soak。Android物理设备可被ADB识别，但fixture安装在APK传输后等待手机端USB安装审批，因此旧Android报告仍不自动证明该candidate已通过Android RF。

最新candidate现通过39项Windows测试、114项workspace测试、Standard Cargo staged package、metadata-enforced source isolation、offline locked artifact-only Windows consumer和用户级`examples/windows-central`静态gate。Android fixture随后在OnePlus 11（`3ac5cc09`）通过非破坏性scan/connect/GATT/read/write/Notification矩阵；combined orchestration在严格Indication附近中断，因此严格Indication独立重跑并通过，不从中断推断结果。当前QEMU与Android、packaging、示例边界及明确未执行项见[`2026-09-01 current-candidate RF与packaging汇总`](reports/2026-09-01-windows-current-candidate-rf-packaging-summary.md)。随后release HIL使用20-byte payload对Android/QEMU各执行三次200-operation read、write和Notification矩阵，并补跑fixture callback ledger矩阵。最新QEMU uncached read、callback-confirmed write with response、write without response WinRT terminal、write without response Peripheral callback中位数为17.9、16.7、1,263.6、91.8 ops/s，Android为8.3、8.3、1,262.4、844.7 ops/s；每轮write batch均达到200/200 unique fixture callbacks且全部sequence violation计数为零。先前lossless Notification中位数为QEMU 85.1、Android 820.0 ops/s。WinRT terminal只表示local/native progress，仍不重标为remote throughput。QEMU HCI trace观测write阶段30 ms connection interval与54.732 ms ATT Write Request间隔中位数；零pacing BLESS burst曾只收到183/200，正式0.5 ms requested guest pacing及Android callback背压的三轮结果均零missing/duplicate/order violation。完整原始值、原因分析和边界见[`2026-09-01 Android/QEMU吞吐矩阵`](reports/2026-09-01-windows-android-qemu-throughput-matrix.md)。这些不是PHY/controller吞吐。最新candidate的10-cycle scan/connect与完整活动图resource-accounting smoke现已通过；每轮第一次及幂等第二次dispose后，backend逻辑root与全部16类WinRT callback delegate均精确归零，完整活动图process handle在cycle 3后为`225 -> 224`、settled max `225`。实现、辅助process趋势及未覆盖边界见[`2026-09-01 Windows资源记账与QEMU 10-cycle soak`](reports/2026-09-01-windows-resource-accounting-qemu-soak.md)。

2026-09-02同一resource-accounting candidate进一步通过OnePlus 11整机Bluetooth radio disable和QEMU guest-owned UB500 detach的`subscribed`被动断连gate。两个provider均在fault前保留公开GATT与活动Notification subscription，收到`command_id=None`的被动`Disconnected`后，在`dispose`前确认connection/GATT/subscription逻辑root及delegate释放，并在`dispose`后确认全部backend root与16类callback delegate精确归零；Android radio/fixture与QEMU USB/BlueZ/fixture均恢复ready。首轮暴露的OnePlus缺少`bluetooth_manager wait-for-state`及QEMU hotplug到BlueZ枚举延迟均按infrastructure failure修复并完整重跑，不伪装为backend失败或通过。详见[`2026-09-02 active-subscription被动断连与恢复报告`](reports/2026-09-02-windows-passive-disconnect-resource-recovery.md)。

后续收口通过双provider各3次active-subscription破坏性fault/recovery、四条同步到已进入read/write/CCCD/value callback的真实fault race，以及Android/QEMU的strict Notification/Indication各1,000包长soak；所有正式burst均零missing/duplicate/out-of-order、actual mode匹配、disable后无stale route并最终资源归零。刷新QEMU image后，BlueZ restart通过；TCG同进程guest reboot在当时controller下完成guest shutdown、SSH/controller/fixture/doctor及Windows scan恢复。controller随后增加Linux boot ID变化硬断言，但该TCG RF路径未重跑；QEMU 11.1.0/WHPX同进程guest reboot在guest shutdown后因`Unexpected VP exit code 4`进入QMP `paused`，但新WHPX进程完整恢复UB500、BlueZ/BLESS、doctor和Windows scan。该WHPX行为归类为fixture infrastructure限制，支持的WHPX恢复是fresh-process restart。提权脚本、Zadig和USB rollback已完成source/static TOCTOU hardening，但改变USB绑定的UAC路径及真实Windows host reboot未执行。详见[`2026-09-02 Phase 2 fault/soak/reboot/hardening报告`](reports/2026-09-02-windows-phase2-fault-soak-reboot-hardening.md)。随后显式[`Phase 2退出审查`](reports/2026-09-02-windows-phase2-exit-review.md)逐项确认四个规范性退出条件为PASS，Phase 2正式完成；新增TCG boot-ID断言、真实Windows host reboot和改变driver binding的UAC路径仍未执行，不从这些边界推断通过。

运行Windows backend RF smoke前先确认项目Android fixture正在广播：

```sh
pixi run just peripheral::android-start
pixi run just windows::scan-smoke 30
# 或连接smoke：pixi run just windows::connect-smoke 30
# 或GATT discovery smoke：pixi run just windows::gatt-smoke 30
# 或characteristic read/write smoke：pixi run just windows::read-write-smoke 30
# release read/write throughput：
pixi run just windows::gatt-throughput 35 fff00000-0000-4000-8000-000000000000 200 20
# sequenced lossless Notification throughput：
pixi run just windows::notification-throughput 30 fff00000-0000-4000-8000-000000000000 android 200 auto
# 或PlatformDefault notification smoke：
pixi run just windows notification-smoke 30 fff00000-0000-4000-8000-000000000000 default android
# 或严格indication smoke：
pixi run just windows notification-smoke 30 fff00000-0000-4000-8000-000000000000 indication android
# Android fixture GATT-server cancellation（用于区分app-level trigger，不是device-loss gate）：
pixi run just windows passive-disconnect-smoke 30 fff00000-0000-4000-8000-000000000000 android connect-only
# Android整机radio fault（真实device-level passive-disconnect smoke，会自动恢复radio与fixture）：
pixi run just windows passive-disconnect-smoke 60 fff00000-0000-4000-8000-000000000000 android-radio connect-only
# 保留已启用Notification及完整GATT资源的被动断连/释放gate：
pixi run just windows passive-disconnect-smoke 60 fff00000-0000-4000-8000-000000000000 android-radio subscribed
pixi run just peripheral::android-stop
```

QEMU/BLESS完整开发smoke入口使用独立`FFE0...` service identity：

```sh
pixi run just windows scan-smoke 30 ffe00000-0000-4000-8000-000000000000
pixi run just windows connect-smoke 30 ffe00000-0000-4000-8000-000000000000
pixi run just windows gatt-smoke 30 ffe00000-0000-4000-8000-000000000000
pixi run just windows read-write-smoke 30 ffe00000-0000-4000-8000-000000000000
pixi run just windows gatt-throughput 35 ffe00000-0000-4000-8000-000000000000 200 20
pixi run just windows notification-throughput 30 ffe00000-0000-4000-8000-000000000000 qemu 200 auto
pixi run just windows notification-smoke 30 ffe00000-0000-4000-8000-000000000000 default qemu
pixi run just windows notification-smoke 30 ffe00000-0000-4000-8000-000000000000 indication qemu
pixi run just windows passive-disconnect-smoke 45 ffe00000-0000-4000-8000-000000000000 qemu connect-only
pixi run just windows passive-disconnect-smoke 45 ffe00000-0000-4000-8000-000000000000 qemu subscribed
```

QEMU被动断连recipe会detach guest-owned UB500，并在观察到被动事件后执行reattach、guest doctor和fixture restart。`subscribed`锚点会在fault前保留已发现GATT与活动Notification subscription；runner要求被动`Disconnected`后连接/GATT/订阅WinRT delegate和逻辑root在`dispose`前释放，随后要求完整backend资源在`dispose`后归零。它会改变真实硬件/VM状态，运行前必须独占资源并获得授权。

Phase 2 fault、delivery soak和fixture recovery入口：

```sh
# 双严格mode长delivery soak；依次运行Notification和Indication：
pixi run just windows::subscription-soak 60 fff00000-0000-4000-8000-000000000000 android 1000 auto 20 1
pixi run just windows::subscription-soak 60 ffe00000-0000-4000-8000-000000000000 qemu 1000 500 20 1
# 破坏性active-subscription fault/recovery，正式默认3轮，可配置1..10：
pixi run just windows::fault-recovery-soak 60 fff00000-0000-4000-8000-000000000000 android-radio 3
pixi run just windows::fault-recovery-soak 60 ffe00000-0000-4000-8000-000000000000 qemu 3
# callback进入后再施加真实fault：
pixi run just windows::inflight-fault-smoke 60 fff00000-0000-4000-8000-000000000000 android-radio read
pixi run just windows::inflight-fault-smoke 60 ffe00000-0000-4000-8000-000000000000 qemu write
pixi run just windows::inflight-fault-smoke 60 fff00000-0000-4000-8000-000000000000 android-radio cccd
pixi run just windows::inflight-fault-smoke 60 ffe00000-0000-4000-8000-000000000000 qemu notification-value
# QEMU基础设施恢复：
pixi run just peripheral::vm-bluez-restart
pixi run just peripheral::vm-guest-reboot
```

这些入口会改变真实radio/controller/VM状态，必须使用独占runner并在开始前获得fixture授权。WHPX下不支持同进程guest reboot；若QMP报告`paused`，停止失败进程并按普通`vm-start`、USB attach、fixture start与doctor路径启动新WHPX进程。

项目Android fixture现在可通过ADB确定性切换整机Bluetooth radio并等待`STATE_OFF/STATE_ON`，并已配合Windows-only callback gate完成复杂in-flight fault与三轮恢复；它仍不能替代FakeBackend/shared conformance对全部duplicate/late/stale callback和平台错误组合的确定性覆盖。因此它是开发期真实RF managed smoke，不替代FakeBackend/shared conformance、Windows mapping/scripted tests或最终QEMU/BLESS HIL。LightBlue只能证明其实际执行的手工基础互操作，编辑value不等于发送notification。

## Windows backend 测试边界

`WindowsBackend` 直接实现公开 `BleBackend`。不得为了测试再定义逐方法镜像的 `WindowsPlatform` trait、Windows command DTO 或 `WindowsDispatchError`。这种同构接口会复制 accepted/rejected、ID 和 error mapping 规则，并形成可能漂移的第二套 backend contract。

共享行为由 `FakeBackend` 和 backend conformance scenarios 固定。Windows 专属测试位于更低的 native mapping 边界：

```text
BleBackend command
-> WindowsBackend acceptance transaction
-> windows-rs call

WinRT result/callback
-> private plain Windows observation DTO
-> pure mapping function
-> unified BleEvent
-> retained BackendEventIngressHandle.push(event)
```

### Acceptance transaction

- accepted 之前的 dispatcher 或调用失败，直接映射为公开 `CommandDispatchError`，不得 emit 该 command 的 event。
- accepted 之后的 HRESULT、WinRT status 或 callback failure通过`CommandCompleted.error: Some(BleEventError)`表达，不得重新解释为trait method的同步`Err`；被动watcher stop或connection state failure直接在对应result-like state event中携带`BleEventError`，不伪造`CommandKind`。同一command-associated failure还先产生state event时，两处必须使用值相等的`BleEventError`。
- command method 返回前已经同步 emit event 时，返回值必须是 `Ok(())`。
- pending connect上的disconnect按[Plan 8.4](../../.plan/PLAN.md#84-建立连接)可在logical lifecycle与owned resources结束后成功；Windows没有证据证明该disconnect导致原connect被取消时，不得为原connect伪造`Cancelled`，只保留关联晚到结果所需的最小correlation。独立connect failure仍terminal `Failed`；原connect继续受原completion evidence与caller deadline约束，晚到callback保留原connect IDs且不得恢复为当前连接。
- Windows mapping 不引入第二套公共 event、error、ID 或 command lifecycle 形状。

## Private mapping fixtures

Mapping tests 直接构造 crate-private plain Windows observation DTO，而不是 mock 复杂 COM object。Fixture 应围绕原生事实和期望的统一输出组织；输出断言引用Plan的[扫描配置 6.6](../../.plan/PLAN.md#66-扫描配置)、[统一 event payload 6.15](../../.plan/PLAN.md#615-unified-event-payload)、[public event 不变量 6.16](../../.plan/PLAN.md#616-public-event)和[backend 可观察契约 7.1](../../.plan/PLAN.md#71-backend-可观察契约规范性)，不在本页建立第二套矩阵。

每个 fixture 至少记录适用字段：

| Fixture 输入 | 期望断言 |
|---|---|
| operation/callback kind 与 WinRT status | 统一 event variant、domain event 与 terminal attempt 的顺序 |
| HRESULT、status、错误消息 | 映射为`BleEventError { category, message, platform_details }`；passive state event直接携带它，accepted command terminal放入`CommandCompleted.error`，并按`error.category`检查Plan 6.16不变量；同一command的failure state与terminal复用值相等的error |
| `ScanOptions`、WinRT filter positive evidence 与 callback observation | `service_uuids`中`BluetoothUuid.value`为空字符串或其他malformed文本时在accepted前映射`InvalidRequest`且零event，空`service_uuids` vector则单独验证为无service约束；service filtering没有support capability，WinRT native pushdown可选，pushdown evidence与callback补过滤必须共同满足Plan 6.6 predicate |
| caller-owned command/session/connection/GATT/subscription ID | event 原样回显对应 typed ID；晚到 callback 不改挂到当前 session |
| native object identity/key | 同一 native peripheral 复用 `PeripheralId`；不同对象即使 payload 相似也使用不同 ID |
| private native GATT handle/key 与枚举结果 | 映射为session-scoped opaque typed ID；`GattDiscovered.database`嵌套connection/GATT session ID和services，不在variant顶层重复，并保留duplicate UUID instance |
| callback source、到达顺序与重复cleanup request | 同一command的domain/terminal程序顺序和错序callback结果；重复stop与重复disconnect/disable分别遵循Plan 8.3、8.4、8.8的不同outcome，不重复native side effect或state event |
| payload、properties 与 notification mode | 统一 DTO 值、capability 和实际 mode 的准确映射 |

共享 `EventScenario` 不嵌入 HRESULT 或 WinRT status。Windows suite 在共享 scenario 上关联 mapping fixture 和平台能力断言，既复用 contract，又保持平台事实私有。

## 窄 native adapter seam

Production 路径由 `WindowsBackend` 直接调用 `windows-rs`。只有真实调用点无法被确定性驱动时，才增加最小 crate-private adapter 或函数注入。

窄 seam 必须满足：

- 只包住需要替换的 native acquisition、WinRT invocation 或 callback source。
- 形状从已经存在的真实调用点产生，不预先抽象完整平台 API。
- 不镜像整个 `BleBackend`，不定义第二套 command DTO/error，也不拥有独立 command lifecycle。
- 不改变 acceptance transaction、correlation ownership、opaque ID 或 event ingress 路径。
- Scripted Windows integration驱动这个seam或callback fixture，并最终经过统一mapping与production `BackendEventIngressHandle::push(event)`；它不是另一个fake backend。

如果实际调用点可以通过 plain observation fixture 和真实 API integration 充分测试，则不创建 adapter。

## Windows event matrix

下表是 Windows backend、mapping fixture、scripted integration 和 HIL 的联合覆盖清单。除明确需要原生无线环境的项目外，优先放入确定性 mapping/scripted tests。

| 领域 | 必须覆盖的场景 |
|---|---|
| Adapter | `PoweredOn`、`PoweredOff`、`Unavailable`、`Unknown`、scan/connection authorization、恢复、初始化失败 |
| Scan lifecycle | start成功/失败；watcher stopped/aborted；stop后晚到advertisement；错误scan ID；重复stop terminal success但不产生第二个`ScanStopped` |
| Advertisement | Plan 6.6 predicate的pushdown+callback补过滤；重复结果、数据更新；同一native device多次observation复用`PeripheralId`；不同device即使payload相似也使用不同ID |
| Connection | 成功、失败、主动/被动断开、重复callback；timeout后`Connected`；旧connection callback；已移除/unknown connection的重复disconnect同步`InvalidState`且零event；pending connect disconnect无因果证据时完成logical disconnect但不伪造原connect `Cancelled`，独立connect failure仍为`Failed`，晚到结果保留原IDs |
| GATT discovery | 成功、失败、空database、重复UUID、rediscover；`GattDiscovered.database`嵌套完整database且不重复顶层session IDs |
| Read | 成功、平台错误、timeout、错误 command ID |
| Write | with response、without response、平台错误、错序 callback |
| Notification | enable/disable、enabled前到达data、disabled后到达data、disconnect cleanup；已移除/unknown subscription的重复disable同步`InvalidState`且零event |
| Lifecycle/threading | backend 在 command 返回前同步 callback、跨线程 callback、downstream 非重入、重复 close、close 后 callback |
| Downstream dispatch | 慢 consumer、调用方 queue overflow、advertisement coalescing、notification burst policy；downstream sink 只做快速 dispatch，不让这些调用方策略阻塞 backend callback |

执行时应为每行标明覆盖层：

- `mapping`：plain observation DTO 到统一 event/error/ID。
- `scripted`：Windows backend acceptance、native call/callback 装配与 ingress 路径。
- `shared-conformance`：跨平台 accepted/rejected、ID、terminal、sink 与 close 语义。
- `HIL`：只有真实 WinRT adapter、RF、driver 或 peripheral 交互才能证明的行为。

同一场景可以跨层验证，但报告应区分每层证明了什么，避免用 HIL 成功代替 deterministic contract coverage。

## Windows HIL

### 必测能力

真实硬件至少验证：

- adapter 初始化与扫描。
- 真实连接和被动断连。
- GATT discover、read、write、notify。
- 重复 scan/connect 至少 10 次 soak。
- watcher、GATT object 和 event token 释放。

HIL 主要证明 WinRT、蓝牙芯片/驱动、RF 和真实外设组成的闭环。重复、乱序、stale/orphan 和错误组合仍应由 mapping、scripted 和共享 conformance 确定性覆盖。

### 执行与判定

- 普通 PR 不因 Windows HIL 未运行而阻塞；`core-primary` 和 `windows` lane 仍须承担各自的非硬件检查。
- Release candidate 必须运行 Windows HIL 并保存可追踪报告。
- `windows-hil` 使用独占 runner，避免 BLE controller、VM passthrough 或 RF 资源被并行 job 争用。
- Smoke 与 soak 分开记录；soak 报告必须给出计划迭代数、实际完成数、失败迭代和首次失败证据。
- 资源释放检查必须覆盖正常完成、失败、主动断开、被动断开和 close 后晚到 callback 中环境支持的路径。
- 未执行、跳过或环境故障不得记为通过；报告应区分 product failure、test fixture failure 和 infrastructure failure。
- standalone HIL runner默认只输出一行PASS；内部保留最近16条、每条最多512字符的BLE event摘要，失败时才输出该有界轨迹。临时诊断可设置`BLEHUB_HIL_VERBOSE=1`逐条输出完整event，不应把verbose模式用于普通矩阵或soak日志。
- throughput runner必须分别报告WinRT native terminal与Peripheral callback/Notification应用边界，不得把无响应写的本地terminal速率标为远端吞吐。payload矩阵应记录协商MTU、PHY、connection interval、DLE及controller buffer证据；QEMU吞吐runner在计时前请求并验证fixture链路的2M PHY，Android fixture通过平台callback记录实际MTU/PHY。

## 报告要求

每次 release-candidate HIL 至少记录 Plan 要求的环境身份：

- Windows 版本。
- 蓝牙芯片与驱动版本。
- 外设实现与版本。

为使结果可追踪，执行报告还应包含：

| 字段 | 内容 |
|---|---|
| Build identity | commit/revision、构建配置、package/artifact 标识 |
| Runner identity | runner 名称或资产标识；是否独占 |
| Central environment | Windows 版本、蓝牙芯片、驱动版本 |
| Peripheral environment | BLESS managed peripheral 或参考外设、实现版本、固件/fixture revision |
| Topology | controller 分配、QEMU Linux Peripheral VM/USB passthrough 状态；如使用不同 host，明确记录 |
| Test selection | smoke case、soak 场景、计划迭代数、超时与 fixture 参数 |
| Result | pass/fail/skip/infra-error 分项，不只给总状态 |
| Failures | 首次失败迭代、相关 command/session IDs、WinRT status/HRESULT、关键事件顺序 |
| Cleanup | watcher、GATT object、event token 与 runner/VM 恢复结果 |
| Artifacts | 结构化测试结果、日志、telemetry 和必要的诊断附件路径 |

报告字段是执行细节，可以随 runner 和 artifact 系统演进；Windows 版本、蓝牙芯片/驱动、外设实现/版本以及 release-candidate 必须执行并记录的要求来自 Plan，不得在此弱化。
