"""Hardware-free parent ownership/rollback tests, not adapter/RF validation."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import signal
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from tests.peripherals.nrf52840.adapter_transition import (
    CONTROL_NAME,
    MODE,
    PHYSICAL_NAME,
)
from tests.support.rf_lab.markers import SERVICE
from tools.react_native import adapter_transition_capture
from tools.react_native import adapter_transition_ledger as ledger
from tools.react_native import run_android_adapter_transition as module
from tools.tests.react_native.support.adapter_transition_samples import (
    DEVICE,
    RUN,
    bodies,
    physical_sidecars,
    state_control,
)


class Harness:
    def __init__(self, root: Path) -> None:
        spec = importlib.util.spec_from_file_location(
            "synthetic_adapter_parent", module.__file__
        )
        assert spec is not None and spec.loader is not None
        self.api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.api)
        # Parent ownership tests do not authorize the unmigrated live gate.
        self.api.require_v2_coordination = Mock()
        self.api.PROJECT_ROOT = root
        self.events: list[str] = []
        self.commands: list[list[str]] = []
        self.identity = (12345, 12345)
        self.failure: str | None = None
        self.primary = RuntimeError("primary failure")
        self.cleanup_errors: list[str] = []
        self.phone_alive = True
        self.fixture_alive = True
        self.lose_phone_after: str | None = None
        self.raw_padding = 0
        self.controller = Mock()
        self.controller.acquire.side_effect = lambda: self.step("acquire")
        self.controller.initial.side_effect = lambda: self.step("initial")
        self.controller.off.side_effect = lambda: self.step("off")
        self.controller.on.side_effect = lambda: self.step("on")
        self.controller.finish.side_effect = self.control_finish
        self.controller.require_owner.side_effect = self.require_phone
        self.api.write_document = Mock(side_effect=self.write_document)
        self.api._new_controller = Mock(side_effect=self.new_controller)
        self.api.preflight_fixture = Mock(return_value=SERVICE)
        self.api.verify_install_identity = Mock(
            side_effect=lambda *a: self.step("install-identity")
        )
        self.api.require_multifunction = Mock(side_effect=lambda plan: plan)
        self.api.load_session_profile = Mock(
            return_value=SimpleNamespace(
                service_uuid=SERVICE,
                local_name="BleHubNrf52840",
            )
        )
        self.api.prepare_session = Mock(side_effect=self.prepare)
        self.api.verify_receipt = Mock(side_effect=self.verify_receipt)
        self.api.guard_fixture = Mock(side_effect=self.guard_fixture)
        self.api.fixture_identity_from_stdout = Mock(return_value=self.identity)
        self.api.provider = SimpleNamespace(
            PID_FILE=root / "fixture.pid",
            _port=Mock(return_value="/dev/synthetic"),
            DEFAULT_READY_TIMEOUT=5,
            _process_identity=Mock(return_value=self.identity),
        )
        self.api.android_sdk = SimpleNamespace(
            adb_command=lambda device, *args: ["/sdk/adb", "-s", device, *args],
            run_command=Mock(side_effect=self.command),
            grant_bluetooth_permissions=Mock(
                side_effect=lambda device: self.step("grant")
            ),
        )
        self.api.capture_owner = SimpleNamespace(
            validate_inputs=adapter_transition_capture.validate_inputs,
            capture=Mock(side_effect=self.capture),
            abort_owned_app=Mock(side_effect=self.abort_app),
        )
        self.previous_handler = object()
        self.api.signal = SimpleNamespace(
            SIGTERM=signal.SIGTERM,
            SIG_IGN=signal.SIG_IGN,
            getsignal=Mock(return_value=self.previous_handler),
            signal=Mock(),
        )
        self.bundle = Mock()
        self.bundle.prepare_artifact.side_effect = self.freeze
        self.bundle.verify_artifact.side_effect = lambda: self.step("verify-artifact")
        self.bundle.finish.side_effect = self.bundle_finish
        self.api.EvidenceBundle = Mock(side_effect=self.new_bundle)
        self.apk = root / "source.apk"
        self.apk.write_bytes(b"synthetic not executable APK")
        self.snapshot = root / "frozen.apk"
        self.snapshot.write_bytes(self.apk.read_bytes())
        self.root = root
        self.attempt: Path | None = None
        self.plan = None
        self.finish_kwargs = None
        self.receipt_raw = b""

    def step(self, label: str) -> None:
        if label in {"initial", "install", "force-stop", "clear", "grant", "off", "on"}:
            assert self.events[-1] == "phone-guard", (
                f"unguarded phone mutation: {label}"
            )
        self.events.append(label)
        if label == self.lose_phone_after:
            self.phone_alive = False
        if self.failure == label:
            raise self.primary

    def require_phone(self) -> None:
        self.step("phone-guard")
        if not self.phone_alive:
            raise RuntimeError("phone owner lost")

    def guard_fixture(self, identity: tuple[int, int]) -> None:
        self.step("guard")
        if not self.fixture_alive:
            raise RuntimeError("fixture exited")

    def abort_app(self, *args: object, **kwargs: object) -> None:
        kwargs["phone_guard"]()
        self.step("app-abort")

    def write_document(self, path: Path, value: dict) -> None:
        if path.name == "go":
            self.step("go")
            assert (
                "off" not in self.events
                if value.get("adapter_mode") == "active-loss"
                else "off" in self.events
            )
            assert "capture" not in self.events
            assert self.phone_alive
        module.write_document(path, value)

    def new_bundle(self, attempt: Path, **kwargs: object) -> Mock:
        self.attempt = attempt
        self.step("bundle")
        return self.bundle

    def freeze(self, apk: Path) -> Path:
        self.step("freeze")
        return self.snapshot

    def prepare(
        self, contract: Path, profile: Path, root: Path, **kwargs: object
    ) -> SimpleNamespace:
        self.step("prepare")
        root.mkdir()
        self.plan = SimpleNamespace(
            session_id=RUN,
            root=root,
            request_path=root / "fixture-request.json",
            receipt_path=root / "fixture-receipt.json",
            contract=object(),
            profile_path=profile,
        )
        runtime = ledger.verify_runtime(self.text(), RUN, SERVICE, Path("runtime.log"))
        request, receipt, self.physical = physical_sidecars(runtime)
        self.plan.request_path.write_text(json.dumps(request, indent=3) + "\n")
        self.receipt_raw = (json.dumps(receipt, indent=4) + "\n").encode()
        self.physical["receipt_sha256"] = hashlib.sha256(self.receipt_raw).hexdigest()
        self.physical["readiness"]["receipt_sha256"] = self.physical["receipt_sha256"]
        return self.plan

    def verify_receipt(self, plan: object, **kwargs: object) -> dict:
        self.step("receipt")
        return json.loads(self.receipt_raw)

    def new_controller(self, device: str, run_id: str, path: Path) -> Mock:
        self.control_path = path
        self.step("controller")
        return self.controller

    def control_finish(self, primary: BaseException | None) -> list[str]:
        self.step("control-finish")
        self.control_path.write_text(json.dumps(state_control()))
        return list(self.cleanup_errors)

    def command(self, command: list[str], **kwargs: object) -> SimpleNamespace:
        self.commands.append(command)
        if "direct-start" in command:
            self.step("fixture-start")
            self.plan.receipt_path.write_bytes(self.receipt_raw)
        elif "tools.android.install_apk" in command:
            self.step("install")
        elif "force-stop" in command:
            self.step("force-stop")
        elif "-c" in command:
            self.step("clear")
        elif "getprop" in command:
            return SimpleNamespace(stdout="34\n", returncode=0)
        elif "dumpsys" in command:
            self.step("permission-readback")
            return SimpleNamespace(
                stdout=(
                    "  android.permission.BLUETOOTH_SCAN: granted=true, flags=[]\n"
                    "  android.permission.BLUETOOTH_CONNECT: granted=true, flags=[]\n"
                ),
                returncode=0,
            )
        elif "stop-owned" in command:
            self.step("fixture-stop")
        else:
            raise AssertionError(f"unexpected synthetic command: {command}")
        self.assert_bounded(kwargs)
        return SimpleNamespace(stdout="synthetic stdout", returncode=0)

    @staticmethod
    def assert_bounded(kwargs: dict) -> None:
        if not 0 < kwargs["timeout"] <= 300 or kwargs.get("check") is not True:
            raise AssertionError("unbounded/unverified command")

    @staticmethod
    def text() -> str:
        return "".join(ledger.PREFIX + body + "\n" for body in bodies())

    def capture(
        self,
        device: str,
        log: Path,
        coordination: Path,
        *args: object,
        **kwargs: object,
    ) -> str:
        self.step("capture")
        self.assert_go(coordination)
        kwargs["guard"]()
        kwargs["phone_guard"]()
        kwargs["radio_on"]()
        text = self.text()
        log.write_text(text)
        log.with_suffix(".raw.log").write_text("diagnostic\n" * self.raw_padding + text)
        self.physical["go_sha256"] = hashlib.sha256(
            (coordination / "go").read_bytes()
        ).hexdigest()
        (coordination / "adapter-transition-ready.json").write_text(
            json.dumps(self.physical["readiness"])
        )
        (coordination / PHYSICAL_NAME).write_text(json.dumps(self.physical))
        if self.failure == "physical-hash":
            self.physical["receipt_sha256"] = "0" * 64
            (coordination / PHYSICAL_NAME).write_text(json.dumps(self.physical))
        if self.failure == "receipt-drift":
            self.plan.receipt_path.write_bytes(self.receipt_raw + b" ")
        if self.failure == "fixture-exited":
            self.fixture_alive = False
        return text

    @staticmethod
    def assert_go(coordination: Path) -> None:
        assert json.loads((coordination / "go").read_text()) == {
            "schema_version": 1,
            "run_id": RUN,
            "adapter_mode": "startup-off",
        }
        assert not list(coordination.glob("*.tmp"))

    def bundle_finish(self, **kwargs: object) -> None:
        self.finish_kwargs = kwargs
        self.step("bundle-finish")

    def run(self, **kwargs: object) -> Path:
        return self.api.run(
            DEVICE, self.apk, self.root / "contract", self.root / "profile", **kwargs
        )


class AdapterParentTests(unittest.TestCase):
    def test_active_mode_installs_on_and_defers_off_to_capture_owned_handshake(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(Path(directory))

            def stop_before_fault(*args: object, **kwargs: object) -> str:
                self.assertEqual(kwargs["mode"], "active-loss")
                self.assertIs(kwargs["radio_off"], h.controller.off)
                h.controller.off.assert_not_called()
                self.assertEqual(
                    json.loads(
                        (h.attempt / "coordination" / CONTROL_NAME).read_bytes()
                    )["adapter_mode"],
                    "active-loss",
                )
                self.assertEqual(
                    json.loads((h.attempt / "coordination/go").read_bytes())[
                        "adapter_mode"
                    ],
                    "active-loss",
                )
                raise RuntimeError("intentional active boundary stop")

            h.api.capture_owner.capture.side_effect = stop_before_fault
            with self.assertRaisesRegex(RuntimeError, "intentional active boundary"):
                h.run(mode="active-loss")
            h.controller.off.assert_not_called()
            h.controller.finish.assert_called_once()
            self.assertFalse(h.finish_kwargs["completed"])
            self.assertLess(
                h.events.index("control-finish"), h.events.index("fixture-stop")
            )

    def test_large_radio_journal_is_not_limited_as_a_fixture_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            h = Harness(Path(temporary))

            def finish(primary: BaseException | None) -> list[str]:
                errors = h.control_finish(primary)
                value = json.loads(h.control_path.read_text())
                for action in value["actions"]:
                    for receipt in action["receipts"]:
                        if "dumpsys" in receipt["command"]:
                            receipt["stdout"] = (
                                "diagnostic=" + "x" * 300_000 + "\n" + receipt["stdout"]
                            )
                h.control_path.write_text(json.dumps(value))
                return errors

            h.controller.finish.side_effect = finish
            h.run()
            self.assertGreater(h.control_path.stat().st_size, 1024 * 1024)
            self.assertLess(h.control_path.stat().st_size, h.api.MAX_BYTES)
            self.assertTrue(h.finish_kwargs["completed"])
            self.assertEqual(h.finish_kwargs["cleanup_errors"], [])
            self.assertLess(
                h.events.index("control-finish"), h.events.index("fixture-stop")
            )
            self.assertLess(
                h.events.index("fixture-stop"), h.events.index("bundle-finish")
            )

    def test_phone_owner_covers_install_off_capture_and_final_then_fixture_bundle(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            h = Harness(Path(temporary))
            manifest = h.run()
            self.assertEqual(manifest.parent, h.attempt)
            self.assertTrue(manifest.parent.name.startswith("rf-adapter-transition-"))
            ordered = (
                "freeze",
                "install-identity",
                "prepare",
                "controller",
                "acquire",
                "initial",
                "fixture-start",
                "install",
                "force-stop",
                "clear",
                "grant",
                "permission-readback",
                "off",
                "go",
                "capture",
                "on",
                "control-finish",
                "fixture-stop",
                "bundle-finish",
            )
            positions = [h.events.index(label) for label in ordered]
            self.assertEqual(positions, sorted(positions))
            self.assertEqual(sum("force-stop" in c for c in h.commands), 1)
            install = next(c for c in h.commands if "tools.android.install_apk" in c)
            self.assertEqual(install[install.index("--apk") + 1], str(h.snapshot))
            self.assertTrue(h.finish_kwargs["completed"])
            self.assertEqual(h.finish_kwargs["cleanup_errors"], [])
            self.assertEqual(h.controller.finish.call_args.args, (None,))
            start = next(c for c in h.commands if "direct-start" in c)
            self.assertEqual(start[start.index("--coordination-mode") + 1], MODE)
            self.assertFalse(any("flash" in arg for c in h.commands for arg in c))
            control = json.loads(
                (h.attempt / "coordination" / CONTROL_NAME).read_text()
            )
            self.assertEqual(
                control,
                {"schema_version": 1, "run_id": RUN, "adapter_mode": "startup-off"},
            )
            value = json.loads(
                (h.attempt / "adapter-transition-ledger.json").read_text()
            )
            self.assertEqual(value["status"], "passed")
            self.assertEqual(
                h.api.capture_owner.capture.call_args.kwargs["timeout"], 180
            )
            self.assertEqual(h.api.signal.signal.call_args.args[1], h.previous_handler)

    def test_lock_refusal_never_installs_toggles_finishes_or_stops_someone_else(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            h = Harness(Path(temporary))
            h.failure = "acquire"
            with self.assertRaises(RuntimeError) as caught:
                h.run()
            self.assertIs(caught.exception, h.primary)
            h.controller.finish.assert_not_called()
            self.assertFalse(h.commands)
            self.assertFalse(h.finish_kwargs["completed"])

    def test_initial_on_precondition_and_every_downstream_failure_finalizes_owner(
        self,
    ) -> None:
        for stage in (
            "initial",
            "fixture-start",
            "install",
            "grant",
            "off",
            "capture",
            "on",
        ):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary:
                h = Harness(Path(temporary))
                h.failure = stage
                with self.assertRaises(RuntimeError) as caught:
                    h.run()
                self.assertIs(caught.exception, h.primary)
                h.controller.finish.assert_called_once_with(h.primary)
                self.assertTrue((h.attempt / "coordination" / "abort").exists())
                self.assertFalse(h.finish_kwargs["completed"])
                self.assertEqual(h.events.count(stage), 1)
                if stage == "initial":
                    self.assertFalse(h.commands)
                if "fixture-stop" in h.events:
                    self.assertLess(
                        h.events.index("control-finish"), h.events.index("fixture-stop")
                    )

    def test_cleanup_errors_do_not_mask_primary_and_bundle_always_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            h = Harness(Path(temporary))
            h.failure = "capture"
            h.cleanup_errors = ["restore/final failed"]
            h.api.provider._process_identity.return_value = (999, 999)
            with self.assertRaises(RuntimeError) as caught:
                h.run()
            self.assertIs(caught.exception, h.primary)
            self.assertIn("restore/final failed", " ".join(h.primary.__notes__))
            self.assertIn("refusing cleanup", " ".join(h.primary.__notes__))
            self.assertNotIn("fixture-stop", h.events)
            self.assertFalse(h.finish_kwargs["completed"])
        for stage in ("control-finish", "fixture-stop", "bundle-finish"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary:
                h = Harness(Path(temporary))
                h.failure = stage
                with self.assertRaises(RuntimeError):
                    h.run()
                self.assertEqual(h.events.count("bundle-finish"), 1)
                if stage != "bundle-finish":
                    self.assertFalse(h.finish_kwargs["completed"])

    def test_pure_physical_hash_and_live_receipt_bytes_are_not_bypassed(self) -> None:
        for stage, expected in (
            ("physical-hash", "hash"),
            ("receipt-drift", "bytes changed"),
        ):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary:
                h = Harness(Path(temporary))
                h.failure = stage
                with self.assertRaisesRegex((ValueError, RuntimeError), expected):
                    h.run()
                self.assertFalse(h.finish_kwargs["completed"])
                self.assertEqual(h.events.count("capture"), 1)
                self.assertIn("app-abort", h.events)
                self.assertLess(
                    h.events.index("app-abort"), h.events.index("control-finish")
                )

    def test_permission_readback_rejects_denied_missing_or_ambiguous(self) -> None:
        for output in (
            "",
            "android.permission.BLUETOOTH_SCAN: granted=false\n",
            (
                "android.permission.BLUETOOTH_SCAN: granted=true\n" * 2
                + "android.permission.BLUETOOTH_CONNECT: granted=true\n"
            ),
        ):
            with (
                self.subTest(output=output),
                tempfile.TemporaryDirectory() as temporary,
            ):
                h = Harness(Path(temporary))
                original = h.command

                def command(
                    argv: list[str],
                    *,
                    output=output,
                    original=original,
                    **kwargs: object,
                ) -> SimpleNamespace:
                    if "dumpsys" in argv:
                        return SimpleNamespace(stdout=output, returncode=0)
                    return original(argv, **kwargs)

                h.api.android_sdk.run_command.side_effect = command
                with self.assertRaisesRegex(RuntimeError, "permission readback"):
                    h.run()
                h.controller.off.assert_not_called()
                h.api.capture_owner.capture.assert_not_called()

    def test_phone_owner_rechecked_between_each_parent_mutation(self) -> None:
        for stage in ("install", "force-stop", "clear", "grant", "off"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary:
                h = Harness(Path(temporary))
                h.lose_phone_after = stage
                with self.assertRaisesRegex(RuntimeError, "phone owner lost"):
                    h.run()
                self.assertFalse(h.finish_kwargs["completed"])
                self.assertNotIn("capture", h.events)
                h.controller.finish.assert_called_once()
                self.assertFalse((h.attempt / "coordination/go").exists())

    def test_parent_cleanup_uses_phone_owner_even_after_fixture_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            h = Harness(Path(temporary))
            h.failure = "fixture-exited"
            with self.assertRaisesRegex(RuntimeError, "fixture exited"):
                h.run()
            self.assertIn("app-abort", h.events)
            self.assertFalse(h.finish_kwargs["completed"])
            callback = h.api.capture_owner.abort_owned_app.call_args.kwargs[
                "phone_guard"
            ]
            self.assertIs(callback, h.controller.require_owner)
            self.assertIs(
                h.api.capture_owner.capture.call_args.kwargs["phone_guard"],
                h.controller.require_owner,
            )

    def test_go_never_published_before_off_or_on_prepare_failures(self) -> None:
        for stage in ("install", "grant", "permission-readback", "off"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary:
                h = Harness(Path(temporary))
                h.failure = stage
                with self.assertRaises(RuntimeError):
                    h.run()
                self.assertNotIn("go", h.events)
                self.assertFalse((h.attempt / "coordination/go").exists())

    def test_raw_log_above_one_mib_passes_and_eight_mib_is_the_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            h = Harness(Path(temporary))
            h.raw_padding = 110_000
            h.run()
            raw = h.attempt / "adapter-transition-runtime.raw.log"
            self.assertGreater(raw.stat().st_size, 1024 * 1024)
            self.assertTrue(h.finish_kwargs["completed"])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "bounded.log"
            with path.open("wb") as stream:
                stream.truncate(module.MAX_BYTES + 1)
            with self.assertRaisesRegex(ValueError, "oversized"):
                module._read_small(path, module.MAX_BYTES)
            with path.open("wb") as stream:
                stream.truncate(1024 * 1024 + 1)
            with self.assertRaises(RuntimeError):
                module._read_bytes(
                    path
                )  # Session JSON keeps its independent 1 MiB bound.

    def test_invalid_device_timeout_precedes_any_work_and_cli_requires_explicit_inputs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            h = Harness(Path(temporary))
            for timeout in (0, 301, float("inf"), float("nan"), True):
                with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                    h.run(timeout=timeout)
            for device in ("", " a", "-x", "a\nb"):
                with self.subTest(device=device), self.assertRaises(ValueError):
                    h.api.run(device, h.apk, h.root / "contract", h.root / "profile")
            h.api.EvidenceBundle.assert_not_called()
            h.api.preflight_fixture.assert_not_called()


if __name__ == "__main__":
    unittest.main()
