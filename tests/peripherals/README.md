# BleHub test peripherals

This directory contains project-managed BLE Peripheral fixtures used to validate BleHub Central backends. They are test infrastructure, not a BleHub Peripheral product API.

All providers consume the shared profile under [`../conformance/scenarios/`](../conformance/scenarios/) rather than duplicating UUIDs or characteristic properties.

- [`android/`](android/) is the Phase 2 development fixture for physical Android devices.
- [`bless/`](bless/) contains the Linux/BlueZ BLESS provider used inside the managed Peripheral VM. A USB adapter enumerated by a Windows or macOS host is not a BLESS provider until the Linux guest exclusively owns it and passes doctor/readiness.
- `nrf52840/` is reserved for an optional independently implemented hardware reference fixture after its firmware stack is reviewed.

Passing one provider does not imply another provider's stronger gate passed. In particular, the Android fixture does not replace BLESS fault injection or soak testing.
