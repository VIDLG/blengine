"""Hardware-free boundary tests for the explicit ATT diagnostic flasher."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import flash_diagnostic as diagnostic
import package_diagnostic


@dataclass
class FakePort:
    device: str
    description: str = "bootloader"
    hwid: str = "USB VID:PID=1915:521F"
    vid: int | None = diagnostic.PCA10059_BOOTLOADER_VID
    pid: int | None = diagnostic.PCA10059_BOOTLOADER_PID
    serial_number: str | None = "BOOTLOADER-123"


class FlashDiagnosticTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.blehub = self.root / "blehub"
        self.nrftest = self.root / "nrftest"
        self.diagnostic_build = self.blehub / ".work" / "nrf-att-hold" / "build"
        self.stock_build = self.nrftest / ".work" / "build" / "pca10059-tester"
        self.diagnostic_build.mkdir(parents=True)
        self.stock_build.mkdir(parents=True)

        self.diagnostic_package = (
            self.diagnostic_build / diagnostic.DIAGNOSTIC_PACKAGE_NAME
        )
        self.diagnostic_package.write_bytes(b"diagnostic package")
        self.stock_package = self.stock_build / diagnostic.STOCK_PACKAGE_NAME
        self.stock_package.write_bytes(b"stock package")
        self.stock_manifest = self.stock_build / diagnostic.STOCK_MANIFEST_NAME
        self._write_manifests()

        self.original_diagnostic_sha = diagnostic.DIAGNOSTIC_PACKAGE_SHA256
        self.original_stock_sha = diagnostic.STOCK_PACKAGE_SHA256
        self.original_stock_manifest_sha = diagnostic.STOCK_MANIFEST_SHA256
        diagnostic.DIAGNOSTIC_PACKAGE_SHA256 = self._sha(self.diagnostic_package)
        diagnostic.STOCK_PACKAGE_SHA256 = self._sha(self.stock_package)
        diagnostic.STOCK_MANIFEST_SHA256 = self._sha(self.stock_manifest)
        self._write_manifests()

        self.nrfutil = self.root / "managed" / "bin" / "nrfutil"
        self.nrfutil.parent.mkdir(parents=True)
        self.nrfutil.write_bytes(b"managed nrfutil")

    def tearDown(self) -> None:
        diagnostic.DIAGNOSTIC_PACKAGE_SHA256 = self.original_diagnostic_sha
        diagnostic.STOCK_PACKAGE_SHA256 = self.original_stock_sha
        diagnostic.STOCK_MANIFEST_SHA256 = self.original_stock_manifest_sha
        self.temporary.cleanup()

    @staticmethod
    def _sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _write_manifests(self) -> None:
        stock = {
            "schema_version": 1,
            "board": package_diagnostic.BOARD,
            "dfu_package": {
                "path": self.stock_package.name,
                "sha256": self._sha(self.stock_package),
            },
        }
        self.stock_manifest.write_text(json.dumps(stock), encoding="utf-8")
        self.diagnostic_manifest = self.diagnostic_build / "diagnostic-manifest.json"
        diagnostic_manifest = {
            "schema_version": 1,
            "kind": package_diagnostic.DIAGNOSTIC_KIND,
            "board": package_diagnostic.BOARD,
            "dfu_package": {
                "path": self.diagnostic_package.name,
                "sha256": self._sha(self.diagnostic_package),
                "format": "nrf5-sdk-secure-dfu",
                "signed": False,
            },
        }
        self.diagnostic_manifest.write_text(
            json.dumps(diagnostic_manifest), encoding="utf-8"
        )

    def _tool(self) -> dict[str, object]:
        return {"command": diagnostic.TOOL_COMMAND, "executable": str(self.nrfutil)}

    def _flash(self, mode: str, *, ports: list[list[FakePort]]) -> object:
        with (
            patch.object(package_diagnostic, "verify_tool", return_value=self._tool()),
            patch.object(diagnostic.list_ports, "comports", side_effect=ports),
            patch.object(
                diagnostic.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0),
            ) as run,
            redirect_stdout(io.StringIO()),
        ):
            result = diagnostic.flash(
                mode=mode,
                port_name="/dev/cu.bootloader",
                bootloader_serial="BOOTLOADER-123",
                confirmed_sha256=(
                    diagnostic.DIAGNOSTIC_PACKAGE_SHA256
                    if mode == "diagnostic"
                    else diagnostic.STOCK_PACKAGE_SHA256
                ),
                diagnostic_manifest=self.diagnostic_manifest,
                nrftest_root=self.nrftest,
            )
        return result, run

    def test_diagnostic_requires_verified_stock_and_invokes_only_explicit_dfu(
        self,
    ) -> None:
        port = FakePort("/dev/cu.bootloader")
        package, run = self._flash("diagnostic", ports=[[port], [port]])

        self.assertEqual(package.path, self.diagnostic_package)
        run.assert_called_once_with(
            [
                str(self.nrfutil),
                "nrf5sdk-tools",
                "dfu",
                "usb-serial",
                "-pkg",
                str(self.diagnostic_package),
                "-p",
                "/dev/cu.bootloader",
            ],
            env=unittest.mock.ANY,
            check=True,
            timeout=600,
        )

    def test_restore_uses_stock_without_needing_diagnostic_manifest(self) -> None:
        self.diagnostic_manifest.unlink()
        package, run = self._flash(
            "restore",
            ports=[[FakePort("/dev/cu.bootloader")], [FakePort("/dev/cu.bootloader")]],
        )

        self.assertEqual(package.path, self.stock_package)
        self.assertEqual(run.call_args.args[0][5], str(self.stock_package))

    def test_diagnostic_refuses_missing_stock_before_tool_or_usb(self) -> None:
        self.stock_manifest.unlink()
        with (
            patch.object(package_diagnostic, "verify_tool") as verify_tool,
            patch.object(diagnostic.list_ports, "comports") as comports,
            self.assertRaisesRegex(diagnostic.DiagnosticFlashError, "stock manifest"),
        ):
            diagnostic.flash(
                mode="diagnostic",
                port_name="/dev/cu.bootloader",
                bootloader_serial="BOOTLOADER-123",
                confirmed_sha256=diagnostic.DIAGNOSTIC_PACKAGE_SHA256,
                diagnostic_manifest=self.diagnostic_manifest,
                nrftest_root=self.nrftest,
            )
        verify_tool.assert_not_called()
        comports.assert_not_called()

    def test_application_port_and_identity_change_never_start_nrfutil(self) -> None:
        application = FakePort(
            "/dev/cu.app",
            vid=diagnostic.APPLICATION_VID,
            pid=diagnostic.APPLICATION_PID,
            serial_number="APP-123",
        )
        with (
            patch.object(package_diagnostic, "verify_tool", return_value=self._tool()),
            patch.object(diagnostic.list_ports, "comports", return_value=[application]),
            patch.object(diagnostic.subprocess, "run") as run,
            self.assertRaisesRegex(diagnostic.DiagnosticFlashError, "application port"),
        ):
            diagnostic.flash(
                mode="restore",
                port_name="/dev/cu.app",
                bootloader_serial="APP-123",
                confirmed_sha256=diagnostic.STOCK_PACKAGE_SHA256,
                diagnostic_manifest=self.diagnostic_manifest,
                nrftest_root=self.nrftest,
            )
        run.assert_not_called()

        replacement = FakePort("/dev/cu.bootloader", serial_number="REPLACEMENT")
        with (
            patch.object(package_diagnostic, "verify_tool", return_value=self._tool()),
            patch.object(
                diagnostic.list_ports,
                "comports",
                side_effect=[[FakePort("/dev/cu.bootloader")], [replacement]],
            ),
            patch.object(diagnostic.subprocess, "run") as run,
            self.assertRaisesRegex(diagnostic.DiagnosticFlashError, "identity changed"),
        ):
            diagnostic.flash(
                mode="restore",
                port_name="/dev/cu.bootloader",
                bootloader_serial="BOOTLOADER-123",
                confirmed_sha256=diagnostic.STOCK_PACKAGE_SHA256,
                diagnostic_manifest=self.diagnostic_manifest,
                nrftest_root=self.nrftest,
            )
        run.assert_not_called()

    def test_confirmation_and_tool_provenance_must_match_before_usb(self) -> None:
        with (
            patch.object(
                package_diagnostic, "verify_tool", return_value={"command": "other"}
            ),
            patch.object(diagnostic.list_ports, "comports") as comports,
            self.assertRaisesRegex(
                diagnostic.DiagnosticFlashError, "confirmation mismatch"
            ),
        ):
            diagnostic.flash(
                mode="restore",
                port_name="/dev/cu.bootloader",
                bootloader_serial="BOOTLOADER-123",
                confirmed_sha256=diagnostic.DIAGNOSTIC_PACKAGE_SHA256,
                diagnostic_manifest=self.diagnostic_manifest,
                nrftest_root=self.nrftest,
            )
        comports.assert_not_called()

        with (
            patch.object(
                package_diagnostic, "verify_tool", return_value={"command": "other"}
            ),
            patch.object(diagnostic.list_ports, "comports") as comports,
            self.assertRaisesRegex(
                diagnostic.DiagnosticFlashError, "managed tool command"
            ),
        ):
            diagnostic.flash(
                mode="restore",
                port_name="/dev/cu.bootloader",
                bootloader_serial="BOOTLOADER-123",
                confirmed_sha256=diagnostic.STOCK_PACKAGE_SHA256,
                diagnostic_manifest=self.diagnostic_manifest,
                nrftest_root=self.nrftest,
            )
        comports.assert_not_called()


if __name__ == "__main__":
    unittest.main()
