# iOS Central recreation recovery diagnostic

Date: 2026-10-30

Status: diagnostic evidence only; not a system-loss gate pass.

## Scope

This candidate evaluates whether the existing `BlengAppleIOSProbe` can recover
CoreBluetooth advertisement delivery after replacing its native
`CBCentralManager`. It does not toggle the iPhone Bluetooth radio, modify
firmware, connect to GATT, enable notifications, or claim same-owner recovery.

The maintained entry point is:

```text
bleng-ios-rf-central-recovery-spike
```

The App mode is explicitly opt-in:

```text
--rf-central-recovery-spike
```

The implementation remains in the existing HIL App and uses the private Apple
SPI `diagnosticRecreateCentral`. It is not part of the Stable UniFFI ABI.

## Real non-destructive candidate

Run ID:

```text
a855509a-d387-4cf1-9e5c-3ecf0b2842e3
```

Fixture:

```text
contract=multifunction-rf-v2-b
profile=multifunction-rf-v2-b
image_sha256=f101a5c4b13b2c869848edd6f0bd563543ade85728cd9f3fa6ffce71430e96f5
```

Observed result:

```text
first_matching_advertisements=2
central_recreation.recreated=true
central_recreation.state=CBManagerState(rawValue: 5)
second_matching_advertisements=2
outcome=advertisement-observed
production_gate_claim=none
```

The provider was stopped after the run. The diagnostic JSON is retained under
the ignored candidate work directory:

```text
.work/apple-bleng-ios-backend/a855509a-d387-4cf1-9e5c-3ecf0b2842e3-central-recovery-spike.json
```

## Production system-loss candidate after fallback integration

Run ID:

```text
2b818da4-6691-4cf2-a84d-d74a37ebe990
```

The real candidate passed Bluetooth OFF, the provider disconnect receipt, old
operation terminals, Bluetooth ON restoration, and UI cleanup. The bounded
fallback did execute a fresh scan after native Central recreation, but no new
advertisement callback arrived. No new connection, GATT session, subscription,
or EF notification was observed.

Therefore:

```text
system-loss gate=not passed
recovery outcome=platform-timeout
```

The phone was restored ON and the provider ended stopped. This result must not
be relabeled as a firmware, sniffer, or host-relay failure. It narrows the open
boundary to iOS/CoreBluetooth delivery after a real radio OFF/ON transition.

## Maintained policy

- Keep the spike as an opt-in diagnostic and regression probe.
- Keep `diagnosticRecreateCentral` private to the Apple backend diagnostics SPI.
- Do not use spike success as proof of system-loss recovery.
- Production recovery may attempt native Central recreation at most once after
  the ordinary fresh-scan timeout.
- Production pass still requires fresh advertisement, connection, GATT
  discovery, subscription, and the real EF notification.
- A missing callback remains a bounded `platform-timeout` with diagnostic
  evidence; no retry loop or synthetic event is allowed.
- Do not repeat destructive OFF/ON solely to reconfirm this candidate.

## Next investigation boundary

The non-destructive Central recreation path is now formalized sufficiently for
continued diagnostics. The remaining question is not whether a fresh
`CBCentralManager` can scan in a healthy process; that is demonstrated. The
remaining question is whether iOS requires a broader native backend lifecycle
replacement after radio system-loss, and whether that can preserve the same
BLeng owner without weakening backend identity, event-reader, or resource
retirement guarantees. That requires a separate design/spike review before any
new destructive RF run.
