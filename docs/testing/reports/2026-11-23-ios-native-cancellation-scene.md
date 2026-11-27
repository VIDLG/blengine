# iOS 原生驱动第五个场景：`cancellation` 真机通过（候选事实）

日期：2026-11-23。范围：把 fault-armed 的 managed `cancellation` 场景从 Appium 迁到进程内 XCTest
驱动，并在真机 iPhone 对真机 nRF52840 DK 跑通。只记候选事实与已修缺陷：不改 gate、不冻结 ABI、
不宣称 release 或 Phase exit；`host_gate` 仍为 `pending`。

## 结论

| 项 | 值 |
|---|---|
| 入口 | `formal-host-ios-managed-cancellation`（现在自动走原生驱动） |
| run id | `1fb58bb8-8085-4b9c-b53c-97e09047e8b7` |
| 结果 | `status=passed`、`host_gate=pending`、`rf=cancellation-ui-observation` |
| 驱动 | `ios-xctest-in-process`，plan `cancellation`，**四阶段** 59 步、`xcodebuild` 每阶段 rc 0 |
| 阶段 | `open` 29 步 36.4 s → `hold` 5 步 2.9 s → `queue` 11 步 5.0 s → `settle` 14 步 6.1 s |
| App 树 | `0fe5920d0f3afc14309c42662bf05ceb6b8b7883c96c1a14d2b9634edee14ea9` |
| fixture | `jlink:1050252028`，contract/profile `multifunction-rf-v2-a`，收尾 `stopped` |

13 项 trace 全齐：

```
idle → started → scan:running → scan:advertisement-matched → scan:stopped
     → link:connected → gatt:ready → read:held → read:queued → cancel:queued-accepted
     → read:active-completed → read:recovered → closed
```

App 自己发布的关键帧：

| 事实 | 回执 |
|---|---|
| 被对端持有的读 | `gattRead=waiting;gattReads=1` |
| 排在后面的第二次读 | `gattRead=waiting;gattReads=2` |
| 取消后（被持有的那次仍在） | `gattRead=cancelled;gattReads=1`，`gatt-result` **absent** |
| 取消终态 | `cancel` = `operation/bleng.cancelled: BLeng operation was cancelled` |
| 持有读完成 | `gattRead=done;gattReads=0;gattReadTotal=2`，值 `1 bytes [00]` |
| 恢复读 | `gattReadTotal=3`，值 `1 bytes [00]` |

App 的账本恰好 18 条：三次 `gatt-read` 全部 succeeded，三次 `gatt-read-wait` 中两次 succeeded、
一次 failed 且 detail 点名取消，`gatt-read-cancel` 一条 succeeded，其余全 succeeded，以 `close` 收尾。

## 为什么是四个阶段

`hold`/`queue`/`settle` 各自是一次独立的 App 运行，因为中间的 `arm`、`require_held`、`release`
是 **fixture 命令**，不是 UI 步骤：

1. `open`：建链、discovery、按 UUID 声明目标。**必须先有连接**——实测 fixture 拒绝在无连接时 arm
   （`READ hold requires a connected peer`），所以「开链路」不能和「提交被持有的读」同相。
2. `hold`：host 先 arm，App 再提交读，必须停在 `waiting`。
3. `queue`：host 先确认固件真的持有这个请求，App 再提交第二次读并取消**排队的那一个**。
4. `settle`：host 先 release，App 观察到持有读完成，再读一次证明 lane 没有被污染。

每相开始只有 `activate` + `state=started`（`_resume()`）：App 的 run 由 host 自己 pin 住，后续相
不重开会话、不重扫、不重连，也不从上一相的回执里推断任何东西。

## 一次读的归属：靠标量计数，不靠阶段/存在性

App 在派发命令时清空 `gatt-result`，但**读完以后它会一直显示上一次的值**，而 `gattRead=done` 在
第二次读时本来就还是 `done`。所以「我要的那次读结束了」在取消后无法从阶段或文本上区分——这一轮给
`run-status` 增加标量 `gattReadTotal`（本 run 已结算的读等待数，任何终态）。

顺序保证：App 在 `publish` 结果**之后**才自增计数（`finally`），React 不可能在中间提交渲染，因此
观察到 `gattReadTotal` 前进时，新的 `gatt-result` 渲染也已经在同一帧里。这个不起眼的顺序正是
「等到计数即可信值已渲染」的依据，所以恢复读的等待写成 `gattRead=done & gattReadTotal=3`，而**不**
写 `bytes [`（那会命中上一次读的旧文本）。

## 真机暴露的 App 缺陷：账本一直丢弃失败细节

