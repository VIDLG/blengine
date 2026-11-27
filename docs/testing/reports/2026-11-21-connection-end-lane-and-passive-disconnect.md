# 连接结束 lane（`awaitEnd`）与 `passive-disconnect` 两端真机通过

日期：2026-11-21。范围：B 类第一项 `passive-disconnect`。本文记录**为什么必须加一条引擎能力**、**它的形状**、
**场景如何改写**，以及**两端真机回执**。它是候选事实，不改变任何 gate，也不构成 ABI freeze。

## 1. 缺口（上一轮实测结论）

公开 RN/BLeng 面只有 scan 与 notification 两条 observation lane，没有任何连接状态事件。于是 App 对
**没人要求过的链路结束**只能从某条命令失败里间接得知，`link` 会一直是 `connected`。详见
[`2026-11-20-passive-disconnect-blocker.md`](2026-11-20-passive-disconnect-blocker.md)。

## 2. 增量：`BlengConnection.awaitEnd`

形状按理想架构收紧（不是 §3 草案的照抄）：

```ts
awaitEnd(timeoutMs: number): Promise<BlengConnectionNext>

export type BlengConnectionEndBy = 'operator' | 'unrequested' | 'owner'
export interface BlengConnectionEnd { by: BlengConnectionEndBy; failure?: BlengFailureInfo }
export interface BlengConnectionEndedEvent { ended: BlengConnectionEnd }
export type BlengConnectionNext = BlengConnectionEndedEvent | BlengFailure
```

设计要点，逐条都是语义而非实现细节：

- **lane 与 connect 提交同锁安装**（`SessionObservationProjection::submit_connect`），所以它不会错过自己那条
  链路的结束；绑定发生在**自己那条 connect 命令**的 `Connected` 上，别的连接、别的命令、unsolicited 的
  `Connected` 都不能绑定它。
- **连接是集合不是槽位**：同一 owner 的多条链路共存，`ConnectionLanes` 保存多个 weak 句柄；scan/notification
  仍是单一 `current` 槽位，因为它们本来就互相替换。
- **终止是保留事实而非一次性回执**：重复读返回同一个终止，晚到的观察者也读得到。exactly-once 属于
  connect 结果，那是另一个事实。
- **归因是能力自己的事实，不是对 peer 的猜测**：只有「本能力向平台提交过 disconnect」才会把终止标成
  `operator`，其余一律 `unrequested`（peer、本地栈、adapter reset 都可能）；平台错误原样携带，缺失不等于
  「干净关闭」；`owner` 表示观察结束（owner 退役），**不**声称链路已断。
- **deadline 是调用方自己的界**：超时返回 `observation/bleng.timeout`，终止仍然保留；同一连接最多一个
  pending pull，两个消费者不可能把同一个结束报两次。
- `owner` 退役与既有两条 lane 一起关闭（`revoke()` 持三把锁、固定顺序）。
- 顺带收敛：`observe_with_timeout` 原本在 scan/enable 两处逐字重复，现在落在
  `OperationAdapter::observe_with_timeout`，connect 成为第三个调用者而不是第三份实现。

Rust 侧新增 `ForeignConnectionEnd{By,Observation}`、`SupervisedConnection::await_end`；
`ConnectOperation::observe_with_timeout` 复用连接自己 operation 的 runtime（调用方不需要自己的 timer）。

## 3. App 侧

- 连接成功后在自己那条 lane 上观察；超时重新挂上（deadline 不是事实），任何非超时失败停止观察。
- `publish` 成为 `link` 的**唯一写者**并拒绝让已结束的链路复活（`run.linkEnded`），所以「自己的 disconnect
  回执」与「lane 的终止」谁后到都不会互相覆盖。
- `settleEnd` 是唯一的「结束」写入点，接收回执与 lane 终止走同一条路；平台为结束报的错误成为 run 最新失败，
  没有错误的结束不会清掉真实发生过的失败。
- run-status 新增 `linkEnd` 标量：`none` / `operator` / `unrequested` / `owner`。

## 4. 场景改写

`exercise_passive_disconnect` 不再提交任何命令去发现结束：

