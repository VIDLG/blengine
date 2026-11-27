# Supervised scan UniFFI candidate

Date: 2026-11-03. Scope: the current unfrozen worktree candidate. This extends the
[Rust delivery slice](2026-11-03-rust-scan-delivery.md), without reinterpreting its historical results.

## Implemented boundary

`Supervisor.begin_scan` now returns a native-only `SupervisedScan` UniFFI object. The proxy owns
an original `StartScanOperation`, its Rust delivery endpoint and a fixed source context. It holds
only a **weak** Supervisor reference, not an `Arc<Bleng>` that could keep the Hub/backend open.

- `wait_until_started` consumes the original result and returns unit; the confirmed scan resource
  remains private. This receipt can precede the reader's observation of Started.
- `cancel_start` forwards the original typed cancellation receipt. It does not consume the result
  or imply remote rollback.
- `next_event(timeout_ms)` uses the start operation's existing executor for a pull observation
  deadline. The Rust-only `StartScanOperation::observe_with_timeout` validates the same 1 ms–24 h
  range, constructs the timer inside the existing owner runtime and uses the existing panic/drop
  boundary. No per-scan/global runtime or timer service was added.
- Pull timeout, invalid timeout and duplicate start wait do not detach delivery or issue BLE
  cancellation/stop. Real failed start results wake observation waiters with `StartFailed`, release
  observation admission and preserve the original typed operation error. This is not native rollback.
- A matching late Started can still retain an explicit-stop resource after delivery overflow;
  it does not reopen publication or erase the original failure.
- `begin_stop` checks owner/context/policy and resolves the fixed resource under the same bounded
  submission guard. A missing resource cannot mask owner retirement or cause a fresh context to
  be substituted. The result is the original `StopScanOperation`.
- `ScanSessionError` separates submission, original operation, observation and delivery failures.
  Error variants/causes remain typed; diagnostic message matching is not used.
- `observation_snapshot` is a read-only native diagnostic DTO. It is not automatically exported
  to JS and is not a native resource-zero assertion. `detach`/proxy drop end observation only.

Primary implementation:

- [`ffi/scan_session.rs`](../../../crates/bleng-supervisor/src/ffi/scan_session.rs)
- [`ffi/operations.rs`](../../../crates/bleng-supervisor/src/ffi/operations.rs)
- [`scan_delivery.rs`](../../../crates/bleng-supervisor/src/scan_delivery.rs)
- [`BLeng operation adapter`](../../../crates/bleng/src/facade/operations.rs)

## Tests and packaging actually executed

| Command | Observed result |
|---|---|
| `pixi run rtk cargo test --locked -p bleng --lib scan_observation` | Six new observation-executor regressions passed |
| `pixi run rtk cargo test --locked -p bleng --lib facade::tests` | 35 passed, including those six |
| `pixi run rtk cargo test --locked -p bleng --lib cleanup` | 14 passed |
| `pixi run rtk cargo test -p bleng-supervisor --all-features --test ffi_scan` | Final run: eight new FFI facade integration tests passed |
| `pixi run just test supervisor` | 83 passed across six suites |
| `pixi run just test bleng` | 270 passed across three suites |
| `pixi run just binding bleng-snapshot-update` | Accepted only the intentional new unfrozen candidate signatures |
| `pixi run just binding bleng-snapshot-check` | 25 extractor tests passed; six reviewed snapshots verified |
| `pixi run python -m tools.api_snapshots check` | Default three snapshots verified; unchanged by this work |
| `pixi run just apple consumer-bleng-macos` | Staged combined image built and actual Swift artifact consumer passed, including `supervisor_scan_proxy=passed` |
| `pixi run just ffi android-bleng-build` | New Kotlin bindings, AAR, App/test APK compilation and both ABI single-image audits passed; ART not run |
| `pixi run just fmt` / `pixi run just lint` | Passed, including workspace Clippy |
| `pixi run rtk cargo clippy --locked -p bleng-supervisor --all-targets --no-default-features -- -D warnings` | Pure Rust/default-feature boundary passed |
| `pixi run just docs-check-mermaid` | Passed |

The Rust FFI tests deliberately use a timerless caller runtime with a separate finite watchdog.
The BLeng tests also exercise an executor with no Tokio runtime at all. They cover pending-pull
wakeups, late resource observation, cancellation/result independence, failed start, old/new owner
context fencing and retained proxy retirement after shutdown/direct Supervisor drop.

The existing macOS artifact consumer gained a separate scan-proxy scenario; its ten-operation
regression was retained. The new Swift scenario exercises binary/optional advertisement data,
invalid and consumed start waits, a real pull timeout followed by another advertisement, original
stop control/result semantics, exact reader/terminal observations and retained-proxy retirement.
It resolves generated bindings and native code from staged artifacts, not checkout source includes.
The scenario's backend is scripted; its resource observations are not CoreBluetooth evidence.

Two Android instrumentation test methods exercise the equivalent generated Kotlin proxy and
pending-pull/control boundaries. They compiled in the artifact-only build but were **not executed**.
Neither Kotlin compilation nor Rust tests substitute for ART.

## Strict API coverage

The new proxy, errors, diagnostic record and `Supervisor.beginScan` appear in all applicable
reviewed snapshots. The generic observation helper appears only in the Rust BLeng snapshot;
it is not a Swift/Kotlin method or public runtime handle. Snapshot extraction still rejects unknown
exports, omitted methods, unexpected native-image files and DTO/error shape changes. No old API
was removed to obtain a pass; default BleHub snapshots were not changed.

## Failed attempts retained

- The first observation-helper test run was red because the method did not exist. Fixture setup
  errors in its first implementation run were corrected before the final six tests passed.
- The first FFI run passed six of eight tests. The stop path initially returned a missing-resource
  error before validating a retired owner; the production order was fixed. The fresh-owner fixture
  also lacked the required explicit old-operation barrier configuration; setup was corrected.
- A later test still conflated two retirement boundaries. It was changed to assert each precisely:
  a dropped weak Supervisor yields `Delivery(OwnerRetired)`, while a surviving-but-shutdown
  Supervisor yields `Submission(NoActiveSession)`. It does not accept arbitrary errors.
- The initial snapshot check had an overstrict cross-language method-order assertion. Full method
  signatures/counts are now checked without asserting identical language-specific ordering.
- A no-default-features Clippy check found two FFI-only delivery helpers compiled without their
  consumer. They are now gated on the FFI feature rather than suppressing dead-code diagnostics;
  the pure Rust check, full regressions, snapshots and artifact builds were rerun successfully.

Logs remain in ignored `.work/logs/ffi-scan-*.log`, `.work/api-snapshots-scan-*.log` and
`.logs/bleng-scan-observe-*`. These facts do not establish a historical TDD claim for all code.

## Remaining boundaries

- Formal Nitro specs/exports and the UI App still expose only the existing lifecycle API. No JS
  scan button or product scan object was added. The native FFI event/error types may contain raw
  IDs; the future Nitro projection must keep those below JS and explicitly map product errors.
- Opaque JS peripheral capabilities and their future connection use still require implementation.
- New-candidate iOS native runtime, Android ART, Hermes/UI/reload and RF gates remain unexecuted.
  macOS Swift execution does not prove iPhone behavior. Apple multi-slice packaging does not prove
  an iOS consumer executed the new proxy.
- No phone, simulator, permission, radio, firmware or RF mutation was performed. No legacy
  `react-native-internal` scenario or unique regression was deleted. ABI and phase exits remain open.
