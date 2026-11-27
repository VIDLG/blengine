# Isolated nRF52840 pending-ATT read spike

Archived historical experiment. Commands below refer to the original `spikes/nrf-att-hold/` location and are not maintained entry points. For the optional maintained DK/iPhone gate, see [`tests/peripherals/nrf52840/README.md`](../../../tests/peripherals/nrf52840/README.md) and the [maintained RF report](../../../docs/testing/reports/2026-09-27-react-native-ios-dk-pending-att-maintained.md); do not run the archived flash scripts as a current recipe.

Status: **isolated DK flash, BTP control-only smoke and iPhone RN pending-ATT initial-read RELEASE/ABORT/EXPIRED, BTP control-session-loss, and same-host two-generation RN engine-reload RF feasibility runs passed on 2026-09-27**. The latest reload candidate passed a fresh generation-2 read after the old hold expired and GAP disconnected, and separately observed native dispose return and test-only synthetic old-read rejection by the closed sink. This is not an actual CoreBluetooth late-callback or already-handed-to-Nitro JS drain proof, a full `rf-io`, physical USB-loss/fault, or maintained production-provider pass. The existing default Dongle fixture and production ABI are untouched. See the [initial-read](../../../docs/testing/reports/2026-09-27-react-native-ios-dk-pending-att-initial-read.md) and [fault-boundary](../../../docs/testing/reports/2026-09-27-react-native-ios-dk-pending-att-faults.md) reports.

The current Zephyr Tester returns directly from its read callback; its write-value event does not mark a pending ATT response. See [the feasibility audit](../../../docs/research/react-native/react-native-pending-att-fault-feasibility-2026-09-27.md). This spike tests whether a bounded, one-shot firmware read barrier can be controlled over the *same* single-owner BTP serial session while the ATT callback is held. This is an intentionally risky, nonproduction violation of Zephyr's nonblocking GATT callback guidance.

## Inputs and isolation

- Pinned source: `VIDLG/zephyr` commit `b5f309bc2da57b49012648c9432d0d4cb5ee1cc8` from `nrftest/upstream.lock.toml`. No automatic download or vendor-source edit.
- [`firmware/0001-bttester-gatt-read-hold.patch`](firmware/0001-bttester-gatt-read-hold.patch) targets the Tester source only. The option is off by default. [`build_diagnostic.py`](build_diagnostic.py) builds a patched copy, never the shared Zephyr checkout, and validates Kconfig, application flash ranges and output hashes. The built image is **not** RF-tested.
- [`read_hold.py`](read_hold.py) is a hardware-free host protocol controller that accepts a caller-owned synchronous BTP session; it never opens another serial port and is not imported by the maintained provider.
- The normal `nrftest/tools/build_firmware.py` hardcodes a pristine pinned Tester and canonical config and records `tester_patch: null`. Do **not** use its normal build/package output for the patched variant. The spike copies just the Tester app into ignored `blehub/.work/nrf-att-hold`, builds against the pinned pristine Zephyr base, and writes a separate diagnostic manifest; it does not overwrite the stock build manifest or DFU ZIP.
- [`package_diagnostic.py`](package_diagnostic.py) verifies that manifest, canonical stock HEX/package identity, pinned tool identity, flash bounds and Nordic parser result before writing a separate DFU ZIP. This ZIP is **not** automatically flashed.

Use the configured `nrftest` Pixi environment for the spike's build and tests (it supplies `west`, `dtc`, and `intelhex`; the BleHub default Pixi environment does **not** supply `intelhex`). Resolve the Zephyr checkout and SDK paths from `pixi run just toolchain-paths` in `nrftest`. From `nrftest`:

```sh
pixi run python -m unittest discover -s ../blehub/spikes/nrf-att-hold -p 'test_*.py' -q
pixi run python ../blehub/spikes/nrf-att-hold/build_diagnostic.py \
  --zephyr-root /path/to/pinned/west-workspace/zephyr \
  --sdk-root /path/to/pinned/zephyr-sdk-1.0.1 \
  --nrftest-root /path/to/nrftest
pixi run python ../blehub/spikes/nrf-att-hold/package_diagnostic.py \
  --package-only --nrftest-root /path/to/nrftest
```

