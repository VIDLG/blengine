# Managed fault-reject scenes: offline semantics and evidence boundaries

日期：2026-11-20。范围：新 lane（`tests/consumers/react-native-bleng` + `tools/react_native/`）里
A 类最后三项——`write-reject`、`ccc-enable-reject`、`ccc-disable-reject`——的场景语义与离线验证。
**本文是候选事实**：它记录实现了什么、离线证明了什么，不把任何真机 gate 升级。

## 1. 结论

- 三项场景已实现为独立 managed 场景，与 `scan` / `notification` / `indication` / `io` / `cancellation`
  共用同一固定命令面、同一 App 与同一 evidence 词汇表；每项各有 Android 与 iOS 的 just recipe。
- **两端三项均已在真机通过**，每端一个冻结产物：
  - Android：APK `33a60d3aedcfb449726fe7afccf940be82fa49d13d36eaae4017a035716d8b32`，
    `write-reject`（`6daf11eb`）、`ccc-enable-reject`（`1fbf59db`）、`ccc-disable-reject`（`1613527b`）
  - iOS：App 树 `2db7b1e653e1ed25e6bc95b6bb7e1c6718570ec3f04e4fd5bddf10b679c51089`，
    `write-reject`（`f3ebcb1b`）、`ccc-enable-reject`（`e11be9a5`）、`ccc-disable-reject`（`9f23dce1`）
  - 六条全部 `scenario/session/phone/fixture=passed`、`cleanup_errors=[]`、`phone_cleanup_errors=[]`、
    fixture `advertising_inactive`+`resources_closed`；`host_gate=pending` 是设计状态。
- 固件侧 hold seam 从「只支持 read」泛化成「read / response-write / CCC」三个操作共用一个引擎与
  一个状态机，`support/fault_emulator.py` 是这一事实的离线模型（原 `read_fault_emulator.py` 改名）。
- 离线：`tests/peripherals/nrf52840` 545 项、`tools/tests/react_native` 2187 项全绿。
- **iOS 未跑**。所以 `react-native-internal` 仍不能退休。

## 2. 一个 hold，三种操作

`DirectFixture.arm_hold(operation, role, requested)` 是唯一入口；`arm_read_hold` / `arm_write_hold` /
`arm_ccc_hold` 只是声明式别名。

| 事实 | 由谁证明 | 为什么不能替代 |
|---|---|---|
| 精确请求被停住 | firmware `HELD` + 绑定时的 `before` | ARMED 只说明引擎就绪；普通读/写不会被捕获 |
| 请求被拒 | firmware retired receipt：`winner=REJECT`、`INJECTED_REJECT`、无 `CALLBACK_INVOKED`/`COMMIT_OBSERVED`、`after == before` | 平台报错也可能是本地能力/超时；payload 相同也不等于 commit |
| 请求被提交 | firmware retired receipt：callback + `COMMIT_OBSERVED` + `after == requested` | `perform_write()==0` 本身不是提交 |
| CCC 句柄 | server 自己的有序属性表（value handle 之后、下一个声明之前的唯一 `0x2902`，权限 3） | profile 的布局声明不是已安装镜像的事实；按位置猜更不是 |
| App 侧失败 | App 自己的 phase + Evidence 回执，且细节必须点名 peer 的 ATT status（`REJECT_ATT_RESULT = 0x0e`） | 「App 报错了」不足以说明是谁拒绝的 |

`REJECT_ATT_RESULT` 写进 `fault_control.py` 而不是散落在场景里：STATUS 没有这个字段（`normal_att_result`
记录的是从未执行的正常路径），而平台会把收到的 ATT status 原样投影成自己的错误码，所以断言必须引用
固件契约常量，而不是字面量。

## 3. 三项场景各自的判别点

**`write-reject`**：读初值 → host ARM 精确 payload → 点写 → `HELD` → host REJECT → App 报失败（点名 14）
且没有写回执 → 读回仍等于初值 → 同一次写成功 → 读回等于请求。Evidence 另证两次提交、恰好一次失败 wait。
读回是这里唯一能区分「被拒」和「被拒但偷偷写入」的证据；离线用 `refuse_pending_write(stores=True)` 覆盖。

