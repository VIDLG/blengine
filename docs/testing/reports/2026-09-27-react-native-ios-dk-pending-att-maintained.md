# Maintained iPhone RN pending-ATT read/reload gate

Date: 2026-09-27. Status: **optional private RN/Nitro iPhone + DK pending-read reload gate passed** on this unfrozen candidate. This does not freeze the public BLeng/RN ABI or replace normal Apple/native RF gates.

## Candidate and build provenance

- The BleHub-owned `tests/peripherals/nrf52840/diagnostic/build_dk.py` staged the tracked read-hold patch only under ignored `.work/nrf-att-hold-maintained-dk/`. The explicit `pixi run just peripheral nrf52840-att-hold-build` reused the locally configured `nrftest` west workspace, SDK 1.0.1 and Pixi tools **for build only**. Zephyr origin/HEAD `https://github.com/VIDLG/zephyr.git` / `b5f309bc2da57b49012648c9432d0d4cb5ee1cc8`; all listed west modules were verified at their pinned revisions and clean both before and after building. Reference Pixi lock SHA-256: `6d649e6735cca242996ba1a0f24929e770eaad1ada482604a5a1a41b3f22d3cd`.
- The validated **local** `nrf52840dk/nrf52840` HEX SHA-256 is `e7a66e9aebd81495fa77b88ccb15702df3dd0424cf2c192b02a4d68d5a906304`, byte-identical by hash to the historical spike's local HEX. The manifest and artifact live under `.work/nrf-att-hold-maintained-dk/build/`; `flash_performed=false`. Neither identical local hashes nor read-only BTP capabilities attest the DK's installed flash bytes. No new flash, SWD readback or reset was performed in this run.
- The signed physical-iPhone RN host build passed and its `BleHubNitroHost.debug.dylib` passed the iOS single-image symbol gate. This is not an isolated package-consumer gate.

## RF execution

The explicit `pixi run just react-native host-ios-device-rf-pending-read-reload` recipe ran against physical iPhone 14 Plus `00008110-000C61160C39401E` and nRF52840 DK J-Link serial `001050252028`. The normal nRF provider was stopped, the RN fixture lock was absent after completion, and the device was unlocked before the attempt. A read-only precheck found exactly one responding BTP VCOM with the private read-hold capabilities and a quiescent `RELEASED` state. The maintained runner installed the signed app and exclusively owned the BTP port; it reset only the disconnected Tester's dynamic GATT database and provisioned the isolated `fd100000-0000-4000-8000-000000000000` profile.

- Old generation's read entered `WAITING` for handle `41`; the host posted its UUID-scoped reload notification at that boundary. The old hold reached `EXPIRED` (sequence `23`) with a matching old-peer GAP disconnect. The app reported exactly one native old-read acceptance, `dispose()` return and synthetic closed-sink late-submit rejection in order.
- After re-advertising, generation 2 initialized, scanned, connected and rediscovered GATT; the fresh hold reached `WAITING` then `RELEASED` (sequence `24`) and the exact read returned `00`. Stale RELEASE/ABORT requests for sequence `23` were rejected without changing the fresh arm.
- The independent maintained ledger checker passed with old/new characteristic IDs `13,13`, run ID `3e69b09b-3a09-4eae-85ed-5e0ba28c9a69`, probe ID `b098158e-6ab5-4c74-a4ac-6c297f425387`, and no observed old-generation marker leakage. The complete 27-marker RN attempt log and companion runner log are retained locally as `.work/nrf-att-hold-maintained-dk/rn-pending-read-reload-3e69b09b3a094eae85ed5e0ba28c9a69.{attempt,runner}.log`. The runner exited successfully, verified cleanup and released its RN lock before printing `passed`.

This is observed RN JS → Nitro → production UniFFI/Apple backend RF and native dispose/synthetic closed-sink evidence. It does **not** show an actual CoreBluetooth late callback, drained already-submitted Nitro JS events, full `rf-io`, physical USB-loss recovery, independent installed-HEX readback, Android behavior or public BLeng ABI stability. The earlier [spike report](2026-09-27-react-native-ios-dk-pending-att-faults.md) remains historical evidence, not the maintained run.

## Static checks and archival

The hardware-free diagnostic suite passed 46 tests; `pixi run just fmt`, `pixi run just lint`, RN host TypeScript, signed host build, iOS single-image gate and independent ledger replay passed. The source-only spike was moved to `spikes/archive/nrf-att-hold/`; no maintained import or recipe references it. Daily production and normal nRF provider gates remain independent of `nrftest` and of the optional diagnostic firmware.
