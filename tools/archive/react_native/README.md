# Archived RN diagnostics

This directory preserves superseded diagnostic source, not maintained RF gates.
There is no active Just recipe for these entries. Historical reports retain their
original commands and candidate boundaries; moving source does not change those
results or make them current architecture evidence.

## Local-HEX iPhone pending-read reload

`run_ios_pending_read_reload.py` required an explicitly confirmed local HEX and a
matching older diagnostic host/profile. It did not provide the current frozen
artifact/live-attested multifunction evidence boundary. Its CLI refusal test is
retained alongside it for historical reproduction, outside the maintained RN
tooling discovery.

Use the maintained gate instead:

```text
pixi run just react-native host-ios-device-pending-read-reload <udid> <team> <fixture-contract> <fixture-profile>
```

The replacement is not an argument alias: device/contract/profile/live receipt
and installed App evidence must be established anew. Do not run the archived
controller against a current App and infer support merely from an old pass.

The `tests/peripherals/nrf52840/diagnostic` directory is **not** wholly archived.
Current attestation and the read-only doctor still use its J-Link/BTP helpers;
read-hold commands also support the current multifunction image. Do not delete
those live dependencies with this old iPhone entry.
