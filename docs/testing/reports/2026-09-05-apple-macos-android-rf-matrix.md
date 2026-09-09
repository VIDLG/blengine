# Apple macOS to Android Peripheral RF matrix

This report records the cleaned test-architecture candidate's complete production
RF matrix. The maintained entry point was:

```text
pixi run -e apple-ci just apple rf-macos-android
```

The recipe built and staged the production Apple package, started the project
Android Peripheral on OnePlus 11 (`3ac5cc09`), launched the bundled macOS HIL,
and exercised the exported production `BleHub` with `AppleBleBackend`, a direct
Swift `BleEventSink`, CoreBluetooth, and the real Android GATT server.

## Environment

| Item | Value |
|---|---|
| Central | macOS production Apple backend through `BleHub` |
| Peripheral | OnePlus 11, ADB serial `3ac5cc09` |
| Android profile | `blehub-android-peripheral-v1` |
| Advertised service | `FFF00000-0000-4000-8000-000000000000` |
| macOS controller | system-selected built-in CoreBluetooth controller |
| RF recipe | `rf-macos-android all` |

## Matrix

| Case | Result | Evidence |
|---|---|---|
| Scan | PASS | provider-filtered advertisement, connectable result, stop and post-stop silence |
| Connection | PASS | `Connecting`, `Connected`, logical disconnect, post-disconnect silence |
| GATT | PASS | aggregate discovery, 4 services, 34 characteristics, 23 descriptors, event order and profile validation |
| Characteristic I/O | PASS | initial read `00`, write with response/readback, write without response/readback, ordering and silence |
| PlatformDefault Notification | PASS | actual mode `Notification`, `DE AD BE EF`, disable ordering and stale-route silence |
| Strict Indication | PASS | `FFF50000...` `read + indicate` characteristic, actual mode `Indication`, `DE AD BE EF`, disable ordering and stale-route silence |
| RSSI | PASS | exact connected-peripheral RSSI event before terminal; observed `-44` in the final matrix run |
| Passive disconnect/recovery | PASS | Android radio `STATE_OFF`, passive disconnect, post-disconnect silence, `STATE_ON`, fixture resume/readiness |

The Android fixture's single `android-indicate` command routes to the
characteristic whose Indication CCCD is active. The strict case therefore uses
the indication-only `FFF50000...` characteristic without a second fixture or
compatibility command.

## Fixes exercised

- Android deployment now skips uninstall when the package is absent, avoiding
  ROM-specific `DELETE_FAILED_INTERNAL_ERROR` for an already-clean device.
- Android fixture deployment rejects a locked device before installation with a
  clear preflight error.
- Strict Indication's final ledger validation now carries the expected mode
  through the disconnect-side validation; it no longer defaults to Notification.

The matrix finished with the Just cleanup trap, which stopped the Android
Peripheral after the run. This report proves macOS RF only; iPhone RF and the
future Android Central backend remain separate gates.
