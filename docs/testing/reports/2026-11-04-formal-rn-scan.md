# Formal RN typed scan 与关闭终态候选

日期：2026-11-04。主机：真实 Mac，本地 Pixi/Just、Xcode Swift 6.4.2。
候选是基于 `7ba9ae40c9b3` 的当前未提交工作树，不是该 commit 本身的发布结果；ABI 未冻结。

**结论：正式 scan API 已接通 Rust → UniFFI → Nitro Swift/Kotlin → 唯一 App。软件回归及两端
App/native-test 编译通过。未执行 iOS/Android native runtime、Hermes/Appium 或物理 RF。**
本报告不声明 internal 迁移完成、resource-zero、release 或 Phase exit。

## 实现与责任

规范入口：[Plan §1.5](../../../.plan/PLAN.md)、[scan contract](../../research/react-native/react-native-bleng-scan-contract.md)。
前段证据：[Rust delivery](2026-11-03-rust-scan-delivery.md)、[native-only FFI](2026-11-03-supervised-scan-ffi.md)。

- [`scan_view.rs`](../../../crates/bleng-supervisor/src/ffi/scan_view.rs) 集中投影输入、typed failures、
  advertisement/end 与 opaque peripheral。无 raw command/scan/peripheral/context identity 进入 JS；
  必要 BLE/platform 细节保留，不从错误文本重建身份。
- [`BlengScan.nitro.ts`](../../../integrations/react-native-bleng/specs/BlengScan.nitro.ts) 提供同步
  `beginScan`、原 start wait/cancel、单 pending pull、独立 typed stop wait/cancel。
- Swift/Kotlin 仅做 DTO/union、owned `ArrayBuffer` 与 Promise 映射，不复制 scan 状态机、错误策略
  或 recovery。Rust 复用原 owner runtime 和唯一 reader；detach/dispose 只结束 observation。
- Rust delivery 沿用每 scan 64 条广告 / 256 KiB owned data 与独立 stop error 预算；overflow
  明确失败而不隐式 stop/retry/close。它不是 JS heap、空口吞吐或 native resource-zero 保证。
- [`useManualScan.ts`](../../../tests/consumers/react-native-bleng/src/useManualScan.ts) 的所有动作均由
  用户显式触发。controller 位于 Tests / Current run / Evidence 切换之上，导航不发 BLE 命令。
  广告只保留最近 8 个有界文本摘要、16 字节 binary prefix，不保存 opaque peripheral 或原 buffer；
  Evidence 保留最近 32 个 action，明确披露遗漏，不宣称 reload 持久化。

## 审查发现与修正

### 一次性 stop 结果不能重新等待成功

原 App mock 让同一 stop handle 的 wait 先失败、再成功，违反 BLeng 的单次结果槽：操作失败或
wait timeout 已消费结果，但 cancel control 仍独立可用。修正前新增回归得到 **5 failed / 36 passed**。

现在只有用户点击 `Submit another stop` 才重新提交；新提交拒绝/异常时保留旧 cancel handle。
`input/invalid_timeout` 是消费前错误，允许改输入后等待原 handle，不在 JS 复制 Rust 超时范围。
更新后的 mock 模拟单次消费，不再用不可能的二次成功掩盖问题。

### 逻辑关闭与清理成功必须分离

Rust `shutdown` 在开始退休时即进入 `Closed`。即使后续 reader/resource validation 失败，或
未启动 owner 的 shutdown 返回 `NoActiveSession`，旧 supervisor 也不能再次 start；原 native bridge
却只在 shutdown 成功后释放它，导致 App 可能永远卡在可重试外观。

新增 Rust [`shutdown_view`](../../../crates/bleng-supervisor/src/ffi/object.rs) 返回
`ForeignOwnerClose { owner_closed, failure }`：

- busy/unavailable 不是终态，不缓存、不假称关闭；
- 获取实际 supervisor 后，在恢复 FFI slot 前读取真实关闭状态；
- 首次终态回执被保留，原 `shutdown` 也记录它，混用入口不能把首次失败改成成功；
- 产品 failure 的 code/message 由 Rust 固定分类，无 raw identity；原 native `shutdown` 的详细错误
  和资源快照签名保留给 HIL/artifact 通道，不是 compatibility JS 控制面。

