# React Native Android production backend → nRF52840 DK RF

**Date:** 2026-10-03
**Scope:** private `@blehub/react-native-internal` Nitro bridge, production Android backend, one physical Android Central and one standalone nRF52840 DK

## Topology

- Android Central: OnePlus 11, ADB serial `3ac5cc09`
- Peripheral: standalone nRF52840 DK, J-Link serial `1050252028`
- RN host: React Native `0.87.1`, Nitro `0.37.1`
- Production path:

```text
RN shared App.tsx
→ Nitro HybridBleHubProbe
→ Kotlin production AndroidBleBackend
→ Stable UniFFI BleHub
→ Android Framework BLE
→ nRF52840 DK
```

The run used the provider-filtered service UUID
`fd000000-0000-4000-8000-000000000000`. No phone Peripheral, Dongle, second DK,
Tauri host, or fake Android backend was used for these RF cases.

## Executed gates

The release RN host was built once and installed with the project-owned
root-aware `adb push + su -c pm install` flow. RN CLI Android autolinking,
production initialization, and the APK single-image gate were checked as part
of the host recipe.

The following independent runner modes then passed against the same candidate
and DK profile:

| Gate | Result | Evidence |
|---|---|---|
| Scan/filter/stop | passed | `tests/consumers/react-native-internal/android/build/rf-runtime.log` |
| Connect/disconnect | passed | `.../rf-connect-runtime.log` |
| Aggregate GATT discovery | passed | `.../rf-gatt-runtime.log` |
| Characteristic I/O | passed | `.../rf-io-runtime.log` |
| Connected RSSI | passed, `-58 dBm` | `.../rf-rssi-runtime.log` |
| PlatformDefault Notification | passed, actual mode `Notification`, value `ef`, 2 s post-disable silence | `.../rf-notification-runtime.log` |
| Strict Indication | passed, dual-property rejection plus pure Indication, value `ef`, 2 s post-disable silence | `.../rf-indication-runtime.log` |
| Passive disconnect/recovery | passed, `commandId=none`, stale-route silence, recovery connection | `.../rf-passive-disconnect-runtime.log` |

The ledgers reported scan session `9004`, the first connection `9007`, and
separate GATT/subscription/command IDs. The passive-disconnect gate observed
an unsolicited disconnect (`commandId=none`) before the recovery connection.

## Runner hardening found during this run

The first Indication attempt did not reach BLE result checking. `adb logcat`
returned a non-UTF-8 byte in unrelated log output and the Python runner raised
`UnicodeDecodeError`. The runner now reads subprocess output with UTF-8 and
`errors="replace"`, preserving structured `BLEHUB_NITRO_RUNTIME` markers while
making unrelated binary/noisy logcat safe. The rerun passed the complete strict
Indication gate. This was a test-controller robustness failure, not an
Indication or Android backend RF failure.

## Non-RF runtime/lifecycle gates on the same device

The same release host and OnePlus 11 also passed:

| Gate | Result | Evidence |
|---|---|---|
| Basic contract runtime | passed | `tests/consumers/react-native-internal/android/build/runtime.log` |
| Checkout-host lifecycle stress | passed | `tests/consumers/react-native-internal/android/build/lifecycle-stress.log` |
| Clean tarball-host lifecycle stress | passed | `.work/react-native-android-artifact-consumer/run-4883c2bcd9484718a3819a5b59a68dbf/logs/lifecycle-stress-release-retry3.log` |

`host-android-lifecycle-stress` uses the shared `App.tsx` lifecycle flow and
checks eight probe recreate/close cycles, listener removal, late-callback
silence, and post-close dispatch rejection. The new Android controller stops
on structured failure or terminal marker, persists the bounded log, invokes the
existing checker, and force-stops only the host package. It does not use a
fixed sleep as its completion authority. The clean artifact-device variant
builds a release bundle from the installed npm tarball and generates Metro
configuration from `require.resolve()`; it does not depend on checkout module
paths or Metro. This is host/Nitro/production-backend lifecycle evidence, not
RF evidence.

Android JS engine reload/invalidation is still deliberately unclaimed. The
current host has no verified RN 0.87 Android runtime-reload hook; restarting an
Activity or process would not prove JS runtime invalidation. Pending Promise
cleanup, queue overflow behavior, and reload therefore remain separate work.

The corresponding RN artifact-only Android consumer had already passed in this
candidate: npm tarball install, clean-host RN CLI resolution, fixture-Maven
Gradle build, and the APK single-image check. The RF/runtime controller tests
and targeted Ruff checks also passed.

This report proves the private RN low-level bridge through the production
Android backend for one DK and one Android Central. It does **not** prove:

- RN reload/invalidation, pending Promise cleanup, or queue overflow gates;
- two-DK concurrency or interference isolation;
- Android production multi-device policy;
- the public `@blehub/react-native` package or BLeng API;
- ABI freeze or Phase 5 completion.
