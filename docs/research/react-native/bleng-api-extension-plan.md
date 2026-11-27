# BLeng RN API 扩展与平台能力设计

Date: 2026-11-25

Status: 设计记录，不是 ABI 冻结、不是平台能力已实现。本文说明本轮已落地的 API 变化，以及仍需要平台后端和真机证据的扩展。

## 本轮已落地

| 变化 | 设计理由 | 证据 |
|---|---|---|
| `BlengPeripheral.key()` | 同一次 owner 内跨广告稳定，只作为去重 map key；不是平台 device id，不能寻址原生状态 | Rust FFI `SupervisedPeripheral::key` + Nitro spec + 双端映射；`ffi_scan` 断言同一广告的 `key` 相等 |
| `overflow` lane 终态 | 慢消费者只结束自己的观察 lane，不再把整个 runtime 当作故障，也不静默丢事件 | `ForeignScanEndReason::Overflow` / `ForeignNotificationEndReason::Overflow`；`ffi_scan` 覆盖 scan/notification |
| `platform.status` / `platform.attCode` | `code` 仍是唯一控制流字段；数值字段补 Android GATT status 与 Apple `CBError` / ATT code | `PlatformErrorDetails` + Apple/Android 映射 + Nitro spec |
| `BlengProvider` / `getBlengCentral()` / `closeBlengCentral()` | 一个 JS runtime 一个 central；Fast Refresh 保留 central 和 capability；完整 reload 必须重连 | `src/central.ts`、`src/provider.ts`；consumer Jest 覆盖 provider |
| `shareLane()` | 原生 lane 保持单 pull、单背压预算；多消费者在 JS 层显式 fan-out，不复制原生队列 | `src/share.ts`；consumer Jest 覆盖 fan-out、timeout 过滤、终态重放、stop |
| `connection.beginQueryMtu()` / `beginRequestMtu(mtu)` | 查询和请求共用一条有界命令与一个 `BlengMtu` 句柄；结果永远是平台快照，不是请求值回显 | `MtuCommand` / `MtuInfo` / `BleEvent::MtuRead`；Android `requestMtu`+`onMtuChanged`；Apple 只查询、请求返回 `not_supported` |
| `connection.beginSetConnectionPriority(priority)` | 一条只有终态回执的命令：平台不回报告生效模式，就不发明一个 | `SetConnectionPriorityCommand`；Android `requestConnectionPriority`；Apple 返回 `not_supported` |

## 为什么 `key` 不是 device id

`key` 来自 owner 内部的 `PeripheralId`，只在当前 owner 内有意义。它：

- 不包含 MAC、UUID、地址或平台句柄；
- 不能用于寻址连接、GATT 或取消；
- 不能跨 owner 比较；
- 只解决“同一台设备的多条广告如何归并”的问题。

这样既修复了扫描列表无法去重的产品问题，又保留了 capability 不可跨 owner/引擎寻址的安全边界。

## Fast Refresh 设计

推荐结构：

```text
globalThis.__blehubBlengCentral
        ▲
        │ getBlengCentral()
BlengProvider ── useBlengCentral() ── screen
```

- owner 槽位放在 `globalThis`，所以 Fast Refresh 重新求值模块不会新建引擎，也不会丢 capability。
- `BlengProvider` 的挂载/卸载是惰性的：不 `initialize()`、不 `close()`。否则导航和 Fast Refresh 会拆掉活着的 BLE 工作。
- 完整 reload / 进程重启是新的 JS runtime；RN 会 invalidate 旧引擎，owner/capability 按设计消失。App 必须重连，不能试图“恢复”旧 capability。
- `closeBlengCentral()` 是显式收尾，只在 App 真正结束 BLE 会话时调用。

## MTU 与最大写长度（已落地）

实际形状与拟议不同：没有单独的 `maximumWriteLength()`，而是把“查询”也当成一条有界命令。

```ts
connection.beginQueryMtu(): BlengMtuSubmission
connection.beginRequestMtu(mtu: number): BlengMtuSubmission
// 结果：{ info: { mtu?, maximumWriteWithResponse?, maximumWriteWithoutResponse? } } | { failure }
```

- 一条规范命令 `MtuCommand { command_id, connection_id, request: Option<u16> }`：`None` 只查询，`Some(mtu)` 才请求协商。
- 范围 `MIN_MTU = 23 … MAX_MTU = 517`，在 BleHub 层校验，越界是 `invalid_request`，不会到达 backend。
- 回执是 `BleEvent::MtuRead` + `CommandCompleted` 两条证据；`MtuInfo` 的三个字段都可缺失，“未知”不是 0。
- Android：`BluetoothGatt.requestMtu` + `onMtuChanged`，查询读本地跟踪值，回执写 `mtu` 与 `max_write = mtu - 3`（最小 20）。
- Apple：CoreBluetooth 自动协商，`mtu` 恒为 `nil`，只报 `maximumWriteValueLength(for:)`；`beginRequestMtu` 抛 `CommandDispatchError::NotSupported`，映射为 `bleng.dispatch_rejected.not_supported`。
- 仍缺真机 RF 证据：两端都只有编译/合成验证，不能据此声称协商成功。

## 连接优先级（已落地）

```ts
connection.beginSetConnectionPriority(priority: 'balanced' | 'high' | 'lowPower'): BlengPrioritySubmission
```

