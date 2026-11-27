# bleng-supervisor FFI 表面收敛：单一扫描能力面、类型化 Busy 与 lane 命名对齐

这份报告记录 `crates/bleng-supervisor` 审阅后的一次**故意的公共形状变更**。ABI 仍未冻结，但它删除
了 FFI 符号、新增了一个错误变体，并把 supervisor 的 lane 提交方法重新命名，因此属于需要评审并重生成
审定 snapshots 的变更（`AGENTS.md`：snapshots 是冻结的机械替身，更新是受评审的行为，不是修复）。

变更前共有 9 份 snapshots（blehub 3 + bleng 3 + supervisor 3）。本次只影响 bleng 与 supervisor 两组
中的 supervisor 部分；blehub 与 bleng 的快照内容未变。

## 1. 规则：plain 名字是原生命令，`_view` 是记录/能力

同一行为在 FFI 上曾有多个拼写。这次把词表固定成两条：

- `begin_<command>`（在 `Supervisor` 上）返回**原生 typed operation**，失败即抛错；
- `begin_<thing>_view`（在 `Supervisor` 上）或能力对象上的 `begin_<command>` 返回 `Foreign*BeginResult`
  记录 + `Supervised*` 能力。

这条规则在其它 20 多个 `Supervised*` 对象上本来就成立，只有扫描是例外。

## 2. 变更

| # | 位置 | 旧 | 新 | 理由 |
|---|---|---|---|---|
| A | `SupervisedScan` 的导出方法 | `wait_until_started(u64)`、`cancel_start()`、`next_event(u64)`、`begin_stop()` 与记录风格四方法并存 | 只导出 `start_result(f64)`、`cancel_start_result()`、`next_observation(f64)`、`begin_stop_view()`（外加 `detach`/`is_scanning`/`observation_snapshot`） | 一个行为一个拼写：Kotlin/Swift 自动补全从 11 个方法降到 7 个。typed 四步仍是**实现**（`pub`，未导出），供 crate 自己的集成测试直接断言 typed 结果 |
| A' | `Supervisor::begin_scan`（抛出型、返回 `SupervisedScan`） | 与 `begin_scan_view` 重复，且是全 FFI 唯一返回能力的 `begin_x` | 删除；raw 命令是 `begin_start_scan` | 违反第 1 节规则；RN 的 iOS/Android 胶水本来只用 `beginScanView` |
| B | 超时参数类型 | 这两个方法是全 FFI 仅有的 `u64` 超时 | 随 A 一并消失；FFI 的 25 个超时参数现在统一 `f64` + `normalize_timeout` | 非整数/负数在进入 Rust 前就能得到 `invalid_timeout`，而不是绑定层的转换异常 |
| C | `SupervisorFfiError` | lifecycle 并发时返回 `Platform { detail: "supervisor operation is already in progress" }` | 新增 `Busy` 变体（5 处使用） | 外部调用方要能分支，而不是匹配文本；Rust 层早有 `SupervisorOperationError::SupervisorBusy`、failure 编解码早有 `supervisor.busy` 码，缺的就是 FFI 变体。iOS `BlengFailureMapping` 增 `.Busy → supervisor.busy`（lifecycle 域） |
| D | `SessionSupervisor` 的 lane 提交 | `begin_scan`、`begin_connect`、`begin_connect_with_options`、`begin_notifications` | `begin_scan_view`、`begin_connect_view`、`begin_connect_view_with_options`、`begin_notifications_view` | 与 bond 既有的 `begin_create_bond_view` 对齐。此前 Rust 层 plain 名=带观察通道，FFI 层 plain 名=原生命令，两层含义相反 |

`D` 之后 `SessionSupervisor` 不再有 plain `begin_connect`：这一层从不提供「不带 end lane 的 connect」，
现在名字如实表达这一点。

## 2.1 Rust 面也单一拼写（`A` 的收尾）

第一轮曾把 typed 四步留成 `pub`（不导出），理由是 `tests/ffi_scan.rs` 用它们做精确断言。那仍然是
两套拼写，只是其中一套不在 ABI 里。这一步把它收尾：

| 项 | 变更 |
|---|---|
| typed 四步 | `pub(super)`；只有 `ffi::scan_view` 的记录适配器和 crate 内测试能看到 |
| `ScanSessionError` | `pub(super)`，并去掉 `uniffi::Error` derive |
| FFI 测试 | `tests/ffi_scan.rs` + `tests/ffi_scan/` 整体下沉为 `src/ffi/tests/ffi_scan.rs`（`#[cfg(test)] #[path]`），断言一字未改 |

