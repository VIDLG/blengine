# Windows Phase 2 exit review — 2026-09-02

## Decision

**PASS. Phase 2 is complete.**

The user explicitly approved the Phase 2 exit after reviewing the completed Windows backend, packaging, managed-fixture, lifecycle, soak, and destructive fault evidence. This review closes only the four normative Phase 2 exit conditions in [`.plan/PLAN.md`](../../../.plan/PLAN.md). It does not freeze the public ABI and does not convert unexecuted infrastructure or host-boundary checks into passes.

## Normative exit conditions

| Phase 2 exit condition | Evidence | Result |
|---|---|---|
| Windows completes the v0.1 loop and serves as the conformance reference implementation | Production `WindowsBackend` covers initialization, scan/filter/stop, connect/disconnect, public GATT discovery, read/write, Notification/Indication, passive disconnect, error mapping, and resource cleanup. The latest static baseline passed 39 Windows tests and 114 workspace tests, with 43 Windows tests when HIL callback gates were enabled. Real RF matrices passed against both Android and QEMU/BLESS providers. | PASS |
| `tests/consumers/windows` consumes only staged Windows/core crate artifacts | Standard Cargo staged archives, metadata-enforced source isolation, and the offline locked artifact-only Windows consumer passed. The consumer does not use workspace/path source or feature unification. | PASS |
| No known callback token or native-handle leak | Ten-cycle scan/connect and full active-resource lifecycle soaks returned every logical root and all 16 instrumented WinRT callback delegate families to zero after each cycle and final disposal. Passive-disconnect and in-flight callback fault gates also returned connection, GATT, subscription, and callback resources to zero. | PASS |
| Android development Peripheral and BLESS managed Peripheral are Just-managed and produce traceable reports | The Android fixture can be built, installed, started, controlled, and used for deterministic Notification/Indication. The QEMU Debian + BlueZ/BLESS fixture can be provisioned, started, attached, diagnosed, faulted, recovered, and stopped through project recipes. Provider-specific profiles prevent concurrent service-filter collisions. RF, throughput, soak, fault, and recovery reports are retained under `docs/testing/reports`. | PASS |

Primary evidence:

- [`2026-09-01 current-candidate RF and packaging summary`](2026-09-01-windows-current-candidate-rf-packaging-summary.md)
- [`2026-09-01 resource accounting and QEMU soak`](2026-09-01-windows-resource-accounting-qemu-soak.md)
- [`2026-09-02 passive-disconnect resource recovery`](2026-09-02-windows-passive-disconnect-resource-recovery.md)
- [`2026-09-02 fault, soak, reboot, and elevated-tooling follow-up`](2026-09-02-windows-phase2-fault-soak-reboot-hardening.md)

## Accepted infrastructure limitation

QEMU `11.1.0` under WHPX cannot reliably reboot the guest in the same QEMU process. The observed run completed guest shutdown and then paused with `WHPX: Unexpected VP exit code 4`. Starting a fresh WHPX process restored UB500 ownership, BlueZ/BLESS readiness, guest doctor, and Windows scan.

The supported WHPX recovery procedure is therefore a fresh-process restart. This is accepted as fixture infrastructure behavior and is neither a `WindowsBackend` pass nor a product failure.

## Boundaries that remain unpassed

The following checks were not required by the four Phase 2 exit conditions and remain explicitly unpassed:

- the TCG guest-reboot path after the controller was strengthened to require a changed Linux boot ID was not rerun over RF;
- a real Windows host reboot was not executed;
- the hardened UAC Zadig bind and USB rollback paths were not executed because they would change the active driver binding.

Earlier evidence is not relabeled to cover these later or unexecuted boundaries.

## Next implementation lane

The subsequent user-approved implementation order is now:

- Phase 3: one shared Swift/CoreBluetooth Apple backend, macOS first and iOS independently validated;
- Phase 4: Android Central backend;
- Phase 5: Windows + Apple + Android v0.1 and production ABI freeze;
- work before Phase 5 consumes the current unfrozen candidate, and affected gates must rerun against the freeze candidate;
- macOS evidence does not prove iOS, Windows evidence does not prove Android, and Mutagen synchronization proves only source transport.

Phase 2 completion does not freeze the public ABI or mark Phase 3, Phase 4, or Phase 5 complete.
