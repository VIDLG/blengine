# Peripheral provider ownership and layout

## Decision

BleHub owns the test Peripheral provider contract and all provider-specific test
implementation under `tests/peripherals/{android,bless,nrf52840}`. Shared profile
and scenario JSON belongs only under `tests/conformance/scenarios`. Generic
process, readiness, cleanup, locking, and reporting mechanisms belong under
`tests/support/peripheral`.

`tools/` is reserved for project-wide tools and must not be the long-term home
of Android, BLESS, or nRF provider implementations.

These fixtures are Central-test infrastructure, not a BleHub Peripheral product
API.

## Provider boundary

Each provider exposes the same operational concepts:

```text
start → ready → status → stop
```

The provider owns platform-specific behavior:

- Android owns ADB, APK installation, GATT server controls, radio controls, and
  callback ledgers.
- BLESS owns QEMU, guest SSH, BlueZ/BLESS readiness, USB attach/detach, and
  guest recovery. `vm/archive/linux-peripheral` remains the archived VM image/provisioning input.
- nRF52840 owns its host transport, BTP/GATT control, standalone firmware
  identity, and hardware recovery boundary.

The shared support layer may own mechanics, but must not encode provider policy.

## Profile boundary

Provider profiles are conformance inputs, not provider source files. They must
be consumed from `tests/conformance/scenarios` and must preserve provider
isolation through distinct profile IDs, local names, and advertised/root service
UUIDs. A profile file does not imply that a provider is available or that a
Central RF gate has passed.

## nRF and nrftest boundary

The final nRF provider must not import, invoke, or require the `nrftest`
checkout. `nrftest` remains an independently versioned golden reference for
behavior comparison, RF diagnosis, and migration evidence. It is not a BleHub
build dependency, default-test dependency, or production dependency.

The nRF migration is intentionally last because it may require a project-owned
BTP host path and a project-owned standalone firmware project. Until that
migration is complete, any bridge to `nrftest` is transitional only and must be
clearly labeled as such.

## Migration sequence

1. Android provider-specific Python/control logic lives under `tests/peripherals/android`;
   its Android project and RF behavior remain maintained because the Apple RF lane's
   `just apple rf-android` recipe drives the fixture.
2. Archive BLESS provider-specific Python/control logic under
   `tests/peripherals/archive/bless`; preserve `vm/archive/linux-peripheral` and Windows
   recovery behavior.
3. Move nRF provider logic under `tests/peripherals/nrf52840`; independently
   lock or replace its BTP/AutoPTS host path and maintain standalone firmware
   inputs.
4. Remove `tools/peripheral_fixture` once all provider imports and recipes are
   migrated.

Each step is behavior-preserving first; common abstractions are extracted only
after the provider move is validated.

## Non-goals

This decision does not move generated/build output, SDKs, external source
checkouts, Nordic tools, SEGGER tools, or QEMU guest images into Git. It also
does not make hardware RF a prerequisite for ordinary BleHub tests.
