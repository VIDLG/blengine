# Android pending-read reload：同 PID、两个 RN engine 的独立 RF gate

日期：2026-10-14

## 结论与 candidate 边界

**Android 独立 `pending-read-reload` gate 已真实通过：同一 Android App PID、两次不同 JS runtime nonce、一次 native-validated reload。**第一代真实 ATT read 被 DK 保持在 WAITING 后才触发 reload；旧 engine 的 `beforeDestroy` native owner 首先关闭 probe，完整 cleanup 成功后第二代才 initialize，并完成新的 ARM/WAITING/RELEASE/read `00`/close。Host、专项 ledger、physical evidence 和最终 bundle 均 passed。

这是 **same App process / two engines**，不是 same JS runtime continuous10，也不是 continuous10 的第 11 个 flow。整个 gate 共用一个 provider lease/BTP session/live receipt；无 flash、无 firmware 修改，DK-A 保持原 multifunction contract 和 installed image SHA-256 `dd7dea99a325db32fa1fd66648d0a0814c8c7c442a77cf08998f2b91a1cc5b7a`。没有 commit、清锁或 release 升级。

本轮修改的是 RN host/private Nitro/native lifecycle 与测试控制链，**production Rust/backend ABI 未修改，但未冻结的 private Nitro contract 已改变**：追加 `awaitPendingReloadReady(runId, generation)`；Swift 提供 Android-only reject stub。Android pending gate 和 continuous10 回归不能推断 iOS 结果，因此后续独立重建/签名 iOS 真机 App 并完成 `7fedcc…` 连续 10/10 回归，详见下节。同当前 private Nitro/shared RN 实现的两端 continuous10 回归已补齐；不声明 iOS 的两代 pending API parity、source-to-build attestation 或 release 全门禁通过。

## 实现与关闭 authority

| 层 | 入口与职责 |
|---|---|
| Host | [`run_android_pending_read_reload.py`](../../../tools/react_native/run_android_pending_read_reload.py) 冻结必需的 `--apk`，live fixture proof 后安装；固定 PID logcat，检查控制前后/运行中 PID，实际动作严格为 `arm-1 → reload → arm-2` |
| 专项 checker | [`pending_reload_ledger.py`](../../../tools/react_native/pending_reload_ledger.py)、[测试](../../../tools/react_native/test_pending_reload_ledger.py) 审计两代 routes/nonces、native close 顺序、receipt、physical boundaries、预算和 cleanup；[host 测试](../../../tools/react_native/test_run_android_pending_read_reload.py) 覆盖控制/capture 边界 |
| Peripheral | [`pending_reload.py`](../../../tests/peripherals/nrf52840/pending_reload.py)、[测试](../../../tests/peripherals/nrf52840/test_pending_reload.py)，经 [`provider.py`](../../../tests/peripherals/nrf52840/provider.py) / [`direct_fixture.py`](../../../tests/peripherals/nrf52840/direct_fixture.py) 的 `android-pending-reload` mode 使用现有 multifunction hold |
| Native owner | [`BleHubProbeLifecycleOwner.kt`](../../../integrations/react-native-internal/android/src/main/java/com/margelo/nitro/blehubreactnative/BleHubProbeLifecycleOwner.kt) 按旧 `ReactApplicationContext` identity 强持有 probe 至 cleanup；[`HybridBleHubProbe.kt`](../../../integrations/react-native-internal/android/src/main/java/com/margelo/nitro/blehubreactnative/HybridBleHubProbe.kt) 关闭所有 listener/handoff routes，再异步 dispose/close Hub、停止并 join backend/delivery threads，检查 `isAlive` |
| App host | [`MainApplication.kt`](../../../tests/consumers/react-native-internal/android/app/src/main/java/com/blehubnitrohost/MainApplication.kt) 注册 `beforeDestroy`；[`MainActivity.kt`](../../../tests/consumers/react-native-internal/android/app/src/main/java/com/blehubnitrohost/MainActivity.kt) 原生校验 matching runId/exactly-once reload，并对同一 stopped surface 调用 `updateInitProps` 更新 generation 2 |
| RN/private API | [`App.tsx`](../../../tests/consumers/react-native-internal/src/App.tsx) 的共享 pending mode 在 Android 使用 native props，无 Settings fallback；[`BleHubProbe.nitro.ts`](../../../integrations/react-native-internal/specs/BleHubProbe.nitro.ts) 声明新 barrier；[Swift stub](../../../integrations/react-native-internal/ios/HybridBleHubProbe.swift) 只 reject，不提供 iOS 对等 gate |

