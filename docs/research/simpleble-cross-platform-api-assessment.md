# SimpleBLE 跨平台实现与 API 对 BleHub 的借鉴评估

- 状态：本地源码调研与实现建议；最终约束以 [`.plan/PLAN.md`](../../.plan/PLAN.md) 为准
- 日期：2026-08-29
- 评估对象：SimpleBLE `1.1.1`
- 本地源码：workspace `D:\projects\contrib\simpleble`
- 审计 revision：`ceb2f295a2cca0d38d19a5a30946e8997ffc2f61`
- revision 日期：2026-08-26
- revision 标题：`Fix SimpleJavaBLE JPMS compatibility`
- BleHub 相关研究：[BoltFFI 原生后端与 Rust 直连平台 API 的技术评估](boltffi-vs-rust-native-ble-backends.md)
- BleHub command/event 基线：[BleHub 跨平台命令下行与事件上行契约](cross-platform-command-event-contract.md)

## 1. 评估问题与结论

本报告回答四个问题：

1. SimpleBLE 如何把 Windows、Linux、macOS 和 Android 的 BLE Central API 统一到同一套公共 API；
2. 它把哪些平台差异隐藏在 backend 内部，又有哪些差异仍然泄漏为不一致语义；
3. SimpleBLE 的公共对象、同步 API、callback、identity、GATT 和跨语言包装相对 BleHub 有什么优劣；
4. BleHub 可以直接借鉴什么，必须避免什么，是否需要修改当前总体架构。

结论是：**不建议把 BleHub 改成 SimpleBLE 式的统一 C++ backend 或共享 mutable object API；BleHub 当前“统一 Rust contract + 平台原生 backend + dispatch/event 两阶段语义”的总体方向更适合本项目。**

SimpleBLE 的价值主要来自两方面：

- 它是四个平台真实实现细节的本地源码样本，暴露了 native handle、callback thread、GATT completion、disconnect 和 identity 的实际差异；
- 它积累了若干值得借鉴的 backend 内部模式，例如一个 native identity 只保留一个 wrapper、per-scan seen set、先清 callback 再做 best-effort teardown、Android subscription rollback、callback payload 拷贝和发布物内容检查。

SimpleBLE 最重要的反面证据是：**把 callback-based BLE API 强行包装成统一阻塞同步 API，会把 timeout、线程、重入、late callback、共享状态和 completion 强度差异隐藏进实现，而不是消除它们。**

## 2. 证据范围与可信度

### 2.1 本地证据状态

本报告只使用当前 workspace 中的本地源码、测试、CI 配置和 Git history，没有使用网络资料。下文 SimpleBLE 源码路径均相对于 `D:\projects\contrib\simpleble` 仓库根目录。

SimpleBLE worktree 在审计时只包含未跟踪的 `.codegraph/` 索引；产品源码与 `HEAD` 一致。`simpleble/AGENTS.md` 提到的相邻 `../design-docs/` 目录当前不存在，因此本文不能引用维护者私有设计意图，只能依据代码和历史判断。

### 2.2 证据标记

| 标记 | 含义 |
|---|---|
| **Verified** | 当前本地 revision 的源码、测试或 CI 直接证明 |
| **Inferred** | 可由多个本地实现事实合理推出，但未通过 runtime reproduction 观察 |
| **Unverified** | 当前未构建、未运行或未做硬件验证，不能声称为运行时事实 |

本次没有运行 SimpleBLE build、emulator、package consumer 或 BLE HIL。涉及死锁、late callback 误关联、泄漏规模和硬件行为的结论，如果没有直接测试证据，均按 **Inferred** 处理。

## 3. 两种架构的根本差异

### 3.1 SimpleBLE 的统一方式

SimpleBLE 的主要统一层是 C++：

```text
C++ public Adapter / Peripheral / Service / Characteristic
  -> shared_ptr-backed frontend handles
  -> BackendBase / AdapterBase / PeripheralBase
  -> Windows C++/WinRT
     Linux C++/SimpleBluez/SimpleDBus
     macOS Objective-C++/CoreBluetooth
     Android C++/JNI/Java Framework BLE
  -> optional C / Python / Rust / Java / Kotlin wrappers
```

公共 `Adapter` 和 `Peripheral` 是持有 `std::shared_ptr<...Base>` 的小对象：

- `simpleble/include/simpleble/Adapter.h:29-113`
- `simpleble/include/simpleble/Peripheral.h:20-80`
- `simpleble/src/backends/common/AdapterBase.h:22-85`
- `simpleble/src/backends/common/PeripheralBase.h`

这让平台 native type 保持在 backend 内，也使公共对象复制成本很低。但复制后的对象共享同一个 backend implementation、callback slot、native handle 和 mutable state。

### 3.2 BleHub 的统一方式

BleHub 的统一层是 Rust contract，而不是统一 backend 实现语言：

```text
Rust application
  -> BleHub command API
  -> BleBackend trait object
  -> platform-native backend

platform callback
  -> backend constructs BleEvent
  -> push_backend_event(token, event)
  -> Rust ingress
  -> caller-provided BleEventSink
```

关键区别：

- SimpleBLE 统一“对象和阻塞方法”；
- BleHub 统一“DTO、command dispatch、event outcome 和 lifecycle contract”；
- SimpleBLE 尝试让平台异步 API 看起来同步；
- BleHub 明确同步 `Result` 只表示 accepted/rejected，最终平台结果由 event 表达；
- SimpleBLE public `Peripheral` 是共享 mutable backend state 的句柄；
- BleHub public boundary 使用 caller-owned typed IDs 和 immutable DTO/event，不建立 canonical public Peripheral object。

