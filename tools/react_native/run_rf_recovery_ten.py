"""Run ten attested RF recovery/close lifecycles in one mobile App/runtime.

Each cycle creates a fresh production Probe/Hub and verifies its observed logical
resources after native close. This is not ten cycles on one Hub, OS heap proof,
or a source-to-build attestation. No ordinary run builds or flashes firmware.
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
    timeout: float = 900,
) -> Path:
    """Run only the standalone RecoveryTen scenario."""
    return rf_scenario_runner.run(
        platform,
        device,
        artifact,
        fixture_contract,
        fixture_profile,
        timeout=timeout,
        scenario="recovery-ten",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("android", "ios"), required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--fixture-contract", type=Path, required=True)
    parser.add_argument("--fixture-profile", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=900)
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
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"rf-recovery-ten:failed {error}", file=sys.stderr)
        return 2
    print(
        f"rf-recovery-ten:passed platform={args.platform} cycles=10 bundle={manifest} app=kept-open"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
