# Formal RN scan contract：设计与验证边界

日期：2026-11-03；实现边界更新：2026-11-04。

状态：**设计与测试分层已获用户批准；Rust delivery、产品 DTO/error 投影、正式 Nitro Swift/Kotlin bridge 与 App 手动扫描已接通，两端正式 App/native-test 编译通过；移动平台 runtime、Appium 与 RF 尚未验证，ABI 未冻结。** 唯一计划仍是
[`.plan/PLAN.md` §1.5](../../../.plan/PLAN.md)。本文不是第二份实施计划；批准记录本身不改变正式
[`BlengSessionOwner.nitro.ts`](../../../integrations/react-native-bleng/specs/BlengSessionOwner.nitro.ts)
或 package exports，不将 codegen 成功当作实现、runtime 或 RF 通过。

## 要解决的问题

正式 RN package 原先只有初始化与关闭。迁走 legacy scan/RF 场景需要实际的扫描控制与广告
交付，而不是把旧 probe 的 raw ID、completion listener 和 RF 方法直接搬过来。现在已有正式
scan API 和手动 App 流程，但尚未迁入 legacy 的 fixture/provider、RF 场景和 host ledgers。

已有可复用边界：

- [`Supervisor` 的 `begin_start_scan` / `begin_stop_scan`](../../../crates/bleng-supervisor/src/ffi/operations.rs)
  在完整 context fence 内同步提交，直接返回原 BLeng typed operation。
- [`SessionScanProjection`](../../../crates/bleng-supervisor/src/scan.rs) 由每 owner 的唯一 reader
  同步调用，保留 Started / Advertisement / Stopped 与固定完整 source context。
- 原 operation 的 admission、domain/terminal reconciliation、一次性 wait、deadline 与独立 cancel
  仍由 BLeng 负责。scan delivery 不能再实现一套这些机制。
- projection revoke 只阻止后续投影，不能撤回已经返回或排队的 envelope。

## 推荐：同步提交、typed handle、按需拉取

已批准采用下列概念边界；精确命名及错误映射随实现验证收紧，不据此冻结 ABI：

```text
owner.beginScan(options)
    → { scan } 或 { failure }

scan.waitUntilStarted(timeoutMs)
    → 原 StartScanOperation 的结果投影
scan.cancelStart()
    → 原 StartScanOperation 的显式取消回执
scan.nextEvent(timeoutMs)
    → { advertisement } / { ended } / { failure }
scan.beginStop()
    → { operation } 或 { failure }

stopOperation.wait(timeoutMs)
stopOperation.cancel()
    → 原 StopScanOperation 的结果/取消投影
```

### 为什么不只做 `startScan(): Promise<ScanSession>`

等启动成功才返回 session，确实可以少一个启动等待方法，但启动尚未完成时调用方没有
scan handle。尤其 wait timeout 不代表 native 已停止，不能让“Promise 失败”隐含释放 scan
或自动重试。同步返回 handle 与现有 BLeng 的 begin/wait/cancel 模型一致，也保留独立控制入口。

`beginScan` 返回成功只说明提交被接受，**不是扫描已启动**。只有原 typed start wait 成功才表示
启动结果得到确认。广告或 Started 投影不能代替这个结果。`cancelStart` 不等同于停止已启动
的扫描，也不承诺取消可以回滚平台行为。

这不是额外提供另一套 command ID：JS 只持有对象。command/scan ID 与完整 context 留在 Rust
及 native adapter 内部，用于关联原 operation 和 delivery。

### 为什么先选 pull，不选普通 callback

Nitro 0.37.1 本地源码中的普通 `void` callback 经 `AsyncJSCallback::callAndForget` 向 dispatcher
投递，native 侧没有 JS 处理完成确认。在 reader 前面放一个有界 FIFO，再无节制地转发 callback，
只是把积压移到 dispatcher；它不证明整条交付链有界。

pull 候选规定：

- 每个 scan 最多一个尚未完成的 `nextEvent`，第二个返回明确 busy，不影响第一个 waiter。
- 只有显式 pull 才向 JS 交付下一项；没有 pull 时只使用有界广告缓冲。
- 不创建第二个 `Bleng.next_event` reader，不阻塞现有 reader 等待 JS。
- stop、cancel 和 owner close 独立于 pending pull，不被它持有的锁阻塞。
- pull observation timeout 不停止扫描、不丢弃尚未交付的广告，也不取消 start/stop operation。

