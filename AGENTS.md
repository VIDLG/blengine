# AGENTS.md

## Purpose

This file defines the working rules for coding agents in the `blehub` project.
Read it before researching, planning, or changing the project.

## Current Project State

BleHub's architecture plan is approved; Phase 0 implementation is in progress, but its gate is not yet complete and the public ABI is not frozen.

- The single reviewed plan is [`.plan/PLAN.md`](.plan/PLAN.md).
- The plan status is `已批准`.
- Implementation must start from Phase 0 and follow the Plan's phase gates; do not skip ahead to the public API, production FFI bindings, platform backends, or BLE implementation.
- Project-authored research belongs under [`docs/research/`](docs/research/).
- Downloaded platform API evidence belongs under [`docs/platform-api-reference/`](docs/platform-api-reference/).

## Source Of Truth Order

When sources disagree, use this order:

1. Explicit instructions in the current user conversation.
2. This `AGENTS.md`.
3. The reviewed decisions in [`.plan/PLAN.md`](.plan/PLAN.md).
4. Project-authored research in [`docs/research/`](docs/research/).
5. Locally downloaded official platform API references in [`docs/platform-api-reference/`](docs/platform-api-reference/).
6. Relevant source repositories already present in the workspace.
7. External online material only when the required information is not available locally.

A research report records evidence and recommendations; it does not silently override the plan. Proposed architectural changes must be made explicit in the plan and reviewed by the user.

## Local-First Research Policy

### Always inspect local source first

When investigating a library, API, behavior, or architecture:

1. Search the current workspace first.
2. Use `codegraph` against the relevant local repository before using broad text searches.
3. Read the actual implementation and tests rather than relying on remembered behavior, package summaries, or third-party articles.
4. Record whether a conclusion is:
   - verified by local source or official API evidence;
   - inferred from that evidence;
   - still unverified and requires a spike.

Do not browse the web merely to get a newer or more convenient copy of material that is already available locally.

### Relevant local repositories

The workspace currently provides these source trees:

| Project | Workspace path | Use |
|---|---|---|
| BleHub | `D:\projects\blehub` | Current plan, research, tools, and future implementation |
| BoltFFI | `D:\projects\contrib\boltffi` | FFI generator/runtime implementation, generated bindings, packaging code, examples, and tests |
| espkit | `D:\projects\users\hyc\espkit` | Optional BLE domain reference, especially `crates/std/btleplus`; it is not BleHub's architecture baseline |

When investigating BoltFFI behavior, query `D:\projects\contrib\boltffi` directly. Do not infer capabilities only from generated API names or old discussion.

When investigating `btleplus`, follow `espkit/AGENT.md` and its `.planning/` source-of-truth rules. Reuse only domain structures or behavior that fit BleHub's deliberately thin architecture.

### Native platform API evidence

Official platform BLE Central references have already been downloaded to:

```text
docs/platform-api-reference/
├── windows/
├── android/
├── apple/
├── bluez/
├── README.md
└── sources.lock.json
```

Use these local snapshots before looking online:

- Windows: WinRT Bluetooth API documentation.
- Android: Android 36 Bluetooth Framework source and Javadocs. The API is Java-defined even though the backend will be written in Kotlin.
- Apple: CoreBluetooth documentation snapshots for iOS and macOS.
- Linux: BlueZ D-Bus API documentation.

The platform subdirectories are generated evidence. Do not hand-edit files under `windows/`, `android/`, `apple/`, or `bluez/`.

Refresh them only through:

```text
pixi run just docs-update
```

### Missing external source

If required information is absent locally:

- A small, narrowly scoped official page may be queried online only when it is genuinely needed and the local snapshots do not contain the answer.
- Prefer official specifications, vendor documentation, and upstream source over blogs or summaries.
- If a large library or repository is important enough that its implementation must be audited, do not clone or download the large repository automatically. Tell the user exactly which repository, revision/tag, and reason are needed, and ask the user to add it to the workspace.
- After the user provides the repository, initialize/query it with `codegraph` and inspect its source locally.
- Do not introduce a dependency based only on online documentation when its ownership, threading, runtime, FFI, or lifecycle behavior is architecture-critical.

## Project Architecture Guardrails

The current intended architecture is a thin cross-platform BLE Central contract:

- Rust owns the unified DTOs, command API, event API, and thin facade.
- `BleBackend` uses ordinary non-async methods.
- Commands are fire-and-forget:
  - synchronous `Result` means accepted/rejected for dispatch;
  - eventual platform/BLE outcomes are events.
- The caller owns command IDs, timeout policy, cancellation policy, stale/late/duplicate event handling, and business state.
- BleHub core owns no runtime, executor, worker thread, mailbox, timer, event loop, or canonical BLE device/GATT state.
- Backends own unavoidable platform handles, callback correlation, and serialized native GATT operations.
- In v0.1, native backends push events through exported `push_backend_event(BackendEventIngressToken, BleEvent)` and a process-local weak registry; migrate to a direct Rust-provided ingress handle when BoltFFI supports that foreign-trait parameter shape.
- Downstream supplies `BleEventSink` and decides whether events go to a channel, actor, UI loop, or runtime.

