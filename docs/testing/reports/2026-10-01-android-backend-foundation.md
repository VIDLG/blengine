# Android production backend foundation

Date: 2026-10-01

## Scope

This report records the first Phase 4 Android production Central backend
foundation and the subsequent static connection/GATT/I/O slice. It does not
claim Android device BLE runtime, Android RF, or a frozen production ABI.

- Backend source: `platforms/android/library/`
- AAR coordinate: `dev.blehub:blehub-android:0.0.0`
- Canonical binding/native-image dependency: `dev.blehub:blehub-uniffi:0.0.0`
- Generated binding and JNI inputs are consumed from the existing production
  generation/staging recipe; they are not copied into the backend AAR.

## Implemented slice

`AndroidBleBackend` currently provides:

- Android `BleBackend` implementation and Android capability declaration;
- caller-owned dedicated `Handler`/`Looper` execution boundary;
- process-scoped backend ownership lease with typed busy construction failure;
- ingress binding and close-time ingress retirement;
- adapter initialization snapshot with API-level scan/connection authorization
  mapping;
- one exact scan reservation at a time;
- Android LE scan start/stop with callback-route correlation;
- service UUID, local-name-prefix, and company-ID predicate filtering;
- owned advertisement DTO conversion for service UUIDs, manufacturer data,
  service data, RSSI, and API-dependent connectability;
- passive scan-failure retirement without a duplicate start terminal;
- bounded local duplicate suppression when `allowDuplicates` is false;
- idempotent backend disposal of the owned scan route without owning the
  caller's Handler/Looper;
- one private peripheral-address map populated only by this backend's scan
  observations;
- connection routes with one active lifecycle per peripheral, monotonic
  generation identity, exact native `BluetoothGatt` object correlation,
  connecting/connected/disconnecting/disconnected event ordering, explicit and
  passive disconnect handling, and late-callback rejection;
- aggregate GATT discovery with one pending discovery per connection, Android
  service/characteristic/descriptor instance preservation, characteristic
  property mapping, all-or-error database publication, and old-database
  retention when a rediscovery fails;
- one serialized per-connection operation slot for characteristic reads,
  response/no-response writes, CCCD notification/indication configuration, and
  connected RSSI; and
- notification mode selection for PlatformDefault/strict Notification/strict
  Indication, CCCD completion, value-event correlation, and local no-response
  submission semantics.

These operations are statically compiled only. No Android backend operation,
GATT callback, or RF result is claimed by this report yet.

## Static/package validation

The following host-only validations passed:

```text
# Equivalent direct Gradle invocation after the bounded Android doctor check
platforms/android :library:assembleDebug
platforms/android :library:assembleRelease
platforms/android :library:publishReleasePublicationToFixtureRepository
platforms/android :library:compileDebugKotlin

tests/consumers/android :app:assembleDebug :app:assembleDebugAndroidTest
```

The Android library compiled successfully. The release AAR contains the
backend `classes.jar` and no `jni/` directory. Its published POM depends on
`dev.blehub:blehub-uniffi:0.0.0`; it does not republish the canonical generated
binding or native image.

The Android backend source was then structured into a public lifecycle façade,
`AndroidBackendState.kt` for the shared route/operation state model, and
`AndroidBackendErrors.kt` for platform error construction. This is an internal
source split only; it does not add a public class or a second native image.

Active Android builds now use the Huawei Cloud Maven mirror at
`https://mirrors.huaweicloud.com/repository/maven/`, with the staged local
repository retained first for project-owned artifacts. The RN Android Gradle
wrapper resolves its Gradle distribution from
`https://mirrors.huaweicloud.com/gradle/`. Both the production library and
clean consumer were rebuilt with `--refresh-dependencies`; dependency
resolution and compilation passed. `gradlePluginPortal()` remains as the
plugin-marker fallback.

The project recipe is:

```text
pixi run just ffi android-backend-build
pixi run just ffi android-backend-publish
```

On this Mac, nested Pixi activation can remain silent for minutes before the
Android toolchain command starts. The verified fallback is the existing
project-managed Android environment and direct Gradle invocation with its
explicit `JAVA_HOME`/`PATH`; this is an execution fallback, not a second build
or package path.

