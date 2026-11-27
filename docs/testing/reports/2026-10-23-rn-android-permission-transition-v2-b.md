# RN Android permission transition v2-B report

日期：2026-10-23

## 结论

Android permission transition 在 v2-B DK-B 上通过。该 gate 保持独立语义：denied permission、active held-read、权限撤销后的 OS/process 证据、fault retirement、fresh restored process 和恢复后的 RF 均分别记录，不并入 full-suite。

- Android：OnePlus `3ac5cc09`
- DK-B：`jlink:1050251479`
- image：`nrf-multifunction-rf-v2-b`
- profile：`blehub-nrf52840-multifunction-v2-b`
- run ID：`e063a0fa-fbfb-4ba4-b7ed-d9d7584b1059`
- evidence bundle：`tests/consumers/react-native-internal/android/build/rf-permission-transition-bcc734f51eec417684b1dc2779829931/evidence-bundle.json`

## Passed boundaries

- Denied process：adapter PoweredOn、scan authorization Denied。
- Scan dispatch 在缺少 `BLUETOOTH_SCAN` 时收到 SecurityException/permission error。
- Denied phase resource-zero 与 close。
- Fresh active process：baseline scan/connect/GATT/read/write/disconnect 通过。
- Active baseline resource-zero。
- Active second connection/GATT route 与 exact characteristic route 通过。
- Pending read accepted，native watchdog/pending resources 正确记录。
- Permission revoke 后 fault owner retirement/physical disconnect 通过。
- Fresh restored process：权限 Authorized、scan/connect/GATT/read/write/disconnect 通过。
- Restored resource-zero。
- Physical：3 connections、3 disconnections、advertising stopped、hold inactive、peer count 0、cleanup errors 0。
- Evidence bundle offline verifier 通过。

## 编排修复

active baseline 的普通 RF I/O 会把 read-write characteristic 写成 `10`/`ab`。旧编排随后直接进入 permission ARM，并要求 pre-state 为 `00`，造成 `permission before value drift`。

当前 `PermissionTransition` 在 baseline close、无 peer、第二次 advertising/connection 之前，复用 v2 profile 的 canonical `initial_value` 对所有 characteristic 做 bounded GATT reset，然后 `_arm()` 继续严格验证 `00` pre-state。没有放宽 drift 检查，也没有新增 evidence boundary；原有 12-boundary schema 保持不变。Synthetic permission tests：`10 tests OK`。

## 失败边界

- 首次 permission attempt：denied phase 和 active baseline 已通过，但在 active-held 前因 baseline value drift 失败；原 failed bundle 保留。
- 第二次尝试：baseline reset 后进入 physical/permission provider，但旧 runner attempt 的 fixture identity 曾失配；该失败也保留。
- 最终 v2-B run 在修复和 cleanup-only recovery 后通过。

## 未由本报告证明

- stock/non-root Android API-tier 全覆盖。
- 10,000 lifecycle、long-running stress。
- 最终 public ABI/Phase 5 freeze。
- Linux/BabbleSim scheduling/resource-failure/illegal interleaving。