两端薄桥机械映射为 `close(): Promise<BlengCloseResult>`，只有 Rust 确认 `ownerClosed` 才释放
当前 owner；重复 close 保留首次终态失败，下一次显式 start 才创建新 owner。意外 bridge exception
仍 rejection，不据此猜 owner 已关闭。

App 终态失败显示 `closedWithError`，退休旧 controls、保留错误和启动快照 Evidence，不记作成功
close/resource-zero。新 owner 只由显式 Start 创建，不自动重试清理。旧异步结果可以结算原 action，
不能覆盖已关闭/替换的 scan 或新 session。非终态失败/未知 bridge exception 则保留 owner 供显式处理。

回归位于 [`ffi/close.rs`](../../../crates/bleng-supervisor/tests/ffi/close.rs)、
[`ffi/tests/close.rs`](../../../crates/bleng-supervisor/src/ffi/tests/close.rs)、正式 consumer 的
[App tests](../../../tests/consumers/react-native-bleng/src/App.test.tsx) 和
[scan tests](../../../tests/consumers/react-native-bleng/src/Scan.test.tsx)。
两端 native tests 已增加真实 Rust startup/cleanup failure、重复终态失败和 busy 区分；**本次仅编译，未执行。**

### native receipt 与生成器边界

- synchronous call-entry ticket/epoch 同时约束 delayed start admission 和跨 await receipt publication。
  close 后的旧 start 不能构造 owner、恢复 gate；duplicate start rejection 不清除已有成功 receipt。
- Nitrogen 把 `'android'` union member 生成为 C++ `ANDROID`，与 NDK 宏冲突，首轮 Android build
  确实失败。诊断 platform 改为 string 后，正式两端 graph 编译通过；不手改生成代码。
- 新 close DTO 初次被 strict snapshot declaration gate 拒绝；精确补充两项白名单与三语言 fixtures，
  保留 missing/unknown rejection，重新生成并验证快照，不放宽成允许未知 API。

## 实际执行的软件验证

以下命令在本次工作树实际运行。测试数量只描述 discovery 结果，不代表覆盖率或 native 执行。

| 命令 | 结果与范围 |
|---|---|
| `pixi run just react-native bleng-check` | TypeScript + 锁定 Nitrogen 0.37.1，4/4 HybridObjects 生成 |
| `pixi run just react-native formal-host-check` | 作为两端 build prerequisite 通过：host typecheck、正式 Android autolinking、无 internal dependency |
| `pixi run just react-native formal-host-test` | 2 suites / **48 passed**；只 mock 正式 package boundary |
| `pixi run just test supervisor` | **106 passed**，6 suites |
| `pixi run just test bleng` | **273 passed**，3 suites；Just 使用 RTK |
| `pixi run cargo check --locked -p bleng-supervisor --all-targets --no-default-features` | 通过 |
| `pixi run cargo clippy --locked -p bleng-supervisor --all-targets --all-features --no-deps -- -D warnings` | 通过 |
| `pixi run just binding bleng-snapshot-check` | **29 工具测试 + 6 份 reviewed snapshots** 通过 |
| `pixi run just ffi android-bleng-build` | combined AAR/backend/consumer 静态构建通过，无 ART 执行 |
| `pixi run just react-native formal-host-ios-build` | staged image、Nitro-free PlatformContract、正式 CocoaPods App/native-test `build-for-testing` 通过；未 boot simulator |
| `pixi run just react-native formal-host-android-build` | App + instrumentation test APK 构建通过；未安装或启动设备 |
| `pixi run python -m unittest -v tools.tests.react_native.test_bleng_ios_consumer` | **7 passed**，硬件隔离的 build-owner regressions |
| `pixi run python -m unittest -v tools.tests.react_native.test_bleng_ui` | **32 passed**，合成 Appium lifecycle 回归，不是设备 UI 执行 |
| `pixi run just fmt` | 通过 |
| `pixi run just lint` | workspace Clippy、Ruff、Just/profile 检查通过 |
| `pixi run just docs-check-mermaid` | 通过 |

### 全量工具回归尚未通过

`pixi run just react-native tools-test` 在 **180 秒超时**；没有 `Ran … tests` 汇总，不能报告完成或
通过数量。仅清理本次进程组，确认无残留，不重试延长超时。定向两项工具回归和 snapshot tests
通过不替代这次全量未完成。完整日志：
`.work/logs/2026-11-04-formal-scan-tools-test.log`；退出/清理记录在相邻 `.result.json`。

