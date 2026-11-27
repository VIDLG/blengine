# React Native BLeng 集成边界

> 这是 [`README.md`](README.md) 的中文镜像，记录**归属与构建边界**。面向使用者的文档是
> [`API.zh.md`](API.zh.md)（中文）/ [`API.md`](API.md)（英文），从五步快速开始读起；可运行示例在
> [`examples/react-native-bleng-quickstart`](../../examples/react-native-bleng-quickstart/README.md)。

`BlengProvider` / `getBlengCentral()` 让一个 JS runtime 只保留一个 central，所以 React Fast
Refresh 会保留同一个 central 和活着的 capability。完整 reload 是新的 JS runtime，必须重连；
挂载/卸载 provider 从不启动或关闭 BLE。显式收尾用 `closeBlengCentral()`。

这个目录是生产用的 Apple + Android BLeng 包线。它刻意与下面这些保持分离：

- `tests/consumers/android/bleng`：只消费产物的消费者与测试参照。

正式 RN 线有自己的[干净消费者宿主](../../tests/consumers/react-native-bleng/README.md)，
没有第二个源码根。正式的原生桥契约测试放在该消费者的 Swift/Kotlin 测试目标里，不在本库。

被维护的移动模块只消费已发布的产物：

```text
Android: 标准 RN Android library module + dev.blehub:bleng:0.0.0 + dev.blehub:blehub-android:0.0.0
Apple:   已 staged 的 combined apple-bleng UniFFI 包 + BleHubApple
```

Apple 包线由下面这条命令显式 staging：

```text
pixi run just react-native stage-apple-bleng
```

它与默认的 `apple-production` staging（供 BleHub-only RN 包使用）是两条线。两者都复制
[`platforms/apple/packaging/BleHubApple.podspec`](../../platforms/apple/packaging/BleHubApple.podspec)
里的后端 Podspec，而不是复制集成方自己的副本。本包保留它在 `packaging/apple/Package.swift`
下的后端 SwiftPM manifest；`tools/react_native/stage_bleng_apple.py` 生成它组合后的 UniFFI
Podspec。BleHub-only 线保留自己的 UniFFI 模板与原生镜像。

正式 iOS 消费者只有一个受维护的编译入口：

```text
pixi run just react-native formal-host-ios-build
```

它 stage 组合 Apple 包，并检查/生成锁定的 Nitro glue；Node 与 Pod 安装仍然是显式前置条件，
不是藏在普通构建里的工作。一个[构建所有者](../../tools/react_native/bleng_ios_consumer.py)
校验已安装的正式 Podspec、在**不含 Nitro/React** 的隔离 staged SwiftPM 图里编译四个原生
adapter/owner 源，然后构建既有的正式 CocoaPods App/原生测试 workspace。隔离目标属于正式宿主
的 `ios/PlatformContract`；它渲染出来的工程与构建产物只落在 `.work/`。

`formal-host-ios-build` 接受一个位置参数：省略或 `false` 表示校验本地 Pods 准备回执；`true`
显式传入 Python 的 `--refresh-pods`。缺失或陈旧的准备会 fail closed，不采用、不降级。显式刷新
先在安装前失效，安装成功后才封存。源/原生字节改动被接受，但元数据、codegen、schema、依赖成员
或环境漂移必须刷新。宿主 Podfile 拒绝请求预编译的 React Native 源码回退。

