"""Live J-Link identity and bounded BTP VCOM detection; no diagnostic wire dependency."""

from __future__ import annotations

import contextlib
import re
from collections.abc import Generator
from dataclasses import dataclass
from typing import Protocol, cast

from .btp_commands import GAP, GATT, BtpCommandError, CoreCommands
from .btp_protocol import BtpFrameError, BtpSessionError
from .btp_session import BtpSession
from .btp_transport import BtpTransportError, FramedTransport
from .resource_lease import DeviceLease, owned_lease
from .serial_transport import SerialByteTransport, SerialPort

PROBE_SERIAL_PATTERN = re.compile(r"[0-9]+\Z")


class PortInfo(Protocol):
    device: str
    vid: int | None
    serial_number: str | None
    pid: int | None
    location: str | None


class ProbeDetectionError(RuntimeError):
    """A required diagnostic identity, ownership, or lifecycle boundary failed."""


@dataclass(frozen=True)
class ProbeIdentity:
    """The USB identity shared by all VCOM ports exposed by one J-Link."""

    pid: int
    location: str
    ports: tuple[str, ...]


@dataclass(frozen=True)
class EnumeratedProbe:
    """A J-Link identity discovered without selecting a configured target."""

    serial_number: str
    pid: int
    location: str
    ports: tuple[str, ...]


def validate_probe_serial(serial_number: str) -> None:
    """Require the numeric USB serial printed on the selected J-Link."""
    if PROBE_SERIAL_PATTERN.fullmatch(serial_number) is None:
        raise ValueError("--probe-serial must be an explicit numeric J-Link USB serial")


def _canonical_probe_serial(serial_number: str) -> str:
    """Compare decimal USB serials without treating formatting zeros as identity."""
    validate_probe_serial(serial_number)
    return serial_number.lstrip("0") or "0"


def _probe_serial_matches(actual: object, expected: str) -> bool:
    if not isinstance(actual, str):
        return False
    try:
        return _canonical_probe_serial(actual) == _canonical_probe_serial(expected)
    except ValueError:
        return False


def _list_ports() -> list[PortInfo]:
    try:
        from serial.tools import list_ports
    except ImportError as error:
        raise ProbeDetectionError(
            "pyserial is required only for the opt-in DK runner"
        ) from error
    return list(list_ports.comports())


def enumerate_probes() -> tuple[EnumeratedProbe, ...]:
    """Enumerate every complete SEGGER J-Link VCOM identity currently visible."""
    grouped: dict[str, list[PortInfo]] = {}
    for port in _list_ports():
        if getattr(port, "vid", None) != 0x1366:
            continue
        device = str(getattr(port, "device", ""))
        serial = getattr(port, "serial_number", None)
        if not device.startswith("/dev/cu.usbmodem") or not isinstance(serial, str):
            continue
        try:
            canonical = _canonical_probe_serial(serial)
        except ValueError:
            continue
        grouped.setdefault(canonical, []).append(port)

    result: list[EnumeratedProbe] = []
    for canonical, ports in grouped.items():
        identities = {
            (getattr(port, "pid", None), getattr(port, "location", None))
            for port in ports
        }
        names = tuple(sorted(str(port.device) for port in ports))
        if (
            not 1 <= len(names) <= 2
            or len(identities) != 1
            or len(set(names)) != len(names)
        ):
            raise ProbeDetectionError(
                f"SEGGER VCOM ports do not identify one complete USB device for {canonical}"
            )
        pid, location = identities.pop()
        if not isinstance(pid, int) or not isinstance(location, str) or not location:
            raise ProbeDetectionError(
                f"SEGGER VCOM has incomplete USB product/location identity for {canonical}"
            )
        result.append(EnumeratedProbe(canonical, pid, location, names))
    return tuple(sorted(result, key=lambda probe: probe.serial_number))


def verify_probe(serial_number: str) -> ProbeIdentity:
    """Require one SEGGER USB identity and one or two distinct J-Link VCOMs."""
    canonical_serial = _canonical_probe_serial(serial_number)
    matches = [
        probe for probe in enumerate_probes() if probe.serial_number == canonical_serial
    ]
    if len(matches) != 1:
        raise ProbeDetectionError(
            f"expected one SEGGER probe with 1-2 VCOM ports for {serial_number}; found {len(matches)}"
        )
    probe = matches[0]
    return ProbeIdentity(probe.pid, probe.location, probe.ports)


@contextlib.contextmanager
def open_btp_port(port: str) -> Generator[SerialPort, None, None]:
    """Open one exclusive 115200 J-Link VCOM with hardware flow control."""
    try:
        import serial
    except ImportError as error:
        raise ProbeDetectionError(
            "pyserial is required only for the opt-in DK runner"
        ) from error
    try:
        connection = serial.Serial(
            port=port,
            baudrate=115200,
            timeout=1,
            write_timeout=1,
            rtscts=True,
            exclusive=True,
        )
    except (OSError, ValueError) as error:
        raise ProbeDetectionError(
            f"cannot exclusively open J-Link VCOM {port}: {error}"
        ) from error
    try:
        yield cast(SerialPort, cast(object, connection))
    finally:
        connection.close()


def _is_btp_probe_error(error: BaseException) -> bool:
    return isinstance(
        error,
        (
            TimeoutError,
            EOFError,
            BtpFrameError,
            BtpSessionError,
            BtpTransportError,
            BtpCommandError,
        ),
    )


def detect_btp_port(
    serial_number: str,
    requested_port: str | None = None,
    *,
    lease: DeviceLease | None = None,
) -> tuple[str, ProbeIdentity]:
    """Hold physical ownership before probing any VCOM; callers may borrow it."""
    with owned_lease("jlink", serial_number, purpose="BTP detection", lease=lease):
        return _detect_btp_port(serial_number, requested_port)


def _detect_btp_port(
    serial_number: str, requested_port: str | None
) -> tuple[str, ProbeIdentity]:
    """Select exactly one exclusive VCOM that answers maintained BTP Core."""
    probe = verify_probe(serial_number)
    found: list[str] = []
    for candidate in probe.ports:
        with open_btp_port(candidate) as connection:
            try:
                session = BtpSession(FramedTransport(SerialByteTransport(connection)))
                services = CoreCommands(session).read_supported_services()
            except Exception as error:
                if not _is_btp_probe_error(error):
                    raise
                continue
            required_services = 1 | (1 << GAP) | (1 << GATT)
            if services & required_services == required_services:
                found.append(candidate)
    if len(found) != 1:
        raise ProbeDetectionError(
            f"expected exactly one BTP VCOM on J-Link {serial_number}; "
            f"validated {found} among {probe.ports}"
        )
    if requested_port is not None and found[0] != requested_port:
        raise ProbeDetectionError(
            f"--port {requested_port} is not the unique BTP responder {found[0]}"
        )
    if verify_probe(serial_number) != probe:
        raise ProbeDetectionError(
            "J-Link USB identity changed during BTP VCOM detection"
        )
    return found[0], probe
