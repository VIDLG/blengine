"""Opaque synthetic bundle runtime bytes; not native ACK or RF evidence."""

import json

from tools.react_native import rf_evidence_bundle as evidence

SESSION = "12345678-1234-4123-8123-123456789abc"


def recovery_runtime(platform: str = "android") -> bytes:
    """Synthetic native JSON and counter projections, not real RF evidence.

    Six/eight-field native frames are representative opaque payloads; the
    bundle does not assert their real SDK identity or ACK semantics.
    """
    boot = {
        "runId": SESSION,
        "bootId": "abcdefab-1234-4123-8123-123456789abc",
        "pid": 123,
        "bundleId": "synthetic-host",
        "executable": "Synthetic",
        "nonce": "synthetic-one-boot",
    }
    owners = {key: 0 for key in evidence.RECOVERY_RESOURCE_FIELDS}
    for key in (
        "hubRoots",
        "handoffRoots",
        "ingressRoots",
        "backendLeases",
        "adapterDelegates",
        "listeners",
    ):
        owners[key] = 1
    owners.update(
        workerThreads=2 if platform == "android" else 0,
        lifecycleOwners=1 if platform == "android" else 0,
    )
    snapshots = {
        "baseline": owners,
        "active": {
            **owners,
            "connections": 1,
            "subscriptions": 1,
            "gattAttributes": 7,
            "callbackRoutes": 3,
            "peerCacheEntries": 1,
        },
        "passive": {**owners, "peerCacheEntries": 1},
        "recovery-disconnected": {**owners, "peerCacheEntries": 1},
        "closed": {key: 0 for key in evidence.RECOVERY_RESOURCE_FIELDS},
    }
    lines = [
        "native:bootstrap:" + json.dumps(boot),
        f"runtime:runId={SESSION}:nonce=synthetic-one-boot:phase=start",
    ]
    for cycle in range(10):
        lines.extend([f"cycle:{cycle}:start", "listener:registered-before-initialize"])
        # A legitimate text field containing ':failed:' is not a verdict marker.
        lines.append(
            "event:"
            + json.dumps(
                {
                    "kind": "advertisementObserved",
                    "cycle": cycle,
                    "runId": SESSION,
                    "localName": "synthetic:failed:text",
                    "error": None,
                }
            )
        )
        for phase in evidence.RECOVERY_PHASES:
            if phase == "passive":
                lines.append(
                    f"cycle:{cycle}:passive-disconnect:passed:commandId=none:"
                    f"connectionId={10004 + cycle * 100}:"
                    f"error={'connection' if platform == 'android' else 'none'}"
                )
            if phase == "recovery-disconnected":
                lines.extend(
                    [
                        f"cycle:{cycle}:recovery-awaiting:runId={SESSION}",
                        "native:signal:"
                        + json.dumps({**boot, "cycle": cycle, "phase": "recovered"}),
                        f"cycle:{cycle}:recovery-ready:runId={SESSION}:source=linking",
                    ]
                )
            lines.append(
                f"resources:cycle={cycle}:phase={phase}:snapshot="
                + json.dumps(snapshots[phase], separators=(",", ":"))
            )
        lines.append(f"cycle:{cycle}:passed")
    lines.extend(
        [
            "close:passed:listener-removed=true:probe-closed=true",
            f"runtime:runId={SESSION}:nonce=synthetic-one-boot:phase=close",
            f"passed:cycles=10:runId={SESSION}",
        ]
    )
    return "".join(evidence.RECOVERY_PREFIX + line + "\n" for line in lines).encode()
