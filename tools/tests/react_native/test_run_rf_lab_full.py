"""Parent lifecycle mocks only; SDK pump and semantic gates are tested separately."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.peripherals.nrf52840 import full_suite
from tests.peripherals.nrf52840.cancellation_control import (
    load_suite_controls,
    read_document,
)
from tests.peripherals.nrf52840.recovery_ten import CONTROL_NAME
from tools.tests.react_native.support.recovery_parent import CYCLES, IDENTITY, Harness


class FullParentTests(unittest.TestCase):
    def execute(
        self, platform: str, fault: str = ""
    ) -> tuple[Harness, Mock, Mock, object]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        h = Harness(Path(temporary.name), platform)
        bundle = SimpleNamespace(
            prepare_artifact=Mock(return_value=h.artifact),
            verify_artifact=Mock(),
            finish=Mock(),
        )

        def make_bundle(attempt, **kwargs):
            snapshot = attempt / h.artifact.name
            if h.artifact.is_dir():
                shutil.copytree(h.artifact, snapshot)
            else:
                shutil.copyfile(h.artifact, snapshot)
            bundle.prepare_artifact.return_value = snapshot
            return bundle

        h.api.EvidenceBundle = Mock(side_effect=make_bundle)
        command = h._command

        def checked_command(argv, **kwargs):
            if "direct-start" in argv:
                self.assertEqual(
                    argv[argv.index("--coordination-mode") + 1], full_suite.MODE
                )
                root = Path(argv[argv.index("--coordination-path") + 1])
                controls = load_suite_controls(root)
                self.assertEqual([c.session for c in controls], [h.plan.session_id] * 2)
                self.assertEqual(
                    read_document(root / "recovery" / CONTROL_NAME),
                    {"schema_version": 1, "run_id": h.plan.session_id, "cycles": 10},
                )
            return command(argv, **kwargs)

        h.command.side_effect = checked_command

        def capture(
            platform_, device, log, coordination, run_id, service, name, **kwargs
        ):
            self.assertEqual(kwargs["fixture_identity"], IDENTITY)
            self.assertEqual(run_id, h.plan.session_id)
            self.assertEqual(log.name, "full-suite-runtime.log")
            kwargs["guard"]()
            h.events.append("full-capture")
            if fault == "capture":
                raise RuntimeError("synthetic capture failure")
            log.write_text("opaque mocked full capture; not RF\n", encoding="utf-8")
            return log.read_text(), CYCLES

        def checker(text, **kwargs):
            self.assertEqual(kwargs["fixture_identity"], IDENTITY)
            self.assertEqual(kwargs["host_cycles"], CYCLES)
            self.assertEqual(kwargs["log"].name, "full-suite-runtime.log")
            h.events.append("full-checker")
            if fault == "checker":
                raise RuntimeError("synthetic semantic failure")
            if fault == "foreign-cleanup":
                h.replace_owner()
            return {"status": "passed", "synthetic": "not RF"}

        with (
            patch.object(
                h.api,
                "full_suite_capture",
                SimpleNamespace(capture=Mock(side_effect=capture)),
            ),
            patch.object(
                h.api,
                "full_suite_ledger",
                SimpleNamespace(verify=Mock(side_effect=checker)),
            ),
        ):
            captured = h.api.full_suite_capture.capture
            checked = h.api.full_suite_ledger.verify
            invoke = lambda: h.api.run(
                platform,
                "synthetic-device",
                h.artifact,
                h.contract,
                h.profile,
                timeout=1000,
                scenario="full-suite",
            )
            if fault:
                with self.assertRaises(RuntimeError):
                    invoke()
            else:
                manifest = invoke()
                self.assertEqual(manifest.name, "evidence-bundle.json")
                self.assertTrue((manifest.parent / "full-suite-ledger.json").is_file())
            return h, captured, checked, bundle

    def test_same_shared_parent_no_build_flash_second_capture_or_runner(self) -> None:
        for platform in ("ios", "android"):
            with self.subTest(platform=platform):
                h, capture, checker, bundle = self.execute(platform)
                capture.assert_called_once()
                checker.assert_called_once()
                self.assertFalse(h.capture.called)
                self.assertFalse(h.checker.called)
                self.assertEqual(
                    h.api.EvidenceBundle.call_args.kwargs["scenario"], "full-suite"
                )
                self.assertTrue(bundle.finish.call_args.kwargs["completed"])
                self.assertEqual(bundle.finish.call_args.kwargs["cleanup_errors"], [])
                self.assertEqual(sum("direct-start" in c for c, _ in h.commands), 1)
                self.assertEqual(sum("stop-owned" in c for c, _ in h.commands), 1)
                self.assertFalse(
                    any("flash" in c or "build" in c for c, _ in h.commands)
                )
                self.assertLess(
                    h.events.index("full-checker"), h.events.index("fixture-stop")
                )

    def test_failures_cleanup_only_owned_provider_and_finalize_failed(self) -> None:
        for platform in ("ios", "android"):
            for fault in ("capture", "checker", "foreign-cleanup"):
                with self.subTest(platform=platform, fault=fault):
                    h, capture, _checker, bundle = self.execute(platform, fault)
                    self.assertTrue(
                        (capture.call_args.args[3] / "abort").exists()
                        if fault != "foreign-cleanup"
                        else True
                    )
                    if fault != "foreign-cleanup":
                        self.assertFalse(bundle.finish.call_args.kwargs["completed"])
                        self.assertIsNotNone(
                            bundle.finish.call_args.kwargs["primary_error"]
                        )
                    else:
                        self.assertFalse(any("stop-owned" in c for c, _ in h.commands))
                        self.assertTrue(
                            bundle.finish.call_args.kwargs["cleanup_errors"]
                        )


if __name__ == "__main__":
    unittest.main()
