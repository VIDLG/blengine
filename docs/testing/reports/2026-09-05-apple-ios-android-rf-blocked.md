# iPhone Central to Android Peripheral RF attempt

Date: 2026-09-05

## Result

The iPhone production runtime and Android Peripheral preflight passed, but the
iPhone-to-Android RF scenario did not reach GATT. This is not recorded as an
iOS backend RF pass.

Command:

```sh
pixi run -e apple-ci just apple runtime-ios-android
```

The Android fixture built, installed, started, and reported advertising
readiness. The iPhone app signed, installed, launched, initialized
CoreBluetooth, and started a scan, but timed out waiting for the matching
`fff00000-0000-4000-8000-000000000000` advertisement.

## Isolation evidence

While the same Android fixture was advertising, the macOS CoreBluetooth probe
passed the provider-filtered scan with the expected service UUID, RSSI `-61`,
and `connectable=true`.

An iPhone diagnostic scan without a native service filter received BLE
advertisement callbacks during the fixture run, but every mapped observation had
empty `advertisedServices`, `localName`, `manufacturerData`, and `serviceData`.
The recorded diagnostic did not include `CBPeripheral.identifier` or correlate
individual callbacks with repeated Android advertising stop/start transitions,
so it does not prove that every such callback came from the Android fixture. The
current HIL helper also continued to require the expected service in the mapped
observation before selecting a candidate; it therefore did not attempt a
diagnostic connection followed by GATT service identification.

A temporary experiment added the service UUID and device name to an Android scan
response without changing the iPhone observations, then reverted that change.
The maintained fixture instead places its service UUID in the primary Android
`AdvertiseData` and calls the three-argument `startAdvertising` overload without
a scan response. No raw over-air capture was recorded for either arrangement,
so the experiment does not establish how the Android controller encoded the ADV
and SCAN_RSP packets observed by each Apple device.

The raw `advertisementData` diagnostic now records the native dictionary before
BleHub mapping. Across repeated foreground runs on iPhone 14 / iOS 26.6.1, the
dictionary contained only `kCBAdvDataIsConnectable`, PHY, timestamp, and
occasionally TxPower. `kCBAdvDataServiceUUIDs`, overflow UUIDs, local name,
manufacturer data, and service data were absent. The app reported
`UIApplicationState=active` and `didBecomeActive`, so this was not a background
scan result. Repeating the run with `allowDuplicates=false` produced the same
native fields and the same timeout; duplicate coalescing is therefore not the
observed cause.

The same raw diagnostic was added to the macOS CoreBluetooth probe and run
against the same Android fixture while it was advertising. macOS reported
`kCBAdvDataServiceUUIDs=fff00000-0000-4000-8000-000000000000`,
`kCBAdvDataIsConnectable=1`, and RSSI `-62`, and the provider-filtered scan
passed. This confirms that the Android `advertiseData` contains a service UUID
that the Apple macOS stack can parse; the iPhone result differs before BleHub
mapping.

### 2026-09-07 native-identity correlation follow-up

The host Bleak diagnostic was first corrected to pass its accepted service UUIDs
to `BleakScanner.discover(service_uuids=...)` while retaining callback-side
validation. With the Android fixture advertising, the corrected macOS scan
repeatedly observed the exact provider UUID at RSSI approximately `-51` and
`-48`. This verifies that the fixture was transmitting and visible to another
Central immediately before the corresponding iPhone runs. It is not a raw
packet capture.

The iPhone diagnostic was extended without changing the public BleHub ABI. It
now records the native `CBPeripheral.identifier`, associates that identifier
with the already allocated `PeripheralId`, and aggregates each native identifier
and advertisement-field shape by first/last observation time, callback count,
and RSSI range. RSSI-only changes no longer consume the 32-entry diagnostic
limit. An explicitly supplied native identifier can be selected during an
unfiltered diagnostic scan and passed to the existing connection/GATT scenario;
ambient devices are never selected automatically.

A fresh service-filtered iPhone 14 / iOS 26.6.1 run was active and authorized,
started scanning successfully, and produced zero `didDiscover` callbacks in 30
seconds. Unfiltered runs did receive many ambient callbacks, and iOS exposed
service UUID, local-name, manufacturer-data, and service-data fields for
unrelated nearby devices. It never exposed the Android provider UUID.

