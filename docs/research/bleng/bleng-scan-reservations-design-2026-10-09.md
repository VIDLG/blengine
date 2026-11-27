# BLeng scan reservations: session facts and stop command evidence

Date: 2026-10-09

[中文版本](bleng-scan-reservations-design-2026-10-09.zh-CN.md)

## Scope and evidence

This note explains the current internal design, not a new public contract or a native-platform guarantee. It is verified against [scan reservations](../../../crates/bleng/src/runtime/reservations/scan.rs), [command progress](../../../crates/bleng/src/runtime/reservations/dispatch.rs), and [scan regression tests](../../../crates/bleng/src/runtime/tests/scan.rs). See the broader [actor/reservation design](bleng-actor-reservations-design-2026-10-07.zh-CN.md) and [waiter design](bleng-waiter-design-2026-10-06.md) for runtime coordination.

The central distinction is:

> A scan session ending is not the same as an explicit stop command successfully completing. Retiring the caller's waiter is neither of those events.

## Ownership, not three competing state machines

```mermaid
flowchart TD
    A[ScanState: single scan slot] --> B[ScanReservation: retained session]
    B --> C[Start command progress]
    B --> D[Start evidence and session-end evidence]
    B --> E[Optional StopReservation: explicit stop slot]
    E --> F[Stop command progress and native stop error]
```

- `ScanState` privately represents `Idle` or `Reserved`. `Reserved` includes starting, running, stopping, and stopped-but-unsettled sessions; it does not mean native scanning is necessarily active. Its gate selects the retained session; `ScanReservation::gate_event()` owns start-before-stop buffer routing.
- `ScanReservation` owns the whole session, not just start. It retains session identity, the initiating command's `start_progress` and `started_evidence`, and independent `session_end_evidence`. That end fact can exist with no stop child, or while late start evidence is still missing.
- `StopReservation` occupies the session's single explicit stop command slot. It exists only after an admitted stop operation claims the caller handoff; it has its own command ID and progress, but no independent scan session.
- The actor owns caller waiter delivery separately. A reservation does not own its caller's future.

Nesting expresses ownership. The name `Reservation` expresses slot occupancy and retained correlation/evidence, not a requirement that the object exist independently. This is consistent with connection-owned `DisconnectReservation`, `RssiReservation`, and characteristic I/O reservations.

## Why `session_end_evidence` and `stop` are separate

Both represent evidence, at different levels:

| Member | Meaning | Owner |
|---|---|---|
| `started_evidence` | The matching `ScanStarted` was observed. | Start command, retained by the scan session |
| `session_end_evidence` | Valid native session-end evidence was observed. | Scan session |
| `stop: Option<StopReservation>` | An explicit stop command's slot and evidence are retained. | Scan session, through a child reservation |

The four `session_end_evidence`/`stop` combinations are meaningful:

| `session_end_evidence` | `stop` | Meaning |
|---|---|---|
| `false` | `None` | No observed session end and no explicit stop command. Start may still be incomplete. |
| `false` | `Some` | Stop is reserved, but valid session-end evidence has not arrived. |
| `true` | `None` | Session end was observed without a retained explicit stop command. Start evidence may still need settlement. |
| `true` | `Some` | Session end was observed and stop command evidence remains retained, for example while its terminal is missing. |

`started_evidence` and `session_end_evidence` are independent observed facts, not exclusive phases. Session-end evidence can be processed before the start's matching domain event or terminal. A late `ScanStarted` does not mean the ended session restarted.

This is why scan does not have a `start_error` parallel to connection's command-correlated domain error. `ScanStopped::Err` carries no command ID and can describe passive teardown around a start; assigning it to start would manufacture an error relationship. When an explicit stop reservation exists, the child retains the error for that stop waiter. Otherwise the parent records only session termination.

Moving `session_end_evidence` into the optional child would make passive teardown unrepresentable when no explicit stop exists. Adding another wrapper around both would not remove any independent fact or decision. The existing parent/child composition already groups them under one owner.

## Recording responsibilities

`ScanReservation::record_event()` first correlates the event with the session. For a matching `ScanStopped`:

1. With a stop child, call `StopReservation::try_record_stop_result()`. It retains native errors and validates Requested command ownership plus dispatch acceptance. Its boolean reports valid session-end evidence, not command success; native failure also returns true.
2. Without a stop child, only native scan error or `SystemSuspended` establishes session end. A Requested notification has no retained stop command to confirm it.
3. The parent latches the returned evidence into `session_end_evidence`; a later unrelated notification cannot undo an already observed session end. Successful observations do not erase the child's retained native error.

The child receives a typed `ScanStopResult`; it does not repeat the parent's session check. It owns its command-specific error and correlation policy. The parent owns the session-end fact. Neither path resets `session_end_evidence` to `false` within a retained session.

Native scan error proves teardown; a failed stop command terminal or rejected stop dispatch alone does not.

## Success, failure, and retirement are different decisions

### Success

Start success requires accepted dispatch, a successful terminal, and matching `ScanStarted` evidence.

Explicit stop success requires accepted dispatch, a successful terminal, valid session-end evidence, and no recorded native stop error. Session end may be a matching requested stop or system suspension; it need not prove the stop command caused the end.

`CommandProgress::owns_accepted_command()` checks only command ownership and dispatch acceptance; it does not interpret scan events, prove terminal success, or settle a waiter. `StopReservation` applies that check to Requested events after the parent has matched the session; the parent retains the session-end fact. The parent's `start_failed()` refers specifically to the start command's terminal failure, not stop failure.

