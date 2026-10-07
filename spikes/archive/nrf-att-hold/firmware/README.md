# nRF ATT read-hold firmware patch

This directory contains a tracked, test-only source patch for the Zephyr
Tester at pinned revision `b5f309bc2da57b49012648c9432d0d4cb5ee1cc8`.
It does not modify the Zephyr checkout, the BleHub default fixture, or any
flash workflow.

Apply only to a separate, already pinned **clean disposable Zephyr worktree**
after verifying its HEAD equals `b5f309bc2da57b49012648c9432d0d4cb5ee1cc8`.
Never check out revisions or apply this patch to the shared `contrib/zephyr`
tree, nor to the workspace that builds the standard fixture. The patch is not a
build/flash recipe; the disposable image needs a distinct build root and its
own artifact/provenance manifest.

The patch is compile-time fail-closed. `CONFIG_BTTESTER_GATT_READ_HOLD` is
`n` by default; with it disabled, the existing authorization-read behavior and
BTP command table are unchanged. The timeout is bounded by
`CONFIG_BTTESTER_GATT_READ_HOLD_TIMEOUT_MS` (default 5000 ms, range 1..60000)
**after a matching callback claims the arm**. ARMED without a read has no
firmware timer; use a matching ABORT for cleanup.
No vendor or live Zephyr patch is maintained here.

## Protocol

The patch adds private GATT-service BTP opcodes after the pinned Tester range:

| Opcode | Name | Request | Response |
| ---: | --- | --- | --- |
| `0x24` | `READ_HOLD_ARM` | `handle: u16 LE` | `sequence: u32 LE`, `handle: u16 LE` |
| `0x25` | `READ_HOLD_RELEASE` | `sequence: u32 LE`, `handle: u16 LE` | Empty success, or BTP status failure |
| `0x26` | `READ_HOLD_ABORT` | `sequence: u32 LE`, `handle: u16 LE` | Empty success, or BTP status failure |
| `0x27` | `READ_HOLD_STATUS` | Empty | `state: u8`, `sequence: u32 LE`, `handle: u16 LE` |

The standard BTP header is **5 bytes**: service `u8`, opcode `u8`, index `u8`,
and length `u16 LE`. The extension is included in
`READ_SUPPORTED_COMMANDS` only when the Kconfig option is enabled. The ARM
response is the firmware-issued sequence used by later RELEASE, ABORT, and
STATUS operations.

Status values are:

| Value | State | Meaning |
| ---: | --- | --- |
| `0` | `IDLE` | No arm has been issued; sequence/handle are zero initially. |
| `1` | `ARMED` | One matching remote read may claim the target handle. |
| `2` | `WAITING` | The exact read claimed the arm and its callback is held. |
| `3` | `RELEASE_REQUESTED` | Release was accepted; callback completion is pending. |
| `4` | `ABORT_REQUESTED` | Abort was accepted for a waiting callback; completion is pending. |
| `5` | `RELEASED` | The callback resumed and proceeds with the attribute read. |
| `6` | `ABORTED` | Abort completed. From `ARMED`, no read claimed the arm and no semaphore was signaled; from `WAITING`, the callback resumed and returns ATT authorization error. |
| `7` | `EXPIRED` | The finite semaphore wait returned nonzero, including `-EAGAIN`; a waiting callback returns ATT authorization error. |
| `8` | `PREPARING` | ARM is resetting the semaphore; reads and control commands are rejected until ARMED. |

`PREPARING` is transient and is deliberately reported as a distinct state.
The Tester dispatches BTP handlers serially on its command worker. ARM publishes
`PREPARING` before calling `k_sem_reset()` and publishes `ARMED` only after the
reset completes; reads and controls reject while it is preparing. This prevents
an old waiter or late control give from becoming a token for the new generation.

Terminal states (`RELEASED`, `ABORTED`, and `EXPIRED`) persist for later
STATUS polling. A subsequent ARM is required to create another one-shot
operation; a later read never re-arms the hold automatically.

## Intended sequence

