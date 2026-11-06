# Apple HIL test support

This directory contains test-only Swift support shared by the maintained Apple UI/HIL targets.
It is not part of the production App, Stable UniFFI image, or BLE runtime.

## Bluetooth Settings control

`BluetoothSettingsControl.swift` is the single Settings UI control implementation used by:

- `BlengAppleIOSProbeUITests` for native BLeng system-loss HIL;
- `BleHubNitroHostUITests` for RN/Nitro radio-control and restoration checks.

It owns only accessibility navigation, the stable `BLUETOOTH` identity, the nested actionable
control, bounded readback, and actionable UI errors. Host authorization markers, `devicectl`
container relay, phone leases, provider state, and RF verdicts remain outside this support code.

## App boundaries

The two existing test Apps are intentionally not merged:

- `BlengAppleIOSProbe` proves the production Apple backend and BLeng HIL owner/runtime;
- `BleHubNitroHost` proves the RN New Architecture/Nitro integration and its runtime boundary.

A Settings helper must be shared, but an RN runtime test must not silently become native BLeng
coverage or vice versa. No additional App should be introduced for Settings control.
