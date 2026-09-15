# BleHub test peripherals

This directory contains project-managed BLE Peripheral fixtures used to validate BleHub Central backends. They are test infrastructure, not a BleHub Peripheral product API.

All providers consume the shared profile under [`../conformance/scenarios/`](../conformance/scenarios/) rather than duplicating UUIDs or characteristic properties.

- [`nrf52840/`](nrf52840/) is the active standalone hardware reference provider. Its host provider, BTP/AutoPTS adapter, profile provisioning, firmware identity and recovery logic belong here. AutoPTS is nRF-provider-only and is not a public BleHub or shared Peripheral dependency.
- [`archive/android/`](archive/android/) retains the former Android development fixture for historical and optional compatibility runs; it is not part of the active conformance baseline.
- [`archive/bless/`](archive/bless/) retains the former Linux/BlueZ BLESS provider and guest server for historical and optional compatibility runs; its VM/image inputs are archived separately under `vm/archive/linux-peripheral/`.

The active baseline is nRF52840 + standalone firmware + isolated BTP/AutoPTS control + real macOS/iOS/Windows Central RF. Archived providers and their reports are not automatically evidence for the active nRF lane.