Multiple separate Android OFF/ON/OFF comparisons rejected apparent anonymous
candidates because they remained visible while the fixture was stopped or did
not repeat. Two stronger 120-second runs then changed Android advertising from
OFF to ON and back to OFF during the same foreground iPhone scan session. The
Android control channel reported successful `start`, readiness, and `stop` in
both runs. In the first run one anonymous, connectable identifier appeared only
once near the middle of the scan (`af338835-2ada-1d6c-2851-b14f7f464f5d`, RSSI
`-102`), but it did not recur in the second controlled run. No native identifier
was stably correlated with both Android advertising transitions. The diagnostic
therefore did not select or connect any candidate, and connection/GATT behavior
remains untested rather than failed.

The provider UUID is already a complete custom 128-bit value,
`fff00000-0000-4000-8000-000000000000`; matching is not performed on the `FFF0`
prefix alone. There is no evidence of a UUID collision, and changing to another
custom UUID would not address the observed absence of a correlated CoreBluetooth
callback. The prior 16-bit custom and SIG Heart Rate experiments also did not
restore the filtered iPhone result.

### 2026-09-08 xiaoze iPhone comparison

A real RF run against the same managed Android Peripheral was repeated with
`BLEHUB_APPLE_DEVICE=xiaoze`. The selected device was an iPhone 13 running iOS
26.6.1, connected by USB, paired, and in Developer Mode. The Android fixture
reported advertising readiness. The iPhone was foreground, `PoweredOn`, and
Bluetooth-authorized.

Unlike the earlier iPhone 14 runs, xiaoze's iPhone 13 successfully received the
service-filtered discovery result:

```text
service UUID: fff00000-0000-4000-8000-000000000000
RSSI: -66 dBm
connectable: true
```

The same run then passed connection and GATT validation, including 11 services,
45 characteristics, and 30 descriptors, followed by the expected initial read
`00`, write-with-response/readback, write-without-response local submission and
readback, logical disconnect, and post-disconnect silence. The terminal marker
was `apple_ios_corebluetooth_rf=passed`.

This is decisive comparison evidence against a universal iOS-versus-Android
interoperability block or an Android fixture that cannot be used by iOS. It also
shows that the maintained Apple backend and production package can complete the
full Android Peripheral path on another iPhone under the same iOS release. The
remaining iPhone 14 result is therefore device-, state-, RF-environment-, or
run-condition-specific until the two phones are tested side by side with the
same placement and controlled advertisement capture. This comparison does not
by itself identify which of those variables explains the iPhone 14 result.

A second real RF rerun was performed after the Peripheral phone was changed
back from the Honor device to the OnePlus 11. The Android selector reported ADB
serial `3ac5cc09`, and the same production iOS scenario was run again with
`BLEHUB_APPLE_DEVICE=xiaoze`. xiaoze again discovered the complete
`fff00000-0000-4000-8000-000000000000` service-filtered advertisement at RSSI
`-72 dBm`, with `connectable=true`, then passed connection, GATT discovery (4
services, 34 characteristics, 23 descriptors), initial read `00`, both write
and readback paths, logical disconnect, and post-disconnect silence. The final
`apple_ios_corebluetooth_rf=passed` marker was present.

This repeat confirms that the iPhone 13 can repeatedly use the current OnePlus
11 Android Peripheral advertisement and complete the production Apple Central
flow. It further weakens any explanation based on a universal iOS-to-Android
compatibility restriction; the unresolved discrepancy remains specific to the
previous iPhone 14 run or its test conditions.

### iPhone1 / phone1 follow-up

The iPhone was then changed to the device named `iPhone1` (the requested
`phone1` device), an iPhone 14 running iOS 26.6.1. A new real RF run kept the
OnePlus 11 Android Peripheral selected as ADB serial `3ac5cc09` and executed the
same production `scan -> connect -> GATT -> read/write -> disconnect` scenario.
The iPhone1 service-filtered scan found
`fff00000-0000-4000-8000-000000000000` with `connectable=true` at RSSI `-98
dBm`. Connection, GATT discovery (4 services, 34 characteristics, 23
descriptors), initial read `00`, both write/readback paths, logical disconnect,
and post-disconnect silence all passed, with the terminal marker
`apple_ios_corebluetooth_rf=passed`.

