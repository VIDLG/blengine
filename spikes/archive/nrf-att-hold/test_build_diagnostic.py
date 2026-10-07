"""Hardware-free isolation, pin, Kconfig, and flash-boundary tests."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import build_diagnostic as diagnostic
from intelhex import IntelHex


class BuildDiagnosticTests(unittest.TestCase):
    def test_source_requires_exact_head_clean_subtree_and_west_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            zephyr = root / "west" / "zephyr"
            tester = zephyr / diagnostic.APP
            tester.mkdir(parents=True)
            (root / "west" / ".west").mkdir()
            (root / "west" / ".west" / "config").write_text("[manifest]\n")
            nrftest = root / "nrftest"
            nrftest.mkdir()
            (nrftest / "pixi.toml").write_text("[workspace]\n")
            with (
                patch.object(diagnostic, "run", side_effect=[str(zephyr), "wrong"]),
                self.assertRaisesRegex(diagnostic.DiagnosticBuildError, "not pinned"),
            ):
                diagnostic.verify_source(zephyr, nrftest)
            with (
                patch.object(
                    diagnostic,
                    "run",
                    side_effect=[
                        str(zephyr),
                        diagnostic.PINNED_HEAD,
                        " M tests/bluetooth/tester/Kconfig",
                    ],
                ),
                self.assertRaisesRegex(diagnostic.DiagnosticBuildError, "not clean"),
            ):
                diagnostic.verify_source(zephyr, nrftest)
            with patch.object(
                diagnostic, "run", side_effect=[str(zephyr), diagnostic.PINNED_HEAD, ""]
            ):
                self.assertEqual(diagnostic.verify_source(zephyr, nrftest), tester)
            (tester / "escape").symlink_to(root)
            with (
                patch.object(
                    diagnostic,
                    "run",
                    side_effect=[str(zephyr), diagnostic.PINNED_HEAD, ""],
                ),
                self.assertRaisesRegex(diagnostic.DiagnosticBuildError, "symlink"),
            ):
                diagnostic.verify_source(zephyr, nrftest)

    def test_stage_refuses_existing_outputs_without_deleting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work = root / ".work" / "nrf-att-hold"
            work.mkdir(parents=True)
            sentinel = work / "build" / "keep"
            sentinel.parent.mkdir()
            sentinel.write_bytes(b"user output")
            with (
                patch.object(diagnostic, "BLEHUB_ROOT", root),
                self.assertRaisesRegex(
                    diagnostic.DiagnosticBuildError, "refusing overwrite"
                ),
            ):
                diagnostic.stage(root, work, root / "patch", root / "config")
            self.assertEqual(sentinel.read_bytes(), b"user output")
            with self.assertRaisesRegex(
                diagnostic.DiagnosticBuildError, "refusing overwrite"
            ):
                diagnostic.check_empty_outputs(work)

    def test_stage_copies_only_tester_and_combines_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "original" / diagnostic.APP
            source.mkdir(parents=True)
            (source / "Kconfig").write_text("source\n")
            (source / "src").mkdir()
            (source / "src" / "btp_gatt.c").write_text("old code\n")
            canonical = root / "canonical.conf"
            canonical.write_bytes(b"CONFIG_BOOTLOADER_MCUBOOT=n\n")
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
            # Mirror the real failure: stage in an ignored subdirectory of a Git repo.
            (root / ".gitignore").write_text("/.work/\n")
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            sentinel = root / diagnostic.APP
            sentinel.mkdir(parents=True)
            (sentinel / "Kconfig").write_text("unrelated source\n")
            work = root / ".work" / "nrf-att-hold"
            with patch.object(diagnostic, "BLEHUB_ROOT", root):
                config = diagnostic.stage(source, work, patch_file, canonical)
            self.assertEqual(
                (work / diagnostic.APP / "Kconfig").read_text(),
                "config BTTESTER_GATT_READ_HOLD\n",
            )
            self.assertEqual(
                (work / diagnostic.APP / "src" / "btp_gatt.c").read_text(),
                "patched code\n",
            )
            self.assertEqual((sentinel / "Kconfig").read_text(), "unrelated source\n")
            self.assertEqual(
                config.read_bytes(),
                b"CONFIG_BOOTLOADER_MCUBOOT=n\nCONFIG_BTTESTER_GATT_READ_HOLD=y\n",
            )
            self.assertEqual((source / "Kconfig").read_text(), "source\n")
            self.assertEqual((source / "src" / "btp_gatt.c").read_text(), "old code\n")
            with (
                patch.object(diagnostic, "BLEHUB_ROOT", root),
                self.assertRaisesRegex(
                    diagnostic.DiagnosticBuildError, "refusing overwrite"
                ),
            ):
                diagnostic.stage(source, work, patch_file, canonical)

    def test_stage_rejects_silent_noop_before_writing_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "original" / diagnostic.APP
            (source / "src").mkdir(parents=True)
            (source / "Kconfig").write_text("source\n")
            (source / "src" / "btp_gatt.c").write_text("old code\n")
            canonical = root / "canonical.conf"
            canonical.write_text("CONFIG_BOOTLOADER_MCUBOOT=n\n")
            work = root / ".work" / "nrf-att-hold"
            with (
                patch.object(diagnostic, "BLEHUB_ROOT", root),
                patch.object(diagnostic, "run", return_value=""),
                self.assertRaisesRegex(
                    diagnostic.DiagnosticBuildError, "did not modify"
                ),
            ):
                diagnostic.stage(source, work, root / "patch.diff", canonical)
            self.assertFalse((work / "diagnostic.conf").exists())
            self.assertEqual((source / "Kconfig").read_text(), "source\n")

    def test_patch_scope_rejects_unexpected_files(self) -> None:
        diagnostic.verify_patch(diagnostic.PATCH)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "patch"
            path.write_text("diff --git a/other b/other\n")
            with self.assertRaisesRegex(
                diagnostic.DiagnosticBuildError, "expected Tester"
            ):
                diagnostic.verify_patch(path)

    def test_generated_config_requires_all_protections_and_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / ".config"
            config.write_text(
                "CONFIG_BTTESTER_GATT_READ_HOLD=y\n"
                "# CONFIG_BOOTLOADER_MCUBOOT is not set\n"
            )
            values = diagnostic.parse_config(config)
            self.assertEqual(values["CONFIG_BOOTLOADER_MCUBOOT"], "n")
            self.assertEqual(values["CONFIG_BTTESTER_GATT_READ_HOLD"], "y")
            diagnostic.validate_config(dict(diagnostic.REQUIRED_CONFIG))
            for name, value in (
                ("CONFIG_BOOTLOADER_MCUBOOT", "y"),
                ("CONFIG_BTTESTER_GATT_READ_HOLD", "n"),
                ("CONFIG_FLASH_LOAD_OFFSET", "0x0"),
            ):
                with self.subTest(name=name):
                    broken = dict(diagnostic.REQUIRED_CONFIG)
                    broken[name] = value
                    with self.assertRaisesRegex(diagnostic.DiagnosticBuildError, name):
                        diagnostic.validate_config(broken)
            broken = dict(diagnostic.REQUIRED_CONFIG)
            del broken["CONFIG_BOARD_HAS_NRF5_BOOTLOADER"]
            with self.assertRaises(diagnostic.DiagnosticBuildError):
                diagnostic.validate_config(broken)

    def test_hex_segments_reject_mbr_bootloader_uicr_and_empty_image(self) -> None:
        self.assertEqual(
            diagnostic.validate_segments([(0x1000, 0xE0000)]), [(0x1000, 0xE0000)]
        )
        for segments in (
            [],
            [(0, 0x1000)],
            [(0xDFFF0, 0xE0001)],
            [(0x1000, 0x1010), (0x10001000, 0x10001004)],
            [(0x1000, 0x1000)],
        ):
            with (
                self.subTest(segments=segments),
                self.assertRaises(diagnostic.DiagnosticBuildError),
            ):
                diagnostic.validate_segments(segments)

    def test_build_manifest_records_diagnostic_identity_only_after_validation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            zephyr = root / "west" / "zephyr"
            zephyr.mkdir(parents=True)
            sdk = root / "sdk"
            sdk.mkdir()
            nrftest = root / "nrftest"
            app = nrftest / "firmware" / "app"
            app.mkdir(parents=True)
            (app / "pca10059.conf").write_text("CONFIG_BOOTLOADER_MCUBOOT=n\n")
            (app / "pca10059.overlay").write_text("/ {};\n")
            work = root / "work"
            dtc = root / "dtc"
            dtc.touch()

            def fake_stage(*_args: object) -> Path:
                work.mkdir()
                config = work / "diagnostic.conf"
                config.write_text("CONFIG_BTTESTER_GATT_READ_HOLD=y\n")
                return config

            def fake_west(command: list[str], *, cwd: Path, env: dict[str, str]) -> str:
                self.assertEqual(cwd, zephyr.parent)
                self.assertEqual(
                    command[:4], ["/managed/west", "build", "--board", diagnostic.BOARD]
                )
                self.assertEqual(env["ZEPHYR_BASE"], str(zephyr))
                self.assertEqual(env["ZEPHYR_SDK_INSTALL_DIR"], str(sdk))
                self.assertIn(str(work / diagnostic.APP), command)
                output = work / "build" / "zephyr"
                output.mkdir()
                (output / ".config").write_text(
                    "".join(
                        f"{key}={value}\n"
                        for key, value in diagnostic.REQUIRED_CONFIG.items()
                    )
                )
                (output / "zephyr.elf").write_bytes(b"elf")
                (output / "zephyr.bin").write_bytes(b"bin")
                image = IntelHex()
                image[0x1000] = 0xAA
                image.write_hex_file(str(output / "zephyr.hex"))
                return ""

            with (
                patch.object(diagnostic, "WORK", work),
                patch.object(
                    diagnostic, "verify_source", return_value=zephyr / diagnostic.APP
                ) as verify,
                patch.object(diagnostic, "stage", side_effect=fake_stage),
                patch.object(diagnostic, "run", side_effect=fake_west),
                patch.object(diagnostic.shutil, "which", return_value="/managed/west"),
            ):
                manifest_path = diagnostic.build(zephyr, sdk, nrftest, dtc)
            self.assertEqual(verify.call_count, 2)
            manifest = json.loads(manifest_path.read_text())
            self.assertEqual(manifest["kind"], "nrf-att-read-hold-diagnostic")
            self.assertEqual(manifest["zephyr_head"], diagnostic.PINNED_HEAD)
            self.assertEqual(manifest["dfu_package"], None)
            self.assertEqual(
                manifest["flash_segments"],
                [{"start": "0x1000", "end_exclusive": "0x1001"}],
            )
            self.assertEqual(
                manifest["inputs"]["patch"]["sha256"],
                diagnostic.sha256_file(diagnostic.PATCH),
            )
            self.assertEqual(manifest["artifacts"][1]["path"], "zephyr/zephyr.hex")
            self.assertEqual(
                manifest["artifacts"][1]["sha256"],
                diagnostic.sha256_file(work / "build" / "zephyr" / "zephyr.hex"),
            )


if __name__ == "__main__":
    unittest.main()
