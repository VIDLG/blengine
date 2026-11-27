# RN 能力面的证据地图：每个 API 承诺现在由谁证明

本文件回答一个问题：**RN 包新增的能力里，哪些已经有真机证据，哪些只有单元测试，哪些完全没有。**
它记录职责与缺口，不改变任何 gate：一个场景在真机上通过仍然只是候选事实（`host_gate=pending`）。

权威来源：`integrations/react-native-bleng/specs/*.nitro.ts`（能力集由 `tools/tests/react_native/test_api_docs.py`
从 spec 派生）。平台事实来源：`platforms/android/library/.../AndroidBleBackend.kt` 与
`platforms/apple/Sources/BleHubApple/AppleBleBackend.swift`（逐条读过，见下表引注）。

## 四档证据

| 档 | 含义 | 位置 |
|---|---|---|
| **J** | jest：只证明 App 把回答呈现对了，包边界是 mock 的 | `tests/consumers/react-native-bleng/src/*.test.*` |
| **S** | managed 场景：真机、真 App、真 peer（nRF52840），一条完整 trace | `tools/react_native/bleng_scan_evidence.py` 的 `SCENES` |
| **L** | native lane：真机但没有 App UI（instrumented/XCUITest 专用测试） | adapter-transition / permission-transition / multi-link / notification overflow / engine-reload |
| **—** | 无 | |

## A. 已有证据的承诺

| 承诺 | J | S | L | 备注 |
|---|---|---|---|---|
| `beginScan` / `waitUntilStarted` / `nextEvent` / `beginStop` | ✅ | ✅ `scan` | | |
| `beginConnect`（可连接态）/ `BlengConnection.awaitEnd` | ✅ | ✅ `*` | | 全部场景都建链 |
| `beginDisconnect` + `end.reason=requested` | ✅ `Connection.test.tsx` | ✅ `disconnect` | | 两端 2026-11-26 真机通过；`linkEnd` 用包自己的 `BlengEndReason` 词 |
| `beginDiscoverGatt` / `services()` / `characteristics()` | ✅ | ✅ | | |
| `beginRead` / `beginWrite`（两种写型）/ `beginReadRssi` | ✅ | ✅ `io` | | |
| `beginSubscription` / `waitUntilEnabled` / `nextEvent` / `beginDisable` / `detach` | ✅ | ✅ `notification`/`indication` | | |
| `BlengGattDescriptor.beginRead` / `beginWrite`（CCCD）+ `descriptorReadTotal` | ✅ `Descriptor.test.tsx` | ✅ `descriptor-io` | | Android 写 `done`；iOS 读 `done`、写为平台**类型化拒绝**（CoreBluetooth 只允许 `setNotifyValue`，直接写会断言崩溃） |
| `beginDeviceQuery('connected')` + `BlengDeviceInfo.key` 与 `peripheral.key()` 对齐 | ✅ `Devices.test.tsx` | ✅ `devices-query` | | 两端 2026-11-26 真机通过 |
| 取消（`cancel` / 排队 / 恢复） | ✅ | ✅ `cancellation` | | |
| `beginQueryMtu` / `beginRequestMtu` / `beginSetConnectionPriority` / `beginGattCacheRefresh` | ✅ | ✅ `link-facts` | | Android 三者 `done`；iOS 后三者 `unsupported`（两端 2026-11-26 真机通过） |
| CCC 拒绝的两面 | ✅ | ✅ `ccc-enable-reject`/`ccc-disable-reject` | | |
| `beginAdapterObservation`（adapter 快照流） | — | — | ✅ adapter-transition | 场景没有 adapter 面 |
| 权限撤销→进程死亡→恢复 | — | — | ✅ permission-transition | |
| 多链路隔离 | — | — | ✅ multi-link | |
| 通知背压 overflow | ✅ | — | ✅ notification overflow | |
| 引擎 reload / 进程内 invalidate | — | — | ✅ engine-reload | |
| JS 派生层（`pendingOperation` / `requireAccepted` / `isBlengFailure` / `BlengOperationError`） | ✅ | n/a | n/a | 不新增语义，只该由 jest 证明 |
| JS 便利层（`failureReason` / `shareLane` / `BlengProvider` / `getBlengCentral` / `closeBlengCentral`） | ✅ | n/a | n/a | 同上 |

`*` 每条连接类场景都覆盖 `beginConnect`；`connect-failure` 另外覆盖"连不上时如实报告"。

## B. 新能力面：平台事实与当前证据

平台事实是"**要么照做，要么类型化拒绝**"——这是本项目一贯的诚实口径，所以一条场景可以两端通用地证明
"平台答案被如实上报"，而不需要假装两端能力相同。