This confirms that changing from xiaoze's iPhone 13 to iPhone1's iPhone 14 did
not prevent the OnePlus 11 RF flow in this rerun. It also demonstrates that a
weak reported RSSI alone does not imply discovery or GATT failure.

A temporary Android profile spike also changed the advertised/root service to
the standard 16-bit `0000fff0-0000-1000-8000-00805f9b34fb` form while keeping
the same characteristics. The iPhone still received no matching advertisement;
the profile was restored to the provider-specific 128-bit UUID and profile
isolation passed again.

An additional controlled spike changed the root service and advertisement to the
SIG Heart Rate UUID `0000180d-0000-1000-8000-00805f9b34fb`. macOS again passed
the filtered scan and parsed `services=180d` at RSSI `-63`, but iPhone filtered
scan still timed out. iPhone unfiltered raw advertisements still omitted
service UUIDs and all payload fields. The Android profile was restored to
`fff00000-0000-4000-8000-000000000000` after the run.

## CoreBluetooth contract reassessment

Apple documents that `scanForPeripherals(withServices:)` returns only
peripherals CoreBluetooth classifies as advertising one of the requested
services, while `nil` returns all discovered peripherals. Its `didDiscover`
documentation describes `advertisementData` as a dictionary containing “any
advertisement data” and lists the keys through which fields may be accessed; it
does not promise a raw or complete representation of every over-air AD
structure in every callback. See the local official snapshots for
[`scanForPeripherals(withServices:options:)`](../../platform-api-reference/apple/cbcentralmanager-scanforperipherals.md)
and
[`centralManager(_:didDiscover:advertisementData:rssi:)`](../../platform-api-reference/apple/cbcentralmanagerdelegate-diddiscover.md).

Apple separately documents that an app must retain the discovered
`CBPeripheral` to perform commands, may pass that object to `connect`, and may
request selected services after connection with `discoverServices([CBUUID])`.
Neither operation is documented as requiring the service UUID to be present in
the preceding `advertisementData` dictionary. See
[`connect(_:options:)`](../../platform-api-reference/apple/cbcentralmanager-connect.md)
and
[`discoverServices(_:)`](../../platform-api-reference/apple/cbperipheral-discoverservices.md).

The failed filtered scan therefore proves only that the iPhone run did not
receive a CoreBluetooth discovery result usable as a match for the requested
service. It does not distinguish among raw over-air encoding, controller/OS scan
and response handling, CoreBluetooth service-filter behavior, or
CoreBluetooth's advertisement-data exposure. The unfiltered run did not
correlate a callback to the Android fixture and did not test whether a retained
candidate could connect and expose the expected GATT service.

## Classification

The current result is an **iPhone 14-specific provider-identification blocker
under the observed test conditions**, not a universal iOS blocker. It is not
evidence that the Android fixture failed
to start, but it is also not sufficient evidence of an Android-to-iPhone RF
interoperability failure, an unusable BLE link, or an Apple GATT implementation
failure. No iPhone connection or GATT attempt reached a positively identified
Android candidate, so no scan/connect/GATT pass is claimed.

The maintained Android fixture remains valid for macOS RF and remains the first
provider for further iPhone diagnosis. The standalone nRF52840 is a future
independent, byte-controlled reference provider and comparison point; it is not
presumed to fix this result.

The native-identifier diagnostic and repeated Android advertising correlation
runs are now complete, but they produced no stable target. Remaining evidence is:

1. capture the Android fixture's raw ADV and SCAN_RSP structures with an
   independent scanner or sniffer;
2. add a small, controlled provider marker to the Android primary advertisement
   and test whether iOS exposes it during an unfiltered scan; this requires the
   Android Kotlin source to be restored to the synchronized Mac checkout before
   rebuilding the fixture, because `tests/peripherals/android/app/src` was empty
   on this host during the follow-up and only an existing APK/build cache was
   available;
3. only after a candidate is positively correlated, retain it, connect to it,
   and call `discoverServices` for the expected service UUID;
4. classify advertisement/filter behavior separately from connection and GATT
   results.
