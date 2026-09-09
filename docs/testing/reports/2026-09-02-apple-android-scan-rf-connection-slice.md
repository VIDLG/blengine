# Apple Android scan RF and connection slice

Date: 2026-09-02

## Scope

This report records the first real macOS CoreBluetooth scan and connection-lifecycle RF passes for the current unfrozen Apple production candidate. Both RF results use the project-managed Android Peripheral. No GATT, subscription, passive-disconnect, signed-iPhone runtime, or Apple release-artifact pass is inferred.

## RF preflight

The user explicitly confirmed the fixture-ready boundary immediately before the run:

- DUT: Apple Silicon Mac mini using the CoreBluetooth-selected built-in `BCM_4388C2` controller;
- provider: OnePlus 11 / `PHB110`, ADB serial `3ac5cc09`;
- fixture package: `dev.blehub.fixture.peripheral`;
- profile: `blehub-android-basic-gatt-v1`;
- advertised/root service UUID: `fff00000-0000-4000-8000-000000000000`;
- expected duration: 2–5 minutes;
- exclusive resources: the phone remained unlocked, foregrounded, Bluetooth-enabled, USB-connected, and advertising.

The installed fixture was reused rather than rebuilt or reinstalled. `android-resume` initially observed the fixture's transient `STARTING` state and then passed readiness. Fixture logs recorded service registration, `advertising started`, and `advertising readiness confirmed`.

## macOS scan RF result

The following recipe passed:

```sh
pixi run -e apple-ci just apple scan-macos-android
```

Observed terminal markers:

```text
apple_macos_corebluetooth_adapter_state=poweredOn
apple_macos_corebluetooth_scan_authorization=authorized
apple_macos_corebluetooth_connection_authorization=authorized
apple_macos_corebluetooth_initialize=passed
apple_macos_corebluetooth_scan_service_uuid=fff00000-0000-4000-8000-000000000000
apple_macos_corebluetooth_scan_peripheral_id=1
apple_macos_corebluetooth_scan_rssi=-51
apple_macos_corebluetooth_scan_connectable=true
apple_macos_corebluetooth_scan_started=passed
apple_macos_corebluetooth_advertisement=passed
apple_macos_corebluetooth_scan_stopped=passed
apple_macos_corebluetooth_post_stop_silence=passed
apple_macos_corebluetooth_scan=passed
```

This passes real CoreBluetooth initialization/authorization, provider-specific service filtering, advertisement delivery and mapping, local scan start/stop completion, and one-second post-stop silence against the Android fixture. This scan gate alone does not prove BLESS, UB500 use, iOS behavior, connection, or GATT; connection has the separate result below.

## Connection lifecycle slice

The production Swift backend now includes:

- one active lifecycle per `PeripheralId` and unique active `ConnectionId` enforcement;
- monotonically allocated backend-private connection generations;
- `Connecting` after native connect dispatch;
- `didConnect` mapping to `Connected -> CommandCompleted`;
- `didFailToConnect` mapping to a shared connection error in the state and terminal events;
- logical disconnect through `cancelPeripheralConnection` and completion on `didDisconnectPeripheral`;
- explicit pending-connect cancellation correlation;
- suppression and recancellation of a late `didConnect` while disconnect is pending;
- passive normal versus error disconnect mapping;
- route invalidation before failure/disconnect event delivery;
- route draining before native cancellation and delegate detachment during dispose;
- active-route termination if CoreBluetooth state or authorization becomes unavailable.

CoreBluetooth connect attempts have no native timeout. The backend therefore adds no timer; caller policy continues to own deadlines and any subsequent disconnect command.

## Deterministic and build validation

The following gate passed after the connection slice:

```sh
pixi run -e apple-ci just apple check
```

Observed results:

- 16 macOS tests passed, including 5 new connection-controller tests;
- connection exclusivity and connection-ID reuse rejection passed;
- generation advancement after terminal cleanup passed;
- connect-failure callback-first route removal passed;
- pending-connect disconnect, late-connect recancellation, and final disconnect cleanup passed;
- repeated/unknown logical disconnect rejection passed;
- dispose-style drain and late-callback silence passed;
- unsigned generic iOS Simulator and iOS Device builds passed.

## macOS connection RF result

After the Mac graphical session was unlocked and the Android fixture reconfirmed ready, the following recipe passed:

```sh
pixi run -e apple-ci just apple connect-macos-android
```

Observed terminal markers:

```text
apple_macos_corebluetooth_adapter_state=poweredOn
apple_macos_corebluetooth_scan_authorization=authorized
apple_macos_corebluetooth_connection_authorization=authorized
apple_macos_corebluetooth_initialize=passed
apple_macos_corebluetooth_authorized_adapter_state=poweredOn
apple_macos_corebluetooth_authorization=passed
apple_macos_corebluetooth_scan_service_uuid=fff00000-0000-4000-8000-000000000000
apple_macos_corebluetooth_scan_peripheral_id=1
apple_macos_corebluetooth_scan_rssi=-54
apple_macos_corebluetooth_scan_connectable=true
apple_macos_corebluetooth_scan_started=passed
apple_macos_corebluetooth_advertisement=passed
apple_macos_corebluetooth_scan_stopped=passed
apple_macos_corebluetooth_post_stop_silence=passed
apple_macos_corebluetooth_scan=passed
apple_macos_corebluetooth_connection_peripheral_id=1
apple_macos_corebluetooth_connecting=passed
apple_macos_corebluetooth_connected=passed
apple_macos_corebluetooth_logical_disconnect=passed
apple_macos_corebluetooth_post_disconnect_silence=passed
apple_macos_corebluetooth_connection=passed
```

This passes the production candidate's real CoreBluetooth scan-to-connect path, caller-selected connection ID correlation, `Connecting` and `Connected` delivery, successful command terminal, logical disconnect through `cancelPeripheralConnection`, requested `Disconnected` delivery, disconnect command terminal, and one-second post-disconnect connection-state silence.

An immediately preceding launch while macOS reported `IOConsoleLocked=Yes` timed out before receiving initialization events. It produced no Bluetooth TCC request and never entered scan or connection RF, so it is recorded as an invalid locked-session precondition rather than a backend or fixture failure. The passing rerun used `IOConsoleLocked=No` and a bounded `caffeinate` assertion.

## Boundaries

- The public ABI remains unfrozen; applicable Apple gates must rerun against the frozen candidate.
- macOS evidence does not prove iOS.
- The scan used the built-in Mac controller, not UB500.
- BLESS did not advertise on the macOS QEMU path, so no Apple BLESS scan result is claimed.
- Passive disconnect, GATT, read/write, RSSI, Notification, Indication, lifecycle accounting, fault recovery, and soak remain open.