## 4. SimpleBLE 公共 API 评估

### 4.1 薄 frontend 与 backend abstraction

**Verified：** SimpleBLE 使用明确的 backend abstract classes，并由 public frontend 转发：

- `AdapterBase` 规定 adapter、scan、paired peripheral 和 callback 接口：`simpleble/src/backends/common/AdapterBase.h:22-85`；
- `PeripheralBase` 规定 connection、GATT 和 notification 接口；
- public `Adapter`、`Peripheral` 不包含平台 header；
- default-constructed handle 在调用时抛 `NotInitialized`；
- factory/builder 把 `shared_ptr<...Base>` 包装成 public object。

优点：

- 平台 native type 不污染公共 API；
- backend surface 紧凑，平台实现容易对照；
- generic precondition 可以在 frontend 集中检查；
- public handle 可复制并跨 wrapper 传递。

BleHub 应继续借鉴“窄 backend contract + 平台类型不出边界”，但不需要复制 C++ PIMPL/shared-object 形状。

### 4.2 `Peripheral` 是共享 mutable object

**Verified：** public `Peripheral` 复制后仍共享同一个 `PeripheralBase`：`simpleble/include/simpleble/Peripheral.h:75-80`。

结果是：

- 两个看似独立的 `Peripheral` 值共享 connection state；
- scan update 会修改此前已返回对象所观察到的 RSSI/advertisement；
- callback 设置作用于共享 implementation；
- 一个 copy 替换 callback 会影响其他 copy；
- adapter 的 persistent cache 可继续强持有 peripheral，使 drop 某个 public copy 不等于释放 native state。

这种 API 对小型 C++ 程序很方便，但 ownership 和 aliasing 不直观。BleHub 的 typed ID + event snapshot 对 FFI、late event 和 caller state 更清晰。

### 4.3 metadata 与 operation 的分离值得借鉴

SimpleBLE 的 `Service`、`Characteristic` 和 `Descriptor` 主要是 metadata snapshot；实际 read/write/notify 仍从 `Peripheral` 发起：

- `simpleble/include/simpleble/Peripheral.h:49-70`
- `simpleble/include/simpleble/Service.h`
- `simpleble/include/simpleble/Characteristic.h`

这是一个合理方向：

- 避免每个 GATT child 都成为拥有 native lifecycle 的公开对象；
- GATT operation 仍由 connection/peripheral 负责；
- characteristic capability 可以统一为 typed booleans；
- write with response 与 without response 保持两个操作。

BleHub 当前“GATT hierarchy 使用 DTO，operation 使用 connection/GATT session + backend-issued instance ID”的方案比 SimpleBLE 更完整，应保持。

### 4.4 `services()` 混合两种数据来源

**Verified：** `Peripheral::services()` 在连接时返回 discovered GATT services，在未连接时返回 advertisement 中的 service：`simpleble/include/simpleble/Peripheral.h:49-55`。

问题：

- 同一个方法根据瞬时连接状态改变语义；
- advertisement service 和 discovered service 的完整性、来源和 freshness 不同；
- disconnected result 通常没有 characteristic；
- Linux 还可能把 BlueZ `Battery1` policy 合成为 GATT Battery Service；
- caller 无法从 `Service` 本身判断 provenance。

BleHub 不应采用这种便利合并。Advertisement service data 与 discovered GATT hierarchy 应继续通过不同 event/DTO 表达，并携带 scan/GATT session identity。

### 4.5 UUID-only GATT addressing 是明显限制

SimpleBLE 的 GATT operation 使用：

```cpp
read(service_uuid, characteristic_uuid)
write_request(service_uuid, characteristic_uuid, data)
notify(service_uuid, characteristic_uuid, callback)
```

证据：`simpleble/include/simpleble/Peripheral.h:58-70`。

四个平台 backend 最终都存在按 UUID map 或 first-match 查找的路径：

- Windows nested `std::map<UUID, ...>`；
- macOS `characteristicExtras` 和 descriptor extras 按 UUID string；
- Android `_fetch_characteristic`/`_fetch_descriptor` 按 UUID 查找；
- BlueZ helper 返回第一个 UUID match。

这不能可靠表示 duplicate service、characteristic 或 descriptor instance。BleHub 已决定使用 backend-issued opaque GATT instance IDs；SimpleBLE 源码进一步证明该决定必要，不应退回 UUID-only API。

### 4.6 同步方法与 callback 混合

SimpleBLE 大多数 operation 是阻塞调用：

- `scan_for()` = start + caller thread sleep + stop；
- connect 通常等待 link 和 service discovery；
- read/write request/subscribe 通常等待 native callback 或 async result；
- notification payload 和 spontaneous disconnect 走 callback。

各 backend 的 `scan_for()` 都直接 `sleep_for`，例如 Android：`simpleble/src/backends/android/AdapterAndroid.cpp:89-93`。

表面优点：

- C++ caller 使用简单；
- `connect()` 返回后通常可以立即访问 services；
- exception 可以直接表示当前调用失败。

实际代价：

- library 必须拥有 timeout、retry、condition variable 和 thread policy；
- callback 可能在同步 method 返回前触发；
- 同一个 public method 在不同平台的 completion 强度不同；
- callback thread 上调用阻塞 operation 可能发生重入或死锁；
- late callback 很难与新 operation 区分；
- timeout 发生后 native operation 可能仍继续。

这正是 BleHub 不应复制的核心设计。

### 4.7 callback slot 与锁

SimpleBLE backend 通过 `kvn::safe_callback` 保存一个可替换 callback。`SAFE_CALLBACK_CALL` 捕获 user exception 并记录日志。

