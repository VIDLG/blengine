# RN Android full-suite v2-B candidate report

日期：2026-10-22

## 结论

Android RN fixed candidate 完成一次真实 `full-suite`：同一 App/runtime 内顺序执行 10 个 base flows，随后交接到 RecoveryTen 完成 10 个 fresh-Probe recovery cycles。Host runner、RN/Nitro、生产 Android backend、真实 OnePlus Central 和 DK-B v2 Peripheral 均在本次 evidence bundle 中闭合。

该结果只属于本次 Android APK、OnePlus、DK-B、firmware receipt、contract/profile 和 run；不等于最终 public ABI/Phase 5 freeze，也不覆盖所有 Android API tiers、10,000 lifecycle 或 long-running stress。

## Candidate / device

- APK：`tests/consumers/react-native-internal/android/app/build/outputs/apk/release/app-release.apk`
- Android serial：`3ac5cc09`
- Android model：`PHB110`
- DK-B BTP：`/dev/cu.usbmodem0010502514791`
- physical identity：`jlink:1050251479`
- service：`fd700000-0000-4000-8000-000000000000`
- image ID：`nrf-multifunction-rf-v2-b`
- image version：`2`
- image SHA-256：`d57953806f0195dff0b175d80b09279d64e5b2d82b15198de77bd9ebd54e75f7`
- profile ID：`blehub-nrf52840-multifunction-v2-b`
- run/session ID：`e16a24c7-2098-45e3-8bc0-9e2a55b71394`
- full-suite ledger session：`36408cb6-b480-4588-85c0-d4f70c3748f0`
- evidence bundle：`tests/consumers/react-native-internal/android/build/rf-full-suite-0dcec6f62f2d4f25b006ffd501e08b8a/evidence-bundle.json`

## Passed boundaries

- Release APK 包含 `assets/index.android.bundle`。
- Android single-image gate：2 UniFFI slices、2 Nitro slices。
- APK package/activity identity 校验通过。
- Live v2-B image/profile/contract receipt 在 fixture lease 内匹配。
- One App/runtime：`app_runtimes=1`。
- Base flows：`base_flows=10`。
- Recovery cycles：`recovery_cycles=10`。
- Recovery terminal records：`100`。
- Recovery commands：`100`。
- Recovery events：`260`。
- Hub/Probe lifecycles：`10`。
- Notifications：`10`。
- Passive disconnects：`10`。
- Remote reads：`10`。
- Closed resource snapshots：`10`。
- Final recovery groups：20 connections、20 disconnections、10 closed snapshots。
- 每轮 closed resource totals 中 active scans、connections、GATT attributes、pending IO、subscriptions、watchdogs、listeners、worker threads、hub roots、lifecycle owners 等均为零。
- Physical cleanup：无 cleanup error，successful App 保持打开。
- Evidence bundle offline verifier：通过。

Base flow 包含 continuous10 约定的 scan/connect/GATT/I/O、notification/indication、passive disconnect、cancellation 和 watchdog；具体 semantic facts 以本次 ledger/raw evidence 为准，不能从 flow 数量推断未记录的额外覆盖。

## 构建入口边界

本次首次手动使用通用 `assembleDebug` APK 时，runner 正确拒绝了缺失 JS bundle 的 artifact，未进入 RF。Android debug APK 默认依赖开发服务器，不能作为离线 RF evidence candidate。随后使用 active RF recipe 对应的 `assembleRelease` 生成 bundled APK，artifact-only 检查通过后才运行 full-suite。

这不是放宽 evidence 检查；`assets/index.android.bundle` 仍是 RF APK 的硬性 admission 条件。普通 debug/instrumentation 入口与 release RF candidate 入口保持不同职责。

## 未由本报告证明

- Android 所有 API tier / stock non-root 权限行为。
- 10,000 lifecycle 与 long-running stress。
- 最终 public ABI/private RN contract freeze。
- iOS/Android 同一最终 production artifact 的 Phase 5 冻结。
- Linux/BabbleSim scheduling、资源失败与非法协议交错。
- firmware provisioning/reproducibility 的全部外部仓库闭环。

此前 debug APK 的 `missing Android JS bundle` 失败只属于安装前 artifact admission failure，不是 RF 失败，也不改写本次 release candidate 的通过结果。
