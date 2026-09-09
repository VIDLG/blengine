# Apple macOS Central to Android GATT discovery RF

Date: 2026-09-03

## Scope

This report records the first real macOS CoreBluetooth aggregate GATT-discovery RF pass for the current unfrozen Apple production candidate. The Mac mini ran `BleHubApple` as the Central and the project-managed OnePlus 11 Android fixture ran as the Peripheral. It does not prove iOS, read/write, subscription, RSSI, passive disconnect, recovery, or release packaging.

## RF preflight

The user explicitly confirmed this new RF run after the deterministic GATT slice and HIL probe build passed.

- DUT/Central: Apple Silicon Mac mini, production Swift/CoreBluetooth `BleHubApple`, system-selected built-in `BCM_4388C2`;
- provider/Peripheral: OnePlus 11 / `PHB110`, ADB serial `3ac5cc09`;
- fixture package: `dev.blehub.fixture.peripheral`;
- profile: `blehub-android-basic-gatt-v1`;
- advertised/root service UUID: `fff00000-0000-4000-8000-000000000000`;
- expected duration: 2–5 minutes;
- exclusive resources: the phone remained unlocked, foregrounded, Bluetooth-enabled, USB-connected, and advertising; the Mac graphical session remained unlocked.

Immediately before confirmation:

- `IOConsoleLocked=No`;
- the shared Android SDK doctor passed from `/Users/vision/Library/Android/sdk`;
- ADB reported device state `device`;
- `android-resume` reached `result=ok` after one transient `STARTING` response.

## Command

```sh
pixi run -e apple-ci just apple gatt-macos-android
```

The recipe regenerated the current Stable UniFFI Apple package, rebuilt and ad-hoc signed the bundled macOS probe, launched it through LaunchServices, and ran:

```text
initialize
→ provider-filtered scan
→ stop scan and observe post-stop silence
→ connect
→ aggregate service/characteristic/descriptor discovery
→ validate the Android fixture database
→ logical disconnect
→ observe post-disconnect connection/GATT silence
```

## Result

Observed pass markers:

```text
apple_macos_corebluetooth_adapter_state=poweredOn
apple_macos_corebluetooth_scan_authorization=authorized
apple_macos_corebluetooth_connection_authorization=authorized
apple_macos_corebluetooth_initialize=passed
apple_macos_corebluetooth_authorized_adapter_state=poweredOn
apple_macos_corebluetooth_authorization=passed
apple_macos_corebluetooth_scan_service_uuid=fff00000-0000-4000-8000-000000000000
apple_macos_corebluetooth_scan_peripheral_id=1
apple_macos_corebluetooth_scan_rssi=-56
apple_macos_corebluetooth_scan_connectable=true
apple_macos_corebluetooth_scan_started=passed
apple_macos_corebluetooth_advertisement=passed
apple_macos_corebluetooth_scan_stopped=passed
apple_macos_corebluetooth_post_stop_silence=passed
apple_macos_corebluetooth_scan=passed
apple_macos_corebluetooth_connection_peripheral_id=1
apple_macos_corebluetooth_connecting=passed
apple_macos_corebluetooth_connected=passed
apple_macos_corebluetooth_gatt_service_count=5
apple_macos_corebluetooth_gatt_characteristic_count=35
apple_macos_corebluetooth_gatt_descriptor_count=24
apple_macos_corebluetooth_gatt_event_order=passed
apple_macos_corebluetooth_gatt_database=passed
apple_macos_corebluetooth_gatt=passed
apple_macos_corebluetooth_logical_disconnect=passed
apple_macos_corebluetooth_post_disconnect_silence=passed
apple_macos_corebluetooth_connection=passed
```

The complete CoreBluetooth database contained platform-visible services in addition to the fixture service. The validator therefore selected the provider-specific root service rather than assuming that the Peripheral exposes only one service. It required exactly one primary `FFF0...` fixture service and verified all four fixture characteristics:

| Characteristic | Required mapped properties |
|---|---|
| `FFF1...` | read, write with response, write without response |
| `FFF2...` | read, notify, indicate |
| `FFF3...` | read |
| `FFF4...` | write with response, write without response |

It also required the `FFF2...` updates characteristic to expose the standard `00002902-0000-1000-8000-00805f9b34fb` CCCD, verified unique service/characteristic/descriptor opaque IDs, exact connection and GATT session correlation, and `GattDiscovered` before successful `CommandCompleted`.

After successful discovery, logical disconnect completed and the one-second observation window contained no connection-state or GATT-database event for the retired connection.

## Boundaries

- The public ABI remains unfrozen; affected Apple gates must rerun against the Phase 5 freeze candidate.
- This macOS result does not prove iOS behavior.
- The Central used the built-in Mac controller, not UB500.
- The Peripheral was Android, not native macOS BLESS, Linux/BlueZ/BLESS, or nRF52840.
- The result validates aggregate discovery and mapping but does not claim CoreBluetooth callback exactly-once behavior under every fault.
- Characteristic read/write, Notification, Indication, RSSI, passive disconnect, recovery, lifecycle accounting, and soak remain open for the Apple backend.
