"""Read and capture installed nRF52840 image attestation from one physical DK.

This command is deliberately read-only. It does not flash, reset, advertise, or
start a fixture process. The attestation JSON is wire-shaped so it can be fed
straight to ``fixture_attestation assert``; a separate provenance sidecar records
which physical probe and VCOM were used to obtain it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .btp_commands import VendorCommands
from .btp_session import BtpSession
from .btp_transport import FramedTransport
from .jlink_probe import (
    ProbeDetectionError,
    detect_btp_port,
    open_btp_port,
    verify_probe,
)
from .resource_lease import DeviceLease, ResourceLeaseError, owned_lease
from .serial_transport import SerialByteTransport

PROVENANCE_SCHEMA_VERSION = 1


class AttestationCaptureError(RuntimeError):
    """Raised when an installed attestation cannot be captured safely."""


def _attestation_document(attestation: Any) -> dict[str, object]:
    """Convert the strict dataclass into the exact JSON attestation schema."""
    return {
        "schema_version": 1,
        "image_id": attestation.image_id,
        "image_version": attestation.image_version,
        "image_sha256": attestation.image_sha256,
        "board": attestation.board,
        "profile_id": attestation.profile_id,
        "service_uuid": attestation.service_uuid,
        "local_name": attestation.local_name,
        "capabilities": sorted(attestation.capabilities),
    }


def _provenance_document(
    *,
    probe_serial: str,
    selected_port: str,
    probe: Any,
    attestation: dict[str, object],
) -> dict[str, object]:
    return {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "captured_at_utc": datetime.now(UTC).isoformat(),
        "method": "btp-vendor-attestation-read-only",
        "probe_serial": probe_serial,
        "port": selected_port,
        "probe": {
            "pid": probe.pid,
            "location": probe.location,
            "ports": list(probe.ports),
        },
        "attestation": attestation,
    }


def _atomic_json(path: Path, document: dict[str, object]) -> None:
    """Write one JSON file atomically without following a destination symlink."""
    if path.is_symlink():
        raise AttestationCaptureError(f"refusing to replace symlink output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(document, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as error:
        raise AttestationCaptureError(
            f"cannot atomically write {path}: {error}"
        ) from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def capture(
    *,
    probe_serial: str,
    output: Path,
    requested_port: str | None = None,
    lease: DeviceLease | None = None,
) -> tuple[dict[str, object], Path]:
    """Read one physical fixture and atomically write attestation plus provenance."""
    try:
        with owned_lease(
            "jlink", probe_serial, purpose="installed image capture", lease=lease
        ) as owner:
            selected_port, probe = detect_btp_port(
                probe_serial, requested_port, lease=owner
            )
            if verify_probe(probe_serial) != probe:
                raise AttestationCaptureError(
                    "J-Link USB identity changed before attestation read"
                )
            with open_btp_port(selected_port) as connection:
                session = BtpSession(FramedTransport(SerialByteTransport(connection)))
                installed = VendorCommands(session).read_image_attestation()
            if verify_probe(probe_serial) != probe:
                raise AttestationCaptureError(
                    "J-Link USB identity changed after attestation read"
                )
    except (AttestationCaptureError, ProbeDetectionError, ResourceLeaseError):
        raise
    except Exception as error:
        raise AttestationCaptureError(
            "installed image attestation read failed; no attestation was inferred "
            f"from host files: {error}"
        ) from error

    attestation = _attestation_document(installed)
    provenance = _provenance_document(
        probe_serial=probe_serial,
        selected_port=selected_port,
        probe=probe,
        attestation=attestation,
    )
    provenance_path = output.with_name(output.name + ".provenance.json")
    _atomic_json(output, attestation)
    try:
        _atomic_json(provenance_path, provenance)
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return attestation, provenance_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument(
        "--probe-serial",
        required=True,
        help="numeric SEGGER J-Link serial of the physical DK",
    )
    _ = parser.add_argument(
        "--port",
        help="optional expected VCOM; the command still requires unique BTP detection",
    )
    _ = parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="wire-shaped installed-attestation JSON output",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        attestation, provenance_path = capture(
            probe_serial=args.probe_serial,
            requested_port=args.port,
            output=args.output,
        )
    except (
        AttestationCaptureError,
        ProbeDetectionError,
        ResourceLeaseError,
        OSError,
        ValueError,
    ) as error:
        print(f"fixture_attestation_capture=failed:{error}", file=sys.stderr)
        return 2
    print("fixture_attestation_capture=passed")
    print(f"fixture_attestation_output={args.output}")
    print(f"fixture_attestation_provenance={provenance_path}")
    print(f"fixture_image_id={attestation['image_id']}")
    print(f"fixture_image_sha256={attestation['image_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
