# Rust bounded scan delivery candidate

Date: 2026-11-03. Scope: current dirty worktree, Rust software and binding-shape checks only.
This is not a native platform runtime, RN/Hermes, package-consumer or RF verdict.

## Implemented slice

- `SessionSupervisor::begin_scan` validates the same active owner/context/policy boundary as
  existing typed submissions and returns a native Rust `ScanHandoff`: the original
  `StartScanOperation` plus its bounded `ScanDelivery` observation endpoint.
- A short owner-local routing lock spans only synchronous BLeng submission and command binding.
  The existing single reader's `SessionScanProjection::project` cannot pass this slot before
  installation. No second reader, runtime, worker or timer was added.
- The projection still returns its original envelope. Optional delivery overflow does not stop
  the original reader or change raw event/operation delivery.
- Before matching Started, bounded staging retains advertisements and end observations. Binding
  requires the exact start command and immutable full context; replay filters by exact scan ID
  and preserves order, including stop-before-start and advertisement-before-start.
- Advertisement capacity is 64 records and 256 KiB of owned data, including nested Vec element
  storage as well as payload/UTF-8 bytes. Admission precedes cloning, so long lists of empty
  payloads cannot evade the limit.
- End-before-start staging separately allows at most 64 stop events. Stop error strings have
  their own cumulative 256 KiB budget, also checked before clone, including all platform-detail
  strings. A full advertisement queue cannot crowd out normal terminal delivery.
- Single-pending pull uses Tokio synchronization without requiring a Tokio executor for polling.
  Dropping the future releases only pull admission. Normal stop drains earlier matching ads and
  latches its result; overflow fails delivery and clears buffers without issuing BLE commands.
- Explicit detach records observation closure independently from the first failure. It releases
  the routing slot while retaining that failure; it is not proof of native stop. BLeng still
  rejects a new scan when the native/session reservation is unavailable.
- Owner-local weak lifetime bookkeeping includes previously replaced, still-live deliveries.
  Shutdown/drop revokes all of them and clears undelivered observations. It does not keep handles
  alive, route events by a process-global registry or accumulate discarded-handle tombstones.
- Read-only snapshots report advertisement count/data bytes, retained stop-error data bytes and
  pending-pull status. These are point-in-time delivery observations, not native resource-zero.

Implementation: `crates/bleng-supervisor/src/scan_delivery.rs`, `src/scan.rs` and `src/supervisor.rs`.
Tests remain outside production flow in `src/tests/scan_delivery.rs` and `tests/scan_delivery.rs`.

## Evidence

Commands actually executed:

| Command | Result |
|---|---|
| `pixi run rtk cargo test -p bleng-supervisor --all-features --lib scan_delivery` | Initial red: module implementation absent; then unit checks passed as implementation was added |
| `pixi run rtk cargo test -p bleng-supervisor --all-features --test scan_delivery` | Initial six integration tests passed; later regressions included in canonical run below |
| `pixi run just test supervisor` | Final run: 75 passed across five suites, including 15 new delivery unit tests and seven new integration tests |
| `pixi run just test bleng` | 264 passed across three suites |
| `pixi run just binding bleng-snapshot-check` | Combined image rebuilt, Swift/Kotlin bindings regenerated, 22 extractor tests passed and six reviewed snapshots verified without accepting API changes |
| `pixi run just fmt` / `pixi run just lint` | Final checks passed, including workspace Clippy |
| `pixi run just docs-check-mermaid` | Passed |

The integration fixture runs an actual single `Bleng.next_event` task over the scripted backend,
then calls the production projection. Receipt channels establish that the reader processed an
observation; operation wait is not treated as reader flush. Tests cover explicit stop after
count/byte overflow, retained old delivery retirement, detach after failure, start control/result
independence, stale context/phase rejection and pending-pull wakeup. Fake resource observations
are fixture facts only, not proof about CoreBluetooth or Android Framework resources.

## Preserved failed attempts

- A combined regression attempt failed
  `typed_submission_context_guard_precedes_allocation_and_backend_dispatch`: recorded independent
  Connect and StartScan dispatches appeared in the reverse order from the assertion. The test
  was not changed. A subsequent full canonical Supervisor run passed. The ordering failure remains
  recorded here; this candidate does not resolve or explain that test's scheduling sensitivity.
- Initial lint rejected a nested match/if (`clippy::collapsible_match`). The branch was flattened
  with an early return; the final canonical lint run passed.
- Review found replaced deliveries escaping revoke, failed delivery detach retaining the slot,
  uncharged empty collection entries and unbounded stop error text. Regression assertions and
  minimal fixes are in the final candidate; these were not silently waived.

Full outputs remain in ignored `.work/logs/scan-delivery-*.log`; tests do not establish that all
historical development followed TDD. A missing-module red is not a behavioral red for every
subsequent regression.

## Still not implemented or verified

- No new UniFFI scan-session object or Nitro/JS scan API is exposed. The new handoff is Rust-only;
  existing reviewed foreign signatures are unchanged.
- `ScanDelivery::next_event` is a cancel-safe primitive with no internal timeout. The forthcoming
  foreign wrapper still needs observation-deadline projection and typed error mapping.
- The handoff deliberately retains the original operation. If start fails without a public scan
  event, native Rust callers must explicitly detach the observation endpoint when no longer
  needed. Detach is not native rollback; start wait/cancel outcomes and cleanup remain distinct.
  The forthcoming high-level proxy must implement and test this policy rather than infer success
  or native stop from the observation queue.
- Opaque JS peripheral capabilities, precise RN result/error projection, native Promise lifecycle,
  engine invalidation/reload, formal UI scanning and platform RF migration remain pending.
- No device, radio, permission, firmware, signing, iPhone/ART execution or RF action occurred.
  No internal integration, unique safety regression or legacy RF scenario was deleted.
