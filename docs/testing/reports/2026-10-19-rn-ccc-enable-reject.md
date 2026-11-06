# RN CCC enable REJECT：Android / iPhone 独立 RF 与有界静默

日期：2026-10-19。范围：现有 RN workbench 的独立、opt-in `ccc-enable-reject`。不是新 App，不加入 full-suite，不宣告 CCC disable REJECT、最终同候选所有门禁、Phase exit 或 ABI freeze。

## 结果

Android `3ac5cc09` 与 iPhone `00008110-000C61160C39401E` **分别完成真机通过**：

1. 精确 FFF2 managed CCC 初值 `0000`，ARM `0000 → 0100`；其中 `0100` 是小端 bytes，表示通知位 `0x0001`。
2. matching HELD 捕获 CCC Write Request；单次 REJECT，未执行 target callback/commit，CCC 独立 per-peer 回读仍为 `0000`。
3. rejected enable 只有一次 `gatt` 失败 terminal，原生 ATT code `14`；无该命令的成功 state domain，失败 subscription 不接收值。
4. 同一 Probe/connection/GATT/characteristic，用 fresh subscription 正常启用 Notification，App 实际收到 `ef`。
5. 正常 disable + terminal、独立 FFF1 read fence 后，Host 回读 CCC `0000` 并提交 `f0` stimulus；App 至少 1000ms 无通知，再完成另一 FFF1 read fence。
6. disconnect/close 后原 ticket retained receipt 未变，firmware 六类 refs 全零、native 17 项全零、known peers=0、GET_LIMITS idle；physical/ledger/bundle 均 passed，无 cleanup/evidence errors。两台手机成功结果页保持打开。

本轮未更改、刷写、reset 或 unsecure 固件；DK-A 未操作。复用 DK-B installed `nrf-multifunction-rf-v2-b`，normalized digest `2066c7545e8ce2533288adbe980ce1c36ebc18dc9dc425f97c7bab868baa4799`，每次 RF 另有独占租约内即时 installed-image receipt。安装历史见 [DK-B v2 报告](2026-10-18-v2-dk-b-mobile-full-suite.md)。

## 独立身份与产物

| 项目 | Android | iPhone |
|---|---|---|
| attempt（各平台 host `build/` 下） | `rf-ccc-enable-reject-647d3ab7b7934a3d988d7738b19c49f5` | `rf-ccc-enable-reject-8b89ddf5a61e43f1ab851244463fa433` |
| bundle run ID | `1afb1304-f6ae-4980-8f1f-44bf8f68baac` | `39904f84-acfd-4714-a72a-971ec0ecdcc3` |
| fixture/App session | `4f582fd1-02e3-40ef-89f3-4ea604fe2d94` | `defe1977-7dd2-4076-aa22-bd71d345c274` |
| artifact SHA-256 | APK `34e83b835499c9004b1ce0ff5a3fc714b31e029d55e974fc4d773ffa407598a4` | app tree `c53608b0155b47703bafc71b1fad5929e3bbd2cf7471c57675ee22ed8da3aa95` |
| native connection / GATT / FFF2 / FFF1 | `33004 / 33007 / 23 / 22` | `33004 / 33007 / 14 / 13` |
| actual CCC / FFF2 value / FFF1 handle | `42 / 41 / 39` | `42 / 41 / 39` |
| firmware ticket sequence | `19` | `20` |
| native error | `platform_android / BluetoothGatt / "14"` | `platform_apple / CBATTErrorDomain / "14"`, `Unlikely error.` |
| App silence elapsed | `1000ms` | `1014ms` |
| independent host silence elapsed | `1.4330147079890594s` | `1.6590522089973092s` |
| normalized runtime SHA-256 | `71ab952491e188c89536169027ec20770ffca9e7ea6c013f8a4551a2a0b3f4e5` | `9366b853eedb84960cab2924154adb0840d133ba4bfea4b58feaa8b390f9df78` |

