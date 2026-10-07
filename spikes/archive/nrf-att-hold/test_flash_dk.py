"""Hardware-free DK image/probe identity and fail-closed SWD command tests."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import flash_dk as dk_flash
from intelhex import IntelHex


class FlashDKTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / "dk"
        build = self.work / "build"
        zephyr_out = build / "zephyr"
        zephyr_out.mkdir(parents=True)
        self.source = self.root / "source"
        self.staged = self.work / dk_flash.dk.APP
        self.staged.mkdir(parents=True)
        self.source.mkdir()
        self.patch_file = self.root / "patch"
        self.patch_file.write_text("patch\n")
        self.conf = self.work / "diagnostic-dk.conf"
        self.conf.write_text("CONFIG_BTTESTER_GATT_READ_HOLD=y\n")
        self.board_conf = self.source / "boards" / dk_flash.dk.BOARD_CONFIG
        self.board_conf.parent.mkdir()
        self.board_conf.write_text("DK\n")
        self.board_overlay = self.board_conf.with_name(dk_flash.dk.BOARD_OVERLAY)
        self.board_overlay.write_text("UART0\n")
        paths = {
            "application": self.source,
            "staged_application": self.staged,
            "board_config": self.board_conf,
            "board_overlay": self.board_overlay,
            "extra_config": self.conf,
            "patch": self.patch_file,
        }
        for relative in (".config", "zephyr.dts", "zephyr.elf", "zephyr.bin"):
            (zephyr_out / relative).write_text("test\n")
        (zephyr_out / ".config").write_text(
            "".join(
                f"{key}={value}\n" for key, value in dk_flash.dk.REQUIRED_CONFIG.items()
            )
        )
        image = IntelHex()
        image[0] = 0xAA
        image.write_hex_file(str(zephyr_out / "zephyr.hex"))
        self.hex = zephyr_out / "zephyr.hex"
        self.sha = dk_flash.dk.shared.sha256_file(self.hex)
        (zephyr_out / "runners.yaml").write_text(
            "runners:\n- jlink\nargs:\n  jlink:\n    - --device=nRF52840_xxAA\n"
        )
        artifacts = [
            {
                "path": "zephyr/" + relative,
                "sha256": dk_flash.dk.shared.sha256_file(zephyr_out / relative),
                "size": (zephyr_out / relative).stat().st_size,
            }
            for relative in (
                ".config",
                "zephyr.dts",
                "zephyr.elf",
                "zephyr.hex",
                "zephyr.bin",
            )
        ]
        self.manifest_data = {
            "schema_version": 1,
            "kind": "nrf-att-read-hold-dk-build",
            "board": dk_flash.dk.BOARD,
            "zephyr_head": dk_flash.dk.PINNED_HEAD,
            "zephyr_root": str(self.root / "west" / "zephyr"),
            "nrftest_root": str(self.root / "nrftest"),
            "flash_performed": False,
            "validated_config": dk_flash.dk.REQUIRED_CONFIG,
            "inputs": {
                name: {
                    "path": str(path),
                    "sha256": dk_flash.dk.tree_sha256(path)
                    if path.is_dir()
                    else dk_flash.dk.shared.sha256_file(path),
                }
                for name, path in paths.items()
            },
            "artifacts": artifacts,
            "flash_segments": [{"start": "0x0", "end_exclusive": "0x1"}],
        }
        self.manifest = build / "diagnostic-dk-manifest.json"
        self.save_manifest()
        self.patches = [
            patch.object(dk_flash.dk, "WORK", self.work),
            patch.object(dk_flash.dk, "PATCH", self.patch_file),
            patch.object(dk_flash, "MANIFEST", self.manifest),
            patch.object(dk_flash, "HEX", self.hex),
            patch.object(dk_flash.dk.shared, "verify_source", return_value=self.source),
        ]
        for override in self.patches:
            override.start()
            self.addCleanup(override.stop)

    def save_manifest(self) -> None:
        self.manifest.write_text(json.dumps(self.manifest_data), encoding="utf-8")

    def test_pin_checks_reject_swapped_board_artifacts_and_inputs(self) -> None:
        self.assertEqual(dk_flash.validate_image(self.sha), self.sha)
        with self.assertRaisesRegex(dk_flash.DKFlashError, "confirmation mismatch"):
            dk_flash.validate_image("0" * 64)
        for field, value in (
            ("board", "nrf52840dongle/nrf52840"),
            ("flash_performed", True),
            ("flash_segments", []),
        ):
            with self.subTest(field=field):
                old = self.manifest_data[field]
                self.manifest_data[field] = value
                self.save_manifest()
                with self.assertRaises(dk_flash.DKFlashError):
                    dk_flash.validate_image(self.sha)
                self.manifest_data[field] = old
        self.save_manifest()
        (self.source / "changed").write_text("tampered")
        with self.assertRaisesRegex(dk_flash.DKFlashError, "input hash changed"):
            dk_flash.validate_image(self.sha)
        (self.source / "changed").unlink()
        self.hex.write_text("modified")
        with self.assertRaisesRegex(dk_flash.DKFlashError, "artifact changed"):
            dk_flash.validate_image(self.sha)

    def test_probe_accepts_two_vcoms_from_one_segger_only(self) -> None:
        def port(
            serial: str,
            suffix: str = "1",
            vid: int = 0x1366,
            location: str = "0-1.4",
        ) -> SimpleNamespace:
            return SimpleNamespace(
                device=f"/dev/cu.usbmodem{serial}{suffix}",
                serial_number=serial,
                vid=vid,
                pid=0x1061,
                location=location,
            )

        first, second = port("123", "1"), port("123", "3")
        with patch.object(
            dk_flash.list_ports, "comports", return_value=[second, first]
        ):
            self.assertEqual(
                dk_flash.verify_probe("123"),
                (0x1061, "0-1.4", (first.device, second.device)),
            )
            for serial in ("", "auto", "123;erase", "124"):
                with (
                    self.subTest(serial=serial),
                    self.assertRaises(dk_flash.DKFlashError),
                ):
                    dk_flash.verify_probe(serial)
        for ports in (
            [port("123", vid=0x1915)],
            [],
            [port("123", "1"), port("123", "3"), port("123", "5")],
        ):
            with (
                patch.object(dk_flash.list_ports, "comports", return_value=ports),
                self.assertRaisesRegex(dk_flash.DKFlashError, "expected one"),
            ):
                dk_flash.verify_probe("123")
        for ports in ([first, first], [first, port("123", "3", location="0-2.1")]):
            with (
                patch.object(dk_flash.list_ports, "comports", return_value=ports),
                self.assertRaisesRegex(dk_flash.DKFlashError, "do not identify one"),
            ):
                dk_flash.verify_probe("123")

    def test_flash_requires_explicit_target_and_double_probe_before_single_command(
        self,
    ) -> None:
        with patch.object(dk_flash.subprocess, "run") as run:
            with self.assertRaisesRegex(dk_flash.DKFlashError, "confirm-target"):
                dk_flash.flash("123", self.sha, "nrf52840dongle/nrf52840")
            run.assert_not_called()
        with (
            patch.object(
                dk_flash,
                "verify_probe",
                side_effect=[
                    (0x1061, "0-1.4", ("/dev/cu.usbmodem1231",)),
                    (0x1061, "0-2.1", ("/dev/cu.usbmodem1231",)),
                ],
            ),
            patch.object(
                dk_flash.shutil, "which", side_effect=lambda tool: "/usr/bin/true"
            ),
            patch.object(dk_flash.subprocess, "run") as run,
        ):
            with self.assertRaisesRegex(
                dk_flash.DKFlashError, "probe or image changed"
            ):
                dk_flash.flash("123", self.sha, dk_flash.dk.BOARD)
            run.assert_not_called()
        with (
            patch.object(
                dk_flash,
                "verify_probe",
                return_value=(0x1061, "0-1.4", ("/dev/cu.usbmodem1231",)),
            ) as probe,
            patch.object(
                dk_flash.shutil, "which", side_effect=lambda tool: "/usr/bin/true"
            ),
            patch.object(
                dk_flash.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0),
            ) as run,
        ):
            dk_flash.flash("123", self.sha, dk_flash.dk.BOARD)
            self.assertEqual(probe.call_count, 3)
            command = run.call_args.args[0]
            self.assertEqual(command[:2], ["/usr/bin/true", "flash"])
            self.assertIn("--no-erase", command)
            self.assertIn("--no-rebuild", command)
            self.assertEqual(command[command.index("--dev-id") + 1], "123")
            self.assertEqual(command[command.index("--runner") + 1], "jlink")
            self.assertEqual(command[command.index("--file") + 1], str(self.hex))
            self.assertNotIn("--erase", command)
            self.assertEqual(run.call_args.kwargs["timeout"], 180)


if __name__ == "__main__":
    unittest.main()
