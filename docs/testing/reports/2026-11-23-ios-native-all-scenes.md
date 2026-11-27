# iOS 原生驱动收尾：其余六个场景迁移 + 全场景同一候选真机通过（候选事实）

日期：2026-11-23。范围：把 managed iOS 门禁最后六个场景（`write-reject`、`ccc-enable-reject`、
`ccc-disable-reject`、`passive-disconnect`、`recovery`、`recovery-ten`）从 Appium 迁到进程内 XCTest
驱动，并在真机上用**同一份最终 App 产物**把全部 11 个场景整跑一遍。只记候选事实与已修缺陷：不改
gate、不冻结 ABI、不宣称 release 或 Phase exit；`host_gate` 在每个场景仍是 `pending`。

## 结论

同一 App 树 `abf0377afad38a21a435345e68bbd318369c23ad7e42c2f7f0c2027452a18bab`、同一 xctestrun、
同一真机 iPhone 14 Plus（iOS 27.2.1）、同一真机 nRF52840 DK（`jlink:1050252028`，v2-a）：

| 场景 | run | 结果 | 步数 | 步骤耗时 | 阶段 |
|---|---|---|---|---|---|
| scan | `0e198e1f` | passed | 24 | 29.6 s | 1 |
| notification | `38c8a062` | passed | 40 | 41.6 s | 2 |
| indication | `56904601` | passed | 40 | 41.7 s | 2 |
| io | `5ec44a4f` | passed | 45 | 45.4 s | 1 |
| cancellation | `a50b580a` | passed | 59 | 50.9 s | 4 |
| write-reject | `0b8daeca` | passed | 58 | 50.3 s | 3 |
| ccc-enable-reject | `b08ffd70` | passed | 51 | 49.4 s | 4 |
| ccc-disable-reject | `a17fe55b` | passed | 56 | 58.6 s | 5 |
| passive-disconnect | `efc09ca8` | passed | 50 | 49.2 s | 2 |
| recovery | `eedab581` | passed | 94 | 68.1 s | 6 |
| recovery-ten | `91bad2fc` | passed | 276 | 162.0 s | 20 |

全部 `cleanup_errors=[]`、fixture 收尾 `stopped`、每个场景都过 `verify_scan_bundle`。**11 个 managed
iOS 场景现在都跑在进程内驱动上**；Appium 只剩 Android 端使用。

## 六个场景各自的语义（与 Appium 版逐条对齐）

**`write-reject`**：三阶段 `open`（读基线 + 声明 payload）→ host `hold-arm` → `dispatch`（提交被固件
park 的写）→ host `hold-reject`（确认持有 + 用固定 ATT status 拒绝）→ `settle`（读回值未变，再用同一写
成功并读回等于请求）。App 账本恰好一个 failed `gatt-write-wait` 且点名 `apple/14`；`dispatch` 阶段的账本
在那一刻显示 `gatt-write-wait|pending`，这是「已提交且仍在等」的唯一可读事实（写等待期间 App 不发布任何阶段）。

**`ccc-enable-reject`**：peer 拒绝订阅自己的 CCC 写。App 必须把这次尝试结束为 `notify=failed`
（绝不是 `subscribed`），重试后 `subscribed` 且 `notifyMode` 等于 profile 声明的模式，再由 host 发射、
App 拉取到值，最后真正 disable 并消费流终止。

**`ccc-disable-reject`**：镜像场景，也是本轮唯一需要**析取**的场景。被拒绝的 disable 会让 peer 仍
enabled，所以值仍可能到达；平台也可能在拒绝后本地停止投递（实测 iOS 就是后者）。场景发射一个值、拉一次，
用 `waitAny` 记录这次探测**结果是哪一种**，宿主再要求 App 的自述与之相符：投递到 ⇒ `subscribed`；没到 ⇒
只能是 `unverified` + 一个 `observation/*` 失败，绝不能是 `ended`。最后真正的 disable 成功、流终止。

**`passive-disconnect`**：peer 自己断链。App 必须自己宣布（`link=disconnected`、`linkEnd=unrequested`、
failure 身份可读），静默窗口内不得变化（投影 + 屏上值 + failure 身份），所有需要链路的控件必须收回，且不得
从一次失败的读里「学到」这次结束。

**`recovery` / `recovery-ten`**：同一 owner 上 N 轮（3 / 10）「扫到 peer → 连 → 发现 → 读 → peer 断 →
App 宣布」。只有第一轮开会话、只有最后一轮关闭；轮与轮之间由 host 让固件重新广播。`recovery-ten` 依旧
声明 `min_seconds=1500`，本次用 `--timeout 1800`。

## 这一轮的两个驱动能力（都是场景语义，不是花招）

1. **`waitAny`（有界析取）**：2..8 个条件，命中第一个即返回并在回执里记录是哪一个
   （`observed["waitAny"]`）。它存在是因为一个问题可以有两个合法答案，而**哪一个可接受是宿主的判断**，
   不是驱动的。`ccc-disable-reject` 的探测因此不需要把平台分支写进 plan，也不需要轮询到超时再「当作成功」。