优点：

- callback exception 不会直接穿过 native callback boundary；
- callback load/unload 有基本同步保护；
- backend destructor 可以显式 unload。

问题：

- 每类 event 只有一个 callback slot；
- public handle copies 共享该 slot；
- callback holder 在调用 user code 时持有自己的 recursive mutex；
- public API 没有统一 callback queue/thread contract；
- C/Java/Rust wrapper 又需要各自重新设计 callback ownership。

BleHub 的单一 downstream sink 也必须串行化，但 sink ownership、close drain 和 callback-after-close 已进入显式 contract；不应改成分散在对象上的多个 callback slot。

### 4.8 error API

SimpleBLE C++ exception hierarchy区分：

- not initialized；
- not connected；
- service/characteristic/descriptor not found；
- operation not supported；
- operation failed；
- 部分平台 exception。

这是比单一 boolean 更好的起点。但大多数 backend failure 最终仍是 message-oriented `OperationFailed`。

`AdapterSafe`/`PeripheralSafe` 又把所有 exception 压成 `false` 或 `std::nullopt`，例如 `simpleble/src/frontends/safe/AdapterSafe.cpp:13-158`。这会丢失 permission、timeout、unsupported、not found 和 backend failure 的区别。

BleHub 的 typed synchronous dispatch error + typed asynchronous `BleFailure`/platform details 更适合 FFI 和跨平台诊断。

### 4.9 全局配置

SimpleBLE configuration 是 process-global mutable state，且文档要求在首次 backend interaction 前设置。WinRT deferred disconnect 等配置会直接改变同一 public method 的 observable semantics。

优点是配置简单；缺点是：

- 不能为不同 backend instance 使用独立策略；
- 行为依赖调用顺序；
- test isolation 需要 reset global state；
- object 无法说明自己使用了哪份 config；
- global option 可以把 blocking disconnect 改成本地 deferred completion。

BleHub 应继续使用 platform factory/backend construction config，不引入 mutable global policy。

## 5. 跨平台细节：SimpleBLE 实际隐藏了什么

### 5.1 总览

| 维度 | Windows | macOS | Android | Linux |
|---|---|---|---|---|
| Native API | WinRT | CoreBluetooth | Android Framework BLE + JNI | BlueZ D-Bus + SimpleBluez/SimpleDBus |
| 自有执行位置 | singleton MTA thread（可配置） | dedicated serial dispatch queue | JNI runner；binding 另有 callback runner | backend polling thread |
| public identity | address string | CoreBluetooth UUID 放进 `BluetoothAddress` string | address string | address string |
| connect success policy | 完成 GATT tree cache 后 | link + 完整 service/characteristic/descriptor discovery | link + service discovery | Connected + ServicesResolved |
| GATT correlation | retained WinRT object + UUID map | UUID-keyed task objects | Java object-keyed reusable pending flag | D-Bus proxy + UUID lookup |
| disconnect | 默认可 deferred | 等 didDisconnect，无统一 timeout | callback/timeout | property callback/timeout/retry |
| notify/indicate | 可区分 CCCD | 最终同一 CoreBluetooth notify API | 可区分 CCCD value | 两者折叠为 StartNotify |

这张表说明：SimpleBLE 得到的是“相似方法名”，不是“相同 native guarantee”。

## 6. Windows backend

### 6.1 自有 MTA runtime

**Verified：** `MtaManager` 是 singleton，拥有一个 MTA thread、task queue、condition variable 和 blocking `execute_sync()`：

- `simpleble/src/backends/windows/MtaManager.h:15-88`
- `simpleble/src/backends/windows/MtaManager.cpp:8-53`

这解决了 WinRT apartment 适配问题，但把 runtime/thread ownership 固化进 library。BleHub 可以借鉴“WinRT 调用可能需要明确执行上下文”的事实，不应让 core 或统一 API 拥有 process-global MTA worker。Windows backend 只有在 spike 证明需要时，才使用最窄的 backend-private executor 或 caller-injected context。

### 6.2 scan 与 identity

Windows adapter 使用：

- persistent peripheral map；
- 每次 scan 清空的 `seen_peripherals_`；
- 首次 observation 触发 found，后续触发 updated；
- address 作为 map key；
- `RSSI == -127` 的 WinRT synthetic out-of-range observation 被过滤。

“persistent native record + session-local seen set”值得借鉴，但 address 不应成为 BleHub public identity。`-127` 过滤应作为 Windows mapping spike/test 项验证，不能仅因 SimpleBLE 如此处理就直接升级为 BleHub policy。

### 6.3 connection 与 GATT handle retention

**Verified：** Windows connect 会获取 `BluetoothLEDevice`，最多尝试三次，并在成功路径缓存完整 service/characteristic/descriptor tree。WinRT child objects 被保留在 nested map 中，因为其 lifetime 对后续 operation 有影响。

Spontaneous disconnect 和 manual disconnect 会关闭 child service object、清 GATT map，再释放 parent device：`simpleble/src/backends/windows/PeripheralWindows.cpp:102-238`。

可借鉴：

- backend 必须显式拥有必要的 WinRT child handle；
- teardown 顺序应先阻止 callback，再清 subscription/child GATT object，最后释放 device；
- native event registration token 必须可撤销；
- GATT map 是 platform-private correlation，不应进入 core canonical state。

### 6.4 deferred disconnect 暴露 completion 差异

**Verified：** 默认 deferred path 把 state 设为 `Disconnecting`，关闭 child services，调用 `device_.Close()` 后立即发 disconnected callback；native physical teardown 仍可能在后台继续：`simpleble/src/backends/windows/PeripheralWindows.cpp:172-218`。

