# BLESS managed peripheral fixture

This fixture implements the provider-neutral conformance scenarios with BLESS.
The project-managed Debian Linux Peripheral VM loads
`qemu-basic-gatt-peripheral.json` through BlueZ. It is test infrastructure, not
a BleHub product backend.

The fixture loads the selected provider JSON at runtime. Roles, properties,
characteristic UUIDs, and initial values stay aligned across providers, while
each provider uses a distinct profile ID, local name, and advertised/root
service UUID. UUIDs and profile data must not be copied into the fixture
implementation.

The Linux guest keeps its pinned `pixi.lock`. The fixture environment, source,
and scenario are deployed independently after the guest image boots.

```sh
pixi run just peripheral vm-fixture-install
pixi run just peripheral vm-fixture-sync
pixi run just peripheral vm-fixture-start
pixi run just peripheral vm-fixture-status
pixi run just peripheral vm-fixture-stop
```

BLESS `0.3.0` does not expose reliable per-update
Notification-versus-Indication selection, delivery completion, or
manufacturer-data configuration on Linux. The Central selects Notification or
Indication through CCCD. The fixture accepts run-correlated out-of-band
`vm-fixture-update` and `vm-fixture-burst` commands, updates the scenario's
`updates` characteristic, and calls `BlessServer.update_value`; those responses
prove local update acceptance only. Exact delivery and actual mode must be
proven by the Central's `NotificationReceived` and enabled-state events.

```sh
pixi run just peripheral vm-fixture-update deadbeef
pixi run just peripheral vm-fixture-burst 0123456789abcdef 1000
pixi run just peripheral vm-fixture-burst 0123456789abcdef 1000 500
pixi run just peripheral vm-fixture-write-batch-start 0123456789abcdef 200
pixi run just peripheral vm-fixture-write-batch-status 0123456789abcdef
pixi run just peripheral vm-fixture-request-throughput-phy
```

A burst performs one guest-local `update_value` loop for sequences `0..count-1`.
After each successful update except the last, it awaits the requested
`interval_us` (`0..1_000_000`); the host and Just commands default it to zero.
Each 20–512-byte value is ASCII `BHN1`, the raw 8-byte burst ID, the sequence as
little-endian `u64`, and deterministic filler. The single final response reports
the interval, accepted count, payload size, and guest-local elapsed time; no
per-notification control event is emitted.

The write ledger recognizes 20–512-byte Central writes containing ASCII `BHW1`,
the raw 8-byte batch ID, a zero-based little-endian `u64` sequence, and
deterministic filler. Its final status reports received/unique/missing/duplicate/
order/range/foreign/malformed counters and the guest-local first-to-last
callback span. Matching benchmark writes update only the in-memory ledger and
do not emit one journald record per payload.

The control path uses request/response JSON under the systemd runtime directory
and `SIGUSR1`, with a unique control ID and active fixture run ID. It does not
call BleHub ingress or bypass the BLE data path, and a burst does not make one
SSH call per notification.
