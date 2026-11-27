# Permission transition 正式 App lane（Android 真机）

本文件记录 C 类 #17（`permission-transition`）从退役 consumer 迁到正式 App 的三阶段切片：
语义、暴露的两个共享组件缺陷、修复与真机结果。只记录本轮实际跑过的事实。

## 主张

三个**独立进程**，每个阶段一个：

| 阶段 | 主机侧（host-owned） | App 侧（product fact） |
|---|---|---|
| `denied` | `deny_startup()`：撤销 SCAN+CONNECT 并回读写确认 | `start()` 成功但 `scanAuthorization == DENIED`；scan 终止于 `bleng.operation.permission` |
| `active` | 观察到 `active-connected` 后在 fixture ARM 一个 read hold；观察到 `armed` 后确认 HELD、`revoke_connected()`、`await_permission_death()` | 建链、GATT、发出一次 read 后阻塞；进程被 OS 结束，read 没有终态、连接没有 clean close |
| `restored` | `grant()` 后重开广播（`prepare_next_cycle`） | 新进程重扫、重连、读到固件 provision 的值并断开 |

主机仍拥有：手机 lease、fixture lease、权限撤销/恢复、OS 死亡证据（reason 8）、最终判定。
App 只做 opt-in（`blehubPermissionTransition=permission-transition`）与事实断言。

## stage 契约

```
denied   : bootstrap → denied-start → denied-scan
active   : bootstrap → active-start → active-scan → active-connected → active-gatt → armed
restored : bootstrap → restored-start → restored-scan → restored-connected → restored-gatt → restored-io → done
```

- `bootstrap:<json>` 携带进程身份（`pid/uid/user_id/process_name/start_ticks/native_started_unix_ms`），
  主机用它把权限死亡证据钉在**这一个**进程实例上（PID 复用由 `start_ticks` 区分）。
- `active` 的契约**没有**终态：App 正常结束反而是失败。
- 三个阶段的 `(pid, start_ticks)` 必须两两不同；主机会在 `_require_fresh_identity` 里拒绝复用。
- Python 词表与 Kotlin `marker("...")` 由离线测试精确比对；Kotlin 里唯一多出的是
  `unexpected-held-read-terminal`，它只表示“本该被打断的 read 完成了”。

## 暴露的两个共享组件缺陷（已根治）

### 1. `AndroidPermissionController` 用模块默认包名做进程校验

`revoke_connected` 的 cmdline 校验和 `await_permission_death` 的 exit-record 校验都写成
模块级 `PACKAGE`（默认 `com.blehubnitrohost`），而其他所有查询都用 `self.package`。旧 lane
恰好就用那个包名，所以缺陷从未显形；正式 App 一用就报 `old process cmdline mismatch`。

修复：两处改为 `self.package`。控制器本来就是“显式选择的 canonical package”，这才是它的契约。

注意现场事实：instrumentation 进程的 `/proc/<pid>/cmdline` 是包名后跟一长串 NUL padding，
校验依赖 `rstrip("\0")`；这不是包名错误。

### 2. 该 lane 不能复用 `AndroidRadioController.finish()`

`finish()` 的成功契约要求 `initial/off/on` 三步；本 lane 从不改无线电，只要求 ON 并做
收尾读回。正确入口是 `finish_observation()`（final ON readback + release，不伪造 off/on）。
用错会把一个完整通过的三阶段场景判成 `radio transition did not complete initial/off/on`。

## 时序约束（不是产品主张）

`active-connected` 之后与 GATT 发现之前有一个 1.5 s 的 host-setup 屏障：
fixture 的 ARM 必须绑定**已存在**的 GAP 连接，所以只能在连接之后 arm；屏障给主机的 arm
留出时间。它不参与任何断言——`require_hold_held` 直接读固件的 HELD 状态，arm 没成功就
失败，而不是“多等一会儿也许就过”。

第一次真机运行正好抓到没有这个屏障时的失败：
`armed read left ARMED without a held request: phase=RETIRED outcome=DISCONNECTED_UNCOMMITTED`
（read 在 ARM 之前就完成，App 正常收尾并断开）。

## 真机结果

| 项 | 值 |
|---|---|
| 设备 | `3ac5cc09`（PHB110，Android 15，root） |
| App APK | `aa4af97bddee89e1508366e9e1f6942b1f9048f323efa82f999e882e45a0dc53` |
| androidTest APK | `aa58620223515b89578cb2b38c0c552ccfe171c95b107fbdc10567c3ed6f295c` |
| fixture | `jlink:1050252028` / `fd600000-…` / `BleHubMultiRfV2DKA` / read `fff10000-…` |
| run | `.work/android-permission-transition-r6/a8c8868c-9318-4d5a-bbfa-e849161d76b3/result.json` |
| 进程身份 | denied `15335/361285431`、active `15464/361285623`、restored `15629/361286233` |
| 授权变化 | 初始 granted → `deny_startup` 两者 false → 重授权 → active 阶段 `revoke` 后 CONNECT=false、SCAN=true → 死亡 → 重授权 → restored |
| 死亡证据 | `reason=8`、`reason_name=PERMISSION CHANGE`、`package_uid == real_uid == 10378`、`user_id=0`、`process_name=com.blehubblenghost` |
| hold | `armed` → `held`（固件自己的陈述） |
| 链路 | `active_link_ended=true`（fixture settle 到 no-peer 才允许重开广播） |
| 清理 | `permissions.restore` → `fixture.close`（passive close 回执）→ lease release → `phone.finish`（final ON）；无错误 |
| 结论 | `status=passed`，`host_gate_status=pending` |

`pixi run python -m pytest tools/tests/react_native/test_bleng_android_permission_transition.py`
→ 36 passed / 83 subtests（无硬件，含 adapter lane 的两个文件一起跑）。

## 对 C 类核对的影响

#17 `permission-transition` 的 Android 阻塞项关闭：三阶段、OS 死亡证据、权限回执和恢复
都在正式产物上。`react-native-internal` 的 `rf_permission_transition.tsx` 及其 host runner
不再被任何正式 lane 依赖；iOS 侧本来就没有对应入口（权限弹窗归 #5 的 XCUITest 迁移）。

## 未覆盖 / 已知边界

- 本 lane 要求起始状态 **两个权限都已授予**（与旧 lane 相同的显式前置），因为
  `AndroidPermissionController.initial()` 的契约就是“记录真实初始态并精确恢复”。未授予时
  runner 在 preflight 直接失败并给出可操作提示。
- Apple 侧未做等价 lane；iOS 的权限事实由 Apple backend 测试 + #5 的 UI 自动化覆盖。
- `scan while adapter off` 的 Android 门禁（见 adapter lane 报告）是本场景的依赖项：
  没有它，`denied` 阶段拿不到 `bleng.operation.permission`。
