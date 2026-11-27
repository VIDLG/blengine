# Archived FFI and integration spikes

- Archived: 2026-08-30
- Status: frozen historical evidence; not an active build or maintenance boundary
- All fixtures under this directory are archived; there is no active FFI production-candidate fixture.

These fixtures supported the Phase 0 FFI decision and are retained so the evidence and rejected alternatives remain reviewable. Stable UniFFI's production-candidate object graph supersedes them.

Archived fixtures are intentionally excluded from default `fmt`, `check`, `lint`, test recipes, active CI, and the normal Just recipe list. Their old READMEs and command examples describe historical executions; those recipe names are retired. Do not update archived code merely to follow the current public contract. If a past experiment must be rerun, restore it on a dedicated branch, adapt its archived tooling explicitly, and record the new evidence separately.

| Fixture | Historical purpose | Authoritative report |
|---|---|---|
| [`tauri-downstream/`](tauri-downstream/) | Superseded Tauri assembly, UI, and RF feasibility workspace | [`../../docs/testing/reports/2026-09-07-tauri-production-migration.md`](../../docs/testing/reports/2026-09-07-tauri-production-migration.md) |
| [`nrf-att-hold/`](nrf-att-hold/) | Isolated DK ATT read-hold firmware, initial-read and RN reload feasibility; maintained optional gate lives under `tests/peripherals/nrf52840/diagnostic/` | [`../../docs/testing/reports/2026-09-27-react-native-ios-dk-pending-att-maintained.md`](../../docs/testing/reports/2026-09-27-react-native-ios-dk-pending-att-maintained.md) |
| [`boltffi-class-handle-callback-param/`](boltffi-class-handle-callback-param/) | Expected-failure probe for BoltFFI foreign callback class-handle parameters (#869) | [`../../docs/research/integrations/boltffi-event-ingress-workaround.md`](../../docs/research/integrations/boltffi-event-ingress-workaround.md) |
| [`boltffi-lifecycle/`](boltffi-lifecycle/) | Early BoltFFI callback/lifecycle mechanism and JNI comparison fixture | [`../../docs/research/integrations/uniffi-vs-boltffi-for-blehub.md`](../../docs/research/integrations/uniffi-vs-boltffi-for-blehub.md) |
| [`boltffi-token-ingress/`](boltffi-token-ingress/) | Production-shaped token-ingress workaround, Android/Swift lanes, and ACK probes | [`../../docs/research/integrations/phase0-token-ingress-runtime-results.md`](../../docs/research/integrations/phase0-token-ingress-runtime-results.md) |
| [`uniffi-error-field-collision/`](uniffi-error-field-collision/) | One-off Kotlin error-field naming generator probe | [`../../docs/research/integrations/uniffi-vs-boltffi-for-blehub.md`](../../docs/research/integrations/uniffi-vs-boltffi-for-blehub.md) |
| `uniffi-lifecycle/` | Representative Stable UniFFI retention, lifecycle, Android, and Apple packaging mechanism fixture | [`../../docs/research/integrations/ffi-lifecycle-concurrency.md`](../../docs/research/integrations/ffi-lifecycle-concurrency.md) |
| `uniffi-production-candidate/` | Superseded production-candidate object graph and package fixture | [`../../docs/testing/ffi-runtime.md`](../../docs/testing/ffi-runtime.md) |
| [`codesign-access/`](codesign-access/) | Isolated Apple Development private-key access and `errSecInternalComponent` signing behavior | Current Apple signing workflow and iPhone runtime reports |
| [`ios-signing-options/`](ios-signing-options/) | Compared Xcode-managed signing options for the generated iOS HIL project | Current iOS signing workflow and iPhone runtime reports |

Fixture-specific Python utilities were moved to [`tools/`](tools/) with the fixtures. They are frozen as source evidence and are not part of the root `tools` package or Ruff checks.

The historical BoltFFI token fixture still refers to the then-current production crate contract. Its archived path dependency is adjusted only so the source tree remains internally resolvable; it is not expected to compile against the evolving contract without deliberate restoration work.