缺失的包依赖不能被宿主的显式 Pod 依赖掩盖。该构建是未签名的通用 iOS Simulator
`build-for-testing`：它既不执行原生/UI 测试，也不安装/启动 App、不签名、不控制设置，也不主张
RF/恢复证据。这个正式的源码/本地包宿主不是干净的 npm-tarball/artifact-only 门禁；原生 staged
包消费者保留它们各自的打包/来源责任。此前独立的 Swift/Nitro 编译消费者及其重复的 npm/Pod
配置已退休。见 [iOS Pods 准备边界](../../tools/react_native/README.md#formal-ios-pods-preparation-boundary)
与[当前候选报告](../../docs/testing/reports/2026-11-11-ios-pod-preparation-boundary.md)。

在 Apple 上，`platforms/apple/Sources/BleHubApple` 是唯一后端真源。
`.work/staged/apple-bleng-react-native/BleHubApple/Sources` 是打包 recipe 为 CocoaPods 复制的
生成 staging 产物，禁止直接编辑。Android 消费已发布的 `blehub-android` 后端产物，而不是复制
后端源码。

两个平台都暴露一个生命周期 Nitro 桥，带类型化初始化快照，背后是 Rust `Supervisor` 与原生
`PlatformSession` 适配器。Android 包是一个单一根 library 模块，所以 RN CLI 能直接自动链接它；
它不需要嵌套的 `:library` 工程。Nitro 的 C++/JNI 代码只是生成/最小化的框架胶水；Swift 实现 iOS
HybridObject，Kotlin 实现 Android HybridObject。没有任何手写的 C++ BLeng 或 UniFFI 业务接口。
`createBlengCentral()` 暴露类型化的初始化/关闭、已批准的扫描操作投影、连接/断开操作投影、
GATT 发现/读/写/RSSI 投影、订阅流以及 adapter 状态观察 lane（`beginAdapterObservation()`）。
它不暴露裸 UniFFI 句柄、operation ID、裸 BLE 命令/事件、恢复上下文、generation ID 或策略计划。
owner 退休与 fresh-owner 恢复仍是 Supervisor 内部动作，不是 JS 方法。

## Apple 引擎生命周期

在 iOS 上，`createBlengCentral()` 先实例化私有的生命周期 TurboModule，再构造 Nitro owner，
然后把那个真实的 NativeState 绑定到属于当前 runtime 的模块实例。模块/绑定器缺失、重复绑定、
跨引擎重绑都会 fail closed。生产原生构造器同样拒绝未绑定时的 `initialize()`；工厂不能通过直接向
Nitro 要注册对象名来绕过。JS 包与原生二进制必须一起更新：没有该模块的旧二进制无法满足新工厂。

一个模块在失效前最多保留 64 个已绑定原生 owner，包括那些显式关闭、否则可能在同一个 live
runtime 里重启的 owner。容量拒绝既不认领也不退休被拒绝的对象。RN 的 `RCTInvalidating` 回调会
永久封存每个已绑定 owner，并启动它既有的原生退休屏障，而不调用旧的 JS runtime。同步回调返回
不等于异步清理完成或资源归零。原生消费者可以观察保留的结果（包括清理失败）；本机制不向 JS
导出任何生命周期控制、ID 或裸句柄。

拥有原生宿主的一方可以在绑定新 owner 之前挂上确切的 predecessor 生命周期。继任者的 `initialize()`
会等待真实的旧 RN 回调与成功的原生退休，然后构造 backend，再重新校验它的 start ticket。观察
期限被限制在 1–30 秒；到期不取消旧清理，也不制造回调证据。失效会取消挂起的启动准入，中间引擎
失败也不能抹掉更早的转换失败。没有进程级"当前引擎"注册表。

正式原生目标用打包入口（调用真实包工厂与 `initialize()`，跑在脚本化 BLE backend 上）验证真实
Hermes/manager 失效与同进程同宿主 `RCTHost` reload。正式消费者的 AppDelegate 与 reload 骨架
现在共享一个宿主作用域的 `FormalReactNativeDelegate`，在 RN 收到替换模块之前安装这些 predecessor
关系。它必须属于一个工厂/宿主，不能跨宿主复用。创建被拒时返回一个已封存、不可绑定的模块，让
RN 完成注册并触发真实失效；它绝不静默回退，也不把拒绝报成成功启动。

模拟器测试还会挂载一个小的 Fabric 状态视图，并在 reload 前后独立检查它的原生 UIKit 子树。这不是
完整工作台、已呈现的 App 窗口、物理 BLE、mobile/RF、Android 引擎绑定或已完成的旧线迁移。见
[runtime binding](../../docs/testing/reports/2026-11-10-apple-engine-runtime-binding.md)、
[scoped RCTHost reload](../../docs/testing/reports/2026-11-11-apple-rcthost-reload.md)、
[shared App delegate/rendered-subtree evidence](../../docs/testing/reports/2026-11-11-apple-formal-delegate-rendered-reload.md)。

## Android 引擎生命周期

Android 现在承载同一套语义，实现落在平台真正提供钩子的地方。`BlengEngineLifetime`（纯 Kotlin）
在第一次失效时封存准入、退休每个已绑定 owner 并汇总它们确切的关闭回执、最多连接一个继任者，
并用 1–30 秒的有界观察窗口把继任者闸在 predecessor 的干净退休之后。被取消或从未派发的
predecessor 闸门不能被报成干净退休。

`BlengEngineLifetimeRegistry` 只按确切的引擎 key 解析生命周期；未知 key 返回 `null` 而不是默认值，
所以没有进程级"当前引擎"捷径。`HybridBlengCentral` 在构造时绑定到自己
`ReactApplicationContext` 的生命周期，并在未绑定时拒绝 `initialize()`。

`BleHubBlengEngineLifecycleModule` 是 RN 边界：一个无 spec、没有 JavaScript 方法的 TurboModule，
每个引擎急切创建，它的 `invalidate()` 退休该引擎。RN 0.87 的 bridgeless
`TurboModuleManager.invalidate()` 正是投递该回调的东西。引擎退休是永久的：已绑定 owner 会拒绝
来自已拆除引擎的后续 `initialize()`，而显式 `close()` 仍允许 JS 层重启。

继任者闸门有生产调用方：`BleHubBlengEngineHostWiring` 由宿主的 `ReactPackage` 实例拥有，该实例
与宿主同寿，因此能跨引擎 reload 存活。`getModule(name, context)` 每个新引擎运行一次，并在那里
安装确切的 predecessor；被拒的连接会封存继任者，而不是让它无闸启动。那个 package 实例是
Apple 宿主作用域 delegate 在 Android 的对应物；没有进程级当前引擎。

Nitro 的 Android 侧没有 Kotlin JSI 绑定钩子，所以 `NitroModules.applicationContext` 是唯一的
引擎上下文来源，owner 执行确切上下文查找并 fail closed。因此要求每个进程一个 live `ReactHost`；
并发引擎配置是未验证的限制，不是受支持模式。生产代码里没有测试专用旁路。

已验证：45 条 JVM 测试（含 21 条新的 lifecycle/registry/wiring 测试）、Android App 与
`androidTest` 编译，以及一个**真机 reload 门禁**
（`formal-host-android-engine-reload-test`），它在正式宿主 App 的 instrumentation 进程里启动、
reload 并销毁真实 `ReactHost`。见
[对等报告](../../docs/testing/reports/2026-11-11-android-engine-retirement-parity.md)。

## 连接投影

一个失败 DTO 服务所有操作视图。`BlengFailureInfo`（`domain`、`code`、`message` 与可选平台细节）
由 Rust 在 `crates/bleng-supervisor/src/ffi/failure.rs` 产生，由两个平台的 `BlengFailureMapping`
机械投影；扫描与连接结果永远不会把同一个运行时错误分类成不同东西。`code` 稳定，`message`
只用于展示。

连接从一条广告产生的能力开始：`BlengPeripheral.beginConnect()` 返回有界提交，只有成功的 `wait()`
才产出 `BlengConnection`。`BlengPeripheral` 与 `BlengConnection` 都是 Rust 创建的对象，携带产生
它们的那个确切 owner 上下文；没有 `ConnectionId`、地址、句柄或 owner 身份跨进 JS，来自已退休
owner 的能力 fail closed，而不是去寻址一个替换引擎。`BlengConnection.beginDisconnect()` 是唯一
拆除控制，它的等待消费一个结果。取消是独立回执，绝不回滚原生状态；断开失败让原生状态保持未知，
所以 UI 保留该能力，而不是声称链路已关闭。

声明的超时值在任何提交分配命令之前由 Rust 校验；投影出的 `input/invalid_timeout` 失败意味着
没有命令被发出。提交、等待、期限与取消都留在 BLeng 里 —— Swift/Kotlin 桥只做 DTO 映射。

已验证：Rust `ffi_scan` 集成测试覆盖成功、单消费者语义以及两个能力在 owner 退休时的拒绝；5 条
Jest Workbench 测试覆盖 JS 路径；正式 App 与 `androidTest` APK 编译通过。未验证：对真实外设的
真机连接/断开（需要 nRF52840 lease），所以这不是 RF 证据。

## GATT 发现、特征 I/O 与 RSSI

`BlengConnection.beginDiscoverGatt()` 返回有界提交，其 `wait()` 产出 `BlengGattSession`。该会话
及其之下的一切都是 Rust 创建的能力树：`services()` → `BlengGattService`（`uuid()`、`kind()`、
`characteristics()`）→ `BlengGattCharacteristic`（`uuid()`、`properties()`、`descriptors()`、
`beginRead()`、`beginWrite()`）。没有 `ConnectionId`、`GattSessionId`、`GattCharacteristicId` 或
`GattDescriptorId` 跨进 JS，所以没有数字地址可以猜，也没有第二条访问特征的路径。

值得保留的设计决定：

- **身份就是能力。** 会话的身份是 Rust 分配的确切 owner + connection + GATT session。later
  discovery 产出*新*会话；上一套拓扑的能力不会被重新指向。提交保持有界且被接受，Rust 在准入处
  拒绝陈旧拓扑（`bleng.not_ready.gatt_not_ready`），不做任何派发 —— 有测试先重新发现、再寻址
  已退休拓扑来验证。
- **描述符只是数据。** BleHub 契约里没有任何东西读写描述符，所以投影只是 `{ uuid }`，描述符
  句柄被丢弃，而不是为了对称导出去。
- **服务类型是三态。** `primary: Option<bool>` 被投影成 `'primary' | 'secondary' | 'unknown'`，
  而不是可空布尔：这个面的其余部分用 `undefined` 表示缺失，Nitro 边界上的 `null` 会成为第二种
  约定。
- **缺失与值不同。** `BlengReadResult` 携带字节，所以零长度读是成功；`BlengRssiResult` 携带读数，
  所以 `0` 与负 dBm 都是真实值。两者都有测试。
- **写是回执。** 成功的写等待意味着 BLeng 观察到完成，而不是对端已存储该值；Workbench 正是这样
  表述的。写长度校验留在 Rust（`MAX_WRITE_VALUE_BYTES`），不在桥里镜像一份。
- **App 内有界保留。** Workbench 最多保留 8 个特征能力，并报出有多少无法再寻址，因为能力会让
  原生状态保持存活。

已验证：5 条 Rust `ffi_scan` GATT 测试（快照投影、单消费者读/写、被替换的拓扑、零/负 RSSI、
退役 supervisor 的脱离）、6 条 Jest Workbench GATT 测试、`bleng-snapshot-check`，以及正式
App/测试图编译通过。未验证：在真实外设或真实 iPhone 上的任何一项。从未对真实设备读写过特征，
所以这不是 RF 或"能在真实硬件上工作"的证据。

## 订阅

订阅是一条**流**，所以它是唯一不是请求/响应回执的能力：

```text
BlengGattCharacteristic.beginSubscription(mode)
  → BlengSubscription.waitUntilEnabled(timeoutMs)  → { mode? }
  → BlengSubscription.nextEvent(timeoutMs)         → { value } | { ended } | failure
  → BlengSubscription.beginDisable() → operation.wait(timeoutMs)
```

值得保留的设计决定：

- **enable 回执携带实际模式。** `EnableNotificationsOperation::wait` 返回
  `NotificationSubscription { subscription, actual_mode: Option<NotificationMode> }`，而不是把模式
  丢进规范事件流。调用方不该为了知道自己操作的结果再去观察第二条通道，而从事件里关联模式会让
  "订阅已激活但模式未知"成为真实的静默状态。`None` 表示平台没有报告模式；它被投影成缺失，绝不
  从请求猜测。
- **一个 owner、一个 reader、两条 lane。** `SessionObservationProjection` 把 owner 既有的规范事件
  流分发给扫描 lane、连接端 lane(s)、订阅 lane 与 adapter 状态 lane。不存在第二个 reader 或
  runtime，`ObservationProjectionLease` 在退休时原子地关闭所有 lane。因此 FFI 摄入对象叫
  `SessionEventProjection`，而不是扫描专用名。
- **lane 在 enable 提交之前就存在。** `begin_enable_notifications` 在同一把锁下注册有界 lane，
  随后才执行 BLeng 提交，lane 绑定在携带*它自己* enable command id 的 `NotificationStateChanged`
  上。因此值不会在状态变化与稍后注册之间溜走，来自更早 enable 的陈旧订阅也绝不会被误认为本次。
- **溢出让流 fail closed。** 慢消费者不能静默丢值：64 个值或 16 KiB 时未读积压被丢弃，
  流以 **`reason: 'overflowed'` 结束** —— 这是这条 lane 自己的终结点，不是操作失败：原生订阅与
  其他操作都继续跑（扫描 lane 的形状相同）。`nextEvent` 是单挂起 pull，它的期限是调用方的 ——
  超时不等于订阅已停止。
- **终结点显式且被保留。** `disabled`（订阅结束）、`connectionClosed`（链路消失）与 `ownerClosed`
  （本引擎退休）彼此不同，退休永远不会被报成合成的原生 disable。流的结束是**被保留的事实**：
  再读一次 `nextEvent` 会返回同一个终结点，因为按自己节奏轮询的消费者仍必须能知道流为什么结束。
  精确一次投递属于操作回执（`wait`/`waitUntilEnabled`），那是单消费者的。退休是唯一会替换保留
  终结点的事件。扫描 lane 遵守同一规则。
- **disable 通过能力寻址。** 订阅身份永远不到达调用方：`beginDisable()` 使用从本次订阅自己的
  enable 学到的身份，所以陈旧能力无法关闭别人的订阅，在订阅被观察到之前调用它会以
  `notification.subscription_unknown` 失败，而不是猜测。

已验证：6 条 Rust `ffi_scan` 订阅测试（含空值的有序值、实际模式回执与缺失、disable 终结、未知
订阅拒绝、溢出 fail closed、退休 owner 终结、外来订阅过滤）、8 条 Jest Workbench 测试、两个正式
原生图编译通过，以及 macOS 产物消费者对重新生成的包镜像重跑类型化操作链。未验证：来自真实外设
或 iPhone 的真实通知，以及受门禁的 RF 通知场景。见
[订阅报告](../../docs/testing/reports/2026-11-12-rn-notification-subscription.md)。

## 内部扫描事件交接

既有原生 reader 也接受 Rust 的 per-owner `SessionEventProjection`。它只把扫描
start/advertisement/stop 信封同步转发给可选的 native `scanEventSink`，完整源上下文由 Rust 固定。
它不创建第二个事件 reader、缓冲或 runtime。既有裸诊断 sink 与 Apple recovery inbox 仍是分开的
路径；它们不加入正式 JS 导出。

Rust 在显式退休之前、以及所有权 drop 时撤销端点。这阻止的是将来的投影，而不是已经返回的信封
的投递；独立排队的消费者仍必须比较完整源上下文。它不是原生 stop/资源归零的证据。Apple 的恢复
广告捕获现在是按需作用域：在恢复扫描之前订阅、选择它确切的 scan ID，并在选中/失败/取消时释放。
普通广告没有空闲恢复缓冲。主动捕获溢出仍会让 owner fail closed，终结/收音机历史仍单独有界；
这次改动没有确立持续扫描。见
[扫描投影职责](../../docs/architecture/bleng-recovery.md#scan-event-projection固定来源而非第二个-reader)。

正式 JS 扫描 API 通过 Rust 的有界 `ScanDelivery` 使用同一个 reader/投影；它绝不导出裸原生信封，
也不增加第二个 reader。原生诊断 sink 与产品面保持分离。

## 类型化初始化结果（未冻结候选）

```ts
import { createBlengCentral, requireOutcome } from '@blehub/react-native-bleng'

const central = createBlengCentral() // 不会初始化 BLE。
const { snapshot } = requireOutcome(await central.initialize())
// snapshot.state、scanAuthorization、connectionAuthorization、name?
const closed = await central.close()
// closed.ownerClosed 与 closed.failure 是两个独立事实。
```

`initialize(): Promise<BlengInitializeResult>` 返回 `{ snapshot }` 或 `{ failure }` —— 原生失败是数据，
和 `close()` 报告清理失败完全一样。`requireOutcome` 是同一个联合的抛错视图（基于唯一的
`BlengOperationError`），不是第二套结果类型。`snapshot` 投影 Rust 初始化返回的确切
`ForeignOwnerStart.adapterSnapshot`。Swift/Kotlin 只映射四个 adapter 状态、五个授权状态与可选
名字；它们不推断 readiness，也不从回调重建身份。扫描与连接授权彼此独立。缺失的名字保持缺失，
空名字保持为空。初始化可以在 `poweredOff`、`unavailable`、`unknown`、`denied` 或 `notDetermined`
下成功；成功意味着会话被初始化，不意味着 BLE 可用。

owner 装配有库内上界（当前 30 秒）。命中上界时返回 `{ failure }`，`code: 'bleng.timeout'`；此时原生工作
可能还在跑，唯一有意义的下一步是 `close()`。`close()` 等原生清理完成，没有上界。

`close(): Promise<BlengCloseResult>` 把逻辑上的 `ownerClosed` 与 Rust 投影出的可选、无身份的
`{ code, message }` 失败分开报告。非终结的 busy 保留 owner；终结的清理失败会关闭它，但不主张
资源归零。重复 close 保留第一个终结结果（包括失败），直到一次显式的新 start 构造新 owner。
意外的桥异常仍是 Promise 拒绝；它们不证明 owner 已关闭。原生层面的启动失败不是拒绝，而是 `initialize()` 的 `{ failure }` 数据。

这是一个**时间点初始化快照**。之后的收音机/权限变化只能通过 `beginAdapterObservation()` 观察
（见 [`API.zh.md`](API.zh.md)），绝不从这个 DTO 推断。这个 DTO 里没有 generation、命令身份或恢复信号。
它替换了此前私有的 `Promise<void>` 返回；它不是兼容垫片，也不是 ABI/公共契约冻结。

正式消费者拥有映射契约测试与 UI 测试。`pixi run just react-native formal-host-test` 运行它的
Jest 发现；原生映射断言需要各自的原生运行时门禁。组件测试 mock 的是这个正式包边界，绝不是旧
探针。工厂测试用隔离的 RN/Nitro 依赖跑真实的包源码工厂。

## 类型化扫描投影（未冻结候选）

```ts
const submitted = central.beginScan({ serviceUuids: [], allowDuplicates: false })
if ('scan' in submitted) {
  const scan = submitted.scan
  const started = await scan.waitUntilStarted(5000)
  // 检查 started.failure；仅仅提交被接受不是扫描已启动。
  const observed = await scan.nextEvent(5000) // 显式单挂起 pull，不是回调循环。
  const stopping = scan.beginStop()
  if ('operation' in stopping) {
    const stopped = await stopping.operation.wait(5000)
    // 检查 stopped.failure；超时从不意味着原生 stop 或自动重试。
  }
}
```

start/stop 结果等待是单消费者的；取消通过 `cancelStart()` / `stop.cancel()` 保持独立。只有消费
之前的输入失败才允许再次等待那个原始结果。已被消费的超时/失败不是"稍后能成功重等"的承诺。
广告携带不透明、绑定 owner 的外设、可选原生事实与自有 `ArrayBuffer` 字节；没有裸身份进入 JS。
Rust 拥有 64 条广告/256-KiB 的有界观察投递、确切上下文绑定、超时/错误投影与退休。原生
`dispose()` 只结束观察，不停止 BLE 扫描。溢出显式失败，没有隐式 stop、重试或 owner 关闭。见
[扫描契约](../../docs/research/react-native/react-native-bleng-scan-contract.md) 与
[候选证据](../../docs/testing/reports/2026-11-04-formal-rn-scan.md)。

## 等待一个操作

`begin*` 仍是提交模型。`pendingOperation()` 是它上面的派生便捷层，给想要 `await` 又不想失去回执或
取消控制的调用方：

```ts
import { pendingOperation, BlengOperationError } from '@blehub/react-native-bleng'

const pending = pendingOperation(characteristic.beginRead(), 5_000) // 被拒的提交在这里抛出
try {
  const { value } = await pending.result // 只有成功臂；失败会被抛出
  // ...
} catch (error) {
  if (error instanceof BlengOperationError && error.code === 'bleng.cancelled') {
    // `code` 是唯一可以拿来做控制流的字段；`message` 只用于展示。
  }
}
// 取消永远是显式的：丢弃 `result` 不会取消。
const receipt = await pending.cancel()
```

三条规则让它成为派生的，而不是第二个操作模型：它只通过该操作自己的有界 `wait` 结算；取消只通过
显式 `cancel()` 发生，且回执原样返回；原生失败从不被重新措辞。`beginScan()` 与
`beginSubscription()` 保留各自按 lane 命名的控制，所以它们使用显式形式
`pendingOperation({ operation: { wait: (ms: number) => scan.waitUntilStarted(ms), cancel: () => scan.cancelStart() } }, 5_000)`，
而不是一次会把"调用方在哪条 lane"藏起来的统一改名。见
[`pending-operation-layer.md`](../../docs/research/react-native/pending-operation-layer.md)。

本模块刻意把 `blehub-uniffi` 从 `blehub-android` 依赖里排除，因为已发布的 BLeng AAR 已经包含
组合后的 `blehub + bleng + bleng-supervisor` UniFFI Kotlin/原生镜像。这保持了"单镜像"包不变量。
