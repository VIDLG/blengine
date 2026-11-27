# `multi` 场景：多链路隔离（Android 真机通过）

本文件记录 `rf-multi` 迁移的第一份真机证据，以及这轮真机**第一次跑就抓到的产品缺陷**。场景与回执形状
在 [`rn-internal-rf-scenario-migration.md`](../research/react-native/rn-internal-rf-scenario-migration.md)
第 18 行与 [`b-class-scene-reachability.md`](../research/react-native/b-class-scene-reachability.md) 里描述。

## 这个场景声明什么

同一 owner 上同时存在 N 条链路（N 由 host 声明，2..4）。host 只让**第一条**链路的对端消失，其余每条链路
都必须继续交付一条**在该结束之后才被注入**的值；每条链路写下的字节只能从它自己那条链路读回。

它不用 workbench 表达，因为 bench 按设计只持有一条链路和一个订阅，而 artifact consumer 只允许一个
`dev.blehub` artifact。唯一同时组合「引擎 + 真实 Android 后端」的形状是正式 App 自己的**非 UI 仪器化**
lane：`am instrument` 跑 `BlengMultiLinkInstrumentedTest`，host 拥有 phone lease、fixture lease 和两/多份
close 回执，没有 Appium、没有页面、没有截图。

## 真机第一次跑就失败：后一条订阅接管了前一条的通知流

前两轮（`.work/android-multi-r1/cf813a0e-…`、`.work/android-multi-r2/…`）都停在同一个位置：

```text
observed [..., 'subscribed:0', 'subscribed:1']
java.lang.IllegalStateException: link 0 delivered nothing the host applied
```

host 侧账本（`result.json.published`）证明两条链路各被注入过一次值；设备 logcat 证明**两条链路都真的收到了**
空中通知，而且用的是各自的 `BluetoothGatt`（两个 `mClientIf`，两个 device 地址）：

```text
D/BluetoothGatt: onNotify() - Device=DC:26:E2:E1:5B:69 handle=41
D/BluetoothGatt: onNotify() - Device=C9:7F:38:9F:5E:BE handle=41
```

所以丢的不是射频、不是后端回调，而是引擎的投影层。

### 根因

`SessionObservationProjection` 对通知事件只有一个 `current` 槽：

```rust
let slot = self.lock_notification();
if let Some(delivery) = slot.current.upgrade() {
    delivery.offer(&event);
}
```

`submit_notifications` 每次都用新的 lane 替换 `current`。于是第二条 `enable` 一提交，第一条订阅的 lane 就再也
收不到任何事件——它的值被送给了一个按身份拒绝它们的 lane，`NotificationDelivery::offer` 直接丢掉。连接结束
lane 早就是「集合」，扫描本来就是「单例」（一个 owner 只有一个 scan），通知这条被写成了扫描的形状，而它
实际是连接/订阅的形状。

### 修法（按理想形状，不为测试让路）

- 通知 lane 改为集合，与连接结束 lane 共用同一形状：`LaneSet<L> { live: Vec<Weak<L>> }` + `OwnerLane`
  trait（`install` / `fan_out` / `close_all`）。`project()` 向**每个** lane 投递，每个 lane 自己按
  `(connection, session, characteristic, subscription)` 身份决定要不要。
- 独占的扫描**故意不用** `LaneSet`：它真的互斥，第二个提交在 lane 活着时直接 `DeliveryBusy`，
  不会被静默顶掉。两种形状的取舍写在模块文档里，并由测试分别锁住。
- 单槽才需要的 `can_replace()` / `NotificationSubmissionError::DeliveryBusy` 随之删除：两个 `enable`
  共存不再需要「谁替换谁」的仲裁。ABI 快照未变（该 enum 不跨 UniFFI 边界，FFI 侧映射为
  `ForeignOperationFailure`），`pixi run just binding::bleng-snapshot-check` 仍是 verified 6。
- 回归：`crates/bleng-supervisor/src/tests/observation_lanes.rs`（集合形状四条）与
  `crates/bleng-supervisor/tests/ffi_scan/notification_view.rs::a_second_subscription_never_takes_over_the_first_stream`
  （一个 owner、两个特征、两条订阅；先 `enable` 的第一条在第二条 `enable` 之后仍必须收到自己的值）。

## 顺手根治的 host 侧问题

