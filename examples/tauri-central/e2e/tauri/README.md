# Tauri native E2E

This suite uses WebdriverIO's official `@wdio/tauri-service` with the
`embedded` provider. The provider starts the WebDriver server inside the Tauri
application, so it drives macOS `WKWebView` without `tauri-driver` or
Accessibility scripting. The same configuration remains usable on Linux and
Windows.

The embedded server is enabled only in the E2E host build through
`tauri-plugin-wdio-webdriver` when the explicit `wdio` Cargo feature is
enabled. Basic element interactions do not require the companion
`tauri-plugin-wdio`; the spec explicitly selects the current window to bypass
its optional focus-recovery path. The embedded server is a test-only
dependency and is not part of the production integration.

```text
pixi run npm install --no-audit --no-fund   # from this directory
pixi run npm test                            # from this directory
```

The spec selects the real macOS `Initialize CoreBluetooth` flow on macOS and
the deterministic fake contract flow on non-macOS hosts. With
`BLEHUB_TAURI_RF=1`, the suite drives the Android Peripheral fixture through
scan, connection, GATT discovery, notification, radio-loss recovery, and
reconnection; without that opt-in it verifies startup, event projection, and
process-owned disposal. The service's optional focus-recovery path is skipped
by the explicit window selection in the spec, avoiding its IPC warning and
unnecessary delay. WDIO 1.3.0 may still emit a non-fatal mock-store cleanup
warning after deleting the WebDriver session; element assertions and
application cleanup still pass.
