# Web UI tests

`basic-flow.spec.js` runs the React/Astryx page in Playwright's bundled
headless Chromium with a test-only `window.__TAURI__` bridge. It verifies the
visible basic-flow state, projected event log, and a screenshot baseline. It
does not claim that the native Tauri bridge or BLE backend is working. The
native Tauri WebDriver suite is separate under `../../e2e/tauri/` and is only
available on Linux/Windows because `tauri-driver` does not support macOS.
