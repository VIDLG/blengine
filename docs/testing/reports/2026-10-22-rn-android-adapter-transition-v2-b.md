# RN Android adapter-transition v2-B candidate report

日期：2026-10-22

## 结论

Android RN `adapter-transition` 的 `startup-off` 模式使用 release candidate、真实 OnePlus Central 和 DK-B v2 Peripheral 通过。该场景证明 Android adapter OFF 时 command rejection、同一 App/PID/boot 的 ON restoration、fresh Probe RF I/O 和 cleanup；不等于 active-loss held-read 模式或 permission transition 通过。

## Identity

- APK：`tests/consumers/react-native-internal/android/app/build/outputs/apk/release/app-release.apk`
- Android serial：`3ac5cc09`
- Android model：`PHB110`
- DK-B BTP：`/dev/cu.usbmodem0010502514791`
- physical identity：`jlink:1050251479`
- service：`fd700000-0000-4000-8000-000000000000`
- image：`nrf-multifunction-rf-v2-b`
- image version：`2`
- run ID：`0b41a1d8-ad29-40a8-88f5-c2daa4c8a52c`
- evidence bundle：`tests/consumers/react-native-internal/android/build/rf-adapter-transition-d9370f44cf51431f8c4f917732114f46/evidence-bundle.json`

## Passed boundaries

- Live v2-B image/profile/contract receipt 与 physical DK identity 绑定。
- Android adapter OFF snapshot：`PoweredOff + Authorized`。
- OFF scan command 明确拒绝为 `adapter_unavailable`。
- OFF native resources 全零。
- 同一 Android App PID/boot 在 OFF→ON 期间保持稳定。
- Bluetooth 恢复为 ON，authorization 保持 Authorized。
- Fresh Probe scan、service advertisement、scan silence、connect、GATT discovery、read/write、disconnect 通过。
- GATT/I/O transcript：initial `00`、response write/read `10`、without-response write/read `ab`。
- Probe close、listener removal、ON resources 全零。
- Evidence bundle status=`passed`、completed=`true`、cleanup errors=`[]`。
- 最终 Android Bluetooth 状态为 ON。
- Offline evidence verifier 通过。
- App kept-open。

## 仍未证明

- Android `active-loss` held-read transition。
- Android permission transition 的 denied/active/fresh-process OS reason proof。
- response-write / CCC enable / CCC disable REJECT 独立 gate。
- 10,000 lifecycle、long-running stress 和 Android API-tier coverage。
- 最终 public ABI/private RN contract freeze。