非 deferred path 则等待最多 10 秒确认。

这证明同一个“disconnect succeeded”至少可能表示：

1. local logical connection 已关闭；
2. native stack 已报告 disconnected；
3. physical link 已消失。

BleHub 已把 disconnect success 定义为 logical lifecycle completion，并禁止伪造更强语义；SimpleBLE 的实现支持该决策，但也说明必须把 completion evidence 写入 Windows mapping tests。

## 7. macOS backend

### 7.1 CoreBluetooth queue 与 identity

SimpleBLE 在 dedicated serial dispatch queue 上创建 `CBCentralManager`。CoreBluetooth 没有 MAC address，public `address()` 实际返回 `CBPeripheral.identifier` 的 UUID string：`simpleble/src/backends/macos/PeripheralBaseMacOS.mm:170-175`。

可借鉴：

- Apple public identity 必须以 CoreBluetooth identifier/native object correlation 为基础；
- 不要把统一字段命名或语义解释为真实 Bluetooth MAC；
- delegate queue policy 属于 Apple backend factory/config，不属于 BleHub core。

### 7.2 阻塞等待与潜在死锁

**Verified：** 只有 initial connect 使用 5 秒 timeout；service、characteristic、descriptor discovery，disconnect，read，write request 和 subscribe 使用 `WAIT_UNTIL_FALSE` 无限轮询：

- wait macro：`simpleble/src/backends/macos/PeripheralBaseMacOS.mm:17-38`；
- connect/discovery：`.../PeripheralBaseMacOS.mm:178-268`；
- disconnect：`.../PeripheralBaseMacOS.mm:270-287`；
- read/write/notify：`.../PeripheralBaseMacOS.mm:322-450`。

**Inferred：** 如果从同一个 CoreBluetooth serial delegate queue 上调用这些 blocking method，而 completion delegate 也需要该 queue，就可能死锁。即使从其他线程调用，平台 callback 永久缺失也会造成无限等待。

BleHub 的 fire-and-forget backend command + event completion 避免了这种同步等待，不应为“易用”而在 core 恢复 blocking connect/read/write。

### 7.3 read/notify policy 泄漏

**Verified：** 如果 characteristic 已在 notifying，SimpleBLE `read()` 不发 CoreBluetooth read，而是等待下一条 notification 并把它作为 read result：`.../PeripheralBaseMacOS.mm:337-363`。

这是强烈的 library policy，不是 native read guarantee。BleHub 不应合并 read 与 notification；二者必须保持不同 operation/event correlation。

### 7.4 duplicate UUID collision

`characteristicExtras` 以 characteristic UUID string 为 key，descriptor extras 以 descriptor UUID string 为 key：`.../PeripheralBaseMacOS.mm:233-265`、`:334-336`。

这再次证明 backend-issued GATT instance IDs 必须保留 native instance，而不能只存 UUID。

## 8. Android backend

### 8.1 JNI bridge 与 callback thread

SimpleBLE Android 有多层：

```text
Android Framework callback
  -> Java callback subclass
  -> JNI
  -> C++ bridge callback
  -> SimpleBLE backend
  -> SimpleDroidBLE JNI wrapper
  -> Kotlin Flow/callback
```

JNI bridge 会把 Java object 转成 global reference，byte array 转成 owned vector，再投递到 runner。SimpleDroidBLE 还拥有单独的 JVM-attached callback runner。

可借鉴：

- 离开 native callback frame 前复制 byte payload；
- 明确 local/global JNI reference ownership；
- foreign callback 必须有 exception containment；
- Kotlin/Java callback subclass 可以作为单独 bridge artifact；
- callback thread attachment/detachment 必须显式验证。

不应借鉴：

- process-global worker 和 unbounded queue；
- core-owned callback runtime；
- 把 callback scheduling policy固定在统一 API 中。

### 8.2 scan cache 与 scan failure

Android adapter 以 address 保存 persistent `peripherals_`，每次 start 清 `seen_peripherals_`，然后立即报告 scan started：`simpleble/src/backends/android/AdapterAndroid.cpp:20-97`。

问题：

- scan map 没有在相邻实现中表现出统一 mutex protection；
- framework scan failure callback 未完整进入公共 failure；
- local `scanning_` 可能只表示 start call 已执行，而不是 framework 已接受；
- address/private-address rotation 仍没有 identity scope。

BleHub 应让 Android backend 保存 native object correlation，但 public event 使用 caller scan session ID 和 backend opaque `PeripheralId`。Scan failure 必须成为 terminal/event，不应只保留本地 bool。

### 8.3 connect 与 GATT operation correlation

SimpleBLE Android connect 在 link connected 后继续 `discoverServices()`，直到 services discovered 才认为 connect 完成。这个“GATT usable”强 completion 对 caller 很方便，但必须明确它是 library policy。

GATT read/write/descriptor operation 使用 Java characteristic/descriptor object 对应的 reusable `FlagData { flag, status, value, cv, mutex }`：

- `simpleble/src/backends/android/bridge/BluetoothGattCallback.h:69-94`；
- `simpleble/src/backends/android/bridge/BluetoothGattCallback.cpp:85-222`。

每次 operation：

1. 把 object 对应 flag 设为 pending；
2. 调 Android method；
3. 等最多 5 秒；
4. callback 清 flag 并写 status/value。

**Inferred：** 由于没有 command/generation，timeout 后的 late callback 可能清除同一 native object 的下一次 operation flag。实现也没有显式展示 per-connection global GATT queue；不同 object 的 operation 可能被多个 caller thread 并发提交。