Each step rejects unexpected source/output identity. The build refuses to overwrite an existing `.work/nrf-att-hold` tree; do not remove it without checking ownership and saving any evidence first. These commands have **no flash or USB action**. The package gate currently also checks the exact known-good local stock manifest/package hashes, and fails closed if that local stock artifact changes.

This Mac's validated isolated artifact identities (not a claim of installed firmware):

| Artifact | SHA-256 |
| --- | --- |
| Diagnostic HEX | `0bf698d8efe07253b58adea20ee49333f83f20205f3428cb1ee1fb69d87551be` |
| Diagnostic DFU ZIP | `d11c9fad11623551230d5bdc79d7d50a8b8b6d73b53bf89c70756e54d32db36c` |
| Standard restore DFU ZIP, kept under `nrftest/.work/build/pca10059-tester/` | `d27fc6562febc070a928376ce765c3a07ca0c5ec6adf7aada2fa669f75f75ff7` |

This build validated `CONFIG_BTTESTER_GATT_READ_HOLD=y` and flash segments ending below `0xe0000`; the diagnostic DFU ZIP was normalized and parsed by the pinned Nordic tool. The standard ZIP hash was rechecked after packaging. The Mac subsequently reported only the standard `2FE3:0004` application port (stock BTP doctor passed); no `1915:521F` bootloader port appeared.

[`flash_diagnostic.py`](flash_diagnostic.py) is a separate, explicitly invoked diagnostic/restore flasher. It verifies the selected ZIP and the known-good stock package, pinned managed nRF Util, the exact `1915:521F` bootloader serial and port (twice) before a single DFU command. It cannot enter the bootloader on its own. With the **observed** unique bootloader port and serial selected after a physical RESET/SW2 press, and only after checking the ZIP identity above, run from `nrftest` using its Pixi environment:

```sh
pixi run python ../blehub/spikes/nrf-att-hold/flash_diagnostic.py \
  --mode diagnostic --nrftest-root /path/to/nrftest \
  --port /path/to/observed/bootloader-port \
  --bootloader-serial observed-bootloader-serial \
  --confirm-sha256 d11c9fad11623551230d5bdc79d7d50a8b8b6d73b53bf89c70756e54d32db36c
# After the experiment, press RESET/SW2 again and use the same command with:
# --mode restore --confirm-sha256 d27fc6562febc070a928376ce765c3a07ca0c5ec6adf7aada2fa669f75f75ff7
```

Do not supply the application port, infer bootloader presence from the app's normal USB serial, or execute this command until physical bootloader identity is visible. Flash command success alone is not a doctor/profile smoke, and a return to application mode does not prove the exact flashed image. Record both post-flash identity and control/protocol observations. If the physical RESET transition cannot be performed, stop before flashing and retain the standard fixture.

`read_hold.py` sends private GATT BTP opcodes `0x24`–`0x27`. It requires the firmware-issued sequence and target handle to match on every status response, and refuses release unless the exact callback reached `WAITING`. Status is not an oracle for ATT response delivery to iOS. See [the firmware protocol](firmware/README.md). The main fixture and RF runner deliberately do not recognize these opcodes.

## DK/PCA10056 path (separate from Dongle DFU)

[`build_dk.py`](build_dk.py) has built and validated an isolated `nrf52840dk/nrf52840` diagnostic HEX at `.work/nrf-att-hold-dk/build/zephyr/zephyr.hex` (SHA-256 `e7a66e9aebd81495fa77b88ccb15702df3dd0424cf2c192b02a4d68d5a906304`). It uses Tester’s DK UART0/J-Link VCOM with hardware flow control and RTT logging. The DK J-Link flash, BTP control-only smoke, bounded Central initial-read experiment, and isolated two-generation RN engine-reload RF gate below passed. Do not use the Dongle ZIP or Dongle DFU script on the DK.