其他完整日志：

- `.work/logs/formal-host-ios-build.log`
- `.work/logs/formal-host-android-build.log`
- `.work/logs/2026-11-04-formal-scan-{supervisor,bleng,jest,lint}.log`
- `.work/logs/2026-11-04-snapshot-fixture-targeted.log`
- `.work/logs/2026-11-04-formal-scan-test_bleng_{ios_consumer,ui}.log`

### 静态构建产物指纹

这些是 build-only artifacts，不是已安装/运行的移动端候选；Debug App 构建也不证明打包 JS/Hermes
完整运行或 clean npm tarball provenance。

| 产物 | SHA-256 |
|---|---|
| Android `app-debug.apk` | `726a3e88fd921f5aa81c9b03be2ece75d586dff5100cca998463db1eaf5d7127` |
| Android `app-debug-androidTest.apk` | `469dbc171398bed97bdd8117d147650b0032bf21788e85603ac3353816dd7fdc` |
| iOS Simulator `BleHubBlengHost.app/BleHubBlengHost` | `13a0d9a972878053b54ae47960127178ed8654fa49dd4cd665b22556f9a9c5ae` |
| iOS Simulator `BleHubBlengHostNativeTests.xctest/BleHubBlengHostNativeTests` | `e9ef9e69d5877dd01540aa268edfac18a5b464101efd31aa574b593aa7503a00` |

## 尚未证明 / 不得删除的职责

1. 新 native XCTest/instrumentation 在明确 simulator/physical Android target 上的实际执行。
2. Hermes/Nitro union dispatch、binary round-trip、reload/invalidation 和 old-owner retirement。
3. scan Appium 的真实 host/device/fixture ownership 接线与执行。后续软件步骤已实现 host-injected
   有界场景（见下），CLI 仍 lifecycle-only；不以 UI advertisement 计数替代 RF verdict。
4. 真实手机 → 当前有 installed-image receipt 的物理 DK 扫描及后续 connect/GATT/I/O/notification，
   legacy full-suite、REJECT/radio/permission/multi-DK 与 host cleanup/ledger 迁移。
5. clean package/artifact、同候选移动运行、最终 ABI/Phase exits，不能从本次构建推断。

未执行设备安装/启动、radio/permission 变更、固件 provision 或 RF。继续保留 legacy internal 的
独有 reload/probe/RF 责任，迁移条件见 [consumer ownership](../../../tests/consumers/README.md)。

## 同日后续：host-injected scan UI 软件场景

本步骤只改 Python UI 场景、synthetic support/tests 及文档；没有修改 Rust/native/product JS，
没有重新执行或转移上述 native 构建产物证据，没有运行设备/RF。

[`exercise_scan`](../../../tools/react_native/run_bleng_ui.py) 接收现有 driver 与必填、持续检查的
`require_authority`，复用原 lifecycle 的 UI mechanics。`main/run/formal-host-ui` 仍只执行 lifecycle；
`--scenario scan` 被 argparse 拒绝，不以 ready JSON 或旧 receipt 文件放行扫描。phone owner、
DK 活动 lease、contract/profile、新鲜 installed-image receipt 与 App/run/candidate 均须由受信 host
绑定。iOS 非 radio observation 收尾与 WDA 预备/attach 尚未接通，因此不增加危险临时 CLI。

场景覆盖 initialize、start scan、最多 8 次显式 pull、浏览、一次 stop/close，使用同一总 deadline。
只匹配同一条新广告的 name 与 advertised service；广告计数不是物理身份或 RF verdict。每次远程
读/动作前后都检查 guard，失权后不执行推测 cleanup；不确定 click 不重投。最终 Evidence 要保留
同一个 run、精确编号的成功 trace、scan count/samples、`ownerClosed` 和原初始化快照。

独立复核后追加了三项 test-first 修正：

- pending pull 恰在 count 与 samples 两次远程读取之间完成，不应被误判损坏；采用
  count → samples → count 稳定读取，仅重读观察，不重复 Pull。稳定坏摘要仍失败。
- close 后即使 action trace 成功，丢失/替换广告摘要、phase 或初始化 Evidence 也必须失败。
- 严格识别第八项 service 后的 `(+N omitted)` 展示后缀，不误拒可见 UUID，也不猜第九项。