2. **`still`（有界时间断言）**：条件必须在 `for` 秒内持续成立，每轮采样一次；不成立即失败并报出采样到的
   值。「什么都没变」不是 `wait`（已成立会立刻返回），所以静默窗口需要自己的步骤。

两个能力都只是 **UI 测试 bundle** 的改动，App 本身没有因此改动。

## 真机暴露并修复的三个缺陷

1. **`setText` 对已填值的受控输入会追加而不是替换**：`recovery` 第 2 轮给 `gatt-target` 重新输入同一 UUID
   时，实测 `fff10000-…` 变成 `fff10000-…fff10000-…`（36 → 48 字符）。根因：原实现按「当前长度」一次性
   发删除键，而 React 受控输入在编辑中会重渲染，部分删除被补回、新文本被追加。修法：删到字段确实为空
   （有界循环）再输入，仍然验证最终值完全相等。另加一条 plan 规则：**同一个 App 内的后续轮次不再重新输入
   目标**，只等待它仍解析为唯一一行（输入属于 App，不属于轮次）。
2. **`gattReadTotal` 的语义**：App 里「一次 scan = 一个 run」，`start-scan` 会给这次扫描一个新的读通道与
   视图，所以 `recovery` 每一轮的读都是**该轮 run 的第一次**结算（计数为 1）。第一次按 `cycle+1` 去等，
   真机上立刻失败；已按 App 的真实模型改正，并在 App 与宿主两侧都写清楚。
3. **peer drop 与「引擎忙」的竞态**：`DirectFixture.drop_peer` 一次性采样 `require_idle`，而引擎的 busy
   位由「活故障槽 **或** GATT target 的待消费 evidence」组成，普通 ATT 流量后会短暂置位。十轮 recovery 的
   第 9 次 drop 因此撞上 `fault engine busy/quarantined`。修法放在**这条路径**上（不是放宽全局守卫）：在它
   自己的 deadline 内等到 idle，超时仍按原样失败并报出 flags。`require_idle` 本身保持严格（arming 路径
   仍是一次采样就拒绝）。

## 收尾时的一处离线缺口

十轮场景的 App 动作账本是一个**有界窗口**（`omitted=80`），所以「每轮都记录了一次读」无法从窗口里数出来。
现在的断言是：窗口内没有任何失败动作，且**窗口内完整的那一轮**（最后一次 `begin-scan` 之后）与标准轮次
逐项一致；而「每轮自己的读确实结算」由该轮 run 的投影（`gattReadTotal=1`）逐轮断言，后者更强。

## 离线覆盖

- `test_scene_ios_native.py`：新增 24 条（write-reject 9、ccc-enable 4、ccc-disable 6、passive 5、recovery 5，
  以及 `waitAny`/`still` 的判定与「plan 没读的帧不能被断言」）。假回执只发布 plan 真正读过的字段，并由
  plan 步骤推导，因此「宿主断言了 plan 没读的事实」在离线就会失败。
- `test_ios_scene_driver.py`：新增 ccc / passive / recovery 的 plan 形状契约；`waitAny` 的分支形状与
  `still` 的有界窗口；全部 plan 的读数名唯一。
- `tests/peripherals/nrf52840` 全套在项目自己的 runner 下仍 OK（356 tests）。
- `tools-test`：1725 passed / 21 skipped / 5238 subtests。

## 边界

- 11 个场景的通过都是**候选事实**：`host_gate=pending`，不迁移任何 gate。
- 析取与静默窗口都把「平台差异」记录成事实，而不是掩盖成统一结论：`ccc-disable-reject` 在 iOS 上走的是
  「本地停止投递」分支，这仍是平台事实，不是断言所有平台都如此。
- Android 仍走 Appium；`AppiumSceneDriver`、WDA、tunnel、iproxy 只有在 Android instrumented 驱动对齐后
  才能删除。

## 复现（每个场景一条命令，参数相同）

```sh
pixi run just react-native formal-host-ios-device-test-build --device 00008110-000C61160C39401E

for scene in scan notification indication io cancellation write-reject \
             ccc-enable-reject ccc-disable-reject passive-disconnect recovery recovery-ten; do
  pixi run just react-native formal-host-ios-managed-$scene \
    --device 00008110-000C61160C39401E \
    --app-tree-sha256 abf0377afad38a21a435345e68bbd318369c23ad7e42c2f7f0c2027452a18bab \
    --xctestrun tests/consumers/react-native-bleng/ios/build/device-derived/Build/Products/BleHubBlengHost_iphoneos27.2-arm64.xctestrun \
    --fixture-contract tests/peripherals/nrf52840/firmware/contracts/multifunction-rf-v2-a.json \
    --fixture-profile tests/peripherals/nrf52840/firmware/profiles/multifunction-rf-v2-a.json \
    --fixture-port /dev/cu.usbmodem0010502520281 \
    --fixture-resource-identity jlink:1050252028 \
    --output .work/ios-native-final-$scene \
    --authorize-install --authorize-fixture-start --release-before-install --timeout 1800
done
```
