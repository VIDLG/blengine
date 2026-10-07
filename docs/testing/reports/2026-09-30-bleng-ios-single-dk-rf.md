# BLeng candidate iOS single-DK RF matrix

Date: 2026-09-30

## Scope and candidate

The isolated BLeng iOS HIL passed the single-Peripheral RF matrix on a physical
iPhone using the production `AppleBleBackend` and opt-in two-component UniFFI
image. This is **BLeng candidate iOS evidence**, not RN, Android, multi-device,
ABI-freeze or default production-package acceptance.

- Worktree baseline: `dbfd1e6cb1e1bacf688bcd7ecda7fd0bdb1d597d`, with uncommitted
  BLeng/package/HIL changes. The Git commit alone does not identify this candidate.
- DUT: iPhone 14 Plus, `00008110-000C61160C39401E`.
- Signing team: `FWVYQ287W5`; no password was read, stored or passed by the runner.
- Peripheral: one standalone nRF52840 DK.
- Profile: `blehub-nrf52840-indication-v2`.
- Service: `fd000000-0000-4000-8000-000000000000`.
- Package: `.work/staged/apple-bleng-candidate`.
- Final signed app: exactly one image with `blehub` + `bleng` UniFFI component
  symbols; the default single-component production package remains unchanged.

The iOS device static archive SHA-256 measured after the final series was
`b25e278d53db2f11dbc034ff5bc41e115fd87fe0e0646889215f433e4ef1d1d6`.
This is a final artifact fingerprint, not a per-run comparison of every rebuilt
XCFramework. No Rust/backend/API changes occurred between the final series' gates.

## Previous string-error candidate series

The first complete matrix below used the candidate facade before structured error
mapping. It remains historical evidence for that exact candidate and is retained
for failure/correction provenance. Each command used a matching device run-ID
receipt rather than a UI success string.
Receipts are retained locally under `.work/apple-bleng-candidate-ios-backend/`:
`<run-id>-result.txt` for basic gates and `<run-id>-<scenario>-result.txt` for
coordinated subscription/recovery gates.

| Recipe | Final passing run ID | Evidence |
|---|---|---|
| `bleng-candidate-ios-rf-scan` | `307dc812-3c90-47b2-9261-97ca5bcb99a5` | Profile-filtered scan, matching advertisement, requested stop, one-second app-event silence |
| `bleng-candidate-ios-rf-connection` | `fcad26ce-0ca3-4331-bfdf-149a1ba391e8` | Same-command Connecting/Connected, different non-nil command for logical disconnect |
| `bleng-candidate-ios-rf-gatt` | `d86b3752-91c9-401b-8342-0d8ee687ca1e` | One primary fixture service, five characteristics, exact properties, two CCCDs and unique instance IDs |
| `bleng-candidate-ios-rf-io` | `690a723b-071f-42b2-8050-a653998f21ff` | Initial `00`, response write/readback `10`, no-response local submission followed by separate delayed `ab` readback |
| `bleng-candidate-ios-rf-rssi` | `adb463ec-1a44-4af0-939d-384368794767` | `-67` dBm, same-connection RSSI event correlation |
| `bleng-candidate-ios-rf-notification` | `9619aab1-3880-47c5-9710-a38c72504570` | FFF2 PlatformDefault, actual Notification, exact `EF`, enable/disable ordering, two-second app-event silence |
| `bleng-candidate-ios-rf-indication` | `fed77a61-bba1-4535-957b-aeb538a641cb` | FFF2 strict rejection, successful subsequent FFF2 enable/disable, FFF5 actual Indication, exact `EF`, disable and two-second app-event silence |
| `bleng-candidate-ios-rf-passive-disconnect` | `711a1054-4d1c-4035-a5fb-9bc2e9c01dc4` | Active subscription, DK-initiated command-less connection terminal, stale-handle rejection, fresh reconnect/discovery and `00` readback |
| `bleng-candidate-ios-rf-recovery` | `0a0fc511-8f5c-48e8-a774-eb29b47d88a4` | Three passive-loss/reconnect cycles on one BLeng owner, scoped `EF`, fresh IDs, `00` reads, final logical disconnect/shutdown |

The four coordinated gates reran after the startup and normal-exit cleanup
hardening. The five basic gates used the shell-managed provider recipes. All
commands exited successfully and the provider was confirmed `stopped` after the
series.

## Structured-error candidate rerun

After the candidate facade changed from debug-string errors to structured
canonical payloads and typed local reasons, the affected single-DK matrix was
rerun against a rebuilt two-component package. These are the current receipts
for the structured-error candidate:

