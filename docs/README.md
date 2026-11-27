# BleHub documentation

This directory is organized by document responsibility. The approved architecture and phase gates remain in [`.plan/PLAN.md`](../.plan/PLAN.md); this directory does not create a second plan.

## Sections

- [`architecture/`](architecture/): stable, durable architecture descriptions. These documents explain responsibilities and boundaries that should remain useful across candidate runs.
- [`research/`](research/): project-authored design research, implementation assessments, and decision context. Research supports the Plan but does not silently change it.
- [`testing/`](testing/): maintained execution guidance for deterministic, native, FFI/package, and RF validation.
- [`testing/reports/`](testing/reports/): dated candidate facts, failures, and unverified boundaries. Reports are evidence records, not a second set of current requirements.
- [`platform-api-reference/`](platform-api-reference/): generated snapshots of official platform API documentation. These are evidence sources, not public BleHub API definitions.
- [`git-workflow.md`](git-workflow.md): repository and multi-host Git workflow.

## Current entry points

- Product and phase authority: [`.plan/PLAN.md`](../.plan/PLAN.md)
- Working rules and evidence navigation: [`AGENTS.md`](../AGENTS.md)
- Stable recovery architecture: [`architecture/bleng-recovery.md`](architecture/bleng-recovery.md)
- BLeng runtime design tutorial: [`research/bleng/bleng-runtime-review-tutorial-2026-10-30.zh-CN.md`](research/bleng/bleng-runtime-review-tutorial-2026-10-30.zh-CN.md)
- Test execution: [`testing/README.md`](testing/README.md)
- Candidate evidence index: [`testing/reports/README.md`](testing/reports/README.md)

`research/archive/`, archived testing pages, and historical reports preserve context. They are not active implementation, packaging, or release gates unless the Plan explicitly says otherwise.