每个 attempt 内的 `evidence-bundle.json` 绑定 frozen candidate、原始/规范化 runtime、identity/capture、七阶段 SDK 原始回执、安装身份、fixture request/receipt/inputs、control、physical 和 ledger。两次 session 不共享 ticket 或 verdict；opaque IDs 不等同于 ATT handles。

Rejected receipt 精确为 `RETIRED / INJECTED_REJECT`，flags `0x0299`：REQUEST_CAPTURED、RESPONSE_QUEUED、TX_COMPLETED、REARMABLE、ANSWER_ORDERED。CALLBACK_INVOKED / COMMIT_OBSERVED / NORMAL_PATH_EVALUATED 均未置位；before/after `0000`、observed `0100`，normal/callback result 为 `0`，不是 Central 收到的注入错误 `14`。之后恢复启用/禁用不重新 ARM、不替换原 receipt。

Warm failed-enable snapshot 是连接仍活跃时的独立证据：connections=1，subscriptions/pendingIo/watchdogs=0；Android callbackRoutes=1、Apple=2。Apple 保留失败订阅的旧 value-route barrier 是设计行为，不为了凑零删除；完整 17 项零只在最终 awaited close 后要求。

## 控制与复用边界

[`rf_ccc_enable_reject.tsx`](../../../tests/consumers/react-native-internal/src/rf_ccc_enable_reject.tsx) 复用现有 `RfSignalBarrier`、GATT validation、日志有界投影与 workbench `Tests → Current run → Evidence`。Control 为 flow `11` / mode `rf-ccc-enable-reject`，**11 只是 discriminator，不是 full-suite 的第十二项**。

| App request | Host ACK | 独立物理事实 |
|---|---|---|
| arm | armed | 唯一 peer、descriptor、CCC `0000`、exact ARM receipt |
| enable-dispatched | held | 精确 CCC Write Request `0100`，callback/commit 尚未发生 |
| reject | rejected | 一次 REJECT、原 ticket 退休、per-peer CCC `0000` |
| recovery-enabled | value-sent | App Enabled(Notification)+terminal 后提交 FFF2 `ef` |
| recovery-notified | disable-allowed | App 已实际收到 retry subscription 的 `ef` |
| disabled | silence-started | Disabled+terminal 与 FFF1 read 后，CCC `0000`、提交 `f0` |
| silence-done | close-allowed | App/Host 两个不少于 1s 的观察边界，第二 FFF1 read、CCC `0000`、retained receipt 未变 |

最终顺序：disconnect domain/terminal → late observation → awaited Probe close → 17 项零 → close step → runtime close → passed。native ACK 必须先于 Linking/JS ACK；callbacks 可早于对应 JS ACK，缓冲校验不强加跨线程假顺序。

- [`ccc_enable_reject.py`](../../../tests/peripherals/nrf52840/ccc_enable_reject.py) 拥有 CCC 场景的物理策略与事实，复用原 typed fault codec 和唯一 leased BTP session。
- [`reject_capture.py`](../../../tools/react_native/reject_capture.py) 是 response-write/CCC 两个 named adapter 的共享 transport：SDK、流、identity、ACK deadline、local child cleanup；不拥有 CCC 判据。
- [`ccc_enable_reject_ledger.py`](../../../tools/react_native/ccc_enable_reject_ledger.py) 重放实际 native 与 physical，包括八个 runtime-prefix bytes/hash、原 deadlines、拓扑、local readback、实际 delivery、静默及资源。
- [`rf_scenario_runner`](../../../tools/react_native/rf_scenario_runner.py) 复用 frozen installation、fixture ownership 和 finalizer；[`rf_evidence_bundle`](../../../tools/react_native/rf_evidence_bundle.py) 显式 dispatch 场景，无 continuous10 fallback，并复用 reject-family 固定引用/identity 机制。
- App catalog 只展示真实 Just 命令；同时修正原 response-write 卡片命令遗漏 `rf` 的问题，不新建导航或工具 registry。

## 保留的失败与 firmware getter 限制

### Android 首次：ARM 前的 declaration query 失败

