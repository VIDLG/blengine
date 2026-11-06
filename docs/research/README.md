# Research

This directory contains project-authored research, design assessments, and dated implementation decisions. It is supporting evidence for the approved [`.plan/PLAN.md`](../../.plan/PLAN.md), not an independent architecture or release gate.

## Topics

- [`bleng/`](bleng/): BLeng runtime, actor/reservation, waiter, cancellation, cleanup, foreign API, and native queue design.
- [`react-native/`](react-native/): React Native/Nitro integration, host tooling, RF lab architecture, and iOS feasibility work. The [formal scan contract](react-native/react-native-bleng-scan-contract.md) records the approved design and test boundaries, with an isolated pinned-generator experiment; the scan API implementation and runtime validation are still pending.
- [`integrations/`](integrations/): UniFFI, FFI lifecycle, direct ingress, BoltFFI, and Tauri integration boundaries.
- [`platform/`](platform/): platform abstraction, cross-platform contracts, and Apple/native BLE ecosystem assessments.
- [`peripheral/`](peripheral/): nRF52840 firmware/provider responsibilities and peripheral ownership.
- [`cross-cutting/`](cross-cutting/): testing responsibilities and concurrency matrices that span multiple layers.
- [`archive/`](archive/): historical or superseded research retained for provenance; it is not active implementation guidance by default.

For current candidate facts, failures, and unverified limits, use [`../testing/reports/`](../testing/reports/) rather than adding a chronological log here. The current maintained entry points are also summarized in [`AGENTS.md`](../../AGENTS.md).