这只能约束库拥有的缓冲与主动投递，不能反压 BLE 空口，也不能限制 JS 自己保留已收到对象的内存。

## callback-before-return 与身份关联

接线必须按以下顺序实现并测试，不能依赖运行时调度“通常来得及”：

1. 在提交之前安装本次 scan 的有界接收槽。
2. `begin_start_scan` 返回前发生的投影可以进入临时缓冲，不调用 JS。
3. 取得原 start handle 的 command identity 后，按固定 source context、对应 Started 的 command
   identity 及 scan identity 绑定。不以过滤条件相同或“当前最后一个 scan”匹配。
4. 对已绑定的广告继续检查完整 source context 与 scan identity；不将旧 envelope 重标成新 context。
5. 启动结果仍通过原 start wait 领取；如果投影/控制失败，保留真实错误及必要清理能力，不制造成功。

可提前返回的 handle 解决的是控制/关联能力；它不意味着可以建立无限 pre-start 广告缓冲。
缓存中不能容纳的数据必须进入明确失败路径，不能伪装为没有设备。

## 容量与结束语义候选

每 scan 最多 **64 条广告、256 KiB 拥有的数据**，任一预算达到上限后再入队就明确失败。
数据计费包括嵌套 Vec 元素存储和 payload/UTF-8 字节，在 clone 前检查，避免空 payload 的长集合
绕过限制。结束/错误状态不占广告槽；绑定前停止事件另有 64 项 staging 上限，停止错误文本及
全部 platform detail 字符串另受累计 256 KiB 预算约束，不能因“只有一个 terminal”就无界复制。
这些是有界交付的初始预算，不是吞吐性能结论，也不是整个进程或 JS heap 的内存上限。

首版推荐**不静默丢弃或合并广告**：超限给出明确 delivery failure，拒绝后续广告交付。
这不是原 BLeng/native safety overflow 的降级，也不能被描述成扫描已停止。
首版建议调用方仍通过原 stop handle 或 owner close 显式完成清理；不能因 delivery failure
就自动创建新 scan。候选不新增自动 stop/retry/owner-close 副作用，避免关闭某次 scan 时隐式
断开 owner 的其他连接。若产品希望改成自动收尾，须另行批准触发条件及 stop 失败的升级策略，
不能将其隐藏在交付队列实现中。

正常 ScanStopped、带错误的 ScanStopped 与 owner retirement 必须区分。
推荐原广告在有限队列内保序，真实结束结果在队列耗尽后交付并 latch；重复 pull 可以再次读取
同一结束结论，不再创建 waiter。owner close/revocation 则立即终止 pending pull、清空未交付
广告，不能继续把已失效 peripheral 当作当前可操作设备。不得把 owner close 合成为成功的
native ScanStopped。

以上 terminal priority、overflow 与 close 的竞态需要确定性测试；结束状态不能覆盖已经发生的
本地 delivery failure，也不能用 buffered success 掩盖 overflow。

## stop 失败不一定意味着扫描结束

已有 BLeng 测试区分以下情况，RN 投影必须保留，而不是统一成一个 closed 状态：

| 观察 | 不能错误推断的事情 |
|---|---|
| stop dispatch 被拒绝 | 没有其他结束证据时，scan 仍可能有效 |
| failed stop terminal，没有 ScanStopped | 不能删除 scan reservation；仍需保留重试控制 |
| ScanStopped 带 error | 确实观察到 native scan 结束，但不是成功结束 |
| stop wait timeout | 不证明 native 已停止，也不等于取消成功 |
| stop wait 成功 | 不证明 JS 已消费扫描结束事件，更不证明 owner resource-zero |

源码/回归见
[`runtime/tests/scan.rs`](../../../crates/bleng/src/runtime/tests/scan.rs)。

因此不额外增加一个含糊的 `scan.close()` 与 stop 竞争职责。首版使用显式 stop operation，
整个 owner 的最终收尾继续使用已有 `owner.close()`。Nitro 继承的同步 `dispose()` 不承担异步
BLE cleanup 保证；runtime invalidation 仍是另一条必须独立接好的生命周期链路。

