# adapter 状态流：从 Rust lane 到 JS，以及 Android 端暴露的平台缺口

这份报告记录一件事：把"蓝牙状态变化"做成一个可观察的产品面，从 Rust 一直打通到 JS，
以及**在真机上因此发现的一个既有平台缺口**。ABI 仍未冻结。

## 1. 设计（为什么是 lane，而不是一个 getter）

`start()` 报的是初始化那一刻的快照；之后的状态变化此前没有任何产品面。新面按仓库既有的
两种形状做成 **lane**：

| 项 | 决定 |
|---|---|
| 入口 | `owner.beginObserveAdapter()` → `{ adapterObservation } \| { failure }`（或拒绝） |
| 控制 | 只有 `nextEvent(timeoutMs)`：`{ snapshot } \| { ended } \| { failure }` |
| 形状 | coexisting lane（同 owner 可多个观察者，各自收到注册之后的每一条） |
| 有界 | 64 条 FIFO；溢出 **fail closed**，不静默丢状态 |
| 端 | `ended.reason` = `ownerClosed`（引擎退休）或 `detached`（本端点停止）；端点可重读，不是合成失败 |
| 无重放 | 不向晚注册的观察者重放"最新状态"——要看状态请 `start()` |
| 无副作用 | 观察不触发恢复、不改收音机、不取消任何操作 |

Rust 侧：

- `crates/bleng-supervisor/src/adapter_delivery.rs`：lane 本体（顺序、单挂起 pull、溢出、
  首次失败优先、退休/泄漏）。
- `crates/bleng-supervisor/src/observation.rs`：接进 owner 的**唯一**事件读取器（fan-out +
  撤销路径），没有第二个 reader。
- `crates/bleng-supervisor/src/ffi/adapter_view.rs`：UniFFI 视图 + `Supervisor::begin_observe_adapter_view`。
- `crates/bleng/src/facade/mod.rs`：`Bleng::observe_with_timeout`——把有界等待放到 owner 自己的
  runtime 上。**这一条是真机暴露的**：最初视图里直接用 `tokio::time::timeout`，设备上立即
  返回 `bridgeFailure`（0.199s），因为外部调用方的 runtime 没有计时器。

Nitro / 原生 / JS：

- `specs/BlengAdapterObservation.nitro.ts` + `BlengSessionOwner.beginObserveAdapter()`。
- iOS：`HybridBlengAdapterObservation.swift`、`BlengAdapterMapping.swift`；`scanGate` 改名
  `sessionGate`（它是所有 lane 的提交闸门，不是扫描专用）。
- Android：`HybridBlengAdapterObservation.kt`、`BlengAdapterMapping.kt`、
  `AndroidRustSupervisorOwner.beginObserveAdapterView`（该 wrapper 一方法一转发）。
- 文档：`API.zh.md` / `API.md` 各新增一节，并删掉"没有 adapter 状态流"的旧结论。

## 2. 门禁（全部实跑）

| 检查 | 结果 |
|---|---|
| `cargo test -p bleng-supervisor` | 36 + 22 + 8 + 29 全过（含 6 条 lane storage + 2 条航线/注册） |
| `cargo clippy -p bleng -p bleng-supervisor --all-targets --features ffi -- -D warnings` | 干净 |
| `pixi run just binding snapshot-check` | `verified 3` |
| `pixi run just binding bleng-snapshot-check` | `verified 6`（本轮按评审流程 `snapshot-update` 过一次：新增 adapter 面 + `Bleng::observe_with_timeout`） |
| `tools/test_api_snapshots.py` | 36 passed / 1193 subtests（负例矩阵覆盖新类型、新方法、新错误） |
| `tools/tests/test_freeze_inventory.py` | 4 passed（Nitro 面从 6 份 spec 增至 7，计数显式更新） |
| `tools/tests/react_native/test_api_docs.py` | 两份语言镜像都覆盖新能力 |
| `pixi run just react-native formal-host-check` | 类型 + 自动链接通过 |
| `pixi run just react-native formal-host-test` | 136 passed |
| `pixi run just react-native formal-host-android-build debug` | 通过（含新 Kotlin） |
| `pixi run just fmt` / `lint` / `docs-check-mermaid` | 全绿 |

## 3. 真机：发现的是平台缺口，不是 lane 的缺陷

本轮候选：App APK `99854c1a51a5a39c383ab46d047a8bfc22035e11b02d17c4c41849e5e4cca32f`、
test APK `0ab5572c401a9a5f0d1ea2f8b795f7b3a9e09bd3a33b56129a0dc746f03fe289`（含新 Kotlin）。

Android 的 adapter-transition lane 增加了第三阶段 `adapter-stream-on`：在主机把收音机重新打开
**之前**挂起一次 pull，主机打开后该 pull 必须拿到 `poweredOn` 快照。

第一次运行：pull 立刻以 `bridgeFailure` 结束（0.199s）→ 已定位为"外部 runtime 无计时器"，
改为走 owner runtime（§1 的 `observe_with_timeout`）。