| 问题 | 现在 |
|---|---|
| `run_instrumentation` 的收尾重放丢了 `key=stage_key` | 补上；否则 `subscribed:0` / `subscribed:1` 在最后一次重放里会坍缩成同一个 stage |
| `advertisement:$index` 按到达顺序发 | 扫完后按声明顺序发；到达顺序是射频的，不是「观测到哪个 peer」的事实 |
| 只支持两个 peer（survivor 硬编码 1） | 词表与测试都按声明数派生：`survivor-{requested,}:1..N-1`、`disconnected:1..N-1` |
| 所有 fixture 共用一个 session id | 每个 peer 的会话 id 由 run id 派生成 UUIDv5；close 回执同时校验 `session_id` 与 `resource_identity` |
| peer 在 `start()` 之后才入账 | 先入账再 `start()`；启动失败的 peer 只释放它自己的 lease 与串口 |
| 失败现场只有 stage 列表 | 失败时额外落 `device-logcat.txt` 和 `published` 账本（谁在哪个 stage 注入了什么） |

## 通过与回执（Android）

```sh
pixi run just react-native::formal-host-android-native-multi-link \
  --device 3ac5cc09 \
  --app-apk tests/consumers/react-native-bleng/android/app/build/outputs/apk/debug/app-debug.apk \
  --app-apk-sha256 36db92a9e676dd56629bffccbc6fe9c897bb83e12c7da7c96eba4e6b40bece34 \
  --test-apk tests/consumers/react-native-bleng/android/app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk \
  --test-apk-sha256 1cd1e2505e61667def22119dc1198cda92481600df79cf704987bd8d4682c7c0 \
  --peer "port=/dev/cu.usbmodem0010502520281,contract=tests/peripherals/nrf52840/firmware/contracts/multifunction-rf-v2-a.json,profile=tests/peripherals/nrf52840/firmware/profiles/multifunction-rf-v2-a.json,identity=jlink:1050252028" \
  --peer "port=/dev/cu.usbmodem0010502514791,contract=tests/peripherals/nrf52840/firmware/contracts/multifunction-rf-v2-b.json,profile=tests/peripherals/nrf52840/firmware/profiles/multifunction-rf-v2-b.json,identity=jlink:1050251479" \
  --output .work/android-multi-r3 --timeout 900 --cleanup-seconds 150 \
  --authorize-install --authorize-fixture-start --authorize-bluetooth-permissions --use-root
```

`run_id = 7e317cae-d96d-4c27-ad2a-b1de002c60af`（`.work/android-multi-r3/7e317cae-…/result.json`）：

| 字段 | 值 |
|---|---|
| `status` / `scenario_status` | `passed` / `passed` |
| `phone_status` / `fixture_status` | `passed` / `passed` |
| `host_gate_status` | `pending`（设计状态：RF 场景不自行升级 host gate） |
| `cleanup_errors` / `phone_cleanup_errors` | `[]` / `[]` |
| `published` | `subscribed:0`、`subscribed:1`、`survivor-requested:1` 各注入一次 `ef` |
| `fixture_close` | peer 0 `1c968cb7…` `jlink:1050252028`、peer 1 `37b6f6c0…` `jlink:1050251479`；两份都 `advertising_inactive=true`、`resources_closed=true` |
| 24 项 stage | 全部按序观测，结尾 `dropped:0` → `survivor-requested:1` → `survivor:1` → `disconnected:1` → `done` |

同一轮之前先用**同一份 APK**跑了单链路 `native-notification`（`.work/android-nt-control/1ac4637a-…`，passed），
用它把「环境/固件/发布路径坏了」和「多链路特有」区分开——这是定位到投影层而不是 fixture 的关键一步。

## 离线验证

| 检查 | 结果 |
|---|---|
| `pixi run python -m pytest tools/tests/react_native -q` | 2231 passed / 21 skipped |
| `pixi run cargo test -p bleng -p bleng-supervisor -p blehub --all-features` | 全绿（含 44 项 `ffi_scan`） |
| `pixi run just binding::bleng-snapshot-check` | verified 6 reviewed API snapshots |

新增离线回归 `tools/tests/react_native/test_bleng_android_multi_link.py`（24 项）：stage 词表派生、
**Kotlin 与 host 词表漂移**（把两边都归一成 `xxx:{n}` 后强制相等）、写入负载每条链路不同且不等于 provisioned、
per-peer 会话 id、准入边界（peer 数、端口/身份重复、预算、授权、摘要、模拟器序列号）、CLI 解析。

