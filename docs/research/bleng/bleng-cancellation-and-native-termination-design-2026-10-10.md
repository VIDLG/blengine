# BLeng Cancellation and Native Termination Design

**Status:** approved design direction; implementation is partial and remains unfrozen.

## Scope

BLeng exposes operation-level cancellation, while native backends also need connection-level disconnect and backend-wide disposal. These actions share driver infrastructure but do not share public semantics.

The central rule is:

> Share the native termination machinery; keep operation cancellation, connection disconnect, and backend disposal as separate policies.

## Why cancellation is worth adding

Cancellation is valuable for long-running typed operations such as characteristic reads, writes, GATT discovery, and subscription transitions. It allows a caller to stop one operation without silently disconnecting the entire peripheral and gives the caller a precise answer when a platform cannot cancel that operation.

Cancellation is not required to be supported for every operation on every platform. A backend must return `OperationNotCancellable` when it cannot provide the promised native control. It must not emulate single-operation cancellation by disconnecting unless a separate, explicit recovery policy has been selected.

The BLeng handle therefore separates:

- a shared target identity and `OperationControl` used by `cancel()`;
- a single-consumer result slot used by `wait()`.

A caller timeout, task drop, or result consumption retires caller delivery only. None of these events is silently converted into native cancellation.

## Three distinct termination policies

| Action | Scope | Native target | Caller result | Resource effect |
|---|---|---|---|---|
| `cancel_operation` | one operation | one `CommandId` | `Cancelled`, or precise cancellation rejection | preserve the connection when possible |
| `disconnect` | one connection | connection and all child operations | disconnect result; children become `Disconnected` or `OperationAborted` | invalidate GATT routes and release the connection |
| `dispose` | the whole backend/Hub | all scans, connections, callbacks, and handles | `Closed` or cleanup error | close admission, drain callbacks, release every native root |

`disconnect` and `dispose` may internally stop native operations, but they are not implemented by repeatedly calling the public `cancel_operation` API. Their scope, ordering, terminal errors, and completion barriers are different.

## Shared native infrastructure

Each platform driver should provide one serialized control boundary per connection where applicable. The boundary owns the native queue and callback correlation:

```text
ConnectionDriver
├── bounded operation FIFO
├── one active native operation where the platform requires it
├── command and generation correlation
├── callback registration and late-callback barrier
├── exactly-once terminal settlement
├── native handle ownership
└── internal termination control
    ├── CancelOne(command_id)
    ├── Disconnect(disconnect_command)
    └── Shutdown(shutdown_reason)
```

The shared infrastructure is responsible for:

- serializing state transitions with native submission;
- distinguishing queued, active, completed, and retired operations;
- invoking a native cancel/close primitive outside state locks when required;
- preventing stale callbacks from completing a new generation;
- draining or rejecting queued work during teardown;
- releasing callback tokens, native handles, and queue entries exactly once.

The policy layer remains responsible for:

- which operations are cancellable;
- whether a cancellation preserves the connection;
- which terminal error is visible to each child operation;
- whether the connection or entire backend must be retired;
- which cleanup barrier the caller waits for.

## Cancellation flow

```text
caller handle.cancel()
    -> BLeng OperationControl
    -> actor linearizes target command
    -> native driver accepts or rejects control
       ├─ rejected: clear cancellation latch; original operation continues
       └─ accepted: caller receives cancellation receipt
                    -> target waiter becomes Cancelled
                    -> native terminal/recovery evidence still reconciles
```

A native cancellation receipt means that the driver accepted the control request. It does not mean that the peripheral has already undone a write, CCCD change, or other side effect. The original reservation remains until its native terminal or recovery evidence is accounted for.

## Disconnect flow

Disconnect is a connection lifecycle transition, not a batch of independent cancellations:

```text
disconnect(connection)
    -> linearize connection teardown
    -> stop/revoke child routes and queued work
    -> request native disconnect / close
    -> emit child terminal outcomes with connection-specific reasons
    -> emit disconnect domain and terminal evidence
    -> retire the connection generation
```

A child operation terminated by disconnect should not be relabeled as caller cancellation. The expected distinctions are:

- `Cancelled`: the caller won the operation-level cancellation race;
- `OperationAborted`: disconnect or recovery terminated another accepted operation;
- `Disconnected`: passive link loss or native connection loss terminated it;
- `Closed`: backend disposal terminated it.

## Dispose flow

Disposal is backend-wide and idempotent:

```text
dispose
    -> close command admission and ingress
    -> snapshot native roots under the backend state gate
    -> release the state gate
    -> stop scans and terminate every connection driver
    -> cancel/close native operations and callback registrations
    -> drain required callback barriers
    -> publish one retained cleanup result
```

Disposal must not wait on an operation-level cancellation receipt for every command. It owns a global cleanup barrier and returns only after the backend's native roots and callback registrations satisfy the backend cleanup contract.

## Current implementation status

Verified in the current workspace:

- BLeng `OperationControl` and the one-shot read result slot are separate;
- cancellation dispatch acceptance/rejection is returned through an actor receipt;
- the actor does not settle the operation as cancelled before native control acceptance;
- Windows GATT I/O has an initial queued/active cancellation slice using its existing per-connection FIFO and WinRT `IAsyncOperation::Cancel()`;
- Windows connect-stage `BluetoothLEDevice` and GATT-probe operations now use the same native cancel entry;
- Windows disposal already has a separate backend-wide cleanup path.

Still unverified or incomplete:

- native watchdog and connection recovery;
- Android single-operation cancellation semantics;
- Apple single-operation cancellation semantics;
- cross-platform disconnect/cancellation/dispose race gates;
- Windows-target compilation and RF validation from the current macOS host.

No platform result should be described as complete native cancellation evidence until those boundaries are independently tested.