| 能力 | Android | iOS | J | S | L |
|---|---|---|---|---|---|
| `beginAdapterControl` | **targetSdk 33+ 恒拒**（官方注释：DO/PO/系统 App 除外），`disable()` → `bleng.operation.not_supported`；radio 已开时 `enable()` 被接受 → `done`（≠状态变化） | `NotSupported`（dispatch 级）：`CoreBluetooth cannot change the radio power state` | ✅ `AdapterControl.test.tsx` | ✅ `adapter-control`（两端） | — |
| `beginAdapterObservation` + `nextEvent` | ✅ | ✅ | — | — | ✅ |
| `beginQueryDevices(connected)` | ✅ | ✅ | ✅ `Devices.test.tsx` | ✅ `devices-query` | — |
| `beginQueryDevices(bonded)` | ✅ | `NotSupported`：无 bonded 清单 | ✅ `Devices.test.tsx`（无链路） | ✅ `devices-kinds`（两端） | — |
| `beginQueryDevices(companion)` | ✅ | `NotSupported`：companion 是 Android 平台特性 | ✅（同一测试覆盖无链路路径） | ✅ `devices-kinds`（两端） | — |
| `beginCompanionAssociation` | ✅（CompanionDeviceManager） | `NotSupported` | — | — | — |
| `beginCompanionPresence` | ✅ | 无 backend 实现 | — | — | — |
| `adoptPeripheral(key)` | ✅ | ✅ | — | — | — |
| `beginQueryMtu`（`request=None`） | ✅ `route.mtu` | ✅（`mtu=nil`，给两种写上限） | — | — | — |
| `beginRequestMtu(n)` | ✅ `requestMtu` | `NotSupported`：`CoreBluetooth does not support explicit MTU requests` | — | — | — |
| `beginSetConnectionPriority` | ✅ | `NotSupported`：`CoreBluetooth does not expose connection priority` | — | — | — |
| `beginGattCacheRefresh` | ✅（要求连接静默：无 pending discovery/IO） | `NotSupported`：`CoreBluetooth manages its attribute cache` | — | — | — |
| `beginBond` / `beginRemoveBond` / `nextState` | ✅ | `NotSupported`：无程序化配对/解绑 | — | — | — |
| `BlengGattDescriptor.beginRead/beginWrite` | ✅ | 读 ✅；写 `NotSupported`（只允许 `setNotifyValue(_:for:)`，直接写会 abort） | ✅ `Descriptor.test.tsx` | ✅ `descriptor-io` | — |
| `BlengPeripheral.beginAutoConnect` | ✅ `connectGatt(autoConnect=true)` | `NotSupported`：`CoreBluetooth cannot request an auto-connect link`（落在**操作终态**，提交被接受） | ✅ `AutoConnect.test.tsx`（Probe 页） | ✅ `auto-connect`（两端） | — |
| `localName()` / `isConnected()` | ✅ | ✅ | 部分（`localName`） | — | — |

## C. 建议补的场景（按"证明了哪条承诺"排序）

| # | 场景 | 证明什么 | 需要的 App 面 | 成本 |
|---|---|---|---|---|
| 1 | **`link-facts`** | MTU 查询/请求（含请求后重查 = 生效而不只是回执）、连接优先级、GATT 缓存刷新：**照做或类型化拒绝** | 已完成（4 个控件，事实走 `run-status`） | ✅ Android 真机（见 `2026-11-25-link-facts-scene.md`） |
| 2 | **`adapter-control`** | App 自己关/开收音机，**只**证明平台答案被如实上报（API 33+ 与 CoreBluetooth 都拒绝，radio 不会被关）；`done` 只表示请求被接受，状态面归 adapter observation lane（L 档 `adapter-transition`） | 已完成（Probe 页 `probe-radio-strip` 2 个控件 + `radio`/`radioTarget` 标量；owner 级探针，R6） | ✅ 两端真机 2026-11-26（见 `2026-11-26-adapter-control-scene.md`） |
| 3 | **`descriptor-io`** | 描述符读/写（CCCD 原值写回，不改变订阅状态）+ 读回一致：**关→读→写→读**，每次读都必须真的发生 | 已完成（2 个控件，事实走 `run-status`；`descriptorReadTotal` + 计划的 `advance`） | ✅ 两端真机 2026-11-26（Android 写 `done`；iOS 写为平台拒绝，见 `2026-11-26-ios-seven-scenes.md`） |
| 4 | **`devices-query`** | `connected` 清单里出现本场 peer（按 owner-scoped key 比对，不是按位置或名字）；`bonded`/`companion` 在 iOS 上如实拒绝 | 已完成（1 个控件并入 scan 主行，事实走 `run-status`） | ✅ 两端真机 2026-11-26（见 `2026-11-26-ios-seven-scenes.md`） |
| 4b | **`devices-kinds`** | 主线不需要的两种清单（`bonded` / `companion`）：**不需要对端或链路**，每个答案必须是平台自己的清单或自身的 `not_supported` | 已完成（Probe 页 2 个控件 + `devicesKind` 标量；owner 级探针记录，不依赖 `ScanRun`） | ✅ 两端真机 2026-11-26（iOS 两种清单都以平台码拒绝，见 `2026-11-26-ios-seven-scenes.md`） |
| 5 | **`bond`** | 创建/移除配对 + `nextState` 进度；iOS 如实拒绝 | 2 个控件 + 1 个标量 | 高（fixture 需可配对，固件要加 SMP） |
| 6 | **`auto-connect`** | Android 断链后在 peer 重新广播时自动重连；iOS 如实拒绝 | 已完成（Probe 页 1 个控件 + `auto` 标量） | ✅ 两端真机 2026-11-26（iOS 以平台码拒绝，见 `2026-11-26-ios-seven-scenes.md`） |
| 7 | **`companion`** | Android 关联 + presence 流；iOS 如实拒绝 | 1 个控件 + presence 标量 | 高（系统 intent + 用户确认，可能就是不该自动化的那类） |
| 8 | **`adopt-peripheral`** | 本机作为 Peripheral 被外部 Central 连接 | 1 个控件 + 一个 peer 侧 Central | 很高（需要 Central 角色 peer：Mac/iPhone/DK tester） |