Current platform direction:

| Priority | Platform | Backend direction |
|---:|---|---|
| 1 | Windows | Rust + `windows-rs` |
| 2 | Android | Kotlin + Android Framework BLE + BoltFFI |
| 3 | iOS/macOS | Swift + CoreBluetooth + BoltFFI |
| 4 | Linux | Rust + `dbus-rs` + BlueZ D-Bus |

The Kotlin/JVM foreign trait object plus token ingress workaround has partial end-to-end Phase 0 evidence. Swift runtime/ARC, remaining lifecycle counters, fault isolation, packaging, and performance gates still require validation before the public ABI is frozen; the ideal direct ingress-handle shape remains blocked by BoltFFI #869.

## Dependency And Tooling Rules

- Rust dependencies are managed by Cargo once the Rust workspace is approved and created.
- External development tools are managed by Pixi.
- Project recipes are defined in `justfile`.
- `just` itself is managed by Pixi.
- Run project recipes through Pixi:

```text
pixi run just <recipe>
```

Do not assume globally installed Python, Ruff, Just, RTK, sccache, or other tools. Test recipes route verbose Cargo/Gradle output through Pixi-managed RTK; RTK is an output filter only and must preserve command selection, failure detail, and exit status. Pixi-managed sccache wraps ordinary Rust compilation and eligible C/C++ glue with Cargo incremental disabled to avoid duplicate caches; it does not reduce required final artifacts and does not cache Swift/Kotlin compilation. BoltFFI metadata generation must use a dedicated Cargo target and bypass sccache because its cfg/environment-driven macro output is not safely distinguished from ordinary Rust artifacts by the cache key; the CLI itself may remain in the ordinary cached target.

Machine-local toolchain roots resolve in this order: `BLEHUB_<TOOL>_ROOT`, ignored `blehub.local.toml`, then `~/.cache/blehub/toolchains/<tool>`. Copy `blehub.local.example.toml` for stable developer-machine paths and use environment variables in CI. Keep only paths in local config; pin versions, URLs, hashes, repositories, and license policy in tracked scripts and locks. Use `pixi run just toolchain-paths` to inspect effective values. Bootstrap/install recipes must be explicit; ordinary build and test recipes must not silently install Android SDK/NDK, Swift, or accept licenses.

Current recipes include:

```text
pixi run just docs-update
pixi run just docs-check-mermaid
pixi run just fmt
pixi run just lint
```

Prefer mature ecosystem libraries where they clearly reduce risk or maintenance. “Thin core” means limited responsibility, not a ban on dependencies. Lightweight boilerplate-reduction crates such as `derive_more`, `delegate`, or `getset` are allowed when they remove meaningful repeated code without obscuring ownership, FFI, validation, or lifecycle behavior. Do not add accessor/delegation macros merely to replace one or two obvious lines, and verify any attribute macro used in a BoltFFI-scanned crate against binding generation. Do not add a runtime or heavyweight framework that changes the caller-owned execution model.

## Documentation Rules

- Write and edit all project-authored documentation as UTF-8.
- Keep one reviewable architecture plan in [`.plan/PLAN.md`](.plan/PLAN.md).
- Put durable investigations and decision evidence in [`docs/research/`](docs/research/).
- Keep generated official snapshots separate from authored interpretation.
- When making factual claims, link to the relevant local source file or snapshot where practical.
- Distinguish platform guarantees from BleHub policy and from conclusions that still need runtime validation.
- Do not claim that a callback, completion, disconnect, write, shutdown, or cancellation has stronger semantics than the native platform provides.
- Future user-facing docs should be derived from the approved plan and verified implementation, not copied blindly from an unapproved draft.

## Change Discipline

- Inspect relevant files and existing user work before editing.
- Make minimal, focused changes.
- Do not modify vendored/generated code unless the task explicitly requires it.
- Do not broaden planning into implementation without approval.
- Do not commit or create branches unless explicitly requested.
- This directory may not yet be a Git repository; do not assume `git status` or `git diff` is available.

## Validation

Do not add tests for trivial field assignment, obvious constructors/getters, derived trait behavior, or direct delegation that has no project-specific invariant. Prefer tests that protect FFI/wire compatibility, validation boundaries, lifecycle and rollback semantics, concurrency linearization, platform mapping, or previously observed regressions. Combine closely related boundary cases when that improves signal and readability; test count is not a goal.

For documentation/tooling changes, run the narrowest applicable checks through Pixi. At minimum:

```text
pixi run just fmt
pixi run just lint
```

For authored Markdown, also verify:

- Mermaid fences with `pixi run just docs-check-mermaid` when diagrams are present;
- UTF-8 decoding;
- balanced fenced code blocks;
- no trailing whitespace;
- no merge-conflict markers.

For future Rust/platform implementation, add platform-specific build, unit-test, generated-binding, packaging-consumer, and lifecycle stress recipes to `justfile` as each phase begins.
