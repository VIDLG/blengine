# macOS CoreBluetooth to Android Peripheral RF completion

Date: 2026-09-08

## Result

The remaining maintained macOS-to-Android Peripheral RF scenarios were run on
the current unfrozen Apple candidate. nRF52840 scenarios were intentionally
not included.

The provider was the project Android Peripheral profile
`blehub-android-peripheral-v1` with service UUID
`fff00000-0000-4000-8000-000000000000`. The selected Android device in the
individual runs was `3ac5cc09`.

## Passed scenarios

The existing macOS RF runner passed the following scenarios:

- provider-filtered scan, advertisement, scan stop, and post-stop silence;
- connection and logical disconnect;
- aggregate GATT discovery;
- characteristic read/write I/O;
- Notification;
- strict Indication;
- connected RSSI read;
- Android Bluetooth-radio passive disconnect and recovery.

Commands used for the remaining individual scenarios:

```sh
pixi run -e apple-ci just apple rf-macos-android notification
pixi run -e apple-ci just apple rf-macos-android indication
pixi run -e apple-ci just apple rf-macos-android rssi
pixi run -e apple-ci just apple rf-macos-android passive-disconnect
```

Representative passed markers:

```text
apple_macos_corebluetooth_notification_actual_mode=notification
apple_macos_corebluetooth_notification_value=deadbeef
apple_macos_corebluetooth_notification_enable_disable_order=passed
apple_macos_corebluetooth_notification_post_disable_silence=passed
apple_macos_corebluetooth_notification=passed

apple_macos_corebluetooth_notification_actual_mode=indication
apple_macos_corebluetooth_notification_value=deadbeef
apple_macos_corebluetooth_notification_enable_disable_order=passed
apple_macos_corebluetooth_notification_post_disable_silence=passed
apple_macos_corebluetooth_notification=passed

apple_macos_corebluetooth_rssi_value=-34
apple_macos_corebluetooth_rssi=passed

apple_macos_corebluetooth_passive_disconnect=passed
apple_macos_corebluetooth_passive_disconnect_silence=passed
```

Each subscription scenario also passed connect, GATT discovery, logical
 disconnect, and post-disconnect silence. Passive disconnect disabled the
Android radio, observed `STATE_OFF`, re-enabled it, resumed the fixture, and
observed the expected host-side passive disconnect.

## Expected negative control

After notification/indication disable, the runner intentionally sends a second
fixture update. Android returns the expected non-zero control result:

```text
no connected device subscribed for NOTIFICATION
no connected device subscribed for INDICATION
```

This is the required stale-route rejection, not an RF failure. The individual
notification and indication recipes exited successfully after validating that
negative control. The aggregate `rf-macos-android all` invocation also emitted
the expected Just error text from those negative Android control commands; the
individual scenarios were therefore rerun and are the authoritative command
exit evidence for this report.

## Scope

This closes the currently implemented macOS-to-Android RF matrix without nRF
hardware. It does not claim iPhone Notification/Indication/RSSI/passive-radio
recovery, standalone nRF52840 interoperability, background lifecycle, or the
Phase 5 ABI freeze rerun.