第一段新增回归先因缺少 `exercise_scan` 为 red；初步实现后 59 项通过。复核新增 9 项方法后为
red（42 failures / 4 errors，包含 subtests），修正后 **68 项通过**。test-only fake 已移到
[`support/bleng_ui.py`](../../../tools/tests/react_native/support/bleng_ui.py)，live runner 不依赖测试模块。
共用 `wait_find_by` 的 sleep 被收紧到剩余 deadline，避免短 lookup 预算被固定 sleep 超出。

本步骤实际验证：

| 命令 | 结果 |
|---|---|
| `pixi run python -m unittest tools.tests.react_native.test_bleng_ui tools.tests.react_native.test_bleng_scan_ui -q` | **68 passed** |
| 上述两模块再加 `tools.tests.react_native.test_android_appium tools.tests.react_native.test_ios_appium` | **79 passed**，两端 locator/foreground/attach helpers 一起验证 |
| `pixi run just react-native formal-host-test` | **48 passed** |
| `pixi run just react-native formal-host-check` | Typecheck、正式 autolinking、无 internal dependency 通过 |

定向日志：`.work/react-native-bleng-ui-tests/{review-boundaries-red,review-boundaries-green,appium-combined,formal-host-jest,formal-host-check}.log`。

### 全量工具耗时的有界诊断，不冒充通过

没有再次盲目延长原 180 秒 quiet suite。本次对同一 canonical discovery 用 verbose 输出诊断：

```sh
pixi run python -m unittest discover -s tools/tests/react_native -t . -p 'test_*.py' -v
```

90 秒预算内，919 项 discovery 中完成 428 项、均为 `ok`；第 429 项尚未完成。仅清理本次进程组，
不计作全套通过。最后显示的 iOS radio restoration 测试单跑 0.235 秒通过，不能将它认定为卡死根因。
最窄 profile 证实两项 Apple stale-inspection synthetic 测试各花约 5 秒等真实轮询 deadline，另一项
18 子场景的 full-suite fixture/JSON/filesystem 工作约 5 秒；未发现需要削弱生产 safety 的理由。
这些累计成本尚不能唯一解释先前 180 秒超时，不宣称已定位全部慢因。

日志：`.work/logs/formal-scan-tools-{verbose-diagnosis,targeted-diagnosis,clock-boundary-diagnosis,native-deadline-profile,full-suite-profile}.log`。
诊断最窄重现设置 20 秒上限并禁止真实子进程；未触发拦截、未执行设备/RF。

## 同日后续：活动所有权组合与 iOS observation 收尾

本步骤仍是软件接线，没有执行设备/RF，也没有修改 Rust、native integration 或产品 JS。
前述 native build fingerprints 不因这些 Python 改动转变为 runtime/RF 证据。

### iOS 非 radio scope

[`IosRadioLease`](../../../tools/react_native/ios_radio_lease.py) 新增
`SCAN_OBSERVATION_SCOPE = "ios-formal-scan-phone-owner"`，与原 radio scopes 共用同一个 canonical
physical UDID 锁，不为 scan 创建第二套可并行绕过的 phone lock。

`finish_observation(primary_error, finalize_owned=...)` 要求可信、有限 deadline 的 host finalizer，
在调用前后校验 owner。正常完成可释放；原 scenario primary error 独立保留，不因成功释放改为 Passed。
finalizer、owner、发布或删除失败进入 quarantine，保留本次错误和原失败；不会自动修改 radio。
只有 observation scope 增加 `primary_error`、`cleanup_errors` journal 字段；两个 radio scope 的
schema/restoration proof 不变。observation 保持 `restoration=unverified`、`control_sha256=None`，
不伪造 ON/restored/resource-zero。两种 finish 方法交叉误用被拒绝且不消耗合法 ownership；
观察模式 quarantine 不能拿旧 radio transcript 释放。

新增 9 个测试先因缺少 scope/API 为 red，随后 observation + 原 lease 21 项通过，再执行相关
radio restoration/quarantine/native-system-loss 组合回归。测试用实际临时文件/inode/owner，
不调用设备；覆盖主失败、callback 前后失权、发布/rmdir 失败和 provisional released 回执撤回。

### 必须是实际已启动的 fixture，而非旧 receipt 文件