第二次运行：pull 变成真的在等，但没有任何快照到达；场景在 `adapter-off` 之后停住，最终以
`instrumentation exceeded the run budget` 结束（`host_gate=failed`）。fixture 与手机两侧清理都是
干净的（`fixture_close.resources_closed=true`、`advertising_inactive=true`、`cleanup_errors=[]`），
所以这是"没有事件可交付"，不是泄漏。

这轮还留下一个**尚未解释**的点：测试自己的 `awaitSettlement` 上限是 60 s，本应在 60 s 后以断言
失败收场，却一直跑到主机预算耗尽。下一个修这条 lane 的人应当先确认这一点（是 promise 未结算导致
等待路径没有走到超时分支，还是 instrumentation 进程在测试结束后没有退出），再动平台后端。

根因在平台后端，不在 lane：

- iOS 后端每次 `centralManagerDidUpdateState` 都发布
  `adapterSnapshotChanged(commandId: nil, …)`
  （`platforms/apple/Sources/BleHubApple/AppleBleBackend.swift:1265` 起），所以 lane 在 iOS 上有东西可收。
- Android 后端**只在 `initializeOnHandler` 里**发布 `BleEvent.AdapterSnapshotChanged`
  （`platforms/android/library/src/main/kotlin/dev/blehub/android/AndroidBleBackend.kt:1582`）；
  没有 `ACTION_STATE_CHANGED` 之类的被动发布路径。

也就是说：Android 侧"收音机变化可通过事件流观察"这项契约，此前从未在该后端实现过；
lane 只是第一个把它照出来的消费者。

## 3b. 修复：Android 后端的被动发布

`platforms/android/library/.../AndroidBleBackend.kt`：

- 参考 READY 之后注册 `BluetoothAdapter.ACTION_STATE_CHANGED`（API 33+ 用
  `RECEIVER_NOT_EXPORTED`），并在 `dispose()` 里、清空 adapter 句柄之前注销。
- 广播回到后端自己的 Looper，再由 `publishAdapterState()` 发
  `AdapterSnapshotChanged(commandId = null, snapshot = …)`：没有命令拥有这次变化，所以没有相关 id。
- **只发布落定状态（`STATE_ON` / `STATE_OFF`）**。第一次真机运行收到了 `TURNING_ON` 期间的快照，
  而 DTO 只有四个落定状态——把过渡态报成 `POWERED_OFF` 是假话，下一次广播还得收回它。跳过过渡态
  是这个 DTO 下唯一诚实的投影（iOS 侧同样只在 `centralManagerDidUpdateState` 给出落定状态时投影）。
- 注册是尽力而为：OS 拒绝注册不会让一次本来有效的会话失败，但会在日志里说明"本会话没有被动
  发布"，消费者等它会超时。
- `snapshotRfResources()` 增加 `adapterReceivers`，并加入
  `AndroidPlatformSession.REQUIRED_ZERO_RESOURCE_NAMES`：这个 receiver 也要在退休后归零。

## 3c. 修复：把"卡住"变成一个可读的失败

`tools/react_native/bleng_android_instrumentation.py` 的 `_drive` 此前只在进程**退出后**才读它的
stdout，所以上一轮的卡死只留下主机自己路由过的 stage，看不到测试的任何输出。现在：

- 进程控制台按行流式写进日志（测试失败立刻可见）；
- 新增 `STAGE_STALL_SECONDS = 180`：场景每个产品步骤都会发 stage，180 秒没有任何新 stage 就是
  挂住，直接以 `no stage progress for Ns; observed stages=[...]` 失败，而不是耗尽整个 run budget。

这条改动立刻起了作用：修复后的第一次失败在 1 分钟内就以 `FAILURES!!!` 和完整堆栈出现在日志里，
并且直接指出了"收到了 `POWEREDOFF` 而不是 `POWEREDON`"。

## 3d. 真机结果（同一候选，DK-A）

```text
formal_android_native_adapter_transition=passed … radio=restored
stages=adapter-off,adapter-stream-on,adapter-restored,scan-submitted,scan-started,
       advertisement,scan-stopped,connected,gatt,io,done
```

候选：App APK `8d77e5870123211a3a2f913b5d38f284a321263ad61096c0fb59ab46d8d8674b`、
test APK `0ab5572c401a9a5f0d1ea2f8b795f7b3a9e09bd3a33b56129a0dc746f03fe289`。
attempt `passed`、`host_gate=pending`、fixture 关闭时 `resources_closed=true` 且
`advertising_inactive=true`、`cleanup_errors=[]`。

这就是这条 lane 需要的证据：收音机被真正关掉、主机的确认、主机重新打开、**adapter lane 交付
`poweredOn`**、App 自己的重试，然后是一次正常的扫描/连接/读。

## 4. 仍未完成的一项

- **iOS 真机的 lane 证据**：iOS 后端已有被动发布，缺的是一条断言该流的场景（现有 12 场景不含它）。
  Android 侧的证据现在有了（§3d）。

原来那条"Android 上收不到真实变化"的产品缺口已经关闭：现在的 lane 在同一候选上交付了
`adapter-off → adapter-stream-on(poweredOn) → adapter-restored` 的完整顺序。