## 同类缺陷的系统性清理

上面那个 bug 不是「某一行写错了」，而是**形状**允许它发生：一个能共存的资源被写进了单槽。所以修完之后
把这一类一起收了：

**判据**：一个 lane（或路由表）如果对应的资源可以共存（多条链路、多个订阅、多个特征），它就必须是
**集合 + 按完整身份（连接/会话/特征/订阅）匹配**；单槽只能用于真正互斥的资源，而且互斥必须
**拒绝**（`DeliveryBusy`）而不是「新的静默顶掉旧的」。

| 位置 | 资源是否可共存 | 形状 | 旧的问题 |
|---|---|---|---|
| `SessionObservationProjection::connections` | 是（多条链路） | `LaneSet<ConnectionLane>` | 已在上轮修为集合 |
| `SessionObservationProjection::notifications` | 是（每链路/每特征一个订阅） | `LaneSet<NotificationDelivery>` | **本轮根因**：单槽 `current` + `can_replace` 含 `bound`，第二条订阅一 enable 就顶掉第一条 |
| `SessionObservationProjection::scan` | 否（一个 owner 一个扫描） | 单槽，**保留** | 无：`can_replace` 不含 `bound`，扫描活着的第二个提交直接 `DeliveryBusy` |
| `AndroidBackendState::subscriptionsById` | 是 | `Map<subscriptionId, ActiveSubscription>` | 无 |
| `resolveCharacteristicNotificationRoute` | 是 | 先比 `BluetoothGatt` 对象身份，再在**该 route 内**比特征对象身份 | 无 |
| Apple `GattIoController` 的 `queue.subscriptions` | 是 | `[SubscriptionId: SubscriptionRoute]`，回调先比 peripheral 对象身份再比特征对象身份 | 无 |
| `NotificationDelivery::offer` / `ConnectionLane::offer` | — | 各自按自己的 `(connection, session, characteristic, subscription)` 作闸，事件发给所有 lane 也不会串 | 无 |

**结构上让它写不出来**：共存 lane 现在只有一种类型 `LaneSet<L> { live: Vec<Weak<L>> }` + `OwnerLane`
（`install` / `fan_out` / `close_all`）。连接与通知共用它，`NotificationLanes` / `ConnectionLanes` 两个
几乎相同的实现被删掉，也没有 `current` 字段可以写。独占的扫描故意**不**用 `LaneSet`，并在类型上写清
原因 —— 它是真的互斥，不是漏改成集合。

**回归闸门**：

| 层次 | 测试 |
|---|---|
| 集合形状（无硬件） | `crates/bleng-supervisor/src/tests/observation_lanes.rs`：一条事件到达**每个** lane（不是只要最新的）、无人持有的 lane 被剪掉而不是变墓碑、poisoned 集合把所有 lane 标为失败而不是丢事件、retire 只结束一次 |
| 产品投影（无硬件） | `tests/ffi_scan/notification_view.rs::a_second_subscription_never_takes_over_the_first_stream` |
| 被拒绝的 enable 不占用位置 | `tests/ffi_scan/notification_view.rs::a_refused_enable_does_not_block_the_retry_on_the_same_characteristic`（同一特征上被拒后直接重试成功，不需要先 `detach`） |
| 互斥的扫描仍会拒绝 | `tests/scan_delivery.rs::original_start_handle_keeps_independent_cancel_and_single_consumer_wait`（活着的扫描上第二次 `begin_scan` → `DeliveryBusy`） |
| 真实设备 | `native-notification`（scan + 单订阅）与 `native-multi-link`（两条链路）在同一份 APK 上都通过 |

## 重构后复跑（同一份 APK）

APK `7ba832bd…`、androidTest `1cd1e250…`：

| lane | 结果 | 回执 |
|---|---|---|
| `native-notification`（单链路，scan + 订阅 + 值 + disable） | passed | `.work/android-nt-r2/faf2cab6-…` |
| `native-multi-link`（两条链路 + 一条被打断） | passed | `.work/android-multi-r4/5163a428-…`（24 项 stage） |

