import type { BlengFailureInfo } from '../specs/BlengFailure.nitro'

/**
 * Canonical, display-oriented reason for a stable failure `code`.
 *
 * This is derived, never a second taxonomy: `code` stays the only field safe for control flow,
 * and `reason` is a stable English sentence for logs and UI. It falls back to the native
 * `message` for any code this table does not know, so a newly added `code` still has a usable
 * display string without a release of this package.
 *
 * Codes are `path.suffix` (for example `bleng.dispatch_rejected.not_supported` or
 * `delivery.byte_overflow`). A full code is looked up first, then its last segment. The full-code
 * tier exists because two families can share a last segment with different meanings
 * (`supervisor.busy` is not `delivery.busy`); the suffix tier keeps older and newly added family
 * members displayable without a release of this package. Both store the exact spelling the Rust
 * supervisor emits (snake_case there, `bridgeFailure` from the platform bridges), because a
 * near-miss key would silently fall back to the native message.
 */
const REASONS: Readonly<Record<string, string>> = {
  // Dispatch / backend failures (`bleng.*`).
  not_supported: 'The platform does not support this operation.',
  permission: 'Bluetooth permission is missing or denied.',
  adapter_unavailable: 'The Bluetooth adapter is unavailable.',
  invalid_state: 'The operation is not valid in the current state.',
  invalid_request: 'The request was invalid.',
  not_found: 'The target was not found.',
  timeout: 'The operation timed out.',
  cancelled: 'The operation was cancelled.',
  operation_aborted: 'The platform aborted the operation.',
  connection: 'The connection failed or was lost.',
  gatt: 'A GATT operation failed.',
  protocol: 'The peer or platform reported a protocol error.',
  backend: 'The native backend failed.',
  internal: 'An internal error occurred.',
  closed: 'The engine is closed.',
  transport_fault: 'The transport failed.',
  worker_stopped: 'The runtime worker stopped.',
  operation_not_found: 'The operation was not found.',
  operation_already_completed: 'The operation already completed.',
  operation_not_cancellable: 'The operation cannot be cancelled.',
  resource_exhausted: 'A bounded resource is exhausted.',
  resource_busy: 'A bounded resource is busy.',
  backend_unavailable: 'The native backend is unavailable.',
  backend_assembly: 'The native backend failed to assemble.',
  backend_cleanup: 'The native backend failed to clean up.',
  invalid_configuration: 'The configuration is invalid.',
  ffi_contract: 'The FFI contract was violated.',
  invalid_timeout: 'The timeout is not a valid bounded budget.',
  // Owner-level conditions share their last segment with another family, so they are keyed by the
  // full code: a suffix lookup would hand `supervisor.busy` the delivery lane's sentence.
  'supervisor.busy': 'Another lifecycle operation is already in progress on this supervisor.',
  'supervisor.unavailable':
    'The supervisor is unavailable after an interrupted lifecycle operation.',
  // Admission (`bleng.busy.*`) is the only family whose last segment collides with the BLE
  // categories; every other member falls back to the native sentence, which already names the
  // busy resource.
  'bleng.busy.gatt': 'GATT discovery conflicts with pending work or an active subscription.',

  // Bridge-level codes are spelled by the platform bridges, so they are not snake_case.
  bridgeFailure: 'The native bridge failed.',
  submissionUnavailable: 'No owner is active, so nothing can be submitted.',

  // Observation delivery failures (`delivery.*`). These are the codes the supervisor emits
  // today, as the last segment of the stable dotted code; a lane cannot have two consumers, so
  // the refusals name which contract was broken.
  pull_busy: 'Another pull is already pending on this lane.',
  'delivery.busy': 'Another delivery consumer is still attached to this lane.',
  conflicting_source: 'This lane already has a different delivery source.',
  conflicting_start: 'Another start is already in flight for this lane.',
  unexpected_started: 'The platform reported a start this lane did not submit.',
  start_failed: 'The lane failed before its start was observed.',
  advertisement_overflow: 'The bounded advertisement buffer overflowed and ended the lane.',
  byte_overflow: 'The bounded byte buffer overflowed and ended the lane.',
  value_overflow: 'The bounded value buffer overflowed and ended the lane.',
  snapshot_overflow: 'The bounded snapshot buffer overflowed and ended the lane.',
  presence_overflow: 'The bounded presence buffer overflowed and ended the lane.',
  stop_data_overflow: 'The scan-stop payload overflowed and ended the lane.',
  pending_event_overflow: 'The pending event buffer overflowed and ended the lane.',
  owner_retired: 'The owner retired this lane.',
  detached: 'The lane was detached.',
  'delivery.unavailable': 'The lane is no longer available.',
}

/** The last `.`-separated segment of a stable failure code. */
function suffix(code: string): string {
  const index = code.lastIndexOf('.')
  return index < 0 ? code : code.slice(index + 1)
}

/**
 * A stable English sentence for a failure, or the native `message` when the code is unknown.
 *
 * ```ts
 * try {
 *   await pendingOperation(connection.beginRequestMtu(247), 5_000).result
 * } catch (error) {
 *   if (error instanceof BlengOperationError) console.log(error.code, error.reason)
 * }
 * ```
 */
export function failureReason(info: BlengFailureInfo): string {
  return REASONS[info.code] ?? REASONS[suffix(info.code)] ?? info.message
}