BleHub 计划中的 Android per-connection serialized GATT queue、caller command ID 和 backend correlation generation 是必要约束，不应简化成 per-object boolean。

### 8.4 native cache 与 identity

SimpleDroidBLE `NativeCache` 保存 adapter、peripheral 和 callback wrapper map，提供 add/get，但没有对应 adapter/peripheral dispose：`simpledroidble/simpledroidble/src/main/cpp/NativeCache.h:19-101`、`.../NativeCache.cpp:6-50`。

ID 由 adapter identifier/peripheral address hash 派生，没有 generation/collision rejection：`.../NativeCache.cpp:11-33`。Kotlin 又按这些 ID 缓存 wrapper。

风险：

- process-lifetime retention；
- stale ID 无法与新对象区分；
- hash collision 没有 typed failure；
- callback/global ref 的释放依赖进程结束或局部 subscription cleanup。

BleHub token/ID 必须使用不复用、可验证 scope 的 opaque allocation，不使用 hash 作为唯一 handle。

### 8.5 subscription rollback 值得直接借鉴

SimpleDroidBLE notification integration 的正面模式：

- 防止同一 key 重复 collector；
- native subscribe 前先建立 callback owner；
- subscribe 失败时 rollback callback owner；
- unsubscribe 成功或失败路径都移除 owner；
- Kotlin `callbackFlow` 在 finally 中执行 unsubscribe；
- notification buffer overflow 显式关闭 flow，而不是悄悄吞掉当前 payload。

BleHub Android backend 应借鉴“先取得 correlation/cleanup ownership，再成为 accepted；失败原子 rollback”的结构，但 queue/drop policy仍由 downstream，而不是 backend 或 core决定。

## 9. Linux backend

### 9.1 backend-owned polling runtime

**Verified：** `BackendBluez` singleton 创建 polling thread，每 100 微秒调用一次 `bluez.run_async()`：

- `simpleble/src/backends/linux/BackendBluez.cpp:13-31,72-76`
- `simpleble/src/backends/linux/BackendBluez.h:19-39`

这让 SimpleBLE 可以提供同步 C++ API，但与 BleHub core 无 runtime 的约束不同。

BleHub Linux backend 后续仍需要某个 D-Bus dispatch execution position，但应由 backend integration/caller 提供或按 Plan 中明确方式接入，不应把 process-global polling thread变成 core contract。

### 9.2 wrapper uniqueness

`BackendBluez::adapters()` 以 BlueZ object path 缓存 `AdapterLinux`，源码明确说明：围绕同一个 adapter 创建第二个 wrapper，销毁其中一个时会清掉另一个 wrapper 使用的 callback：`simpleble/src/backends/linux/BackendBluez.cpp:34-51`。

这是可直接提升为 BleHub backend implementation invariant 的经验：

> 同一 native identity 在一个 backend ownership domain 内只创建一个 owning wrapper；对外发 opaque ID 或 lightweight record，不创建会竞争 native callback registration 的重复 owner。

### 9.3 connection completion

Linux connect 同时等待 BlueZ `Connected` 和 `ServicesResolved`，并有 backend-owned timeout/retry。`ServicesResolved` 使用本地 delivered property，而不是强制 refresh，因为 property 与 GATT object tree 到达存在时序差。

可借鉴：

- connection event 应区分 transport connected 和 GATT usable evidence；
- mapping test 必须覆盖 property 与 object tree 的先后顺序；
- backend-private correlation state 可以保留 native phase，但不能把它变成 core canonical state。

不应借鉴：

- library 自行固定 connect retry 次数；
- core timeout；
- callback thread 上执行 blocking D-Bus method。

### 9.4 notify/indicate 与 Battery1 policy

SimpleBLE Linux：

- notify 和 indicate 都调用 BlueZ `StartNotify()`；
- Battery1 interface 可被合成为 GATT Battery Service/read/notification；
- unsubscribe 最多轮询 5 秒等待 `Notifying=false`；
- cleanup 先清 callback，再 best-effort stop notifications。

`_cleanup_characteristics()` 的顺序值得借鉴：`simpleble/src/backends/linux/PeripheralLinux.cpp:343-388`。

但 Battery1 合成是上层 convenience policy，不应默认进入 BleHub thin contract。若未来需要，应作为显式 capability/policy 独立评审。

### 9.5 D-Bus timeout 后取消 pending call

SimpleDBus method call 使用 pending callback + condition variable；timeout 时先 cancel pending call，再销毁 stack-local completion context。

这一原则可泛化到 BleHub：

- timeout/cancel 后不能只忘掉 caller state；
- backend 必须使 native correlation record失效或 generation 前进；
- late callback 只能匹配旧 record并被安全消费/丢弃，不能完成下一条 command。

## 10. 跨语言包装与发布经验

### 10.1 `simplecble`：opaque handle 简单，但 ownership/error 太弱

C API 使用 `void*` adapter/peripheral handle，status 只有 success/failure：

- `simplecble/include/simplecble/types.h:11-15,62-63`。

值得借鉴：

- library allocation 必须由 matching free 释放；
- C example 应展示每个 handle/buffer 的 owner 和释放点。

应避免：

- untyped `void*`；
- callback-created handle ownership不清；
- callback无法建立统一 unset/drain boundary；
- 所有 exception 压成一个 failure；
- fixed buffer 截断但仍报告 original length。

当前 `simpleble_service_t` 和 manufacturer data 只有 27-byte array；实现只复制 `min(size, 27)`，却把 `data_length` 设成原始 size：