**`ccc-enable-reject`**：ARM 精确 enable u16 → 点订阅 → `HELD` → REJECT → App 必须以 `notify=failed`
结束该次尝试（`subscribed` 是错的）→ 再订阅成功 → host 注入一条值且必须被 App 拉到（enabled 本身不是投递）
→ disable → `ended`。

**`ccc-disable-reject`**：先建立订阅并收到一条值（disable 的前置条件）→ ARM 精确 disable（u16 `0x0000`，
被拒的 no-op 不被接受）→ REJECT → 订阅仍 `subscribed` → **再注入一条值并必须被拉到**（这是与「订阅悄悄
死掉」的唯一分界）→ 第二次 disable 成功 → `ended`。

## 4. 真机跑出来的三件事

1. **写的提交回执从未 settle**。`write()` 拿了 `beginWrite` 的 capability 就直接去 await wait，没给
   `gatt-write` 提交结算；证据里它永远是 `pending`，与一个从未被受理的提交长得一样（`read()` 早就有
   这条规则并写了注释）。场景的“恰好两次提交”断言在第一次真机跑就把它抓了出来。修在
   `useManualScan.ts`，并补了 Jest 回归。
2. **被拒绝的 enable 会把 observation lane 留在 native 侧**。重试订阅直接失败为
   `delivery/delivery.busy: another notification lane is still attached`，永远到不了 peer。现在失败分支
   调 `detach()`（只结束 observation，不声称 disable）并清掉本地的 lane 引用；Jest 回归断言“被拒 → detach
   → 重试成功”。
   **后续**：`delivery.busy` 本身是通知 lane 单槽的产物，这条根因已消除（订阅 lane 现在是集合，见
   [`2026-11-21-multi-link-isolation.md`](2026-11-21-multi-link-isolation.md)）。`detach()` 保留——
   一个从未 enable 的 lane 不该活到 owner 退休，但它已不再是重试被接受的前提；
   `tests/ffi_scan/notification_view.rs::a_refused_enable_does_not_block_the_retry_on_the_same_characteristic`
   锁住了“不 detach 也能重试”。
3. **pinned 区放不下第二行散文**。把 `notify-error` 挪进 pinned console 后，iOS 上多出的两行把
   `pull-notification` 推出了屏幕（`is_displayed=false`），场景在 `ccc-disable-reject` 中就卡在
   “rendered but not on screen”。修正方式不是去缩小字号，而是把机器需要的东西变成标量：
   `run-status` 新增 `failure=<platform>/<code>`（从 App 自己的失败句子里派生，同一渲染趟），
   散文回到内容区给人读。场景改读标量，pinned 区不再因错误长度而长高。
3. **托管 CCC descriptor 的 getter 读的是 stored default，不是 central 开启的状态**。
   `ccc-disable-reject` 第一次跑在 `arm_ccc_hold` 就以 “no-op on the current value” 结束：App 明明
   `subscribed` 并收到值，descriptor 读回仍是 `0000`。这不是场景该猜的：host 现在把已经开始订阅所
   意味着的 enable 值声明为 pre-state，固件仍在 capture 时重查，声错就是 never-HELD 而不是 pass。
   `Wire.ccc_getter` 在离线模型里表达了这个差别（`None` 表示 getter 与 managed 状态一致）。

## 5. 测量驱动的两处修正

1. **`notify-error` 移到 pinned console**。订阅命令的失败详情和写的失败详情是同一种事实（「我刚下的命令
   结果如何」）。它原来在可滚动内容区，iOS 会把屏幕外元素报成 `is_displayed=false`，场景读不到。移动后
   CCC 场景沿用与 `io` 相同的「零手势读回执」判据。
2. **拉取等待不再把「屏幕上有错误」当成「这次拉取失败」**。被拒的 disable 会在界面上留下错误，而
   `disableNotifications` 只在成功时清它；因此等待循环改为和进入时的错误文本比较，只有**新的**错误才算
   这次拉取的失败。这不是放宽：拉取另有计数推进、`ended` 终止与回执三重判据。

