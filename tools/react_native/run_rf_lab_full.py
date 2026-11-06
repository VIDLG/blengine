"""Install a frozen artifact and run continuous10 + RecoveryTen in one App process.

No ordinary execution builds, flashes, takes over another fixture, or restarts
capture between phases. The shared parent retains failures and owns cleanup.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from tools.react_native import rf_scenario_runner


def run(
    platform: str,
    device: str,
    artifact: Path,
    fixture_contract: Path,
    fixture_profile: Path,
    *,
    timeout: float = 2700,
) -> Path:
    return rf_scenario_runner.run(
        platform,
        device,
        artifact,
        fixture_contract,
        fixture_profile,
        timeout=timeout,
        scenario="full-suite",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("android", "ios"), required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--fixture-contract", type=Path, required=True)
    parser.add_argument("--fixture-profile", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=2700)
    args = parser.parse_args()
    try:
        manifest = run(
            args.platform,
            args.device,
            args.artifact,
            args.fixture_contract,
            args.fixture_profile,
            timeout=args.timeout,
        )
    except (
        OSError,
        ValueError,
        RuntimeError,
        AssertionError,
        subprocess.SubprocessError,
    ) as error:
        print(f"rf-lab-full:failed {error}", file=sys.stderr)
        return 2
    print(
        f"rf-lab-full:passed platform={args.platform} flows=10 cycles=10 bundle={manifest} app=kept-open"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
