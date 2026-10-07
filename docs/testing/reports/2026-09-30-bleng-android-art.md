# BLeng candidate Android artifact and ART gates

Date: 2026-09-30

## Scope

This report records the Android portion of the reviewed-but-unfrozen `Bleng`
foreign API candidate. It covers the isolated two-component Android artifact,
clean Maven consumer, and Android ART instrumentation on one physical device.
It does **not** prove an Android production BLE Central backend, Android RF,
React Native/Nitro runtime, ABI freeze, or default production-package graduation.

- DUT: OnePlus 11, Android 16, adb serial `3ac5cc09`.
- Candidate image: one `blehub + bleng` native image per Android ABI.
- Candidate package: `dev.blehub:blehub-bleng-candidate:0.0.0`.
- Candidate staging: `.work/staged/android-bleng-candidate-maven/`.
- Default production package remains the single-component `blehub` image.
- The Android device exposes Magisk `uid=0`; the runner uses this only for
  package installation and does not change device security settings or require
  a UI confirmation. Non-root devices use ordinary `pm install`.

## Static candidate gates

The following passed:

```text
pixi run just ffi android-bleng-candidate-test-tools
pixi run just ffi android-bleng-candidate-build
pixi run just ffi android-bleng-candidate-audit
```

The candidate lane generated explicit `dev.blehub` and `dev.bleng` Kotlin
bindings, built the arm64-v8a and x86_64 native slices, published the isolated
AAR, and built an artifact-only consumer from the staged Maven repository. The
NDK `llvm-nm` audit found exactly the reviewed `blehub + bleng` UniFFI export
surface in one native image per ABI and rejected probe/test/extra-component
symbols. The consumer resolved no source project dependency.

## ART gates

The project-owned `tools/android/run_instrumentation.py` installs through
`adb push` to `/data/local/tmp`, then runs `pm install -r` (and `-t` for the
instrumentation APK) before invoking `am instrument`. It has bounded install,
execution, progress, timeout, process-group cleanup, and JUnit terminal-status
validation. This avoids the Gradle UTP streamed-install hang observed on this
OnePlus/Android 16 device.

The candidate consumer ART gate passed:

```text
android_instrumentation=passed
OK (5 tests)
```

The five tests covered invalid timeout rejection before backend dispatch,
powered-off/denied initialization snapshots, exactly-once scan events and
closed late ingress, shutdown of pending initialization plus a blocked event
reader, and Kotlin coroutine cancellation followed by explicit shutdown.

The existing single-component production Android runtime gates were also run
with the same runner:

| Gate | Result |
|---|---:|
| production generated bindings ART | `OK (10 tests)` |
| published-AAR artifact consumer ART | `OK (1 test)` |
| `blehub + bleng` candidate ART consumer | `OK (5 tests)` |

The production binding and consumer APKs were built from their existing
production staging. Their native production image was not replaced by the
candidate image.

## React Native Android host runtime

On 2026-10-01, the private RN/Nitro Android host was run on the same OnePlus 11
(`3ac5cc09`) with the project-owned root-aware installer. The corrected Just
invocation passed the device serial as a positional argument:

```text
pixi run just react-native host-android-runtime 3ac5cc09
```

The gate regenerated Nitro glue, passed RN CLI Android autolinking, built the
release host, installed it through `adb push` plus `su -c 'pm install -r ...'`
without the OnePlus graphical installation flow, launched
`com.blehubnitrohost/.MainActivity`, and passed the runtime log checker:

```text
android_apk_install_root=true
android_apk_install=passed
React Native Nitro runtime gate passed
```

The same host also passed the Android single-image audit:

```text
Android single-image gate passed: 2 UniFFI ABI slices and 2 Nitro slices
```

This is evidence for the private RN/Nitro Android host and its
deterministic/test backend handoff only. It does not prove the Android
production Central backend, Android BLE/RF, reload/invalidation, or BLeng
default-package graduation.

## Installation and runner correction

The first direct candidate ART attempt exposed a Kotlin test-fixture error:
`CandidateBackend` used contract version `1` instead of the canonical
`blehubContractVersion()` (`5`). The fixture was corrected and the consumer
was rebuilt; the subsequent run passed all five tests.

The first Gradle connected-test attempt did not execute tests: UTP spent about
five minutes in split-APK streamed installation and raised
`ShellCommandUnresponsiveException`. Direct `adb install --no-streaming`
worked for the candidate. The production consumer's ordinary `pm install` was
also observed to block; on this rooted DUT, `su -c 'pm install -r ...'`
completed immediately. The runner now detects `uid=0` and uses that bounded
root installation path when available, while retaining the non-root path.

The runner's regression tests cover successful Android `INSTRUMENTATION_CODE`
output, per-test failure status despite shell exit code zero, zero-test and
truncated output, installation success-marker/cleanup, and bounded timeout
cleanup:

```text
python -m unittest tools.android.test_run_instrumentation -q
Ran 10 tests ... OK
```

## Boundaries and next steps

The repository currently has no maintained Android production Central backend
under `platforms/`; the Android device in this run is only an ART/package DUT.
Therefore no Android CoreBluetooth-equivalent scan, connect, GATT, notification,
RSSI, passive-disconnect, or RF receipt is claimed. Android RF begins only
after the Phase 4 Android backend and its production-package path are
implemented and independently validated. The candidate remains opt-in and
unfrozen; the default `crates/blehub-uniffi` package is still `blehub` only.
