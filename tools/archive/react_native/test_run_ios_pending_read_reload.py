"""Historical CLI refusal test; excluded from the maintained RN tooling suite."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from tools.archive.react_native import run_ios_pending_read_reload as runner


class PendingReadReloadRunnerTests(unittest.TestCase):
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
