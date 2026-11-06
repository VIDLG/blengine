# Android production native HIL smoke

## Scope and evidence boundary

This opt-in entry extends the existing [Android artifact consumer](../../tests/consumers/android/blehub/)
with one direct Kotlin production path: `BleHub` + `AndroidBleBackend` + `BleEventSink`.
It does not use RN, Nitro, BLeng, a second native image, a new App or a generic runner.

The single smoke is:

1. Initialize and require powered-on/authorized adapter state.
2. Scan with the attested profile's service UUID **and** local-name prefix, obtaining the Peripheral
   ID from an advertisement. This is one combined-filter smoke, not a Cartesian filter matrix.
3. Stop scanning, connect and discover the production GATT database.
4. Resolve characteristic IDs from that database by the profile's UUIDs; read the initial value,
   response-write a profile-sized distinct value and verify it by remote readback. Then perform
   a no-response write with a second distinct, equal-length value and independently read it back.
   Both successful writes require `CharacteristicWritten` followed by `CommandCompleted`, per
   Plan §8.7; no-response events still mean only local submission, not remote acknowledgement.
5. Enable notifications; only after the matched enable event and command completion does the
   host set the profile's notification value through the leased BTP fixture. Require the native
   notification's connection/GATT/subscription/characteristic identity and bytes.
6. Disable notifications, disconnect, dispose/destroy the Hub/backend and join the caller's Handler.

`NativeHilSmokeTest` is skipped without the host's explicit `nativeHil=writes-v2` argument.
This versioned opt-in prevents an older with-response-only test APK from silently passing as
both-write coverage; the existing instrumentation parser treats skips as a failed gate.
The existing `PublishedArtifactConsumerTest` entry remains separate and does not acquire RF.
This smoke has **not been executed on hardware as part of its implementation**. Host-only tests
and APK compilation are not RF passes, package/ABI freeze, lifecycle stress or phase exits.
Negative BLE combinations, recovery/radio/permission transitions and long stress remain outside
this entry; it does not duplicate the RN suite or close their gaps.

## Entry and prerequisites

The [host owner](../../tools/android/native_hil.py) requires an explicit physical Android serial,
BTP port, canonical v2 contract and matching profile. The selected installed image must already
match. Use the reviewed [v2 contracts](../../tests/peripherals/nrf52840/firmware/contracts/)
and [profiles](../../tests/peripherals/nrf52840/firmware/profiles/), not historical attestation
files as authorization. No flashing, firmware reset, Android radio change, permission mutation,
SDK installation or destructive retry is performed by this entry.

Build/publish the intended candidate incrementally through `pixi run just ffi android-backend-consumer-build`
before a separately authorized RF run. Use the separate `pixi run just ffi android-candidate-build`
only when explicitly reconstructing a clean candidate. The consumer continues to resolve its production
backend and UniFFI/native image transitively from the staged Maven artifacts; no checkout-source
or generated-bindings dependency is added to its Gradle project.

The phone must be powered on and already grant this consumer package
(`dev.blehub.ffi.runtime.consumer`) `BLUETOOTH_SCAN` and `BLUETOOTH_CONNECT` on API 31+,
or `ACCESS_FINE_LOCATION` on API 24–30. A first install with missing grants fails before launch
and needs explicit setup, not automatic grants/revokes or an automatic retry. With user authorization,
ADB/root may perform this preparation and read back both user-0 grants; the ordinary RF runner
still never changes permissions itself.
Older Android scanning prerequisites still apply; compiling this smoke does not establish
coverage of additional Android versions. An already-running consumer or an occupied phone/DK
lease is rejected rather than taken over.

Current executable entry (replace the device and port placeholders; **do not run without fresh
RF authorization**):

```sh
pixi run -e android python -m tools.android.native_hil \
  --device '<physical-adb-serial>' \
  --port '<nrf52840-btp-port>' \
  --contract tests/peripherals/nrf52840/firmware/contracts/multifunction-rf-v2-b.json \
  --profile tests/peripherals/nrf52840/firmware/profiles/multifunction-rf-v2-b.json \
  --timeout 120
```

The maintained Just entry is `pixi run just ffi android-native-hil` with explicit `device`, `port`,
`contract`, `profile` and optional `timeout=120`. It runs prebuilt artifacts: neither the recipe nor
the module builds, provisions firmware or installs toolchains as a fallback. Build and run remain
separate; a stale or missing input must be resolved before authorizing another device attempt.

## Ownership, bounds and verdict

- Reuses `AndroidRadioController.acquire/initial/finish_observation` for the cooperative phone
  lease and read-only ON checks. It deliberately never calls the mutating `finish` path.
- Reuses `DirectFixture` with `prepare_session`; startup attests the installed image under the
  actual DK lease before canonical profile/value/advertising setup. Live receipt and lease checks
  precede **each APK install and instrumentation launch**. Neither a copied receipt nor a
  contract hash alone authorizes a run. This is cooperative evidence, not cryptographic attestation.
- Snapshots both APKs and records their SHA-256 hashes. Keeps the immutable fixture request,
  contract/profile snapshots and real live receipt in the same attempt directory.
- Uses the existing bounded installer/instrumentation owners. Native event waits share a 90-second
  budget with a 20-second individual wait; JUnit has a 110-second limit. The host instrumentation
  timeout defaults to 120 seconds and accepts only 100–180. Raw instrumentation capture is capped
  at 1 MiB; installer/ADB operations have separate finite deadlines. There is no retry loop for RF.
- Accepts only this attempt's ordered native phases, including `closed` after disposal and Handler
  termination. A BTP `set_value` acknowledgement is not by itself delivery proof. JUnit success is
  not by itself host success. Host completion also requires idle fault engine/no connected peer,
  fixture teardown and final read-only phone observation/release.
- Cleanup failures stay separate from the primary failure in `result.json`. A still-running Android
  process can be stopped only when its PID and `/proc` start ticks match this attempt; missing,
  changed or unreadable identity is a cleanup failure, never permission to stop a foreign process.
  Process termination is not relabeled as successful native close or resource-zero proof.

Full output and verdict are under ignored
`.work/validation/android-native-hil/<attempt-uuid>/`: `host.log`, `instrumentation.log`,
`fixture-session/fixture-receipt.json`, `phone-observation.json`, snapshotted APKs and `result.json`.
Only a complete, cleanup-successful hardware attempt may emit `android_native_hil=passed`.
Synthetic tests use temporary directories and never contribute to that evidence directory.

## Hardware-free validation

The host regression module is automatically included by the existing
`tools/tests/react_native/test_*.py` discovery. It intercepts actual ADB subprocess/install/launch
boundaries and fixture serial/lease resources; it keeps receipt validation, fixture lifecycle,
phone lock/observation and the instrumentation parser real. Its synthetic success means only
that host ownership and failure gating behaved as expected.

Targeted host test:

```sh
pixi run python -m unittest tools.tests.react_native.test_android_native_hil -v
```

Compile against already staged artifacts without publishing, device use or SDK downloading:

```sh
pixi run -e android python -m tools.run_android -- gradle \
  -p tests/consumers/android/blehub :app:assembleDebug :app:assembleDebugAndroidTest \
  --offline --no-daemon -Pandroid.builder.sdkDownload=false
```

Capture validation output to ignored `.work/validation/` files and bound command deadlines.
Use the relevant combined discovery once for cross-provider/controller changes; do not append the
RN-only subset afterward. Long-running stress is deferred according to [Plan §14](../../.plan/PLAN.md)
and is not part of this bounded native smoke or a prerequisite for starting BLeng code review.
