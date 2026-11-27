# iOS fresh-owner lifecycle diagnostic

Date: 2026-10-31

Status: lifecycle diagnostic passed; no RF and no production gate claim.

## Scope

This candidate validates the application-level lifecycle required for a fresh
BLE owner. It uses the existing `BlengAppleIOSProbe` App and does not start the
nRF provider, scan a Peripheral, toggle Bluetooth, or claim BLE recovery.

The lifecycle is owned by the private application-layer
`BleSessionSupervisor`, not by `crates/blehub`, `crates/bleng`, or the Apple
backend. The supervisor performs:

```text
owner A + backend A
→ event reader shutdown
→ owner A shutdown
→ backend A dispose
→ native resources and backend lease zero
→ owner B + backend B
→ initialize
→ owner B/backend B retirement
```

Maintained entry point:

```text
bleng-ios-fresh-owner-lifecycle
```

App launch mode:

```text
--rf-fresh-owner-lifecycle-spike
```

## Real no-RF candidate

Run ID:

```text
1b1ec8ec-d498-4881-a32e-f817203257e9
```

Observed identities:

```text
old_owner=d8db5532-ac3d-4baf-a46a-9b48b425a19c
new_owner=2c93888c-76fe-43d5-87ae-54b1fc2c5157
```

Both sessions initialized with:

```text
adapter_state=poweredOn
scan_authorization=authorized
```

Both retirement snapshots proved zero:

```json
{
  "activeScans": 0,
  "adapterDelegates": 0,
  "backendLeases": 0,
  "callbackRoutes": 0,
  "connections": 0,
  "gattAttributes": 0,
  "ingressRoots": 0,
  "pendingIo": 0,
  "subscriptions": 0,
  "watchdogs": 0
}
```

Diagnostic artifact:

```text
.work/apple-bleng-ios-backend/1b1ec8ec-d498-4881-a32e-f817203257e9-fresh-owner-lifecycle.json
```

## Meaning

The same-process fresh-owner lifecycle is safe enough to continue to the next
integration step:

- old owner/backend identity is not reused;
- old event ingress and native resources retire before the new owner starts;
- the Apple backend process lease is released and reacquired cleanly;
- this proves lifecycle replacement, not Bluetooth system-loss recovery.

The existing system-loss recovery result remains separate. A fresh owner must
still produce fresh advertisement, connection, GATT, subscription, and EF
notification evidence before it can be called `fresh-owner-recovered`.