首轮 `cancellation` 在 host 侧失败于「被取消的等待没有点名 cancelled」，回执里该行是
`14|gatt-read-wait|failed|`。根因在 App：pinned 账本编码器（`src/actionLedger.ts`）读
`entry.detail`，而记录器把结局句子写进 `entry.error`。**任何失败动作的细节从来没有进入过机器账本**，
只有人能读的 `lifecycle-evidence` 散文里有——这正是旧 Appium 场景从散文而不是账本取证的原因。

修法是根因修：编码器的入参改用记录器真实的字段名（`error`，并在类型注释里写明它对应帧里的 `detail`
段），新增一条 App 测试钉住「失败句子必须出现在机器账本第 4 段」。宿主侧也保留严格断言：被取消的
等待**必须**在账本里点名 `cancelled`，否则场景失败。这两条一起使这类字段错配不可能再静默通过。

## 账本规则（有意的失败也严格）

`cancellation` 是第一个账本里合法包含 failed 动作的场景，因此不复用「全部 succeeded」的模板，而是
声明自己的形状：

- 前缀与所有读写场景一致（`start … gatt-discover-wait`），重复动作先折叠（扫描拉取次数是 RF 环境
  属性，不是场景声明）；
- 读侧尾段恰为 `gatt-read, gatt-read-wait, gatt-read, gatt-read-wait, gatt-read-cancel,
  gatt-read, gatt-read-wait, close`；`gatt-read-cancel` 与被它退役的 `gatt-read-wait` 结算顺序
  独立，两种顺序都算本场景，别的不接受；
- 恰好 1 个 failed 的 `gatt-read-wait` 且 detail 点名 cancelled；其余任何 failed 都是故障。

## 这一轮的另一处结构性整理

- `peer_hold.PeerHold`：arm/require_held/release/reject 从 Appium lane 的私有 `_Hold` 提成两条
  lane 共用的中性模块（fixture 命令 + 宿主守卫），Appium 场景与原生 phase host action 用同一份。
- `scene_ios_native.GATT_TARGET_KEYS`：每个 GATT 场景声明它需要的目标字段（`io` = `uuid`+`write`，
  `cancellation` = `uuid`），多给或少给都在构造期拒绝。

## 同 App 树的全场景回归

App 在本轮改了两次（`gattReadTotal`；账本 detail），因此用最终树
`0fe5920d…` 把四个已迁移场景整条重跑：

| 场景 | run | 结果 |
|---|---|---|
| scan | `f309550c-fc80-443d-80a9-461550c73817` | passed，23 步 29.3 s |
| notification | `af36208a-b2d2-496f-96e8-824ccc22638d` | passed，39 步（30+9） |
| indication | `542372a0-a294-4b0b-89d1-dd056cb777ed` | passed，39 步（30+9） |
| io | `bf295476-2aa7-48c5-bbf7-2eb6d142d7ad` | passed，44 步 44.7 s |

`scene_sequence=passed scenes=scan,notification,indication,io`，每场景 `host_gate=pending`。

## 离线覆盖

- `test_scene_ios_native.py` 新增：需要 profile 目标；三阶段/四阶段与 host action 顺序；`gattReads=2`
  才算排队；取消后队列仍为 1；恢复读必须是自己的结算计数（`gattReadTotal=3`）；账本必须有且仅有
  一个 failed wait 且点名 cancelled。测试的假回执现在**只发布 plan 真正读过的字段**，因此「host 断言
  了 plan 没读的帧」这类真机失败在离线就会失败。
- `tools-test`：1763 passed / 21 skipped / 5360 subtests；App Jest：122 passed。

## 边界

- 一次真机通过是**候选事实**：不迁移任何 gate，`host_gate` 仍 `pending`。
- 观察预算由场景在 App 自己的输入上声明（30000 ms），不是继承默认值；它同时要短于固件的 hold 预算。
- Android 仍走 Appium 的 `kind == "cancellation"` 分支，Android 迁移前不可删。

## 复现命令（iPhone）

```sh
pixi run just react-native formal-host-ios-device-test-build --device 00008110-000C61160C39401E

pixi run just react-native formal-host-ios-managed-cancellation \
  --device 00008110-000C61160C39401E \
  --app-tree-sha256 0fe5920d0f3afc14309c42662bf05ceb6b8b7883c96c1a14d2b9634edee14ea9 \
  --xctestrun tests/consumers/react-native-bleng/ios/build/device-derived/Build/Products/BleHubBlengHost_iphoneos27.2-arm64.xctestrun \
  --fixture-contract tests/peripherals/nrf52840/firmware/contracts/multifunction-rf-v2-a.json \
  --fixture-profile tests/peripherals/nrf52840/firmware/profiles/multifunction-rf-v2-a.json \
  --fixture-port /dev/cu.usbmodem0010502520281 \
  --fixture-resource-identity jlink:1050252028 \
  --output .work/ios-native-cancellation \
  --authorize-install --authorize-fixture-start --release-before-install --timeout 1200
```
