# RN Android pending-read reload v2-B report

日期：2026-10-23

## 结论

Android pending-read reload 在 DK-B v2-B cleanup/re-assert 后通过。该场景使用同一 Android App/PID、两个独立 RN engine/generation，验证真实 pending read 在 engine invalidation 后被安全关闭，并由第二代 Probe 完成 fresh read。

- Android：OnePlus `3ac5cc09`
- DK-B port：`/dev/cu.usbmodem0010502514791`
- physical identity：`jlink:1050251479`
- image：`nrf-multifunction-rf-v2-b`
- image SHA-256：`d57953806f0195dff0b175d80b09279d64e5b2d82b15198de77bd9ebd54e75f7`
- profile：`blehub-nrf52840-multifunction-v2-b`
- run ID：`ed34ccde-1bba-4d90-bda5-ec95db510538`
- evidence bundle：`tests/consumers/react-native-internal/android/build/rf-pending-read-reload-bf3dbbd666d943b1a0f187907b319e18/evidence-bundle.json`

## Passed boundaries

- Generation 1：native ready、initialize、scan、connect、GATT discovery。
- Generation 1：pending read accepted and dispatched on exact characteristic route。
- Engine invalidation close started and native dispose returned。
- Late submit after close was rejected as `closed`。
- Generation 1 engine close completed。
- Generation 2：fresh native ready、initialize、scan、connect、GATT discovery。
- Generation 2：fresh pending-read arm and dispatch。
- Generation 2 read returned `00`。
- Generation 2 close passed。
- Physical cleanup：advertising stopped、hold `RETIRED`/inactive、no known peers、cleanup errors zero。
- Offline evidence verifier passed。

## Recovery boundary

首次 attempt `rf-pending-read-reload-59604cf0f2c141348feaa0e71f6acb63` 保留为 failed evidence。它在 generation 1 scan 后因 DK physical cleanup 发现 foreign peer/advertising race 而 fail closed：`advertising_stopped=false`、`no_known_peers=false`。没有重写该失败证据，也没有直接重试。

随后通过 nrftest explicit DK-B config 完成 v2-B live attestation，并使用现有 `DirectFixture` 做绑定 session 的 recovery start/stop。第二次 pending reload 才获得 fresh live receipt 并通过。nrftest 默认 config 指向 DK-A，不能用于 DK-B；本次使用了明确的 DK-B serial/port/debugger identity。

## 未由本报告证明

- Android permission transition / OS reason-of-death proof。
- Android adapter active-loss transition。
- 10,000 lifecycle、long-running stress 和 Android API-tier coverage。
- 最终 public ABI/Phase 5 freeze。