Before any flash, connect the DK **debugger/J-Link USB** port with a data-capable cable, power on the DK, and check for SEGGER USB VID `1366`, one uniquely identified J-Link USB device with numeric serial (it can expose two `/dev/cu.usbmodem*` VCOM ports), and (if enabled) a JLINK volume. On 2026-09-27 an initial check saw none of these; after reconnecting to the debugger USB, the Mac saw SEGGER `1366:1061`, probe serial `001050252028`, two VCOM ports and a `JLINK` volume. `JLinkExe` was not installed at that point. It was subsequently manually installed via the official macOS SEGGER PKG: `pkgutil` reports `com.segger.pkg.JLink` version `9.80.`, owns `/Applications/SEGGER/JLink_V980/JLinkExe`, and Pixi can resolve `/usr/local/bin/JLinkExe`. `nrftest` owns the read-only `pixi run just verify-jlink` receipt/binary gate; J-Link itself is system software, not a Pixi dependency. The manually downloaded PKG bytes have not been independently hash-verified by this gate. USB enumeration and tool installation are **not** BTP or RF passes. The tool is owned by the Peripheral fixture project; it is a spike prerequisite here, not an automatic DK flash. Never assume the debugger USB presence proves that SWD points to the onboard SoC rather than an externally connected target. The DK's original application has not been backed up: **flash only when replacing it is acceptable**. There is no validated stock DK restore image in this spike.

[`flash_dk.py`](flash_dk.py) validates the pinned build manifest, inputs, artifacts, HEX ranges and exact hash, then checks the same SEGGER VCOM identity before a single explicit `west flash --runner jlink --dev-id <serial> --no-erase` command. The pinned board's *default* flash runner is `nrfutil`; do not omit the J-Link selection. Example **only after** physically confirming the target and J-Link serial:

```sh
pixi run python ../blehub/spikes/nrf-att-hold/flash_dk.py \
  --probe-serial OBSERVED_NUMERIC_JLINK_SERIAL \
  --confirm-hex-sha256 e7a66e9aebd81495fa77b88ccb15702df3dd0424cf2c192b02a4d68d5a906304 \
  --confirm-target nrf52840dk/nrf52840
```

Run this from `nrftest`'s Pixi environment. Command success is **not** a BTP doctor, app-image readback, pending ATT or RF pass. After flash, explicitly verify VCOM/BTP identity and restore/recovery policy. On macOS, J-Link VCOM usually appears as `/dev/tty.usbmodem*` (use the matching `/dev/cu.usbmodem*` for outbound host serial). See the local [Zephyr DK board documentation](../../../../contrib/zephyr/boards/nordic/nrf52840dk/doc/index.rst) and [Nordic J-Link transport documentation](../../../../contrib/zephyr/doc/develop/flash_debug/nordic_segger.rst).

### Observed DK control-only smoke (2026-09-27)

With the explicit probe serial `001050252028`, the validated diagnostic HEX hash above, and `--confirm-target nrf52840dk/nrf52840`, `flash_dk.py` completed its J-Link command and requested a reset. There was no firmware readback; flashing **replaced the previous DK application**, which was not backed up. An independent `nrftest` `verify-jlink` passed for the manually installed 9.80 PKG.

One serial owner at a time opened each observed VCOM with 115200 8N1, hardware flow control and exclusive pyserial access. `/dev/cu.usbmodem0010502520281` returned a valid BTP Core supported-services response (`0x2000000f`, including GAP and GATT); `/dev/cu.usbmodem0010502520283` returned no valid BTP Core frame (EOF). This is an observation on this board, **not** a general VCOM suffix rule. In a new single-owner session, the GATT supported-commands mask was `0xfffdbffffe` (including the private hold opcodes), and `READ_HOLD_STATUS` returned `IDLE`.

A subsequent BTP-only run reset the dynamic GATT server, added a single characteristic with read and read-authorization permissions `0x41`, initialized its value and started the server. `GET_ATTRIBUTES` returned UUID `fff50000-0000-4000-8000-000000000000` at value handle `41` with permissions `65`. `ARM` returned `(sequence=1, handle=41)` and STATUS `ARMED`; aborting this unused arm returned STATUS `ABORTED`; a duplicate direct ABORT returned BTP error opcode `0x00`, payload `01`. This proves only the control path for an **unused arm**: there was no BLE connection, ATT read callback entry, `WAITING`, Central result, RN reload, or RF evidence. The probe exited with the arm aborted and closed its serial owner. The subsequent [iPhone RN diagnostic initial-read run](../../../docs/testing/reports/2026-09-27-react-native-ios-dk-pending-att-initial-read.md) used [`run_dk_initial_read.py`](run_dk_initial_read.py) to confirm `WAITING`, immediately recheck STATUS, release the exact sequence/handle and independently observe the RN initial `characteristicRead` and `commandCompleted` before a marker with exact `00` value. It intentionally does not count the later one-shot-authorization readback error as a full `rf-io` pass. The subsequent [iPhone RN fault-boundary runs](../../../docs/testing/reports/2026-09-27-react-native-ios-dk-pending-att-faults.md) also observed `WAITING → ABORTED` and `WAITING → EXPIRED` with independent Central error terminals, stale-sequence rejection, and a new connection/read after the faults. With the iPhone's stale DK pairing forgotten, a further run passed BTP **control-session close/reopen while WAITING** followed by expiry, an RN error terminal, stale-control rejection and a fresh-launch RELEASE read. Unexpected BLE disconnect while held, physical USB transport loss, and repeated destructive recovery remain unrun. The latest same-host RN engine-reload gate below passed for its JS/event-ledger, native dispose-return and synthetic closed-sink rejection boundaries; it does not prove that native code never generated an actual late CoreBluetooth callback or establish full callback quarantine.

