# CCC disable REJECT：Android 通过、iPhone continuity gap 与新固件回归

日期：2026-10-19。范围：用户明确确认的 getter 修复 B 候选安装、新固件已有 RF 回归、独立 CCC disable REJECT。**Android 严格专项通过；iPhone 两次未通过，不能宣称双平台通过或当前 CCC 工作已闭合。** `IOS-RADIO-GATE` 已重开排队，但当前未运行。

## 已授权 DK-B 安装与 contract 切换

用户明确确认上一轮给出的精确 B HEX 后，仅操作 DK-B：J-Link `1050251479`、USB serial `001050251479`、BTP `/dev/cu.usbmodem0010502514791`。DK-A 未操作。

- Zephyr source：`b59a7aa1191cc7b110e0bde74387b70d71c24659`；nrftest pin：`7b441459cf50e261fe9ef8d33061a917a8508589`。
- raw HEX：`5143964d1db6acafe80dab888c2fa68d9eb2ba19f53cfaeb5e6e58657c806b35`。
- installed normalized image：`834d5a0a62c8d0c4b4fb5b06fe86937c9240eeaf933a0dcdb40f04edbb5e1561`。
- image/profile/service：`nrf-multifunction-rf-v2-b` / `blehub-nrf52840-multifunction-v2-b` / `fd700000-0000-4000-8000-000000000000`。

通过原 `firmware-provision-dk` recipe，在同一 `purpose=provision` 物理 lease 下验证 manifest/HEX/contract、programming、正常 target reset/release、BTP installed-image attestation、exact contract comparison，全程一次执行成功。未提供 `--recover-debugger`，没有 unsecure/mass-erase recovery、destructive retry 或隐式工具安装。

Receipt 位于 ignored `.work/gatt-getter-candidate/provision/`：

- `dk-b-installed-attestation.json`
- `firmware-flash-dk/20261019T041524.196488Z-fc5f52eb7a6842c596fb834caccbffd1.json`
- `firmware-provision-dk/20261019T041526.430734Z-d0c0b2c67ed444d3887417c673173a20.json`
- `provision.log`

安装即时证明后，仅将 tracked B contract 的 image digest 改为新值；A contract/profile/declaration 不改。旧 B contract 已在构建期 history 与旧 RF frozen inputs 中保留。构建及来源边界见 [getter 报告](2026-10-19-nrf-gatt-local-getter.md)。每次后续 RF 仍重新取得 lease 内 live receipt，provision receipt 不是未来运行授权。

## 实现与生产修正

同一个 RN workbench，独立 `rf-ccc-disable-reject` / control flow **12**；不加入 full-suite。[`rf_ccc_reject.tsx`](../../../tests/consumers/react-native-internal/src/rf_ccc_reject.tsx) 共享两个 CCC executor 的有界事件/路由机制，但保留独立 step、ID 和 enable/disable transition。Provider 的 [`ccc_common.py`](../../../tests/peripherals/nrf52840/ccc_common.py) 只复用 topology/peer/lease/status/retirement/prefix/relay 机制，不把两种场景归一成别名；各 coordinator 与 ledger 拥有实际事实。

严格路径：正常 enable `34010` / subscription `34009` → App 收到 `ef` → host 独立 per-peer CCC `0100`、ARM `0100 → 0000` → matching HELD → queued FFF1 read `34012` → 单次 REJECT → failed disable `34011` 精确 ATT14 且无 Disabled domain → queued read `00` / warm subscription=1 → host CCC 仍为 `0100`、提交 `e1` → **App 必须在原 subscription 实际收到 `e1`，没有 re-enable** → normal retry-disable `34013` → CCC `0000` / `f0` stimulus / 双 read-fenced silence → disconnect/close/17 zeros。

八阶段 request/ACK：

```text
baseline-enabled / baseline-value-sent
arm / armed
disable-dispatched / held
reject / rejected
reject-observed / continued-value-sent
continued-notified / retry-allowed
disabled / silence-started
silence-done / close-allowed
```

Rejected receipt 的 before/after 必须保持 `0100`、observed `0000`、flags `0x0299`、无 callback/commit/normal-path、六类 refs 全零。正常 retry-disable 后当前 CCC 为 `0000`，但原 rejected ticket 的历史 before/after 仍为 `0100`；不得重新 ARM 来替代该 receipt。enabled-state local getter 的 ATT success + `0100` 在本次 Android/iPhone 物理证据中真实成立，不能从旧固件软件测试外推。

### Android remote-first disable