[`DirectFixture.require_active_session(plan)`](../../../tests/peripherals/nrf52840/direct_fixture.py)
检查相等 session、port、本地 started/stopping 状态、实际 lease identity 与 broker。
`_started` 仅在 startup/advertising 成功后置真，stop 入口撤销；重复/失败后的 start 被拒绝，
不隐式重新 provision。该查询不产生 serial/BTP I/O，不产生新的 attestation，不证明 RF 存活。
原 failed-image-proof 禁止 teardown mutation 的边界保留。

新增 10 个测试先 red，再与 fixture session/resource lease 共 73 项通过。wire/serial 调用计数及
receipt 内容/mtime 在 guard 前后不变；仅准备测试时使用 fake transport 的真实 start/codec。

### 真实 owner 对象组合到 scan 场景

[`exercise_owned_scan`](../../../tools/react_native/bleng_scan_host.py) 仅接受实际 phone owner、
已启动的 in-process `DirectFixture` 和其 `FixtureSession`。平台从 phone 类型推导，phone run
必须等于 plan session；iOS 必须是 scan observation scope，Android 必须已经只读确认初始 ON、
尚无 off/on/restore/final 动作，并拒绝明显 emulator serial。后者不替代 physical-device selection。

guard 校验 phone/fixture、冻结 request/receipt 原始字节、权威 snapshot hash/live receipt，再调用
必填 App identity callback，随后复查所有权与 proof。name/service 仅由 plan contract 派生。
总预算从组合函数入口计算并将剩余量交给原 `exercise_scan`。函数前后均检查 guard，不自动
取得/释放资源、启动/停止 fixture、创建 driver、install、provision 或执行失败 cleanup。

新增 host 测试先确认缺模块/未实现为 red，随后 12 项通过。其中成功路径贯穿实际 phone 文件锁、
实际 `DirectFixture` active guard、真实 session/receipt validator、vendor codec 和合成 Appium UI；
broker/subprocess/serial SDK 边界均被隔离。该结果不是 live fixture 或物理 RF 证据。

### 本步骤组合验证

在已存在的忽略目录 `.work/ios-observation-lease-validation` 作为本机 `TMPDIR` 下，实际执行：

```sh
pixi run python -W error::ResourceWarning -m unittest \
  tools.tests.react_native.test_bleng_scan_host \
  tools.tests.react_native.test_bleng_scan_ui \
  tools.tests.react_native.test_bleng_ui \
  tools.tests.react_native.test_ios_observation_lease \
  tools.tests.react_native.test_ios_radio_lease \
  tools.tests.react_native.test_ios_radio_quarantine_restore \
  tools.tests.react_native.test_ios_radio_restoration \
  tools.tests.react_native.test_ios_radio_restore \
  tools.tests.react_native.test_ios_system_loss_host \
  tools.tests.react_native.test_android_appium \
  tools.tests.react_native.test_ios_appium \
  tools.android.test_state_control \
  tests.peripherals.nrf52840.test_direct_fixture \
  tests.peripherals.nrf52840.test_fixture_session \
  tests.peripherals.nrf52840.test_resource_lease -q
```

**257 项通过，31.255 秒**；120 秒上限，未超时。相应新测试已由 canonical discovery 自动发现，
未添加重复 test recipe，也未重跑/宣称先前未完成的全量 `tools-test` 通过。

同时实际执行 `pixi run just react-native formal-host-check`（typecheck/autolinking 通过）、
`pixi run just react-native formal-host-test`（2 suites / 48 passed）、`pixi run just fmt`、
`pixi run just lint`、`pixi run just docs-check-mermaid`（通过）。独立只读 review 未发现该
in-process seam 边界内的 mustfix，不据此扩张成 live host 批准。

完整日志：`.work/logs/formal-scan-ownership-{tests,host-check,host-jest,lint}.log`。

### 仍未接通的 live host 职责

- 具体 candidate/PID/boot/App 绑定 callback；当前必填注入边界没有默认成功实现。
- 实际 physical phone selection、WDA/UiAutomator2 预备与既有 driver 的有界 attach。
- 本次 host 的已授权 fixture 生命周期、App 失败 cleanup/成功页保留 finalizer、quarantine 后的
  明确人工处置协议与最终 bundle/ledger verdict。
- native runtime、Hermes/reload、手机→DK RF 及其余 operations/legacy 场景迁移。

因此 CLI/Just 仍 lifecycle-only，`internal` 仍保留独有责任；本轮未执行任何设备、权限/radio
变更、固件 provision 或 RF，未提交 Git。
