# iPhone RN + nRF52840 DK: bounded pending ATT initial read

Date: 2026-09-27. Status: **one diagnostic RF initial-read feasibility pass**, not a full `rf-io`, RN reload/invalidation, stale-callback, passive-disconnect, or production-fixture pass.

## Candidate and boundary

- Central: available physical iPhone 14 Plus `00008110-000C61160C39401E`, existing React Native/Nitro iOS host app and production UniFFI/Apple backend. The JS read path is [`App.tsx`](../../../tests/consumers/react-native-internal/src/App.tsx) `readRfCharacteristic` (`commandId=9010`); the marker is emitted only after exact value, event identity and command completion have been checked.
- Peripheral: nRF52840 DK `nrf52840dk/nrf52840`, J-Link `001050252028`, diagnostic HEX SHA-256 `e7a66e9aebd81495fa77b88ccb15702df3dd0424cf2c192b02a4d68d5a906304`. The [isolated build/firmware protocol](../../../spikes/archive/nrf-att-hold/README.md) and [`flash_dk.py`](../../../spikes/archive/nrf-att-hold/flash_dk.py) guard this candidate. The J-Link flash command succeeded and requested a reset; no installed-flash readback or previous DK application backup is claimed.
- One exclusive 115200 8N1 hardware-flow-control BTP owner on observed `/dev/cu.usbmodem0010502520281`; the other VCOM did not return a BTP Core frame. A separate provider fixture process was not running. Diagnostic service `fd100000-0000-4000-8000-000000000000` is distinct from the normal nRF service. Only its read-write value is provisioned with read authorization (`0x43`) so later reads do **not** inherit the normal profile's success guarantee.
- Experiment runner: [`run_dk_initial_read.py`](../../../spikes/archive/nrf-att-hold/run_dk_initial_read.py). It uses the existing RN `rf-io` launch **only to trigger the first read**; it does not reuse that mode's final pass marker as an oracle for this one-shot experiment. Host logs are local ignored artifacts under `.work/nrf-att-hold-dk/`.

## Observations

1. Separate control-only smoke after the flash returned supported services `0x2000000f`, GATT command mask `0xfffdbffffe` with private opcodes, and `READ_HOLD_STATUS=IDLE`. A read-authorized attribute at handle `41` accepted ARM; an unused ARM was aborted, and a duplicate ABORT returned BTP error. This was not RF evidence.
2. The first physical run observed `WAITING → RELEASED` for `(sequence=2, handle=41)`, then exited **2** because the experiment checker incorrectly expected connection ID `9006`. The RN attempt log actually recorded `connectionId=9007` for the initial read; `9006` is the disconnect **command** ID in `App.tsx`. The checker was corrected and its unit tests plus offline recheck of the first log passed. Do not count the original exit-2 run as a gate pass.
3. The corrected physical rerun exited **0**. The DK's same BTP session observed `WAITING`, immediately rechecked it, sent RELEASE for `(sequence=3, handle=41)` and observed `RELEASED`. The independently captured RN console contained exactly one checked initial-read marker:

   ```text
   BLEHUB_NITRO_RUNTIME rf:step:read-initial:passed:commandId=9010:connectionId=9007:gattSessionId=9009:peripheralId=0:serviceUuid=fd100000-0000-4000-8000-000000000000:characteristicId=13:value=00
   ```

   `App.tsx` emits this marker after matching the `characteristicRead` event, checking its one-byte `00` value, and receiving the matching `commandCompleted` after the read. This is application-boundary evidence of a released pending read, not a firmware status being substituted for the Central result.
4. As expected, the same RN `rf-io` run proceeded past a successful write (`commandId=9011`) and failed its **second** read (`commandId=9012`, permission error) because the diagnostic authorization is one-shot. Its console runner returned **1** and terminated the app; the isolated experiment printed `runtime_exit=1` and did **not** claim a full `rf-io` pass. No RN disconnect/close ledger, reload boundary, late callback quarantine, or new-generation recovery was validated in this run. The experiment lock was absent after exit.

## Next gate

Keep the diagnostic code under `spikes/` until failure paths (ABORT while WAITING, expiration, stale sequence, transport loss, reset/recovery) and a dedicated **single-read** Central mode have been validated. The dedicated mode must coordinate the same BTP owner with RN read dispatch, bound the 5-second firmware hold independently from JS command timeouts, recheck `WAITING` at the fault boundary, and require exact domain-before-terminal accounting, teardown, silence and fresh-generation recovery. Do not promote this test-only firmware or current runner to the maintained provider or count it as RN reload evidence without reviewing the plan.