- `simplecble/include/simplecble/types.h:36-60`；
- `simplecble/src/peripheral.cpp:239-245,315-319`；
- `simplecble/test/src/test_buffer_overflow.cpp:65-87` 明确把“复制 27 bytes、报告原始 50 bytes”固定为预期截断语义。

该实现不会写坏 struct 本身，但 `data_length` 表示原始长度而不是 array 中的有效长度。若 consumer 按通常的“有效字节数”含义直接遍历到 `data_length`，就会越过 fixed array；ABI 也没有独立的 `copied_length`/`required_length` 字段消除歧义。BleHub 应坚持 BoltFFI owned buffer/span shape 和 size guards，不引入类似 fixed payload ABI。

### 10.2 `simplepyble`：GIL 和 payload ownership 处理较好

值得借鉴：

- blocking C++ call 前释放 GIL；
- native callback 进入 Python 前重新获得 GIL；
- byte payload 转成 Python-owned `bytes`；
- `keep_alive` 表达 wrapper lifetime；
- callback exception 进入日志而不越过 native boundary。

但 optional Python async layer拥有默认 executor、全局 cleanup 和 runtime policy，这适合 convenience binding，不适合 BleHub core。

### 10.3 `simplersble`：generated transport 不等于 safe object graph

Rust binding 使用 `cxx::bridge` 生成部分 transport，但 callback/lifecycle wrapper仍手写。C++ wrapper 保存指向 Rust object 的 raw back-pointer：

- `simplersble/src/bindings/Bindings.hpp:58-66,111-118`。

Rust 侧又提供 blanket `unsafe impl Send + Sync`，binding 内建 Tokio/broadcast queue 和固定 buffer policy。

**Inferred：** 当前源码不足以形成完整同步证明，不能确认 unlink/drop 与 concurrent callback 永远安全。

对 BleHub 的启示：BoltFFI 生成成功只证明 wire/glue shape，不证明长期对象图、callback-after-close、foreign exception 和 thread race；Phase 0 runtime/clean consumer gate 必须保留。

### 10.4 Java/Android packaging 中值得借鉴的检查

SimpleJavaBLE/SimpleDroidBLE 的构建配置会检查：

- JAR/AAR 中 native entry；
- ABI 目录；
- Maven metadata/POM；
- module metadata；
- Android example 通过 Maven artifact dependency消费，而不是直接 `project(...)`；
- Android emulator 使用 Plain backend做 deterministic integration。

这些适合加入 BleHub package recipes：

- exact native library list；
- expected ABI list；
- generated binding/runtime revision manifest；
- forbidden duplicate symbol/library；
- clean artifact consumer；
- emulator fake backend与真机 HIL明确分层。

## 11. SimpleBLE 与 BleHub 的优劣对比

| 维度 | SimpleBLE | BleHub 当前设计 | 评估 |
|---|---|---|---|
| 统一层 | C++ public objects + virtual backends | Rust DTO/command/event + `BleBackend` | BleHub 更适合 FFI-first 和显式语义 |
| 平台实现语言 | 主要 C++，Apple ObjC++，Android JNI/Java | Windows/Linux Rust，Android Kotlin，Apple Swift | SimpleBLE 语言更统一；BleHub 更贴近平台官方 API |
| API 易用性 | 阻塞方法 + callback，入门简单 | fire-and-forget command + event，下游需 tracker | SimpleBLE 小 demo 更简单；BleHub 大型/异步应用更可控 |
| 状态 ownership | shared mutable Adapter/Peripheral + backend cache | caller 业务状态，backend native handle，core 无 canonical BLE state | BleHub ownership 更明确 |
| identity | address/Apple UUID string，wrapper cache | typed caller IDs + backend opaque handles | BleHub 更能处理 scope、stale 和 duplicate instance |
| GATT identity | UUID pair/first match | backend-issued service/characteristic/descriptor IDs | BleHub 明显更强 |
| completion | 同步等待，但各平台强度不同 | dispatch acknowledgement 与 operation event分离 | BleHub 更诚实地表达平台差异 |
| timeout/retry | library/backend 内建 | caller policy；backend只做平台必要 serialization | BleHub 更灵活，SimpleBLE 更方便但更隐式 |
| callback thread | backend-specific，公共 contract较弱 | sink `Send + Sync`，线程由 backend callback来源决定，下游快速转发 | BleHub 约束更明确，仍须 runtime验证 |
| error | C++ exception/message；safe layer会丢失细节 | typed dispatch/event/platform details | BleHub 更适合跨语言与诊断 |
| runtime | Windows/Linux/Android/Rust binding拥有线程/queue/runtime | core 不拥有 runtime | BleHub 更适合 Tauri/actor/runtime下游 |
| lifecycle | shared_ptr/cache/destructor + backend-specific cleanup | explicit idempotent close/drain/token unregister | BleHub 设计更严格，但实现验证成本更高 |
| 包装层 | 多套手写 binding，能力丰富 | BoltFFI generated Kotlin/Swift，范围更窄 | SimpleBLE 生态更广；BleHub drift surface更小 |
| package验证 | Android/Java较好，其他surface不一致 | Plan要求每平台 staged artifact clean consumer | BleHub gate更一致，但仍需实现 |
| 成熟度 | 已有多平台产品源码和bindings | Phase 0进行中 | SimpleBLE 经验丰富；BleHub 尚未证明生产 backend |

### 11.1 SimpleBLE 的主要优势

1. 对 C++/Python/Java/Kotlin caller 来说 API 直观；
2. 已实现多个平台和多个 language surface；
3. backend source 包含大量真实平台 workaround；
4. public object façade 小，native type基本不泄漏；
5. connect 返回后 GATT 通常已可用，适合简单程序；
6. Plain backend和 Android artifact检查可用于快速集成测试；
7. platform-specific cleanup经验值得参考。

