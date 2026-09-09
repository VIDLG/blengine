# Apple characteristic read/write deterministic and macOS-to-Android RF slice

Date: 2026-09-03

## Scope

This report records deterministic implementation, review, build validation, and one real macOS-to-Android RF pass for CoreBluetooth characteristic read, write with response, and write without response in the current unfrozen Apple production candidate. It does not record an iPhone runtime pass, a delegate-delivery reentrancy pass, or a release-artifact pass.

## Implementation

The production Swift backend now resolves a public `GattCharacteristicId` through the committed database and backend-private native map only when all of these still match:

- the connected `AppleConnectionCorrelation` and its generation;
- the active `GattSessionId`;
- the public characteristic ID in the committed `GattDatabase`;
- the exact retained native `CBCharacteristic` instance.

A pending discovery blocks read/write, and pending read/write blocks replacement discovery. A stale or absent session is rejected as `InvalidState`; an unknown characteristic in a valid session is rejected as `InvalidRequest`; disagreement between the public database and native routing map is `BackendUnavailable`.

`AppleGattIoController` owns one FIFO per connection. Different connections may have independent outstanding native operations, while each connection has at most one callback-bearing operation in flight. The queue stores the complete caller command, connection generation, exact peripheral identity, GATT session, characteristic ID, and exact characteristic object.

### Read

A read starts with `CBPeripheral.readValue(for:)` and completes only from an exact `didUpdateValueFor` match. Success publishes, in order:

```text
CharacteristicRead
CommandCompleted(error: nil)
```

A callback error or a callback without a value publishes only failed `CommandCompleted`.

### Write with response

A write with response starts with `writeValue(_:for:type: .withResponse)` and completes only from the matching `didWriteValueFor` callback. Success publishes, in order:

```text
CharacteristicWritten
CommandCompleted(error: nil)
```

A callback error publishes only failed `CommandCompleted`.

### Write without response

A write without response does not have a per-write CoreBluetooth completion callback. The backend:

1. waits while `canSendWriteWithoutResponse` is false;
2. uses `peripheralIsReady(toSendWriteWithoutResponse:)` only to resume the FIFO driver;
3. calls `writeValue(_:for:type: .withoutResponse)` when capacity is available;
4. publishes `CharacteristicWritten` and successful `CommandCompleted` only after that local call returns.

This terminal means local CoreBluetooth submission only. It is not remote acknowledgement. A later deterministic readback may independently prove that a particular Peripheral applied a value, but it does not strengthen the earlier write terminal.

## Validation and cleanup boundaries

The public 512-byte ATT value guard is enforced synchronously before acceptance. At the FIFO front, the backend separately queries:

```text
maximumWriteValueLength(for: .withResponse)
maximumWriteValueLength(for: .withoutResponse)
```

A wire-valid value above the current platform maximum receives one asynchronous `Gatt` terminal and no native write call. This is distinct from a capability-level `NotSupported` result and from the shared 512-byte wire guard, which rejects before acceptance.

Active disconnect, passive disconnect, adapter unavailability, and service invalidation retire I/O before removing the GATT/native routes. Every accepted queued command receives one failure terminal. `dispose` silently drains I/O, GATT, and connection ownership after event ingress is removed, consistent with the contract's disposal boundary.

Read and write-with-response callbacks do not carry caller IDs or generations. When a started callback-bearing operation is retired, the backend records an exact `(peripheral identity, characteristic identity, callback kind)` barrier. The barrier strongly retains the native route so object-address reuse cannot alias it. An observed exact retired callback is consumed silently before active-connection lookup. Reuse of the same unresolved route fails closed.

The barrier set is bounded at 128 exact routes. It never evicts unresolved barriers: capacity exhaustion fails closed for future callback-bearing operations until full `drain`/`dispose`. If a retired callback never arrives and CoreBluetooth later reuses the exact same native route, that route also remains fail closed until the callback or full drain. This is an explicit safety degradation rather than a claim that an uncorrelatable callback can be reused safely.

## Deterministic tests

The focused package command passed:

```sh
pixi run -e apple-ci swift test --package-path platforms/apple
```

Observed result:

```text
Executed 37 tests, with 0 failures
```

The 12 GATT I/O controller tests cover:

- command/connection/session/characteristic target mismatch rejection;
- read and both write-property boundaries;
- 512-byte acceptance and 513-byte rejection;
- duplicate-UUID exact native-instance routing;
- per-connection FIFO and independent connection progress;
- exact generation, peripheral, characteristic, and callback-kind matching;
- read success, callback error, and missing-value failure;
- write-with-response success and failure;
- write-without-response backpressure, readiness, local submission, and local terminal semantics;
- separate platform maximums for both write types;
- disconnect/invalidation retirement of every accepted command;
- silent late-callback consumption and same-route fail-closed behavior;
- bounded barrier capacity without unsafe eviction;
- silent full drain and recovery after drain.

