# Architecture

This directory contains stable architecture descriptions for maintained BleHub components. A document belongs here when it describes long-lived ownership, layering, lifecycle, or recovery boundaries rather than one candidate run or an unresolved research comparison.

The approved architecture, public contract, phase order, and exit conditions remain in [`.plan/PLAN.md`](../../.plan/PLAN.md). Architecture pages explain that plan; they must not duplicate or silently override it.

## Documents

- [`bleng-recovery.md`](bleng-recovery.md): the pure recovery policy module inside `bleng-supervisor` and its native Apple/Android session-supervisor boundary.

Candidate-specific observations belong in [`../testing/reports/`](../testing/reports/). Exploratory comparisons and dated design decisions belong in [`../research/`](../research/).
