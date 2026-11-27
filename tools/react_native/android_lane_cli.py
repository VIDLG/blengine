"""The one shape every Android native lane's command line has.

Four runners used to describe the same device, candidate, fixture, budget and authorization
vocabulary inline, five to six times over. A vocabulary that has to be retyped is a vocabulary that
drifts: the fixture identity was once spelled `--fixture-identity` in two lanes and
`--fixture-resource-identity` in the others, and a run failed on the flag name rather than on
anything about the hardware.

Each helper declares one group once. A lane adds only what is its own — a scene name, a peer list, a
fault — and passes the defaults it actually wants, so "the lane owns its budget" stays true without
copying the wording five times.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .rf_runner import add_journal_argument

FIXTURE_IDENTITY_HELP = (
    "explicit canonical jlink:<serial> or nrf-usb:<serial>; no port-only identity"
)
BLUETOOTH_PERMISSIONS_HELP = (
    "authorize exact BLUETOOTH_SCAN/CONNECT root grant with readback and exact restore"
)


def add_device(parser: argparse.ArgumentParser) -> None:
    """The one physical phone this lane drives, by serial rather than by model name."""
    parser.add_argument("--device", required=True)


def add_candidate(parser: argparse.ArgumentParser) -> None:
    """The frozen App and test APKs, each pinned by the digest the lane will verify."""
    parser.add_argument("--app-apk", type=Path, required=True)
    parser.add_argument("--app-apk-sha256", required=True)
    parser.add_argument("--test-apk", type=Path, required=True)
    parser.add_argument("--test-apk-sha256", required=True)


def add_fixture(parser: argparse.ArgumentParser) -> None:
    """One physically attached fixture: what it must serve, where it is, and which one it is."""
    parser.add_argument("--fixture-contract", type=Path, required=True)
    parser.add_argument("--fixture-profile", type=Path, required=True)
    parser.add_argument("--fixture-port", required=True)
    parser.add_argument(
        "--fixture-resource-identity",
        required=True,
        metavar="jlink:SERIAL",
        help=FIXTURE_IDENTITY_HELP,
    )


def add_budget(
    parser: argparse.ArgumentParser,
    *,
    timeout: float,
    cleanup_seconds: float,
) -> None:
    """Where the run publishes, plus the two bounds every lane states for itself."""
    parser.add_argument("--output", type=Path, required=True)
    add_journal_argument(parser)
    parser.add_argument("--timeout", type=float, default=timeout)
    parser.add_argument("--cleanup-seconds", type=float, default=cleanup_seconds)


def add_authorization(
    parser: argparse.ArgumentParser,
    *,
    bluetooth_permissions_help: str = BLUETOOTH_PERMISSIONS_HELP,
) -> None:
    """The four explicit authorizations; none of them is implied by `--device`."""
    parser.add_argument(
        "--authorize-install",
        action="store_true",
        required=True,
        help="authorize installing the frozen App and test APKs; no other package is touched",
    )
    parser.add_argument(
        "--authorize-fixture-start",
        action="store_true",
        required=True,
        help="authorize exact-profile GATT initialization and advertising only; never flash/reset",
    )
    parser.add_argument(
        "--authorize-bluetooth-permissions",
        action="store_true",
        required=True,
        help=bluetooth_permissions_help,
    )
    parser.add_argument(
        "--use-root",
        action="store_true",
        help="explicit root-aware installer, no fallback",
    )
