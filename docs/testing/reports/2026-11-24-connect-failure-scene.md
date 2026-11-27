# `connect-failure` 场景：一个连不上的连接，以及它抓到的"报告了却写进机器面之外的失败"

日期：2026-11-24

范围：新增第 14 个 managed 场景 `connect-failure`，它在真机上暴露并修掉一个产品缺口；同一份候选上
Android 全序列 14/14 通过。iOS 尚未跑。

## 这条场景要证明什么

`passive-disconnect` 证明"没人问过的结束要如实宣布"，`disconnect` 证明"自己发起的结束要归于操作员"。
缺的是第三种：**一个根本没建立起来的连接**。App 的承诺是把它当成失败报告，而不是：

- 停在"正在连接"（超时被当成成功）；
- 声称 `link=connected`；
- 发明一个结束（`linkEnd=unrequested`/`operator`）——没开始就不该有结束。

场景步骤：扫到 fixture 的广告（App 因此拿到一个可寻址的 peer）→ host 撤掉广告并**在控制器上证明**
ADVERTISING 位已清零 → App 点 Connect → 断言。

## 真机发现的两个缺陷

### 1. 机器面把连接失败说成 `failure=none`（产品面）

第一次真机跑死了在推导里：

```text
refused.failure is 'none', not 'operation/bleng.timeout'
```

App 自己的 ledger 明确记着 `8|connect-wait|failed|operation/bleng.timeout: BLeng operation timed out`，
人看的 `scan-error` 面板也写着同一句，但 `run-status` 的 `failure` 是 `none`。根因在 `App.tsx`：

```ts
failureText: scan.view.notifyError ?? scan.view.gattError ?? ''
```

投影只合并了通知与 GATT 两条 lane 的错误，**漏掉 `view.error`**——而扫描与连接/断链 lane 的失败只写
`view.error`。于是连接失败在机器面上不存在。

这正是 managed 场景存在的理由：人眼看到错误、记录里有错误、断言层看到的却是"什么都没发生"。
修法：

```ts
failureText: scan.view.error ?? scan.view.notifyError ?? scan.view.gattError ?? ''
```

`view.error` 领头的理由是它是 run 级（扫描/连接/断链）的失败，也是唯一能承载连接失败的那一条；
`Connection.test.tsx` 两个既有失败用例各加了一条 `failure=operation/bleng.bleng.busy.peripheral` 断言，
把"面板与机器面必须说同一件事"钉住。

### 2. fixture 侧没有"撤下广告但保持会话"的入口（host 面）

原有能力只有 `resume_advertising()`（重新可发现）与 `stop_advertising_and_close()`（关会话）。
新增 `DirectFixture.withdraw_advertising()`：一次 GAP stop + 控制器 info 回读（ADVERTISING 位必须清零）、
拒绝已连接的 peer（那时广告早已因链路停掉，回读证明不了任何事）、回读不支持则是显式 evidence gap。
它是 `resume_advertising` 的镜像，会话保持存活。

`connect-failure` 的 host action 因此是 `withdraw`（`scene_native._host_action`），与 `publish`/`drop` 一样
在 envelope 的 authority 回调之内执行。

## 为什么声明 30 s 预算（这一条是踩出来的）

第一版用 App 默认的 5 s 观测预算。App 的 `wait` 超时只结束 **JS 等待**，**native 的 connect 仍在对端
控制器里挂着**（`pending.ts` 从不因超时取消操作）。于是：

1. 场景在 25 s 结束，App 被 force-stop；
2. 下一次运行让 fixture 开始广播；
3. 那个残留的 initiator 立刻连上，DK 上出现了不属于任何运行中 host 的 peer；
4. 下一场（`scan`）因 `fixture startup refuses an unowned connected peer` 失败，`scan-start-wait` 也红了。

改为在计划里声明 `CONNECT_FAILURE_TIMEOUT_MS = 30000`（`_prefix(..., declare_timeout=True)`，与
`cancellation` 同一机制），让平台有机会自己答完（失败或 `133`），native 操作在场景内落定。真机上
场景片从 25 s 变成 51 s，收尾后 DK 的 `peer_present=False`，紧随其后的 `cancellation` 正常通过。

## 推导里的两条独立证人

