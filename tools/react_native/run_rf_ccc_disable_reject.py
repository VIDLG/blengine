"""Opt-in CCC disable REJECT gate in the existing Android/iPhone workbench.

Install a frozen candidate and use the already provisioned, live-attested v2
Peripheral. Ordinary execution never builds, flashes, resets or joins full-suite.
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
    timeout: float = 180,
    port: str | None = None,
    observe_notification_tx: bool = False,
) -> Path:
    return rf_scenario_runner.run(
        platform,
        device,
        artifact,
        fixture_contract,
        fixture_profile,
        timeout=timeout,
        scenario="ccc-disable-reject",
        port=port,
        **({"observe_notification_tx": True} if observe_notification_tx else {}),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("android", "ios"), required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--fixture-contract", type=Path, required=True)
    parser.add_argument("--fixture-profile", type=Path, required=True)
    parser.add_argument("--port", help="explicit nRF52840 BTP serial port")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument(
        "--observe-notification-tx",
        action="store_true",
        help="capture optional firmware diagnostics; requires explicit installed-image capability",
    )
    args = parser.parse_args()
    try:
        manifest = run(
            args.platform,
            args.device,
            args.artifact,
            args.fixture_contract,
            args.fixture_profile,
            timeout=args.timeout,
            port=args.port,
            observe_notification_tx=args.observe_notification_tx,
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"rf-ccc-disable-reject:failed {error}", file=sys.stderr)
        return 2
    print(
        f"rf-ccc-disable-reject:passed platform={args.platform} bundle={manifest} app=kept-open"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
