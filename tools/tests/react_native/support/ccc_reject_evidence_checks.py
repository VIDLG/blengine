"""Shared raw-native-signal attacks for the two named CCC bundle fixtures."""

from __future__ import annotations

import json

from tools.react_native import rf_evidence_bundle as evidence


def assert_native_signals_reparsed(case, samples, phases) -> None:
    """Use each caller's seeds/clone so seven- and eight-phase contracts stay distinct."""
    changes = {
        "pid": 43,
        "boot_id": "33333333-3333-4333-8333-333333333333",
        "session": "33333333-3333-4333-8333-333333333333",
        "token": "33333333-3333-4333-8333-333333333333",
        "flow": 10,
        "mode": "rf-write-reject",
        "bundle_id": "foreign.host",
        "executable": "file:///foreign/base.apk",
    }
    for platform in case.seeds:
        for phase in phases:
            for key, value in changes.items():
                with case.subTest(platform=platform, phase=phase, key=key):
                    bundle = case.clone(platform)

                    def attack(text, phase=phase, key=key, value=value):
                        result = []
                        for line in text.splitlines(keepends=True):
                            if "rf-host:signal:" in line:
                                head, body = line.split("rf-host:signal:", 1)
                                native = json.loads(body)
                                if native["phase"] == phase:
                                    native[key] = value
                                    line = (
                                        head
                                        + "rf-host:signal:"
                                        + json.dumps(native)
                                        + "\n"
                                    )
                            result.append(line)
                        return "".join(result)

                    samples.edit_runtime(bundle.attempt, attack)
                    samples.rehash(bundle.attempt)
                    with case.assertRaisesRegex(
                        ValueError, "raw native bootstrap/signals"
                    ):
                        evidence.verify_bundle(bundle.attempt / evidence.MANIFEST)