原 attempt：`android/build/rf-ccc-enable-reject-48b07d085b894a4dbbc987a3a9243e11`。Host 解析拓扑时记录 `service declaration read` 失败；尚未 ARM，无 ticket。App 已 connect/discover/baseline read，随后 owned cleanup detach，peer_count=0、cleanup errors 为空。原报告错误未记录 handle/ATT response/value，不能追溯宣称那次确为某个错误码。

本地 Zephyr `tests/bluetooth/tester/src/btp_gatt.c::get_attr_val_rp()`（已安装来源提交 `d9096bd963e24bf157641f537443134676dc0a1d`） 对 Tester-owned attribute 统一将 userdata 当普通 `gatt_value` 读取 enc_key_size；service/characteristic declaration 和 managed CCC 使用不同结构。类型问题来自源代码审查；本次失败印证该查询路径不可用，但不将推断的 `0x0c` 写成实测。

修复未修改固件或把错误结果当成功：Provider 用 actual GET_ATTRIBUTES inventory、唯一 FFF1/FFF2 UUID handle 查询、doctor mapping 一致性与共属 primary-service 区间；根据 Tester allocator 的有序 metadata 建立 declaration/value association，CCC 只选 FFF2 descriptor interval 中唯一 `2902`，不做全局 CCC lookup、不假设 value_handle+1。Native 实际 GATT service UUID/primary/properties/IDs 与本地唯一 value UUID/区间交叉绑定。报告明确 `topology_method="tester-uuid-interval-v1"`、`service_binding.source="native-gatt-unique-value-uuid"`；metadata association 不冒充原始 declaration bytes。回归模拟 declaration getter 失败并要求不调用它。

### Enabled CCC 的 readback 不是本项已证明能力

同一 getter 类型问题也可能影响已启用 CCC 的本地查询；本项**不使用或归一化 enabled-state local readback**。恢复启用通过生产 native Enabled+terminal 与实际收到 `ef` 证明；独立 local read 只在 disabled 状态断言 ATT success + `0000`，前后绑定 exact live peer/epochs。源码审查还发现旧 doctor 的 declaration read 未严格检查 ATT status；本次新证据不依赖其声明内容，这个独立缺口未修复。

后续 CCC **disable REJECT** 需要先证明启用前态，不能未经评审照搬当前受限策略。理想后续是修正 Zephyr getter 类型判别并加 managed-CCC/declaration 回归，再经明确授权构建/安装新的 candidate；普通 RF run 不隐式 provision。

### iOS 构建前置失败

首次 Pods 依赖可用性检查退回 RN source，引发 simulator 缺 x86_64 RN dev-support symbols、device `ReactNativeDependencies` resolution 失败。保留原日志；后续显式使用已存在的 **0.87.1 debug cached core/dependencies tarballs**，未改变版本、源码配置或收集密码。canonical native gate 与 device build/single-image 成功。Pods lock、installed manifest、privacy manifest 和 npm locks 按 pre-validation bytes 保留；没有覆盖原工作。

## 验证与命令

实际执行的软件与 native 检查：

- 最初组合 `tools-fixture-test` 在 120s 外层超时，不算通过；后续 240s 边界运行 923 passed，拓扑修正后 **927 passed / 148.151s**。
- 完整 host Jest：**16 suites / 484 tests passed**；CCC 专项含实际 executor 输出交给实际 Python parser 的四种平台/ACK-race crossing，36 项 mutation 拒绝；另保留 response-write 回归。
- `pixi run just react-native host-check`、`pixi run just fmt`、`pixi run just lint` 通过；最终文档与组合复核另记于末尾。
- `pixi run just react-native android-test 3ac5cc09`：**20 tests passed**，包含 CCC 七阶段、既有 write-reject、handoff/close/lifecycle；Android Release build 与 single-image 通过。
- `pixi run just react-native host-ios-native-lifecycle-test D8098F99-2E1D-4E40-963F-D2F6F4942EEA`：**8 passed / 0 failed / 0 skipped**；device build 与 single-image 通过。Simulator 不能替代上面的独立 iPhone RF。

