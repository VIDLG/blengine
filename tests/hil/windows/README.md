# Windows HIL runners

This standalone Cargo crate contains project-specific Windows BLE Central RF smoke and fault-injection runners. It is test infrastructure, not a consumer example or a release artifact.

Run it through the stable Pixi/Just recipes documented in [`docs/testing/windows.md`](../../../docs/testing/windows.md), for example:

```text
pixi run just windows scan-smoke 30 fff00000-0000-4000-8000-000000000000
pixi run just windows notification-smoke 30 fff00000-0000-4000-8000-000000000000 default android
pixi run just windows passive-disconnect-smoke 60 fff00000-0000-4000-8000-000000000000 android-radio connect-only
```

The crate is deliberately excluded from the root Cargo workspace so ordinary workspace tests and non-Windows portability lanes do not build or execute hardware runners. `pixi run just windows::check` compiles it explicitly without accessing Bluetooth hardware. Its fixed runner set remains directly under `src/` and is declared with explicit `[[bin]]` targets rather than Cargo auto-discovery; shared support provides bounded fixture subprocesses and aggregates scenario, disposal, and recovery failures.

Some cases control a physical Android radio or QEMU USB passthrough. Reserve the devices/controllers, use the provider-specific service UUID, and read the Windows testing guide before running destructive cases.