不做成场景的：`BlengProvider`/`shareLane`/`failureReason` 这类纯 JS 派生（jest 已经是它们的正确层级），
以及 `beginQueryMtu` 之外的纯 Rust facade 契约（`crates/bleng/src/tests/facade_tests.rs` 已覆盖）。

### 明确不列成 S 级的三条（及原因）

这三条不是"还没做"，而是**按当前实验室条件不应该做成 S 级**。写成场景只会得到一条看起来通过、
实际上什么都没证明的 trace：

| 能力 | 为什么不该有 S 级场景 |
|---|---|
| `beginBond` / `beginRemoveBond` | 固件 `multifunction-rf-v2-a` 没有 SMP/配对面，场景只能等到超时；真正要加的是固件能力和一个新 profile，不是 App 面 |
| `beginCompanionAssociation` / `beginCompanionPresence` | 关联会弹系统 pairing/授权 intent，需要人工确认；把"人点同意"自动化会把场景结果变成对操作员的断言 |
| `adoptPeripheral(key)` | 需要**另一个 Central 角色的 peer**（Mac/iPhone/第二个 DK）主动连本机；当前只有一个 DK 且它只作 Peripheral，造不出对端 |

它们仍不落在 `—`：`beginBond`/`beginCompanionAssociation` 在 B 表有平台事实，
`adoptPeripheral` 有 `BlengProvider` 级别的 jest 面。要升成 S 级，先补固件/第二台设备/人工确认流程，
再回表把档位改掉。

## D. 关于 `radio-loss-live` 的一处架构修正

原计划需要给共享驱动加一个"radio off/on" host action，并因此把平台 radio 控制（Android
`AndroidRadioController`）塞进中立的 `env_native`。现在 `beginAdapterControl` 已经是产品 API：
**让 App 自己关/开**，场景就退回"只有 App 行动、host 只作证"的同构形态，不需要任何平台接缝。
代价是场景只在 Android 上有"照做"分支；iOS 分支断言的是同一个 API 的诚实拒绝。

## E. 维护规则

- 能力集变化（spec 新增接口）时，先更新 B 表，再决定它落在 J/S/L 哪一档；**不允许**让一个能力悄悄停在 `—`。
- 一个场景进入 S 档的条件与既有场景一致：`SCENES` 有条目、`ORDER` 有位次、`test_scene_semantics.py` 有派生守卫、
  真机 trace 完整、离线 `integrity=passed`。
- **`CommandConsole` 是固定高度预算，不是随便加的容器**（2026-11-25 实测）：`link-facts` 第一版在 console 里
  加了 4 个按钮 + 4 行结果文本，`notification` 立即在真机上失败——`disable-notifications` 被挤出视口，
  而 Android 对不在视口内的元素**根本不渲染**，于是 `tap` 报 "timed out before … enabled"。新控件优先并入
  已有 wrapping 行，结果事实走 `run-status`，不要新增文本行；确实需要更多空间时应该新开页面而不是加高 console。
- 本文件只记录职责与缺口；候选事实写 `docs/testing/reports/`。
