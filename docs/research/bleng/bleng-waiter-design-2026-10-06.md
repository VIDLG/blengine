# BLeng waiter design and operation completion

## Status and scope

This document describes the current internal waiter implementation in
[`crates/bleng/src/waiter.rs`](../../../crates/bleng/src/waiter.rs) and its runtime
integration in [`crates/bleng/src/runtime/owner.rs`](../../../crates/bleng/src/runtime/owner.rs).
The predispatch-order simplification, consolidation into one reservation module,
and three lifecycle review corrections have their original validation records below.
The 2026-10-07 reservations refactor is described from the current local worktree:
shared `CommandProgress`, a concrete GATT owner, private subscription states, and
connection-local operation admission. It adds no dependency, generic operation
state machine, or routing index. Source inspection is not a current-candidate
validation result; the current deterministic rerun is recorded separately below,
and earlier evidence is not relabeled as a rerun.
This is an implementation/design record, not a new architecture proposal, a
separate public BLE protocol, or a native-operation cancellation guarantee.
The mailbox protocol has a single `runtime/messages.rs` module rather than
separate command/event protocol modules. Actor state and coordination remain
private, while owner retains caller waiting and deadlines.

For a concise Chinese account of the actor/reservation boundaries, see
[BLeng actor 与 reservation 设计](bleng-actor-reservations-design-2026-10-07.zh-CN.md).

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
RuntimeOwner::run_operation(timeout, make_message)
      |
      +-- one Hub/lifecycle preflight; release the temporary Hub
      +-- compute one absolute deadline; scopeguard::guard(BeginHandoff, ...)
      +-- message = make_message(handoff.clone()): synchronous factory, not an ask
      +-- correlation = message.correlation(): snapshot lightweight IDs
      +-- timeout_at(deadline, actor.ask(message)): owner sends and awaits handoff
      |
      v
Actor checks the operation, then registers waiter + IDs
      |
      +-- complete all fallible operation checks before registration
      +-- allocate a typed resource ID separately, if creating a resource
      +-- register_waiter: allocate CommandId, expectation, and oneshot channel
      +-- launch_dispatch: try_claim caller/dispatch handoff
      |     +-- cancelled: remove only waiter; no reservation exists
      |     +-- won: synchronously/infallibly reserve, then spawn_blocking
      +-- return BeginReply: owner defuses handoff guard, then waits on ticket
      +-- backend command cannot start before its reservation exists
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
             settle_waiter: registry contains command?
                    |
                    v
             reservation waiter_result() (only for a live waiter)
                    |
                    v
             WaiterRegistry::finish exactly once
                    |
                    v
             oneshot receiver -> WaiterTicket::wait_until(same deadline)
                    |
                    +-- errors propagate through ?; skip success decoding
                    v
             M::decode_success(correlation, reply.resource, success)
                    +-- Some(output): move out typed result
                    +-- None: owner faults lifecycle, returns TransportFault
```

The private `RuntimeOwner::run_operation(timeout, make_message)` directly performs one
Hub preflight, computes one absolute `Instant` from `timeout`, and creates the
handoff scope guard. `make_message: FnOnce(BeginHandoff) -> M` is a synchronous message
factory: it only constructs a Begin message, not a future, an actor ask, or a wait
for handoff or native BLE completion. Owner calls `make_message(handoff.clone())`,
snapshots lightweight `M::Correlation` IDs with `message.correlation()` before
Kameo consumes the message, then centrally awaits
`timeout_at(deadline, async { self.actor.ask(message).await })`. The factory has
no separate timer; ask and ticket waiting share the already computed deadline.
Receiving `BeginReply` defuses the guard with
`drop(ScopeGuard::into_inner(handoff))`, releasing its captured lifecycle and
returned token before evidence waiting. The reply is handoff metadata, not BLE
completion; ticket waiting uses that same deadline, not a fresh relative timeout.

All 11 `Begin*` types implement the private
[`OperationResult`](../../../crates/bleng/src/runtime/messages.rs) trait. Each defines:

- `Resource`: the newly allocated typed ID returned by `BeginReply`, or `()`;
- `Output`: the operation's typed return value;
- `Correlation`: only the original request IDs needed to validate success, or `()`;
- `correlation(&self) -> Self::Correlation`: snapshot those lightweight IDs; and
- `decode_success(correlation, resource, success) -> Option<Self::Output>`:
  validate the success variant and IDs, then move out the result.

After `WaiterTicket::wait_until(deadline).await?`, owner calls
`M::decode_success(correlation, reply.resource, success)`. All existing variant
and identity checks remain intact:

| Request | Required success variant and identities |
|---|---|
| `BeginInitialize` | `Initialized`; move out the snapshot. |
| `BeginScan` | `ScanStarted`; scan matches the allocated `resource`. |
| `BeginStop` | `ScanStopped`; scan matches the requested session. |
| `BeginConnect` | `Connected`; connection matches the allocated `resource`. |
| `BeginDisconnect` | `Disconnected`; connection matches the request. |
| `BeginDiscoverGatt` | `GattDiscovered`; database connection matches the request and GATT session matches the allocated `resource`. |
| `BeginRssi` | `RssiRead`; connection matches the request. |
| `BeginRead` | `CharacteristicRead`; connection, GATT session, and characteristic all match the request. |
| `BeginWrite` | `CharacteristicWritten`; connection, GATT session, and characteristic all match the request. |
| `BeginEnableNotifications` | `NotificationsEnabled`; connection, GATT session, and characteristic match the request; subscription matches the allocated `resource`. Return subscription and actual mode. |
| `BeginDisableNotifications` | `NotificationsDisabled`; connection, GATT session, and subscription match the request. No new characteristic comparison is added. |

`Some(output)` returns the extracted result. `None` signals an internal invariant
mismatch: owner faults the lifecycle and returns `TransportFault`, not an ordinary
BLE operation failure. Decoding itself has no lifecycle dependency. Begin errors
return through the existing error paths, and BLE/dispatch errors propagate through
the wait result's `?`; neither enters success decoding.

This replaces per-call result-mapping closures with request-owned decoding, not
BLE evidence interpretation or a new execution layer. The integration clones
neither message nor payload and adds no boxing: owner retains only correlation
IDs while Kameo consumes the request, including a write payload, and decoding
moves out read bytes or database
data. `handoff.clone()` only clones the shared handoff token. `WaiterSuccess`
remains a data enum. `From` is unsuitable because conversion is fallible and needs
expected IDs from the request and reply, not just the success value.

The dispatch result, domain event, and terminal event are separate facts. Their
order is not assumed. For example, a GATT operation may observe its database
fact before the terminal event, or receive the terminal event before all facts
needed by the public result have been recorded.

## Independent command and resource IDs

The actor owns two independent, actor-confined `IdGenerator` values in
[`runtime/actor.rs`](../../../crates/bleng/src/runtime/actor.rs): `command_id_gen` for
commands and `resource_id_gen` for new scan sessions, connections, GATT sessions,
and subscriptions. Both start at 1 and need no atomics or locks. Resource types
share one resource sequence; no additional per-type generators are introduced.

All commands use `register_waiter(expectation)`. Resource-creating commands first
allocate and convert their resource ID, then build the expectation directly.
The paired `register_waiter_with_following_id()` API and its expectation closure
are removed. Command and resource IDs need not be adjacent or numerically
unequal: `CommandId(1)` and `ConnectionId(1)` are distinct typed identities.

Issued IDs are never rolled back or reused. Waiter-admission failure, caller
cancellation, dispatch rejection, and operation failure may leave gaps. ID
allocation does not create a reservation or start native dispatch. The waiter
registry requires strictly increasing command IDs, not contiguous numbering.
Each sequence issues `u64::MAX` once and then remains exhausted with
`IdExhausted`, independently of the other sequence; neither wraps to zero.

Regression coverage lives in
[`runtime/tests/id_allocation.rs`](../../../crates/bleng/src/runtime/tests/id_allocation.rs):
equal-valued command/resource callback correlation and failed admission/cancellation
without reuse. The actor-private maximum-ID exhaustion regression lives in
[`runtime/tests/actor.rs`](../../../crates/bleng/src/runtime/tests/actor.rs).
The former test proving two standalone local generator values are independent
was removed; actual actor command/resource allocation separation remains covered.

## Connection reservation collection

`BlengActor.connections` owns `ConnectionReservations` in
[`runtime/reservations/connections.rs`](../../../crates/bleng/src/runtime/reservations/connections.rs).
Its peripheral-keyed map is private. The collection owns root registration,
connection/command lookup, terminal/result routing, and completed-root removal.
It adds no secondary index, synchronization, shared ownership, or scheduling.
The collection resolves the connection root and peripheral key, then delegates
operation admission to `ConnectionReservation`: disconnect, discovery, RSSI,
characteristic I/O, notification enable, and notification disable. The record
owns operation-local evidence and checks; disable also returns the characteristic
ID needed for registration. Moving these checks preserves operation-specific
conditions and error precedence, not just `validate_gatt_access()`.

Registration cannot replace an occupied peripheral slot. Normal duplicate
connects remain admission errors (`PeripheralBusy`); an internal duplicate install
is an invariant failure. A retired peripheral can be reused only with fresh
connection/command IDs, so late terminals cannot affect its replacement.

Dispatch resolution finds the owning connection once, then handles its local
slot. Domain callbacks still require their exact command/session/target match.
The collection exposes no map, `Deref`, or indexing. Pre-ack matching and local
rejection replay filtering now belong to the collection and its reservations;
the event coordinator no longer traverses connection records, and the old
`iter_mut()` API is removed. The actor still executes replay and lifecycle
requests and delivers waiter results.

The actor still publishes events, records evidence, and settles affected waiters
before calling `connections.retire_completed()`. The collection neither owns
nor settles waiters. Collection regressions cover duplicate-root preservation,
terminal isolation between connections, and stale terminal rejection after
reconnecting the same peripheral.

## Actor encapsulation and forwarding

`pub(super)` is limited to runtime internals, including sibling modules that
implement actor handlers; it is not a public crate API. The actor now keeps
`event_tx` and its waiter registry private. Event coordination uses
`publish_event()`, which checks the open gate, enqueues locally, and faults on
outward queue failure. Success means local enqueue only. The private event
coordinator `process_event()` rechecks the lifecycle before processing evidence; publication
admission and continued processing are separate responsibilities, not duplicate
`require_open()` calls inside one helper. The post-send check cannot retract a
queued event or make publication atomic with concurrent shutdown. It is a policy method, not a raw sender delegation.
Command-side cancellation and timeout share
`retire_waiter(command_id, timed_out)`: it calls `waiters.timeout` when timed out,
otherwise `waiters.cancel`, preserving the previous behavior without two separate
delegates. Actor-local result delivery accesses the private registry directly;
a lost handoff in `launch_dispatch()` also calls `waiters.cancel` directly.

`RuntimeLifecycle::is_open()` queries the existing atomic phase without reserving
admission or preventing concurrent close/fault. Paths that distinguish `Faulted`
from `Closed` still use phase; no duplicate state is introduced.
The lifecycle owns phase-to-admission-error mapping (`require_open()`), and
`InitializationState` owns readiness-to-admission-error mapping
(`require_initialized()`). Only `Ready` passes the initialization gate;
`NotStarted`, `Pending`, and `Rejected` return `NotInitialized`. The actor delegates both
gates without reinterpreting their state. Connection registration and operation reservation methods
are delegated to `ConnectionReservations`, which owns their installation logic.
`InitializationState::reserve_initialize()` uses a private `InitReservation::new()`.
`ScanState` owns scan-slot admission, start/stop registration, rejection, and
retirement; stop evidence installation remains local to `ScanReservation`.
All pure forwarding is grouped in one `delegate!` block by owner. The four scan
command entries forward directly to `self.scan`, without actor-side optional-root
checks or unwrapping. Reservation installation uses the same names at both layers
(`reserve_initialize`, `reserve_connection`, `reserve_scan_stop`), eliminating
unnecessary `#[call]` name mappings. Context-specific forwarding names such as
`allocate_resource_id()` to generator `allocate()` remain intentional; waiter
cancellation/timeout instead use the one explicit retirement entry above.
The actor retains ID allocation, cross-reservation settlement, and transport
policy. The connection collection needs no `Option`: its empty map already
represents zero connections. `ScanState` explicitly owns the single scan slot:
`Idle` or `Reserved(ScanReservation)`. Like `InitializationState`, it is an opaque
struct: its phase, phase enum, session record, and stop record are private.
External code uses owner methods rather than constructing variants or replacing
payloads.

