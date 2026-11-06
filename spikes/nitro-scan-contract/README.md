# Nitro scan contract representation experiment

Status: **representation experiment for the approved scan design, not an implementation**.
This directory is not imported by production, the formal RN package, its consumer or any default
build. It does not implement scanning or freeze a JS ABI. The single planning authority remains
[`.plan/PLAN.md`](../../.plan/PLAN.md); approved scope and outstanding implementation checks are
in the [scan contract design](../../docs/research/react-native/react-native-bleng-scan-contract.md).

## Run without another dependency installation

From the repository root, with the existing formal integration's pinned dependencies available:

```sh
pixi run node spikes/nitro-scan-contract/check.mjs
```

The check uses the installed `integrations/react-native-bleng/node_modules` and verifies its
Nitrogen, Nitro runtime and TypeScript versions against the formal package manifest. It never
runs `npm install`, updates a lockfile, autolinks an implementation or touches formal generated
outputs. Missing/mismatched dependencies fail; there is no download or global-tool fallback.

Each attempt has an independent directory under `.work/nitro-scan-contract/` containing the
copied inputs, TypeScript/codegen logs and generated sources. Child commands have 45-second
deadlines; failed attempts remain for inspection. No App/device/backend is created.

## What is checked

- The candidate TypeScript typechecks.
- All **four** candidate HybridObjects generate for Swift and Kotlin; zero or partial generation
  is not counted as success.
- Generated signatures retain independent start waiting/cancellation, pull and stop submission.
- Generated C++ types include a record union, nested opaque peripheral handle, manufacturer/service
  arrays and `ArrayBuffer` payloads.
- A separate, valid-TypeScript negative example using `kind: 'advertisement'` must fail Nitrogen
  specifically because singleton string-literal fields cannot be represented. A missing dependency
  or unrelated generator error does not satisfy this negative check.

The positive candidate uses distinct required keys (`advertisement`, `ended`, `failure`) instead
of singleton string-literal discriminants. It is a representation experiment, **not** evidence of
exact error mappings, variant runtime dispatch, binary round-trip, bounded queue behavior,
cancellation, cleanup, native compilation or BLE/RF success. Those need implementation and their
own tests after contract review. The formal `BlengSessionOwner.nitro.ts` and exports are unchanged.
