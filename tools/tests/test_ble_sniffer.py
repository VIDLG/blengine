"""No-device contracts for decoding nRF Sniffer captures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tools import ble_sniffer


def test_build_command_uses_explicit_port_and_pcap() -> None:
    command = ble_sniffer.build_command(
        Path("/tools/nrfutil"), "/dev/cu.usbmodem31201", Path("capture.pcap")
    )

    assert command == [
        "/tools/nrfutil",
        "ble-sniffer",
        "sniff",
        "--port",
        "/dev/cu.usbmodem31201",
        "--output-pcap-file",
        "capture.pcap",
    ]


def test_build_command_can_follow_scan_responses() -> None:
    command = ble_sniffer.build_command(
        Path("/tools/nrfutil"),
        "/dev/cu.sniffer",
        Path("capture.pcap"),
        scan_follow_rsp=True,
    )

    assert command[-1] == "--scan-follow-rsp"


def test_build_command_can_follow_a_named_peripheral() -> None:
    command = ble_sniffer.build_command(
        Path("/tools/nrfutil"),
        "/dev/cu.sniffer",
        Path("capture.pcap"),
        follow_by_name="BleHubMultiRfV2DKA",
    )

    assert command[-2:] == ["--follow-by-name", "BleHubMultiRfV2DKA"]


def test_parser_binds_pcap_output_argument() -> None:
    arguments = ble_sniffer._parser().parse_args(
        ["capture", "--output-pcap-file", "capture.pcap"]
    )

    assert arguments.output == "capture.pcap"


def test_capture_resolves_configured_values_and_publishes_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "nrfutil"
    executable = home / "bin" / "nrfutil"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"nrfutil")
    port = tmp_path / "sniffer"
    port.write_bytes(b"")
    output = tmp_path / "capture.pcap"
    calls: list[tuple[list[str], dict[str, str]]] = []

    class FakeProcess:
        returncode = 0

        def poll(self) -> int:
            return self.returncode

        def wait(self, **_kwargs: Any) -> int:
            return self.returncode

    def fake_popen(
        command: list[str], *, env: dict[str, str], **_kwargs: Any
    ) -> FakeProcess:
        calls.append((command, env))
        return FakeProcess()

    monkeypatch.setattr(ble_sniffer, "resolve_port", lambda _argument=None: str(port))
    monkeypatch.setattr(
        ble_sniffer, "resolve_nrfutil_home", lambda _argument=None: home
    )
    monkeypatch.setattr(ble_sniffer.subprocess, "Popen", fake_popen)

    assert ble_sniffer.capture(output=str(output)) == output

    assert calls[0][0][-2:] == ["--output-pcap-file", str(output)]
    assert calls[0][1]["NRFUTIL_HOME"] == str(home)
    metadata = json.loads(ble_sniffer.metadata_path(output).read_text(encoding="utf-8"))
    assert metadata["status"] == "completed"
    assert metadata["port"] == str(port)
    assert metadata["pcap"] == str(output)


def test_capture_rejects_missing_configured_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "missing-sniffer"
    monkeypatch.setattr(
        ble_sniffer, "resolve_port", lambda _argument=None: str(missing)
    )

    with pytest.raises(ble_sniffer.BleSnifferError, match="does not exist"):
        ble_sniffer.capture(output=str(tmp_path / "capture.pcap"))
