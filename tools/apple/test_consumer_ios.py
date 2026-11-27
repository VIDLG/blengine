from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.apple import consumer_ios as consumer


class IOSArtifactReceiptTests(unittest.TestCase):
    identity = "apple_ios_artifact_run_id=current\n"
    resources = "backend_resources=passed\n"
    terminal = "apple_ios_artifact_consumer=passed\n"

    def test_only_current_unambiguous_complete_success_is_accepted(self) -> None:
        passed = self.identity + self.resources + self.terminal
        self.assertTrue(consumer.classify_result(passed, "current"))
        self.assertTrue(
            consumer.classify_result(passed.replace("\n", "\r\n"), "current")
        )
        for stale in ("", passed.replace("=current", "=old")):
            with self.subTest(stale=stale):
                self.assertIsNone(consumer.classify_result(stale, "current"))
        for extra in (self.identity, "apple_ios_artifact_run_id=old\n"):
            with (
                self.subTest(extra=extra),
                self.assertRaisesRegex(RuntimeError, "run identity"),
            ):
                consumer.classify_result(passed + extra, "current")

    def test_reject_missing_ambiguous_or_failed_evidence(self) -> None:
        for resources in (
            "",
            "backend_resources=failed\n",
            "backend_resources=passed-old\n",
            self.resources * 2,
            self.resources + "backend_resources=failed\n",
        ):
            with (
                self.subTest(resources=resources),
                self.assertRaisesRegex(RuntimeError, "backend resources"),
            ):
                consumer.classify_result(
                    self.identity + resources + self.terminal, "current"
                )
        for terminal in (
            "",
            self.terminal * 2,
            "apple_ios_artifact_consumer=passed-old\n",
        ):
            with (
                self.subTest(terminal=terminal),
                self.assertRaisesRegex(RuntimeError, "terminal status"),
            ):
                consumer.classify_result(
                    self.identity + self.resources + terminal, "current"
                )
        for terminal in (
            "apple_ios_artifact_consumer=failed\nerror=resource leak\n",
            self.terminal + "apple_ios_artifact_consumer=failed\n",
        ):
            with (
                self.subTest(terminal=terminal),
                self.assertRaisesRegex(RuntimeError, "reported failure"),
            ):
                consumer.classify_result(
                    self.identity + self.resources + terminal, "current"
                )

    def test_device_runner_uses_classifier_not_just_final_pass_marker(self) -> None:
        # Intercept the actual install/launch/copy boundaries. Synthetic device
        # identities must never escape to devicectl, including on a failing test.
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            for resources, succeeds in (("", False), (self.resources, True)):
                with self.subTest(resources=resources):

                    def copy_receipt(
                        *args: object, resources: str = resources, **kwargs: object
                    ) -> subprocess.CompletedProcess[str]:
                        (work / "result.txt").write_text(
                            self.identity + resources + self.terminal, encoding="utf-8"
                        )
                        return subprocess.CompletedProcess([], 0, "", "")

                    with (
                        patch.object(consumer, "WORK", work),
                        patch.object(
                            consumer, "executable", return_value="synthetic-xcrun"
                        ),
                        patch.object(
                            consumer,
                            "device_and_team",
                            return_value=(
                                "synthetic-device",
                                "synthetic-udid",
                                "TESTTEAM",
                            ),
                        ),
                        patch.object(
                            consumer, "build", return_value=work / "Synthetic.app"
                        ),
                        patch.object(consumer, "device_json", return_value={}),
                        patch.object(consumer.uuid, "uuid4", return_value="current"),
                        patch.object(
                            consumer.subprocess, "run", side_effect=copy_receipt
                        ),
                    ):
                        if succeeds:
                            consumer.device_run()
                        else:
                            with self.assertRaisesRegex(
                                RuntimeError, "backend resources"
                            ):
                                consumer.device_run()


class IOSArtifactPreparationTests(unittest.TestCase):
    def test_missing_backend_stage_is_rejected_before_project_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "uniffi"
            package.mkdir()
            (package / "Package.swift").write_text("// staged\n")
            (package / "BleHubUniFfiNative.xcframework").mkdir()
            backend = root / "backend"
            backend.mkdir()
            for manifest, sources in ((False, False), (True, False), (False, True)):
                with self.subTest(manifest=manifest, sources=sources):
                    (backend / "Package.swift").unlink(missing_ok=True)
                    if manifest:
                        (backend / "Package.swift").write_text("// staged backend\n")
                    source = backend / "Sources/BleHubApple"
                    if sources:
                        source.mkdir(parents=True, exist_ok=True)
                    with (
                        patch.object(
                            consumer.platform, "system", return_value="Darwin"
                        ),
                        patch.object(consumer, "PACKAGE", package),
                        patch.object(consumer, "BACKEND_PACKAGE", backend, create=True),
                        patch.object(consumer, "run") as run,
                        patch.object(consumer.xcodegen_toolchain, "verify") as verify,
                        self.assertRaisesRegex(RuntimeError, "backend package"),
                    ):
                        consumer.generate()
                    run.assert_not_called()
                    verify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
