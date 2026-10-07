"""Boundary tests for the package-only ATT diagnostic step."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import package_diagnostic as diagnostic


def _hex_record(address: int, record_type: int, data: bytes) -> str:
    body = bytes([len(data), address >> 8, address & 0xFF, record_type]) + data
    checksum = (-sum(body)) & 0xFF
    return ":" + (body + bytes([checksum])).hex().upper()


def _write_hex(path: Path, address: int, data: bytes) -> None:
    path.write_text(
        "\n".join(
            (
                _hex_record(0, 4, bytes([address >> 24, (address >> 16) & 0xFF])),
                _hex_record(address & 0xFFFF, 0, data),
                _hex_record(0, 1, b""),
            )
        )
        + "\n",
        encoding="ascii",
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PackageDiagnosticTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.blehub_root_patcher = patch.object(
            diagnostic, "BLEHUB_ROOT", self.root / "blehub"
        )
        self.blehub_root_patcher.start()
        self.blehub = self.root / "blehub"
        self.nrftest = self.root / "nrftest"
        self.work = self.blehub / ".work" / "nrf-att-hold"
        self.build = self.work / "build"
        self.zephyr = self.build / "zephyr"
        self.zephyr.mkdir(parents=True)
        self.nrftest.mkdir()
        (self.nrftest / "firmware" / "app").mkdir(parents=True)
        (self.nrftest / "firmware" / "app" / "pca10059.conf").write_text("config\n")
        (self.nrftest / "firmware" / "app" / "pca10059.overlay").write_text("/ {}\n")
        (self.nrftest / "firmware" / "package.toml").write_text(
            "schema_version = 1\n"
            'format = "nrf5-sdk-secure-dfu"\n'
            "hardware_version = 52\n"
            'softdevice_requirement = "0x00"\n'
            "application_version = 1\n"
            "signed = false\n",
            encoding="utf-8",
        )
        (self.nrftest / "firmware-tools.lock.toml").write_text(
            "schema_version = 1\n"
            '[nrfutil]\nversion = "8.2.1"\n'
            'commit = "cli-commit"\n'
            "[nrfutil.command]\n"
            'name = "nrf5sdk-tools"\nversion = "1.1.0"\n'
            'commit = "command-commit"\ninner_version = "6.1.7"\n'
            '[platforms."osx-arm64"]\n'
            'target = "aarch64-apple-darwin"\n'
            'cli_archive = "cli.tar.gz"\ncli_sha256 = "'
            + "0" * 64
            + '"\ncommand_archive = "command.tar.gz"\ncommand_sha256 = "'
            + "1" * 64
            + '"\n',
            encoding="utf-8",
        )
        (self.blehub / "spikes" / "nrf-att-hold" / "firmware").mkdir(parents=True)
        self.patch_file = (
            self.blehub
            / "spikes"
            / "nrf-att-hold"
            / "firmware"
            / "0001-bttester-gatt-read-hold.patch"
        )
        self.patch_file.write_bytes(b"patch")
        (self.work / "diagnostic.conf").write_bytes(b"diagnostic")
        _write_hex(self.zephyr / "zephyr.hex", 0x1000, b"diagnostic")
        (self.zephyr / "zephyr.elf").write_bytes(b"elf")
        (self.zephyr / "zephyr.bin").write_bytes(b"bin")
        self.manifest_path = self.build / "diagnostic-manifest.json"
        self.stock_build = self.nrftest / ".work" / "build" / "pca10059-tester"
        (self.stock_build / "zephyr").mkdir(parents=True)
        _write_hex(self.stock_build / "zephyr" / "zephyr.hex", 0x1000, b"stock")
        self.stock_package = self.stock_build / "stock.zip"
        with zipfile.ZipFile(self.stock_package, "w") as package:
            package.writestr("manifest.json", '{"manifest":{"application":{}}}')
        self.stock_manifest_path = self.stock_build / "nrftest-build-manifest.json"
        self._write_manifests()
        self._make_tool()

    def tearDown(self) -> None:
        self.blehub_root_patcher.stop()
        self.temporary.cleanup()

    def _write_manifests(self) -> None:
        diagnostic_hex = self.zephyr / "zephyr.hex"
        stock_hex = self.stock_build / "zephyr" / "zephyr.hex"
        diagnostic_manifest = {
            "schema_version": 1,
            "kind": diagnostic.DIAGNOSTIC_KIND,
            "board": diagnostic.BOARD,
            "zephyr_head": diagnostic.PINNED_ZEPHYR_HEAD,
            "zephyr_root": str(self.root / "zephyr"),
            "nrftest_root": str(self.nrftest.resolve()),
            "application": "tests/bluetooth/tester",
            "inputs": {
                "canonical_config": {
                    "path": str(self.nrftest / "firmware/app/pca10059.conf"),
                    "sha256": _sha(self.nrftest / "firmware/app/pca10059.conf"),
                },
                "overlay": {
                    "path": str(self.nrftest / "firmware/app/pca10059.overlay"),
                    "sha256": _sha(self.nrftest / "firmware/app/pca10059.overlay"),
                },
                "patch": {
                    "path": str(self.patch_file),
                    "sha256": _sha(self.patch_file),
                },
                "diagnostic_config": {
                    "path": str(self.work / "diagnostic.conf"),
                    "sha256": _sha(self.work / "diagnostic.conf"),
                },
            },
            "flash_segments": [{"start": "0x1000", "end_exclusive": "0x100a"}],
            "artifacts": [
                {"path": "zephyr/zephyr.hex", "sha256": _sha(diagnostic_hex)}
            ],
            "dfu_package": None,
        }
        stock = {
            "schema_version": 1,
            "board": diagnostic.BOARD,
            "artifacts": [{"path": "zephyr/zephyr.hex", "sha256": _sha(stock_hex)}],
            "dfu_package": {
                "path": "stock.zip",
                "sha256": _sha(self.stock_package),
            },
        }
        self.stock_manifest_path.write_text(
            json.dumps(stock, sort_keys=True), encoding="utf-8"
        )
        self.expected_stock_manifest_sha = _sha(self.stock_manifest_path)
        self.expected_stock_package_sha = _sha(self.stock_package)
        self.manifest_path.write_text(
            json.dumps(diagnostic_manifest, sort_keys=True), encoding="utf-8"
        )

    def _make_tool(self) -> None:
        self.install = self.root / "tools" / "nrfutil-8.2.1"
        self.bin = self.install / "bin"
        self.bin.mkdir(parents=True)
        self.nrfutil = self.bin / "nrfutil"
        self.command_executable = self.bin / "nrfutil-nrf5sdk-tools"
        self.nrfutil.write_bytes(b"cli")
        self.command_executable.write_bytes(b"command")
        installed = (
            self.install / "installed" / "nrfutil-nrf5sdk-tools-aarch64-apple-darwin"
        )
        installed.mkdir(parents=True)
        (installed / "manifest.json").write_text(
            json.dumps(
                {
                    "name": "nrfutil-nrf5sdk-tools",
                    "version": "1.1.0",
                    "platform": "aarch64-apple-darwin",
                    "build": "command-commit",
                }
            ),
            encoding="utf-8",
        )

    def _run_package(self, **kwargs: object) -> Path:
        return diagnostic.package(
            self.manifest_path,
            self.nrftest,
            stock_manifest=self.stock_manifest_path,
            nrfutil=self.nrfutil,
            expected_stock_manifest_sha256=self.expected_stock_manifest_sha,
            expected_stock_package_sha256=self.expected_stock_package_sha,
            **kwargs,
        )

    def test_rejects_manifest_identity_and_flash_boundary_errors(self) -> None:
        for field, value, message in (
            ("kind", "wrong", "kind"),
            ("zephyr_head", "wrong", "pinned"),
        ):
            with self.subTest(field=field):
                manifest = json.loads(self.manifest_path.read_text())
                manifest[field] = value
                self.manifest_path.write_text(json.dumps(manifest))
                with self.assertRaisesRegex(diagnostic.DiagnosticPackageError, message):
                    diagnostic.package(
                        self.manifest_path,
                        self.nrftest,
                        stock_manifest=self.stock_manifest_path,
                    )
                self._write_manifests()
        manifest = json.loads(self.manifest_path.read_text())
        manifest["flash_segments"][0]["start"] = "0x0"
        self.manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(
            diagnostic.DiagnosticPackageError, "outside application"
        ):
            diagnostic.package(
                self.manifest_path,
                self.nrftest,
                stock_manifest=self.stock_manifest_path,
            )

    def test_rejects_changed_stock_evidence_and_same_hex(self) -> None:
        self.stock_manifest_path.write_text(
            self.stock_manifest_path.read_text(encoding="utf-8") + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            diagnostic.DiagnosticPackageError, "stock manifest changed"
        ):
            diagnostic.package(
                self.manifest_path,
                self.nrftest,
                stock_manifest=self.stock_manifest_path,
                expected_stock_manifest_sha256=self.expected_stock_manifest_sha,
            )
        self._write_manifests()
        self.zephyr.joinpath("zephyr.hex").write_bytes(
            self.stock_build.joinpath("zephyr/zephyr.hex").read_bytes()
        )
        manifest = json.loads(self.manifest_path.read_text())
        manifest["artifacts"][0]["sha256"] = _sha(self.zephyr / "zephyr.hex")
        manifest["flash_segments"] = [{"start": "0x1000", "end_exclusive": "0x1005"}]
        self.manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(diagnostic.DiagnosticPackageError, "identical"):
            diagnostic.package(
                self.manifest_path,
                self.nrftest,
                stock_manifest=self.stock_manifest_path,
                expected_stock_manifest_sha256=self.expected_stock_manifest_sha,
                expected_stock_package_sha256=self.expected_stock_package_sha,
            )

    def test_existing_output_is_not_overwritten_or_sent_to_tool(self) -> None:
        output = self.build / diagnostic.DIAGNOSTIC_PACKAGE_NAME
        output.write_bytes(b"keep")
        with (
            patch.object(diagnostic, "verify_tool") as verify,
            self.assertRaisesRegex(diagnostic.DiagnosticPackageError, "already exists"),
        ):
            self._run_package()
        verify.assert_not_called()
        self.assertEqual(output.read_bytes(), b"keep")

        output.unlink()
        target = self.build / "do-not-replace.zip"
        target.write_bytes(b"keep")
        output.symlink_to(target)
        with self.assertRaisesRegex(
            diagnostic.DiagnosticPackageError, "already exists"
        ):
            self._run_package()
        self.assertEqual(target.read_bytes(), b"keep")

    def test_normalizer_rejects_zip_traversal(self) -> None:
        package = self.root / "unsafe.zip"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("../escape", b"no")
        with self.assertRaisesRegex(diagnostic.DiagnosticPackageError, "unsafe"):
            diagnostic.normalize_package_zip(package)
        self.assertTrue(package.is_file())

    def test_package_generates_normalizes_validates_and_records_provenance(
        self,
    ) -> None:
        calls: list[list[str]] = []

        def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append(command)
            if command[-1] == "--version":
                return SimpleNamespace(
                    stdout="nrfutil 8.2.1 (cli-commit)\ncommit-hash: cli-commit\n",
                    stderr="",
                    returncode=0,
                )
            if "generate" in command:
                destination = Path(command[-1])
                with zipfile.ZipFile(
                    destination, "w", compression=zipfile.ZIP_DEFLATED
                ) as archive:
                    archive.writestr("zephyr.dat", b"dat")
                    archive.writestr("zephyr.bin", b"bin")
                    archive.writestr("manifest.json", '{"manifest":{"application":{}}}')
            return SimpleNamespace(stdout="", stderr="", returncode=0)

        with (
            patch.object(diagnostic, "BLEHUB_ROOT", self.blehub),
            patch.object(diagnostic.platform, "system", return_value="Darwin"),
            patch.object(diagnostic.platform, "machine", return_value="arm64"),
            patch.object(diagnostic.subprocess, "run", side_effect=fake_run),
        ):
            output = self._run_package()
        self.assertEqual(output.name, diagnostic.DIAGNOSTIC_PACKAGE_NAME)
        self.assertEqual(
            calls,
            [
                [str(self.nrfutil), "--version"],
                [
                    str(self.nrfutil),
                    "nrf5sdk-tools",
                    "pkg",
                    "generate",
                    "--hw-version",
                    "52",
                    "--sd-req",
                    "0x00",
                    "--application",
                    str(self.zephyr / "zephyr.hex"),
                    "--application-version",
                    "1",
                    str(output),
                ],
                [str(self.nrfutil), "nrf5sdk-tools", "pkg", "display", str(output)],
            ],
        )
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["dfu_package"]["sha256"], _sha(output))
        self.assertEqual(manifest["dfu_package"]["parameters"]["hardware_version"], 52)
        self.assertEqual(manifest["dfu_package"]["tool"]["command"], "nrf5sdk-tools")
        self.assertEqual(
            manifest["dfu_package"]["stock_reference"]["package_sha256"],
            self.expected_stock_package_sha,
        )
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(
                archive.namelist(), ["manifest.json", "zephyr.bin", "zephyr.dat"]
            )
            self.assertTrue(
                all(
                    info.date_time == diagnostic.NORMALIZED_ZIP_TIMESTAMP
                    for info in archive.infolist()
                )
            )

    def test_cli_requires_explicit_package_only_opt_in(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            diagnostic.main(["--nrftest-root", str(self.nrftest)])


if __name__ == "__main__":
    unittest.main()
