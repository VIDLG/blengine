# iPhone → nRF52840 RF — 2026-09-13 blocked at connect

## Result

The iPhone nRF RF preparation and two IO attempts reached the real signed iOS
probe and the active nRF profile-filtered scan, but neither run reached a BLE
connection. This is **not** an iOS RF pass.

```text
signing: passed
installation: passed
launch: passed
profile-filtered scan: passed
connect: blocked: Connecting observed, Connected not observed
GATT/I/O: not entered
```

## Environment

| Field | Value |
|---|---|
| Central | iPhone 14, iOS 26.6.1 |
| Device prerequisites | Developer Mode enabled; device doctor passed |
| Peripheral | Existing nRF52840 standalone firmware over USB CDC ACM/BTP |
| Profile | `blehub-nrf52840-basic-gatt-v1` |
| Service | `fd000000-0000-4000-8000-000000000000` |
| Write vectors | response `10`, no-response `AB` |
| Attempts | run IDs `2bd9f15eac7846ba9736ea41a9319413` and `f369bbb90174470fbcbe75ecef732d59` |

No flash, reset, erase, or firmware change was performed.

## Observed evidence

Both attempts emitted:

```text
AdapterSnapshotChanged: PoweredOn + Authorized
ScanStarted
AdvertisementObserved:
  service = fd000000-0000-4000-8000-000000000000
  connectable = true
  RSSI = -83 / -82 dBm
ScanStopped
ConnectionStateChanged: Connecting
```

Neither emitted `Connected`, `didFailToConnect`, GATT discovery, or I/O events
before the bounded connection wait expired. The result recorded
a `UIApplication` state of `inactive` (`rawValue=1`) at the end of both runs.

The nRF direct provider was ready and was stopped by the recipe cleanup trap after
each attempt. macOS-to-nRF RF remains independently passing, so this result does
not identify a macOS or nRF profile failure.

## Current boundary and next prerequisite

This is an iPhone/device RF condition requiring a clean foreground/unlocked iOS
run and a stronger/stable RF position before changing backend code. The scan
identity and source-of-truth profile are already correct. Do not extend this
failure into GATT/Notification evidence or start iOS Tauri RF from it.

After iPhone native IO reaches `Connected` and completes GATT/read/write, extend
the iOS external provider flow to Notification/passive-disconnect coordination,
then run the signed Tauri iOS UI test/RF lane. Unsigned Tauri iOS builds remain a
separate packaging check and do not establish iPhone RF evidence.

## Follow-up attempt — 2026-09-13

A third bounded IO attempt was run after both device and nRF doctor checks passed:

```text
nRF doctor: passed
 iOS device doctor: passed
authenticated build/install/launch: passed
profile-filtered scan: passed
connect: blocked: Connecting observed, Connected not observed
GATT/I/O: not entered
run id: f6c2280fb954429ca44d403251dcdb5a
RSSI: -84 dBm
UIApplication state at result: inactive (rawValue=1)
```

The provider was ready and was stopped by the cleanup trap. The result remains a
bounded device/RF foreground condition, not an implementation pass or a firmware
failure. No iOS Notification, passive-disconnect, or iOS Tauri RF gate should be
started until native iOS IO reaches `Connected` and completes GATT/read/write.

## Follow-up after macOS/Tauri authorization — 2026-09-13

A fourth bounded IO attempt was run after the macOS Tauri authorization work.
The iOS probe again passed signing, installation, launch, initialization,
authorization, and profile-filtered scan, but remained blocked at connection:

```text
run id: 570861d5622e475fad348a9be9d122a8
RSSI: -86 dBm
ConnectionState: Connecting
Connected: not observed
GATT/I/O: not entered
UIApplication state: inactive (rawValue=1)
```

This confirms the macOS Tauri authorization change does not affect the iPhone
RF blocker. The signal was weaker than the prior attempts, so this remains a
physical foreground/proximity/RF condition rather than evidence of a UUID,
scenario, signing, or nRF firmware defect.