### 11.2 SimpleBLE 的主要代价

1. blocking façade把 native async 差异藏进 condition variable、retry、timeout和worker thread；
2. shared mutable public object 混合 identity、observation、connection、GATT 和 callback ownership；
3. callback slot、thread与重入contract不够明确；
4. UUID-only GATT addressing丢失duplicate instance；
5. address-as-identity在Apple和private address环境不成立；
6. global config可改变同一method的completion semantics；
7. 多套手写binding产生不同error、runtime和lifecycle策略；
8. process-lifetime cache与raw back-pointer增加释放风险；
9. 平台行为被统一method name掩盖，但没有统一guarantee；
10. core/platform生命周期测试覆盖弱于实现复杂度。

### 11.3 BleHub 的主要优势

1. command dispatch 和 eventual outcome明确分层；
2. caller-owned IDs 可关联late/duplicate/stale event；
3. core 无 runtime，可自然接入 Tauri、channel、actor 或 UI loop；
4. backend 保留 native handle，公共边界不泄漏对象；
5. duplicate GATT instance可用opaque ID表示；
6. typed error可以同时保留stable category和platform detail；
7. explicit close/drain 和 token lifetime 可被系统性测试；
8. platform-native Kotlin/Swift/Rust实现更贴近官方 API 和 ownership model；
9. clean consumer、FFI spike、conformance和HIL gate已经分层。

### 11.4 BleHub 的主要成本

1. caller 必须实现 command tracker、timeout、stale/duplicate处理；
2. API 比阻塞 `connect()/read()` 更复杂；
3. Kotlin/Swift/Rust 三种 backend implementation 会有重复 glue；
4. BoltFFI长期对象图和package组合需要高成本验证；
5. backend必须自己实现平台必要的correlation和serialized GATT queue；
6. 当前仍处于 Phase 0，很多优势只是已批准设计，尚未获得四平台生产证据。

因此 BleHub 不应声称“全面优于 SimpleBLE”。更准确的结论是：

> SimpleBLE 优先优化简单、同步、对象化的多语言使用体验；BleHub 优先优化跨平台语义强度、FFI ownership、调用方 runtime自由和可验证的 late-event lifecycle。两者目标不同。

## 12. BleHub 应直接借鉴的模式

### 12.1 backend identity record 与 scan observation 分离

每个平台 backend 可以维护：

- persistent native identity/handle record；
- session-local observation tracking；
- caller-visible opaque `PeripheralId`；
- scan session correlation。

但这些 map 只属于 backend，不升级为 BleHub core canonical device inventory。

### 12.2 一个 native identity 只有一个 owning wrapper

尤其在 BlueZ、CoreBluetooth delegate和Android callback object场景，重复 owner会竞争callback registration或清理。

建议写入平台实现不变量：

- 同一 backend/adapter/native identity只建立一个 owning record；
- public事件携带opaque ID和snapshot；
- wrapper重建必须generation前进；
- stale native callback不能命中新generation command。

### 12.3 callback payload 先拷贝，再跨线程/FFI

Android JNI byte array、CoreBluetooth `NSData` 和 WinRT buffer都应在native callback有效期内转为owned payload，再离开callback frame或进入Rust ingress。

### 12.4 accepted transaction 的 failure-atomic rollback

借鉴 SimpleDroidBLE subscription owner：

1. 建立correlation和cleanup owner；
2. 尝试提交native operation；
3. 提交失败则完整rollback并同步reject；
4. 一旦accepted，后续结果只走event；
5. unsubscribe/disconnect/dispose在成功和失败路径都清owner。

这与 BleHub Plan 的 accepted linearization point一致。

### 12.5 先切断 callback delivery，再做 fallible native teardown

借鉴 Linux `_cleanup_characteristics()`：

- 先关闭ingress/callback；
- 使pending correlation失效；
- 再best-effort stop notification；
- 再close child GATT handles；
- 最后释放parent connection/device；
- teardown exception只做diagnostic，不恢复delivery。

### 12.6 平台 package 内容断言

Android/Apple release recipe应检查：

- expected ABI/native library；
- generated binding与runtime revision；
- artifact manifest和contract version；
- POM/Swift Package dependency；
- duplicate/missing native entry；
- clean consumer不依赖source tree；
- callback/close路径实际运行，不只解压看文件名。

### 12.7 deterministic fake 要模拟真实 identity 与时序

SimpleBLE Plain backend适合快速测试，但未完整模拟production cache/async/lifecycle。

BleHub FakeBackend应继续覆盖：

- 同native identity跨多次scan；
- scan session切换；
- event-before-command-return；
- late/duplicate/out-of-order completion；
- timeout后late callback；
- close与callback race；
- notification owner rollback；
- per-connection GATT serialization；
- sink rejection和queue overflow策略边界。

## 13. BleHub 不应借鉴的模式

以下模式与 BleHub目标不兼容：

1. 统一 C++ backend language 作为目标本身；
2. public shared mutable `Peripheral` canonical object；
3. address/hash/raw pointer作为稳定public identity；
4. UUID-only GATT operation；
5. blocking `scan_for`、connect、read/write/subscribe；
6. core/backend替caller固定timeout和retry policy；
7. process-global backend singleton、worker、poll loop或cache；
8. global mutable config改变completion semantics；
9. 一个callback slot挂在共享public object上；
10. callback thread和reentrancy不进入contract；
11. `services()`同时表示advertisement和discovered GATT；
12. read在notifying时偷换成“等待下一条notification”；
13. untyped `underlying(): void*`；
14. safe API把所有error压成false/null；
15. fixed payload buffer截断但报告原长度；
16. generated binding存在就假设lifecycle安全；
17. emulator/plain backend通过就声称真实BLE通过。