## advertisement 与 opaque peripheral

广告保留已有 contract 中所有数据字段：optional name/RSSI/connectable、service UUID、
manufacturer company ID/payload、service data UUID/payload。

- 二进制采用 `ArrayBuffer`，不编码成字符串或 JS number 数组。
- u16 company ID 和 i16 RSSI 可精确使用 JS number，输入仍需校验整数/范围。
- 不向 JS 暴露 CommandId、ScanSessionId、PeripheralId、context 或 generation。
- 广告携带 opaque peripheral capability，以便后续连接，不提前实现 connect API。
- peripheral 绑定 owner lifetime，而不是 scan lifetime：同 owner 停扫后仍可用于后续 connect；
  owner 退休后失效，禁止给旧 handle 换上当前 context 自动重试。
- 不承诺跨 reload/fresh-owner/process 的设备身份，也不为 JS 对象恒等建立永久缓存。
  Nitro 默认对象 `equals` 不能未经实现就宣传为设备语义相等。

这些是未冻结的产品语义；Rust/桥接测试与两端编译已接线，仍不能把 codegen 或编译等同于
实际 JS/Hermes binary round-trip、移动设备身份行为或 RF 证据。

## 错误不要只剩 Promise rejection message

本地 Nitro 默认异常桥接产生普通 JS Error，不能据此认为 Rust variant 和平台细节自动保留。
候选将预期的 submission/operation/delivery/lifecycle failure 显式投影为 record，并保留平台
`platform/code/name/message` 字段；不暴露原 command identity。

正式 [`BlengScan.nitro.ts`](../../../integrations/react-native-bleng/specs/BlengScan.nitro.ts) 已导出
keyed result、opaque peripheral 与 `ArrayBuffer`；产品错误由 Rust
[`scan_view.rs`](../../../crates/bleng-supervisor/src/ffi/scan_view.rs) 集中映射。Swift/Kotlin 仅转换
DTO、union 和 owned bytes，不重新解释错误或从异常文本提取身份。本地 busy/timeout 不混成
native BLE operation failure；平台细节中的 `platform` 使用诊断字符串，避免 Nitrogen 的 C++
`ANDROID` enum member 与 NDK 宏冲突。实验 spec 仍只是历史 codegen 证据。

## 哪层负责什么

| 层 | 本候选建议的职责 |
|---|---|
| BLeng | 原 typed operation、admission、domain/terminal reconciliation、wait/deadline/cancel、scan reservation |
| Rust Supervisor | 完整 context fence、operation forward、owner retirement；若新增 scan delivery，拥有其绑定、有界缓冲和结束状态，不复制 BLE 状态机 |
| Swift/Kotlin | 唯一 reader 同步转交、必要 DTO/opaque handle 映射、Nitro Promise completion；不各自复制 delivery 策略 |
| JS / formal App | 逐次 pull、业务展示、保留当前 handle 的归属检查、显式 stop/close 与 host evidence 导航 |
| Host | fixture/phone lease、安装/物理控制、RF ledgers、cleanup 与最终 verdict |

Rust delivery 已通过 [`SessionSupervisor::begin_scan`](../../../crates/bleng-supervisor/src/supervisor.rs)
接入原 typed start operation 和现有单 reader 的 projection；具体软件证据及剩余缺口见
[实现报告](../../testing/reports/2026-11-03-rust-scan-delivery.md)。该首段返回 native Rust handoff；
后续正式 JS 接线复用它，不增加第二 runtime、reader 或通用路由 registry，也不把 recovery
advertisement capture 当作普通 scan 流。owner-local weak lifetime bookkeeping 在退休时关闭所有
仍存活的交付端，包括已被新 scan 替换的旧端，不因保留错误/历史结果就逃过 revoke。

`ScanDelivery` 的 pull 本身无 timer。native-only `SupervisedScan` proxy 通过原
`StartScanOperation::observe_with_timeout` 在原 owner runtime 内执行 observation deadline，
不增加全局/per-scan runtime，也不要求 Swift/Kotlin caller 有 Tokio timer。原 operation 保留
executor 而不保活 Hub；proxy 弱持有 Supervisor，不持有 `Arc<Bleng>`。