| Recipe | Passing run ID | Evidence |
|---|---|---|
| `bleng-candidate-ios-rf-scan` | `07c25492-2f8b-4e2e-a08b-58da00750543` | Profile-filtered scan and requested stop |
| `bleng-candidate-ios-rf-connection` | `1e12676f-3515-4589-b13f-d5c9934eaa94` | Correlated connect and logical disconnect |
| `bleng-candidate-ios-rf-gatt` | `c8261b23-71a3-471f-94d7-e18a401f52da` | GATT database and instance validation |
| `bleng-candidate-ios-rf-io` | `6d837c95-2cac-40bd-b580-0ed8d81a6e1e` | Read/write and delayed no-response readback |
| `bleng-candidate-ios-rf-rssi` | `4cd067ed-347a-4551-98ca-daa1143b365a` | Connected RSSI correlation |
| `bleng-candidate-ios-rf-notification` | `1deca92a-6215-4aec-882c-565ad7804aea` | PlatformDefault Notification, exact `EF`, disable silence |
| `bleng-candidate-ios-rf-indication` | `8aaba588-c186-453d-8de3-75f0d1ceb6dd` | Typed `NotSupported`, FFF5 Indication, exact `EF` |
| `bleng-candidate-ios-rf-recovery` | `738aaf38-2d74-40a2-858e-8faa39b68556` | Typed stale connection/GATT/subscription rejection and three-cycle recovery |

The recovery scenario includes the passive command-less loss boundary and its
reconnect. No production Rust/Apple backend logic changed for this API update.
The provider was stopped after every run. The prior passive-disconnect run ID
remains useful as historical evidence, but recovery is the current receipt for
that boundary.

## Reviewed `Bleng` name and timeout candidate rerun

After the foreign API review fixed the exported object name to `Bleng`, kept the
internal owner crate-private as `BlengOwner`, selected one logical event reader,
and bounded every timeout to `1..=86_400_000` ms, the complete single-DK matrix
was rerun against a newly staged two-component image. These are the current
receipts for that reviewed-but-unfrozen candidate:

| Recipe | Passing run ID | Evidence |
|---|---|---|
| `bleng-candidate-ios-rf-scan` | `99f9b67d-f4c1-4f4b-af4b-a9068bef9a09` | Profile-filtered scan, matching advertisement, requested stop and post-stop silence |
| `bleng-candidate-ios-rf-connection` | `2e244a36-2df1-43a2-ba32-071b936ec39a` | Correlated Connecting/Connected and logical disconnect after clearing stale Tester pairing |
| `bleng-candidate-ios-rf-gatt` | `84e537a1-3f84-492d-ad98-a443367c409d` | Aggregate database, characteristic/descriptor instances and profile mapping |
| `bleng-candidate-ios-rf-io` | `ef6d87b1-e227-4c09-9c7b-56b901b76ad3` | Initial read, response write/readback and no-response local submission plus delayed readback |
| `bleng-candidate-ios-rf-rssi` | `de4d107e-d1f2-435e-9fdf-ee17eda0267e` | Connected RSSI command/event correlation |
| `bleng-candidate-ios-rf-notification` | `9dfa14f3-63eb-40e4-8a57-b7a48317dde3` | PlatformDefault Notification, exact `EF`, disable ordering and stale-route silence |
| `bleng-candidate-ios-rf-indication` | `78ab14dc-bd7d-4942-a53a-27a9ed2c1f80` | Strict Indication mode, exact `EF`, rejection and subsequent subscription lifecycle |
| `bleng-candidate-ios-rf-recovery` | `dc8aeb52-15ec-404c-9e0e-415172017ca7` | Passive loss, stale-route rejection, reconnect and three recovery cycles |

The run also corrected two infrastructure assumptions exposed by the current
DK: normal profile startup no longer requires optional `GATT_RESET_SERVER`, and
the iOS runner waits for the CoreDevice wired tunnel and launch-unlock state
before starting the app, with bounded retries for transient `SBMainWorkspace`
launch races. The fixture was confirmed `stopped` after the final run.

- Notification: `f0db597c-fb9c-4397-b662-0ae88086e60f`.
- Indication: `b6e6524b-dced-476a-b3e7-0d6caa71e81b`.
- Passive disconnect: `fcbe078e-134a-4e36-9468-49e0789ea8f8`.
- Three-cycle recovery: `76e44a9e-a23a-43e1-89f1-f5d3a49900b1`.

## Failures and corrections

The initial notification attempt installed the signed app and passed its image
check, but `devicectl` denied launch because the iPhone was locked. No app RF
ran; the provider was stopped. Unlocking was a human action, not a script bypass.

Passive-disconnect run `900f5c9f-bceb-4e78-96d2-2a36b17ba66c` timed out because
the new HIL only recognized `.ok(.disconnected)`. The production Apple backend
also emits command-less `.err(category: .connection)` when native disconnect
supplies an error. The existing native HIL already recognizes both paths. The
candidate HIL was corrected to retain the actual terminal kind and category;
errors are **not** relabeled as successful `Disconnected` state events.

Run `d606dece-6542-403d-8ad9-99e22264f516` reached passive loss but failed an
incorrect stale-unsubscribe expectation. BLeng removes the retired connection,
so commands using it return `ConnectionNotActive`; using an old subscription
on a new live connection returns `SubscriptionNotActive`. The HIL and receipt
classifier now enforce that distinction. Neither correction modified production
Rust or the Apple backend.

