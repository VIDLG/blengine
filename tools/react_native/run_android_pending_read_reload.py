"""Live-attested pending ATT read across two RN engines in one Android process.

The provider owns the only BTP lease/session. This controller never flashes,
resets GATT, or infers native entry from dispatch acceptance.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from tools.react_native import pending_read_reload_runner
from tools.react_native.pending_read_reload_runner import DEFAULT_TIMEOUT


def run(
    serial: str,
    *,
    apk: Path,
    fixture_contract: Path,
    fixture_profile: Path,
    timeout: float = DEFAULT_TIMEOUT,
) -> Path:
    return pending_read_reload_runner.run(
        "android",
        serial,
        apk,
        fixture_contract=fixture_contract,
        fixture_profile=fixture_profile,
        timeout=timeout,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("serial")
    parser.add_argument("--apk", type=Path, required=True)
    parser.add_argument("--fixture-contract", type=Path, required=True)
    parser.add_argument("--fixture-profile", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    args = parser.parse_args()
    try:
        manifest = run(
            args.serial,
            apk=args.apk,
            fixture_contract=args.fixture_contract,
            fixture_profile=args.fixture_profile,
            timeout=args.timeout,
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"android-pending-read-reload:failed {error}", file=sys.stderr)
        return 2
    print(f"android-pending-read-reload:passed bundle={manifest} app=kept-open")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