- iOS 入口（`formal-host-ios-native-multi-link`）当时在真机上走到驱动阶段：签名 Debug `build-for-testing`
  成功、App 与 manifest 被 host 安装并启动、`idevicesyslog` 已证明能抓到真机系统日志。卡在启动即 `SIGABRT`
  （**后续已解决，最终通过见下文「iOS 真机通过」**）：
  崩溃栈是 `margelo::nitro::HybridObjectRegistry::registerHybridObjectConstructor` ←
  `+[BleHubReactNativeBlengAutolinking load]`——**测试 bundle 自己链接了一份 Nitro pod**，而 App 已经注册过，
  重复注册直接抛异常。两种解法：
  1. （推荐）给设备侧建一个**不链接 pod 的 hosted target**（`inherit! :search_paths`，靠
     `BUNDLE_LOADER` 向 App 解析符号），多链路场景放进去；现有 logic-test target 不动；
  2. 让 Nitro 接受重复注册（上游行为，不归本仓库控制）。

  本轮沿路修掉的两个 host 侧缺陷（都是真机才暴露）：失败取证件曾用「无限运行的
  `idevicesyslog` + `timeout`」，必然抛错并使 **phone lease 进 quarantine**；`staged_xctestrun` 把
  `__TESTHOST__` 展开成 host 的**父目录**而不是 `.app` 本身，导致 `Missing test product`。

## iOS 真机通过

`formal-host-ios-native-multi-link` 在 iPhone 14 Plus（`00008110-000C61160C39401E`）连续两轮 passed：
24/24 stage、两片 fixture 都给出关闭回执、`phone_status=released`、`cleanup_errors` 与
`phone_cleanup_errors` 均为空。回执：`.work/ios-multi-r10/89348a24-…`、`.work/ios-multi-r11/4e5a456b-…`
（App tree `fbf0380b…`）。

### 缺口 B（值未抵达）的根因：staging 陈旧，不是场景或固件

三路证据互相咬合：

1. 被动嗅探（`ble_sniffer capture --follow <peer 地址>`）证明 B 片**确实发了** 5 条
   `ATT Handle Value Notification`（`handle=41, ef`），此前 CCCD 写入是 `0100`，间隔 6–7 个连接事件（≈30 ms
   连接间隔 × 0.2 s）。固件侧同一份 `set_value` 在没有订阅者时返回成功但不发 PDU，所以「发了」这个事实只能来自空口。
2. Apple 后端诊断（`BLEHUB_APPLE_NOTIFICATION_DIAGNOSTICS=1`）显示 10 条 value 全部
   `value.disposition=notification` → `notification.ingress result=delivered`，两片 id 各自一致
   （conn 2/session 4/sub 6/char 14、conn 3/session 5/sub 7/char 33）。后端与引擎都没丢。
3. 重跑前刷新 staging（`.work/staged/apple-bleng-react-native`）后，同一份场景直接通过。旧的 staged
   Rust 镜像早于「通知 lane 是集合」那次修复，所以 App 里跑的是**退役的单槽 lane**：后一条订阅接管前一条的
   投递，`pull(0)` 永远收不到值。

结论：`platforms/apple` 与 `crates/` 的修复没有进入 staged 产物，而设备构建只接受
`BLEHUB_APPLE_BLENG_STAGING_ROOT`。这类陈旧可以静默通过签名、安装和启动，看起来完全像设备缺陷。

### 缺口 A（前缀 marker 丢失）的根因：中继启动窗口

同一二进制、同一 lane 五次运行里，前三次缺 `started`/`scan-submitted`（有时连带 `scan-started`），后两次全有。
`wait_live` 等到第一条设备行并不等于通道进入稳态；丢的是进程刚启动时自己写的那几行。

修法（保持契约不弱化）：

- 场景开头先打 8 条、间隔 250 ms 的**探针**，tag 是 `BLEHUB_RN_MULTI_SYNC`。它由 marker tag 加后缀构成，
  后缀是词字符，所以宿主的分隔符解析永远不会把它当成 stage；探针也不在 `expected_stages` 里，吸收丢失不会
  掩盖任何声明阶段。真机实测：探针 1–7 进日志、`started` 与后续 24 个 stage 全到。
- 宿主侧 `wait_live` 仍保留一次有界 warm-up（1.5 s），并把 `attach_seconds`、`probe_lines` 写进 `result.json`。
- 失败时把中继自己收到的行落盘成 `markers.txt`（此前失败只留下 cleanup 时**重新 attach** 的采样，看不到现场）。