当前源码与已批准 [Plan §8.8](../../../.plan/PLAN.md) 原先不一致：先关闭本地注册，再写远端 CCCD。现已在 [`NotificationLifecycle.kt`](../../../platforms/android/library/src/main/kotlin/dev/blehub/android/NotificationLifecycle.kt) 修正：

- enable 仍 local-register → CCCD；失败 best-effort local compensation 保留原 primary error。
- disable 先远端 CCCD；被拒绝或提交失败不修改本地和逻辑 subscription。
- 远端成功后才 local-unregister；若此时本地失败，退休已远端 disabled 的逻辑 subscription，只发明确 `remote CCCD already disabled` 的 partial-failure terminal，不伪造 Disabled success，不远端重试/回滚。
- per-operation stage/identity guards 防止重入、重复 unregister/terminal 和旧 owner 操作新 route；native calls 不在 state lock 内，watchdog/retirement 责任不变。

生产后端 AAR 已重新发布，Gradle Debug/Release 的实际解析文件与 APK bytecode 均验证包含新行为，不复用旧同版本缓存。backend AAR SHA-256 `673b9be8179ea93897e0990b0848d45e6b2a03c8d9ba93942b1f3da63e23aa85`；UniFFI AAR `535756ae810ed355af23b29331148f1fc13d7700100c8633ddf3887ffe6ea5ba`。

## Android 严格专项通过

Attempt：`android/build/rf-ccc-disable-reject-d2f8682cab2f4693addaddb0afefd617`。

| 项目 | 结果 |
|---|---|
| bundle run ID | `2a220005-5757-4fc5-b315-89224099062b` |
| fixture/App session | `41a3cfd4-4816-4b7c-9925-132a0e082244` |
| Release APK SHA-256 | `3831ff8e718679b20f8523de7231b2ba7bb1d9b31cf6b3629884271305b2dff9` |
| conn / GATT / FFF2 / FFF1 IDs | `34004 / 34007 / 23 / 22` |
| subscription | 始终 `34009`，未 re-enable |
| actual CCC / FFF2 value / FFF1 handle | `42 / 41 / 39` |
| fault sequence | `1`，新 boot 下独立 ticket |
| failed terminal | command `34011`, `gatt / platform_android / BluetoothGatt / "14"` |
| actual notifications | 原 subscription 的 `ef`，拒绝后 `e1` |
| silence | App `1001ms`；Host `1.507127707998734s` |
| normalized runtime SHA-256 | `6836012ef86dd028e5dcae1b96c01435eccfdac2eaff6e5f88b5daf26fac5f19` |
| final | firmware 六类 refs、native 17 项、known peers 全零；physical/ledger/bundle passed，cleanup/evidence errors 为空 |

Warm snapshot connections=1、subscriptions=1、pendingIo/watchdogs=0；它不是最终 close-zero。十三个 native command terminals 精确一次，通知不是 terminal。`SET_VALUE accepted` 与 `ef/e1` App delivery 分别证明，不能互相代替。

## iPhone：两次 failed，不归因过度、不重订阅绕过

| 尝试 | bundle run ID | fixture/App session | App tree SHA-256 |
|---|---|---|---|
| `ios/build/rf-ccc-disable-reject-68d3d7ac397849bbb92502f1fee1cce9` | `4dc14e45-45b7-44df-a527-3c048763621e` | `6c007b69-c22f-4a66-aada-7a7d897a12f1` | `aa8342af6b71a03284e95f72a77f1d24116650bf1c39e2bb1b538d35b6592ef9` |
| `ios/build/rf-ccc-disable-reject-e5c6372c75bb4db7b7005bb795337edb`，独立 opt-in diagnostics | `a95e4159-1a77-4474-8a3f-435105ecc529` | `2a05ff3b-0096-470d-8a75-fddb71d1a354` | `93322afb8357cf64a47349e8d965fc34e72ea75655dd7197e575ca2d12444c3d` |

两次均先在 FFF2 characteristic `14` / subscription `34009` 收到 baseline `ef`，再收到精确 `gatt / platform_apple / CBATTErrorDomain / "14"` failed-disable terminal，无成功 Disabled domain。FFF1 `34012` read `00`；warm connections=1、subscriptions=1、pendingIo/watchdogs=0、callbackRoutes=3。固件独立 CCC `0100`、原 receipt 未变；`e1` 的 local SET_VALUE 返回 accepted，但没有 App notification。停在 5/8 controls，JS `JS wait expired; no native outcome inferred`，native readiness/runtime failure，host primary `ccc-disable-reject: native/App failure`。physical 记录 `stopped/whole deadline`，两次 cleanup errors 为空，peer detached、最终 17 项零；均保持 failed，无成功 ledger。

