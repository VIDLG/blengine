"""Live-attested iOS two-runtime pending-read gate (not the diagnostic HEX runner).

Installs the frozen App once and uses official payload URLs with one continuously
pumped console. Ordinary execution never builds, flashes or resets the DK.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from tools.react_native import pending_read_reload_runner
from tools.react_native.pending_read_reload_runner import DEFAULT_TIMEOUT


def run(
    device: str,
    *,
    app: Path,
    fixture_contract: Path,
    fixture_profile: Path,
    timeout: float = DEFAULT_TIMEOUT,
) -> Path:
    return pending_read_reload_runner.run(
        "ios",
        device,
        app,
        fixture_contract=fixture_contract,
        fixture_profile=fixture_profile,
        timeout=timeout,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("device")
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--fixture-contract", type=Path, required=True)
    parser.add_argument("--fixture-profile", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    args = parser.parse_args()
    try:
        manifest = run(
            args.device,
            app=args.app,
            fixture_contract=args.fixture_contract,
            fixture_profile=args.fixture_profile,
            timeout=args.timeout,
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"ios-pending-read-reload:failed {error}", file=sys.stderr)
        return 2
    print(f"ios-pending-read-reload:passed bundle={manifest} app=kept-open")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
