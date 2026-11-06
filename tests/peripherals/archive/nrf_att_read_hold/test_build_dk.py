"""Hardware-free staging, pin and build-publish boundary tests."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.peripherals.archive.nrf_att_read_hold import build_dk, image
from tests.peripherals.nrf52840.support.intel_hex import record


class BuildDKTests(unittest.TestCase):
    def test_owned_patch_pin_and_source_revision_rejection_without_zephyr(self) -> None:
        build_dk.verify_patch()
        with tempfile.TemporaryDirectory() as temp:
            zephyr = Path(temp) / "zephyr"
            zephyr.mkdir()
            with (
                patch.object(
                    build_dk, "run", side_effect=[str(zephyr), "wrong-commit"]
                ),
                self.assertRaisesRegex(image.ImageError, "pinned commit"),
            ):
                build_dk.verify_source(zephyr)

    def test_reference_inputs_require_existing_local_pixi_tools_and_absolute_paths(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            reference = Path(temp) / "nrftest"
            reference.mkdir()
            (reference / "pixi.toml").write_text("[workspace]\nname = 'nrftest'\n")
            (reference / "pixi.lock").write_bytes(b"pinned tool environment")
            zephyr = reference / "west/zephyr"
            sdk = reference / "sdk"
            zephyr.mkdir(parents=True)
            sdk.mkdir()
            (reference / "nrftest.local.toml").write_text(
                f'[toolchains]\nzephyr_root = "{zephyr}"\nzephyr_sdk_root = "{sdk}"\n'
            )
            with self.assertRaisesRegex(image.ImageError, "not installed: west"):
                build_dk.reference_inputs(reference)
            bin_dir = reference / ".pixi/envs/default/bin"
            bin_dir.mkdir(parents=True)
            for name in ("west", "dtc", "cmake", "ninja", "gperf"):
                (bin_dir / name).touch()
            inputs = build_dk.reference_inputs(reference)
            self.assertEqual(
                (inputs.zephyr, inputs.sdk, inputs.bin_dir), (zephyr, sdk, bin_dir)
            )
            self.assertEqual(
                inputs.pixi_lock_sha256, image.digest(reference / "pixi.lock")
            )
            (reference / "nrftest.local.toml").write_text(
                '[toolchains]\nzephyr_root = "relative"\nzephyr_sdk_root = "relative"\n'
            )
            with self.assertRaisesRegex(image.ImageError, "absolute zephyr_root"):
                build_dk.reference_inputs(reference)

    def test_west_modules_must_match_pinned_manifest_before_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            zephyr = workspace / "zephyr"
            module = workspace / "modules" / "test"
            zephyr.mkdir()
            module.mkdir(parents=True)
            (workspace / ".west").mkdir()
            (workspace / ".west/config").write_text(
                "[manifest]\npath = zephyr\nfile = west.yml\n", encoding="utf-8"
            )
            revision = "a" * 40
            heads = {zephyr: image.HEAD, module: revision}
            dirty: set[Path] = set()
            listing = f"manifest|{zephyr}|HEAD\ntest|{module}|{revision}"

            def response(command: list[str], *, cwd: Path) -> str:
                if command[1] == "list":
                    return listing
                if command[1] == "status":
                    return " M west.yml" if cwd in dirty else ""
                if command[1] == "rev-parse" and command[-1] == "HEAD":
                    return heads[cwd]
                if command[1] == "rev-parse":
                    return revision
                raise AssertionError(command)

            with patch.object(build_dk, "run", side_effect=response):
                build_dk.verify_workspace(zephyr, "/independent/west")
                heads[module] = "b" * 40
                with self.assertRaisesRegex(
                    image.ImageError, "revision mismatch: test"
                ):
                    build_dk.verify_workspace(zephyr, "/independent/west")
                heads[module] = revision
                listing = f"manifest|{zephyr}|HEAD\ntest|{module}|HEAD"
                with self.assertRaisesRegex(
                    image.ImageError, "unpinned revision: test"
                ):
                    build_dk.verify_workspace(zephyr, "/independent/west")
                listing = f"manifest|{zephyr}|HEAD\ntest|{module}|{revision}\ntest|{module}|{revision}"
                with self.assertRaisesRegex(
                    image.ImageError, "duplicate identity: test"
                ):
                    build_dk.verify_workspace(zephyr, "/independent/west")
                listing = f"manifest|{zephyr}|HEAD\ntest|{module}|{revision}"
                dirty.add(module)
                with self.assertRaisesRegex(image.ImageError, "local changes: test"):
                    build_dk.verify_workspace(zephyr, "/independent/west")
                dirty.clear()
                dirty.add(zephyr)
                with self.assertRaisesRegex(
                    image.ImageError, "manifest has local changes"
                ):
                    build_dk.verify_workspace(zephyr, "/independent/west")

    def test_even_nonfatal_west_warnings_fail_closed(self) -> None:
        result = subprocess.CompletedProcess(
            args=["west", "build"],
            returncode=0,
            stdout="",
            stderr="warning: Kconfig mismatch",
        )
        with (
            patch.object(build_dk.subprocess, "run", return_value=result),
            self.assertRaisesRegex(image.ImageError, "build warning"),
        ):
            build_dk.run(["west", "build"], cwd=Path("."))

    def test_sdk_version_and_arm_toolchain_are_required(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            sdk = Path(temp)
            (sdk / "sdk_version").write_text("wrong")
            with self.assertRaisesRegex(image.ImageError, "version 1.0.1"):
                build_dk.verify_sdk(sdk)
            (sdk / "sdk_version").write_text(image.SDK_VERSION)
            with self.assertRaisesRegex(image.ImageError, "ARM compiler"):
                build_dk.verify_sdk(sdk)

    def test_stage_is_copy_only_and_refuses_existing_or_linked_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            source = root / "original" / image.APP
            (source / "src").mkdir(parents=True)
            (source / "Kconfig").write_text("source\n")
            (source / "src/btp_gatt.c").write_text("old\n")
            (source / "prj.conf").write_text("CONFIG_TEST=y\nCONFIG_BT_SUBRATING=y\n")
            patch_file = root / "patch.diff"
            patch_file.write_text(
                "diff --git a/tests/bluetooth/tester/Kconfig b/tests/bluetooth/tester/Kconfig\n"
                "--- a/tests/bluetooth/tester/Kconfig\n+++ b/tests/bluetooth/tester/Kconfig\n"
                "@@ -1 +1 @@\n-source\n+config BTTESTER_GATT_READ_HOLD\n"
                "diff --git a/tests/bluetooth/tester/src/btp_gatt.c b/tests/bluetooth/tester/src/btp_gatt.c\n"
                "--- a/tests/bluetooth/tester/src/btp_gatt.c\n+++ b/tests/bluetooth/tester/src/btp_gatt.c\n"
                "@@ -1 +1 @@\n-old\n+new\n"
            )
            old_work = root / ".work/nrf-att-hold-dk"
            old_work.mkdir(parents=True)
            (old_work / "spike-evidence").write_text("keep")
            work = root / ".work/nrf-att-hold-maintained-dk"
            with (
                patch.multiple(build_dk, ROOT=root, WORK=work),
                patch.object(image, "PATCH", patch_file),
                patch.object(build_dk, "verify_patch"),
            ):
                build_dk.stage(source, work=work)
                self.assertEqual((source / "Kconfig").read_text(), "source\n")
                self.assertEqual((work / image.APP / "prj.conf").read_text(), "")
                with self.assertRaisesRegex(image.ImageError, "refusing overwrite"):
                    build_dk.stage(source, work=work)
            self.assertEqual((old_work / "spike-evidence").read_text(), "keep")
            outside = root / "outside"
            outside.mkdir()
            linked = root / ".work/nrf-att-hold-maintained-dk"
            # Existing output is never removed just to accept a symlink.
            self.assertTrue(linked.is_dir())
            with (
                patch.object(build_dk, "ROOT", root),
                self.assertRaises(image.ImageError),
            ):
                build_dk.stage(source, work=root / ".work/other")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "actual").mkdir()
            (root / ".work").symlink_to(root / "actual", target_is_directory=True)
            with (
                patch.object(build_dk, "ROOT", root),
                self.assertRaisesRegex(image.ImageError, "linked path"),
            ):
                build_dk.stage(root, work=root / ".work/nrf-att-hold-maintained-dk")
            self.assertFalse((root / "actual/nrf-att-hold-maintained-dk").exists())

    def test_build_refuses_missing_tools_before_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            zephyr = root / "west/zephyr"
            zephyr.mkdir(parents=True)
            (root / "west/.west").mkdir()
            (root / "west/.west/config").touch()
            sdk = root / "sdk"
            sdk.mkdir()
            (sdk / "sdk_version").write_text(image.SDK_VERSION)
            compiler = (
                sdk
                / "gnu/arm-zephyr-eabi/bin"
                / (
                    "arm-zephyr-eabi-gcc.exe"
                    if build_dk.os.name == "nt"
                    else "arm-zephyr-eabi-gcc"
                )
            )
            compiler.parent.mkdir(parents=True)
            compiler.touch()
            work = root / ".work/nrf-att-hold-maintained-dk"
            with (
                patch.object(
                    build_dk, "verify_source", return_value=zephyr / image.APP
                ),
                patch.object(build_dk, "verify_patch"),
                patch.object(build_dk, "WORK", work),
                patch.object(build_dk.shutil, "which", return_value=None),
                self.assertRaisesRegex(image.ImageError, "west and dtc"),
            ):
                build_dk.build(zephyr, sdk)
            self.assertFalse(work.exists())

    def test_build_publishes_validated_manifest_only_after_good_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            zephyr = root / "west/zephyr"
            source = zephyr / image.APP
            source.mkdir(parents=True)
            (source / "Kconfig").write_text("original")
            (root / "west/.west").mkdir()
            (root / "west/.west/config").touch()
            sdk = root / "sdk"
            sdk.mkdir()
            (sdk / "sdk_version").write_text(image.SDK_VERSION)
            compiler = (
                sdk
                / "gnu/arm-zephyr-eabi/bin"
                / (
                    "arm-zephyr-eabi-gcc.exe"
                    if build_dk.os.name == "nt"
                    else "arm-zephyr-eabi-gcc"
                )
            )
            compiler.parent.mkdir(parents=True)
            compiler.touch()
            dtc = root / "dtc"
            dtc.touch()
            work = root / ".work/nrf-att-hold-maintained-dk"

            def fake_stage(_source: Path) -> None:
                staged = work / image.APP
                staged.mkdir(parents=True)
                (staged / "Kconfig").write_text("patched")
                (staged / "boards").mkdir()
                (staged / "boards/nrf52840dk_nrf52840.conf").write_bytes(b"dk-config")
                (staged / "boards/nrf52840dk_nrf52840.overlay").write_bytes(
                    b"dk-overlay"
                )
                (work / "diagnostic-dk.conf").write_text(image.EXTRA_CONFIG)

            def fake_run(cmd: list[str], *, cwd: Path, env: dict[str, str]) -> str:
                self.assertEqual(
                    cmd[:4], ["/local/west", "build", "--board", image.BOARD]
                )
                self.assertEqual(cwd, zephyr.parent)
                self.assertEqual(env["ZEPHYR_BASE"], str(zephyr))
                z = work / "build/zephyr"
                z.mkdir()
                (z / ".config").write_text(
                    "".join(f"{k}={v}\n" for k, v in image.POLICY.items())
                )
                (z / "zephyr.dts").write_text("/dts-v1/;\n")
                (z / "zephyr.elf").write_bytes(b"elf")
                (z / "zephyr.bin").write_bytes(
                    image.DIGEST_SLOT_MARKER + b"0" * image.DIGEST_SLOT_LENGTH
                )
                (z / "zephyr.hex").write_text(record(0, 0, b"\xaa") + record(1))
                return ""

            synthetic_image_sha = hashlib.sha256(
                image.DIGEST_SLOT_MARKER + b"0" * image.DIGEST_SLOT_LENGTH
            ).hexdigest()
            synthetic_contract = replace(
                image.load_contract(image.CONTRACT), image_sha256=synthetic_image_sha
            )
            with (
                patch.object(image, "load_contract", return_value=synthetic_contract),
                patch.object(build_dk, "verify_source", return_value=source),
                patch.object(build_dk, "verify_patch"),
                patch.object(build_dk, "WORK", work),
                patch.object(image, "WORK", work),
                patch.object(build_dk, "stage", side_effect=fake_stage),
                patch.object(build_dk, "verify_workspace") as workspace_check,
                patch.object(build_dk, "run", side_effect=fake_run),
                patch.object(build_dk.shutil, "which", return_value="/local/west"),
                patch.object(
                    image, "BOARD_CONF_SHA256", hashlib.sha256(b"dk-config").hexdigest()
                ),
                patch.object(
                    image,
                    "BOARD_OVERLAY_SHA256",
                    hashlib.sha256(b"dk-overlay").hexdigest(),
                ),
            ):
                manifest = build_dk.build(zephyr, sdk, dtc=dtc)
                self.assertEqual(workspace_check.call_count, 2)
                data = json.loads(manifest.read_text())
                self.assertEqual(data["inputs"]["patch"]["sha256"], image.PATCH_SHA256)
                self.assertEqual(
                    data["inputs"]["board_config"]["sha256"], image.BOARD_CONF_SHA256
                )
                self.assertEqual(
                    data["inputs"]["board_overlay"]["sha256"],
                    image.BOARD_OVERLAY_SHA256,
                )
                self.assertFalse(data["flash_performed"])
                self.assertEqual(
                    image.validate_local_image(
                        image.normalized_image_digest(work / "build/zephyr/zephyr.bin"),
                        work=work,
                    ).manifest_path,
                    manifest,
                )


if __name__ == "__main__":
    unittest.main()