`Reserved` includes pending, running, stopping, and stopped-but-unsettled
sessions. It does not claim native scanning is active. A matching start dispatch
rejection or evidence-complete retirement returns the slot to `Idle`; native
stop alone cannot release incomplete start evidence. Installation cannot replace
an occupied slot, and a stale rejection cannot release a different session.
The state type owns availability, registration, rejection, terminal/result
routing, and retirement rather than existing only to support delegation. It adds
no separate Running/Stopping flags or linear state machine over independent
callback evidence. `reserved()` / `reserved_mut()` remain private owner helpers,
not outward actor getters. Start installation reuses `require_scan_available()`
as an invariant assertion; stop installation similarly reuses `validate_stop()`.
Internal misuse cannot install stop before start evidence is complete, overwrite
an unresolved stop, or install stop on an ended session. The actual reserved payload is
`Box<ScanReservation>`. Boxing keeps the idle slot compact at the cost of one
allocation per admitted scan session, not per callback, without adding shared
ownership or synchronization. The earlier inline-size measurement is not a
current-layout claim.

Rejected stop dispatch distinguishes independent native teardown from requested
stop evidence. The [contract's `command_id()`](../../../crates/blehub/src/contract/events.rs)
returns no command ID for `SystemSuspended` or `ScanStopResult::Err`. Their
buffered events are replayed in order after removing the rejected stop slot;
the actor settles `DispatchRejected` before publishing and recording teardown,
then retires the scan. Passive teardown cannot turn rejection into stop success,
and the rejected command's terminal is discarded. Any buffered `Requested`
stop still faults as contradictory, even when mixed with passive teardown.
The scan regression covers both passive outcomes, restart, late old-session
callbacks, and both mixed requested/passive callback orders.

All actor fields are private, including `lifecycle`, `initialization`, `scan`,
and `connections`. Command and subscription handlers use admission methods
forwarded with `delegate!`; validation returns only an error or the IDs needed
for reservation registration, never an entire reservation or mutable getter.
The owning reservation validates its local conditions with the same error
precedence as before. `ScanState` owns slot availability and release, while
`ScanReservation` owns session-local stop validation.

`launch_dispatch()` belongs to the actor root, which clones its private
lifecycle internally for detached blocking work. Messages do not acquire the
actor's lifecycle Arc. The sibling `runtime/messages.rs` unifies the mailbox
protocol. Its two asynchronous-input `Message` handlers only call
`self.handle_ingress_event(event)` and
`self.handle_dispatch_result(message.id, message.result)`, respectively;
these entry names remain unchanged. These synchronous entries are `pub(super)`
methods in actor.rs, not public crate APIs. All state coordination stays in
actor.rs behind those two entries: early-callback buffering, foreign-disconnect
exclusion, publication, evidence recording, settlement, retirement,
`DispatchAction` execution, and replay. The remaining coordination helpers are
private. Messages cannot access actor fields or obtain state through new getters.
This is source organization within the existing runtime, not a new boundary;
it adds no actor, lock, queue, index, or state machine and changes neither ABI
nor processing order. Delegation still replaces pure forwarding, not execution
order or lifecycle handoff.

Runtime production files and tests use explicit imports instead of parent or
prelude globs. `runtime/mod.rs` no longer acts as a shared external-import bag.
The test files stay under `runtime/tests/`, but their logical parents follow the
state owner. `owner.rs` mounts `tests/mod.rs` for owner and end-to-end flow tests;
`actor.rs` mounts `tests/actor.rs` for private-invariant assertions and
`tests/actor_support.rs` for fixture message handlers. Only the fixture messages
are re-exported under `#[cfg(test)]`, not actor fields. Custom fixture tests run
the production `run_operation(timeout, make_message)` with synchronous message
factories and typed `BeginReply` fixture replies. Fixture messages implement the
same `OperationResult` interface rather than passing result-mapping closures or
using a test-only begin helper.

All `RuntimeOwner` fields are private, including `actor`, `lifecycle`, and
`shutdown_result_tx`; `BackendPort.lifecycle` is private too. `WaiterTicket`, its
receiver/wait method, `run_operation()`, and `ticket()` are owner-private
implementation details, with no runtime-level ticket re-export. Production
visibility follows actual system dependencies, not test access. No production
getters, generic actor-access interfaces, or test-only production methods are
added to accommodate white-box assertions.

## Event reconciliation boundaries

Private methods in `actor.rs` coordinate routing and side effects rather than
unpacking reservation fields; the two asynchronous-input handlers in
`runtime/messages.rs` only forward through the restricted actor entries.
Initialization, scan, and connection owners provide:

- `gate_event(event)`: gate a live callback before publication and evidence
  recording. Correlated callbacks awaiting a dispatch result enter the existing
  bounded buffer; native dispatch may already be running. `Ok(None)` means buffered,
  `Ok(Some(event))` means continue to the next owner or normal processing, and overflow
  or initialization evidence contradicting rejection returns `ReconciliationFault`.
  Passing through is not an ownership answer: both unclaimed events and owned events
  no longer needing buffering pass. Buffering retains the event for ordered replay;
  it does not discard it.
- `record_event(&event)`: validate command/connection/session/target identity and
  record local evidence, without publishing, settling, or retiring resources.
- `resolve_dispatch(command_id, result)`: accept dispatch and take buffered events,
  or remove rejected provisional state while preserving committed GATT databases
  and active routes where required.

Initialization keeps admission checks separate from reservation installation:
check before waiter registration, install only after the caller handoff is claimed.
After evidence recording, settle the waiter before calling
`advance_after_settlement()` to advance readiness or report accepted failure.
`Ok(())` from that method does not imply readiness; incomplete evidence stays pending.
Likewise, `waiter_result(None)` is not an ownership answer: the command may be owned
but incomplete. In contrast, `record_terminal()` returns command ownership and
`resolve_dispatch(None)` means no pending initialization owns that dispatch result.

These result and settlement contracts also apply to scan and connection owners.
Their `waiter_result()` returns `None` for either unowned commands or incomplete
results; ID-free queries on already-selected I/O/GATT leaves mean incomplete evidence.
The connection collection is the actor's final terminal route, so it needs no
ownership boolean. Retirement APIs retain resource-specific names and run after
waiter settlement. Subscription retirement returns `None` for no completed
transition, or `Some(SubscriptionTransitionRetirement)` containing the completed
command ID and an independent `Keep` / `Remove` route disposition. Keeping a route
does not imply command success; `SubscriptionReservations` applies `Remove` within its private collection.

Leaf correlation methods are named `matches_related_event()` and reused for
buffering and recording. Related terminals or connection loss are not success
evidence, and an event can relate to multiple reservations. Buffer decisions now
live in reservation-owned `should_buffer_event()` methods for initialization,
disconnect, GATT discovery, RSSI, read/write, subscription transitions, and scan
stop. Scan start uses `should_buffer_start_event()`; the connection root uses
`should_buffer_connect_state_event()` to retain its broader command-or-connection
scope for state callbacks only. Terminals already use the common command gate,
not a second branch in this private state predicate.
These methods combine unresolved dispatch with correlation. An active subscription
without a transition cannot buffer. Containers select targets without repeating
dispatch checks. The affected connection's gates precede any foreign root-connect
fallback; a command route cannot bypass a connection-wide state gate.
Reusable `matches_related_event()` predicates remain pure correlation queries for
recording, and initialization's pure matcher also detects callbacks contradicting
rejection. Recording is gated by the specific domain-event variant, so broader
loss/terminal buffering matches cannot manufacture domain evidence. Connection lifecycle and
explicit-disconnect domain evidence are updated through a single lookup.

The pre-ack order remains initialization, connections, then scan start/stop.
Non-state events with registered command routes still select their connection
through that route. The collection fallback selects the affected connection by event
identity, or a terminal's owner by command identity; unrelated events pass through
without scanning connection records. Only state callbacks probe multiple categories within the selected record,
in the order disconnect, discovery, RSSI, read, write, subscription transition,
connect. Other events select their one relevant category. A state callback naming a
different root command can reach that connect fallback only after the affected
connection's gates. This preserves child-before-foreign-connect priority without
probing every category across every connection or adding a second routing index.
Connection-ID lookup remains linear over the peripheral-keyed collection.
Foreign-command loss of a known connection remains publicly visible at the actor
boundary but bypasses buffering and cannot mutate reservation evidence.

`DispatchResolution` is a transient reconciliation outcome, not another state registry.
It synchronously transfers early callbacks and pending work to the actor, not a
terminal operation result or the caller/actor submission claim performed by
`OperationSubmission::try_claim()`. No native ownership transfer is implied. It
contains `events_to_replay` and an `action: DispatchAction`: `NoAction`, `StartCleanup`,
or `FaultRuntime(ReconciliationFault)`. The action is requested work, not an
already-applied effect; `StartCleanup` does not promise completed cleanup.
`DispatchEvidenceConflict` identifies a dispatch-result/event-evidence conflict.
The actor executes the action, settles any dispatch rejection, then replays in
order. Each replay publishes, records, settles waiters, then retires state.
Generic operation rejection permits only passive-loss replay; subscription
rejection retains its broader connection-loss policy. Clean initialization
rejection requests Hub cleanup without faulting the runtime. No reservation
holds a lifecycle, waiter registry, event sender, or executor.

Each command's `CommandProgress` owns the existing 64-event buffer bound.
Reconciliation faults identify overflow, notification-mode mismatch, or contradictory dispatch;
existing tracing records the reason without callback payloads. Regression
coverage in [`reconciliation.rs`](../../../crates/bleng/src/runtime/tests/reconciliation.rs)
protects the exact bound, foreign-event pass-through, ordered replay, and distinct
I/O/subscription rejection policies alongside the existing actor regressions.

## Actor/reservation ownership

[`runtime/actor.rs`](../../../crates/bleng/src/runtime/actor.rs) is the actor root.
All actor-local reservation evidence belongs in
[`runtime/reservations/`](../../../crates/bleng/src/runtime/reservations/mod.rs).
Ownership and interfaces were unified first; after the single file grew to about
2,300 lines, the stable lifecycle boundaries were separated into this directory.
This source organization adds no actor, executor, synchronization, or routing layer.
The facade re-exports only the state owners and transient outcomes used by runtime.
Implementation modules remain private, and source declarations also restrict their
scope explicitly. Actor-consumed outcomes, actions, faults, cancellation receipts,
and admission kinds use `pub(in crate::runtime)` in leaf modules, with
`pub(super) use` re-exports in `mod.rs`. Directory-local command progress, buffered
classifications, publication facts and FIFO types use `pub(super)` and ordinary
private imports in `mod.rs`; they are not re-exported to runtime. Types and fields
are restricted independently, including runtime-only dispatch-result fields.

Root-owner types and methods directly exercised by existing runtime tests are
runtime-visible; methods used only by the coordinator and directory-local tests
remain reservations-visible. Test-only access helpers are also runtime-limited,
not crate-wide. The `rssi`, `read`, and `write` leaf modules under `io` are
private. Their types and the construction, correlation, recording, result,
completion methods and fields needed by the aggregate use `pub(super)` only to
reach the parent `io` module; other implementation details remain private. Leaf
types are not re-exported to connection or the reservations facade. Connection
uses `IoReservations::progress_for_cmd()` / `progress_for_cmd_mut()` for command
point lookups, `resolve_dispatch()` for dispatch acknowledgement/rejection, and
`pending_*_progress_for_event_mut()` for event-buffer selection. I/O entry removal
is private to the collection, not a separate connection-layer operation.
`GattReservation` also keeps its discovery slot, leaf type, and leaf fields
private to `gatt.rs`. Connection uses owner methods for event recording, buffer
target selection, dispatch resolution, result queries, and progress access rather
than filtering or removing the leaf itself. `ConnectionReservation.subscriptions`
holds `SubscriptionReservations`, which privately owns
`HashMap<SubscriptionId, SubscriptionReservation>`. Connection uses collection APIs
instead of iterating, reading, or removing subscription entries. Leaf fields and
phases are not mechanically refactored for encapsulation symmetry. Other progress
fields and subscription identities needed by sibling owners retain `pub(super)`; no equivalent getters
are introduced merely to hide those fields. These changes narrow internal Rust boundaries
without introducing an `IoOperation` trait or generic framework or changing the
public ABI.

`InitializationState` is an opaque struct: its `InitializationPhase` enum, phase
field, and `InitReservation` are private. Consumers cannot construct `Ready` or
overwrite pending evidence. Scan enum payload types still match their enum declaration
visibility, but are not independently re-exported and keep their fields private. Raw-record
queries (`pending`, `reserved`, `reserved_mut`, `find`, and `find_mut`) are internal.
The actor requests `accepted_command_ids(connection_id)` for loss settlement rather
than inspecting a connection record. Tests now exercise dispatch resolution,
correlated callbacks, results, and admission rules instead of depending on wider
record-field visibility.

| Owner / source | Responsibility |
|---|---|
| [`owner.rs`](../../../crates/bleng/src/runtime/owner.rs): `RuntimeOwner` | Owns the executor, `ActorRef`, outward `event_rx`, caller deadline/waiting policy, and shutdown coordination. It does not interpret BLE reservation evidence. |
| [`messages.rs`](../../../crates/bleng/src/runtime/messages.rs) | Unified mailbox protocol: 11 `Begin*` requests with `OperationResult` success decoding, `BeginReply`, and admission handlers; `IngressEvent`/`DispatchResolved` and two forwarding handlers; `RetireWaiter` and its handler. Owns no state, caller deadlines, or waiting. |
| [`actor.rs`](../../../crates/bleng/src/runtime/actor.rs): `BlengActor` | Owns serialized BLE orchestration, `WaiterRegistry`, waiter delivery, `event_tx`, reservation storage, and all state coordination. Retains `Drop` and register/reserve methods, not a prelaunch rollback guard. |
| [`backend_port.rs`](../../../crates/bleng/src/runtime/backend_port.rs) | Backend assembly and non-blocking ingress; imports `IngressEvent` directly from messages. Retains neither owner nor responsibility for interpreting reservation evidence. |
| [`reservations/mod.rs`](../../../crates/bleng/src/runtime/reservations/mod.rs) | Documents ownership and exposes the runtime-facing state facade. No command-index map. |
| [`dispatch.rs`](../../../crates/bleng/src/runtime/reservations/dispatch.rs) | `CommandProgress`: shared dispatch/first-terminal progress, bounded pre-ack buffering, and transient reconciliation outcomes. |
| [`initialization.rs`](../../../crates/bleng/src/runtime/reservations/initialization.rs) | Initialization evidence and readiness. |
| [`scan.rs`](../../../crates/bleng/src/runtime/reservations/scan.rs) | Scan slot, start/stop evidence, and retirement. |
| [`connections.rs`](../../../crates/bleng/src/runtime/reservations/connections.rs) | Peripheral-keyed roots, peripheral-slot admission, connection/command lookup and key resolution, operation installation, affected-connection-before-foreign-connect routing, and completed-root removal. |
| [`connection.rs`](../../../crates/bleng/src/runtime/reservations/connection.rs) | One connection root, per-operation admission, the shared I/O/subscription-transition capacity bound, disconnect, child-progress traversal, local correlation/reconciliation, and retirement. |
| [`gatt.rs`](../../../crates/bleng/src/runtime/reservations/gatt.rs) | Committed database and pending discovery evidence. |
| [`io/mod.rs`](../../../crates/bleng/src/runtime/reservations/io/mod.rs) | `IoReservations`, three command-keyed one-shot RSSI/read/write maps, aggregate routing, and retirement; no long-lived database or subscription routes. |
| [`io/rssi.rs`](../../../crates/bleng/src/runtime/reservations/io/rssi.rs), [`io/read.rs`](../../../crates/bleng/src/runtime/reservations/io/read.rs), [`io/write.rs`](../../../crates/bleng/src/runtime/reservations/io/write.rs) | Operation-specific event correlation, evidence recording, result selection, and completion checks. |
| [`subscriptions.rs`](../../../crates/bleng/src/runtime/reservations/subscriptions.rs) | `SubscriptionReservations`: private subscription map, lookup/occupancy queries, installation, progress inventory, buffer selection, event/dispatch-result routing, result queries, and retirement. |
| [`subscription.rs`](../../../crates/bleng/src/runtime/reservations/subscription.rs) | One subscription's enable/disable phase, mode verification, `validate_disable()`, `resolve_dispatch()`, and route disposition. |

GATT database/discovery state, one-shot I/O commands, and subscription routes are
sibling owners with different lifetimes. Connection owns the private 1,024-operation
admission bound and counts `io.len() + subscriptions.transition_count()`; stable
active routes, discovery, and connection lifecycle commands are outside this budget.
Sharing that budget does not make these owners one storage collection or mirror
all native execution-queue contents.

Reservations are the state model shared by command and event handling, not merely
passive storage: they own local admission, callback correlation, evidence updates,
dispatch reconciliation, result decisions, and retirement rules. They remain
ordinary actor-confined values, not independently scheduled objects. The actor coordinates across reservations and settles caller waiters;
reservation methods interpret and retire their own local evidence.
`ConnectionReservation::retire_completed_operations()` and `is_retirable()`
localize connection retirement. `IoReservations::resolve_dispatch()` owns I/O
acknowledgement/rejection and targeted entry removal. The connection supplies
`classify_rejected_child_event()`; `GattReservation::resolve_dispatch()` owns
discovery acknowledgement/rejection and slot removal.
`DisconnectReservation::resolve_dispatch()` resolves its own evidence using the
same passive-loss classification; connection clears its disconnect slot on rejection.
`SubscriptionReservations::resolve_dispatch()` delegates to the matching route
and immediately applies its disposition after buffered evidence is drained. These are dispatch
rejection/failure handling and evidence retirement, not prelaunch rollback.
Ingress/dispatch-result coordination belongs to
[`actor.rs`](../../../crates/bleng/src/runtime/actor.rs), with admission and mailbox
handlers unified in [`messages.rs`](../../../crates/bleng/src/runtime/messages.rs).
Notification requests follow the same split as other commands: admission
handlers live in messages.rs, caller-facing operations in owner.rs, and
subscription collection/state in reservations/subscriptions.rs and subscription.rs.
`WaiterRegistry` remains the caller command ledger; callback correlation uses the
existing reservation hierarchy, not an additional `CommandId` index.

Suggested reading order: `reservations/mod.rs` for the boundary, `dispatch.rs`
for shared progress, `connections.rs` for root lookup/scoped routing, then
`connection.rs` for local admission, settlement, and retirement. Follow with
`gatt.rs`, `io/mod.rs` and its three leaf modules, then `subscriptions.rs` for
the collection boundary and `subscription.rs` for typed evidence; initialization and
scan have their own owners. Finally read `messages.rs` admission/handoff and
`actor.rs` event coordination. Tests physically live under
[`reservations/tests/`](../../../crates/bleng/src/runtime/reservations/tests/):
`connections.rs` exercises collection-level invariants; `subscription.rs` is
mounted with `#[path]` as a child of the production subscription module, preserving
its privacy boundary. Production files contain no inline test bodies and expose
no additional visibility for tests. End-to-end tests remain under `runtime/tests/`.

### Command progress and derived state

[`CommandProgress`](../../../crates/bleng/src/runtime/reservations/dispatch.rs)
replaces `DispatchReservation` plus the separate terminal field on each concrete
record. Every command's `progress` retains `command_id`, private `buffered_events`,
private `dispatch_accepted`, and private `terminal_result: Option<Result<(), BleEventError>>`.
Initialization, scan start/stop, connect/disconnect, discovery, RSSI, reads,
writes, and notification transitions use this same boundary; they no longer
store a separate `terminal` or `start_terminal`.

`record_terminal()` preserves the first result, including its native error;
conflicting duplicate terminals cannot overwrite it. Queries use `has_terminal()`,
`terminal_succeeded()`, and `terminal_failed()`. `is_awaiting_dispatch_result()` means
the synchronous native dispatch result is still awaiting actor processing, not
that the caller waiter or native operation is pending. The result arrives in
`DispatchResolved`; it is neither the Begin reply nor a native BLE response.
Accepted dispatch alone is never BLE completion.

`CommandProgress::terminal_result()` takes no success closure and does not depend
on `WaiterSuccess`: unresolved dispatch or a missing terminal returns `None`;
a terminal error becomes an operation error; terminal success returns only
`Ok(())`, not proof of domain completion. Concrete reservations use straight-line
control flow: `terminal_result()?` waits for an accepted command's terminal;
`Err(error)` immediately returns `Some(Err(error))`; only success proceeds to domain
evidence and constructs `Some(Ok(success))`. Missing domain evidence returns `None`.
This replaces nested `map(...).transpose()` without reintroducing a success closure;
success payloads are still cloned or constructed only after terminal success.
`is_complete(domain_evidence_ready)` requires accepted dispatch plus a terminal,
and then either terminal failure or all correlated domain evidence required for
success. This is command-evidence completion, not the end of a native lifecycle;
it neither depends on a live waiter nor decides when native resources are removable.
Connection failure and passive loss retain their concrete-record precedence.
Typed snapshots, connection facts/errors, discovery drafts, RSSI/read payloads,
write evidence, and notification domain state remain local to their reservations,
not in this helper.

Peer modules cannot mutate `dispatch_pending_events`: only bounded
`buffer_dispatch_pending_event()` enqueues events while the dispatch result is unresolved.
`CommandProgress` owns exactly two dispatch-resolution entry points; both drain
buffered evidence:

- `resolve_accepted_dispatch()` marks dispatch accepted and transfers all early callbacks
  in order. It explicitly returns `DispatchAction::NoAction`; normal acceptance itself
  requests neither cleanup nor fault.
- `resolve_rejected_dispatch(action_if_no_conflict, classify)` drains early evidence without
  marking acceptance. The concrete reservation returns `BufferedEventDisposition`
  for each event: `Replay`, `Discard`, or `Conflict`. The shared loop preserves
  replay order and drops legitimately ignored evidence. `Replay` and `Discard`
  retain `action_if_no_conflict`; any `Conflict` overrides it with a fault, but
  classification continues.

Initialization, scan start, and connect classify every early callback as a conflict;
with no buffered events, initialization requests `StartCleanup` and the others
request no action. Ordinary I/O permits only passive-loss replay; subscriptions
retain their broader connection-loss policy. Scan stop uses the same entry point
with its local three-way policy: passive stop/error replays, requested stop conflicts,
and the rejected command's terminal is discarded.

`DispatchResolution` is only result data, with no separate classifier constructor.
`rejected_resolution_with_replay()` and `from_rejected_events()` are removed.
`take_dispatch_pending_events()` is private; peers no longer extract buffers or assemble
rejection outcomes in their own loops. Strict rejection also drains the buffer
instead of relying on later reservation removal to discard evidence.

For connection commands, the coordinator removes a rejected command's directory
entry only when its reservation recognized and consumed the result. Concrete owners
release the reservation or restore the subscription before this removal; an unmatched
result must not erase a directory entry.

Resource removal and subscription restoration stay with their concrete owners;
resolution construction executes no lifecycle action or event publication. The
actor still executes actions, settles rejection, then replays events; a fault
prevents further public replay.
Initialization and scan `record_event()` return `()` because they cannot
fail; the actor retains a fallible reconciliation boundary for connection-side
notification-mode validation.

Redundant flags are removed rather than maintained beside their source facts:

| Removed flag | Authoritative evidence |
|---|---|
| Scan `failed` | Start `progress.terminal_failed()`. |
| Connection `failed` | `connect_error` is `Some(_)`. |
| Discovery `failed` | Discovery `progress.terminal_failed()`. |
| Discovery `discovered` | `draft_database` is `Some(_)`. |

The active subscription route does not retain another copy of `actual_mode`:
the enable result reads it from transition evidence before that evidence retires.

The remaining independent domain facts are not collapsed into one status enum.
In particular, a draft database does not imply successful discovery until
accepted dispatch and a successful terminal are also accounted for.

### Shared child progress without uniform admission

`ConnectionReservation::child_progresses()` inventories disconnect, discovery,
RSSI, read, write, and subscription-transition progress.
`has_child_awaiting_dispatch_result()` queries only unresolved dispatch acknowledgements.
`command_progresses()` adds the connect root for connection-wide accepted-command
collection, publication barriers, and root retirement. Command membership,
terminal recording, and cancellation use `progress_for_cmd()` /
`progress_for_cmd_mut()`: inspect fixed slots, query existing I/O command maps,
then delegate subscription-transition lookup to `SubscriptionReservations`.
No mutable inventory iterator is needed.
Disconnect cancellation still applies its explicit non-cancellable/completed
policy before the shared lookup.

Each `IoReservations` already belongs to one connection; no `ConnectionId` index
is needed. Ordinary I/O callbacks locate a candidate by command ID and still
validate its full identity and dispatch state. Connection-state callbacks retain
the ordered category gates, even when they carry a command ID: loss can affect
other pending commands. Characteristic conflict checks and bulk retirement keep
their collection traversal rather than maintaining redundant indexes.

Shared predicates preserve the following distinct admission rules:

| Operation | Connection-local checks retained |
|---|---|
| Disconnect | Accepted connect owns a native lifecycle even before `Connected`. Reject disconnected/connect failure/unresolved connect first, then an existing disconnect or unresolved child handoff. |
| Discovery | Require active connection, then reject disconnect, unresolved RSSI, pending discovery, read/write, or any subscription route as `GattBusy`. An accepted RSSI is not blanket-blocked by the shared predicate. |
| RSSI | Require active connection, then reject an existing RSSI, disconnect, or unresolved child handoff as `OperationBusy`. |
| Read/write | Preserve connection → disconnect/unresolved RSSI → committed session/characteristic → I/O/subscription conflict → discovery precedence. Invalid targets still precede later busy checks. Read additionally rejects a subscription on the same characteristic; write does not. |
| Enable | Use `GattAccess::EnableNotifications` for shared GATT checks, then reject any route on the same characteristic after the pending-discovery check. |
| Disable | After collection root lookup, check subscription/session and activity, then transition busy, then connection and sibling-operation checks. `Disabling` remains active, so repeated disable returns `OperationBusy`, not `SubscriptionNotActive`. |

`GattAccess::{Read, Write, EnableNotifications}` replaces `read: bool` and makes
admission intent explicit in `validate_gatt_access()`. Enable no longer borrows
the write identity. Read checks same-characteristic subscription conflicts before
pending discovery; enable checks duplicate routes after it. The enum preserves
this precedence and does not describe backend capabilities.

### Begin requests and named replies

`Begin` names the admission/handoff stage of a command, not the start of every
BLE resource and not operation completion. `BeginStop` begins a stop command;
`BeginDisconnect` begins a disconnect command. Actor validation, handoff,
reservation installation, and native-dispatch submission precede the reply.
Backend acceptance is reported separately by `DispatchResolved`; BLE completion
still requires reservation evidence and waiter settlement.

[`messages.rs`](../../../crates/bleng/src/runtime/messages.rs) defines one runtime-only
`BeginReply<Resource = ()>` with named `command_id`, `result_rx`, and `resource`
fields. Scan, connection, discovery, and notification-enable replies carry their
new typed resource IDs; commands creating no resource use `()`, not an `Option`
or a resource-ID enum. All command handlers share this type, replacing the
four tuple aliases and subscription-specific tuples. Owner methods use named
reply fields and preserve the existing resource/result identity checks.

`WaiterExpectation` remains separate: it describes the result being awaited and
connection invalidation policy, not ownership of a result receiver. `BeginReply`
is transient handoff metadata, not another mutable operation state or a guard.
The generic representation adds no allocation or synchronization and does not
change the public ABI, dispatch ordering, deadlines, or cancellation behavior.

### Unified mailbox protocol and admission order

`messages.rs` organizes protocol types and their handlers in three groups.
All protocol types use `pub(super)` for runtime-only visibility:

1. The 11 requests `BeginInitialize`, `BeginScan`, `BeginStop`, `BeginConnect`,
   `BeginDisconnect`, `BeginDiscoverGatt`, `BeginRssi`, `BeginRead`, `BeginWrite`,
   `BeginEnableNotifications`, and `BeginDisableNotifications`, plus `BeginReply`
   and the command-admission handlers.
2. `IngressEvent` and `DispatchResolved`, with two asynchronous-input handlers
   that only call the restricted actor entries. `DispatchResolved` reports a
   dispatch outcome, not a new admission request.
3. `RetireWaiter` and its handler calling actor `retire_waiter(id, timed_out)`;
   it retires caller delivery, not the native reservation.

Owner imports requests and waiter-retirement messages directly from messages
rather than through runtime-root re-exports, receives the named reply, and owns
caller waiting and deadlines. Backend_port imports `IngressEvent` from messages; actor imports
`DispatchResolved` from the same module. Subscription APIs remain in owner.rs
and subscription state in reservations/subscription.rs.

Begin handlers retain explicit operation-specific orchestration through
restricted actor methods: lifecycle/readiness and operation validation, resource
ID allocation, and waiter registration. They are not one-line forwarders.
Actor-owned `launch_dispatch` then acquires a dispatchable Hub, claims the
handoff, installs the reservation,
and submits native dispatch. There is no await or normal-error branch between
installation and submission. Validation cannot install native reservations;
pre-handoff cancellation retires only the waiter. Issued ID gaps are allowed,
not rolled back or reused.

The first group orders Begin handlers by initialization, scan start/stop,
connection, GATT, I/O, and subscriptions, followed by the asynchronous-input and
waiter-retirement groups. Command/resource/peripheral ID names remain explicit.
Validation, expectation, reservation, and native command remain visible together;
`OperationResult` only associates success decoding with a request, not admission
or dispatch execution. No mega message enum, generic execution-trait framework,
handler-generating macro, queue, or additional state machine hides that
correspondence. Dispatch coordination remains inside the actor privacy boundary
rather than exposing its fields to
messages.rs. Command comments point to actor.rs for that coordination implementation.

### Success semantics by `Result` layer

Keep each layer's `Result<(), E>`: unit means no additional success payload at
that layer, not absence of errors or result accounting. Do not add information-free
success markers or merge the layers' Error types.

| Layer | Meaning of success |
|---|---|
| Validation | Checks passed, not handoff, backend acceptance, or BLE completion. Validations returning IDs or other data retain those values. |
| `launch_dispatch()` / `BeginReply` | Handoff won, reservation installed, and dispatch task submitted; not proof of backend entry or acceptance. |
| Synchronous backend `Result<(), CommandDispatchError>` | `Ok(())` means dispatch acceptance; `Err` means rejection. The complete result returns to actor through `DispatchResolved`, never ignored or treated as asynchronous completion. |
| Asynchronous operation / `WaiterSuccess` | Settled from dispatch, domain, and terminal evidence. Read bytes, RSSI, database, IDs/mode, and other data-bearing results retain their values. Platform write-without-response local submission/progress is not upgraded to a remote-processing guarantee. |
| Cleanup | `Ok(())` means accounting completed without a reported disposal error. Result slots retain success/errors; all slots settling can still produce `Err`, so settlement alone does not mean cleanup succeeded. |

### Cancellation before mutation and evidence lifetime

Predispatch ordering removes the need to undo reservation mutation:

```text
all fallible operation checks
  -> register waiter + command/operation IDs
  -> launch_dispatch: acquire Hub and check lifecycle
       failed    -> finish the waiter with that error; no reservation or dispatch
       ready     -> BeginHandoff::try_claim
                      cancelled -> cancel only the waiter; no reservation
                      won       -> synchronous, infallible reserve closure
                                  -> spawn_blocking native dispatch
```

Waiter/ID registration must succeed before handoff. There is no `await` between
the successful handoff, reservation registration, and blocking-task launch, and
no fallible operation check is deferred into the reserve closure. The native
command cannot start before its reservation exists. A cancellation that wins
`try_claim` therefore needs only waiter removal, not restoration of a previous
reservation or session. `ReservationRollback`, local prelaunch rollback methods,
and `RollbackOutcome` are removed by this change.

Owner-side handoff protection remains, but `scopeguard::guard` replaces the
custom `BeginHandoffGuard` type and its local `armed` bool (the old name describes
only the superseded implementation). `BeginHandoff` still arbitrates the atomic
caller-versus-dispatch race; this is not actor-local reservation rollback.
`BeginHandoff` replaces both `BeginState` and the Hub-carrying `BeginContext`.
It is one cloneable atomic token, with no Hub or lifecycle payload. It remains
in `owner.rs` because the owner creates it and owns acknowledgement/cancellation;
the actor only consumes `try_claim()`. Consumption in `launch_dispatch()` does
not make the token actor-owned state.

Shared atomic state and generic RAII protection are deliberately separate:

```text
BeginHandoff atomic: Pending -> Claimed
                      |           |
                      +-------> Cancelled
                                  (claimed cancellation requests a lifecycle fault)

scopeguard::guard(BeginHandoff::new(), closure)
  +-- un-defused Drop -> abandon() -> lifecycle.fault() if was_claimed
  +-- receive BeginReply -> drop(ScopeGuard::into_inner(handoff))
                           do not run closure; release lifecycle capture and token
```

`Claimed` means the actor won dispatch ownership, not that native entry occurred.
The atomic `Pending` / `Claimed` / `Cancelled` protocol is unchanged and has no
`Acknowledged` phase. `run_operation()` uses
`scopeguard::guard(BeginHandoff::new(), move |handoff| { ... })`; the closure
captures only `Arc<RuntimeLifecycle>`, never `RuntimeOwner` or a Hub. On an
un-defused Drop it calls `handoff.abandon()` and calls `lifecycle.fault()` only
if `was_claimed`. That return value reports prior actor ownership, not successful
native cancellation. Invalid atomic values retain the same fail-closed policy.

The begin-timeout branch explicitly drops the guard **before** classifying the
phase, preserving `Closed` / `TransportFault` precedence over ordinary `Timeout`.
Classifying first would hide a fault caused by abandonment. Receiving `BeginReply`
instead executes `drop(ScopeGuard::into_inner(handoff))`: `into_inner` does not
run the abandonment closure, drops its captured lifecycle, and returns the token
for explicit release, all before operation-evidence waiting. No custom
`acknowledge(self)` or local armed flag remains, and this defusal makes no atomic
write or native-cancellation promise.

The owner retains its backend-availability preflight to preserve existing error
precedence, but releases that temporary Hub reference before waiting for the
mailbox reply. Begin messages carry only `handoff` and operation parameters.
The actor reacquires the dispatch root through its lifecycle and checks phase
after the cleanup lock, before claiming the token. Missing roots, close, and fault
finish an already-registered waiter with the corresponding error and skip both
reservation installation and dispatch. There is no new long-lived actor Hub
field, and the check does not make native dispatch atomic with concurrent cleanup.

Both owner preflight and actor preparation use
`RuntimeLifecycle::hub_for_dispatch() -> Result<Arc<BleHub>, WaiterError>`.
Only cleanup retains the lower-level `Option` lookup, which knows its own gate
but not runtime phase. Lifecycle combines that lookup with the phase check after
the cleanup lock; runtime callers do not choose between unchecked and open Hub
accessors or duplicate the error mapping. Owner and actor checks occur at distinct
times: preflight preserves admission error precedence, while actor preparation
revalidates after mailbox waiting.

`try_claim()` only atomically claims the handoff: it neither installs a reservation
nor submits a dispatch task or proves native entry. The actor's `launch_dispatch()`
coordinates those later steps. The claim is an ownership-changing CAS, not a
read-only cancellation probe.
All fallible Hub/lifecycle preparation must precede it. Moving it first would
leave the token `Claimed` on ordinary preparation failure, causing the un-defused
caller guard to fault rather than return a clean rejection. The early-return
order is preparation error, lost handoff, then infallible reserve and task launch.

The reserve closure remains deliberately operation-specific: it delays mutation
until handoff wins without adding an installation enum, trait framework, or
rollback mechanism. Pure forwarding needed by sibling command handlers remains;
the actor-local `finish_waiter` forwarding helper is removed in favor of direct
registry access inside the actor privacy boundary.

The cancellation boundary has three distinct outcomes:

```text
owner's centralized ask within run_operation waiting for actor reply
  +-- caller cancellation wins before try_claim
  |     -> skip reservation and native dispatch; remove only waiter
  +-- try_claim wins -> synchronous reserve -> blocking-task launch
        +-- cancellation before owner acknowledges the begin reply
        |     -> owner Faulted -> close/cleanup; no rollback or native cancellation
        +-- owner acknowledges begin reply -> WaiterTicket
              -> timeout/cancel retires only caller waiter; retain reservation
```

Here acknowledgement means the owner accepting the actor's begin reply, not the
backend's dispatch acknowledgement or a BLE terminal event. Cancellation after
handoff but before that owner acknowledgement fails the owner closed through
the handoff scope guard; it is not ordinary waiter-only retirement. The handoff
establishes dispatch ownership, not proof that the native command has entered.
Cleanup may race native dispatch, so no native-entry or native-cancellation
guarantee follows from the fault.

Every callback path retains this ordering:

```text
record domain/terminal evidence
  -> settle the waiter from that evidence
  -> retire completed reservations or commit successful discovery
```

After the owner acknowledges the begin reply, timeout/cancellation retires only
the waiter and retains the reservation. Reservations still process late
callbacks and can reach readiness, retire a native/session slot, or commit a
successful discovery without delivering a stale caller result. A failed
rediscovery discards only its draft and preserves the previously committed GATT
database; it must not publish a replacement session as though discovery had
succeeded. Cache preservation does not imply native subscription survival (see
below). These rules apply to either terminal/domain arrival order and to
callbacks buffered before dispatch ack.

### Scan retirement review corrections

Scan retirement takes no raw event argument: it uses only correlated reservation
evidence, including `stop.stop_error`. The outer `retire_completed()` first uses
the pure `is_retirable()` decision to remove a completed session. Otherwise,
`retire_failed_stop()` discards only an accepted stop with a failed terminal so
the retained session can retry. It neither removes the session nor settles its
caller. No mutation is hidden in `is_some_and()`, and no boolean helper both
mutates a child record and decides whether its root should be removed. An unrelated `ScanStopped::Err` must not
retire the current scan or discard its pending stop correlation. A stopped scan
also retains incomplete start evidence until its domain/terminal facts can settle
the start waiter; settlement precedes retirement, including for late callbacks
and callbacks buffered before dispatch acknowledgement.

### Subscription state and explicit disposition

[`SubscriptionReservations`](../../../crates/bleng/src/runtime/reservations/subscriptions.rs)
owns one connection's collection. Its `validate_disable()` locates the route and
delegates session/activity and transition-busy checks to the single subscription;
connection and sibling-operation checks follow, preserving error precedence.
[`SubscriptionReservation`](../../../crates/bleng/src/runtime/reservations/subscription.rs)
retains session/characteristic identity and a private `phase: SubscriptionPhase`:

```rust
enum SubscriptionPhase {
    Enabling {
        mode: NotificationModeRequest,
        transition: NotificationTransition,
    },
    Active,
    Disabling(NotificationTransition),
}
```

It no longer combines `active: bool` with an optional transition.
`NotificationTransition` contains only `progress` and
`domain_evidence: Option<NotificationState>`, not a duplicated action. The transition type
and the phase-local `transition()` are private to subscription.rs.
`SubscriptionPhase` owns phase queries, progress access, disable installation,
state/direction matching, phase mutation and route retirement/rejection policy.
`NotificationTransition::record_enabled()` / `record_disabled()` own mode validation
and first-domain-evidence retention; `is_complete()` joins dispatch, terminal/domain
and cancellation barriers. The transition neither duplicates enable/disable direction
nor decides whether the subscription route is retained. The reservation uses
the existing `delegate!` macro for phase-only interfaces, without exposing the enum.
Command ownership, transition presence and unresolved dispatch are phase queries;
outer event correlation no longer extracts a raw transition or reads its progress.
Connection-loss correlation remains connection-scoped rather than adopting the
command-ownership checks used for notification-state and terminal callbacks. The
actor's foreign-disconnect protection is unchanged.
The reservation still validates command/connection/session/subscription/characteristic
identity before passing an observed `NotificationState` to `phase.record_state()`
for dispatch to the matching transition recorder;
`NotificationTransition::result_for_cmd()` selects a deliverable state result by
checking command ownership, accepted terminal evidence and terminal-error precedence
over domain evidence. The phase's `transition_result()` selects the current transition.
The reservation only maps a successful `NotificationState` into a typed waiter result
with subscription identities; it does not repeat command-evidence or retirement checks.
The collection uses each route's `progress()` /
`progress_mut()` for shared command accounting and
`waiter_result(command_id, connection_id, subscription_id)` for typed results,
without reading transition fields or assembling result identities.
`is_active()` includes `Active` and
`Disabling`; `is_transitioning()` includes enable and disable. Keeping a route
while disabling does not promise continued remote value delivery.

```text
Enabling -- accepted + terminal success + matching Enabled --> Active (Keep)
    +-- dispatch rejection / accepted terminal failure --> Remove
Active -- begin_disable --> Disabling
Disabling -- accepted + terminal success + matching Disabled --> Remove
    +-- dispatch rejection / accepted terminal failure --> Active (Keep)
Incomplete evidence, unresolved dispatch ack or cancel result --> keep state (retirement None)
```

`retire_transition()` returns `Option<SubscriptionTransitionRetirement>`.
`None` means no transition or incomplete evidence; `Some` explicitly carries the
completed `command_id` and an independent `disposition: SubscriptionDisposition`.
`SubscriptionReservations::retire_completed()` collects that command ID and
applies `Keep` / `Remove`, without inferring command retirement from before/after
progress queries. `reject_transition()` still returns only `Keep` / `Remove`,
since the dispatch caller already knows the rejected command ID. The collection
immediately removes a route when requested; there is no Removed tombstone state.
On dispatch rejection, `SubscriptionReservation::resolve_dispatch()` first drains
buffered evidence, then returns the resolution and disposition for immediate
application by the collection.
Failed enable removes its provisional route;
failed disable restores `Active`. Successful transitions require their matching
domain state; terminal failure needs no domain but retirement still waits for
acknowledgement and any pending cancellation result. Strict mode is checked on every correlated Enabled callback,
even after the first domain evidence, so duplicates cannot hide a contradictory
mode. Actual mode is extracted from transition evidence, not duplicated on the
active route. This is a local subscription-state enum, not a generic BLE operation
state machine.

`begin_disable()` installs a transition only from stable `Active`, after the caller
has completed admission and submission claim. An internal assertion prevents
silently overwriting an unfinished enable/disable progress; normal Busy errors
remain owned by admission. Failed disable restores local Active route state, not
proof of native rollback or resumed value continuity.

### GATT commit and regression coverage

`ConnectionReservation.gatt` is a concrete `GattReservation`, not an
`Option<GattReservation>`. Its `database: Option<GattDatabase>` and
`discovery: Option<GattDiscoveryReservation>` are independent: both empty means
no committed database or pending discovery; both may coexist during rediscovery.
They must not become mutually exclusive enum variants. The committed database
field is private: connection calls
`require_characteristic(session_id, characteristic_id)`, and the GATT owner looks
up the session and characteristic internally. Missing database, mismatched session,
or absent characteristic still yields `GattNotReady`, before later I/O/subscription
conflict and discovery-busy checks. Only the GATT owner commits replacements.
The draft stays inside the discovery record.

```text
ConnectionReservation.gatt: GattReservation
  +-- database: None / Some(committed database)
  +-- discovery: None / Some(progress + session_id + draft_database)
       full success -> settle waiter -> replace database -> clear discovery
       rejection/failure -> clear discovery only -> preserve database
```

There is no outer `Option`, lazy `get_or_insert_with`, or normalization of empty
inner slots back to an absent GATT owner. `IoReservations::resolve_dispatch()`
locates RSSI/read/write commands in its existing maps. Acceptance returns buffered
events for replay; rejection classifies and drains them before removing only the
targeted entry. The connection supplies `classify_rejected_child_event()`, which
permits only passive connection loss replay; the collection does not call back
into a private connection implementation. For discovery,
`GattReservation::resolve_dispatch()` shares that classifier while owning its
private slot; `DisconnectReservation::resolve_dispatch()` handles its evidence
and connection removes its slot on rejection. Ordinary I/O, GATT, and disconnect
rejection permit only passive loss. Subscription dispatch resolution passes
through the collection to the route, whose rejection policy also permits
commanded connection loss and restores an active route after rejected disable. After waiter settlement,
`retire_completed_discovery()` retires completed discovery and replaces the
committed database only on fully accounted success. It returns no bool and does
not clear subscriptions: discovery admission requires no routes, pending discovery
blocks enable, and native callbacks cannot create routes. Dispatch rejection or
terminal failure still removes only
the provisional discovery, preserving the old session and database. This is
rejection/failure removal, not prelaunch rollback. Cancellation before handoff
creates no provisional discovery to remove. Terminal recording retains the
first result: a conflicting duplicate terminal cannot overwrite it while
discovery waits for its database.

`BeginDiscoverGatt` returns `GattBusy` before dispatch when the target connection
has any active subscription or pending subscription transition (enable or
disable). Callers must explicitly disable subscriptions and let disable complete
before rediscovery; a timeout or waiter cancellation is not completed disable.
This is an intentional BLeng admission behavior correction, not merely a file
merge, and changes no public ABI shape.

The reviewed plan separates [failed-discovery cache preservation (§8.5)](../../../.plan/PLAN.md#85-gatt-discovery)
from [subscription lifecycle (§8.8)](../../../.plan/PLAN.md#88-开启-notification).
Apple can [queue native unsubscription before discovery](../../../platforms/apple/Sources/BleHubApple/AppleBleBackend.swift#L287-L301)
and [retire those routes](../../../platforms/apple/Sources/BleHubApple/GattIoController.swift#L340-L390)
without a subscription-invalidation event; discovery may then fail while the old
GATT database remains committed. Keeping an active BLeng route in that case would
incorrectly imply subscription survival. [Android discovery admission](../../../platforms/android/library/src/main/kotlin/dev/blehub/android/AndroidBleBackend.kt#L310-L312)
already requires disabling subscriptions before rediscovery. The BLeng guard
avoids relying on implicit backend unsubscription or changing the thin contract.

The [GATT regression coverage](../../../crates/bleng/src/runtime/tests/gatt.rs)
retains the following behavior checks:

- rejection or failure of rediscovery preserves the committed database for reads;
- completed disable permits rediscovery, but a new draft alone cannot permit
  enable; only full discovery completion admits a subscription on the new session,
  without reviving the old session or subscription;
- conflicting duplicate terminals cannot replace the first success while waiting
  for the database.

Real cancellation-path regressions replace the former prelaunch RAII tests:

- `prelaunch_rediscovery_cancellation_preserves_committed_database_for_reads`
  verifies cancellation winning before dispatch creates no reservation and
  leaves the committed database/session readable.
- `connection_leaf_prelaunch_cancellations_preserve_siblings_database_and_active_subscription`
  verifies cancellation of RSSI, characteristic I/O, disconnect, and subscription
  transitions preserves sibling pending operations, the committed database, and
  active subscriptions.
- `cancelling_begin_after_launch_handoff_before_reply_faults_and_disposes_once`
  exercises the winning-dispatch side through the actual `launch_dispatch`
  helper. A bounded synchronous gate inside the reserve closure holds the begin
  reply after handoff; dropping the caller then asserts `Faulted`, completed
  close, and exactly one disposal. It deliberately does not assert native entry:
  cleanup races native dispatch.

The owner regression
[`cleanup_before_launch_finishes_registered_waiter_without_reserving_or_false_faulting`](../../../crates/bleng/src/runtime/tests/owner.rs)
covers cleanup with an open phase, explicit close, and fault between waiter
registration and launch. Each case checks the matching waiter error, skipped
reservation/dispatch, no false guard fault, and exactly-once disposal.

The additional scan regressions in
[`runtime/tests/scan.rs`](../../../crates/bleng/src/runtime/tests/scan.rs) cover
`foreign_scan_error_cannot_retire_suspended_stop_awaiting_terminal` and
`native_stop_retains_live_start_until_domain_and_terminal_settle`.
The notification tests in
[`runtime/tests/notifications.rs`](../../../crates/bleng/src/runtime/tests/notifications.rs)
cover active-subscription rejection before rediscovery, failed disable, and
`notification_rediscovery_waits_for_late_disable_without_blocking_other_devices`:
partial late completion stays busy; full disable permits rediscovery; other
connections remain independent.

The previously recorded locked workspace run passed all 222 tests, including
all 134 BLeng tests. Workspace strict Clippy, formatting, BLeng Swift/Kotlin
binding snapshots, and documentation checks also passed at that point; those
historical gates are not relabeled as current-candidate reruns.

The earlier actor/lifecycle simplification record reported 115 passing runtime
actor tests, including both new regressions in
[`runtime/tests/lifecycle.rs`](../../../crates/bleng/src/runtime/tests/lifecycle.rs):
`waiter_deadline_retirement_mailbox_overflow_reports_transport_fault_in_both_paths`
and `close_waits_for_actor_teardown_after_mailbox_closes`. The former protects both
deadline paths; the latter blocks Kameo unregister after mailbox closure and
checks close still waits even when cleanup has completed. This documentation
update claims neither fresh platform RF evidence nor ABI freeze.

The API regression recorded in that earlier simplification
[`dropping_facade_waiter_aborts_private_task_and_releases_its_capture`](../../../crates/bleng/src/tests/facade_tests.rs)
is also reported passing. It observes private-task entry, drops the outer facade
future, and waits within a bounded test window for capture release; it does not
assert immediate destruction or native cancellation.

The 2026-10-07 current uncommitted reservations worktree passed:

- `pixi run rtk cargo test --locked --offline -p bleng -- --test-threads=1`:
  156 tests.
- `pixi run rtk cargo test --locked --offline --workspace -- --test-threads=1`:
  244 tests across 18 suites.
- `pixi run just fmt` and `pixi run just lint`, including strict workspace Clippy.
- `pixi run just binding bleng-snapshot-check`: all 3 reviewed Swift/Kotlin snapshots.
- `pixi run just docs-check-mermaid`: 21 diagrams across 112 Markdown files.
- UTF-8, fenced-block balance, trailing-whitespace, conflict-marker checks,
  and `git diff --check`.

These are local deterministic results, not RF, cross-platform runtime, or ABI
freeze evidence. No dependency was added: independent callback evidence is not a
linear state machine or lexical RAII lifetime, and standard enums, iterators,
and ownership cover the reuse introduced here.

## Responsibilities of the main structures

### `WaiterExpectation`

`WaiterExpectation` states the operation-specific evidence required for
success: initialization, scan start/stop, connection/disconnection, RSSI,
GATT discovery, characteristic I/O, or notification transitions.

It documents the caller's expected typed result and identifies connection-bound
waiters for passive-loss settlement. The actor reservations enforce callback
correlation: a valid-looking command ID alone is insufficient; connection, GATT
session, characteristic, subscription, and generation identity must also match.

### Reservation evidence

Each actor-owned reservation is the authoritative evidence record for its
operation. Common `CommandProgress` records the command ID, native acceptance,
callbacks that escaped before dispatch acknowledgement, and the private first
terminal result, including its native error rather than only a success boolean.
Each operation-specific reservation retains its own typed domain evidence and
interprets it with the shared progress gate; no separate terminal field remains.

For the distinction between scan session facts, explicit stop command evidence,
and caller waiter lifetime, see the [scan reservation design](bleng-scan-reservations-design-2026-10-09.md)
([中文](bleng-scan-reservations-design-2026-10-09.zh-CN.md)).

The result decision now lives beside the state it interprets:

- `InitializationState::waiter_result()` requires a snapshot and successful
  terminal result;
- `ScanReservation` and its optional `StopReservation` preserve start/stop ordering
  and scan-stop errors; the child reserves the session's explicit stop command
  slot and retains its evidence independently of the caller's waiter;
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

### Initialization state and late completion

The actor has one opaque `InitializationState`, rather than an independent
readiness boolean and optional initialization reservation. Its private phase follows:

```text
NotStarted -- successful handoff + reserve --> Pending(InitReservation) -> Ready
     |                                            |
     +-- cancellation before handoff:             +-- timeout/cancel after owner begin ack:
         stay NotStarted; remove only waiter      |   stay Pending; retire only waiter
                                                  +-- cancel before owner begin ack: fault runtime
                                                  +-- accepted terminal failure: fault runtime
                                                  +-- dispatch rejection: Rejected { command_id }
                                                      clean: retire Hub; conflicting evidence: fault
```

Ready requires accepted dispatch, a command-correlated snapshot, and a successful
terminal. The actor settles the waiter before replacing Pending with Ready and
releasing completed evidence. After owner begin acknowledgement, late success
can make the actor ready even after the waiter timed out or was cancelled. Duplicate initialization callbacks after
success still appear on the event stream but cannot change readiness or permit
a second initialization. Readiness is not adapter power state or runtime phase.

Ready retains no adapter snapshot. `Bleng::initialize()` returns the first correlated
initialization snapshot, not a fresh query at the instant the async call returns.
Later platform observations are published as `AdapterSnapshotChanged` through
`Bleng::next_event()` without undoing readiness. There is no current snapshot getter
or refresh command; repeated initialization is rejected. Callers should maintain
last-observed adapter state from a single FIFO reader and share that projection with
other consumers. The FIFO neither broadcasts nor replays last-known state to late
readers. Once later events have been applied, an older initialize return must not
replace them. Late initialization evidence remains observable through the event
stream after caller timeout or cancellation. A future cached query or native refresh
would need separately reviewed ownership and freshness semantics, not extra state in
the initialization reservation.

Dispatch rejection releases the pending record and enters `Rejected { command_id }`.
Only this one correlation ID is retained, not snapshot/terminal evidence or a pending
progress object. Clean rejection still starts Hub cleanup without faulting the runtime;
it cannot retry initialization. A later matching snapshot or terminal contradicts the
rejection and faults before public publication, rather than entering a pre-dispatch
buffer indefinitely. Uncorrelated events continue through the ordinary event stream
without changing initialization state. Duplicate dispatch results cannot revive or
re-settle the rejected operation. `reserve_initialize()` asserts `NotStarted` so an
internal admission violation cannot overwrite existing initialization state.
Regression coverage lives in the runtime initialization and reconciliation tests;
the actor test uses the real runtime sink to model a callback already escaped from
the Hub's closing ingress gate.

After waiter settlement, the actor calls only
`advance_after_settlement() -> Result<(), WaiterError>`, rather than separately querying
`has_failed()` and then completing. Missing dispatch acceptance or terminal keeps
Pending; terminal failure returns the original operation error for actor faulting;
terminal success with a snapshot enters Ready. This check does not clone a success
snapshot. `record_event()` directly matches a correlated snapshot instead of first
matching a broader snapshot-or-terminal category. Private `pending()` / `pending_mut()`
helpers remain shared by multiple owner methods; no raw-record getter is exposed.
The registration assertion reuses `require_not_started()` without a separate
`has_started()` helper. Private `pending_for()` / `pending_for_mut()` centralize
command-ID selection for initialization, and `stop_for()` / `stop_for_mut()` do the
same for scan stop. Single-use connection-slot selection stays inline rather than
adding a getter for every slot. Test fixtures build readiness through the ordinary evidence
transitions, not a test-only production constructor or wider phase visibility.

### `WaiterEntry`

`WaiterEntry` is deliberately small. It stores only the caller expectation and
its oneshot result_tx. It does not copy dispatch state, GATT databases,
characteristic values, terminal errors, or notification state. After owner begin
acknowledgement, caller timeout/cancellation removes this entry while the
reservation remains alive until native state is safe to retire.

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

`settle_waiter()` first checks `WaiterRegistry::contains(command_id)` before
calling reservation `waiter_result()`. A retired caller therefore does not cause
unused results (such as cloned databases or read bytes) to be materialized.
This check skips only caller settlement: late evidence recording and subsequent
reservation retirement/commit still proceed in `process_event()`.

`finish()` is the only exactly-once settlement path. The registry is accessed
through the actor rather than a shared concurrent map, so it does not need a
`Mutex`, `DashMap`, or a second routing registry. It stays on `BlengActor`, not
shared `RuntimeLifecycle`: moving it there would require synchronization or an
actor round trip without changing who must reconcile operation evidence.
Lifecycle notifications wake waiting callers immediately; actor teardown closes
the registry and releases its remaining senders.

### `WaiterTicket`

`WaiterTicket` is the caller-side waiting handle. It waits on three things:

- the operation's oneshot result;
- the operation deadline; and
- the owner ingress lifecycle signal.

`wait_until(deadline)` accepts the absolute deadline shared with begin admission.
When the caller times out or drops the ticket, it attempts a non-blocking
`RetireWaiter` send. Failure on a full/closed mailbox while the lifecycle is Open
faults the runtime rather than silently losing retirement. Both an already-expired
deadline and the timer branch call `finish_timeout()`: retire, disarm the Drop
fallback, then recheck `require_open()`. Only a still-open gate returns `Timeout`;
a retirement-send fault consistently returns `TransportFault`, and a concurrent
close returns `Closed`. Retiring a waiter means that the caller no longer
receives a result. It does **not** claim that an already-dispatched native BLE
operation was cancelled. Native state and late callbacks remain governed by
the backend and actor correlation rules.

`WaiterTicket` retains this domain-specific retirement responsibility and its
Drop fallback; it is not replaced with a generic scope guard. A generic guard
alone cannot express command identity, deadline/gate precedence, or failed
mailbox-retirement handling.

The ticket also detects a closed or faulted ingress before returning an ordinary
operation result. This prevents a result racing with owner shutdown from being
reported as a successful operation after the runtime has already failed closed.

### `WaiterSuccess` and `WaiterError`

These are internal orchestration results. They preserve typed data such as
`GattDatabase`, `ConnectionId`, RSSI, characteristic values, subscription IDs,
and notification mode. `WaiterSuccess` remains a data enum: the Begin message's
`OperationResult` implementation validates and extracts a successful typed return
value, while the BLeng API layer maps errors to the public boundary.

`WaiterError` deliberately includes both local orchestration outcomes and
canonical BLE/dispatch errors. The conversion in `error.rs` is the public
boundary; the waiter must not duplicate the canonical `blehub` error taxonomy.

## Timeout, cancellation, and late events

The operation deadline covers both phases:

```text
run_operation(timeout, make_message): compute one absolute deadline after Hub preflight
├── synchronous make_message factory -> snapshot correlation IDs
├── timeout_at(deadline, actor.ask(message)): admission / dispatch handoff
└── WaiterTicket::wait_until(deadline) -> decode_success: no relative-timeout reset
```

Before dispatch handoff, a timeout or caller cancellation that wins `try_claim`
removes only the waiter: no reservation or native command has been created.
If handoff wins but the owner has not acknowledged the begin reply,
the handoff scope guard faults the owner and starts cleanup; it does not roll back
the reservation or promise native cancellation. After owner begin
acknowledgement, timeout/cancellation is caller-only waiter retirement: it retains
reservation evidence and does not forcibly stop native work. CoreBluetooth,
Android BluetoothGatt, Windows, and other native transports do not provide one
universal synchronous cancellation contract for every operation.

After retirement of a waiter for an already-dispatched operation:

```text
late callback
    -> actor correlation and reservation evidence
    -> contains check skips result materialization for the retired waiter
    -> retire/commit when native evidence permits
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
BlengActor event_tx  -> publishes to the shared bounded event FIFO
RuntimeOwner event_rx -> receives that FIFO for next_event()
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
- `tokio::time` for deadlines;
- `scopeguard` for Begin handoff RAII;
- `tokio_util::task::AbortOnDropHandle` for the local facade task lifetime;
- `HashMap<CommandId, WaiterEntry>` for stable caller command identity; and
- concrete actor reservations for operation evidence and native retirement.

The focused reuse boundary is:

| Mechanism | Reuse or retained domain responsibility |
|---|---|
| Shared shutdown result | `watch::Receiver::wait_for(Option::is_some)` inspects only the channel value; clone the result. |
| Begin handoff RAII | `scopeguard::guard` / `ScopeGuard::into_inner` replace the custom guard, not atomic `BeginHandoff` arbitration or claimed-abandonment fault policy. |
| Facade task lifetime | [`facade/mod.rs`](../../../crates/bleng/src/facade/mod.rs) awaits `AbortOnDropHandle::new(private_handle.spawn(future))`, replacing local `AbortOnDrop`; outer-future release requests local task abort, not native cancellation or immediate destruction. |
| Waiter retirement | Retain `WaiterTicket` for command identity, deadlines, `RetireWaiter`, send-failure fault precedence, and Drop fallback. |
| Cleanup completion | Retain subscribe/check Mutex ledger/unlock/`changed().await`; signals are wakeups, and a ledger-locking `wait_for` predicate would invert producer lock order. |
| Cleanup execution and phases | Retain executor-independent OS workers for late cleanup after runtime shutdown and error-bearing phases/results, not a boolean cancellation token. |

The root [workspace dependencies](../../../Cargo.toml) pin `scopeguard = "=1.2.0"`
with `default-features = false`, and `tokio-util = "=0.7.19"` with
`default-features = false` and only `features = ["rt"]`; [BLeng](../../../crates/bleng/Cargo.toml)
uses those workspace dependencies. [Cargo.lock](../../../Cargo.lock) adds exactly
these two packages; all other required packages already existed. Resolution was
offline. This reuse adds no network access, extra runtime, thread, or ABI change;
existing independent cleanup workers remain.

Local cached source was audited: `scopeguard-1.2.0/src/lib.rs` (`guard`, `Drop`,
`ScopeGuard::into_inner`), `tokio-util-0.7.19/src/task/abort_on_drop.rs` (`Future`
forwarding to the JoinHandle and `Drop` calling abort), and
`tokio-1.53.1/src/sync/watch.rs` (`wait_for` executes its predicate under the watch
read lock). These source facts support the reuse and the explicit
[cleanup lock-order constraint](bleng-cleanup-design-2026-10-07.md#6-notifications-completion-and-retained-results);
they do not establish immediate task destruction or stronger native semantics.

No additional actor, promise, cancellation, slot-map, or concurrent-map crate
would replace the BLE-specific correlation policy. In particular,
`CancellationToken` could signal caller interest being withdrawn, but it could
not remove a `CommandId` from the registry, validate event identity, preserve
dispatch-rejection precedence, or cancel native BLE work. Nor can it replace
error-bearing lifecycle phases (`Closed` versus `Faulted`) or retained cleanup
results.

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
4. cancellation before handoff skips reservation, cancellation after handoff
   before owner begin acknowledgement faults the owner, and timeout/cancellation
   after that acknowledgement retires only the waiter, never promising native
   cancellation; and
5. late callbacks cannot resolve a retired command into a stale caller result.

### Cleanup and event-delivery boundaries

`RuntimeOwner::new()` uses Kameo `PreparedActor` and retains its spawn
`JoinHandle` in one inline shutdown task in the `runtime.handle().spawn` block,
replacing the first-close atomic/two-watcher model. That block clones `actor`,
`lifecycle`, and `shutdown_result_tx` without retaining `RuntimeOwner`.
The coordinator selects actual actor-task completion versus lifecycle leaving
Open; the lifecycle branch starts cleanup, calls `stop_gracefully()`, then joins
the task. A successful task returns the actor by value, which is explicitly
dropped before cleanup waiting and shared-result publication. Unexpected task
completion while Open is also consumed/dropped, faulting lifecycle through actor
Drop. Mailbox closure / `ActorRef::wait_for_shutdown()` alone is not teardown.
Task-join or Kameo lifecycle errors map to `CleanupError::WorkerStopped` only
after cleanup waiting; successful task teardown returns cleanup's result, so a
runtime fault does not itself imply disposal failure. `close()` subscribes to the
shared result, closes the gate, starts cleanup, and awaits
`shutdown_result_rx.wait_for(Option::is_some)`, then clones the retained result;
channel closure errors map to `CleanupError::WorkerStopped`. The predicate needs
no external ledger lock. Cancelling one close does not cancel the coordinator. Owner Drop remains non-awaiting kill plus
`shutdown_background()`. See the [cleanup shutdown protocol](bleng-cleanup-design-2026-10-07.md#logical-shutdown-and-cleanup-integration)
for the locked local Kameo evidence and independent cleanup-thread boundary.

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
  -> retire only fully accounted native state / commit successful discovery
```

A passive disconnect has no command ID and therefore cannot directly settle an
explicit disconnect waiter. It can, however, provide the disconnect domain
evidence; the explicit command terminal remains authoritative, regardless of
which of those two events arrives first.
