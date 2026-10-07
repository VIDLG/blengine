# Tauri native E2E

This suite drives the actual Tauri application through its Rust embedded
WebDriver server. macOS uses ordinary WebDriver Classic with a project-owned
LaunchServices process owner; Windows/Linux retain `@wdio/tauri-service` with
the `embedded` provider. Neither path replaces the BLE contract or backend.

## macOS launch and ownership

[`macos-owner.js`](macos-owner.js) compiles and starts
[`launch-app.swift`](launch-app.swift), which uses standard
`NSWorkspace.openApplication` / LaunchServices to launch a new instance of the
actual debug/instrumented app bundle. The Swift owner retains the returned
`NSRunningApplication` on its main run loop. In [`wdio.conf.js`](wdio.conf.js),
macOS has `services: []` and explicitly uses WebDriver Classic; WDIO connects
to the same embedded Rust server rather than letting the npm service spawn the
bundle executable directly.

- Node obtains a temporary `127.0.0.1` port and releases its reservation for the
  app to bind. That handoff is not race-free: `lsof` must match the listener to
  the exact PID returned by LaunchServices, and `/status` must report ready.
  Listener identity is checked again before allowing session creation; a
  responding stale/foreign server is not readiness evidence.
- The stdin pipe is an ownership lease. Normal completion closes it; Node-parent
  `SIGKILL` also produces EOF, allowing the detached Swift owner to clean up.
  Cleanup targets the retained instance with `terminate`, then bounded
  `forceTerminate` if necessary. A stop during launch waits for the returned
  instance to clean it; unconfirmed launch/cleanup is reported as failure.
- This does not use `osascript`, Accessibility scripting, `pkill`, process-name
  matching, or port-based killing. Port/PID checks establish listener identity;
  they do not select a process to terminate.

The npm service has no official attach-to-existing-process option, but that is
not a limitation of ordinary WebDriver connectivity. This path replaces only
macOS process ownership, not the WebDriver protocol, embedded server, or TCC
security policy.

The embedded server is enabled only in the E2E host build through
`tauri-plugin-wdio-webdriver` when the explicit `wdio` Cargo feature is
enabled. Basic element interactions do not require the companion
`tauri-plugin-wdio`; the spec explicitly selects the current window to bypass
its optional focus-recovery path. The embedded server is a test-only
dependency and is not part of the production integration.

## Execution boundaries

Run from the repository root through the managed recipes:

```text
pixi run just tauri::tauri-native-e2e
pixi run just tauri::tauri-native-rf
```

Native is the default; deterministic demo requires explicit
`BLEHUB_TAURI_DEMO=1`. Demo and `BLEHUB_TAURI_RF=1` are mutually exclusive, and
non-macOS does not imply a fake provider. The RF opt-in drives the standalone nRF52840 Peripheral through scan,
connection, GATT discovery, read/write, notification, Peripheral-initiated
loss/recovery, and reconnection. The nRF provider owns the single BTP serial
transport and is started by the RF suite; the WebDriver process only exchanges
coordination markers. Without RF, startup, initialization, event projection, and
cleanup checks are not a substitute for real Central/Peripheral evidence.

RF-only `before` removes stale coordination markers and starts
`tests.peripherals.nrf52840.provider direct-start --coordination-mode tauri`.
`after` stops that provider and removes the coordination directory, including
partial-start failures. The provider continues advertising after a passive
GAP disconnect so recovery cycles use fresh BleHub IDs. Visible-flow waits
propagate early UI failure promptly; fault tests retain their `allowError`
semantics.

The independent owner lifecycle entry is:

```text
pixi run just tauri::macos-owner-test
```

It builds the app bundle and Swift owner before running
[`macos-owner.test.js`](macos-owner.test.js); it must not require a successful
native E2E run to produce those prerequisites. Owner tests open windows but
never click Initialize or issue BLE commands. Their coverage includes
exact-instance cleanup with a neighboring instance left alive, EOF during
pending launch, launch failure, unresponsive-app cleanup, parent death, and
listener identity. These are process-lifecycle checks, not BLE disposal/RF proof.

**Do not parallelize Apple preparation against another preparation or build
using the same staging.** The recipes share `.work/staged/apple-production`.
A coordinating-session parallel run caused a missing-directory failure; serial
recovery restored staging. The subsequent native iOS HIL signing retry still
failed with `errSecInternalComponent`, a separate signing blocker.

## TCC diagnosis and current evidence

System logs established the old direct-spawn failure: Bluetooth authorization
was attributed to `sshd-keygen-wrapper`, with `Policy disallows prompt`. Calling
that merely an unanswered user permission request obscures the launch defect.

The LaunchServices owner fixes that attribution. At **2026-09-09 15:38:30**, TCC
logged the app as responsible and `AUTHREQ_PROMPTING` / `display_prompt`. That
attempt timed out at 30 seconds while awaiting user response. After the user
allowed Bluetooth, native E2E passed **2 tests / 4 skipped**, without RF. The
macOS Tauri TCC blocker is now closed, with no relaxed readiness assertions.

For human authorization use `pixi run just tauri::macos-authorize`, click
**Initialize**, allow Bluetooth when prompted, then quit before the automated
run. Launching alone is not validation. Login-keychain access is separately
human-only via `pixi run just apple unlock-login-keychain`; no password is
passed to an agent.

The demo run remains **3 passed / 3 skipped**, with owner lifecycle scenarios
passed separately. Historical Android-backed Tauri RF results are retained only
as archived evidence and are not active nRF evidence.

The current nRF-backed run of `pixi run just tauri tauri-native-rf` passed
**6 tests / 0 skipped** in about **1m10s**. Coverage includes the visible flow,
two fresh-ID repeats, 10 scan/connect/GATT/read/disconnect cycles, in-flight
GATT discovery termination after a Peripheral disconnect, active-subscription
passive-disconnect recovery, stale-value silence, and dispose. The nRF provider
was ready/stopped by the recipe with one serial owner, and no Android radio
control or archive provider was used. This is not a long-soak result.

All **19 UI component tests** and the Tauri Rust tests passed before the RF run.
The detailed current evidence is in the [dated nRF report](../../../../docs/testing/reports/2026-09-13-tauri-macos-nrf-rf.md).

The latest serial native iOS `runtime-ios-android` retry still failed at signing
with `errSecInternalComponent` before RF. Historical iOS I/O RF is not a current
full matrix. Tauri iOS runtime/development-signing/RF remain unrun, and Windows
was not tested. See the [dated report](../../../../docs/testing/reports/2026-09-09-tauri-apple-dual-target-workflow.md)
for the distinct attempts and candidate boundary; this documentation update
does not rerun them.