本地 **RN 0.87.1** 的 [`ReactHostImpl.kt`](../../../tests/consumers/react-native-internal/node_modules/react-native/ReactAndroid/src/main/java/com/facebook/react/runtime/ReactHostImpl.kt) reload 路径为 `stopSurface → beforeDestroy → destroy old ReactContext/engine → create engine → restart surface`；它不保证旧 JS effect cleanup 完成。App generation 1 的 effect **故意不调用 close**，保证 native hook 是首个 close authority；若 JS 先 close，不能重标为 hook close。Native owner 在关闭失败时不能放行新代；thread 未真正停止时保留旧 context，不能因 join 超时而宣称 cleanup 成功。

generation 2 的 JS 注册 listeners 后调用 `awaitPendingReloadReady(runId, 2)`，等待原生旧 owner cleanup 成功，再 initialize。更新 Activity intent/getLaunchOptions 本身不足以更新 RN 重启使用的同一个 surface，必须更新 surface init props。Private control 只允许显式匹配本轮 runId 的 pending session；两个 nonce 还必须配合同 PID capture/no PID replacement，不能用 App 重启冒充 engine reload。

**`native:late-submit-rejected:source=synthetic:result=closed` 是向保存的旧 handoff 提交 synthetic read 后得到 CLOSED。**它不是观察到真实晚 `BluetoothGattCallback`，也不是 Rust ingress late event；`dispose-returned` 单独不等于 full thread stop，只有随后 join/isAlive 成功的 `engine-close-complete` 才放行 native-ready。Native `read-accepted` 仅表示 native dispatch 接受；真实 pending 的 entry authority 是 DK 同 sequence/handle/peer 的 WAITING。

## 具名 RF 结果

最终 [pending bundle](../../../tests/consumers/react-native-internal/android/build/rf-pending-read-reload-10b3686e19e84146a4d9dc21ee1d9cdb/evidence-bundle.json) 对应 attempt `10b3686e19e84146a4d9dc21ee1d9cdb`，`scenario=pending-read-reload`、`status=passed`。

| 身份/结果 | 本轮值 |
|---|---|
| Bundle run UUID（manifest `run_id`） | `74930d31-0517-4dd2-a835-fd8782810aed` |
| Fixture session / native-JS runId | `a3ad9de4-4ad2-4fdd-8ecd-c9c0d2cf3b55`；**与 bundle run UUID 是不同概念** |
| Android Central / 同一 App PID | OnePlus 11 `3ac5cc09` / `7126` |
| 旧 native probe | `aae0425c-027d-4a84-89fb-45d6bf747d8e` |
| Fixture PID/PGID / physical identity | `16262/16262` / `jlink:1050252028` |
| Generation 1 nonce | `mv86v5sv-qlyfn1ngbms-jehikkwh9a` |
| Generation 2 nonce | `mv86v8fs-btvt2h5vpet-0f7uejl8jfbm` |
| Hold sequences / target ATT handle | `23 / 24` / `41` |
| Target permissions | native `67 = 0x43`（READ/WRITE/PREPARE_WRITE），ordinary canonical profile `3`；不是修改 tracked profile，也不以该权限值替代真实 DK WAITING 或 BTP read authorization 证明 |
| Contract / profile SHA-256 | `3fe22ba286d36b6aa086c7ae92ecc7db27f07189536a539b1d5e03252a876750` / `aa735baac72d19d9f1fd2dcd6f4e1868e4ac61080aa9ee7767faf074a2dd2ed8` |
| 冻结 APK SHA-256 / size | `f9a135a4cd8c728bfed9808aaa1ed4c5e57af30fb89308b4a9e57d352012d72b` / `37,744,118` bytes |
| Physical whole observed span | `6.823619375 s`，不是完整 install/runner wall time |
| 收尾 | physical peer none、hold `RELEASED`、advertising stopped；primary error `null`，cleanup/evidence errors `[]` |

物理与 runtime 顺序：

1. 第一代 ARM sequence 23，DK 观察 exact peer/handle WAITING，真实 pending 后才准许 host reload。
2. native hook 首 close：routes closed → `dispose-returned` → synthetic old-handoff CLOSED → full thread stop `engine-close-complete`；随后第二代才出现 `native-ready` 并 initialize。
3. 显式 RELEASE 第一代 hold，验证 RELEASED 与 exact old-peer physical GAP disconnect，刷新 advertising；不靠 effect cleanup 或固定 sleep 假定旧连接已释放。
4. 第二代 fresh ARM sequence 24，重新 WAITING/RELEASE，读回 exact `00`，matching terminal 后观察一秒 silence，再 close；fixture 观察 physical disconnect，确认无 peer/inactive hold/stopped advertising，cleanup 无 error。

