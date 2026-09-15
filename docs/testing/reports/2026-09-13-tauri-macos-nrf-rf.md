# Tauri macOS → nRF52840 RF — 2026-09-13

## Result

The current Tauri internal low-level BleHub RF lane passed against the standalone
nRF52840 Peripheral:

```text
6 passing / 0 skipped
elapsed: approximately 1m10s
```

This is current nRF-backed evidence. It replaces neither the historical
Android Peripheral Tauri report nor the future BLeng/RN high-level lanes.

## Environment and ownership

| Field | Value |
|---|---|
| Central | macOS Tauri internal example, WebDriver Classic + embedded Rust server |
| Central backend | Production Apple/CoreBluetooth BleHub backend through the Tauri internal low-level plugin surface |
| Peripheral | Existing nRF52840 standalone Peripheral firmware over USB CDC ACM/BTP |
| Active provider | `tests/peripherals/nrf52840/` direct provider, `--coordination-mode tauri` |
| Serial ownership | One provider process; WebDriver exchanged filesystem coordination markers only |
| Profile | `blehub-nrf52840-basic-gatt-v1` |
| Service | `fd000000-0000-4000-8000-000000000000` |
| Read/write characteristic | `fff10000-0000-4000-8000-000000000000` |
| Notification characteristic | `fff20000-0000-4000-8000-000000000000` |
| Notification value | `EF` |
| With-response write | `10` |
| Without-response write | `AB` |

The run did not flash, erase, reset, reprovision, or otherwise destructively
modify the hardware. The existing firmware and resident GATT database were used
as-is.

## Commands and prerequisites

The following deterministic gates passed before the RF run:

```text
nRF provider unit tests: 32 passed
Apple fixture host tests: 4 passed
Tauri plugin Rust tests: 17 passed
Tauri internal Rust tests: 2 passed
Tauri UI component tests: 19 passed
nRF provider Ruff checks: passed
Node syntax checks: passed
```

The RF gate was run with:

```bash
pixi run just tauri tauri-native-rf
```

The recipe built the current UI and Apple production package, started the nRF
direct provider, launched the owned Tauri app instance, and stopped the provider
at teardown. The final run reported:

```text
nrf52840_fixture=ready
nrf52840_fixture=stopped
6 passing (1m10.4s)
```

## Configuration source of truth

The active fixture identity is loaded from
[`tests/conformance/scenarios/blehub-nrf-basic-v1.json`](../../../tests/conformance/scenarios/blehub-nrf-basic-v1.json):

- the Python nRF provider loads the profile directly;
- the Apple HIL receives the profile path from its recipe;
- the Tauri Node E2E derives the advertised service and role UUIDs from the same JSON;
- the Tauri UI imports the same scenario through `ui/src/fixtureProfile.js`;
- the non-RF Rust demo receives generated profile constants from the Tauri
  build script rather than defining a second fixture profile.

The CCCD UUID remains a Bluetooth SIG-defined protocol invariant in the UI
orchestration, not a provider-specific fixture identity. Android/QEMU/BLESS
profile files are not consulted by the active Tauri lane.

## Coverage

The six WebDriver cases passed:

1. Platform-appropriate visible flow: initialization, scan, connection, GATT
discovery, read/write, Notification, RSSI, disable, and logical disconnect.
2. Two repeated visible flows with isolated command and session IDs.
3. Ten repeated scan/connect/GATT/read/logical-disconnect cycles.
4. In-flight GATT discovery termination after an nRF Peripheral-initiated GAP
disconnect, including commandless disconnect delivery.
5. Active-subscription passive-disconnect recovery, stale notification silence,
new-session reconnect, rediscovery, read, and logical disconnect.
6. Process-owned Hub disposal and owned app termination.

The provider remains the sole BTP owner, consumes each coordination request once,
observes GAP connection events, reapplies the notification value only after
CCCD enablement, and resumes advertising after both passive and Central-initiated
link termination.

## Fixes required by the real run

The first post-migration attempt correctly exposed that the Tauri flow still
used archived Android multi-byte write payloads. CoreBluetooth returned
`CBATTErrorDomain` with `The value's length is invalid` for the nRF one-byte
buffer. The active Tauri flow now uses the canonical nRF `10` and `AB` values.

The next attempt completed the first flow but failed at the next scan because
resident nRF advertising did not resume after a Central logical disconnect. The
provider now treats the GAP disconnected event as a best-effort advertising
restart point. The subsequent complete run passed all six cases.

## Evidence boundary and remaining work

This report proves the macOS Tauri **internal low-level BleHub** path against the
nRF52840 provider. It does not prove:

- `crates/bleng` operation-completion, timeout, cancellation, retry, or cache
semantics;
- React Native TurboModule/Codegen or RN high-level API behavior;
- Tauri high-level BLeng surface behavior;
- iOS Tauri RF;
- Windows Tauri RF;
- the short five-minute Tauri RF regression soak;
- strict Indication. The active nRF `FFF2` characteristic has both Notify and
Indicate properties, not indication-only capability, so strict Indication remains
an explicit unsupported boundary.

The historical Android and QEMU/BLESS Tauri/peripheral reports remain archived
reference evidence and are not relabeled as nRF results.

## Follow-up stability rerun — 2026-09-13

The shortened soak path was exercised with a bounded ten-cycle mode:

```bash
pixi run just tauri tauri-native-rf-soak 0
```

This rerun did not reach the pressure loop. The newly launched Tauri bundle timed
out during the initial CoreBluetooth initialization with zero collected events;
the five later cases failed as a consequence of that shared initialization failure.
The only completed case was the final disposal check:

```text
visible flow: failed at initialize (0 events)
repeat/pressure/passive/recovery: cascading failures
hub disposal: passed
nRF fixture: ready -> stopped
```

This does not invalidate the earlier `6 passing / 0 skipped` nRF-backed Tauri RF
run. It does mean the shortened soak is not currently a green regression gate and
requires restoration of the Tauri bundle's macOS Bluetooth authorization/foreground
condition before another RF stability run. The active app bundle identifier is
`dev.blehub.tauri.blehubinternal`; no scenario UUID or provider logic was changed
for this environmental failure.

## Follow-up after macOS authorization — 2026-09-13

After authorizing the Tauri bundle's macOS Bluetooth access, the complete short
RF suite was rerun successfully:

```text
6 passing (1m 7.9s)
nrf52840_fixture=ready -> stopped
```

The five-minute soak was then rerun:

```bash
pixi run just tauri tauri-native-rf-soak 5
```

The Tauri app initialized and the other cases passed, but the continuous pressure
case still timed out:

```text
visible flow: passed
repeat flow: passed
pressure loop: failed: Timeout
in-flight passive disconnect: passed
active-subscription recovery: passed
dispose: passed
5 passing / 1 failing (5m 49.1s)
```

This confirms that the earlier initialization failure was an authorization
condition, while the remaining soak failure is an independent long-running
scan/connect/GATT/read/disconnect stability issue. The normal six-case RF gate is
green; the five-minute soak remains non-passing evidence until the pressure-loop
timeout is diagnosed and fixed.