### 顺带修掉的两个「白花一轮」问题

- `formal-host-ios-device-test-build` 现在依赖 `stage-apple-bleng`/`bleng-check`/`formal-host-check`；
  并且 `bleng_ios_consumer.require_current_apple_backend()` 按**内容**比对 staged 与
  `platforms/apple` 的 Swift 源，不一致就拒（合成构建夹具没有 staged 源时由 `STAGED_FILES` 负责）。
- `_drive_test` 不再用 `** TEST SUCCEEDED **` 文本判定成败：`-quiet` 根本不打印它，通过的一轮会被判成失败。
  判据改为 xcodebuild 退出码，失败时输出末尾若干行。

## 还没做

- 场景层已完整（`multi` / `--rounds` / `--fault` 均两端真机通过）；下一步不是新场景，而是 C 类核对
  （`basic` / `stress` / `adapter` / `permission` / `watchdog`），之后 `react-native-internal` 才具备退休条件。
- iOS 入口已真机连续通过；此前记录的宿主侧/场景侧修法保留如下（都是真机才暴露）：
  - Debug 配置：`@testable` 需要 `-enable-testing`；Release 的报错是 "Unable to resolve Swift module
    dependency"，与“缺切片”难区分。
  - App 容器：设备测试必须有 `TEST_HOST`（条件键必须整体加引号，否则 CocoaPods 解析器拒）。
  - 重复注册：App host 里再链接一份 Nitro pod 会 `abort()`（`registerHybridObjectConstructor`）；新建
    `BleHubBlengHostDeviceTests`（Podfile 嵌套 `inherit! :search_paths`，`BUNDLE_LOADER` 向 App 解析
    符号），多链路场景已迁入；`BleHubBlengHostNativeTests` 退回纯 logic-test 形态。
  - 宿主侧：`-only-testing` 用 **target 名**不是 `.xctest`（且该名从 manifest 的 key 读回）；
    `__TESTHOST__` 展开成 `.app` 本身；失败的 syslog 取样必须用有界 `Popen`（否则 cleanup error 会把
    phone lease 拖进 quarantine）；`pod install` 默认 `--deployment`，改 Podfile 必须显式
    `--accept-podfile-change`。
  - 场景：独立 `HybridBlengSessionOwner` 必须先 `BlengEngineLifetime.bind`，否则 `start()` 报
    `engineUnbound`。

## soak（轮次）

`multi-soak` 不是第二个 runner，而是同一个 lane 的 `--rounds 1..10`：每轮都是一次**完整**的场景（重新 scan → connect →
subscribe → 值 → 掉链 → 幸存者 → 断开），所以 stage 词表不随轮数变化（`MAXIMUM_ROUNDS` + `require_rounds()` 在契约层声明）。

- 轮间由 `DirectFixture.prepare_next_cycle()` 准备：先 `settle_no_peer`（排空事件并证明链路已消失）再
  `resume_advertising()`。两个 runner 共用这一个动词，不各写一遍次序。
- 产物按轮命名：`multi-link-{i}.xcresult` / `xcodebuild-{i}.log` / `instrumentation-{i}.log`（单轮的运行保留原名）；
  `result.json` 新增 `rounds[]`，`published[]` 带 `round`；失败时额外记录 `primary_traceback`。
- iOS 真机：3 轮 passed（`.work/ios-multi-soak-r1/977ea4fa-…`，3×24 stage、9 条 publish、两片回执、phone released）。
- Android 真机：2 轮 passed（`.work/android-multi-soak-r7/ad848fab-…`，2×24 stage、6 条 publish、两片回执）。

### 多轮真机抓到的两个 host 侧缺陷

1. **`adb logcat` 在 attach 时重放已有缓冲区**。第二轮的新 capture 立刻读到第一轮的 marker，于是在链路尚未建立时
   就对 peer 0 执行了 `drop_peer` → `peer drop requires a connected peer`。修法：每轮在创建 capture **之前**清
   logcat 缓冲区（原先只在整轮循环外清一次）。这条是通用规则：任何“新 capture 只看本轮”的假设，都必须
   对 logcat 显式成立。