### 默认关闭的 native 诊断

新增 [`AppleNotificationDiagnostics.swift`](../../../platforms/apple/Sources/BleHubApple/AppleNotificationDiagnostics.swift) 与对应 backend/controller/probe 记录点。只在 app env `BLEHUB_APPLE_NOTIFICATION_DIAGNOSTICS=1` 时写 captured stderr，prefix `BLEHUB_APPLE_NOTIFICATION_DIAGNOSTIC `；不使用 runtime pass/control marker。每 owner 最多 128 条，完整 JSON 行不超过 3000 UTF-8 bytes，显式 truncation/overflow，value preview 最多 16 bytes。不开线程/timer/registry，不改公开 BLE/UniFFI/Nitro contract。

诊断只读 route/state，记录正常 setNotify before/after、delegate entry（所有 guards 之前）、error、native object identity、controller disposition、ingress、handoff/receive/listener submission；不额外 consume barrier、不隐藏 re-enable、不放宽 timeout 或失败判据。记录 Nitro submission 不称作 JS delivery。

第二次通过当前 `devicectl help device process launch` 本机文档确认的 `DEVICECTL_CHILD_` env forwarding，命令前缀为 `DEVICECTL_CHILD_BLEHUB_APPLE_NOTIFICATION_DIAGNOSTICS=1`。共捕获 21 条 backend/probe 诊断，backend sequence 到 17，未达到上限、无 overflow：

- baseline `ef` 完整经过 value delegate → notification disposition → ingress/handoff → listener；App 另有真实接收记录。
- rejected disable 的 callback error 是 `CBATTErrorDomain/14`，**`isNotifying=true`**，同一个 peripheral/characteristic object、connection/subscription generation 匹配。
- 处理失败后，controller phase=`active`、acceptsValue=true、原 IDs 未变，无 read/value/notification-state barrier。
- FFF1 queued read 的 callback 继续到达；此后直到 cleanup，**未观察到新的 FFF2 `value.entry`**。

因此已缩小为“该捕获区间没有 FFF2 delegate callback”，不是 JS 静默丢弃一个已收到的 `e1`；**仍不能宣称 iPhone 已在空中收到 `e1` 或 CoreBluetooth 丢包**。当前 `SET_VALUE` 只证明本地更新/发送 admission，没有该通知的 TX-completion/air evidence；fault ticket 的 REJECT-response TX_COMPLETED 不能证明后续 `e1` notification 发送。下一缺口是 exact peer/epoch/value-handle/`e1` 关联的 Peripheral notification submission/completion/error，必要时 HCI/air trace。没有依据做生产“修复”或把严格 continuity 改成可选。

## 新 getter 固件的既有专项与 full-suite 回归

在上述新 installed digest、各自 frozen artifacts 和独立 fresh sessions 下，串行运行：

| Gate | Android attempt | iPhone attempt | 结果 |
|---|---|---|---|
| response-write REJECT | `rf-write-reject-be7f21c976e546eaa965f24986bb760d` | `rf-write-reject-d4203447ab1c4b63aa94c6cf5831a3db` | 各自 passed |
| CCC enable REJECT | `rf-ccc-enable-reject-55d3aeb1358e4a398765ec190a09feba` | `rf-ccc-enable-reject-d8ba5a2320ca42c991ed234e586afa57` | 各自 passed |
| full-suite | `rf-full-suite-8c1560387420454792b37cd7e6a69b7c` | `rf-full-suite-110a458dc3d54d85ac56c73f63413820` | 各自 10 flows + RecoveryTen 10 cycles passed |

Android 使用上表 APK；iPhone 回归使用含默认关闭诊断代码的 tree `93322afb...`，full-suite bundle run `1054d707-9eb2-4d30-a726-45b852542db2`。后者 raw capture 的诊断 prefix 行数为 **0**，没有把 opt-in env 延续到普通运行。

两台手机最终保留成功 full-suite 页面。成功 baseline 不覆盖或解释两次 CCC disable failed，也不替代旧 unresolved iOS recovery cycle-8 报告、native HIL、clean artifact consumer、long stress 或最终 ABI freeze gates。

## 软件、native 与 offline 验证

实际执行与边界：