- 没有域事件：`CommandCompleted` 就是全部回执，成功只意味着平台接受了请求。
- Android：`BluetoothGatt.requestConnectionPriority`，bool 返回值就是回执；没有回调。
- Apple：CoreBluetooth 无等价 API，直接 `NotSupported` → `bleng.dispatch_rejected.not_supported`。
- 仍缺真机证据：没有在 Android 真机上验证调度真的变化。

## descriptor 读写（已落地）

实际形状：`descriptor` 成为能力对象（HybridObject），把原生句柄留在 bridge 里。

```ts
characteristic.descriptors(): BlengGattDescriptor[]   // { uuid(), beginRead(), beginWrite(value) }
descriptor.beginRead(): BlengDescriptorReadSubmission  // 结果：{ value: ArrayBuffer } | { failure }
descriptor.beginWrite(value: ArrayBuffer): BlengDescriptorWriteSubmission  // 恒为 with-response
```

- descriptor 只能通过发现它的 characteristic 访问；`SupervisedGattDescriptor` 持有原生句柄，characteristic 失效则 descriptor 一并失效。
- `beginWrite(value)` 没有 write-type 参数：descriptor 在链路上没有写类型，永远是 with-response。
- 与 characteristic 读写共用同一套有界 operation 契约（reservation、wait、cancel、单消费者回执），但没有把 descriptor 伪装成 characteristic。
- Apple：`GattIoController` 新增 `descriptorRead`/`descriptorWrite` 两类 callback，`CallbackKey.descriptorIdentity` 做相关性；`didReadDescriptor`/`didWriteDescriptor` 消费回执，retire 后回调被隔离。
- Android：`PendingIo.DescriptorRead/DescriptorWrite`；`onDescriptorWrite` 先判通用 descriptor 且 `descriptor ===` 命中，再落回 CCCD 通知路径，避免吞掉 CCCD 回执。
- 仍缺真机证据：两端都只有编译/合成测试，不能据此声称 descriptor I/O 已在真机成立。

## 绑定 bonding（已落地）

实际形状否决了“peripheral.beginBond + connection.awaitBondEnd”这种拆两处的草案：bond 是连接上的有界
operation，回执就是平台报告的终态。

```ts
connection.beginBond(): BlengBondSubmission  // { bond } | { failure }
connection.beginRemoveBond(): BlengBondSubmission  // 反方向；成功是 'none'
bond.wait(ms): { state: 'none' | 'bonding' | 'bonded' } | { failure }
```

- `bonding` 是进度而非终态；成功指平台报告目标终态（create=`bonded`，remove=`none`），不承诺链路已加密或重连免提示。
- 两个方向共享一个 `BondOperation`（与 `MtuOperation` 同时服务 query/request 一致），由方向决定哪个状态算成功。
- Android：`BluetoothDevice.createBond()` 发起，`ACTION_BOND_STATE_CHANGED` 广播回报；已绑定则立即回
  `bonded`；失败（`BOND_NONE`）以带错误的 `CommandCompleted` 终止，不伪造 `BondStateChanged` 域事件。
- Apple：CoreBluetooth 没有公开的发起绑定 API，返回 `CommandDispatchError::NotSupported` →
  `bleng.dispatch_rejected.not_supported`，不用系统托管的受保护访问配对冒充。
- 取消记录请求而非回滚；对端仍可能完成配对。

## 仍延迟的平台能力

这些不是 API 命名问题，而是平台后端能力问题；在没有真机证据前不应提供“看起来跨平台、实际只在一端工作”的接口。

### 连接优先级

已落地，见上方。

### 扫描选项（部分已落地）

已落地的跨平台过滤（由 Rust 固定谓词在两个平台统一应用）：

```ts
interface BlengScanOptions {
  serviceUuids: string[]
  namePrefix?: string
  exactName?: string
  companyId?: number
  minRssi?: number
  allowDuplicates: boolean
}
```

- `exactName` 与 `minRssi` 是本轮新增：`minRssi` 在缺失 RSSI 时不匹配，避免“无 RSSI 猜测通过”。
- 仍在待办（需要明确标注平台）：`scanMode` / `phy` / `legacy` / `reportDelay` 是 Android 原生 tuning，
  Apple 无等价能力；`manufacturerData` 数据/掩码过滤也还没加。它们不能用“静默忽略”伪装成跨平台。

### raw advertisement / selective discovery

- iOS CoreBluetooth 不提供完整原始广播包；Android `ScanRecord.bytes` 有。跨平台 `raw?: ArrayBuffer` 会制造“iOS 永远是 undefined”的假统一。
- Apple `discoverServices([CBUUID]?)` 支持服务过滤；Android `discoverServices()` 不支持同样的选择性发现。两者都不适合现在伪装成统一 API。
- 若产品确实需要，先定义平台能力矩阵和明确的 `unsupported`，再做平台专用接口。

### 日志级别

当前没有稳定日志门面。加 `setLogLevel` 需要先定义日志归属、隐私边界和 native 输出通道；不应在 RN helper 里做一个只改 JS 变量的假开关。

## 冻结前建议

1. 确认 `BlengPlatformCapabilities` 是否要进入 ABI（bonding、descriptor、restore）。
2. 若进入，先定义 capability 查询和 `unsupported` 的稳定 `code`，再逐个平台实现。
3. 每个平台能力都必须有真机证据；类型编译和合成测试不能替代 RF 行为。
4. 在实现前，API 文档继续明确列出“未暴露”的能力，避免使用者按竞品经验假设它们存在。

MTU 不走 capability 快照：平台间差异（Android 协商、Apple 只读）用已有的 `bleng.dispatch_rejected.not_supported`
表达，调用方不必先查能力表。
