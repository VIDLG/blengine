"""Read-only report for the host switches behind unattended XCTest runs.

The Mac reports its automation-mode authentication policy, developer-tools access and
keychain state. XCTest requests automation mode for a session on demand. The operator
can observe the separate UI Automation setting on the iPhone.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


# A locked keychain does not fail a build: every `codesign` invocation waits at 0% CPU on
# the unlock prompt, so the run looks like a hang. These probes are read-only and bounded.
class UnattendedError(RuntimeError):
    """A host switch that a run cannot proceed past is readable and not in its state."""


REMEDIES = {
    "automation_mode": (
        "pixi run just apple enable-automation-mode   "
        "(sudo automationmodetool enable-automationmode-without-authentication)"
    ),
    "developer_mode": "sudo DevToolsSecurity -enable",
    "keychain": "pixi run just apple unlock-login-keychain",
    "documented_device_half": (
        "iPhone Settings > Developer > UI Automation is visible to the operator on the phone. "
        "This Mac reads only host settings. With the authentication exemption, XCTest can "
        "enable automation mode without a host prompt even if the current session is disabled."
    ),
}


def report() -> dict[str, str]:
    """The three readable host facts, by the switch they describe."""
    return {
        "automation_mode": automation_mode_authentication(),
        "developer_mode": developer_mode_state(),
        "keychain": keychain_lock_state(),
    }


def unattended() -> bool:
    """True only when every readable switch is in its unattended state."""
    return all(
        state == "unattended" for state in report().values() if state != "unknown"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read this Mac's automation-mode authentication policy, developer-tools "
            "access and keychain state. XCTest requests the current session's mode on demand."
        )
    )
    parser.add_argument(
        "--json", action="store_true", help="print the report as one JSON document"
    )
    arguments = parser.parse_args(argv)
    states = report()
    if arguments.json:
        print(json.dumps({"switches": states, "unattended": unattended()}, indent=2))
    else:
        for name, state in states.items():
            line = f"xctest_{name}={state}"
            if state == "requires-authentication":
                line += f" remedy={REMEDIES[name]!r}"
            print(line)
        print(f"xctest_documented_device_half={REMEDIES['documented_device_half']!r}")
        print(f"xctest_host_unattended={'yes' if unattended() else 'no'}")
    return 0


_AUTOMATION_AUTH_UNATTENDED = re.compile(
    r"does not require user authentication", re.IGNORECASE
)
_AUTOMATION_AUTH_REQUIRED = re.compile(
    r"(?<!not )requires user authentication", re.IGNORECASE
)
# `enable-automationmode-without-authentication` creates this marker (root-owned, empty);
# its presence *is* the state. The directory appears with the first write, so a present
# directory without the marker means the requirement is still on.
AUTOMATION_MODE_DIRECTORY = Path("/var/db/com.apple.dt.automationmode")
AUTOMATION_MODE_MARKER = AUTOMATION_MODE_DIRECTORY / "no-auth-required"
# `security show-keychain-info` answers in ~30 ms unlocked and blocks on the unlock prompt
# when locked; this bound is the difference between a named precondition and a hang.
KEYCHAIN_PROBE_SECONDS = 5.0
LOCKED_KEYCHAIN_REMEDY = "pixi run just apple unlock-login-keychain"


def require_unlocked_keychain() -> str:
    """Refuse to start while the login keychain does not answer, i.e. it is locked.

    A locked keychain does not fail a build: every `codesign` invocation waits at 0% CPU on
    the unlock prompt, so the run looks like a hang and leaves half-signed products behind
    when it is interrupted. Probing is unprivileged and bounded; the fix needs the login
    password, so it is a human step with a named recipe. Nothing here unlocks anything.
    """
    security = shutil.which("security")
    if security is None:
        return "unknown"

    def locked(error: BaseException | None = None):
        return UnattendedError(
            "the login keychain did not answer within "
            f"{KEYCHAIN_PROBE_SECONDS:.0f} s, which is what a locked keychain looks like. "
            "Every codesign step of a build would then wait on the unlock prompt instead of "
            "failing, so nothing can be signed. Unlock it as a human step: "
            f"`{LOCKED_KEYCHAIN_REMEDY}`. This owner never unlocks, rewrites or re-keys a "
            "keychain."
        )

    try:
        completed = subprocess.run(
            [security, "show-keychain-info", "login.keychain-db"],
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_PROBE_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise locked(error) from error
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if completed.returncode != 0:
        raise locked() from None
    return completed.stdout.strip() or "unlocked"


def _marker_state() -> str:
    """The host automation-mode requirement as recorded on disk, or `unknown`."""
    try:
        if AUTOMATION_MODE_MARKER.exists():
            return "unattended"
        if AUTOMATION_MODE_DIRECTORY.is_dir():
            return "requires-authentication"
    except OSError:
        return "unknown"
    return "unknown"


def _tool_state() -> str:
    """The same state as `automationmodetool` prints it, or `unknown`."""
    tool = shutil.which("automationmodetool")
    if tool is None:
        return "unknown"
    try:
        completed = subprocess.run(
            [tool], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    text = completed.stdout + completed.stderr
    if _AUTOMATION_AUTH_UNATTENDED.search(text):
        return "unattended"
    if _AUTOMATION_AUTH_REQUIRED.search(text):
        return "requires-authentication"
    return "unknown"


def automation_mode_authentication() -> str:
    """Read the Mac's authentication requirement for enabling automation mode.

    `automationmodetool` reports both this policy and the current session's mode.
    XCTest runs on this Mac have succeeded with an initial session state of "disabled".
    The `no-auth-required` marker records the host exemption. When the marker and the
    tool both yield a policy, disagreement gives `unknown`.
    """
    recorded, printed = _marker_state(), _tool_state()
    if recorded == "unknown":
        return printed
    if printed in {"unknown", recorded}:
        return recorded
    return "unknown"


def developer_mode_state() -> str:
    """Whether this Mac lets developer tools attach without a password prompt.

    `DevToolsSecurity -enable` is the switch behind "Developer Tools Access needs to take
    control of another process": enabled, a member of `_developer` attaches without
    authentication; disabled, every attach asks. It is read only and reported, never
    enforced: a WDA launch has been observed to work on this Mac and no evidence yet ties
    a disabled switch to a failed run, so blocking on it would be an unverified claim.
    """
    tool = shutil.which("DevToolsSecurity")
    if tool is None:
        return "unknown"
    result = subprocess.run(
        [tool, "-status"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        return "unknown"
    printed = result.stdout.strip().lower()
    if "currently enabled" in printed:
        return "unattended"
    if "currently disabled" in printed:
        return "requires-authentication"
    return "unknown"


def keychain_lock_state() -> str:
    """Whether `login.keychain-db` answers a non-interactive probe.

    Reuses the bounded preflight probe rather than a second implementation: a locked
    keychain makes the probe time out, which is the same observation the preflight acts
    on. A probe that cannot finish is `unknown`, not `unattended`.
    """
    try:
        state = require_unlocked_keychain()
    except UnattendedError:
        return "requires-authentication"
    return "unknown" if state == "unknown" else "unattended"


if __name__ == "__main__":
    sys.exit(main())