The isolated runner accepts `--action release|abort|expire|transport-loss` with an explicit iPhone UDID, J-Link serial and diagnostic HEX SHA-256. It uses `pyserial` to enumerate only VCOMs belonging to that J-Link USB identity, opens them one at a time with exclusive access, and selects the **unique** valid BTP Core responder; `--port` is optional and only asserts the result. It rechecks the USB identity after probing. It does not infer the BTP endpoint from a `/dev/cu.usbmodem*` suffix or use J-Link SWD to access the BTP UART. Run it from `nrftest`'s Pixi environment; each action keeps UUID-separated ignored local attempt/console logs. No action is a full RN `rf-io` pass, and the script does not reflash the DK. The `transport-loss` action closes and reopens the BTP serial owner while the ATT read is held; it is **not** USB unplug, device power loss, or SWD reset. The held-read BTP control-session-loss experiment passed on 2026-09-27; see the [fault-boundary report](../../../docs/testing/reports/2026-09-27-react-native-ios-dk-pending-att-faults.md). The separate same-host reload gate is run by [`run_dk_pending_read_reload.py`](run_dk_pending_read_reload.py) and does not establish behavior under physical USB disconnect, a native late callback, full `rf-io`, or a maintained provider.

## Same-host RN engine reload / two-generation gate (2026-09-27)

Using the unchanged [`run_dk_pending_read_reload.py`](run_dk_pending_read_reload.py), the validated diagnostic HEX SHA-256 `e7a66e9aebd81495fa77b88ccb15702df3dd0424cf2c192b02a4d68d5a906304`, and J-Link serial `001050252028`, the **latest candidate** passed a real iPhone two-generation RF gate. The runner validates the SHA and target identity; it does not flash the DK. Primary ignored evidence is `.work/nrf-att-hold-dk/rn-pending-read-reload-d8792205409b406cb2bca82fa94bc917.attempt.log` with its companion `.runner.log`; the old `34455fd4a5784a1ba5361751b6264237` attempt remains an earlier JS-boundary-only pass. See the [fault-boundary report](../../../docs/testing/reports/2026-09-27-react-native-ios-dk-pending-att-faults.md) for evidence scope. For this candidate the signed Xcode iPhone device build passed (reported separately from the RF log) and 57 hardware-free spike tests passed. The built device host's `BleHubNitroHost.debug.dylib` subsequently passed the iOS UniFFI single-image symbol checker; that symbol check does not establish an independent package/clean-consumer pass.

After rebuilding the signed RN device host, with the iPhone unlocked and this exact diagnostic image already on the DK, run from `nrftest`'s Pixi environment (the runner installs the built app but does not flash or reset the board):

```sh
pixi run python ../blehub/spikes/nrf-att-hold/run_dk_pending_read_reload.py \
  --udid 00008110-000C61160C39401E --probe-serial 001050252028 \
  --confirm-hex-sha256 e7a66e9aebd81495fa77b88ccb15702df3dd0424cf2c192b02a4d68d5a906304
```

These are the identities of the *observed* run, not defaults for another iPhone or J-Link. The runner requires a free fixture lock and unique BTP responder; it does not steal another fixture's process.

