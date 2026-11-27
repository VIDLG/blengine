# BleHub test peripherals

This directory contains project-managed BLE Peripheral fixtures used to validate BleHub Central backends. They are test infrastructure, not a BleHub Peripheral product API.

The active nRF provider consumes the caller-selected declarations, profiles and expected contracts under [`nrf52840/firmware/`](nrf52840/firmware/), followed by live installed-image proof. Shared conformance profiles and archived provider inputs retain their own evidence scope; they are not interchangeable live defaults.

- [`nrf52840/`](nrf52840/) is the active standalone hardware reference provider. Its host provider, BTP/AutoPTS adapter, profile provisioning, firmware identity and recovery logic belong here. AutoPTS is nRF-provider-only and is not a public BleHub or shared Peripheral dependency.
- [`archive/android/`](archive/android/) retains the former Android development fixture for historical and optional compatibility runs; it is not part of the active conformance baseline.
- [`archive/bless/`](archive/bless/) retains the former Linux/BlueZ BLESS provider and guest server for historical and optional compatibility runs; its VM/image inputs are archived separately under `vm/archive/linux-peripheral/`.

The active RF priority is standalone nRF52840 Peripheral + isolated provider-owned BTP control, with real Android/iPhone RN Centrals controlled by the Mac. Native macOS CoreBluetooth is an independent backend/HIL lane. Windows and the other archived lanes are not active gates; their reports do not automatically establish evidence for the current nRF candidate.

Within `nrf52840/`, ordinary modules are live fixture infrastructure, `test_*.py` files are hardware-free regressions, and `support/` contains only synthetic emulators and samples. `DirectFixture` owns the shared connection/resource lifecycle; the existing `full_suite` module owns continuous10 order and handoffs. Keeping these responsibilities distinct is more important than reducing file or test counts.
