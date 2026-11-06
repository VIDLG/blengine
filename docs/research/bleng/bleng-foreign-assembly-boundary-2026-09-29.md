# BLeng foreign assembly boundary: evidence and unresolved ownership

> Historical assembly/foreign-probe evidence, not a current source-layout or ABI guide.
> Crate-private candidate names and test paths below belong to the recorded candidate.
> The maintained ownership model is in the [actor/operation design](bleng-actor-reservations-design-2026-10-07.zh-CN.md)
> and [approved Plan](../../../.plan/PLAN.md); current verification boundaries are in the
> [functional audit](../../testing/reports/2026-10-25-core-functional-convergence.md).
> Original findings and failed attempts remain preserved here.

## Verified on the current unfrozen Rust candidate and isolated Mac probe

- `crates/bleng/src/runtime/tests/assembly.rs` uses the existing exported
  `blehub::attach_backend(ticket, backend)` path with a Rust fake backend.
  BLeng's crate-private `BackendPort::assemble_backend` now keeps
  the `BackendAssembly` ticket inside one synchronous, owner-admitted call:
  admission, foreign attach, `finish`, and Hub registration share one cleanup
  owner. The resulting Hub permits real actor `initialize` and `start_scan`,
  then closes exactly once; a captured late ingress returns `Closed`.
  Invalid contract version rolls back without attaching a half Hub.
  Deterministic blocked-backend tests verify duplicate admission rejection,
  close/fault vs in-flight attach, post-close rejection, exactly-once disposal,
  and close waiting for failed-assembly rollback. Regression tests also pin
  the phase-close-before-cleanup-start window and the result-slot order when a
  rejected Hub precedes the admitted primary Hub. Cancelling a close waiter
  does not cancel admitted assembly or cleanup. This is **Rust-only** evidence,
  not a Swift or Kotlin foreign callback test.
- `crates/bleng/src/lib.rs` now has a crate-private shared `Bleng` candidate.
  `RuntimeOwner::close(&self)` and `next_event(&self)` no longer require an
  exclusive foreign-owned mutable borrow; event readers serialize only with
  each other via an async receiver mutex, never with close. A caller can
  await owner-side assembly without blocking an actor/UI thread: the foreign
  synchronous attach runs on the owned runtime's blocking lane, and the
  admitted job continues to be tracked if its waiter is cancelled.
  `crates/bleng/src/tests/owner_tests.rs` verifies real fake-backend
  initialization and scan through one shared owner, concurrent close while
  initialization and event reading are pending, repeatable close, and exactly
  one backend dispose. Shutdown now runs as a single task on the owned
  runtime; cancelling one close waiter cannot cancel actor stop or cleanup.
  A blocked-dispose test observes that shared shutdown result without calling
  close a second time. Cancelling an assembly waiter likewise does not
  cancel a foreign attach that has already entered its blocking lane; a
  blocked-attach test verifies duplicate rejection, close waiting and one
  dispose after waiter cancellation. The final assembly outcome can still
  depend on close timing; this is not a native cancellation guarantee. These
  unit tests still run in Rust, not against an exported object.
- The isolated, non-workspace
  [`spikes/archive/bleng-uniffi-async/`](../../../spikes/archive/bleng-uniffi-async/README.md)
  now links three components into one **experimental** Mac image: existing
  `blehub`, an opt-in historical `crates/bleng/src/foreign.rs` `foreign-probe` shell over
  the real BLeng actor, and a toy async component. Bindgen generated Swift and
  Kotlin for all three. Generated Kotlin bindings compiled with the Pixi JVM
  Kotlin and JNA 5.18.1; a macOS ARM64 JVM harness ran against the same
  experimental `.dylib`. A macOS Swift executable linked one static archive
  and called the real foreign BLeng shell with a direct Swift `BleBackend`: production ticket/Hub admission,
  synchronous Swift initialization/scan ingress, exact scan result, repeated
  `shutdown`, once-only backend dispose, post-shutdown rejection, and late
  ingress rejection passed. Kotlin/JVM independently exercised the same
  real-actor assembly/initialize/scan/shutdown path through a Kotlin
  `BleBackend`, with once-only dispose and late-ingress rejection. Exporting
  async `close()` initially caused a Kotlin binding compile conflict with
  UniFFI's generated object `close()`; the probe calls it `shutdown()` instead.
  A Swift Task cancelling a pending real BLeng initialization did not stop it;
  explicit owner shutdown settled it as `Closed`. The toy pending-future task
  also returned normally after cancellation and release. Kotlin coroutine
  cancellation terminated a pending toy coroutine and dropped the Rust future
  without release (observed via an isolated drop guard); a cancelled pending
  BLeng initialization followed by explicit shutdown disposed its backend
  once. This validates only the named Mac/probe candidate, not Android ART,
  iOS runtime, production packaging, native BLE cancellation or public BLeng
  semantics.

## Owner admission and remaining foreign boundary

