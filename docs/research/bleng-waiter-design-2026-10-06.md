# BLeng waiter design and operation completion

## Status and scope

This document describes the current internal waiter implementation in
[`crates/bleng/src/waiter.rs`](../../crates/bleng/src/waiter.rs) and its runtime
integration in [`crates/bleng/src/runtime/owner.rs`](../../crates/bleng/src/runtime/owner.rs).
It is an implementation/design record, not a separate public BLE protocol and
not a native-operation cancellation guarantee.

BLeng uses an actor-owned reservation to correlate one accepted command with
the facts required to produce its typed asynchronous result. A waiter is only
the caller-facing delivery entry: it is not the BLE state owner, not the GATT
operation queue, not the event broadcast mechanism, and not a promise that the
underlying native operation can be interrupted.

## End-to-end flow

```text
Public BLeng API
      |
      v
RuntimeOwner::begin_operation
      |
      +-- check owner lifecycle
      +-- acquire the dispatchable primary Hub
      +-- ask the BLeng actor to begin the operation
      |
      v
Actor registers WaiterRegistry entry
      |
      +-- validate monotonic CommandId
      +-- record WaiterExpectation
      +-- create oneshot result channel
      +-- dispatch the backend command
      |
      +-----------------------------+
      |                             |
      v                             v
dispatch result              backend events
accepted/rejected             domain + terminal events
      |                             |
      +-------------+---------------+
                    v
             Reservation facts
                    |
                    v
             reservation waiter_result()
                    |
                    v
             WaiterRegistry::finish exactly once
                    |
                    v
             oneshot receiver
                    |
                    v
             WaiterTicket::wait
                    |
                    v
             typed operation result
```

The dispatch result, domain event, and terminal event are separate facts. Their
order is not assumed. For example, a GATT operation may observe its database
fact before the terminal event, or receive the terminal event before all facts
needed by the public result have been recorded.

## Responsibilities of the main structures

### `WaiterExpectation`

`WaiterExpectation` states the operation-specific evidence required for
success: initialization, scan start/stop, connection/disconnection, RSSI,
GATT discovery, characteristic I/O, or notification transitions.

It prevents an event from being accepted merely because its command ID looks
valid. Connection, GATT session, characteristic, subscription, and generation
identity must also match the expectation.

### Reservation evidence

Each actor-owned reservation is the authoritative evidence record for its
operation. The common dispatch evidence records the command ID, native
acceptance, and callbacks that escaped before dispatch acknowledgement. Each
operation-specific reservation records its own domain payload and terminal
result, including the native error rather than only a success boolean.

The result decision now lives beside the state it interprets:

- `InitReservation::waiter_result()` requires a snapshot and successful
  terminal result;
- `ScanReservation` and `StopReservation` preserve start/stop ordering and
  scan-stop errors;
- `ConnectionReservation` handles connection failure and passive loss;
- GATT, RSSI, read, write, and notification reservations build their typed
  `WaiterSuccess` values from their own evidence.

Every result method preserves the same precedence:

1. unresolved dispatch cannot complete the waiter;
2. operation-specific failure takes precedence over success;
3. the required domain evidence must exist;
4. only then can the operation succeed.

This is intentionally not one generic `Operation<T>` type. BLE operations
have different native retirement and late-callback rules, so small methods on
concrete reservations are clearer than a type-level state-machine abstraction.

### `WaiterEntry`

`WaiterEntry` is deliberately small. It stores only the caller expectation and
its oneshot sender. It does not copy dispatch state, GATT databases,
characteristic values, terminal errors, or notification state. A caller timeout
or cancellation removes this entry while the reservation remains alive until
native state is safe to retire.

### `WaiterRegistry`

`WaiterRegistry` is the actor-confined command ledger:

```text
WaiterRegistry
├── CommandId -> WaiterEntry
├── last accepted CommandId
└── closed/faulted reason
```

It owns the linearization of waiter registration and completion. It:

- rejects closed registries;
- rejects reused or non-monotonic command IDs;
- bounds the number of caller waiters;
- delivers actor-computed results exactly once;
- retires waiters on cancellation and timeout;
- fails accepted connection-bound waiters after actor-confirmed passive loss;
- fails all remaining waiters when the owner closes; and
- removes an entry before sending its result through the oneshot.

It does not parse `BleEvent`, correlate native callbacks, or decide whether an
operation is complete. Those responsibilities belong to the matching actor
reservation.

`finish()` is the only exactly-once settlement path. The registry is accessed
through the actor rather than a shared concurrent map, so it does not need a
`Mutex`, `DashMap`, or a second routing registry.

### `WaiterTicket`

`WaiterTicket` is the caller-side waiting handle. It waits on three things:

- the operation's oneshot result;
- the operation deadline; and
- the owner ingress lifecycle signal.

When the caller times out or drops the ticket, it sends `RetireWaiter` to the
actor on a best-effort basis. Retiring a waiter means that the caller no longer
receives a result. It does **not** claim that an already-dispatched native BLE
operation was cancelled. Native state and late callbacks remain governed by
the backend and actor correlation rules.

The ticket also detects a closed or faulted ingress before returning an ordinary
operation result. This prevents a result racing with owner shutdown from being
reported as a successful operation after the runtime has already failed closed.