## Clean Android consumer

The existing clean Android consumer was switched from the binding-only
coordinate to the published backend coordinate:

```text
dev.blehub:blehub-android:0.0.0
```

Its POM resolved `dev.blehub:blehub-uniffi:0.0.0` transitively from the staged
Maven repository. The consumer APK and instrumentation APK built successfully.
On the OnePlus 11 (`3ac5cc09`), the project-owned root-aware instrumentation
runner installed both APKs through `adb push` plus `su -c pm install` and ran
all three consumer tests successfully:

```text
INSTRUMENTATION_STATUS: numtests=3
android_instrumentation_root=true
android_instrumentation=passed

The same consumer was rebuilt after the backend capability declaration changed to
advertise connected RSSI support, then rerun on the same OnePlus 11 with the
updated staged AAR. The three-test instrumentation result remained passed; the
additional test initializes the real Android backend through `BleHub` and
receives `AdapterSnapshotChanged`. It does not scan or perform BLE RF.
```

The new test loaded `AndroidBleBackend`, initialized the production `BleHub`
object graph, received `AdapterSnapshotChanged`, verified the Android
capabilities and contract version, and disposed it while the caller-owned
`HandlerThread` remained caller-owned. This is an artifact/foreign-lifecycle
and initialization gate only; it did not scan, connect, perform GATT I/O, or
perform BLE RF.

## React Native Android production runtime

The private Nitro Android probe now obtains the application context through
Nitro's supported `NitroModules.applicationContext` API, creates a caller-owned
`HandlerThread` for `AndroidBleBackend`, and disposes the Hub before stopping
that thread. The RN host also passes runtime mode and service UUID through launch
extras and declares Android BLE permissions. The scan event projection covers
adapter snapshots, scan start/advertisement/stop, command completion, and
backend errors; connection/GATT/I/O projection remains fail-closed until its
full event catalog is mapped and tested.

The Huawei-Cloud-backed RN host was rebuilt and its single-image audit passed.
The OnePlus 11 (`3ac5cc09`) ran the production RN contract smoke successfully:

```text
command-id-validation:passed
round-trip:passed
dispatch:accepted
completion:18446744073709551615:ok
listener-removal:passed
close:resolved
```

This is production Android backend initialization/lifecycle evidence, not scan,
connection, GATT, or Android RF evidence. The runtime recipe now waits a bounded
12 seconds before collecting logcat because the OnePlus can emit the first JS
markers several seconds after process launch; it does not stream the build log.

## nRF52840 fixture status

The current machine-local BleHub configuration resolves Android SDK from
`[toolchains].android_root` in ignored `blehub.local.toml` and resolves the
nrftest checkout from `[toolchains].nrftest_root`. The nRF endpoint is still a
single local override under `[peripheral_nrf52840].port`; it is not a portable
board identity.

The nrftest checkout currently targets the old PCA10059 application firmware
identity (`2FE3:0004`). The connected macOS host currently exposes one SEGGER
J-Link OB serial (`1050252028`) and two CDC paths belonging to that one J-Link,
not two independent DKs. The provider doctor can inspect the configured old
fixture, but provider start rejected the current path because it was not the
nrftest application identity and exited safely after the bounded readiness
window. No Android RF result was produced. The blank/new DK requires explicit
DK firmware provisioning and a board-aware nrftest target before it can become
an RF provider; changing a BleHub port string would be the wrong fix.

## Boundaries and next steps

The OnePlus 11 was used only as an artifact consumer DUT; no Android BLE
operation and no nRF52840 DK RF was used for this slice. It does not prove
Android initialization, scan, connection, GATT, notification, RSSI, recovery,
Android RN production-backend runtime, or Android RF.

Before using this backend in a production consumer, the next deterministic
work is to add Android lifecycle/scan/connection tests around handler
rejection, queued start versus dispose, passive scan failure, repeated stop,
permission/radio transitions, native callback identity, rediscovery rollback,
operation retirement, and ingress status handling. The static operation layer
must then be exercised through Android instrumentation and the production RN
host. Only after those layers pass should Android RN switch from its
deterministic backend and proceed to nRF52840 DK RF.