真实启动失败通过 `StartScanOperation::observe_failure` 的 Rust-only 非消费式摘要唤醒
并释放 observation，不再依赖某次公开 wait 恰好取得错误。内部统一使用
[`OperationOutcomeObservation`](../../../crates/bleng/src/operation_observation.rs)，公开注册类型为
`OperationFailureObserver` / `OperationFailureRegistration`；当前仅扫描启动在提交进入 actor 前
启用摘要，不为其余 typed operation 批量增加公开监听 API。BLeng 从准入拒绝、dispatch 拒绝、
原生结果和取消回执的权威路径锁存一次结果分类；waiter 已超时、drop 或被回收后仍能观察迟到
失败，包括没有 canonical BLE event 的拒绝路径。该能力不复制结果 payload、不占用原 wait，
不新增 UniFFI/Nitro 方法或后台桥接任务，也不取代 native retirement 的 completion latch。
每个 operation 只有一个弱监听槽，delivery 持有可撤销 registration；`State` 裁决结果，`Listener`
封装有效性、撤销与弱接收方通知。回调在摘要锁外只设置失败标记并唤醒，delivery 状态锁内再
清空未交付数据并保留首错。晚注册可以同步回放，不能持有被回调使用的锁注册；registration
撤销不等待已准入回调。owner 退休仍由原 projection lease 撤销，不从 reservation Drop 推断启动失败。

timeout/invalid/duplicate wait 本身不 detach；成功 start 或失败 stop terminal 不结束 delivery。
确认资源可以早于 reader 的 Started receipt，stop 在固定 context fence 内提交。
失败、取消回执和观察关闭均不代表 native 已停止。具体 Swift artifact 执行、Kotlin 静态构建及
尚未验证的移动平台边界见 [FFI candidate 报告](../../testing/reports/2026-11-03-supervised-scan-ffi.md)。
这些 native FFI 类型不直接进入 JS；正式 Nitro 投影隐藏 raw identities，并保留 Rust 的产品错误。
新的 [正式 scan/close 集成报告](../../testing/reports/2026-11-04-formal-rn-scan.md) 区分软件回归、
两端编译与尚未执行的 runtime/RF。

## 初始 codegen 实验及其边界

可复现命令：

```sh
pixi run node spikes/nitro-scan-contract/check.mjs
```

锁定 Nitro/Nitrogen 0.37.1 的本地 TypeScript/codegen 验证：

- 四个候选 HybridObject 完整生成 Swift/Kotlin/C++；零或部分生成会失败。
- 同步提交的 nested HybridObject、独立 start wait/cancel、pull、typed stop handle、
  optional 字段、二进制 payload 数组与 keyed record union 均生成。
- negative example 是合法 TypeScript，但 Nitrogen 明确拒绝 `kind: 'advertisement'` 单值字符串
  discriminant。正例使用 `advertisement` / `ended` / `failure` 不同必需字段的 record union。
- 日志和生成产物保留在每次独立 `.work/nitro-scan-contract/attempt-*`。
- 未安装新依赖，未改正式生成输出，未编译 native，未执行 JS/native runtime、BLE 或 RF。

该实验不验证 union runtime dispatch、错误映射、bytes round-trip 或资源回收。
实现后的必要测试仍包括：callback-before-return、start failure/timeout/cancel、重复 pull、
slow consumer/overflow、stop rejection/late stop、close 唤醒、完整 context fencing，以及两端
Nitro native/runtime 与正式 App/RF 接线；不能只靠 codegen 或 Jest 结束迁移。

## 正式 App 与一次性结果语义

正式 App 的 controller 位于视图切换之上。只有显式 Start scan / Pull next / Stop / Cancel 才调用
对应 API；切换 Tests / Current run / Evidence 不提交命令、不自动 pull，也不清理当前 scan。
广告只保留最近 8 条有界文本摘要，二进制只显示前 16 字节，不把 peripheral 或原始 buffer
保存到 UI state；共享 Evidence 最多保留 32 个 action。

