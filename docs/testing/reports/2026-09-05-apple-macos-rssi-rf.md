# Apple macOS RSSI RF gate

Date: 2026-09-05

## Result

The macOS production Swift/CoreBluetooth backend passed a real RSSI read over
the project-managed Android Peripheral. This gate does not claim iPhone RF,
strict Indication, passive-disconnect recovery, or ABI freeze.

Command:

```sh
pixi run -e apple-ci just apple rssi-macos-android
```

The Android fixture was OnePlus 11 (`3ac5cc09`) using profile
`blehub-android-basic-gatt-v1`; the Central was the macOS production backend
using the system-selected built-in controller.

Observed result:

```text
adapter_state=poweredOn
authorization=authorized
scan_service_uuid=fff00000-0000-4000-8000-000000000000
scan_rssi=-64
scan_connectable=true
connected=true
rssi_value=-43
logical_disconnect=passed
post_disconnect_silence=passed
```

The HIL issued `ReadRssiCommand` after the exact connected lifecycle, required a
correlated `RssiRead` before the successful command terminal, then performed
logical disconnect and checked post-disconnect connection/GATT silence.

The macOS HIL entry point is `rssi-macos-android`; it consumes the Android
profile service UUID dynamically and does not introduce a provider-specific
UUID constant.

