"""iOS radio-control source samples; not live authority or RF evidence.

Only the identity constants and one independently authored UI source sequence
remain: the journal/summary derived from the retired adapter-startup golden
attachment were removed with that lane. Expected UI steps stay independent of
the production verifier's private transition table.
"""

from __future__ import annotations

import copy

DEVICE = "00008110-001234567890001E"  # Synthetic physical-shaped ID; never queried.
RUN = "12345678-1234-4234-8234-123456789abc"
SERVICE = "11111111-1111-4111-8111-111111111111"
_UI_SOURCE_STEPS = (
    ("authorization-verified", False, None),
    ("settings-initial-on", False, "1"),
    ("settings-off", False, "0"),
    ("off-probe-closed", True, None),
    ("settings-on", False, "1"),
    ("original-session-returned", True, None),
    ("host-window-requested", True, None),
    ("host-window-ready", True, None),
    ("on-requested", True, None),
    ("app-runtime-passed", True, None),
    ("settings-final-on", False, "1"),
    ("original-result-returned", True, None),
)


def passed_control(identity: dict, receipt_sha256: str) -> dict:
    """One independently authored UI source sequence; each call owns its data."""
    return {
        "schema_version": 1,
        "scope": "ios-adapter-startup-control-window",
        "run_id": identity["run_id"],
        "fixture_receipt_sha256": receipt_sha256,
        "status": "passed",
        "mutation_attempted": True,
        "restored": True,
        "same_session": True,
        "observations": [
            {
                "action": action,
                "elapsed_seconds": index / 2,
                "identity": copy.deepcopy(identity) if identified else None,
                "switch_value": switch,
            }
            for index, (action, identified, switch) in enumerate(_UI_SOURCE_STEPS)
        ],
        "primary_error": "",
        "cleanup_error": "",
    }
