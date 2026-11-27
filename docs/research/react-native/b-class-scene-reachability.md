# B 类剩余场景的可达性评估

日期：2026-11-21。范围：`react-native-internal` 的 B 类 mode 在正式 managed lane
（`tests/consumers/react-native-bleng` + `tools/react_native/`）里的可达性。结论是候选事实，不改变
[`rn-internal-rf-scenario-migration.md`](rn-internal-rf-scenario-migration.md) 的迁移条件，也不降低任何
gate。

判定沿用那份对照表的四项条件（自有入口 / 该入口的回执 / 按名字失败 / 旧 mode 退役）。这里只回答一个更早的
问题：**需要补哪一类东西**。三类：

- **引擎能力**：Rust/UniFFI 面缺一个真实事实（例如上一轮的连接结束 lane）。
- **产品面**：引擎已经能给，但 App 模型/控件要有新的形状（例如多连接）。
- **不可达**：旧场景保护的性质在新架构里不存在，硬迁移会造出一个假的等价物。

## 逐场景

### `recovery`（3 轮）—— **已迁移，两端真机通过**

旧场景（`App.tsx: runRfRecovery`）每轮：scan → connect → discover → enable notification → 收到一条值 →
**fixture 侧被动断链（`commandId == null` + connection error）** → 断言旧路由静默（该 `connectionId`/
`gattSessionId`/`subscriptionId` 之后不再有任何事件）→ 同一 owner 内再 connect → 下一轮。

新 lane 已具备全部要素，且都不需要新增 API：

- 被动结束：`BlengConnection.awaitEnd`（本轮增量），归因 `unrequested`。
- 同一 owner 内多轮：`stop()` 成功会把 run 置为 terminal，因此同一 owner 可以再起一轮扫描（`canStart`
  只要求旧 run terminal）；`settleEnd` 只退掉 connection/topology/notifications，**不**退掉 per-run 的
  peripheral 观测能力。
- 旧路由静默：`run-status` 的 `link`/`linkEnd`/`notifyTotal`/`scanTotal` 加上 App 的 receipt 账本足以
  断言「结束后没有新值、没有新回执」。

结论：**按 `passive-disconnect` 的形状直接实现**，复用 `PeerDropSeam`、`_require_link_end` 与现有
`_open_live_target`。每轮成本 ≈ 与 `notification` 场景同量级；3 轮在 900 s 内有余量。

一个实现时才发现的事实：**固件在链路建立后会停掉可连接广播，且不会自行恢复**（旧 lane 的 host 是
自己做的）。所以轮间必须有一个显式的 fixture 动作：新增 `DirectFixture.resume_advertising()`（只发
一条 GAP 广播写 + 要求 controller-info 的 ADVERTISING 位回读），scene 用新的 `PeerRecoverySeam`
(`drop()` + `resume()`) 在轮间调用它。不把「peer 会自己回来」当成借口。

预期：这个场景不修改 App；实测也确实没有改。

### `recovery-ten`（10 轮）—— **已迁移，两端真机通过**

旧场景与上面同一实现，只是 `cycles = 10` 并额外核对每轮资源计数。形状不需要新能力，但**iOS 的单步成本**
决定了它不能直接塞进 900 s：

- 实测（PLAN 2026-11-19 记录）：iOS 一次 `ui.click()` ≈ 9.4 s；一轮 recovery 至少 5–6 次点击（扫描、拉取、
  停止、连接、发现、关闭），加 identity 与设备侧往返 ≈ 60–90 s/轮。
- 10 轮 ≈ 600–900 s，只剩零余量，失败会退化成超时。

结论：**轮数参数化**并由场景自己声明所需预算，而不是抬高全局上限：`SceneEvidence.cycles` 与
`recovery_trace(cycles)` 同源，`min_seconds` 声明「这个 scene 跑完至少需要多少 scenario 预算」，runner
在驱动 App **之前**就拒绝不满足它的运行。实测 10 轮在 iOS 2400 s 场景预算内跑完
（`.work/ios-rec10-r12`），Android 一次运行预算上限同时从 1200 s 提到 2400 s —— 否则一条声明 1500 s 的
scene 在那个 lane 上永远无法成立。

### `multi` / `multi-soak` / `multi-fault` —— 两层都已验（Android），soak/fault 待按同一 runner 扩展

旧场景（`multi_rf.ts`）两片 DK：各自 connect / discover / write / read-back / RSSI / subscribe / disable /
disconnect，host 交错驱动，App 只投影最新观测；`multi-fault` 才是它的核心——**一条链路出故障时另一条照常工作**。

这件事的本质是**引擎隔离**，所以证据应该落在引擎层，而不是 workbench 的产品面上。两层现在都有证据：

1. **引擎簿记（已验）**：两条连接各自的 reservation、GATT session、订阅路由与结束 lane 互不干扰。
   `crates/bleng/src/runtime/reservations/tests/connections.rs` 已覆盖多连接准入/路由；
   `crates/bleng-supervisor/tests/ffi_scan/connection_end.rs::two_connections_of_one_owner_end_independently`
   在**产品投影**上验证：同一 owner 两条连接，A 的对端消失后 A 的 lane 报 `unrequested`、B 的 lane 仍开着
   且引擎仍接受 B 上的新操作，B 随后独立结束。