The eight aggregate GATT tests include the active-session characteristic resolver and exact native-instance result.

The complete Apple gate also passed:

```sh
pixi run -e apple-ci just apple check
```

Observed results:

- the Stable UniFFI production-candidate Apple package was regenerated;
- arm64 iOS device, arm64+x86_64 iOS simulator, and arm64+x86_64 macOS XCFramework slices passed;
- all 37 macOS package tests passed;
- unsigned generic iOS Simulator and iOS Device builds passed.

Editor diagnostics reported no errors or warnings in the changed Swift production and controller-test files.

## macOS-to-Android RF validation

The first attempted run rebuilt and re-ad-hoc-signed the source-tree app, then timed out before the first CoreBluetooth initialization callback while macOS TCC awaited a new Bluetooth decision for the changed code identity. It never scanned or entered RF and is not classified as a read/write backend failure. The visible authorization probe was relaunched, the user selected **Allow**, and the same candidate then passed a separate `PoweredOn + Authorized` setup gate.

After a fresh RF preflight, the user explicitly confirmed the run with:

- DUT/Central: Apple Silicon Mac mini, production Swift/CoreBluetooth `BleHubApple`, system-selected built-in `BCM_4388C2`;
- provider/Peripheral: project-managed OnePlus 11 Android fixture, ADB serial `3ac5cc09`;
- profile: `blehub-android-basic-gatt-v1` with advertised/root service `fff00000-0000-4000-8000-000000000000`;
- exclusive resources: the unlocked Mac graphical session, built-in controller, Android Bluetooth radio, USB/ADB connection, and fixture app;
- excluded providers: UB500, QEMU/BLESS, native macOS BLESS, and iPhone.

The setup checks reported `IOConsoleLocked=No`, Android fixture `result=ok`, and a complete shared Android SDK at `/Users/vision/Library/Android/sdk`. The confirmed command passed:

```sh
pixi run -e apple-ci just apple io-macos-android
```

The probe executed:

```text
initialize
→ provider-filtered scan and stop
→ connect
→ aggregate GATT discovery and Android profile validation
→ initial FFF1 read
→ write with response 10 20 30
→ exact readback
→ write without response AB CD
→ caller-side 500 ms observation delay
→ exact readback
→ logical disconnect
→ one-second post-disconnect I/O/connection silence
```

Observed evidence included:

```text
apple_macos_corebluetooth_adapter_state=poweredOn
apple_macos_corebluetooth_scan_authorization=authorized
apple_macos_corebluetooth_scan_rssi=-50
apple_macos_corebluetooth_scan_connectable=true
apple_macos_corebluetooth_gatt_service_count=5
apple_macos_corebluetooth_gatt_characteristic_count=35
apple_macos_corebluetooth_gatt_descriptor_count=24
apple_macos_corebluetooth_gatt_initial_read=00
apple_macos_corebluetooth_write_with_response=passed
apple_macos_corebluetooth_write_with_response_readback=passed
apple_macos_corebluetooth_write_without_response_local_submission=passed
apple_macos_corebluetooth_write_without_response_readback=passed
apple_macos_corebluetooth_gatt_io_event_order=passed
apple_macos_corebluetooth_gatt_io=passed
apple_macos_corebluetooth_logical_disconnect=passed
apple_macos_corebluetooth_post_disconnect_silence=passed
```

After the silence window, commands 6 through 10 each had exactly one correctly correlated domain event and one successful terminal, with domain-before-terminal ordering and no late duplicate. The exact `AB CD` readback after the caller-side delay independently proves that this fixture applied that no-response write; it does not change the earlier terminal from local CoreBluetooth submission into a remote acknowledgement.

## Independent review

A focused read-only review found no High issues and initially found two Medium correctness risks in this slice:

1. unresolved late-callback barriers were stored only as object identifiers and could be unsafely evicted after 128 entries;
2. the first HIL draft could report event ordering without detecting duplicate domain or terminal events.

Both were corrected as described above. Follow-up review found no remaining High/Medium implementation issue in the reviewed read/write paths.

The review also identified a still-open verification gate: no runtime test yet drives a legal next backend operation from the delegate/event-delivery path to prove queue reentrancy and downstream delivery failure behavior. Source ordering and controller tests do not substitute for that runtime evidence.

## Boundaries

- One user-confirmed macOS-to-Android read/write RF run passed; it does not establish fault, repetition, or soak behavior.
- The macOS result does not prove iOS behavior; signed installation, launch, authorization, and RF must be validated independently on the connected iPhone.
- Delegate/event-delivery reentrancy runtime evidence remains open.
- Notification/Indication will share `didUpdateValueFor`; its controller must explicitly arbitrate read versus subscription ownership rather than treating every value callback as a read.
- RSSI, passive disconnect/recovery, lifecycle accounting, and soak remain open for Apple.
- The public ABI remains unfrozen; applicable gates must rerun against the Phase 5 freeze candidate.