RF 从 BleHub 根目录执行，先有上述 frozen-candidate 构建检查：

```sh
BLEHUB_NRF52840_PORT=/dev/cu.usbmodem0010502514791 pixi run python -m tools.react_native.run_rf_ccc_enable_reject --platform android --device 3ac5cc09 --artifact tests/consumers/react-native-internal/android/app/build/outputs/apk/release/app-release.apk --fixture-contract tests/peripherals/nrf52840/firmware/contracts/multifunction-rf-v2-b.json --fixture-profile tests/peripherals/nrf52840/firmware/profiles/multifunction-rf-v2-b.json
BLEHUB_NRF52840_PORT=/dev/cu.usbmodem0010502514791 pixi run python -m tools.react_native.run_rf_ccc_enable_reject --platform ios --device 00008110-000C61160C39401E --artifact tests/consumers/react-native-internal/ios/build/device-derived/Build/Products/Debug-iphoneos/BleHubNitroHost.app --fixture-contract tests/peripherals/nrf52840/firmware/contracts/multifunction-rf-v2-b.json --fixture-profile tests/peripherals/nrf52840/firmware/profiles/multifunction-rf-v2-b.json
```

维护入口：`host-android-rf-ccc-enable-reject <device> <contract> <profile>` / `host-ios-device-rf-ccc-enable-reject <udid> <team> <contract> <profile>`。两个 Just recipe 先检查显式 inputs/mode、构建同一个 App，再交 runner 消费冻结产物；不会刷机。

完整 logs 位于 ignored `.work/ccc-enable-reject-*.log`、`.work/ccc-enable-reject-ios/`、`.work/ccc-enable-reject-provider/`、`.work/ccc-enable-reject-ledger/` 和 `.work/ccc-enable-reject-topology-fix/`；bundle tests 的单次日志在 ignored `.logs/ccc-evidence-tools-test.log`。RF 原始 capture/SDK/receipts 在各 attempt 内。

### 最终复核

拓扑修复、命令卡片与文档完成后：`tools-fixture-test` **927 passed / 149.140s**，完整 Jest **16 suites / 484 passed**，`host-check`、`fmt`、`lint` 通过；`docs-check-mermaid` **152 files / 27 diagrams** 通过，五份 authored Markdown 的 UTF-8/fences/whitespace/conflict checks 通过。Jest 最初发现一项浏览测试仍匹配旧 write-reject 命令字符串，更新关联断言后整套重跑通过，不修改实际测试语义。

两份 CCC passed bundle、首份 CCC failed bundle 以及前次 Android/iPhone write-reject passed bundles 的 `host-rf-evidence-check` 全部通过；各 bundle 原 verdict 保持不变，离线重放不重新授权 RF。两个新增 Just recipe 的 dry-run 成功，未额外操作设备。独立只读审阅未发现成功判据、ACK/cleanup 或 original-ticket 保留方面的阻塞问题；不替代这些实测与重放。

## 证据边界

有界 silence 不是 radio/CoreBluetooth/Android/Nitro 全局 flush，也不证明今后永远无晚到回调；FFF1 fence 是独立 native progress，不是读取 FFF2（Apple 在禁用/失败 barrier 上禁止这类有歧义 read）。`SET_VALUE` success 只表示本地更新/可能发送的 admission，`ef` 的 delivery 来自独立 App notification；`f0` disabled stimulus 并不制造一次成功发送。

Android best-effort local notification compensation 未导出可证回执，不能由无通知或零 subscription 反推为调用成功。已有文档/native disable 顺序差异仍需单独审阅。本项未修改生产 backend、安全 watchdog、public BLE API、UniFFI DTO 或 Nitro spec。

`IOS-RADIO-GATE` 继续 deferred。该次证据不更新历史 full-suite、native HIL、clean artifact consumer、10,000 lifecycle、long stress、Android API-tier 或 firmware 内部 scheduling/resource failure 覆盖。相关报告中的失败保持失败；没有 commit/push。