1. 活链路上先读一次（链路可用是结束有意义的前提），并记住这条读数；
2. `drop.drop()`（fixture 侧 GAP disconnect，返回前确认控制器已无 peer）；
3. 只等 App 自己宣布 `link=disconnected` + `linkEnd=unrequested`，并要求归因**不是** `operator`；
4. 静默窗口内 `link`/`linkEnd`/`failure` 三元组不变、渲染值仍是第 1 步那条；
5. 需要链路的命令全部退场；
6. 读 App 自己的 receipt 账本，要求**没有** `gatt-read-wait: failed` —— 即没有回到旧路径。

第 6 条是这次改写的关键防回归点：只断言「宣布了结束」的话，把 lane 拆掉、改回「靠失败读发现」的实现
仍然会通过。离线 fake 因此新增 `learns_end_from_failed_read` 缺陷开关。

trace：`idle → started → scan:running → scan:advertisement-matched → scan:stopped → link:connected →
gatt:ready → read:initial → link:dropped → link:announced → link:silent → controls:withdrawn → closed`。

## 5. 离线验证

| 命令 | 结果 |
|---|---|
| `cargo test -p bleng -p bleng-supervisor -p blehub --all-features` | 45 / 300 / 11 / 1 / 35 / 15 / 42 / 22 / 8 / 29 passed，0 failed |
| `pixi run python -m unittest tools.test_api_snapshots -q` | 32 tests OK |
| `pixi run just binding::bleng-snapshot-check` | verified 6 reviewed API snapshots |
| `pixi run python -m pytest tools/tests/react_native -q` | 2200 passed / 21 skipped / 0 failed |
| `npx jest`（formal host） | 116 passed |
| `pixi run just fmt` / `lint` / `docs-check-mermaid` | 全绿 |

新增回归：`connection_delivery::tests::*`（绑定、归因、保留终止、第二 pull 被拒、owner 退役、进度态不结束）、
`ffi_scan/connection_end.rs` 6 项（真实 event reader、真实 revoke、真实 connect 失败不当作结束）、
Jest 3 项（无命令宣布、平台错误被保留、deadline 重新挂起）、
`test_bleng_passive_disconnect_ui.py` 6 项（含 4 种缺陷）。

## 6. 真机回执

Android（`3ac5cc09`，APK `f41bfb07bde225e39a1f29c3719becbaca46d333c55bea913dda75939a83d3c8`，
bundle `.work/android-pd-r11/6e5a647f-3baf-415e-8685-8de07d087082`）：

```
formal_android_managed_scene=passed scene=passive-disconnect scope=formal-android-managed-passive-disconnect
scenario_status=passed session_status=passed phone_status=passed fixture_status=passed
cleanup_errors=[] phone_cleanup_errors=[]
fixture_close.advertising_inactive=true resources_closed=true
marker=link:dropped → marker=link:announced  (带命令驱动的间隔，见下)
```

iPhone（`00008110-000C61160C39401E`，App 树 `a90d0314…`，bundle
`.work/ios-pd-r11/bc7e697f-79d4-4e1f-b87e-6d0d7e379208`）：

```
formal_ios_managed_scene=passed scene=passive-disconnect scope=formal-ios-managed-passive-disconnect
scenario_status=passed session_status=passed phone_status=passed fixture_status=passed
cleanup_errors=[] phone_cleanup_errors=[]
fixture_close.advertising_inactive=true resources_closed=true
```

两端 `link:announced` 都不是命令驱动的：中间没有任何 receipt，`host_gate_status=pending` 是设计状态
（host gate 只由最终 bundle verdict 给出）。

注意 scope 已经是平台名：这是本轮 §7 修复后的产物；§6 初稿当时引用的 `r6`/`r10` bundle 早于修复，仍是旧命名。

### A 类回归（同一个 App 产物，同一台 fixture）

新的 observer 在每次 connect 后都会挂上，因此顺手用 `io` 确认正常路径没被干扰：

```
formal_android_managed_scene=passed scene=io  bundle=.work/android-io-r6/5ba22c5f-58dd-4ab1-a105-de44fde52637
formal_ios_managed_scene=passed     scene=io  bundle=.work/ios-io-r6/d555d983-cb72-4a8f-bd14-d2a45977d18d
```

## 7. 已修复：证据 scope 必须点名平台