The earlier ownership gap was a raw ticket escaping to the host: an in-flight
foreign `attach_backend` could outlive BLeng `close()` before any Hub was
registered. The crate-private `BackendPort` in
`crates/bleng/src/runtime/backend_port.rs` and `AssemblyAdmission` in
`crates/bleng/src/cleanup.rs` now avoid exporting a ticket. Admission and
cleanup registration share `HubCleanup`'s state lock; final registration also
checks the owner's open phase under that lock. Close/fault starts cleanup and
waits until the admitted synchronous attach finishes or rolls back. A Hub
returned after the phase closes is scheduled for tracked disposal before the
admission ends. An admitted foreign call that never returns **cannot** be
forcibly cancelled; close then also waits. The old direct `attach_hub` test
helper is not a foreign constructor.

## Cleanup ownership boundary

BLeng deliberately separates Hub cleanup from runtime shutdown. The cleanup
coordinator owns the lifecycle of every `Arc<BleHub>` that crossed the
assembly boundary:

```text
HubCleanup
├── assembly admission reservation
├── primary Hub registration
├── rejected/late Hub disposal slots
├── blocking native dispose scheduling
└── cleanup completion and error accounting
```

`RuntimeOwner` owns the separate runtime resources:

```text
RuntimeOwner
├── Tokio runtime and Kameo actor
├── actor mailbox and event receiver
├── waiter registry and command deadlines
├── ingress phase/fault propagation
└── orchestration of actor stop followed by HubCleanup::wait()
```

`RuntimeOwner::close()` therefore coordinates two lifecycles; it does not move
actor shutdown or waiter retirement into `cleanup.rs`. Cleanup does not wait for
BLE commands, scan sessions, connections, GATT operations, event readers, or
waiter futures. Those are runtime concerns and must settle or fail through the
runtime's actor/ waiter policy before or while the owner closes.

A BLeng instance has exactly one dispatchable primary Hub. A second Hub is not
another active slot. It can exist transiently when an admitted synchronous
foreign assembly finishes after a concurrent close/fault, or when a test
constructs an additional Hub. Registration rejects it, but cleanup must still
own and dispose it. The rejected Hub is therefore recorded as a disposal slot
before its ownership is moved into the detached `spawn_blocking` closure; it is
not synchronously `drop`ped or disposed on the registration stack. This
fire-and-forget closure records its terminal result in the cleanup ledger. This
guarantees that:

1. potentially blocking native disposal runs on Tokio's blocking worker pool
   rather than an actor, callback, or UI stack;
2. `close()` cannot report completion before late/rejected Hub disposal has
   settled; and
3. disposal errors and panic containment use the same result ledger as primary
   Hub cleanup.

The public assembly error contains only the caller-facing reason
(`AdditionalHubRejected`, `Closed`, or duplicate ownership). It must not carry
an `Arc<BleHub>` or an internal disposal-slot index. Those values represent
cleanup ownership, not API error data. `HubRegistrationOutcome` is the
internal handoff between state registration and asynchronous disposal.

This fixes the tested Rust-only ownership race; the isolated Swift and Kotlin/JVM
probes also exercise a real BLeng owner, but do not prove production packaging.
`BleHub::new` currently logs and discards a `dispose()` error during failed
construction (`crates/blehub/src/hub.rs`); BLeng can wait for that rollback
but cannot report its swallowed cleanup error through `close()`. Resolve and
test this error-accounting boundary before claiming complete rollback outcome
accounting. The current constructor rollback boundary now reports a failed
backend `dispose()` as `BleHubError::BackendCleanup`, retaining both the
assembly stage and primary/cleanup diagnostics; a regression test covers
capabilities failure plus cleanup failure. Still unverified for **production
BLeng**: Android ART, iOS runtime, foreign ARC/GC release, reload, concurrent
cross-language calls,
foreign callback panic, and cancellation during blocked attach. Dropping
`RuntimeOwner` without explicit close starts cleanup but uses background
runtime shutdown; it is not a synchronous dispose boundary.

The pinned UniFFI 0.32.0 Swift `Async.swift` generator template uses
`withUnsafeContinuation` and `defer freeFunc` after completion but does not
wire Swift `Task.cancel()` to `rust_future_cancel`. Its Kotlin `Async.kt`
template uses `suspendCancellableCoroutine` with `finally freeFunc`; Kotlin
cancellation was also observed on macOS JVM via a pending Rust-future drop
guard, but not on Android ART. Core's `ffi/rustfuture/future.rs` documents
cancel/free, but an available FFI cancel
symbol does not mean generated foreign callers invoke it. BLeng upper waiter
cancellation and explicit close must retain their own bounded lifecycle;
neither promises native BLE cancellation.

The current Apple production packager (`tools/apple/package_uniffi.py`) has
an exact allowlist for only `blehub.swift`/`blehubFFI.h` and stages one header
and Swift binding; `tools/api_snapshots.py` snapshots only the `blehub`
component. The feature-gated real BLeng Mac probe demonstrates mechanism
without changing that production packager. Next: design the public error/API
surface, explicitly allowlist both **production** components (`blehub` and
`bleng`) in the one-image package, run artifact-only Apple/Kotlin consumers,
and validate close/release/reload on their actual platforms. Do not stage the
toy third component or treat the opt-in probe ABI as frozen. No second
process-wide routing registry or second BLE contract belongs in
`crates/blehub`.

With the current single DK, single-device RF can be prepared independently;
true multi-device RF stays unverified until at least two standalone DKs are
independently controllable. Neither hardware gate changes the assembly
ownership requirement.
