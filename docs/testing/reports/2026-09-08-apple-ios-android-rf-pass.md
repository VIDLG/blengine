# iPhone Central to Android Peripheral RF pass

Date: 2026-09-08

## Result

The maintained iPhone Central to Android Peripheral production RF scenario
passed on the current unfrozen Apple candidate.

Command:

```sh
pixi run -e apple-ci just apple runtime-ios-android
```

The run used the staged production Apple package, the production
`AppleBleBackend`, a direct Swift `BleEventSink`, CoreBluetooth on a physical
iPhone 14, and the project Android Peripheral profile
`blehub-android-peripheral-v1` with service UUID
`fff00000-0000-4000-8000-000000000000`.

## Device and runtime evidence

```text
apple_ios_device_name=iPhone1
apple_ios_device_model=iPhone 14
apple_ios_device_version=26.6.1
apple_ios_developer_mode=enabled
apple_ios_signed_device_build=passed
apple_ios_device_install=passed
apple_ios_device_launch=passed
apple_ios_corebluetooth_adapter_state=poweredOn
apple_ios_corebluetooth_scan_authorization=authorized
apple_ios_corebluetooth_connection_authorization=authorized
apple_ios_corebluetooth_initialize=passed
apple_ios_corebluetooth_authorization=passed
apple_ios_corebluetooth_runtime=passed
apple_ios_corebluetooth_rf=passed
```

The application reported `didBecomeActive` and completed while in the active
application state. This is a foreground RF result, not background or restored
lifecycle evidence.

## RF and GATT evidence

```text
apple_ios_corebluetooth_scan_service_uuid=fff00000-0000-4000-8000-000000000000
apple_ios_corebluetooth_scan_filter=service
apple_ios_corebluetooth_scan_selection=service
apple_ios_corebluetooth_scan_allow_duplicates=true
apple_ios_corebluetooth_scan_peripheral_id=0
apple_ios_corebluetooth_scan_rssi=-99
apple_ios_corebluetooth_scan_connectable=true
apple_ios_corebluetooth_scan_advertised_services=fff00000-0000-4000-8000-000000000000
apple_ios_corebluetooth_scan_started=passed
apple_ios_corebluetooth_advertisement=passed
apple_ios_corebluetooth_scan_stopped=passed
apple_ios_corebluetooth_post_stop_silence=passed
apple_ios_corebluetooth_scan=passed
apple_ios_corebluetooth_connecting=passed
apple_ios_corebluetooth_connected=passed
apple_ios_corebluetooth_connection=passed
```

Aggregate GATT discovery passed with the Android fixture database shape:

```text
apple_ios_corebluetooth_gatt_service_count=11
apple_ios_corebluetooth_gatt_characteristic_count=45
apple_ios_corebluetooth_gatt_descriptor_count=30
apple_ios_corebluetooth_gatt_event_order=passed
apple_ios_corebluetooth_gatt_database=passed
apple_ios_corebluetooth_gatt=passed
```

Characteristic I/O passed:

```text
apple_ios_corebluetooth_gatt_initial_read=00
apple_ios_corebluetooth_write_with_response=passed
apple_ios_corebluetooth_write_with_response_readback=passed
apple_ios_corebluetooth_write_without_response_local_submission=passed
apple_ios_corebluetooth_write_without_response_readback=passed
apple_ios_corebluetooth_gatt_io_event_order=passed
apple_ios_corebluetooth_gatt_io=passed
```

The no-response terminal remains local CoreBluetooth submission evidence; the
reported readback is separate fixture-application evidence, consistent with
the established contract.

Connection teardown also passed:

```text
apple_ios_corebluetooth_logical_disconnect=passed
apple_ios_corebluetooth_post_disconnect_silence=passed
```

## Scope and remaining boundaries

This is the first current-candidate iPhone-to-Android Peripheral RF pass. It
closes the earlier iPhone provider-identification/connection blocker for this
managed Android profile and proves the production iOS package/backend path
through aggregate GATT and characteristic I/O.

This report does not claim:

- Notification or strict Indication RF for iPhone;
- iPhone passive-disconnect or radio-loss recovery;
- iPhone RSSI command evidence;
- standalone nRF52840 DK Peripheral interoperability;
- iPhone background/restoration behavior;
- Phase 5 ABI freeze.

The earlier blocked attempt remains historical evidence and is not rewritten;
this report records the later successful run separately.