2. **通知投递（本轮新增回归）**：`crates/bleng-supervisor/tests/ffi_scan/notification_view.rs::
a_second_subscription_never_takes_over_the_first_stream` 把「后一条订阅不得接管前一条的通知流」
   锁成契约。它是真机先暴露的缺陷的引擎级复现：投影层原本只有**一个** `current` 通知 lane，
   第二条 `enable` 会替换它，于是第一条订阅的投递被整个静默偷走。
3. **RF/后端双对端（Android 已验）**：真实两片 nRF52840 同时连着、交错 I/O、A 的 peer 断链而 B 继续交付。

#### 第 3 层为什么不放在 BLeng artifact consumer

`tests/consumers/android/bleng` 的构建契约是「只允许一个 `dev.blehub` artifact」（`verifyBlengArtifact`
硬校验），而 `dev.blehub:bleng` 是**引擎-only**：真实 Android 后端在 `dev.blehub:blehub-android`。
于是两不靠：BLeng consumer 没有后端，blehub consumer 又不是 BLeng。

唯一同时组合「引擎 + 真实后端」的模块就是**正式 RN host App**，而它已经有一条**非 UI** 的仪器化 lane：
`tools/react_native/bleng_android_native_notification.py` +
`tests/consumers/react-native-bleng/android/app/src/androidTest/.../BlengNotificationInstrumentedTest.kt`
（stage marker 驱动、host 拥有 fixture/phone、没有 Appium）。本轮把 `multi` 放进同一条 lane：

- host：`DirectFixture` × N（N 由 `--peer` 声明，2..4），各自 lease、各自派生会话 id、各自 close 回执；
- 测试进程：扫一次按声明顺序认领 peer → 各自 connect → 各自 discover → 交错 write/read-back → 各自 subscribe →
  host 让第一个 peer 消失 → 断言**其余每条链路仍收到一条在该结束之后注入的值** → 各自断开 → close；
- 标记词表按链路索引分段（`subscribed:0` / `subscribed:1` …），host 按**完整 stage**去重，
  所以两条链路的同一个动作不会坍缩成一次推进；
- 不碰 App store、不碰 `run-status` 契约、不碰 UI —— 这正是选这条路的理由。

`multi-soak` / `multi-fault` 不需要三份 runner：同一 lane 已声明「N 条 peer、第一条被打断」，soak 是同一场景
按轮重复，fault 是把「peer 消失」换成一次注入的故障。下一步是在现有 runner 上加轮次/故障参数，并证明仍是
同一份回执形状。

**iOS 侧**：同一形状需要的不是 XCUITest，而是在真机上跑 `xctest` 的 host 工具（Android 侧对应 `am instrument`）。
引擎与 Swift 后端已按连接/会话/特征身份隔离，缺的是这个入口而不是能力；在此之前 `multi` 只能记「Android 已验」。

### `reload` / `pending-read-reload` —— 新架构里不可达（建议 C 类）

旧场景保护的是「**native host 拥有 generation**」：`MainActivity` 用 `getReactDelegate()?.reload()` 触发
JS 重载，native 侧 `awaitPendingReloadReady(runId, generation)` 管闸门，`dev.blehub.rn.reload.<uuid>` 通知名
把 reload 绑到 run，App 用 `Linking` 收 arm URL，第 2 代必须看不到第 1 代的任何 ID。

新架构里这个概念**不存在**：owner 就是 JS 通过 Nitro 创建的 `BlengCentral`，JS runtime 一结束它就
被关闭/回收；正式 App 是 release + Hermes 产物，没有 native gate，也没有「跨 reload 的 native generation」。
硬迁移只能靠给产品 App 加 native test seam —— 这是 `AGENTS.md` 明确禁止的。

新架构里真正等价的性质是「owner 退役后不可能再有投放，且资源回到零」，这已经由
`cancellation` 场景 + retirement 契约（`peerCacheEntries`/`callbacks` 等 zero-resource 断言）覆盖。

结论：**C 类**，理由如上。若将来确实要一条 reload 断言，它应该是**新形状**——debug lane（Metro）里
host 触发 `DevSettings.reload()`，断言的是「reload 之后 App 无法再寻址上一个 owner」，而不是 native
generation 的跨代过滤。

## 建议顺序

1. ~~`recovery`（3 轮）~~：已迁移，两端通过。
2. ~~`recovery-ten`~~：已迁移，两端通过（轮数 + 自声明预算）。
3. ~~`multi`~~：Android 已迁移并通过真机；引擎层的「订阅不得互相接管」回归同时落地。
   `multi-soak` / `multi-fault` 与 iOS 入口是后续（同一 runner 加轮次/故障参数；iOS 需要真机 xctest lane）。
4. `reload` / `pending-read-reload`：C 类，写清理由。

## 如果产品真的需要多链路 UI

那是独立一步、按产品能力做，形状是「按身份选，不按位置」：`linkTarget` 为对端广播的 local name，
`linkMatch ∈ {none, invalid, absent, one, ambiguous}` 与 `gattTarget`/`gattMatch` 同构，`links` 只报有界计数。
不要把它当成迁移 `multi` 的手段。

## 不做的事

- 不为 `multi` 在测试里造第二套连接模型：App 是唯一产品面，多连接必须先是产品面的能力。
- 不为 `reload` 往正式 App 加 native 重载闸门：那是把旧 lane 的 test seam 搬进产品。
- 不为任何场景抬高全局预算；预算由场景自己声明并写进回执。