可点击原始证据：[raw logcat source](../../../tests/consumers/react-native-internal/android/build/rf-pending-read-reload-10b3686e19e84146a4d9dc21ee1d9cdb/pending-read-reload-runtime.raw.log)、[runtime ledger source](../../../tests/consumers/react-native-internal/android/build/rf-pending-read-reload-10b3686e19e84146a4d9dc21ee1d9cdb/pending-read-reload-runtime.log)、[专项 ledger](../../../tests/consumers/react-native-internal/android/build/rf-pending-read-reload-10b3686e19e84146a4d9dc21ee1d9cdb/pending-reload-ledger.json)、[physical evidence](../../../tests/consumers/react-native-internal/android/build/rf-pending-read-reload-10b3686e19e84146a4d9dc21ee1d9cdb/coordination/pending-reload-physical.json)。它们及 capture/identity/request/live receipt 均进入 bundle hash inventory；完整 native 第二代 event bodies 并非全部单独记录，checker 不等于独立 BLE/BTP replay。

### 同 APK continuous10 回归与保留失败

Native root lifecycle 修改后，Android 新 attempt `73174345d0244b52893f9957ca626ad9` 使用**同一上述 APK**完整通过 continuous REAL 10/10、host/bundle passed、`app=kept-open`，[regression bundle](../../../tests/consumers/react-native-internal/android/build/rf-lab-suite-73174345d0244b52893f9957ca626ad9/evidence-bundle.json) 已保留；该次 PID `8707` 保持不变并在运行结束时保留。这是另一次独立 continuous10，不与 pending attempt 拼接，也不是十次 recovery。

### 后续 iOS：独立重建并通过当前共享候选的 continuous10

在相同 private Nitro contract/shared RN 源码状态下，iOS physical-device Debug App 重新编译、生成 Metro/Hermes bundle、完成 development signing；从冻结 `.app` 安装后，独立跑完整十项。最终 [`7fedcc763c324e5ca0652b084e525b3b` bundle](../../../tests/consumers/react-native-internal/ios/build/rf-lab-suite-7fedcc763c324e5ca0652b084e525b3b/evidence-bundle.json) 为 `scenario=continuous10`、`status=passed`，host/physical checks 通过、App kept-open、fixture 已停止；离线完整性复核也通过。未刷 DK、未修改测试断言或实现，不复用旧 `f0c851…` 代替新运行。

| iOS identity / artifact | 本轮值 |
|---|---|
| Bundle UUID | `82a5f0fd-0d58-4649-a4af-f601a6aabe28` |
| Fixture session / native-JS session | `a2d673ce-edcf-4e36-bbcd-f842ff7a6ace` |
| Runtime nonce | `mv8801fl-b5nxzj64p86`，十项 start/close 一致 |
| App PID / boot | `43461` / `a0da7d4b-0484-4db2-8a7b-a4a7b05c7089` |
| Frozen app-tree SHA-256 | `7d26e125a7ecd02259cec3fb89394722b84fe355dada9e08eca90e1a2526c259` |
| `main.jsbundle` SHA-256 | `d8cc8071985d6660b9cff7483ebab8829e5f32991f5058143d188810696f04ca` |
| Fixture PID/PGID / target handle | `28800/28800` / `41` |
| Cancellation / watchdog sequences | `27 / 28` |
| Watchdog post-release old-route silence | `1002.836083 ms`，恢复读 exact `00` |
| Native / JS ACK | flow 8 三组、flow 9 四组，共七组各自完整 passed |
| Raw-file inventory / cleanup errors | `71` / `[]`，evidence errors `[]` |

两份 [cancellation](../../../tests/consumers/react-native-internal/ios/build/rf-lab-suite-7fedcc763c324e5ca0652b084e525b3b/coordination/flow-8-rf-cancellation/physical-evidence.json) / [watchdog](../../../tests/consumers/react-native-internal/ios/build/rf-lab-suite-7fedcc763c324e5ca0652b084e525b3b/coordination/flow-9-rf-watchdog/physical-evidence.json) physical evidence 均 passed；[native identity/ACK](../../../tests/consumers/react-native-internal/ios/build/rf-lab-suite-7fedcc763c324e5ca0652b084e525b3b/device-rf-lab-suite-continuous.attempt.rf-host-identity.json) 为 passed。[Console cleanup](../../../tests/consumers/react-native-internal/ios/build/rf-lab-suite-7fedcc763c324e5ca0652b084e525b3b/device-rf-lab-suite-continuous.attempt.console-cleanup.json) 为 `action=detached`；收尾后另用官方 `devicectl device info processes` 确认原 PID `43461` 和安装路径的 executable 仍存活，没有重启结果页。该查询未追加进已 final 的 attempt，避免改变 bundle inventory。

