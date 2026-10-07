"""Run the optional maintained iPhone pending-ATT read/reload diagnostic.

This entry point is opt-in and never builds, flashes, resets, starts/stops the
normal nRF provider, installs SDKs, or imports the historical spike. It verifies
the pending-read host/profile identity before taking a lock or hardware ownership.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from typing import cast

from tests.peripherals.nrf52840.diagnostic import device

__all__ = ["build_parser", "device", "main"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--udid", required=True, help="explicit physical iPhone UDID")
    parser.add_argument(
        "--probe-serial",
        required=True,
        help="numeric J-Link USB serial; VCOM is selected by BTP response",
    )
    parser.add_argument(
        "--confirm-hex-sha256",
        required=True,
        help="lowercase SHA-256 of the already-built local diagnostic HEX",
    )
    parser.add_argument(
        "--port",
        help="optional assertion against the uniquely responding J-Link VCOM",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        device.run(
            cast(str, args.udid),
            cast(str, args.probe_serial),
            cast(str, args.confirm_hex_sha256),
            cast(str | None, args.port),
        )
    except (
        OSError,
        RuntimeError,
        ValueError,
        subprocess.SubprocessError,
    ) as error:
        print(f"iOS pending-read reload refused or failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