| 检查 | 意义 |
|---|---|
| ledger 里失败的动作**只能**是 `connect-wait`，且 `connect` 提交为 succeeded | "提交被接受、等待失败"——不是没提交，也不是成功了 |
| `run-status.failure` == 从该记录里读出的身份 | 同一句失败在两个证人下的同一种写法（`android/133` 或 `domain/code`） |
| `link=none` 且 `linkEnd=none` | 没开始就没有结束；静默窗口再守住 6 s，并比较 `failure` 前后一致 |
| `CONTROLS_REQUIRING_LINK` 全部退场 | 失败之后不该还提供只能失败的命令 |

`_recorded_failure_identity()` 直接复用 `runStatus.ts` 的两种形状（平台标记优先、否则 `domain/code`），
所以平台拒绝（`gatt/... [android/133 ...]`）与自身超时（`observation/bleng.timeout`）都能被同一个断言覆盖，
场景里没有任何平台常数。

## 真机结果（Android，同一候选）

| 项 | 值 |
|---|---|
| 单场景 | `.work/android-native-connect-failure/2026-11-24-d`，`status=passed`，trace 11/11，场景片 51 s |
| 全序列 | `.work/android-native-sequence/2026-11-24-h`，**14/14** `status=passed` / `integrity=passed` / `host_gate=pending` |
| App APK | `298e6c27f5550e268eb340c42ae43e4d65e954d347913759379818fc0557fd0b` |
| test APK | `0ab5572c401a9a5f0d1ea2f8b795f7b3a9e09bd3a33b56129a0dc746f03fe289`（Kotlin 未改） |
| 序列顺序 | scan, notification, indication, background, io, disconnect, **connect-failure**, cancellation, write-reject, ccc-enable-reject, ccc-disable-reject, passive-disconnect, recovery, recovery-ten |

真机上该场景记录的失败身份是 `operation/bleng.timeout`（App 自己的预算），静默窗口与 close 后都保持不变。

## 顺带的 lab 事故与恢复（都是审计路径，不是 `rm`）

- 一次运行在 `fixture.start` 失败后留下**被隔离的 phone lease**（这是 sealed-failure 契约要求的），
  后续运行因此 `FileExistsError`。新增 recipe `just android-release-stale-lease`（包
  `tools.android.release_stale_lease`）——它要求名字与 `owner.json` 的 run id 一致、且记录的 host pid 已死，
  再写 `stale-release-<run>.json` 之后才解锁。
- DK 上出现了一个**不属于任何 host 的已连接 peer**（`peer=01b9ad644db45e`，`boot_nonce` 与上一次运行相同），
  fixture 因此拒绝启动。手机侧已确认无 GATT 连接、无 App 进程；对 DK 做了一次 **J-Link 软复位（只复位、
  不写 flash）**，`boot_nonce` 变为 `9e572a2fe0a3fbb18ab0362e402b6c8d`，`peer_present=False`，之后全序列通过。
  复位后需要先 `NrfDoctor.ensure_services()` 才能读 fault limits（否则 `NOT_READY`）。

## 守卫（不需要设备）

- `tools/tests/react_native/test_scene_semantics.py`（+9）：完整 trace 与 peer 侧只有一次 `withdraw`、
  投影与记录不一致、`link=connected`、`linkEnd=unrequested`、平台状态形态（`android/133`）、
  等待成功（不是失败）、失败之后还有动作、记录里读不出身份、静默窗口未被采样。
- `tests/peripherals/nrf52840/test_direct_fixture_passive_close.py`（+5）：撤广告成功且会话存活（还能
  `resume_advertising` 再 `close`）、已连接 peer 被拒、回读不支持是 evidence gap、控制器没照做是失败、
  回读长度错是失败。
- `test_scene_sequence.py`：顺序钉住 `io < disconnect < connect-failure < cancellation`。

## 已验证（本机）

```sh
pixi run python -m pytest tools/tests -q                     # 1024 passed, 21 skipped, 2893 subtests
pixi run python -m pytest tests/peripherals/nrf52840/test_direct_fixture_passive_close.py -q   # 59 passed
pixi run just react-native::formal-host-test                # 137 passed
pixi run just react-native::formal-host-check
pixi run just fmt && pixi run just lint
```

注：`tests/peripherals/nrf52840` **整目录**跑时 `test_system_loss.py` 有 108 项失败（`session snapshot
changed after preparation`）——这是本次改动之前就存在的 worktree 状态，与本轮无关，单独跑该文件即可复现。

## 剩余

- iOS：`connect-failure`/`disconnect` 都需要重建设备测试包后跑一次。
- `dual-subscription`：需要 App 支持多条并发订阅 lane（当前 `run.notifications`/`notifyPhase`/`notifyTotal`
  是单槽），属于机器面设计决定，未开工。