下沉而不是重写断言，是因为那些用例断言的是 `StopScanOperation` 的一次性语义（`wait` 两次、完成后
`cancel`）与 `InvalidTimeout` 分类；改成记录风格会削弱精度。测试挪进 crate 后仍驱动同样的导出对象，
只是额外能看见实现层。

## 2.2 FFI 镜像不再声明不可达的错误枚举

`ScanSessionError`、`ScanDeliveryError`、`AdapterDeliveryError`、`CompanionPresenceDeliveryError`
原本都带 `uniffi::Error`，但**没有任何导出签名能产生它们**：lane 观察失败统一经
`ForeignOperationFailure` 的记录码过界。只保留 `SupervisorFfiError`（lifecycle 抛出）与
`SupervisorOperationError`（raw `begin_*` 抛出），FFI 的错误面恰好等于「两种抛出 + 一种记录」。

四个枚举仍是 `pub` 的 Rust 词汇（lane 的 `next_event` 等公开方法返回它们，集成测试也直接匹配），
所以它们继续是 Rust API；变的只是 FFI 镜像。

## 3. 顺带修掉的内部问题（零 ABI 影响）

审阅同一 crate 时发现并修复，均不改变任何公共形状：

| 位置 | 问题 | 修法 |
|---|---|---|
| `bond_progress.rs` `BondProgressDelivery::fail_unavailable` | 无条件覆盖 `retained`，违反本模块"first end wins"的规则（其余 4 个 lane 都保留首个失败） | 只在 `None` 时写入 |
| `observation.rs` `project` | 通知 lane 集合对**每条**事件 fan out（每条广告为每个订阅取一次锁），而另外 4 个 lane 集合都按事件类型门控 | 按 `NotificationStateChanged｜NotificationReceived｜ConnectionStateChanged` 门控；与 `NotificationDelivery::offer` 接受的事件完全一致 |
| `bond_progress.rs` / `connection_delivery.rs` | `#[allow(dead_code)] source_context`：bond 的字段确实没人读，ConnectionLane 的 allow 是多余的 | bond 的 `Debug` 与其它 lane 一致打印它，两处 allow 删除 |
| `ffi/gatt_view.rs` | `descriptors()` 每次调用新建 `Arc`，而 `services()`/`characteristics()` 返回构建时存好的同一批 | 描述符能力随拓扑一次性构建（`DescriptorId` 中间结构删除），三级身份都稳定 |

## 4. 验证

| 命令 | 结果 |
|---|---|
| `cargo test --workspace` | 633 passed, 0 failed |
| `cargo test -p bleng-supervisor --features ffi` | 191 passed, 0 failed |
| `cargo clippy --workspace --all-targets -- -D warnings` | 无告警 |
| `python -m unittest tools.test_api_snapshots` | 32 passed（scan 的期望方法集在三种语言上完全一致；不可达错误枚举的 fixture 与 mutation 用例移除） |
| `just binding bleng-snapshot-check` | verified 6 reviewed API snapshots |
| `just binding freeze-inventory` | 重生成 `docs/research/bleng/abi-freeze-inventory.md` |

`tools/test_api_snapshots.py` 的改动是这次评审的一部分：scan 的期望方法集只有一份（三种语言相同），
并显式断言 `beginScan(` 不再存在（防止删除被悄悄恢复）；不可达错误枚举的 fixture 与 mutation 用例一并
移除，`RUST_VIEW_SOURCES` 补上真正拥有 `next_event` 的 `companion_presence_view.rs`。

## 5. 明确未改的

- `normalize_timeout` 只判断 f64 可表示性，`1..=24 h` 仍由 bleng 的 canonical 校验决定；两条消息各自
  描述自己拥有的规则，未合并。
- `Supervisor` 的 `InFlight`/`Interrupted` 语义、`OwnerRetirementEvidence` 的失败优先级、
  `RecoveryPolicy` 的状态迁移：逐段读过，无问题。
- RN 的产品层名字（`waitUntilStarted`、`nextEvent`、`cancelStart`、`beginStop`）是 Nitro spec 的 lane
  词汇，与 Rust FFI 的记录层名字分属两层，本次不动；两者之间的映射留在 Kotlin/Swift 胶水里。

## 6. 消费者同步（发布产物对外证明）

FFI 形状改了，消费已发布 AAR 的测试也必须同步，否则 `just ffi android-bleng-build` 会停在
`consumer-build`。这一步是语义迁移，不是改名：typed 抛出变成了身份无关的记录。