### One stop completion policy

`StopReservation::is_complete(session_end_evidence)` is the shared, allocation-free readiness check used by waiter result construction and session retirement. It does not inspect whether a caller still has a waiter, and does not mutate or retire state.

| Evidence | Complete? |
|---|---|
| Dispatch unresolved | No, regardless of other evidence. |
| Dispatch accepted, native stop error recorded | Yes, failure; terminal is not required. |
| Dispatch accepted, failed terminal | Yes, failure; session end is not required. |
| Dispatch accepted, successful terminal and session end | Yes, success unless a native stop error takes precedence. |
| Other combinations | No. |

`ScanReservation::is_retirable()` checks complete start evidence first, then complete stop evidence if a child exists, then whether start failed or the session ended. It no longer interprets the child's terminal/error fields itself. Completed stop evidence without session end can fail the stop waiter but cannot retire the scan session.

### Failure and retirement

- No waiter result is deliverable while the relevant dispatch result is unresolved.
- After stop dispatch acceptance, a native stop error fails the stop waiter even if its terminal is absent. It also proves teardown, allowing session retirement after waiter settlement without waiting for that terminal.
- An accepted stop's failed terminal, without session end, fails the waiter and releases only the stop slot. `CommandProgress::has_accepted_terminal_failure()` owns the shared dispatch-and-terminal check, not retirement policy; the parent removes the child only after waiter settlement and considering whole-session retirement. The scan session remains available for another stop attempt.
- System suspension without an explicit stop can retire the session once start evidence is complete. If start domain/terminal evidence is still missing, retain the session until the start waiter can be settled.
- Waiter timeout or cancellation alone does not release native reservations or promise native cancellation. Late evidence still updates state and can permit retirement.

The actor settles waiters before `ScanState::retire_completed()` removes either the session or a failed stop child. A successful start releases its initiating waiter, not the ongoing scan slot.

## Early callbacks and dispatch rejection

Callbacks can reach the actor before the native dispatch result is processed. This is not a promise of any particular native callback order.

- `should_buffer_start_event()` combines unresolved start dispatch with correlation of command callbacks and same-session observations, including advertisements and native stop. It decides whether buffering is needed now, not whether start succeeded.
- `StopReservation::should_buffer_event()` likewise combines unresolved stop dispatch with related terminals and same-session stop observations. `StopReservation::gate_event()` owns both this decision and buffering. The parent checks the start gate first, then delegates to the stop child without accessing its buffer.
- Accepted dispatch transfers buffered events for ordered actor replay.
- Rejected start releases the slot; any correlated buffered callback is contradictory evidence and requests a runtime fault.
- `StopReservation::resolve_dispatch()` owns the stop rejection classification: buffered system suspension or native scan error can replay as independent teardown; the rejected command's terminal is discarded. Buffered requested-stop evidence conflicts with rejection only when its command ID belongs to the rejected stop. A requested-stop observation from another command remains publicly replayable, but does not establish session end for the current stop. `ScanReservation::resolve_dispatch()` routes both start and stop results and releases rejected stop children; `ScanState` only applies the returned root-slot disposition.

`ScanDispatchResolution` pairs the classified work with `KeepSession` or `ReleaseSession`. Only a matched start rejection releases the root at this boundary; stop rejection retains the session. Unknown command IDs return `None`. The state owner does not inspect start/stop progress or classify callbacks to infer a phase change. Session queries (`stop_command()`, `completion_observer()`, and `command_ids()`) also belong to `ScanReservation`, not the slot owner.

The actor settles `DispatchRejected` before replay. A passive end must not convert the rejected stop into a success.

## Examples

| Observation sequence | Consequence |
|---|---|
| Running scan, no stop command, then system suspension | Set `session_end_evidence`; no stop child is invented. Retire after required start settlement. |
| Accepted stop, requested end, then successful terminal | Record session end, wait for terminal, settle success, then retire. |
| Accepted stop, native scan error, terminal absent | Record session end and child error; settle failure and retire without that terminal. |
| Accepted stop, failed terminal, no session end | Settle failure, discard stop child, retain session for retry. |
| Stop waiter times out, then valid completion evidence arrives | Caller remains timed out; retained native state is reconciled and retired when safe. |

## Review and regression guidance

`ScanReservation::progress_for_cmd()` and `progress_for_cmd_mut()` centralize start/stop command lookup for cancellation control and terminal recording. Lookup does not grant cancellation permission: start uses its command-progress policy; the stop child's `request_cancellation()` owns the decision that an installed stop is non-cancellable. Submission cancellation before installation remains a separate caller/actor protocol. The session also owns publication-scope checks and deferred-event release; `ScanState` locates the retained session and controls slot admission and phase transitions.

Review `gate_event()` and `resolve_dispatch()` before `record_event()`, then `waiter_result()`, `is_retirable()`, and `retire_failed_stop()`. Buffer correlation, success evidence, caller delivery, and native retirement answer different questions.

The scan tests cover start evidence arriving after native end, stop domain/terminal order, native errors, failed/rejected stops, passive end during rejection, foreign requested-stop evidence during a rejected retry, stale session evidence, and installation admission. [Reconciliation tests](../../../crates/bleng/src/runtime/tests/reconciliation.rs) also cover buffering and rejected-event replay policy.

Do not simplify this design by making `session_end_evidence` equivalent to `stop.is_some()`, treating every terminal failure as native teardown, or tying reservation lifetime to a live waiter.
