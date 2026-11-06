# BLeng Reservation Reconciliation with Native Per-Connection Queues

**Date:** 2026-10-11
**Status:** BLeng storage migration and bounded concurrent admission implemented; cross-platform verification remains pending

## Finding

The native operation-queue work is complete enough to expose a modeling mismatch in the current BLeng reservations.

The maintained Android and Apple backends now serialize GATT work through a per-connection FIFO while allowing different connections to progress independently. The native queue owns queued admission, active-head protection, queued cancellation, active-operation rejection, watchdog retirement, and late-callback rejection.

`ConnectionReservation` now stores read, write, and RSSI entries in the command-keyed `IoReservations` collection. BLeng admission now allows accepted read/write/RSSI siblings and route-specific subscription transitions, subject to the same per-connection bound as the maintained native queues. Pre-dispatch handoff safety, disconnect/discovery barriers, duplicate subscription routes, and same-characteristic read/subscription protection remain explicit policy checks.

The storage and admission alignment is implemented and covered by deterministic Rust tests. Cross-platform native queue gates, Android/Apple artifact gates, and real runtime evidence still need to be rerun against the same candidate before claiming the complete end-to-end design.

## Current ownership boundaries

- `ConnectionReservation` owns one logical connection lifecycle, GATT database/session validity, subscription routes, and child operation evidence.
- The native backend owns the actual per-connection execution FIFO and native handles.
- BLeng reservations own caller completion evidence and command correlation; they must not duplicate native execution state.
- `CommandProgress` remains the common dispatch/terminal/cancellation state for each logical command.

## Recommended target design

Introduce a connection-local keyed operation collection, tentatively `IoReservations`, for read, write, and RSSI child reservations.

```text
ConnectionReservation
├── root connect progress
├── disconnect operation             # exclusive teardown authority
├── GATT discovery operation         # exclusive database replacement barrier
├── IoReservations
│   ├── read: CommandId -> ReadReservation
│   ├── write: CommandId -> WriteReservation
│   └── rssi: CommandId -> RssiReservation
└── subscriptions: SubscriptionId -> SubscriptionReservation
```

`IoReservations` should provide the common operations needed by `ConnectionReservation`:

- insert a reservation by its unique `CommandId`;
- find a reservation for dispatch buffering;
- record an event using exact command/domain correlation;
- resolve dispatch acceptance or rejection;
- enumerate `CommandProgress` for lifecycle accounting and cancellation;
- retire completed entries independently;
- report whether any operation is pending without exposing the maps.

The operation-specific reservation types should retain their domain evidence and waiter-result conversion. The collection should own only storage, lookup, iteration, and common lifecycle plumbing.

## Admission policy

The implemented BLeng policy is:

- read, write, and RSSI may be admitted concurrently on one connected connection;
- multiple read/write/RSSI commands may be queued, subject to a bounded BLeng admission limit aligned with backend capacity;
- the native backend FIFO determines execution order on that connection;
- different connections remain independently concurrent;
- disconnect remains an exclusive teardown barrier and rejects new child admission;
- GATT discovery remains an invalidation barrier and must not race with queued or active GATT work;
- subscription transitions remain route-specific barriers until their CCCD evidence is complete;
- a duplicate subscription route for the same active subscription remains rejected;
- the same-characteristic read/subscription policy must be made explicit rather than inherited from the old global `has_characteristic_io()` check.

A synchronous backend `ResourceExhausted` remains a dispatch rejection. BLeng must remove only the rejected reservation and preserve other queued reservations on the same connection.

## Cancellation and timeout requirements

Each queued child reservation keeps its own `CommandProgress` and cancellation result. Therefore:

- queued cancellation removes or retires only the target command;
- active cancellation returns the precise `OperationNotCancellable` result when the backend cannot cancel the native head;
- timeout retires caller delivery but preserves the reservation until native evidence or recovery retires it;
- late evidence for one command cannot settle a sibling command;
- disconnect/dispose retires the complete native lane, while BLeng settles every affected reservation exactly once with the appropriate local/native error distinction.

## Migration order

1. **Completed:** Add `IoReservations` as a private storage abstraction.
2. **Completed:** Migrate routing, dispatch resolution, cancellation lookup, accepted-command accounting, waiter lookup, and retirement from the three `Option` fields.
3. **Completed:** Preserve and extend deterministic tests without weakening production visibility.
4. **Completed:** Change BLeng admission to bounded concurrent read/write/RSSI operations while retaining lifecycle barriers.
5. **In progress:** Complete deterministic fake-backend coverage for same-connection FIFO admission, sibling completion, queued cancellation, active cancellation rejection, timeout plus late completion, dispatch rejection isolation, subscription-transition capacity, and disconnect retirement.
6. **Pending:** Run native Android JVM, Apple deterministic, Rust workspace, and staged artifact gates against the same candidate.

No public ABI change is required. This is an internal BLeng reservation/storage and admission-policy change. The implementation should not expose native queue internals through `blehub` or the RN low-level module.

## Remaining verification

The BLeng implementation now aligns its storage and bounded admission with the maintained native per-connection queue. The remaining work is verification rather than another admission-policy decision: finish the deterministic sibling/FIFO/cancellation matrix and rerun Android, Apple, Rust, and staged artifact gates against one candidate. Native queue internals remain private to each platform backend; BLeng does not claim physical RF parallelism.
