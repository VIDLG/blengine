# Testing reports

This directory is a date-prefixed evidence ledger. Each report records the exact candidate, host/device/fixture context, observed result, failures, cleanup state, and remaining limits for one validation attempt or review checkpoint.

Reports remain flat and chronological on purpose. Do not reorganize them into platform folders or rewrite an older report to make a later candidate appear to have passed. A later report may supersede a conclusion for a new candidate, but the original facts and failures remain historical evidence.

## How to use reports

- Use [`.plan/PLAN.md`](../../../.plan/PLAN.md) for normative requirements, phase gates, and acceptance conditions.
- Use [`AGENTS.md`](../../../AGENTS.md) for the maintained evidence navigation and current working boundaries.
- Use [`../README.md`](../README.md) for execution commands and test-layer ownership.
- Treat `passed`, `failed`, `blocked`, `pending`, and `unverified` as scoped to the recorded artifact, host, platform, fixture, and session.
- Do not infer a release, ABI, platform, RF, or cleanup gate from a report outside its recorded scope.

New reports should link to the relevant implementation/research entry points and should preserve primary failures separately from cleanup or infrastructure failures. Do not copy an entire report into a second summary document; update the appropriate navigation pointer when a current entry changes.
