"""Hardware-free DK pin, staging, board-policy, HEX, and provenance checks."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import build_dk as dk
from intelhex import IntelHex


class BuildDKTests(unittest.TestCase):
    def test_board_and_generated_config_reject_dongle_policy(self) -> None:
        dk.validate_config(dict(dk.REQUIRED_CONFIG))
        for name, value in (
            ("CONFIG_BOARD_TARGET", '"nrf52840dongle/nrf52840"'),
            ("CONFIG_BOARD", '"nrf52840dongle"'),
            ("CONFIG_FLASH_LOAD_OFFSET", "0x1000"),
            ("CONFIG_BOOTLOADER_MCUBOOT", "y"),
            ("CONFIG_BOARD_HAS_NRF5_BOOTLOADER", "y"),
            ("CONFIG_UART_CONSOLE", "y"),
            ("CONFIG_LOG_BACKEND_UART", "y"),
            ("CONFIG_LOG_BACKEND_RTT", "n"),
            ("CONFIG_BTTESTER_GATT_READ_HOLD", "n"),
        ):
            with self.subTest(name=name):
                broken = dict(dk.REQUIRED_CONFIG)
                broken[name] = value
                with self.assertRaisesRegex(dk.DiagnosticBuildError, name):
                    dk.validate_config(broken)
        with tempfile.TemporaryDirectory() as temporary:
            conf = Path(temporary) / "board.conf"
            overlay = Path(temporary) / "board.overlay"
            conf.write_text("CONFIG_TEST=n\nCONFIG_LOG=y\nCONFIG_LOG_BACKEND_RTT=y\n")
            overlay.write_text(
                "/ { zephyr,uart-pipe = &uart0; };\n&uart0 { hw-flow-control; };\n"
            )
            dk.validate_board_inputs(conf, overlay)
            overlay.write_text("/ { zephyr,uart-pipe = &cdc_acm_uart0; };\n")
            with self.assertRaisesRegex(dk.DiagnosticBuildError, "UART0"):
                dk.validate_board_inputs(conf, overlay)

    def test_nonfatal_kconfig_warnings_are_fatal(self) -> None:
        with (
            patch.object(
                dk.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    args=["west"],
                    returncode=0,
                    stdout="",
                    stderr="warning: TEST set more than once.",
                ),
            ),
            self.assertRaisesRegex(dk.DiagnosticBuildError, "warning"),
        ):
            dk.run_west(["west", "build"], cwd=Path("."), env={})

    def test_only_internal_application_flash_is_accepted(self) -> None:
        self.assertEqual(dk.validate_segments([(0, 0x100000)]), [(0, 0x100000)])
        for segments in (
            [],
            [(-1, 1)],
            [(0, 0)],
            [(0xFFFFF, 0x100001)],
            [(0, 10), (0x10001000, 0x10001004)],
            [(0, 10), (0x12000000, 0x12000004)],
        ):
            with (
                self.subTest(segments=segments),
                self.assertRaises(dk.DiagnosticBuildError),
            ):
                dk.validate_segments(segments)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "image.hex"
            image = IntelHex()
            image[0] = 0xAA
            image[0xFFFFF] = 0xBB
            image.write_hex_file(str(path))
            self.assertEqual(dk.validate_hex(path), [(0, 1), (0xFFFFF, 0x100000)])
            image[0x10001000] = 0xCC
            image.write_hex_file(str(path))
            with self.assertRaises(dk.DiagnosticBuildError):
                dk.validate_hex(path)

    def test_stage_patches_only_a_copy_and_refuses_any_existing_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "original" / dk.APP
            (source / "src").mkdir(parents=True)
            (source / "boards").mkdir()
            (source / "Kconfig").write_text("source\n")
            (source / "prj.conf").write_text("CONFIG_TEST=y\nCONFIG_BT_SUBRATING=y\n")
            (source / "src" / "btp_gatt.c").write_text("old code\n")
            (source / "boards" / dk.BOARD_CONFIG).write_text("CONFIG_TEST=n\n")
            patch_file = root / "patch.diff"
            patch_file.write_text(
                "diff --git a/tests/bluetooth/tester/Kconfig b/tests/bluetooth/tester/Kconfig\n"
                "--- a/tests/bluetooth/tester/Kconfig\n"
                "+++ b/tests/bluetooth/tester/Kconfig\n"
                "@@ -1 +1 @@\n-source\n+config BTTESTER_GATT_READ_HOLD\n"
                "diff --git a/tests/bluetooth/tester/src/btp_gatt.c "
                "b/tests/bluetooth/tester/src/btp_gatt.c\n"
                "--- a/tests/bluetooth/tester/src/btp_gatt.c\n"
                "+++ b/tests/bluetooth/tester/src/btp_gatt.c\n"
                "@@ -1 +1 @@\n-old code\n+patched code\n"
            )
            (root / ".gitignore").write_text("/.work/\n")
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            work = root / ".work" / "nrf-att-hold-dk"
            with patch.object(dk, "BLEHUB_ROOT", root):
                config = dk.stage(source, work, patch_file)
                self.assertEqual(config.read_text(), dk.EXTRA_CONFIG)
                self.assertIn(
                    "config BTTESTER_GATT_READ_HOLD",
                    (work / dk.APP / "Kconfig").read_text(),
                )
                self.assertEqual((source / "Kconfig").read_text(), "source\n")
                self.assertEqual(
                    (source / "prj.conf").read_text(),
                    "CONFIG_TEST=y\nCONFIG_BT_SUBRATING=y\n",
                )
                self.assertEqual((work / dk.APP / "prj.conf").read_text(), "")
                self.assertEqual(
                    (work / dk.APP / "boards" / dk.BOARD_CONFIG).read_text(),
                    "CONFIG_TEST=n\n",
                )
                sentinel = work / "keep"
                sentinel.write_text("untouched")
                with self.assertRaisesRegex(
                    dk.DiagnosticBuildError, "refusing overwrite"
                ):
                    dk.stage(source, work, patch_file)
                self.assertEqual(sentinel.read_text(), "untouched")
                with self.assertRaisesRegex(dk.DiagnosticBuildError, "staging must be"):
                    dk.stage(source, root / ".work" / "nrf-att-hold", patch_file)

    def test_build_manifest_identifies_dk_inputs_and_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            zephyr = root / "west" / "zephyr"
            zephyr.mkdir(parents=True)
            sdk = root / "sdk"
            sdk.mkdir()
            nrftest = root / "nrftest"
            nrftest.mkdir()
            app = zephyr / dk.APP
            boards = app / "boards"
            boards.mkdir(parents=True)
            (boards / dk.BOARD_CONFIG).write_text(
                "CONFIG_TEST=n\nCONFIG_LOG=y\nCONFIG_LOG_BACKEND_RTT=y\n"
            )
            (boards / dk.BOARD_OVERLAY).write_text(
                "/ { zephyr,uart-pipe = &uart0; };\n&uart0 { hw-flow-control; };\n"
            )
            work = root / ".work" / "nrf-att-hold-dk"
            dtc = root / "dtc"
            dtc.touch()

            def fake_stage(_source: Path, target: Path, _patch: Path) -> Path:
                staged = target / dk.APP
                staged.mkdir(parents=True)
                (staged / "Kconfig").write_text("config BTTESTER_GATT_READ_HOLD\n")
                config = target / "diagnostic-dk.conf"
                config.write_text(dk.EXTRA_CONFIG)
                return config

            def fake_run(command: list[str], *, cwd: Path, env: dict[str, str]) -> str:
                self.assertEqual(
                    command[:4], ["/managed/west", "build", "--board", dk.BOARD]
                )
                self.assertEqual(cwd, zephyr.parent)
                self.assertEqual(env["ZEPHYR_BASE"], str(zephyr))
                self.assertIn(str(work / dk.APP), command)
                self.assertEqual(
                    command[-1], f"-DEXTRA_CONF_FILE={work / 'diagnostic-dk.conf'}"
                )
                output = work / "build" / "zephyr"
                output.mkdir()
                (output / ".config").write_text(
                    "".join(
                        f"{key}={value}\n" for key, value in dk.REQUIRED_CONFIG.items()
                    )
                )
                (output / "zephyr.dts").write_text("/dts-v1/;\n")
                (output / "zephyr.elf").write_bytes(b"elf")
                (output / "zephyr.bin").write_bytes(b"bin")
                image = IntelHex()
                image[0] = 0xAA
                image.write_hex_file(str(output / "zephyr.hex"))
                return ""

            with (
                patch.object(dk, "WORK", work),
                patch.object(dk.shared, "verify_source", return_value=app) as verify,
                patch.object(dk, "stage", side_effect=fake_stage),
                patch.object(dk, "run_west", side_effect=fake_run),
                patch.object(dk.shutil, "which", return_value="/managed/west"),
            ):
                manifest_path = dk.build(zephyr, sdk, nrftest, dtc)
            self.assertEqual(verify.call_count, 2)
            manifest = json.loads(manifest_path.read_text())
            self.assertEqual(manifest["board"], dk.BOARD)
            self.assertEqual(manifest["zephyr_head"], dk.PINNED_HEAD)
            self.assertFalse(manifest["flash_performed"])
            self.assertEqual(
                manifest["flash_segments"], [{"start": "0x0", "end_exclusive": "0x1"}]
            )
            self.assertEqual(
                manifest["inputs"]["application"]["sha256"], dk.tree_sha256(app)
            )
            self.assertEqual(
                manifest["inputs"]["patch"]["sha256"], dk.shared.sha256_file(dk.PATCH)
            )
            self.assertEqual(
                [artifact["path"] for artifact in manifest["artifacts"]],
                [
                    "zephyr/.config",
                    "zephyr/zephyr.dts",
                    "zephyr/zephyr.elf",
                    "zephyr/zephyr.hex",
                    "zephyr/zephyr.bin",
                ],
            )


if __name__ == "__main__":
    unittest.main()
