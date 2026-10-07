"""Linked iOS consumer must contain only the intended UniFFI components."""

import unittest
from unittest.mock import patch

from tools.apple import consumer_bleng_ios as consumer
from tools.apple.consumer_bleng_ios import (
    classify_result,
    validate_linked_images,
    validate_linked_symbols,
)


class BlengIOSSymbolsTests(unittest.TestCase):
    def test_linked_symbols_require_both_components_without_probe(self) -> None:
        both = "_uniffi_blehub_checksum_func_x\n_uniffi_bleng_checksum_func_y\n"
        validate_linked_symbols(both)
        for symbols in (
            "_uniffi_blehub_checksum_func_x\n",
            both + "_uniffi_toy_checksum_func_z\n",
        ):
            with self.assertRaisesRegex(
                RuntimeError, "unexpected iOS consumer UniFFI components"
            ):
                validate_linked_symbols(symbols)
        with self.assertRaisesRegex(RuntimeError, "test-only UniFFI symbol"):
            validate_linked_symbols(both + "_uniffi_bleng_foreignprobe_x\n")

    def test_device_result_requires_current_run_and_explicit_terminal(self) -> None:
        current = "apple_bleng_ios_run_id=current\n"
        passed = "apple_bleng_ios_artifact_consumer=passed\n"
        self.assertIsNone(
            classify_result("apple_bleng_ios_run_id=old\n" + passed, "current")
        )
        self.assertTrue(classify_result(current + passed, "current"))
        with self.assertRaisesRegex(RuntimeError, "reported failure"):
            classify_result(
                current + "apple_bleng_ios_artifact_consumer=failed\n",
                "current",
            )
        with self.assertRaisesRegex(RuntimeError, "no terminal status"):
            classify_result(current, "current")

    def test_device_preflight_rejects_before_signing_when_not_connected(self) -> None:
        with (
            patch.object(consumer.consumer_ios, "executable", return_value="xcrun"),
            patch.object(
                consumer.consumer_ios,
                "device_and_team",
                side_effect=RuntimeError("expected one connected physical iPhone"),
            ),
            patch.object(
                consumer, "device_json", return_value={"result": {"devices": []}}
            ),
            patch.object(consumer, "build") as build,
            self.assertRaisesRegex(RuntimeError, "pair and unlock a wired device"),
        ):
            consumer.device_run()
        build.assert_not_called()

    def test_only_one_wired_paired_device_can_wake_tunnel(self) -> None:
        paired = {
            "identifier": "device-id",
            "hardwareProperties": {"reality": "physical", "udid": "udid"},
            "deviceProperties": {"developerModeStatus": "enabled"},
            "connectionProperties": {
                "pairingState": "paired",
                "transportType": "wired",
                "tunnelState": "disconnected",
            },
        }
        with (
            patch.dict(consumer.os.environ, {"BLEHUB_APPLE_DEVICE": "udid"}),
            patch.object(
                consumer.consumer_ios,
                "device_and_team",
                side_effect=[
                    RuntimeError("expected one connected physical iPhone"),
                    ("device-id", "udid", "TEAM"),
                ],
            ),
            patch.object(
                consumer,
                "device_json",
                side_effect=[{"result": {"devices": [paired]}}, {}],
            ) as call,
        ):
            self.assertEqual(
                consumer.connected_device("xcrun"), ("device-id", "udid", "TEAM")
            )
        self.assertEqual(
            call.call_args.args[1][:4], ["device", "info", "details", "--device"]
        )

    def test_debug_dylib_or_executable_must_be_sole_uniffi_image(self) -> None:
        symbols = "_uniffi_blehub_x\n_uniffi_bleng_y\n"
        validate_linked_images({"App": "_main\n", "App.debug.dylib": symbols})
        validate_linked_images({"App": symbols})
        for images in (
            {"App": "_main\n"},
            {"App": symbols, "App.debug.dylib": symbols},
        ):
            with self.assertRaisesRegex(
                RuntimeError, "exactly one linked UniFFI image"
            ):
                validate_linked_images(images)


if __name__ == "__main__":
    unittest.main()
