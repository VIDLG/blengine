"""Hardware-free parent lifecycle tests, not ten-cycle RF/capture validation.

Each harness loads a private parent module and replaces only its dependencies.
No shared signal, subprocess, provider, capture, session or bundle module is
patched. Real session/receipt parsers and artifact/bundle validation still run;
the independently tested capture and pure checker are explicit boundaries.
"""

from __future__ import annotations

import json
import math
import signal
import subprocess
import tempfile
import unittest
import zipfile
import zlib
from collections.abc import Callable
from pathlib import Path
from unittest.mock import call

from tests.peripherals.nrf52840 import fixture_session, provider
from tools.react_native import recovery_ten_capture, recovery_ten_ledger
from tools.react_native import rf_evidence_bundle as evidence
from tools.react_native import rf_scenario_runner as parent
from tools.react_native.ios_device import IosDevicePreconditionError
from tools.tests.react_native.support.recovery_parent import (
    CYCLES,
    IDENTITY,
    Harness,
)


class RecoveryTenParentTests(unittest.TestCase):
    def assert_restored(self, h: Harness) -> None:
        self.assertEqual(len(h.handlers.call_args_list), 3)
        self.assertTrue(callable(h.handlers.call_args_list[0].args[1]))
        self.assertEqual(h.handlers.call_args_list[0].args[0], signal.SIGTERM)
        self.assertEqual(
            h.handlers.call_args_list[1:],
            [
                call(signal.SIGTERM, signal.SIG_IGN),
                call(signal.SIGTERM, h.previous_handler),
            ],
        )

    def assert_no_pass(self, h: Harness) -> None:
        self.assertFalse(
            any(
                "rf-recovery-ten:passed" in str(c.args[0])
                or "recovery-ten completed" in str(c.args[0])
                for c in h.api.print.call_args_list
            )
        )
        document = h.manifest()
        if document is not None:
            self.assertNotEqual(document["status"], "passed")

    def test_both_platforms_install_exact_snapshot_and_prove_lifecycle_before_finalization(
        self,
    ) -> None:
        for platform in ("android", "ios"):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as tmp:
                h = Harness(Path(tmp), platform)
                manifest = h.run(timeout=7)
                self.assertEqual(h.manifest()["status"], "passed")
                self.assertEqual(manifest, h.attempt / evidence.MANIFEST)
                install = next(
                    argv
                    for argv, _ in h.commands
                    if "install" in argv or "tools.android.install_apk" in argv
                )
                installed = (
                    Path(install[install.index("--apk") + 1])
                    if platform == "android"
                    else Path(install[-1])
                )
                self.assertEqual(installed, h.snapshot)
                self.assertNotEqual(installed, h.artifact)
                self.assertEqual(
                    fixture_session.load_session(h.plan.request_path), h.plan
                )
                control = json.loads(
                    (h.plan.root.parent / h.api.CONTROL_NAME).read_bytes()
                )
                self.assertEqual(
                    control,
                    {"schema_version": 1, "run_id": h.plan.session_id, "cycles": 10},
                )
                startup, startup_options = next(
                    (argv, kw) for argv, kw in h.commands if "direct-start" in argv
                )
                self.assertEqual(
                    startup[startup.index("--coordination-mode") + 1],
                    "recovery-ten-attested",
                )
                self.assertEqual(
                    Path(startup[startup.index("--fixture-request") + 1]),
                    h.plan.request_path,
                )
                self.assertEqual(
                    startup_options,
                    {"timeout": provider.DEFAULT_READY_TIMEOUT + 15, "check": True},
                )
                args, kwargs = h.capture.call_args
                profile = fixture_session.load_session_profile(
                    h.plan.contract, h.plan.profile_path
                )
                self.assertEqual(
                    args,
                    (
                        platform,
                        "synthetic-device",
                        h.attempt / "recovery-ten-runtime.log",
                        h.plan.root.parent,
                        h.plan.session_id,
                        profile.service_uuid,
                        profile.local_name,
                    ),
                )
                self.assertEqual(kwargs["timeout"], 7)
                self.assertTrue(callable(kwargs["guard"]))
                self.assertEqual(
                    kwargs["install_json"],
                    h.attempt / "device-install.json" if platform == "ios" else None,
                )
                self.assertEqual(
                    h.receipt.call_count, 5 if platform == "android" else 4
                )
                self.assertEqual(
                    h.events[h.events.index("capture-enter") - 1], "receipt"
                )
                self.assertEqual(
                    h.events[
                        h.events.index("capture-return") + 1 : h.events.index("checker")
                    ],
                    ["receipt"],
                )
                self.assertLess(
                    h.events.index("checker"), h.events.index("fixture-stop")
                )
                self.assertLess(
                    h.events.index("fixture-stop"), h.events.index("finish")
                )
                for key, value in {
                    "platform": platform,
                    "run_id": h.plan.session_id,
                    "service_uuid": profile.service_uuid,
                    "fixture_request": h.plan.request_path,
                    "fixture_identity": IDENTITY,
                    "physical_path": h.plan.root.parent / h.api.PHYSICAL_NAME,
                    "host_cycles": CYCLES,
                }.items():
                    self.assertEqual(h.checker.call_args.kwargs[key], value)
                stop, options = next(
                    (argv, kw) for argv, kw in h.commands if "stop-owned" in argv
                )
                self.assertEqual(stop[-4:], ["--pid", "1234", "--pgid", "1234"])
                self.assertEqual(options, {"timeout": 35, "check": True})
                self.assertFalse(h.pid_file.exists())
                self.assertFalse((h.plan.root.parent / "abort").exists())
                self.assertTrue(h.finish_arguments["completed"])
                self.assertIsNone(h.finish_arguments["primary_error"])
                self.assertEqual(h.finish_arguments["cleanup_errors"], [])
                if platform == "ios":
                    h.ready.assert_called_once_with("synthetic-device", cwd=h.root)
                    self.assertLess(
                        h.events.index("ios-ready"), h.events.index("prepare")
                    )
                    self.assertEqual(
                        (h.snapshot / "Resources/opaque-resource").read_bytes(),
                        (h.artifact / "Resources/opaque-resource").read_bytes(),
                    )
                else:
                    h.ready.assert_not_called()
                    self.assertEqual(h.snapshot.read_bytes(), h.artifact.read_bytes())
                    self.assertEqual(
                        h.events[h.events.index("force-stop") - 1], "receipt"
                    )
                self.assert_restored(h)

    def test_runtime_resources_and_native_evidence_are_copied_without_fabrication(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(Path(tmp), "ios")
            self.assertEqual(h.main(), 0)
            for path, content in h.captured_files.items():
                self.assertEqual(path.read_bytes(), content)
            resources = h.manifest()["flow_index"]["resources"]
            self.assertEqual(len(resources), 50)
            self.assertTrue(
                any(
                    "synthetic:failed:text" in content.decode()
                    for content in h.captured_files.values()
                )
            )
            self.assertTrue(
                any(
                    "rf-recovery-ten:passed" in str(c.args[0])
                    for c in h.api.print.call_args_list
                )
            )

    def test_invalid_apk_freeze_never_touches_fixture_or_phone(self) -> None:
        for invalid in ("missing", "not-zip", "symlink"):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as tmp:
                h = Harness(Path(tmp), "android")
                if invalid == "missing":
                    h.artifact.unlink()
                elif invalid == "not-zip":
                    h.artifact.write_bytes(b"not an APK")
                else:
                    saved = h.root / "saved.apk"
                    h.artifact.rename(saved)
                    h.artifact.symlink_to(saved)
                with self.assertRaises(
                    (OSError, ValueError, RuntimeError, zipfile.BadZipFile)
                ):
                    h.run()
                self.assertEqual(h.commands, [])
                h.provider._port.assert_not_called()
                h.capture.assert_not_called()
                self.assert_no_pass(h)
                self.assert_restored(h)

    def test_snapshot_not_mutable_source_and_snapshot_drift_blocks_install(
        self,
    ) -> None:
        for drift in (False, True):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as tmp:
                h = Harness(Path(tmp), "android")
                if drift:
                    h.faults.before_verify = lambda h=h: h.snapshot.write_bytes(
                        b"corrupted snapshot"
                    )
                    with self.assertRaises(
                        (ValueError, RuntimeError, zipfile.BadZipFile)
                    ):
                        h.run()
                    self.assertNotIn("install", h.events)
                    self.assertNotIn("force-stop", h.events)
                    self.assert_no_pass(h)
                else:
                    before = h.artifact.read_bytes()
                    h.faults.after_freeze = lambda h=h: h.artifact.write_bytes(
                        b"later unrelated source build"
                    )
                    h.run()
                    self.assertEqual(h.snapshot.read_bytes(), before)
                self.assert_restored(h)

    def test_ios_available_unlocked_preflight_fails_before_fixture_changes(
        self,
    ) -> None:
        for reason in ("device not available", "device locked"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as tmp:
                h = Harness(Path(tmp), "ios")
                primary = IosDevicePreconditionError(reason)
                h.ready.side_effect = primary
                with self.assertRaises(IosDevicePreconditionError) as caught:
                    h.run()
                self.assertIs(caught.exception, primary)
                h.ready.assert_called_once_with("synthetic-device", cwd=h.root)
                h.provider._port.assert_not_called()
                self.assertNotIn("prepare", h.events)
                self.assertEqual(h.commands, [])
                self.assert_no_pass(h)
                self.assert_restored(h)

    def test_constructor_freeze_and_session_failures_preserve_cause_without_native_work(
        self,
    ) -> None:
        for platform in ("android", "ios"):
            for point in ("constructor", "freeze", "prepare"):
                with (
                    self.subTest(platform=platform, point=point),
                    tempfile.TemporaryDirectory() as tmp,
                ):
                    h = Harness(Path(tmp), platform)
                    primary = RuntimeError("original " + point)
                    setattr(h.faults, point, primary)
                    with self.assertRaises(RuntimeError) as caught:
                        h.run()
                    self.assertIs(caught.exception, primary)
                    self.assertNotIn("fixture-start", h.events)
                    self.assertNotIn("install", h.events)
                    self.assertNotIn("native-launch", h.events)
                    self.assertNotIn("fixture-stop", h.events)
                    if point == "constructor":
                        self.assertNotIn("finish", h.events)
                    else:
                        self.assertIs(h.finish_arguments["primary_error"], primary)
                        self.assertEqual(h.manifest()["status"], "failed")
                    self.assert_no_pass(h)
                    self.assert_restored(h)

    def test_live_receipt_required_before_any_android_phone_operation(self) -> None:
        for phase in ("initial", "after-install", "permissions"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                h = Harness(Path(tmp), "android")
                if phase == "permissions":
                    h.faults.permissions = h.corrupt_receipt
                else:
                    real_receipt = h.receipt.side_effect
                    target = 1 if phase == "initial" else 2

                    def receipt(
                        *args: object,
                        h: Harness = h,
                        target: int = target,
                        real_receipt: Callable[..., dict] = real_receipt,
                        **kwargs: object,
                    ) -> dict:
                        if h.receipt.call_count == target:
                            h.corrupt_receipt()
                        return real_receipt(*args, **kwargs)

                    h.receipt.side_effect = receipt
                with self.assertRaises(fixture_session.FixtureSessionError):
                    h.run()
                if phase == "initial":
                    self.assertNotIn("install", h.events)
                if phase != "permissions":
                    self.assertNotIn("force-stop", h.events)
                    h.permissions.assert_not_called()
                h.capture.assert_not_called()
                h.checker.assert_not_called()
                self.assertIn("fixture-stop", h.events)
                self.assert_no_pass(h)
                self.assert_restored(h)

    def test_before_capture_and_after_capture_receipt_drift_blocks_launch_or_checker(
        self,
    ) -> None:
        for platform in ("android", "ios"):
            for phase in ("before-capture", "after-capture"):
                with (
                    self.subTest(platform=platform, phase=phase),
                    tempfile.TemporaryDirectory() as tmp,
                ):
                    h = Harness(Path(tmp), platform)
                    if phase == "after-capture":
                        h.faults.after_capture = h.corrupt_receipt
                    else:

                        def before_verify(h: Harness = h) -> None:
                            if "install" in h.events:
                                h.corrupt_receipt()

                        h.faults.before_verify = before_verify
                    with self.assertRaises(fixture_session.FixtureSessionError):
                        h.run()
                    if phase == "before-capture":
                        h.capture.assert_not_called()
                    else:
                        self.assertIn("capture-return", h.events)
                    h.checker.assert_not_called()
                    self.assert_no_pass(h)
                    self.assert_restored(h)

    def test_install_capture_and_physical_checker_failures_keep_original_and_stop_owned(
        self,
    ) -> None:
        for platform in ("android", "ios"):
            for point in ("install", "capture", "checker"):
                with (
                    self.subTest(platform=platform, point=point),
                    tempfile.TemporaryDirectory() as tmp,
                ):
                    h = Harness(Path(tmp), platform)
                    primary = (
                        subprocess.CalledProcessError(7, "installer")
                        if point == "install"
                        else RuntimeError("original " + point)
                    )
                    setattr(h.faults, point, primary)
                    with self.assertRaises(type(primary)) as caught:
                        h.run()
                    self.assertIs(caught.exception, primary)
                    self.assertIs(h.finish_arguments["primary_error"], primary)
                    self.assertFalse(h.finish_arguments["completed"])
                    self.assertEqual(
                        h.events[h.events.index("finish") - 1], "fixture-stop"
                    )
                    self.assertEqual(
                        (h.plan.root.parent / "abort").read_text(), "parent-failed\n"
                    )
                    self.assertEqual(h.manifest()["status"], "failed")
                    self.assert_no_pass(h)
                    self.assert_restored(h)

    def test_only_authoritative_stdout_grants_ownership_never_existing_or_foreign_marker(
        self,
    ) -> None:
        for point in (
            "existing",
            "failed-start",
            "invalid-stdout",
            "duplicate-stdout",
            "replaced-start",
            "replaced-after-checker",
        ):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as tmp:
                h = Harness(Path(tmp), "android")
                if point == "existing":
                    h.replace_owner()
                elif point == "failed-start":
                    h.faults.foreign_on_start = True
                    h.faults.start = subprocess.CalledProcessError(2, "direct-start")
                elif point == "invalid-stdout":
                    h.faults.foreign_on_start = True
                    h.faults.start_stdout = "provider PID=1234 PGID=1234\n"
                elif point == "duplicate-stdout":
                    h.faults.foreign_on_start = True
                    h.faults.start_stdout += "nrf52840_fixture_pid=1234\n"
                elif point == "replaced-start":
                    h.faults.foreign_on_start = True
                else:
                    h.faults.after_checker = h.replace_owner
                with self.assertRaises((RuntimeError, subprocess.CalledProcessError)):
                    h.run()
                self.assertNotIn("fixture-stop", h.events)
                self.assertEqual(h.pid_file.read_text(), "9999 9999\n")
                if point != "replaced-after-checker":
                    self.assertNotIn("install", h.events)
                    self.assertNotIn("native-launch", h.events)
                self.assert_no_pass(h)
                self.assert_restored(h)

    def test_owner_replacement_after_primary_failure_cannot_be_adopted_during_cleanup(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(Path(tmp), "ios")
            primary = RuntimeError("original native failure")

            def fail(*_args: object, **_kwargs: object) -> None:
                h.replace_owner()
                raise primary

            h.capture.side_effect = fail
            with self.assertRaises(RuntimeError) as caught:
                h.run()
            self.assertIs(caught.exception, primary)
            self.assertIn("refusing cleanup", " ".join(primary.__notes__))
            self.assertNotIn("fixture-stop", h.events)
            self.assertEqual(h.pid_file.read_text(), "9999 9999\n")
            self.assert_no_pass(h)
            self.assert_restored(h)

    def test_unexpected_zlib_finalization_and_stop_failures_do_not_replace_primary(
        self,
    ) -> None:
        for platform in ("android", "ios"):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as tmp:
                h = Harness(Path(tmp), platform)
                primary = RuntimeError("original capture failure")
                h.faults.capture = primary
                h.faults.stop = OSError("owned fixture stop failed")
                h.faults.finish = zlib.error("unexpected compressed evidence failure")
                with self.assertRaises(RuntimeError) as caught:
                    h.run()
                self.assertIs(caught.exception, primary)
                notes = " ".join(primary.__notes__)
                self.assertIn("owned fixture stop failed", notes)
                self.assertIn("unexpected compressed evidence failure", notes)
                self.assertIs(h.finish_arguments["primary_error"], primary)
                self.assertEqual(
                    h.finish_arguments["cleanup_errors"],
                    ["fixture cleanup: owned fixture stop failed"],
                )
                self.assert_no_pass(h)
                self.assert_restored(h)

    def test_success_cannot_hide_owned_stop_or_unexpected_finalizer_failure(
        self,
    ) -> None:
        for point in ("stop", "finish"):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as tmp:
                h = Harness(Path(tmp), "android")
                setattr(
                    h.faults,
                    point,
                    zlib.error("unexpected finalizer failure")
                    if point == "finish"
                    else OSError("owned stop failure"),
                )
                self.assertEqual(h.main(), 2)
                self.assertTrue(h.finish_arguments["completed"])
                self.assertIsNone(h.finish_arguments["primary_error"])
                self.assert_no_pass(h)
                self.assert_restored(h)

    def test_passing_checker_cannot_make_false_markers_or_resource_copier_pass_bundle(
        self,
    ) -> None:
        for fault in ("summary-only", "missing-cycle", "closed-root-leak"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as tmp:
                h = Harness(Path(tmp), "android")

                def invalid(raw: bytes, fault: str = fault) -> bytes:
                    lines = raw.splitlines(keepends=True)
                    if fault == "summary-only":
                        return lines[-1]
                    if fault == "missing-cycle":
                        return b"".join(
                            line for line in lines if b"cycle:9:passed" not in line
                        )
                    position = next(
                        i
                        for i, line in enumerate(lines)
                        if b"resources:cycle=0:phase=closed:" in line
                    )
                    prefix, payload = lines[position].split(b"snapshot=", 1)
                    snapshot = json.loads(payload)
                    snapshot["hubRoots"] = 1
                    lines[position] = (
                        prefix + b"snapshot=" + json.dumps(snapshot).encode() + b"\n"
                    )
                    return b"".join(lines)

                h.faults.runtime = invalid
                self.assertEqual(h.main(), 2)
                h.checker.assert_called_once()
                self.assert_no_pass(h)
                for path, content in h.captured_files.items():
                    self.assertEqual(path.read_bytes(), content)
                self.assert_restored(h)

    def test_abort_publication_failure_still_stops_finishes_and_restores_handler(
        self,
    ) -> None:
        for platform in ("android", "ios"):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as tmp:
                h = Harness(Path(tmp), platform)
                primary = RuntimeError("original capture error")

                def fail_capture(
                    *_args: object,
                    owner: Harness = h,
                    failure: RuntimeError = primary,
                    **_kwargs: object,
                ) -> None:
                    (owner.plan.root.parent / "abort").mkdir()
                    raise failure

                h.capture.side_effect = fail_capture
                with self.assertRaises(RuntimeError) as caught:
                    h.run()
                self.assertIs(caught.exception, primary)
                self.assertEqual(h.events[h.events.index("finish") - 1], "fixture-stop")
                self.assertIs(h.finish_arguments["primary_error"], primary)
                self.assertIn(
                    "abort publication:", h.finish_arguments["cleanup_errors"][0]
                )
                self.assertIn("abort publication:", " ".join(primary.__notes__))
                self.assert_no_pass(h)
                self.assert_restored(h)

    def test_sigterm_interrupt_preserves_error_aborts_stops_owned_and_restores_handler(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(Path(tmp), "ios")

            def interrupt(*_args: object, **_kwargs: object) -> None:
                h.handlers.call_args_list[0].args[1](signal.SIGTERM, None)

            h.capture.side_effect = interrupt
            with self.assertRaisesRegex(InterruptedError, "SIGTERM") as caught:
                h.run()
            self.assertIs(h.finish_arguments["primary_error"], caught.exception)
            self.assertIn("fixture-stop", h.events)
            self.assertEqual(
                (h.plan.root.parent / "abort").read_text(), "parent-failed\n"
            )
            self.assert_no_pass(h)
            self.assert_restored(h)

    def test_startup_install_capture_and_cleanup_budgets_are_explicit_and_bounded(
        self,
    ) -> None:
        for platform in ("android", "ios"):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as tmp:
                h = Harness(Path(tmp), platform)
                h.run(timeout=0.25)
                self.assertEqual(h.capture.call_args.kwargs["timeout"], 0.25)
                for _, kwargs in h.commands:
                    self.assertTrue(math.isfinite(kwargs["timeout"]))
                    self.assertGreater(kwargs["timeout"], 0)
                    self.assertTrue(kwargs["check"])
                start = next(kw for argv, kw in h.commands if "direct-start" in argv)
                self.assertEqual(start["timeout"], provider.DEFAULT_READY_TIMEOUT + 15)
                install = next(
                    kw
                    for argv, kw in h.commands
                    if "install" in argv or "tools.android.install_apk" in argv
                )
                self.assertEqual(
                    install["timeout"], 300 if platform == "android" else 120
                )
                self.assert_restored(h)
        for timeout in (0, -1, 901, math.nan, math.inf):
            with self.subTest(timeout=timeout), tempfile.TemporaryDirectory() as tmp:
                h = Harness(Path(tmp), "android")
                with self.assertRaises(ValueError):
                    h.run(timeout=timeout)
                self.assertEqual(h.commands, [])
                h.handlers.assert_not_called()
                self.assertEqual(h.events, [])

    def test_harness_does_not_patch_shared_modules_or_real_signal_handlers(
        self,
    ) -> None:
        original = (
            parent.provider,
            parent.android_sdk.run_command,
            fixture_session.prepare_session,
            recovery_ten_capture.capture,
            recovery_ten_ledger.verify,
            evidence.EvidenceBundle,
            signal.getsignal(signal.SIGTERM),
        )
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(Path(tmp), "android")
            h.run()
            self.assertEqual(
                (
                    parent.provider,
                    parent.android_sdk.run_command,
                    fixture_session.prepare_session,
                    recovery_ten_capture.capture,
                    recovery_ten_ledger.verify,
                    evidence.EvidenceBundle,
                    signal.getsignal(signal.SIGTERM),
                ),
                original,
            )


if __name__ == "__main__":
    unittest.main()