### 6.1 `tests/consumers/android/bleng/.../PublishedBlengTest.kt`

| 旧断言 | 新断言 |
|---|---|
| `beginScan(context, ScanOptions)` 或抛 `ScanSessionException.DeliveryBusy` | `beginScanView(context, ForeignScanOptions)` → `failure.code == "delivery.busy"` 或 `"supervisor.no_active_session"` |
| `waitUntilStarted(u64)` / `cancelStart()` 抛 `ScanSessionException.Operation` | `startResult(f64)` / `cancelStartResult()` → `failure.code`（`invalid_timeout`、`bleng.operation_result_consumed`、`bleng.operation_already_completed`、`bleng.operation_not_cancellable`） |
| `nextEvent(u64)` 抛 `ScanSessionException.Observation` / `.Delivery` | `nextObservation(f64)` → `failure`（`invalid_timeout`、`bleng.timeout`、`delivery.pull_busy`）或 `ended`（`OWNER_CLOSED`、`REQUESTED`） |
| `beginStop()` 或抛 `ScanSessionException.StartNotObserved` | `beginStopView()` → `failure.code == "scan.start_not_observed"`；成功时取 `.operation` 交给 `waitResult`/`cancelResult` |
| 断言宿主的 `commandId` 出现在异常里 | `ForeignOperationFailure` 按设计**不带身份**；需要宿主命令 ID 的用例改从后端 fixture（`backend.stopping.await()`）读取，这对观察更直接 |

广告的比较也随视图走：`ForeignScanAdvertisement` 没有 scan 身份，peer 身份只有 owner 作用域的
`peripheral.key()`；`assertScanAdvertisement` 因此拆成「事件层」与「视图层」两个重载，共享 payload 断言。

`SCAN_OPTIONS`（bleng `ScanOptions`）仍保留：`ForeignScanOptions` 只在边界做可表示性转换，随后交给同
一条 canonical 校验，所以后端收到的命令选项与 `SCAN_OPTIONS` 逐字段相等——这条断言本身仍在盯
「视图没有偷偷改写扫描过滤」。

### 6.2 平台后端审计（host-only，无 RF）

两端 `platforms/` 与 `integrations/react-native-bleng/{android,ios}` 的会话层逐段对照后结论：

- `capabilities()`、11 处 `NotSupported()` 边界、网络/线程/回调三类资源计数、
  `ForeignResourceCount` 的「count vs reports N」语义在 Apple 与 Android 上等价；
- 唯一真实缺陷是 Apple `ApplePlatformSession.recoverTarget` 里的 `withEventReaderLock { self.owner! }`
  强制解包：owner 缺失时会 trap，而同文件 `activeOwnerForHost` 已按 `guard let` 返回 typed failure。
  现改为 `guard let` + `SupervisorFfiError.Platform(detail: "Apple recovery requires an active owner")`。
  `swiftc -parse` 通过；`swiftc -typecheck` 需 `BleHubUniFfi` 模块，改以生成绑定的人工核对替代
  （`InvalidContext`、`NoActiveContext`、`Busy`、`Timeout`、`Platform` 均为无标签 case）。

### 6.3 同步后的验证

| 命令 | 结果 |
|---|---|
| `cargo fmt --all --check` | 通过 |
| `cargo test --workspace` | 633 passed, 0 failed |
| `cargo clippy --workspace --all-targets -- -D warnings` | 无告警 |
| `just binding bleng-snapshot-check` | verified 6 reviewed API snapshots |
| `just lint` / `just docs-check-mermaid` | 通过（29 diagrams / 306 files） |
| `python -m unittest tools.tests.react_native.test_api_docs tools.tests.test_freeze_inventory -q` | 10 passed |
| `cd integrations/react-native-bleng && npm run typecheck` | 通过 |
| `just ffi android-bleng-build` | `consumer-build` 通过，`bleng_android_consumer=static-build-passed`（ART instrumentation 未跑） |
| `just react-native bleng-android-unit-test` | 通过 |

ART instrumentation（真机 `connectedDebugAndroidTest`）本次没有运行：审计与迁移都是 host-only，本机
没有启动设备，也不允许隐式切换 radio/permission。记录层名字的正确性由消费端**静态编译**证明，语义
正确性由 crate 内 `src/ffi/tests/ffi_scan.rs` 与 `tests/consumers` 的编译期 surface 断言共同承担。