**原 start/stop wait 的结果只能消费一次。** 操作失败和 wait timeout 都不会归还结果槽，cancel
控制则独立保留。stop 失败后，App 不会反复等待同一 handle 并期待成功：只有用户明确点击
`Submit another stop` 才重新提交；新提交被拒绝时保留旧 cancel handle。Rust 在消费前返回的
`input/invalid_timeout` 允许修改输入后等待原 handle，不在 JS 复制 Rust 的超时范围规则。

owner close 返回 typed `BlengCloseResult`，将 `ownerClosed` 与 `failure` 分开。关闭前 busy 与
关闭后 cleanup failure 不再都伪装成可重试异常。后者使 App 退休旧 controls、保留失败并显示
`closedWithError`，不声称 resource-zero 或 host gate 通过；只有下一次显式 Start 才创建新 owner。
旧 pending action 可以结算其原始 Evidence，但不得覆盖已关闭/替换的 scan UI 或新 session。
重复 close 保留首次终态回执，不因 owner 已释放就把清理失败改成成功。

## 共享 Appium 场景与 host 准入

[`run_bleng_ui.exercise_scan`](../../../tools/react_native/run_bleng_ui.py) 是接收既有 driver 和必填
`require_authority` 的 host-injected 场景，不是新的公开 scan CLI。既有 `run/main/formal-host-ui`
仍只执行 lifecycle；不接受 `--scenario scan`、ready JSON 或 receipt 路径来代替真实活动所有权。

场景总预算覆盖 initialize、scan、最多 8 次显式 pull、浏览、一次 stop 和一次 close；输入框的
observation timeout 必须预先与 host 预算一致，不自动修改。每次远程操作前后都检查 host guard，
不确定 click 不重投，失权/失败后不猜测执行 stop/close。等待也受同一总 deadline 约束，但在途
WebDriver 调用仍需 host 设置有限 HTTP timeout。

只有同一条新 pull 广告的 name 与 advertised service 同时匹配才记录目标观察；不是物理设备身份
认证或 host RF verdict。count → samples → count 稳定读取避免异步完成时的撕裂；malformed/缺失
摘要不能通过，省略的 service 不能靠推测命中。最终 Evidence 同时验证原 run、精确 action trace、
scan 摘要、`ownerClosed` 与初始化快照，不能只因 action 文本成功就放过结果页丢失。

[`bleng_scan_host.exercise_owned_scan`](../../../tools/react_native/bleng_scan_host.py) 已将 guard
收敛到实际 phone owner、同 run 的活动 in-process `DirectFixture` 与 `FixtureSession`：从 phone
类型推导平台，期望 name/service 仅由 contract 派生；每次检查 broker、冻结 request/receipt
原始字节、权威 receipt/snapshot 验证，并在 App identity callback 之后复查所有权与 proof。
`DirectFixture.require_active_session()` 只观察本地 started/stopping 状态和真实 broker lease，
不产生串口 I/O 或新的 attestation。未启动/已停止 fixture 即使留下有效 receipt 也不能授权。

普通扫描的 iOS owner 复用现有 phone lock，使用独立 `SCAN_OBSERVATION_SCOPE` 与
`finish_observation(primary_error, finalize_owned=...)`。finalizer 调用前后验证 owner；主失败
独立保留，cleanup/失权/发布/删除失败进入 quarantine，不产生假的 ON/restored/resource-zero。
两种收尾禁止跨 scope 使用，不降低 radio gate 的 restoration proof。released 只代表锁收尾，
不是场景 Passed；观察模式 quarantine 也不能套用 radio restoration transcript 自动解除。

具体 App 身份检查已由同一 host module 的 `pin_android_app` / `pin_ios_app` 提供：固定 phone/run、
driver/session/capabilities 和 candidate，逐命令检查 owner 与同一绝对 deadline，不因不匹配重选
进程。Android 采样 PID、UID/cmdline、start ticks、OS boot UUID 与 installed base.apk hash；iOS
读取 exact install/launch provenance receipts、当前 PID/executable、native boot，并每次执行新鲜
challenge。正式 Apple consumer 只在显式 run-id 参数下于 RN 前启动 responder；不创建 BLE owner，
不进入正式 JS API，nonce 不随 RN reload 或 operation owner 改变。规范请求拒绝重复/转义 key，
Darwin post 的异步响应只对同 boot 的合法 pending 状态做有界只读等待，sentinel 不算 live proof。

