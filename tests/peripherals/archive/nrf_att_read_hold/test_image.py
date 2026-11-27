"""Pure-stdlib regression tests for local image identity and HEX safety."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.peripherals.archive.nrf_att_read_hold import image
from tests.peripherals.nrf52840.support.intel_hex import record


def fixture(work: Path) -> str:
    app = work / image.APP
    app.mkdir(parents=True)
    (app / "prj.conf").write_text("config\n")
    (app / "boards").mkdir()
    (app / "boards/nrf52840dk_nrf52840.conf").write_bytes(b"dk-config")
    (app / "boards/nrf52840dk_nrf52840.overlay").write_bytes(b"dk-overlay")
    (work / "diagnostic-dk.conf").write_text(image.EXTRA_CONFIG)
    build = work / "build" / "zephyr"
    build.mkdir(parents=True)
    (build / ".config").write_text(
        "".join(f"{name}={value}\n" for name, value in image.POLICY.items())
    )
    (build / "zephyr.dts").write_text("/dts-v1/;\n")
    (build / "zephyr.elf").write_bytes(b"ELF")
    (build / "zephyr.bin").write_bytes(
        image.DIGEST_SLOT_MARKER + b"0" * image.DIGEST_SLOT_LENGTH
    )
    (build / "zephyr.hex").write_text(record(0, 0, b"\xaa") + record(1))
    hex_sha256 = image.normalized_image_digest(build / "zephyr.bin")
    contract = image.load_contract(image.CONTRACT)
    manifest = {
        "schema_version": 1,
        "kind": "nrf-att-read-hold-dk-build",
        "image_id": contract.image_id,
        "image_version": contract.image_version,
        "image_sha256": hex_sha256,
        "profile_id": contract.profile_id,
        "service_uuid": contract.service_uuid,
        "local_name": contract.local_name,
        "capabilities": sorted(contract.capabilities),
        "board": image.BOARD,
        "zephyr_head": image.HEAD,
        "application": image.APP.as_posix(),
        "flash_performed": False,
        "source_repository": "https://github.com/VIDLG/zephyr.git",
        "source_tree_sha256": "0" * 64,
        "board_config_sha256": image.BOARD_CONF_SHA256,
        "board_overlay_sha256": image.BOARD_OVERLAY_SHA256,
        "sdk_version": image.SDK_VERSION,
        "build_toolchain": {
            "source": "nrftest-pixi",
            "pixi_lock_sha256": "0" * 64,
        },
        "inputs": {
            name: {
                "path": str(path),
                "sha256": image.tree_digest(path)
                if name == "staged_application"
                else image.digest(path),
            }
            for name, path in (
                ("patch", image.PATCH),
                ("staged_application", app),
                ("board_config", app / "boards/nrf52840dk_nrf52840.conf"),
                ("board_overlay", app / "boards/nrf52840dk_nrf52840.overlay"),
                ("extra_config", work / "diagnostic-dk.conf"),
            )
        },
        "artifacts": [
            {
                "path": name,
                "size": (work / "build" / name).stat().st_size,
                "sha256": image.digest(work / "build" / name),
            }
            for name in image.ARTIFACTS
        ],
        "validated_config": dict(image.POLICY),
        "flash_segments": [{"start": "0x0", "end_exclusive": "0x1"}],
    }
    (work / "build" / "diagnostic-dk-manifest.json").write_text(json.dumps(manifest))
    return image.normalized_image_digest(build / "zephyr.bin")


class ImageTests(unittest.TestCase):
    def test_normalized_digest_zeroes_only_the_unique_slot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "zephyr.bin"
            path.write_bytes(
                b"prefix"
                + image.DIGEST_SLOT_MARKER
                + b"a" * image.DIGEST_SLOT_LENGTH
                + b"suffix"
            )
            expected = hashlib.sha256(
                b"prefix"
                + image.DIGEST_SLOT_MARKER
                + b"0" * image.DIGEST_SLOT_LENGTH
                + b"suffix"
            ).hexdigest()
            self.assertEqual(image.normalized_image_digest(path), expected)
            path.write_bytes(image.DIGEST_SLOT_MARKER * 2 + b"0" * 128)
            with self.assertRaisesRegex(image.ImageError, "exactly once"):
                image.normalized_image_digest(path)
            path.write_bytes(image.DIGEST_SLOT_MARKER + b"!" * 64)
            with self.assertRaisesRegex(image.ImageError, "lowercase hexadecimal"):
                image.normalized_image_digest(path)

    def setUp(self) -> None:
        # Synthetic board bytes keep default-Pixi tests independent of local Zephyr.
        for name, value in (
            ("BOARD_CONF_SHA256", hashlib.sha256(b"dk-config").hexdigest()),
            ("BOARD_OVERLAY_SHA256", hashlib.sha256(b"dk-overlay").hexdigest()),
        ):
            mocked = patch.object(image, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)

    def test_local_only_and_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp) / "work"
            sha = fixture(work)
            result = image.validate_local_image(sha, work=work)
            self.assertEqual(result.sha256, sha)
            self.assertEqual(result.hex_path, work / "build/zephyr/zephyr.hex")
            self.assertFalse(hasattr(result, "installed_sha256"))
            with self.assertRaisesRegex(image.ImageError, "SHA-256 mismatch"):
                image.validate_local_image("0" * 64, work=work)
            with self.assertRaises(image.ImageError):
                image.validate_local_image("not a hash", work=work)
            manifest = work / "build/diagnostic-dk-manifest.json"
            data = json.loads(manifest.read_text())
            data["flash_performed"] = 0  # bool vs int must not silently match
            manifest.write_text(json.dumps(data))
            with self.assertRaisesRegex(image.ImageError, "flash_performed"):
                image.validate_local_image(sha, work=work)
            data["flash_performed"] = False
            data["build_toolchain"]["pixi_lock_sha256"] = "not a sha"
            manifest.write_text(json.dumps(data))
            with self.assertRaisesRegex(image.ImageError, "lowercase SHA-256"):
                image.validate_local_image(sha, work=work)

    def test_mutated_artifacts_and_escaping_links_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work = root / "work"
            sha = fixture(work)
            config = work / image.APP / "boards/nrf52840dk_nrf52840.conf"
            manifest = work / "build/diagnostic-dk-manifest.json"
            original_manifest = manifest.read_text()
            config.write_bytes(b"swapped")
            data = json.loads(original_manifest)
            data["inputs"]["staged_application"]["sha256"] = image.tree_digest(
                work / image.APP
            )
            data["inputs"]["board_config"]["sha256"] = image.digest(config)
            manifest.write_text(json.dumps(data))
            with self.assertRaisesRegex(image.ImageError, "board_config"):
                image.validate_local_image(sha, work=work)
            config.write_bytes(b"dk-config")
            manifest.write_text(original_manifest)
            data = json.loads(original_manifest)
            data["inputs"]["board_overlay"]["sha256"] = "0" * 64
            manifest.write_text(json.dumps(data))
            with self.assertRaisesRegex(image.ImageError, "board_overlay"):
                image.validate_local_image(sha, work=work)
            data["inputs"]["board_overlay"]["sha256"] = image.BOARD_OVERLAY_SHA256
            manifest.write_text(json.dumps(data))
            (work / "build/zephyr/zephyr.elf").write_bytes(b"changed")
            with self.assertRaisesRegex(image.ImageError, "artifact changed"):
                image.validate_local_image(sha, work=work)
            sha = fixture_after_elf(work)
            outside = root / "outside.hex"
            outside.write_text(record(0, 0, b"\xaa") + record(1))
            path = work / "build/zephyr/zephyr.hex"
            path.unlink()
            path.symlink_to(outside)
            with self.assertRaisesRegex(image.ImageError, "linked path"):
                image.validate_local_image(sha, work=work)
            path.unlink()
            path.write_text(outside.read_text())
            manifest = work / "build/diagnostic-dk-manifest.json"
            manifest.unlink()
            manifest.symlink_to(root / "outside.hex")
            with self.assertRaisesRegex(image.ImageError, "linked path"):
                image.validate_local_image(sha, work=work)

    def test_hex_boundaries_and_malformed_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "test.hex"
            path.write_text(
                record(4, data=b"\x00\x0f") + record(0, 0xFFFF, b"\x01") + record(1)
            )
            self.assertEqual(image.hex_segments(path), [(0xFFFFF, 0x100000)])
            bad = [
                record(4, data=b"\x00\x10") + record(0, 0, b"x") + record(1),
                record(4, data=b"\x10\x00") + record(0, 0, b"x") + record(1),
                record(5, data=b"\x10\x00\x00\x00") + record(0, 0, b"x") + record(1),
                record(0, 0, b"x") + record(0, 0, b"y") + record(1),
                record(0, 0, b"x"),
                record(0, 0, b"x") + record(1) + record(0, 1, b"y"),
                record(0, 0, b"x")[:-3] + "00\n" + record(1),
            ]
            for text in bad:
                with self.subTest(text=text):
                    path.write_text(text)
                    with self.assertRaises(image.ImageError):
                        image.hex_segments(path)


def fixture_after_elf(work: Path) -> str:
    manifest = work / "build/diagnostic-dk-manifest.json"
    data = json.loads(manifest.read_text())
    data["artifacts"][2]["size"] = (work / "build/zephyr/zephyr.elf").stat().st_size
    data["artifacts"][2]["sha256"] = image.digest(work / "build/zephyr/zephyr.elf")
    manifest.write_text(json.dumps(data))
    return image.normalized_image_digest(work / "build/zephyr/zephyr.bin")


if __name__ == "__main__":
    unittest.main()