A subsequent focused audit found that detached provider startup could outlive a
readiness or post-start status failure. Provider startup now rolls back only its
recorded PID/PGID, and the Apple runner uses the explicit `stop-owned` command
for exact-identity cleanup, including normal exit without a check-then-global-stop
race. Regression tests exercise readiness failure, existing
provider preservation, post-start status failure and identity change. Ordinary
RF tests do not deliberately create these startup faults on hardware.

## Semantics and limits

- The tested path is Swift HIL → candidate `Bleng` → production `BleHub` →
  production `AppleBleBackend` → CoreBluetooth → DK. It does not use Nitro/JS.
- Both accepted passive terminal kinds require a nil command ID, exact connection
  correlation, one terminal and ordering after the observed notification. The
  recorded passing recovery receipts preserve `connection-error` with category
  `connection`, rather than claiming `.ok(.disconnected)`.
- Recovery uses `recovery-ios` coordination: cycles 1–3 keep the recovered
  connection for the next cycle rather than actively disconnecting between cycles.
- Stale-handle rejection proves BLeng admission/projection behavior; it is not
  independent proof of native callback-route teardown or exact resource accounting.
- Silence is bounded absence of observed app events. It is not absence of RF
  packets, proof that no late CoreBluetooth callback occurred, or a soak result.
- `post-disable-triggered` is a provider acknowledgement; it does not send a
  second GATT value. Do not claim attempted delivery after disable.
- No-response write completion is local CoreBluetooth submission. Delayed readback
  is separate fixture-application evidence, not stronger terminal semantics.
- The structured-error rerun uses `Operation(error: BleEventError)` for canonical
  domain failures and typed `NotReady` reasons for stale connection/GATT/
  subscription handles. The HIL no longer parses debug-formatted error text.
  This validates the candidate mapping, not a frozen public ABI.
- Candidate timeout inputs are integer milliseconds in `1..=86_400_000`; this
  RF matrix does not itself prove the invalid-timeout foreign binding cases.
- One DK cannot prove concurrent independent Peripherals. Two-DK RF remains
  pending another board. No Android ART/RF, RN high-level API, throughput/soak,
  pending-operation destructive fault or ABI freeze is claimed here.

## Implementation references

- [Scan/subscription evidence](../../../tests/hil/apple/bleng-candidate-ios/Sources/ScanScenario.swift)
- [Recovery scenario](../../../tests/hil/apple/bleng-candidate-ios/Sources/RecoveryScenario.swift)
- [App flags/result publication](../../../tests/hil/apple/bleng-candidate-ios/Sources/AppDelegate.swift)
- [Host coordination and receipt validation](../../../tools/apple/bleng_candidate_ios_backend.py)
- [Host regression tests](../../../tools/apple/test_bleng_candidate_ios_backend.py)
- [Provider lifecycle](../../../tests/peripherals/nrf52840/provider.py)
- [Provider coordination](../../../tests/peripherals/nrf52840/direct_fixture.py)
- [Apple passive terminal mapping](../../../platforms/apple/Sources/BleHubApple/AppleBleBackend.swift)
- [BLeng connection retirement](../../../crates/bleng/src/runtime/actor/events.rs)
- [BLeng subscription admission](../../../crates/bleng/src/runtime/subscriptions.rs)

## Final software validation

All passed after the final RF series:

- `pixi run just fmt` and `pixi run just lint` (including workspace Clippy).
- The targeted runner regression `pixi run python -m unittest tools.apple.test_bleng_candidate_ios_backend -q`: 20 tests passed; `tests.peripherals.nrf52840.test_provider`: 19 tests passed. These mocks/classifiers are software regression evidence, not additional physical RF runs.
- `pixi run rtk cargo test -p bleng --features candidate-api`: 136 tests passed, including the shared-FIFO reader and timeout boundary tests.
- `pixi run just binding candidate-snapshot-check`: all three candidate snapshots passed.
- `pixi run just apple consumer-bleng-candidate-macos`: macOS artifact-only foreign runtime passed.
- `pixi run just apple consumer-bleng-candidate-ios-unsigned`: generic iOS device and Simulator builds plus their single-image checks passed.
- `pixi run just apple bleng-candidate-ios-apple-init-build`: production Apple backend candidate generic device and Simulator builds plus image checks passed.
- `pixi run just docs-check-mermaid`, UTF-8/fence/whitespace/conflict checks and `git diff --check`.

## Next acceptance boundaries

The enumerated iOS single-DK RF matrix is complete for the reviewed `Bleng`
name/timeout candidate, but it remains unfrozen. The approved error mapping,
single-reader semantics and timeout boundary are validated; default-package
graduation is still separate. Do not silently promote these results to default
production or public RN. Android package/ART/RF and two-DK concurrent RF remain
independent gates.