两端采样、正式 iOS 编译与 macOS notification 软件证据见[本轮报告](../../testing/reports/2026-11-04-formal-host-app-identity.md)。
iOS 本地 candidate hash 不等于 installed hash，`require_candidate_provenance` 仍必须由本轮受信
managed install/launch owner 提供；旧 receipt、inventory 或正确 PID 都不能单独证明候选字节。
`create_ios_attached_driver` 只复用已运行 WDA，不配置 build/signing/fallback，仍需 host 预备并拥有
WDA/转发端口。频繁 fresh checks 的真机支持/耗时尚未验证，不以缓存旧成功来降低 identity fence。

具体 live host 仍须接通 physical phone selection、本轮 managed candidate provenance、automation
预备/attach、失败 cleanup/finalization 和最终 bundle。App 身份 callback 是可信 host 代码边界，
不是防止任意 Python 调用者传假 callback 的沙箱。Android 的明显 emulator serial 拒绝不替代
实际 physical-device selection；初始化快照也不是 live permission proof。当前仅支持 host 已持有
的 in-process fixture，不将外部 provider 的 receipt 文件当作可借用 lease。组合函数不获取或释放
资源，不启动/停止 fixture，不 provision/install/开串口，因此仍不开放 scan CLI、不执行设备，
也不宣称 RF/legacy ledger 已迁移。

## 已批准范围与实现检查

用户已批准：

1. **同步 begin + 原 typed wait/cancel + 独立 stop operation + 单 pending pull**，不把成功后才
   返回 session 的 convenience API 作为唯一控制入口。
2. 初始 64 条/256 KiB 双重预算；overflow 明确失败、保留控制，首版不隐式 stop/retry/关 owner，
   由调用方显式 stop/close 完成清理。
3. keyed result/error 形状及 opaque peripheral 的 owner lifetime；精确错误映射仍按实际
   Rust variants 收紧，不预先冻结 ABI。

批准设计不等于完成实现或取得 runtime/RF 证据；codegen spike 不能直接充当正式实现，
也不能据此删除 `react-native-internal` 的独有测试或 RF 场景。

### 测试贯通与底层可观测性

正式 RN 操作走真实 Supervisor → BLeng → BleHub → backend 链路，尽量直接委托原 typed
operation，不绕过 context/lifecycle fence。底层完整 contract 则通过各自测试通道覆盖：

| 通道 | 负责证明 |
|---|---|
| Rust 单测/集成测试 | contract、admission、reservation、wait/cancel、delivery 竞态和 Supervisor fencing/recovery |
| 直接 UniFFI/native artifact consumer | Swift/Kotlin ABI、typed handles、错误/二进制、生命周期、打包和单 image |
| 正式 RN consumer | JS/Nitro/Hermes、正式 API 全链路、UI/reload 与适用真机 RF 场景 |

可观测性用于 debug 与区分真实错误，不是另一套控制 API：

- 按责任提供只读、结构化的必要观察，例如 queue 使用量/容量、终止原因、reader 退休与 cleanup
  结论。优先复用已有观察；没有使用者和断言需求时不增加通用 telemetry framework。
- raw ABI、内部关联 identity、资源计数与故障注入优先留在 Rust/native tests 或最窄 test-only seam。
  正式 JS 只取得业务必需的 typed result/error/state；确有产品 debug 需求时再评审安全摘要。
- 不返回可变内部对象、raw Bleng/BleHub getter，不允许诊断方法发命令、清除故障或强制状态推进。
- queue empty、reader stopped、close returned 和 resource-zero 是不同观察；单个 getter 不替代
  既定 retirement/cleanup 屏障，也不把快照变成跨时刻的安全保证。
- 关联日志按需启用且有界；不默认输出设备身份、广告 payload、GATT value 或其他敏感数据。
- 正式 App 的 native tests 如需访问低层，仍使用该 App 已有的同一 combined UniFFI image，
  不增加第二份 BleHub native image，也不复制一个产品 UI App。

具体字段/入口要由实现中的诊断和测试需求驱动。此处批准的是职责边界，不声称这些 diagnostics
已经实现，也不授权将所有 native inspector 自动加入正式 JS exports。
