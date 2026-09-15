# nRF52840 provider independence

## Purpose

The standalone nRF52840 Peripheral is a hardware reference provider for
BleHub Central testing. The independent `nrftest` repository is retained as a
golden behavioral reference, but the final BleHub provider must not depend on
that repository at runtime.

## Current implementation status

The first hardware-free protocol spike is now under `spikes/nrf-btp/`. It has
ten passing unit cases covering frame round trips, fragmented input, truncated
streams, interleaved events, response correlation, concurrent-request
rejection, late frames after close, timeout/EOF invalidation, and terminal
mismatched responses. The validated wire/session boundary has now been
promoted into `tests/peripherals/nrf52840/btp_protocol.py` and
`btp_session.py`; it still does not import or invoke AutoPTS or `nrftest`.
Provider-owned `btp_transport.py` adds a framed byte-stream adapter and bounded
Unix-socket transport; `serial_transport.py` adds a lazy pyserial-compatible raw
CDC transport with per-read timeouts. The provider transport tests now cover
fragmentation, queued frames, EOF, close, short writes, and serial lifecycle
with eleven host-level tests passing. These transports are not yet wired into
the provider lifecycle. A provider-owned `btp_commands.py` now adds the local
Core command subset for supported-services, supported-commands, service
registration/unregistration, and exact success-payload validation; four fake
session command tests pass. GAP/GATT command wrappers remain gated on further
local payload inspection. A provider-owned `gap_commands.py` now covers
controller-info validation, power/connectable/discoverable state changes, and
advertising AD/scan-response encoding; four fake-session GAP tests pass. The
advertising payload format and controller-info size were verified against the
pinned local AutoPTS implementation. A provider-owned `gatt_commands.py` now
covers UUID little-endian conversion, service/characteristic/descriptor
registration, value setup, server start, and UUID-to-handle lookup; five fake
session tests pass. The wrappers remain host-level only until a real session
proves the firmware response/event behavior. A provider-owned `doctor.py` now
orchestrates Core capability checks, GAP/GATT service registration, controller
information validation, fixed-profile provisioning, and role-handle mapping;
two fake-session doctor tests pass. This remains a no-hardware orchestration
check and does not claim a live firmware doctor result. A provider-owned `profile.py` now
loads the canonical nRF scenario, maps properties/permissions and CCCD needs,
and performs the fixed service/characteristic/value/descriptor/start-server/
UUID-handle provisioning sequence against a command object; two profile tests
pass. This is deterministic planning and fake provisioning only, not evidence
that a device accepted the profile.

## Current reference boundary

The current reference path is:

```text
nrftest BTP fixture → USB CDC ACM → nRF52840 standalone Peripheral firmware
```

It establishes the expected behavior for Core/GAP/GATT registration, dynamic
GATT profile mapping, advertising, connection, characteristic I/O, and
lifecycle cleanup. The reference revision and firmware identity must be
recorded in migration reports; source synchronization alone is not a test
result.

## Final ownership

BleHub will own:

- the nRF provider implementation under `tests/peripherals/nrf52840`;
- the standalone fixture firmware project and its build configuration;
- the firmware dependency manifest/lock and artifact identity;
- the host serial/BTP control path;
- dynamic GATT profile mapping and fixture lifecycle;
- readiness, cleanup, and RF reports;
- explicit build, flash, and recovery recipes.

BleHub will not vendor tool caches, Nordic-restricted binaries, SEGGER
software, or complete external Zephyr/AutoPTS source trees. External sources
and tools must be explicitly versioned/locked and obtained through supported
recipes.

## Dependency rule

The following must work without `nrftest`, nRF hardware, or nRF toolchains:

```text
blehub build
blehub lint
blehub ordinary unit/conformance tests
blehub Apple/Windows tests using another provider
```

Only an explicitly selected nRF provider or nRF RF recipe may require the nRF
fixture environment.

## Required spikes before removing the bridge

### BTP host spike

Prove that a BleHub-owned host can connect to the standalone firmware, encode
and decode the required BTP frames, correlate responses/events, and run the
Core/GAP/GATT fixture without importing `nrftest` modules.

### AutoPTS decision spike

Choose explicitly between:

1. a BleHub-owned BTP client with no AutoPTS runtime dependency; or
2. an independently locked AutoPTS integration owned by the BleHub provider.

The transitional `blehub → nrftest → AutoPTS` bridge is not a final option.

### Firmware reproducibility spike

Build the standalone firmware from BleHub-owned inputs, verify the board
identity and BTP capability mask, and compare dynamic GATT and RF behavior
against the pinned `nrftest` reference.

### License/distribution spike

Record the license and redistribution boundary for AutoPTS, Zephyr, socat,
Nordic tooling, and SEGGER tooling. The repository may contain manifests,
locks, scripts, and provenance, but must not redistribute restricted binaries.

## Migration exit criteria

The nRF provider migration is complete only when:

- no provider source imports or invokes `nrftest`;
- clean BleHub checkout checks pass without nRF configuration;
- the standalone firmware is reproducibly built or its immutable artifact is
  explicitly identified;
- BTP Core/GAP/GATT and dynamic profile tests pass;
- macOS and iPhone RF scan/connect/GATT/I/O/disconnect gates pass;
- applicable Windows RF gates pass independently;
- provider cleanup and failure recovery are reported;
- behavior is compared with the pinned `nrftest` golden reference;
- generated outputs and machine-local configuration remain untracked.

## Risks

The highest risks are hidden firmware/host version coupling, BTP response and
event ordering differences, dynamic GATT allocation/rebuild semantics,
AutoPTS teardown behavior, standalone-versus-HCI firmware confusion, USB
application-versus-bootloader identity confusion, and license boundaries.
These risks must be handled by explicit spikes and evidence, not by assuming a
previously flashed device is an immutable fixture.
