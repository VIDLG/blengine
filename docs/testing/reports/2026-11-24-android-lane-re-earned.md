# Android lane 重挣（2026-11-24，候选事实）

日期：2026-11-24
范围：Android managed 11 场景 + 4 条 native-contract lane
设备：OnePlus PHB110 `3ac5cc09`（rooted）；DK-A `jlink:1050252028`；DK-B `jlink:1050251479`（仅多链路）
结论：11/11 场景与 4 条 native lane 全部通过；过程中发现并根治一个 **plan 竞态**。iOS 仍未跑。

## 1. 先发现一个真缺陷：tap → ledger 读取的竞态

第一次整跑（`.work/android-native-sequence/2026-11-24-c`）在 `ccc-disable-reject` 失败：

```text
RuntimeError: the App did not record an accepted disable whose wait is outstanding:
[('notify-enable-wait', 'succeeded'), ('notify-pull', 'succeeded')]
```

对照同一计划的通过运行（`2026-11-24-b`）逐字比较，**计划与步骤完全相同**：

| | 步骤 43 tap | 步骤 44 read ledger，账本尾部 |
|---|---|---|
| run b（通过） | `disable-notifications` | `14\|notify-disable\|succeeded\|`、`15\|notify-disable-wait\|pending\|` |
| run c（失败） | `disable-notifications` | 止于 `13\|notify-pull\|succeeded\|` |

根因：**tap 只注入一次触摸**。UiAutomator 的 `click()` 在触摸分发后立即返回，不会等待 App 的
RN 处理器；紧接着的账本读取因此可能赢过处理器，读到 tap 之前的账本。iOS 上 `XCUIElement.tap()`
会等 app idle，所以同一计划在 iOS 上稳定。

App 侧的写入顺序（`src/useManualScan.ts`）决定了正确的 settle 信号：`record('<action>')` →
`settle(submission, …)` → `record('<action>-wait')` → 才 `await`。三者是**同一个同步块**，所以
「wait 行存在」就等于「处理器已跑完」，且与结果无关（`pending` 表示对端仍持有，`succeeded` 表示已落地）。

### 修法（声明式，不放松判定）

`scene_plans._await_recorded(action)` 生成一个等待步骤：

```python
{"op": "wait", "id": "action-ledger", "labelContains": f"{action}-wait|", "timeout": 30}
```

用于所有「tap → 立刻读账本」的位置：

| 计划 | action |
|---|---|
| `notification.collect` / `indication.collect` / `ccc-enable-reject.collect` | `notify-disable` |
| `ccc-disable-reject.hold` | `notify-disable` |
| `ccc-enable-reject.enable` | `notify-enable`（保留原 status wait，再加账本 settle） |
| `write-reject.dispatch` | `gatt-write` |

推导本身不变，仍断言精确 tail，所以 pass 的含义没有变化。守卫：
`test_ios_scene_driver.py::test_every_ledger_read_states_what_it_expects_to_see` 要求**任何**
`action-ledger` 读取的前一步必须是 `wait`/`waitAny`/`still`。

计划是运行期数据（host 写进 App 的 plan），因此**不需要重建 App**。

## 2. 11 个 managed 场景（`.work/android-native-sequence/2026-11-24-d`）

同一 App APK（sha `fb914530573210670c60825f1fff6cbeed9f06233755e893857be58f18339274`）、同一
test APK（sha `feea61906c4cf6aa51386bda470b82cd57b0c859c019b201494688221fa8bd4a`）、同一 DK-A。

```text
scene_sequence=passed scenes=scan,notification,indication,io,cancellation,write-reject,
ccc-enable-reject,ccc-disable-reject,passive-disconnect,recovery,recovery-ten
```

| 场景 | 场景内耗时 |
|---|---:|
| scan | 27 s |
| notification | 32 s |
| indication | 32 s |
| io | 33 s |
| cancellation | 39 s |
| write-reject | 38 s |
| ccc-enable-reject | 38 s |
| ccc-disable-reject | 53 s |
| passive-disconnect | 38 s |
| recovery | 52 s |
| recovery-ten | 120 s |

逐 bundle 离线复验（`python -m tools.react_native.bleng_scan_evidence <attempt>`，11/11 rc=0）：

```text
integrity=passed  host_gate=pending  cleanup=[]
```

离线重放不升级宿主门禁，`host_gate=pending` 是正确值。

## 3. 四条 native-contract lane（同一 App/test APK、同一 DK-A）

| lane | 结果 |
|---|---|
| native-notification `single` | `passed`，stages 到 `done` |
| native-notification `sequence` | `passed`，`values-matched` |
| native-notification `overflow` | `passed`，`overflow-observed` |
| adapter-transition | `passed`，`radio=restored` |
| permission-transition | `passed`，`hold=held`、`death_reason=8`、phases `active,denied,restored` |
| multi-link（2 peer / 2 轮，DK-A + DK-B） | `passed`，`peers=2 rounds=2`，`survivor:1` |

多链路两条 peer 分别是 v2-a 与 v2-b profile，`--peer` 各自带 port/contract/profile/identity。

## 4. 收尾状态（实测）

- `adb shell pidof com.blehubblenghost` → 无进程（rc 1）。
- `settings get global bluetooth_on` → `1`（adapter-transition 自己恢复）。
- `BLUETOOTH_SCAN`/`BLUETOOTH_CONNECT` → `granted=true`（permission-transition 精确恢复）。
- 无残留 fixture/btp 进程，无残留 phone lock（`.work/android-radio-leases` 只有历史 `stale-release-*.json` 记录）。

## 5. 限制

- **iOS 未跑**：iPhone「设置 › 开发者 › UI 自动化」未生效时 XCTest 以
  `Timed out while enabling automation mode`（exit 65）失败；这条已是 observed 回归
  （`tools/tests/observed/test_ios_scene_driver.py`）。iOS 的 11 个场景与 `run_ios_scene_driver`
  的失败证据改动都还没有真机验证。
- 本轮未改 App/Kotlin/Swift 源码，因此 APK sha 未变；改 plan 不影响已安装的 App。
- 这些通过只绑定本次 run root、这两个 APK 与这台设备/固件；不是 ABI 冻结、Phase 退出或压力测试结论。
