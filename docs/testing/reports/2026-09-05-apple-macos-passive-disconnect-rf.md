# Apple macOS passive disconnect and recovery RF gate

Date: 2026-09-05

## Result

The macOS production Swift/CoreBluetooth backend passed a destructive passive
disconnect and Android fixture recovery cycle. This gate does not claim strict
Indication, iPhone RF, or ABI freeze.

Command:

```sh
pixi run -e apple-ci just apple passive-disconnect-macos-android
```

The Mac mini connected to the OnePlus 11 Android Peripheral using the production
Apple backend. The host then disabled the Android Bluetooth radio and required
the passive connection event before restoring the radio and fixture.

Observed behavior:

```text
android_bluetooth_radio=STATE_OFF
connection_state_changed(command_id=None, category=connection)
apple_macos_corebluetooth_passive_disconnect=passed
apple_macos_corebluetooth_passive_disconnect_silence=passed
android_bluetooth_radio=STATE_ON
android_fixture_readiness=passed
```

The passive event is represented as a connection-category error state with no
command ID, which is the expected non-normal CoreBluetooth disconnect boundary.
After that event, the HIL observed no later connection-state, GATT, or
characteristic event for the retired connection. The host then restored the
Android radio and re-established fixture advertising readiness.

