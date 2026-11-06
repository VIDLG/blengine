# Archived Windows Central example

> This example is retained for historical compatibility only. It is not an active example, package gate, or maintained product target.

## Historical example details

This example demonstrates the Rust-first BleHub consumer model on Windows:

- the application creates `WindowsBackend` and assembles `BleHub`;
- `BleEventSink` only dispatches events into an application-owned `std::sync::mpsc` channel;
- command IDs, scan session IDs, deadlines, timeout handling, and the event loop belong to the application;
- command return values only acknowledge synchronous dispatch acceptance or rejection;
- eventual Bluetooth outcomes arrive as `BleEvent` values;
- shutdown calls the idempotent `BleHub::dispose` explicitly.

Build it without accessing Bluetooth hardware:

```text
pixi run --justfile just/archive/windows/mod.just example
```

Run a service-filtered scan on Windows:

```text
cargo run --manifest-path examples/archive/windows-central/Cargo.toml -- \
  fff00000-0000-4000-8000-000000000000 10
```

The first argument is the required service UUID. The optional second argument is the caller-owned scan duration in seconds. The example uses an unbounded standard-library channel for clarity; production applications should select their own channel, actor, UI dispatcher, capacity, and backpressure policy.
