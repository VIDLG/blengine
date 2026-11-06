"""Parent-only ownership/order failures; every device/fixture operation is mocked."""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.peripherals.nrf52840 import permission_transition as wire
from tests.peripherals.nrf52840.cancellation_control import write_document
from tests.support.rf_lab.markers import SERVICE
from tools.android.state_control import AndroidRadioController
from tools.react_native import run_android_permission_transition as api
from tools.tests.react_native.support.permission_host_samples import DEVICE, NAME, RUN


class FinalizationTests(unittest.TestCase):
    def test_restore_failure_and_bundle_failure_preserve_primary_and_owner_order(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            events = []
            primary = RuntimeError("primary permission death failure")
            permission = Mock()
            permission.finish.side_effect = lambda p: (
                events.append("restore"),
                ["grant restoration failed"],
            )[1]
            capture = Mock()
            capture.stop_owned_app.side_effect = lambda: events.append("app-stop")
            radio = Mock(finished=False)
            radio.finish_observation.side_effect = lambda *a, **kw: (
                AndroidRadioController.finish_observation(radio, *a, **kw)
            )
            radio.document = {}
            radio.initial_state = "ON"
            radio._action.side_effect = lambda *a, **k: events.append(
                ("radio-final", a, k)
            )
            radio._release.side_effect = lambda: events.append("release")
            radio._publish.side_effect = lambda: events.append("radio-publish")
            bundle = Mock()

            def finish(**kwargs: object) -> None:
                events.append("bundle")
                self.assertFalse(kwargs["completed"])
                self.assertIs(kwargs["primary_error"], primary)
                self.assertIn("grant restoration failed", kwargs["cleanup_errors"])
                raise OSError("bundle disk failure")

            bundle.finish.side_effect = finish
            stack.enter_context(
                patch.object(api.provider, "_process_identity", return_value=(1, 1))
            )
            stack.enter_context(
                patch.object(
                    api.android_sdk,
                    "run_command",
                    side_effect=lambda *a, **k: events.append("fixture-stop"),
                )
            )
            api._finalize(
                permission=permission,
                radio=radio,
                acquired=True,
                capture=capture,
                identity=(1, 1),
                coordination=root,
                bundle=bundle,
                completed=False,
                primary=primary,
                log=root / wire.RUNTIME_NAME,
            )
            self.assertEqual(events[:2], ["restore", "app-stop"])
            self.assertEqual(
                events[2], ("radio-final", ("final", "ON"), {"change": False})
            )
            self.assertLess(events.index("release"), events.index("fixture-stop"))
            self.assertLess(events.index("fixture-stop"), events.index("bundle"))
            self.assertIn("bundle disk failure", " ".join(primary.__notes__))
            radio.finish.assert_not_called()
            radio.off.assert_not_called()
            radio.on.assert_not_called()

    def test_final_on_failure_stops_only_owned_app_before_releasing_lease(self) -> None:
        events = []
        radio = Mock(document={}, initial_state="ON", finished=False)
        radio._action.side_effect = RuntimeError("final ON missing")
        radio._release.side_effect = lambda: events.append("release")
        errors = AndroidRadioController.finish_observation(
            radio, None, on_failure=lambda: events.append("owned-stop")
        )
        self.assertIn("final ON", errors[0])
        self.assertEqual(events, ["owned-stop", "release"])
        radio.on.assert_not_called()
        radio.off.assert_not_called()

    def test_cleanup_error_without_primary_and_bundle_failure_are_failures(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            permission = Mock()
            permission.finish.return_value = ["restoration readback failed"]
            bundle = Mock()
            bundle.finish.side_effect = OSError("bundle disk failure")
            with self.assertRaisesRegex(
                RuntimeError, "restoration readback failed.*bundle disk failure"
            ):
                api._finalize(
                    permission=permission,
                    radio=None,
                    acquired=False,
                    capture=None,
                    identity=None,
                    coordination=Path(directory),
                    bundle=bundle,
                    completed=True,
                    primary=None,
                    log=Path(directory) / wire.RUNTIME_NAME,
                )
            self.assertFalse(bundle.finish.call_args.kwargs["completed"])


class RunnerTests(unittest.TestCase):
    def test_frozen_install_session_capture_and_initial_final_only_radio(self) -> None:
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            events = []
            commands = []
            # Test private parent cleanup ordering, not the disabled live entry.
            stack.enter_context(patch.object(api, "require_v2_coordination"))
            stack.enter_context(patch.object(api, "PROJECT_ROOT", root))
            stack.enter_context(patch.object(api, "has_root", return_value=False))
            stack.enter_context(
                patch.object(api, "adb_executable", return_value=Path("/sdk/adb"))
            )
            stack.enter_context(
                patch.object(api, "preflight_fixture", return_value=SERVICE)
            )
            stack.enter_context(
                patch.object(
                    api,
                    "verify_install_identity",
                    side_effect=lambda *a: events.append("manifest"),
                )
            )
            stack.enter_context(patch.object(api, "require_multifunction"))
            stack.enter_context(
                patch.object(
                    api,
                    "load_session_profile",
                    return_value=SimpleNamespace(service_uuid=SERVICE, local_name=NAME),
                )
            )
            stack.enter_context(patch.object(api, "guard_fixture"))
            stack.enter_context(patch.object(api, "verify_receipt"))
            fixture = (12345, 12345)
            stack.enter_context(
                patch.object(api, "fixture_identity_from_stdout", return_value=fixture)
            )
            stack.enter_context(
                patch.object(api.provider, "PID_FILE", root / "absent-fixture.pid")
            )
            stack.enter_context(
                patch.object(api.provider, "_port", return_value="/dev/synthetic")
            )
            stack.enter_context(
                patch.object(api.provider, "_process_identity", return_value=fixture)
            )
            plan = None
            attempt = None
            bundle = Mock()
            snapshot = root / "frozen.apk"
            snapshot.write_bytes(b"synthetic not APK")
            bundle.prepare_artifact.return_value = snapshot

            def new_bundle(path: Path, **kwargs: object) -> Mock:
                nonlocal attempt
                attempt = path
                self.assertEqual(kwargs["scenario"], "permission-transition")
                return bundle

            stack.enter_context(
                patch.object(api, "EvidenceBundle", side_effect=new_bundle)
            )

            def prepare(
                contract: Path, profile: Path, session: Path, **kwargs: object
            ) -> SimpleNamespace:
                nonlocal plan
                session.mkdir()
                plan = SimpleNamespace(
                    session_id=RUN,
                    request_path=session / "fixture-request.json",
                    receipt_path=session / "fixture-receipt.json",
                    contract=contract,
                    profile_path=profile,
                )
                write_document(plan.request_path, {"synthetic": "request"})
                return plan

            stack.enter_context(
                patch.object(api, "prepare_session", side_effect=prepare)
            )
            radio = Mock(initial_state="ON", document={}, finished=False)
            radio.finish_observation.side_effect = lambda *a, **kw: (
                AndroidRadioController.finish_observation(radio, *a, **kw)
            )
            radio.acquire.side_effect = lambda: events.append("acquire")
            radio.initial.side_effect = lambda: events.append("initial-ON")
            radio._action.side_effect = lambda *a, **k: events.append("final-ON")
            radio._release.side_effect = lambda: events.append("release")
            radio._publish.side_effect = lambda: write_document(radio.path, {})

            def new_radio(device: str, run: str, path: Path) -> Mock:
                radio.path, radio.run_id, radio.device = path, run, device
                return radio

            stack.enter_context(
                patch.object(api, "AndroidRadioController", side_effect=new_radio)
            )
            permission = Mock()
            permission.initial.side_effect = lambda: events.append(
                "permissions-initial"
            )
            permission.finish.side_effect = lambda *a: (
                events.append("permissions-restore"),
                write_document(permission.path, {}),
                [],
            )[2]

            def new_permission(
                device: str, run: str, path: Path, **kwargs: object
            ) -> Mock:
                self.assertIs(kwargs["use_root"], False)
                permission.path, permission.run_id, permission.device = (
                    path,
                    run,
                    device,
                )
                return permission

            stack.enter_context(
                patch.object(
                    api, "AndroidPermissionController", side_effect=new_permission
                )
            )
            owner = Mock()

            def new_capture(
                device: str,
                log: Path,
                coordination: Path,
                *args: object,
                **kwargs: object,
            ) -> Mock:
                self.assertIs(kwargs["permission"], permission)
                owner.result = {
                    "os_death_proof": "permission-transition-os-death-proof.json",
                    "os_raw_proof": "permission-transition-exit-info.raw.log",
                }
                owner.documents = {}
                for name in (
                    owner.result["os_death_proof"],
                    owner.result["os_raw_proof"],
                    api.capture_owner.RAW_NAME,
                ):
                    (log.parent / name).write_bytes(b"synthetic\n")
                write_document(coordination / wire.PHYSICAL_NAME, {})

                def capture() -> str:
                    events.append("capture")
                    log.write_text("synthetic\n")
                    return "synthetic\n"

                owner.run.side_effect = capture
                return owner

            stack.enter_context(
                patch.object(
                    api.capture_owner, "PermissionCapture", side_effect=new_capture
                )
            )
            stack.enter_context(
                patch.object(
                    api.ledger, "verify_runtime", return_value={"synthetic": True}
                )
            )
            stack.enter_context(patch.object(api.ledger, "verify_physical"))
            stack.enter_context(patch.object(api.ledger, "verify_permission_control"))
            stack.enter_context(patch.object(api.ledger, "verify_state_control"))

            def command(argv: list[str], **kwargs: object) -> SimpleNamespace:
                commands.append(argv)
                self.assertTrue(0 < kwargs["timeout"] <= 300)
                if "direct-start" in argv:
                    write_document(plan.receipt_path, {"synthetic": "live-receipt"})
                if "tools.android.install_apk" in argv:
                    self.assertIn(str(snapshot), argv)
                    events.append("install")
                if "force-stop" in argv:
                    events.append("replace-known")
                if "stop-owned" in argv:
                    events.append("fixture-stop")
                return SimpleNamespace(returncode=0, stdout="synthetic")

            stack.enter_context(
                patch.object(api.android_sdk, "run_command", side_effect=command)
            )
            stack.enter_context(
                patch.object(
                    api.android_sdk,
                    "adb_command",
                    side_effect=lambda d, *a: ["/sdk/adb", "-s", d, *a],
                )
            )
            grant = stack.enter_context(
                patch.object(api.android_sdk, "grant_bluetooth_permissions")
            )
            result = api.run(
                DEVICE,
                root / "source.apk",
                root / "contract.json",
                root / "profile.json",
            )
            self.assertEqual(result, attempt / "evidence-bundle.json")
            self.assertLess(events.index("manifest"), events.index("replace-known"))
            self.assertLess(events.index("initial-ON"), events.index("install"))
            self.assertLess(
                events.index("permissions-initial"), events.index("capture")
            )
            self.assertLess(
                events.index("permissions-restore"), events.index("final-ON")
            )
            self.assertLess(events.index("final-ON"), events.index("release"))
            self.assertTrue(bundle.finish.call_args.kwargs["completed"])
            self.assertEqual(
                json.loads((attempt / "coordination" / wire.CONTROL_NAME).read_bytes()),
                {"schema_version": 1, "run_id": RUN},
            )
            radio.on.assert_not_called()
            radio.off.assert_not_called()
            radio.finish.assert_not_called()
            grant.assert_not_called()
            owner.stop_owned_app.assert_not_called()
            self.assertEqual(sum("force-stop" in c for c in commands), 1)