- `pixi run just react-native tools-fixture-test`：capture/bundle 完成后 **964 passed / 205.659s**；完整 host Jest **17 suites / 553 tests passed**，含实际 executor → actual Python parser crossing；不把模拟输入当 RF。
- `host-check`、`fmt`、`lint`：通过；文档完成后的最终复核另记于下。
- `BLEHUB_VERBOSE=true pixi run just ffi android-backend-check --offline --rerun`：**34/34**，Debug/Release build 通过。首次在线 targeted run 外层四分钟超时，保留日志，不算通过。
- `pixi run just ffi android-backend-publish`、`host-android-config-check`、Release build/single-image：通过；Gradle 实际 AAR resolution 与 APK bytecode/native slice hashes 已核对。
- `pixi run just react-native android-test 3ac5cc09`：**23/23**（12 routing、2 handoff、7 lifecycle owner、2 close）；非 RF。
- 现有 direct Kotlin physical ART gate **10/10**、published-AAR consumer **4/4**。先构建实际 instrumentation/consumer，再 `BLEHUB_ANDROID_SERIAL=3ac5cc09 pixi run just --no-deps ffi android physical`，保持已发布 candidate；当前 dependency chain 不自动构建 bindings test APK 的限制已保留。
- Apple 默认关闭诊断修改后：targeted **5**、full backend **87**、canonical scenario selection **16**、host Swift handoff **2**；simulator native lifecycle **8 passed / 0 failed / 0 skipped**，只选择 canonical class，其他未选择类不称为 skipped。
- iOS signed device build 与 single-image 通过；cached 0.87.1 core/dependencies 复用，23 份 lock/privacy/npm 文件保留 pre-run bytes。最新 diagnostic build dylib SHA-256 `f2c4b7cd608f784b05a53f4691a2941f3afb87e79ba6f29931360c150cfc5871`。
- 九份本轮 passed/failed RF bundles（上面的三个 disable attempts 和六个回归 attempts）逐份 `verify_bundle` offline 完整性重放通过，保持原 verdict；未重新授权 RF。
- 独立只读 review 未发现 Android stage/ownership 或成功判据的阻塞问题；确认 iPhone 下一缺的是通知 TX evidence，不支持归因过度。

最终文档/诊断修改后再跑：`tools-fixture-test` **964 passed / 202.790s**；完整 host Jest **17 suites / 553 passed**；`fmt`、`lint`、`host-check` 通过；Mermaid **154 Markdown files / 27 diagrams** 通过，六份 authored Markdown 的 UTF-8/fences/whitespace/conflict checks 通过。所有 RF 收尾后 provider marker 不存在；该检查只补充进程收尾观察，不代替各 bundle 的 peer/native/firmware 证据。日志为 `.work/ccc-disable-final-*.log`。

维护入口：`host-android-rf-ccc-disable-reject <device> <contract> <profile>` / `host-ios-device-rf-ccc-disable-reject <udid> <team> <contract> <profile>`。普通 runner 使用冻结产物、explicit contract/profile 和每次 fresh installed receipt，不 flash/reset。完整 RF 命令使用 `run_rf_ccc_disable_reject --platform ... --device ... --artifact ... --fixture-contract tests/peripherals/nrf52840/firmware/contracts/multifunction-rf-v2-b.json --fixture-profile tests/peripherals/nrf52840/firmware/profiles/multifunction-rf-v2-b.json`，显式 port override `/dev/cu.usbmodem0010502514791`；默认 absolute budget 180s，上限 300s。full-suite 回归传 900s，其 outer child bound 960s。

Ignored logs/evidence：`.work/ccc-disable-*.log`、`.work/ccc-disable-regressions/`、`.work/ccc-disable-reject-ios/`、`.work/ccc-disable-apple-diagnostics/`、`.work/android-notification-disable/`，以及每个 `tests/consumers/react-native-internal/{platform}/build/<attempt>/`。构建/模拟测试的一次 Pixi 子进程超时与后续成功保留，不混同真机结果。

## 尚未闭合与 radio 顺序

- CCC disable REJECT 的 Android pass 与 iPhone continuity gap 分开维护。下一步补 Peripheral 通知 TX 证据；若需要新固件，重新绑定 candidate/hash 并按显式 provision 规则执行，不沿用本次 exact HEX 安装授权。
- `IOS-RADIO-GATE` 已明确重开并排在当前 firmware/CCC 工作后；未以本项有缺口为由偷偷豁免，也未并行运行 Settings/XCUITest/Continuity 控制或 radio mutation。原 no-password/no-terminal-input、失败历史、独立 RF/cleanup 责任保持。
- 本轮只安装已获确认的 B image，未新增 firmware commit，未 commit BleHub 或 push 任何仓库。既有 Zephyr/nrftest 本地 getter commits 保持不变；各仓库无关工作未 reset/stash/clean。