此轮验证的是真实 iOS continuous10 回归，不是调用 Android-only barrier、不是 iOS 新两代 pending gate、也不是全面 native/package/ABI freeze 证明。前轮 RN 134 / host 309 / Peripheral 237 为各自已执行的软件验证，本次未重跑这些 unit tests；本次实际新增的验证为 physical-device build/sign、完整 iOS RF、bundle 核验与原进程查询。

### 保留的 Android 早期失败

首轮 pending attempt `91127703962a41bab380ee9e050ef477` 的 [failed manifest](../../../tests/consumers/react-native-internal/android/build/rf-pending-read-reload-91127703962a41bab380ee9e050ef477/evidence-bundle.json) 保留不改写。ADB return 0 且返回精确文本 `Warning: Activity not started, intent has been delivered to currently running top-most instance.`，被 host 误判拒绝；修复仅允许该 exact line，其他 errors 仍拒绝。该失败尝试不是完整 RF gate pass。

## Bundle compatibility、运行与验证

Bundle 追加 scenario 区分 `continuous10` 与 `pending-read-reload`。离线 verifier 对旧 v1 **缺少 scenario** 的 manifest 只读解释为 continuous10，历史 manifest bytes 不变，不回填；pending evidence 缺 scenario 必须拒绝。新 pending/new regression 的专项 checker/bundle 离线复核均通过。Offline bundle verify 只复核 hash/inventory/引用与 scenario 结构，不重新授权 RF，不重复真实 Bluetooth callback，不将 failed manifest 升级为 pass。

普通运行必须显式提供设备、`--apk`、fixture contract/profile；port 从 ignored local config/环境变量解析，无硬编码机器默认值。安装冻结 APK 后运行，不刷机；需要可用且解锁的真机、已匹配 contract 的 DK 和独占 fixture。`host-android-pending-read-reload` recipe 已新增并通过 dry-run；本阶段真机结果来自直接 runner，不能把 dry-run 当成硬件运行。可先查看直接入口：

```sh
pixi run python -m tools.react_native.run_android_pending_read_reload --help
```

运行形状：`pixi run python -m tools.react_native.run_android_pending_read_reload <android-serial> --apk <built.apk> --fixture-contract <multifunction-contract.json> --fixture-profile <multifunction-profile.json>`。身份和路径由调用者/本机配置提供，不把本报告具名设备变为默认值。

两份新 bundle 的短复核入口（无硬件访问）：

```sh
pixi run just react-native host-rf-evidence-check tests/consumers/react-native-internal/android/build/rf-pending-read-reload-10b3686e19e84146a4d9dc21ee1d9cdb/evidence-bundle.json
pixi run just react-native host-rf-evidence-check tests/consumers/react-native-internal/android/build/rf-lab-suite-73174345d0244b52893f9957ca626ad9/evidence-bundle.json
```

本轮软件验证由执行 agent/主执行线报告，**本次文档更新不声称重跑其 build/test/RF**：

| 验证 | 已报告结果与边界 |
|---|---|
| Native agent builds | Android debug + test APK、iOS generic Simulator build passed；不是 iOS 真机 RF |
| OnePlus 11 instrumentation | 11 tests passed（包含 7 个新 lifecycle tests）；不是 RF |
| RN agent | Jest 134 tests、typecheck passed；synthetic UI/state-machine 验证 |
| Parent host / Peripheral full discovery | 309 / 237 tests passed；`peripheral nrf52840-test` recipe 的 109 是子集，不相加 |
| 聚焦子集 | pure checker 25、controller 20、bundle 34 passed；不叠加为 full suite 总数 |
| Parent formatting/lint | 主执行线实际运行 fmt/lint passed；文档 agent 不并发运行全局 fmt/lint |

## 剩余门禁

**Android pending reload 与当前共享候选的两端 continuous10 回归已完成，不再列为未完成项。**仍缺 adapter/permission transitions、current-candidate 10-cycle recovery、最终 same-candidate native/package/RN gates、source-to-build/设备端 binary byte attestation、clean locked release 和 ABI freeze；本轮 iOS continuous10 不等于 iOS 两代 pending gate parity。Manifest hash 是 host-submitted installation snapshot 完整性，不是签名/安全证明或源码到二进制证明。

架构状态与历史候选边界见 [`react-native-rf-lab-test-architecture.md`](../../research/react-native/react-native-rf-lab-test-architecture.md)；旧两端 bundle RF 记录见 [`2026-10-14-rn-rf-evidence-bundle.md`](2026-10-14-rn-rf-evidence-bundle.md)，该历史报告不回写。
