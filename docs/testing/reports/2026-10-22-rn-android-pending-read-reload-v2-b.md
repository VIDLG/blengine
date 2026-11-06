# RN Android pending-read reload v2-B candidate report

日期：2026-10-22

## 结论

Android RN pending-read reload gate 使用 release candidate、真实 OnePlus Central 和 DK-B v2 Peripheral 通过。该场景在一个 App/PID 内验证两个 RN/native engine generation：第一代持有真实 pending read，随后执行 engine invalidation/close；旧 owner 的晚提交必须被拒绝；第二代重新建立 Probe、GATT route 并完成 read。

该结果是独立 `pending-read-reload` candidate evidence，不是 full-suite 的第 11 项，也不替代 permission/adapter transition、CCC REJECT、最终 ABI 或长期压力覆盖。

## Identity

- APK：`tests/consumers/react-native-internal/android/app/build/outputs/apk/release/app-release.apk`
- Android serial：`3ac5cc09`
- Android model：`PHB110`
- DK-B BTP：`/dev/cu.usbmodem0010502514791`
- physical identity：`jlink:1050251479`
- service：`fd700000-0000-4000-8000-000000000000`
- image：`nrf-multifunction-rf-v2-b`
- image version：`2`
- run ID：`de3cb22d-aa23-4602-a3e2-d6e0c80b7018`
- evidence bundle：`tests/consumers/react-native-internal/android/build/rf-pending-read-reload-6ae7ae4f1eb64652a2e7ca13a46903aa/evidence-bundle.json`

## Passed boundaries

- Live v2-B contract/profile/image receipt 与 DK-B physical identity 绑定。
- Generation 1 native ready、initialize、scan、connect、GATT discovery。
- 真实 pending read 已 dispatch 并收到 native read accepted/ARM evidence。
- Generation 1 engine invalidation close started、dispose returned、engine close complete。
- Generation 1 late submit 被 fail-closed 拒绝：`result=closed`。
- Generation 2 native ready、initialize、scan、connect、GATT discovery。
- Generation 2 read 完成，value=`00`。
- Generation 2 close 完成。
- Evidence bundle status=`passed`、completed=`true`、cleanup errors=`[]`。
- Offline evidence verifier 通过。
- App kept-open。

## 仍未证明

- Android permission transition 的 denied/active/fresh-process OS reason proof。
- Android adapter startup-off/active-loss transition。
- response-write / CCC enable / CCC disable REJECT 独立 gate。
- 10,000 lifecycle、long-running stress 和 Android API-tier coverage。
- 最终 public ABI/private RN contract freeze。