“Generation 1” and “generation 2” mean the RN JavaScript/native probe instances before and after **one engine reload**, not old and new versions of the BleHub API. The latest run's old read used command `12008`, connection `12004`, GATT session `12006`, characteristic `13`: the native `read-accepted` marker preceded JS `read-dispatched`. All three native markers carry exact `runId=d8792205-409b-406c-b2bc-a82fa94bc917` and `probeId=34b60b5a-e045-490b-b309-74a75e7e0a79`. The run's firmware/control result was old sequence `21` → `EXPIRED` and old GAP disconnect; these states are not printed in the RN attempt log. After the host posted `RCTTriggerReloadCommandListeners` during the pending read, `generation=1:cleanup-started` preceded `native:dispose-returned` and `native:late-submit-rejected` for that same old read, both before generation 2's `arm-ready`. The latter is a private test-only [`HybridBleHubProbe.swift`](../../../integrations/react-native-internal/ios/HybridBleHubProbe.swift) `NotificationCenter` diagnostic from a synthetic `.characteristicRead` submitted directly to the closed sink, **not** an actual CoreBluetooth late callback. The dispose-return marker independently establishes return from native `BleHub.dispose()`, but neither marker drains callbacks already handed to Nitro's JS queue. After explicit advertising restart, generation 2 used command `13008`, connection `13004`, GATT session `13006`, characteristic `13`; the run's new sequence `22` → `RELEASED` and the RN read returned `00`.

The native `arm-notification-received` barrier was accepted and the JS `Linking` listener observed the matching event (`arm-linking-dispatched:accepted=true` followed by `arm-js-observed:source=linking:url=matched`). The ordered ledger passed generation correlation with no old-ID leakage in the **observed JS/event stream**. This is bounded RF recovery plus native dispose-return/closed-sink rejection, not proof that actual native late callbacks never happen or previously submitted Nitro callbacks drained, nor a full `rf-io`, physical USB-fault, or maintained production-provider result.

Earlier attempts failed this gate because of a one-second-over-strict time limit, missing restart of connectable advertising after the first GAP disconnect, and an RN Settings cache that was not live; none is counted as a pass. The final `Linking` event path passed. The separate non-RF reload-signal check passed with a `0.153 s` response; that timing is not an RF result.

## Hardware feasibility gate before a maintained RN reload gate

1. For a **Dongle**, recheck the manifest, diagnostic package hash, standard restore package hash and physical target identity immediately before flashing. The USB application port `2FE3:0004` is **not** the DFU port. The pinned [PCA10059 board instructions](../../../../contrib/zephyr/boards/nordic/nrf52840dongle/doc/index.rst) require physically pressing RESET/SW2 to enter the `1915:521F` bootloader (red LED fades). The current Mac has no configured external J-Link; no safe software-only transition has been verified. Require the unique configured bootloader serial, explicit port, and confirmed ZIP hash for **both** diagnostic flash and standard restore. If the bootloader identity or restore path is unavailable, do not flash. Do not use the `nrftest` standard `firmware-flash` recipe for the diagnostic ZIP: it intentionally selects only its own stock manifest/package.
2. With an exclusive, verified BTP serial owner, configure exactly one diagnostic readable value (`0x40` authorization bit), arm its value handle, dispatch a Central read, and observe matching `WAITING` well inside the firmware's **5-second default** bounded hold. Confirm the BTP status and release/abort commands actually complete while the ATT callback is waiting. Explicitly verify the Central read outcome. Avoid doing BTP operations on the target attribute while armed.
3. Repeat with deliberate abort, timeout, stale/duplicate sequence, unexpected disconnect, transport loss and recovery. Do not count a snapshot of `WAITING` as proof that the callback was still held later; correlate timestamps and immediately recheck before the fault. On failure, allow the bounded hold to expire and reset/recover externally if needed. The Dongle has a separately validated stock restore ZIP; this DK spike has **no backed-up original application or validated stock DK restore image**, so do not claim one.
4. Before promoting this isolated passing RN pending-read reload experiment into a maintained gate, complete the approved staged graduation criteria in [the plan](../../../.plan/PLAN.md) and establish a BleHub-owned diagnostic firmware/control surface with exact old/new generation and terminal ledger checks. The provider-owned protocol/profile extraction is hardware-free and does not promote the spike RF run by itself. Write-with-response is a distinct second experiment, not inferred from read.

The patch does not yet identify a particular BLE connection inside its status response or guarantee disconnect callback execution while the ATT callback is blocked. Restrict this experiment to a single connection and treat disconnect during hold as a potentially destructive diagnostic, not as an automatic-cleanup guarantee. If those limits cannot be removed or verified, **do not promote it to an RN RF oracle**.