1. Build the disposable Tester with `CONFIG_BTTESTER_GATT_READ_HOLD=y`.
2. Create a characteristic using the existing Tester GATT commands with the
   read-authorization permission bit (`0x40`). Start the server and retain its
   value handle.
3. Send `READ_HOLD_ARM` for that value handle. Save the returned firmware
   `sequence` and `handle`.
4. Have the Central issue the target read, then poll `READ_HOLD_STATUS` until
   `WAITING`.
5. Send `READ_HOLD_RELEASE` or `READ_HOLD_ABORT` with the exact sequence and
   handle. The command response means only that the request was accepted; poll
   until `RELEASED`, `ABORTED`, or `EXPIRED`.

`READ_HOLD_ABORT` may also be sent while `ARMED` with the exact sequence and
handle. It transitions directly to `ABORTED`, returns success, and does not
signal or wait on the semaphore. This is the explicit cancellation path when
the expected Central read never arrives. RELEASE while `ARMED`, and duplicate
or stale controls after any terminal state, are rejected.

Only a read with `conn != NULL`, `attr->handle` equal to the armed handle,
and `offset == 0` can claim the arm. A BTP-local `GET_ATTRIBUTE_VALUE` of
the target is rejected before calling the attribute callback while armed,
even if it has a connected address. Every other authorization-protected read
returns the original ATT authorization error and leaves the ARM state
unchanged. After the matching read claims the arm, it is one-shot and cannot
be claimed again.

While the target is `PREPARING`, `ARMED`, `WAITING`, `RELEASE_REQUESTED`, or
`ABORT_REQUESTED`, the Tester `GET_ATTRIBUTE_VALUE` command rejects the target
before invoking `attr->read`, including when its address resolves to a
connected `bt_conn`. This BTP-local path cannot claim or block the held read;
the guard is compiled only with the opt-in feature.

Only one arm/hold operation can exist at a time. ARM is rejected while the
operation is `PREPARING`, `ARMED`, `WAITING`, `RELEASE_REQUESTED`, or `ABORT_REQUESTED`.
RELEASE and waiting-state ABORT require `WAITING` plus an exact sequence and
handle, so stale or duplicate commands are rejected. The sequence is
incremented by firmware for each accepted ARM and skips zero on wrap; this
prevents a delayed command from a prior operation from releasing a later
operation on the same handle.

The ATT callback wait is deliberately bounded and treats a nonzero
`k_sem_take()` result as expiration. If a release/abort give races after the
finite wait has returned `-EAGAIN`, the callback still enters `EXPIRED`, fails
the read closed, and drains the late semaphore give. A late control command
cannot turn an expired read into a successful read. ARM then enters
`PREPARING`, resets the semaphore, and only afterward publishes `ARMED`, so a
late token cannot poison the next arm.

## Safety and result limits

The Zephyr attribute-read callback has a synchronous `ssize_t` return API. It
has no asynchronous completion handle for deferring the ATT response. This
patch intentionally blocks inside that callback to create a deterministic
fault-injection point, which violates the normal nonblocking callback contract
and can stall Bluetooth host processing. It is therefore test-only and not
production-safe; the finite timeout limits but does not remove that risk.

`RELEASED`, `ABORTED`, and `EXPIRED` describe only the Tester firmware state
machine. In particular, `RELEASED` means the callback was allowed to continue;
it does not prove that the callback finished, that an ATT response was sent, or
that the Central received a success. `ABORTED` from `ARMED` has no ATT result at
all. Central-side ATT status must be observed independently through the
Central's own GATT callback/result path.

The attribute callback cannot identify the originating ATT opcode or fully
solve concurrent peers/read-by-type ambiguity. This experiment must use one
connection and must not issue another read after discovery while the target
read is being coordinated. Even under that restriction, STATUS is firmware
synchronization evidence only; it is not proof that the observed operation was
the intended Central ATT read or that its terminal ATT result reached the
Central.

This patch is intentionally not applied or flashed by this change. The narrow
source check is `git apply --check` against the pinned Zephyr checkout; a real
build and RF run require a separately selected disposable fixture and are not
claimed by this spike.