### `WaiterSuccess` and `WaiterError`

These are internal orchestration results. They preserve typed data such as
`GattDatabase`, `ConnectionId`, RSSI, characteristic values, subscription IDs,
and notification mode until the BLeng API layer maps them to public errors and
return values.

`WaiterError` deliberately includes both local orchestration outcomes and
canonical BLE/dispatch errors. The conversion in `error.rs` is the public
boundary; the waiter must not duplicate the canonical `blehub` error taxonomy.

## Timeout, cancellation, and late events

The operation deadline covers both phases:

```text
begin_operation deadline
├── actor admission and command dispatch
└── remaining duration passed to WaiterTicket::wait
```

A timeout or caller cancellation retires the waiter, but does not forcibly stop
native work. This is required because CoreBluetooth, Android BluetoothGatt,
Windows, and other native transports do not provide one universal synchronous
cancellation contract for every operation.

After retirement:

```text
late callback
    -> actor correlation
    -> no matching active waiter
    -> no stale caller result
```

The backend may still need to retain a native operation slot until its late
callback or terminal state arrives. That resource-lifetime rule belongs to the
platform backend/runtime state, not to `WaiterTicket`.

A passive disconnect is handled separately from caller timeout: the registry
finds all accepted waiters bound to that connection and settles them as
`Disconnected`. This prevents read, write, RSSI, GATT, and notification waiters
from remaining pending after their connection has ended.

## Event-stream semantics

`RuntimeOwner::next_event()` is separate from command waiters:

```text
WaiterRegistry       -> resolves one specific command
RuntimeOwner events  -> exposes the shared bounded event FIFO
```

The event FIFO has one logical consumer. Concurrent `next_event()` calls are
serialized and divide the stream; events are not broadcast to every reader.
This is independent of the rule that one BLeng instance may manage multiple
Peripheral connections.

An event may both contribute evidence to a waiter and be made available on the
public event stream. These are two intentional projections of the same ingress,
not two competing application-level consumers of one command result.

## Why this is not over-abstracted

The apparent complexity comes from real BLE ordering and lifecycle constraints:

- dispatch acceptance is not operation completion;
- domain and terminal callbacks may arrive in either order;
- callbacks can be late after caller timeout;
- callbacks can belong to another connection, GATT session, characteristic, or
  subscription;
- a disconnect can invalidate several operations at once; and
- closing the BLeng owner must fail pending waiters without pretending to cancel
  native work.

A single `Future<Result<T, E>>` per command would not express these rules safely.
Likewise, replacing the concrete reservations with one enum state would lose
the independent facts needed for out-of-order callback handling and native
retirement.

## Simplification and ecosystem assessment

The current implementation already uses the relevant ecosystem primitives:

- Kameo actor/mailbox for serialized registry access;
- `tokio::sync::oneshot` for one waiter result;
- `tokio::sync::watch` for owner lifecycle wakeups;
- `tokio::sync::mpsc` for the bounded event stream;
- `tokio::time` for deadlines; and
- `HashMap<CommandId, WaiterEntry>` for stable caller command identity; and
- concrete actor reservations for operation evidence and native retirement.

No additional actor, promise, cancellation, slot-map, or concurrent-map crate
would replace the BLE-specific correlation policy. In particular,
`CancellationToken` could signal caller interest being withdrawn, but it could
not remove a `CommandId` from the registry, validate event identity, preserve
dispatch-rejection precedence, or cancel native BLE work.

The high-value simplification is now complete: the former `Pending` event
state machine was removed rather than duplicated beside reservations. Further
abstraction should remain conservative. Splitting `WaiterError` into several
smaller enums or replacing concrete reservations with a generic state-machine
abstraction would add conversion and matching complexity without reducing the
actual lifecycle states.

The current design should therefore be treated as a deliberately explicit
correlation layer. Its invariants are:

1. one actor-confined registry owns waiter linearization;
2. `finish()` is the only settlement path;
3. dispatch, domain, and terminal evidence may arrive in any order;
4. timeout/cancellation retires the waiter but does not promise native
   cancellation; and
5. late callbacks cannot resolve a retired command into a stale caller result.

### Cleanup and event-delivery boundaries

`HubCleanup` is a separate ownership/disposal coordinator. `AssemblyAdmission`
reserves the single primary Hub slot while foreign backend assembly is in
flight; `PrimaryRegistration` retains the dispatchable Hub, while rejected or
late Hubs receive their own disposal ledger slot. `start_cleanup()` is
idempotent, and `wait_for_completion()` waits for admission release plus every
registered disposal result. Disposal is deliberately fire-and-forget on an
executor-independent blocking thread: the cleanup coordinator can outlive the
Tokio runtime when a cancelled foreign assembly leaves a late attach behind.

Event delivery follows one fixed order:

```text
publish to bounded event FIFO
  -> record callback/domain/terminal evidence in reservations
  -> fail unrelated connection-bound waiters on passive loss
  -> settle the explicit waiter from reservation evidence
  -> retire only fully accounted native state
```

A passive disconnect has no command ID and therefore cannot directly settle an
explicit disconnect waiter. It can, however, provide the disconnect domain
evidence; the explicit command terminal remains authoritative, regardless of
which of those two events arrives first.