2. **失败的一轮会在 peer 上留下固件自己认定仍存在的连接**，下一次 `fixture.start()` 按契约拒绝：
   `fixture startup refuses an unowned connected peer`。现场确认两台手机都没有 `blehub` 进程，属于 peer 侧
   链路状态；软件恢复用 J-Link 的 **`r`/`g`**（`SYSRESETREQ` + 运行，flash 不变），不是重刷：
   `JLinkExe -NoGui 1 -CommandFile <script>`，脚本用 `SelectEmuBySN <sn>` 选中目标。
   同一次失败还留下了 Android phone lease（owner PID 已退出）；按 `AndroidRadioController` 的契约不做自动采纳，
   由操作者显式删除了那一个锁目录后才重跑。

## 声明式故障（`multi-fault`）

故障也是同一个 lane 的声明：`--fault write-reject:<index>`。host 在该 peer 的链路一建立（`connected:<index>`）就 ARM
一次写 hold —— 早于发现，因此早于 App 的第一次写；固件报告 HELD 后由 host REJECT，并要求 peer 自己的回执
（`winner=REJECT`、`outcome=INJECTED_REJECT`）作为证据，而不是从 App 的症状倒推。

场景契约只改**被故障链路那一对** stage：该链路要求 `write-rejected:<index>`（App 报告了失败）与
`unchanged:<index>`（读回等于写之前的值），其余链路仍是 `written` / `io`；值注入、对端断链与幸存者逻辑不变，
这就是隔离主张。声明与回执都落在 `result.json`（`fault`、`rounds[].fault`）。

真机证据（全部 `refused=true`）：

| 入口 | 回执 |
|---|---|
| Android `--fault write-reject:1` | `.work/android-multi-fault-r1/a3bad269-…` |
| iOS `--fault write-reject:1` | `.work/ios-multi-fault-r1/b88c90d3-…` |
| iOS `--fault write-reject:1 --rounds 2` | `.work/ios-multi-fault-soak-r1/710b1a9a-…` |
| Android `--fault write-reject:1 --rounds 2` | `.work/android-multi-fault-soak-r1/0c8db815-…` |

选 `write-reject:1` 是有意的：被拒写的是**幸存链路**，因此它同时证明「写的拒绝只影响该链路」与「被拒过的链路
仍能收到对端结束之后注入的值」。ARM 的时机是一个真约束：若 hold 在本轮预算内始终未被捕获，本轮直接失败
（`the declared write refusal was never captured`），不会以干净场景侥幸通过。

## 前置检查（拿 lease 之前）

`run_multi_link` 现在在**创建 attempt 目录、申请 phone lease、启动任何 fixture 之前**，把所有「晚发现就白花
一次安装和清场」的前提变成具名检查（`bleng_ios_multi_link._preflight`）。每一条只读本机文件或 PATH：

| 检查 | 拒绝的是 |
|---|---|
| App 目录存在、非符号链接、带 `embedded.mobileprovision` | 未签名的产物 |
| `Info.plist` 的 `CFBundleIdentifier` | 装上去以后才发现宿主身份不对 |
| `tree_sha256(app)` 等于 `--app-tree-sha256` | 换了构建却复用旧摘要 |
| manifest 里存在承载 `BleHubBlengHostDeviceTests.xctest` 的 target | 选错 manifest |
| 该 target 的 `TestHostPath` 就是 `--app` | `test-without-building` 会装 manifest 命名的另一个 App |
| `TestHostBundleIdentifier` 等于 App 的 bundle id | 同名路径、不同产品 |
| App 的 `PlugIns/` 里真的有这个测试 bundle | 构建没有把测试 bundle 嵌进 App |
| `xcrun`、`idevicesyslog` 在 PATH 上 | 起进程时才 `FileNotFoundError` |

`-only-testing` 用的 target 名现在**从 manifest 的 key 读回来**（key 即 target 名，bundle 是另一种拼写），
宿主不再重复一份常量。`resolve_xctestrun` 是 plist 解析、`__TESTROOT__`/`__TESTHOST__` 展开和宿主身份比对的
唯一实现，`staged_xctestrun` 只在它的结果上加本次运行的环境变量。

离线证据：`test_bleng_ios_multi_link.py`（覆盖上面每个拒绝分支）与 `test_bleng_ios_native_common.py` 新增的
解析/host 工具用例。对真机产物单独跑过一次前置检查：bundle id `FWVYQ287W5.bleng-rn-host`、tree
`413291fa…`、target `BleHubBlengHostDeviceTests`，与构建入口打印的摘要一致。