初稿把两端 bundle 的 `scope` 写成 `formal-android-managed-*` 常量（上一轮 `ios-cancellation-r5` 也一样）：
**iOS 回执里写着 Android scope**。这是 producer 声明错误，不只是措辞。

修法按「一条事实一个来源」：

- `SceneEvidence` 不再存两个 scope 字符串，而是从 scene 名派生：`scope(platform)` →
  `formal-{platform}-managed-{name}`，`inner_scope` → `formal-rn-prepared-{name}`（内部 prepared run 本来就是跨平台共享，所以不带平台）。
- 未知平台是**被拒的声明**，不是更长的字符串：`MANAGED_PLATFORMS` 之外的参数直接 ValueError。
- 三个使用点一起改：两端 runner 的 request/result，以及 bundle 校验（`_result`/`_request`/`_scene_for`），
  后者现在用 bundle 自己声明的 platform 推回 scope，因此「用别的平台的 scope 回放」会直接失败。
- 十个 scene 定义里的 20 行字面量随之消失。

新产物已经验证：`formal-{android,ios}-managed-passive-disconnect` 各自出现在对应平台的回执里。
较早的 `r5`/`r6`/`r10` bundle 保持原样作为历史记录，不重写。

## 8. 后续（同日完成）：`recovery`（3 轮）与 `recovery-ten`（10 轮）均已迁移并两端通过

同一轮的引擎增量让 B 类的第二项也变成了观察题：`recovery` 需要的四个要素（被动结束、同一 owner 内多轮、
旧路由静默、可再次到达 peer）在 App 侧已经具备，**不需要再改 App**。

实现中暴露并修掉一个真实假设：**固件在链路建立后停掉可连接广播，且不会自行恢复**。所以轮间必须有显式
的 host 动作：`DirectFixture.resume_advertising()`（一条 GAP 广播写 + 要求 controller-info 的
ADVERTISING 位回读，peer 仍在时拒绝），scene 通过新的 `PeerRecoverySeam`（`drop()` + `resume()`）在轮间
调用它。旧 lane 的「host ACKs passive disconnect」就是这件事。

新场景 `exercise_recovery`：每轮 scan → link → discover → **活链路读一次** → peer 自己断链 → App 宣布
`link=disconnected` + `linkEnd=unrequested` → 需要链路的命令退场 → 下一轮（同一 owner）。失败时会把 App
自己的 receipt 账本带进错误句子里，因为这一层坏掉的往往就是「哪一步的命令失败了」。

```
android  recovery      passed  scope=formal-android-managed-recovery      .work/android-rec-r11/d8c84c03…
ios      recovery      passed  scope=formal-ios-managed-recovery          .work/ios-rec-r11/feceb2a0…
android  recovery-ten  passed  scope=formal-android-managed-recovery-ten  .work/android-rec10-r12/a9fdd7a1…
ios      recovery-ten  passed  scope=formal-ios-managed-recovery-ten      .work/ios-rec10-r12/14c76a04…
trace=idle, started, cycle:{0..N}:{scan,link,dropped,announced}, closed   (N=2 / N=9)
cleanup_errors=[] phone_cleanup_errors=[] fixture_close.advertising_inactive=true resources_closed=true
```

`recovery-ten` 现在是一条独立 scene（`SceneEvidence.cycles = 10`，trace 由同一函数派生），并且
**自己声明所需预算**（`min_seconds = 1500`）：runner 在驱动 App 之前就用它拒绝预算不足的运行，而不是在
第 7 轮才失败。Android 的一次运行完整预算上限随之从 1200 s 提到 2400 s（与 scene 天花板和 iOS 的允许值
一致），否则一条声明了 1500 s 的 scene 在这个 lane 上永远无法满足 —— 这是修正不一致，不是放宽 gate。

`recovery-ten` 与 `multi*` / `reload` 的结论见
[`b-class-scene-reachability.md`](../../research/react-native/b-class-scene-reachability.md)。

## 9. 下一步

- `multi*`：需要产品面决策（连接列表 + 目标选择，或降级 C 类）。
- `reload` / `pending-read-reload`：建议 C 类，理由已写入可达性评估。
- `react-native-internal` **仍不能退休**：B 类完成 3/5，C 类未核对。
