"""Hardware-free CLI boundary tests for the optional pending-read runner."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from tools.react_native import run_ios_pending_read_reload as runner


class PendingReadReloadRunnerTests(unittest.TestCase):
    def test_delegates_explicit_arguments_without_hardware(self) -> None:
        with patch.object(runner.device, "run") as run:
            self.assertEqual(
                runner.main(
                    [
                        "--udid",
                        "00008110-0016655002D9401E",
                        "--probe-serial",
                        "123456789",
                        "--confirm-hex-sha256",
                        "a" * 64,
                        "--port",
                        "/dev/cu.usbmodem123",
                    ]
                ),
                0,
            )
        run.assert_called_once_with(
            "00008110-0016655002D9401E",
            "123456789",
            "a" * 64,
            "/dev/cu.usbmodem123",
        )

    def test_requires_all_hardware_and_image_identity_arguments(self) -> None:
        with patch.object(runner.device, "run") as run:
            for arguments in (
                [],
                ["--udid", "00008110-0016655002D9401E"],
                [
                    "--udid",
                    "00008110-0016655002D9401E",
                    "--probe-serial",
                    "123456789",
                ],
            ):
                with (
                    self.subTest(arguments=arguments),
                    redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit),
                ):
                    runner.main(arguments)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
