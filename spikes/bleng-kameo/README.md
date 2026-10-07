# BLeng Kameo spike

This is a standalone, non-workspace spike for evaluating Kameo as the actor and
bounded-mailbox implementation behind the future BLeng async facade.

It is intentionally not production code and is not referenced by the root Cargo
workspace, the RN integration, or long-lived test paths.

## Run

```text
cargo test --manifest-path spikes/bleng-kameo/Cargo.toml
cargo clippy --manifest-path spikes/bleng-kameo/Cargo.toml --all-targets -- -D warnings
```

## What this proves

- a Kameo actor can use an explicit bounded mailbox;
- mailbox overflow and mailbox wait timeout are observable rather than silently
  dropping messages;
- BLeng-style waiter correlation remains application logic above Kameo;
- two peripheral connections can interleave without cross-completing waiters;
- timeout, cancellation, duplicate/late events, and connection generations are
  fail-closed;
- an actor handler registers and dispatches work without waiting for a BLE
  callback;
- graceful actor shutdown is explicit and idempotent for the BLeng owner.

Kameo does not provide BLE operation correlation, native cancellation semantics,
connection/GATT generations, or cleanup of native resources. Those remain BLeng
responsibilities. In particular, this spike does not treat an actor restart as
safe recovery for a live `BleHub`; a future production implementation must fail
closed and explicitly dispose the underlying hub instead.