## 14. 对 BleHub 各 Phase 的具体建议

### Phase 0 / Phase 1

新增或确认以下mechanism/conformance cases：

- callback payload ownership在foreign frame结束后仍有效；
- close先切断delivery，再dispose backend；
- late callback不能命中新token/new generation；
- accepted transaction registration failure完整rollback；
- sink panic/foreign exception不越过ABI；
- duplicate GATT instance fixture不按UUID合并；
- same native identity在重复scan中的opaque ID策略；
- generated artifact内容、symbol和contract version检查。

### Phase 2 Windows

参考 SimpleBLE 但重新按BleHub语义实现：

- 明确WinRT apartment/execution context；
- retain必要的service/characteristic/descriptor objects；
- revoke watcher/device/notification tokens；
- teardown按child-to-parent顺序；
- mapping test区分logical disconnect与native confirmation；
- 验证`RSSI == -127` synthetic observation，而非直接照搬过滤；
- 不内建三次connect retry和global deferred flag。

### Phase 4 Android

必须重点避免SimpleBLE当前风险：

- 一个connection一条serialized GATT operation queue；
- correlation包含command ID、operation kind、target instance ID和generation；
- timeout/late callback不得复用boolean pending flag；
- scan failure必须上行；
- JNI/Kotlin payload必须owned；
- subscription owner在所有失败路径rollback；
- backend和wrapper explicit idempotent dispose；
- 不使用hash-derived handle或process-lifetime `NativeCache`；
- AAR contents + artifact-only consumer + emulator fake + real-device HIL分层。

### Phase 3 Apple

- 使用CoreBluetooth identifier作为backend identity evidence，不暴露为MAC；
- delegate queue policy在factory固定并测试；
- command method不等待delegate completion；
- service/characteristic/descriptor使用native instance-backed opaque ID；
- 不以UUID字典保存operation task；
- read与notification严格分离；
- close/deinit时先detach delegate/ingress，再release ARC object；
- 从delegate queue同步调用command的reentrancy case必须测试。

### Phase 6 Linux

- 明确D-Bus driver/poll ownership，不让core创建polling thread；
- 一个BlueZ object path只对应一个owning wrapper；
- `Connected`与`ServicesResolved`分别映射，不把property和object-tree到达假定为原子；
- pending D-Bus call timeout后先cancel/invalidate correlation；
- callback-first cleanup；
- notify/indicate无法区分时通过capability/平台语义表达；
- Battery1 synthesis不默认进入thin contract。

## 15. 对当前架构计划的影响

本次源码审计**不支持推翻当前 Plan**，也没有发现必须修改公共 ABI 或平台实现语言的证据。SimpleBLE 反而加强了以下既有决策：

- 统一contract，不强求统一backend语言；
- command dispatch acknowledgement与operation outcome分离；
- caller-owned timeout/retry/stale policy；
- core不拥有runtime/worker/queue；
- backend拥有native handle和平台必要serialization；
- Android per-connection GATT queue；
- Apple/Android使用平台原生语言；
- opaque GATT instance IDs；
- typed platform errors；
- explicit close/drain/late callback；
- artifact-only clean consumers与真实HIL。

经用户评审，本报告中的以下实现经验已提升为[`.plan/PLAN.md`](../../.plan/PLAN.md)规范：

- SimpleBLE只作为选择性平台细节/失败模式参考，不成为统一C++架构基线；
- 同一backend ownership domain内一个native identity只有一个owning record，scan observation与persistent native record分离；
- accepted前失败必须failure-atomic rollback，late callback不得命中新operation；
- callback payload在离开native frame、跨线程或跨FFI前转成owned DTO；
- cleanup先切断callback/correlation，再按child-to-parent释放native ownership；
- package同时执行native entry/ABI/revision/manifest内容断言和artifact-only consumer运行验证；
- Windows、Android、Apple与Linux各Phase加入对应mapping、correlation、teardown和packaging门槛。

这些更新不引入新的public event，不改变Windows/Linux Rust、Android Kotlin、Apple Swift的方向，也不把backend-private generation升级为caller可见状态。未来若要增加connection phase/public event、改变公共identity scope或更换平台实现语言，仍须单独Review Plan。

## 16. 最终推荐

### 16.1 采用

- backend-private persistent native handle record；
- session-local scan observation tracking；
- one native identity / one owning wrapper；
- immutable metadata DTO + connection-rooted operation；
- explicit write request/command distinction；
- callback payload ownership conversion；
- failure-atomic subscription/correlation rollback；
- callback-first teardown；
- package content assertions；
- deterministic fake + artifact consumer + HIL分层。

### 16.2 拒绝

- SimpleBLE式阻塞统一API；
- public shared mutable Peripheral；
- UUID-only GATT；
- address/hash/raw pointer identity；
- global singleton/runtime/config；
- binding-owned fixedqueue/drop策略；
- process-lifetime native cache；
- untyped或message-only error；
- source-tree example代替clean consumer；
- mock/emulator代替真实BLE验收。

### 16.3 一句话结论

> SimpleBLE 值得作为平台 backend 细节和失败模式的参考实现，但不适合作为 BleHub 的架构基线；BleHub 应借鉴它的 native handle、cleanup、subscription 和 packaging 经验，同时保留当前更薄、更事件化、更显式的跨平台 contract。