另外把 `disable_settled` 收紧为「存在**成功**的 disable 回执」——失败的 disable 不会退役订阅，离线模型
原先把它当成已退役，正好把 disable-reject 的核心判别点抹掉。

## 6. `ccc-disable-reject` 的 iOS 分叉与选择（B）

iOS 第一次跑这条时，被拒的 disable 之后 App 的下一次 pull 以 `observation/bleng.timeout` 结束，
而固件回执同时证明 **peer 仍 enabled**（`after == before` = `0001`）；Android 同一步则继续投递。
最可能的机制是 CoreBluetooth 在 `setNotifyValue(false)` 得到 ATT 拒绝时已清掉本地通知状态
（**推断**，未读 CB 源码、未用 sniffer 验证）。

这在产品上是一处分叉而不是场景缺陷：**App 在那一刻既不能声称“已停用”，也不能声称“仍在订阅”**。
选定方案 B（不弱化契约）：

- `notify` 新增阶段 `unverified`：被拒的 disable 之后 App 只能报这个；一个真正到达的值把它解成
  `subscribed` 并退掉那条错误，超时则保持 `unverified`（超时不是“结束”的证明）。
- 场景在被拒后做**一次有界探针**：值到达则要求 App 报 `subscribed`，`observation` 类失败则要求仍
  `unverified`，`ended` 直接失败。trace 记录 `notify:probed`——平台结果不进 trace，因为它是平台事实；
  契约是“App 不许报错另一边”。
- 旧的 `subscribed` 默认值现在是**缺陷**，有专门的 Jest 与离线场景回归守着。

两端用新语义重跑后全部通过（见 §1）。

- **iOS `write-reject` 与 `ccc-enable-reject` 已真机通过**（树指纹
  ``8b3a32e81e7537bf887666b36163d93ee46243c19a10993df70aea647dc0ca06``；run
  `0c81faf0` / `455b27d3`），`cleanup_errors=[]`、fixture 干净退场。
- **iOS `ccc-disable-reject` 未通过，拿到的是一个平台事实而不是场景缺陷**：被拒的 disable 之后，
  App 的下一次 pull 以 App 自己的 `observation/bleng.timeout` 结束（run `71d118cc`，观测停在
  `notify:rejected`）。两侧证据因此分叉：**固件回执说 peer 仍 enabled**（`after == before` = `0001`），
  **App 说收不到值**。Android 同一步是继续投递。最可能的机制是 CoreBluetooth 在
  `setNotifyValue(false)` 得到 ATT 拒绝时已清掉本地通知状态（**推断**，未读 CB 源码，也未用 sniff 验证）。
  可选项：
  (A) 把“被拒后仍投递”明确成平台差异（只在 Android 上作为判别点，iOS 只要求失败可见 + 重试成功）；
  (B) 视为产品缺陷：被拒的 disable 后订阅状态未知，App 不该继续声称 `subscribed`。
- 场景侧同时改了两处健壮性：失败身份走 pinned `failure` 标量（不再读散文），单次 pull 有自己的上限，
  因此“lane 不再投递”现在 45 s 内点名而不是吃掉整个 run 预算（实测 169 s 含重试，之前是 1800 s）。
- `notify-error` 换位置已在 Android 真机验证（三条场景都读它）；iOS 的 tree 是否能读到仍需真机确认。
- 订阅失败文本里的 ATT status：Android 已确认是十进制的 `14`；iOS 投影的是 `CBATTErrorDomain` 的
  `code`，也应为 `14`，但未实测。
## 7. 已知缺口

- 固件 getter 对托管 CCC 读回 stored default 这处限制，应在下一版 image 里修或写进 capability 声明，
  否则任何依赖“读回 CCC”的场景都会在真机上误导。本轮的 host 声明 + 固件 capture 重查只是不再依赖它。
- iOS 在拒绝 CCC 写之后是否总是丢掉本地投递，只有一台设备、一个 iOS 27.2 的证据；其他机型/版本需实测。
- `cancellation` 等既有场景应该在新增的 `failure` 标量上补一层断言（当前它们只用坐标）。
