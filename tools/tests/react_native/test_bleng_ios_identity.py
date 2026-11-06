"""Formal iOS pinning at the real ios_sdk subprocess seam; never an actual SDK."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tools.react_native import bleng_scan_host as host
from tools.react_native import ios_sdk
from tools.react_native.ios_radio_lease import SCAN_OBSERVATION_SCOPE, IosRadioLease


class IosIdentityTests(unittest.TestCase):
    device = "00008110-001234567890ABCD"
    core_device = "130256F7-7150-5A81-A906-6806AAFBFADA"
    run_id = "12345678-1234-4234-8234-123456789abc"
    boot = "23456789-2345-4345-8345-23456789abcd"
    app_id = "dev.blehub.formal-host"
    executable = (
        "file:///private/var/containers/Bundle/Application/"
        "11111111-2222-4333-8444-555555555555/FormalHost.app/FormalHost"
    )
    digest = "ab" * 32

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.clock = 100.0
        self.events: list[str] = []
        self.commands: list[list[str]] = []
        self.challenges: list[str] = []
        self.response: dict | bytes | None = None
        self.responses: list[dict | bytes] = []
        self.sleeps: list[float] = []
        self.native = {
            "run_id": self.run_id,
            "boot_id": self.boot,
            "pid": 321,
            "bundle_id": self.app_id,
            "executable": self.executable.removeprefix("file://"),
        }
        self.processes = [{"processIdentifier": 321, "executable": self.executable}]
        self.process_device = self.core_device
        self.on_command = None
        self.enterContext(patch("time.monotonic", side_effect=lambda: self.clock))
        self.sleep_mock = self.enterContext(
            patch.object(ios_sdk.time, "sleep", side_effect=self.sleep)
        )
        self.sdk = self.enterContext(
            patch.object(ios_sdk.subprocess, "run", side_effect=self.subprocess)
        )
        self.phone = IosRadioLease(
            self.device,
            self.run_id,
            self.root / "phone.json",
            lock_root=self.root / "locks",
            scope=SCAN_OBSERVATION_SCOPE,
        )
        self.phone_owner_mock = self.enterContext(
            patch.object(self.phone, "require_owner", side_effect=self.phone_owner)
        )
        self.owner = Mock(side_effect=lambda: self.events.append("protocol"))
        self.provenance = Mock(side_effect=lambda: self.events.append("provenance"))
        self.candidate = Mock(return_value=self.digest)
        self.driver = SimpleNamespace(
            session_id="session-1",
            capabilities={
                "platformName": "iOS",
                "appium:udid": self.device,
                "appium:bundleId": self.app_id,
            },
        )
        self.install = self.root / "install.json"
        self.launch = self.root / "launch.json"
        self.attempt = self.root / "attempt"
        self.attempt.mkdir()
        self.write(
            self.install,
            self.receipt(
                "devicectl.device.install.app",
                {
                    "installedApplications": [
                        {
                            "bundleID": self.app_id,
                            "installationURL": self.executable.rsplit("/", 1)[0] + "/",
                        }
                    ],
                },
                [str(self.root / "frozen.app")],
            ),
        )
        self.write(
            self.launch,
            self.receipt(
                "devicectl.device.process.launch",
                {
                    "process": self.processes[0].copy(),
                },
                [self.app_id, "--blehub-formal-identity-run-id", self.run_id],
            ),
        )

    def phone_owner(self) -> None:
        self.events.append("phone")

    def sleep(self, seconds: float) -> None:
        self.assertGreater(seconds, 0)
        self.assertLessEqual(seconds, min(0.1, 105.0 - self.clock))
        self.sleeps.append(seconds)
        self.clock += seconds

    def inspection_commands(self) -> list[list[str]]:
        return [
            command
            for command in self.commands
            if "--source" in command
            and command[command.index("--source") + 1].endswith(
                "/native-inspection.json"
            )
        ]

    def assert_single_request_and_post(self) -> None:
        self.assertEqual(
            sum(command[3:5] == ["copy", "to"] for command in self.commands), 1
        )
        self.assertEqual(
            sum(command[3:5] == ["notification", "post"] for command in self.commands),
            1,
        )

    def write(self, path: Path, value: dict | bytes) -> None:
        path.write_bytes(
            value if isinstance(value, bytes) else json.dumps(value).encode()
        )

    def receipt(self, kind: str, result: dict, suffix: list[str] | None = None) -> dict:
        return {
            "info": {
                "outcome": "success",
                "commandType": kind,
                "arguments": [
                    *kind.split("."),
                    "--device",
                    self.device,
                    *(suffix or []),
                ],
            },
            "result": {"deviceIdentifier": self.core_device, **result},
        }

    def subprocess(
        self, command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess:
        self.assertEqual(command[:3], ["xcrun", "devicectl", "device"])
        action = tuple(command[3:5])
        self.assertIn(
            action,
            {
                ("info", "processes"),
                ("copy", "from"),
                ("copy", "to"),
                ("notification", "post"),
            },
        )
        self.assertEqual(command[command.index("--device") + 1], self.device)
        self.assertEqual(kwargs["cwd"], self.root)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertGreater(kwargs["timeout"], 0)
        self.assertLessEqual(kwargs["timeout"], 105.0 - self.clock)
        self.assertLessEqual(
            int(command[command.index("--timeout") + 1]), 105.0 - self.clock
        )
        self.commands.append(command)
        self.events.append("sdk")
        if self.on_command:
            self.on_command(command)
        kind = "devicectl.device." + ".".join(action)
        result = {}
        if action == ("info", "processes"):
            result = {
                "runningProcesses": self.processes,
                "deviceIdentifier": self.process_device,
            }
        elif action[0] == "copy":
            self.assertEqual(
                command[command.index("--domain-type") + 1], "appDataContainer"
            )
            self.assertEqual(
                command[command.index("--domain-identifier") + 1], self.app_id
            )
            source = command[command.index("--source") + 1]
            destination = command[command.index("--destination") + 1]
            remote = source if action[1] == "from" else destination
            prefix = f"Documents/blehub-formal-identity/{self.run_id}/"
            self.assertTrue(remote.startswith(prefix))
            if action[1] == "to":
                self.assertEqual(remote, prefix + "identity-request.json")
                request = json.loads(Path(source).read_bytes())
                self.assertEqual(set(request), {"boot_id", "challenge"})
                self.assertEqual(request["boot_id"], self.boot)
                self.challenges.append(request["challenge"])
            elif remote.endswith("native-identity.json"):
                self.write(Path(destination), self.native)
            else:
                self.assertEqual(remote, prefix + "native-inspection.json")
                self.write(
                    Path(destination),
                    self.responses.pop(0)
                    if self.responses
                    else self.response
                    if self.response is not None
                    else {
                        "identity": self.native,
                        "challenge": self.challenges[-1],
                    },
                )
        else:
            self.assertEqual(
                command[command.index("--name") + 1],
                f"dev.blehub.formal-rn.{self.run_id}.{self.boot}.inspect",
            )
        receipt = self.receipt(kind, result)
        receipt["info"]["arguments"] = command[1:]
        self.write(Path(command[command.index("--json-output") + 1]), receipt)
        self.clock += 0.01
        return subprocess.CompletedProcess(command, 0, "synthetic SDK output\n")

    def pin(self, **changes: object):
        kwargs = {
            "phone": self.phone,
            "device": self.device,
            "run_id": self.run_id,
            "app_id": self.app_id,
            "expected_candidate_sha256": self.digest,
            "read_candidate_sha256": self.candidate,
            "require_candidate_provenance": self.provenance,
            "require_owner": self.owner,
            "install_receipt": self.install,
            "launch_receipt": self.launch,
            "attempt": self.attempt,
            "cwd": self.root,
            "deadline": 105.0,
        }
        kwargs.update(changes)
        return host.pin_ios_app(self.driver, **kwargs)

    def test_pin_and_callable_recheck_perform_fresh_ipc_and_remain_frozen(self) -> None:
        binding = self.pin()
        self.assertIsInstance(binding, host.IosAppBinding)
        self.assertEqual(binding.identity.boot_id, self.boot)
        self.assertEqual(binding.identity.device_identifier, self.core_device)
        self.assertEqual(len(self.commands), 6)
        binding()
        self.assertEqual(len(self.commands), 12)
        self.assertEqual(len(set(self.challenges)), 2)
        self.assertEqual(self.events.count("sdk"), 12)
        for index, event in enumerate(self.events):
            if event == "sdk":
                self.assertEqual(
                    self.events[index - 3 : index], ["phone", "protocol", "provenance"]
                )
                self.assertEqual(
                    self.events[index + 1 : index + 4],
                    ["phone", "protocol", "provenance"],
                )
        self.assertGreaterEqual(self.provenance.call_count, 24)
        with self.assertRaises(FrozenInstanceError):
            binding.identity = None
        with self.assertRaises(FrozenInstanceError):
            binding.identity.boot_id = self.run_id
        self.assertTrue(list(self.attempt.rglob("*.log")))

    def test_initial_pin_requires_current_process_and_fresh_challenge(self) -> None:
        self.processes = []
        with self.assertRaises(RuntimeError):
            self.pin()
        self.assertEqual(len(self.commands), 1)
        self.processes = [{"processIdentifier": 321, "executable": self.executable}]
        self.response = {"identity": self.native, "challenge": self.run_id}
        with self.assertRaises(TimeoutError):
            self.pin()

    def test_recheck_never_reselects_a_replacement_pid_executable_or_boot(self) -> None:
        binding = self.pin()
        for change in ("missing", "pid", "executable", "duplicate", "boot", "device"):
            with self.subTest(change=change):
                self.processes = [
                    {"processIdentifier": 321, "executable": self.executable}
                ]
                self.native["boot_id"] = self.boot
                self.process_device = self.core_device
                if change == "missing":
                    self.processes = []
                elif change == "pid":
                    self.processes[0]["processIdentifier"] = 322
                elif change == "executable":
                    self.processes[0]["executable"] = self.executable.replace(
                        "11111111", "99999999"
                    )
                elif change == "duplicate":
                    self.processes *= 2
                elif change == "boot":
                    self.native["boot_id"] = self.run_id
                else:
                    self.process_device = self.run_id
                with self.assertRaises(RuntimeError):
                    binding()

    def test_native_exact_schema_strict_json_and_size_are_required(self) -> None:
        baseline = self.native.copy()
        invalid = [
            {**baseline, "unknown": "x"},
            {key: value for key, value in baseline.items() if key != "boot_id"},
            {**baseline, "pid": True},
            {**baseline, "run_id": self.boot},
            {**baseline, "bundle_id": "other.app"},
            {**baseline, "executable": "/other"},
            {**baseline, "boot_id": self.boot.upper()},
            b"{}" + b" " * 4096,
            b"not-json",
            b"\xff",
            b'{"pid":321,"pid":321}',
        ]
        for native in invalid:
            with self.subTest(native=native):
                self.native = native
                with self.assertRaises(
                    (RuntimeError, ValueError, TypeError, UnicodeError)
                ):
                    self.pin()

    def test_inspection_rejects_foreign_duplicate_and_oversized_responses(
        self,
    ) -> None:
        for response in [
            {
                "identity": {**self.native, "boot_id": self.run_id},
                "challenge": self.run_id,
            },
            {"identity": self.native, "challenge": self.run_id, "extra": 1},
            b'{"identity":{},"challenge":"x","challenge":"x"}',
            b"{}" + b" " * 4096,
        ]:
            with self.subTest(response=response):
                self.response = response
                with self.assertRaises((RuntimeError, ValueError, TypeError)):
                    self.pin()

    def test_delayed_recheck_waits_without_reposting_and_preserves_evidence(
        self,
    ) -> None:
        binding = self.pin()
        previous_challenge = self.challenges[-1]
        previous_evidence = {
            path: path.read_bytes()
            for path in self.attempt.rglob("*")
            if path.is_file()
        }
        self.commands.clear()
        self.events.clear()
        self.responses = [
            {"identity": self.native.copy(), "challenge": previous_challenge},
            {"identity": self.native.copy(), "challenge": None},
        ]
        binding()
        self.assertEqual(len(self.commands), 8)
        self.assertEqual(len(self.sleeps), 2)
        self.assertEqual(len(self.challenges), 2)
        self.assertNotEqual(self.challenges[-1], previous_challenge)
        self.assert_single_request_and_post()
        inspections = self.inspection_commands()
        self.assertEqual(len(inspections), 3)
        destinations = [
            Path(command[command.index("--destination") + 1]) for command in inspections
        ]
        receipts = [
            Path(command[command.index("--json-output") + 1])
            for command in self.commands
        ]
        self.assertEqual(len(set(destinations)), 3)
        self.assertEqual(len(set(receipts)), 8)
        self.assertEqual(
            [json.loads(path.read_bytes())["challenge"] for path in destinations],
            [previous_challenge, None, self.challenges[-1]],
        )
        for receipt in receipts:
            self.assertTrue(receipt.is_file())
            self.assertEqual(
                receipt.with_suffix(".log").read_text(), "synthetic SDK output\n"
            )
        for path, raw in previous_evidence.items():
            self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(self.commands[-1][3:5], ["info", "processes"])
        for index, event in enumerate(self.events):
            if event == "sdk":
                self.assertEqual(
                    self.events[index - 3 : index], ["phone", "protocol", "provenance"]
                )
                self.assertEqual(
                    self.events[index + 1 : index + 4],
                    ["phone", "protocol", "provenance"],
                )

    def test_initial_sentinel_waits_for_fresh_response_and_final_process_sweep(
        self,
    ) -> None:
        self.responses = [{"identity": self.native.copy(), "challenge": None}]
        binding = self.pin()
        self.assertEqual(binding.identity.boot_id, self.boot)
        self.assertEqual(len(self.commands), 7)
        self.assertEqual(len(self.inspection_commands()), 2)
        self.assertEqual(len(self.sleeps), 1)
        self.assert_single_request_and_post()
        self.assertEqual(
            [command[3:5] for command in self.commands],
            [
                ["info", "processes"],
                ["copy", "from"],
                ["copy", "to"],
                ["notification", "post"],
                ["copy", "from"],
                ["copy", "from"],
                ["info", "processes"],
            ],
        )

    def test_never_ready_response_exhausts_shared_deadline_without_reposting(
        self,
    ) -> None:
        for challenge in (None, self.run_id):
            with self.subTest(challenge=challenge):
                self.clock = 100.0
                self.commands.clear()
                self.sleeps.clear()
                self.response = {"identity": self.native.copy(), "challenge": challenge}
                with self.assertRaisesRegex(TimeoutError, "deadline"):
                    self.pin()
                self.assertGreater(len(self.inspection_commands()), 1)
                self.assertGreater(len(self.sleeps), 1)
                self.assertGreater(self.clock, 104.0)
                self.assertLessEqual(self.clock, 105.0)
                self.assert_single_request_and_post()
                self.assertEqual(
                    sum(
                        command[3:5] == ["info", "processes"]
                        for command in self.commands
                    ),
                    1,
                )
                self.assertEqual(
                    len(self.commands), 4 + len(self.inspection_commands())
                )

    def test_pending_then_foreign_or_bad_response_fails_without_another_read(
        self,
    ) -> None:
        invalid = [
            {"identity": {**self.native, "boot_id": self.run_id}, "challenge": None},
            {"identity": {**self.native, "pid": 322}, "challenge": self.run_id},
            {"identity": {**self.native, "pid": 321.0}, "challenge": None},
            {"identity": {**self.native, "extra": 1}, "challenge": None},
            {"identity": {**self.native, "run_id": self.boot}, "challenge": None},
            {"identity": {**self.native, "bundle_id": "other.app"}, "challenge": None},
            {"identity": {**self.native, "executable": "/other"}, "challenge": None},
            {"identity": None, "challenge": None},
            {"identity": self.native.copy(), "challenge": None, "extra": 1},
            {"identity": self.native.copy()},
            *(
                {"identity": self.native.copy(), "challenge": challenge}
                for challenge in ("", "invalid", self.boot.upper(), 123, True, [], {})
            ),
            b'{"identity":{},"challenge":null,"challenge":null}',
            b"{}" + b" " * 4096,
        ]
        for response in invalid:
            with self.subTest(response=response):
                self.clock = 100.0
                self.commands.clear()
                self.sleeps.clear()
                self.responses = [
                    {"identity": self.native.copy(), "challenge": None},
                    response,
                ]
                with self.assertRaises((RuntimeError, ValueError, TypeError)):
                    self.pin()
                self.assertEqual(len(self.commands), 6)
                self.assertEqual(len(self.inspection_commands()), 2)
                self.assertEqual(len(self.sleeps), 1)
                self.assert_single_request_and_post()

    def test_owner_or_provenance_loss_during_wait_prevents_next_read(self) -> None:
        for owner in (self.owner, self.provenance, self.phone_owner_mock):
            with self.subTest(owner=owner):
                self.clock = 100.0
                self.commands.clear()
                self.sleeps.clear()
                self.response = {"identity": self.native.copy(), "challenge": None}
                previous = owner.side_effect

                def lose_owner(seconds: float, owner: Mock = owner) -> None:
                    self.sleep(seconds)
                    owner.side_effect = RuntimeError("revoked while waiting")

                self.sleep_mock.side_effect = lose_owner
                try:
                    with self.assertRaisesRegex(RuntimeError, "revoked while waiting"):
                        self.pin()
                    self.assertEqual(len(self.commands), 5)
                    self.assertEqual(len(self.inspection_commands()), 1)
                    self.assertEqual(len(self.sleeps), 1)
                    self.assert_single_request_and_post()
                finally:
                    owner.side_effect = previous
                    self.sleep_mock.side_effect = self.sleep

    def test_repeated_read_sdk_errors_fail_without_retry_or_final_sweep(self) -> None:
        for fault in ("nonzero", "timeout", "arguments", "missing-file", "other"):
            with self.subTest(fault=fault):
                self.clock = 100.0
                self.commands.clear()
                self.sleeps.clear()
                self.response = {"identity": self.native.copy(), "challenge": None}

                def command(args: list[str], *, fault: str = fault, **kwargs: object):
                    result = self.subprocess(args, **kwargs)
                    if len(self.inspection_commands()) == 2:
                        if fault == "nonzero":
                            return subprocess.CompletedProcess(args, 7, "SDK failed\n")
                        if fault == "timeout":
                            raise subprocess.TimeoutExpired(args, 1)
                        if fault == "other":
                            raise RuntimeError("unexpected SDK error")
                        if fault == "missing-file":
                            Path(args[args.index("--destination") + 1]).unlink()
                        else:
                            receipt = Path(args[args.index("--json-output") + 1])
                            document = json.loads(receipt.read_bytes())
                            document["info"]["arguments"].append("foreign")
                            self.write(receipt, document)
                    return result

                self.sdk.side_effect = command
                with self.assertRaises((RuntimeError, ValueError, FileNotFoundError)):
                    self.pin()
                self.assertEqual(len(self.commands), 6)
                self.assertEqual(len(self.inspection_commands()), 2)
                self.assertEqual(len(self.sleeps), 1)
                self.assert_single_request_and_post()
        self.sdk.side_effect = self.subprocess

    def test_pending_response_sleep_cannot_extend_deadline(self) -> None:
        self.response = {"identity": self.native.copy(), "challenge": None}

        def nearly_expire(command: list[str]) -> None:
            if len(self.inspection_commands()) == 1:
                self.clock = 104.98

        self.on_command = nearly_expire
        with self.assertRaisesRegex(TimeoutError, "deadline"):
            self.pin()
        self.assertEqual(len(self.commands), 5)
        self.assertEqual(len(self.sleeps), 1)
        self.assertAlmostEqual(self.sleeps[0], 0.01)
        self.assertAlmostEqual(self.clock, 105.0)
        self.assert_single_request_and_post()

    def test_mandatory_callbacks_and_foreign_arguments_fail_before_sdk(self) -> None:
        for changes in [
            {"require_owner": None},
            {"read_candidate_sha256": None},
            {"require_candidate_provenance": None},
            {"expected_candidate_sha256": "not-a-sha"},
            {"run_id": "../escape"},
            {"device": "different-device"},
            {"deadline": float("nan")},
            {"deadline": 100.0},
        ]:
            with (
                self.subTest(changes=changes),
                self.assertRaises((RuntimeError, TypeError, ValueError, TimeoutError)),
            ):
                self.pin(**changes)
        self.assertEqual(self.commands, [])

    def test_fixed_receipts_cannot_drift_or_describe_a_foreign_launch(self) -> None:
        binding = self.pin()
        original = self.launch.read_bytes()
        for change in (
            "device",
            "pid",
            "executable",
            "run",
            "duplicate-key",
            "whitespace",
        ):
            with self.subTest(change=change):
                value = json.loads(original)
                if change == "device":
                    value["result"]["deviceIdentifier"] = self.run_id
                elif change == "pid":
                    value["result"]["process"]["processIdentifier"] = 322
                elif change == "executable":
                    value["result"]["process"]["executable"] = self.executable.replace(
                        "11111111", "99999999"
                    )
                elif change == "run":
                    value["info"]["arguments"][-1] = self.boot
                elif change == "duplicate-key":
                    value = b'{"info":{},"info":{},"result":{}}'
                else:
                    value = original + b" "
                self.write(self.launch, value)
                before = len(self.commands)
                with self.assertRaises((RuntimeError, ValueError, TypeError)):
                    binding()
                self.assertEqual(len(self.commands), before)
        self.launch.write_bytes(original)

    def test_binding_rejects_driver_phone_candidate_and_provenance_drift(self) -> None:
        binding = self.pin()
        for callback in (self.owner, self.provenance):
            callback.side_effect = RuntimeError("ownership/provenance revoked")
            with self.assertRaises(RuntimeError):
                binding()
            callback.side_effect = lambda: None
        self.candidate.return_value = "cd" * 32
        with self.assertRaises(RuntimeError):
            binding()
        self.candidate.return_value = self.digest
        self.driver.session_id = "new-session"
        with self.assertRaises(RuntimeError):
            binding()
        self.driver.session_id = "session-1"
        self.driver.capabilities["bundleId"] = "other.app"
        with self.assertRaises(RuntimeError):
            binding()
        self.driver.capabilities.pop("bundleId")
        self.phone.document["run_id"] = self.boot
        with self.assertRaises(RuntimeError):
            binding()
        self.phone.document["run_id"] = self.run_id
        with self.assertRaises(RuntimeError):
            binding.require_current(
                driver=SimpleNamespace(),
                phone=self.phone,
                device=self.device,
                run_id=self.run_id,
                app_id=self.app_id,
                expected_candidate_sha256=self.digest,
            )

    def test_before_after_guards_stop_at_every_sdk_boundary(self) -> None:
        for step in range(1, 7):
            with self.subTest(step=step):
                self.commands.clear()
                self.driver.session_id = "session-1"

                def mutate(command: list[str], step: int = step) -> None:
                    if len(self.commands) == step:
                        self.driver.session_id = "changed-in-sdk"

                self.on_command = mutate
                with self.assertRaises(RuntimeError):
                    self.pin()
                self.assertEqual(len(self.commands), step)
        self.on_command = None

    def test_shared_deadline_and_sdk_error_do_not_cleanup_or_retry(self) -> None:
        def expire(command: list[str]) -> None:
            self.clock = 105.0

        self.on_command = expire
        with self.assertRaises(TimeoutError):
            self.pin()
        self.assertEqual(len(self.commands), 1)
        self.clock = 100.0
        self.on_command = None
        self.sdk.side_effect = subprocess.TimeoutExpired("devicectl", 1)
        self.events.clear()
        with self.assertRaises(ios_sdk.IosSdkError):
            self.pin()
        self.assertGreaterEqual(self.events.count("phone"), 2)
        self.assertEqual(self.events.count("protocol"), self.events.count("phone"))
        self.assertEqual(self.events.count("provenance"), self.events.count("phone"))

    def test_process_is_rechecked_after_ipc_and_challenges_cannot_repeat(self) -> None:
        def remove_after_response(command: list[str]) -> None:
            if "--source" in command and command[
                command.index("--source") + 1
            ].endswith("native-inspection.json"):
                self.processes = []

        self.on_command = remove_after_response
        with self.assertRaises(RuntimeError):
            self.pin()
        self.assertEqual(len(self.commands), 6)
        self.on_command = None
        self.processes = [{"processIdentifier": 321, "executable": self.executable}]
        with patch.object(
            ios_sdk.uuid, "uuid4", return_value=ios_sdk.uuid.UUID(self.run_id)
        ):
            binding = self.pin()
            with self.assertRaises((RuntimeError, FileExistsError)):
                binding()

    def test_nonzero_and_foreign_sdk_receipts_stop_at_each_boundary(self) -> None:
        for step in range(1, 7):
            for fault in ("nonzero", "device", "arguments", "duplicate"):
                with self.subTest(step=step, fault=fault):
                    self.commands.clear()
                    self.clock = 100.0

                    def command(
                        args: list[str],
                        *,
                        step: int = step,
                        fault: str = fault,
                        **kwargs: object,
                    ):
                        result = self.subprocess(args, **kwargs)
                        if len(self.commands) == step:
                            if fault == "nonzero":
                                return subprocess.CompletedProcess(
                                    args, 7, "complete failure output"
                                )
                            receipt = Path(args[args.index("--json-output") + 1])
                            document = json.loads(receipt.read_bytes())
                            if fault == "device":
                                document["result"]["deviceIdentifier"] = self.run_id
                            elif fault == "arguments":
                                document["info"]["arguments"] = [
                                    *document["info"]["arguments"],
                                    "foreign",
                                ]
                            else:
                                document = b'{"info":{},"info":{},"result":{}}'
                            self.write(receipt, document)
                        return result

                    self.sdk.side_effect = command
                    with self.assertRaises((RuntimeError, ValueError)):
                        self.pin()
                    self.assertEqual(len(self.commands), step)
        self.sdk.side_effect = self.subprocess

    def test_receipt_candidate_and_phone_changes_inside_sdk_fail_before_next_command(
        self,
    ) -> None:
        original = self.install.read_bytes()
        for fault in ("receipt", "candidate", "phone", "provenance"):
            with self.subTest(fault=fault):
                self.commands.clear()
                self.events.clear()
                self.clock = 100.0
                self.install.write_bytes(original)
                self.candidate.return_value = self.digest
                self.phone.device = self.device
                self.provenance.side_effect = lambda: self.events.append("provenance")

                def mutate(command: list[str], fault: str = fault) -> None:
                    if fault == "receipt":
                        self.install.write_bytes(original + b" ")
                    elif fault == "candidate":
                        self.candidate.return_value = "cd" * 32
                    elif fault == "phone":
                        self.phone.device = "other"
                    else:
                        self.provenance.side_effect = RuntimeError("provenance revoked")

                self.on_command = mutate
                with self.assertRaises(RuntimeError):
                    self.pin()
                self.assertEqual(len(self.commands), 1)
        self.on_command = None

    def test_install_and_launch_schema_are_checked_before_initial_sdk(self) -> None:
        original_install, original_launch = (
            self.install.read_bytes(),
            self.launch.read_bytes(),
        )
        for fault in (
            "command",
            "device",
            "installation",
            "duplicate-install",
            "bool-pid",
            "missing-run",
            "duplicate-json",
            "oversized",
        ):
            with self.subTest(fault=fault):
                self.install.write_bytes(original_install)
                self.launch.write_bytes(original_launch)
                installed, launched = (
                    json.loads(original_install),
                    json.loads(original_launch),
                )
                if fault == "command":
                    installed["info"]["commandType"] = "devicectl.device.info.apps"
                elif fault == "device":
                    launched["info"]["arguments"][5] = "other-device"
                elif fault == "installation":
                    installed["result"]["installedApplications"][0][
                        "installationURL"
                    ] = "file:///other.app/"
                elif fault == "duplicate-install":
                    installed["result"]["installedApplications"] *= 2
                elif fault == "bool-pid":
                    launched["result"]["process"]["processIdentifier"] = True
                elif fault == "missing-run":
                    launched["info"]["arguments"] = launched["info"]["arguments"][:-2]
                elif fault == "duplicate-json":
                    installed = b'{"info":{},"info":{},"result":{}}'
                else:
                    installed = original_install + b" " * (1024 * 1024)
                self.write(self.install, installed)
                self.write(self.launch, launched)
                with self.assertRaises((RuntimeError, ValueError, TypeError)):
                    self.pin()
                self.assertEqual(self.commands, [])

    def test_response_nested_schema_is_exact_even_for_python_bool_integer_equality(
        self,
    ) -> None:
        self.processes[0]["processIdentifier"] = 1
        self.native["pid"] = 1
        launch = json.loads(self.launch.read_bytes())
        launch["result"]["process"]["processIdentifier"] = 1
        self.write(self.launch, launch)

        def spoof_response(command: list[str]) -> None:
            if "--source" in command and command[
                command.index("--source") + 1
            ].endswith("native-inspection.json"):
                self.response = {
                    "identity": {**self.native, "pid": True},
                    "challenge": self.challenges[-1],
                }

        self.on_command = spoof_response
        with self.assertRaises(RuntimeError):
            self.pin()

    def test_linked_receipts_and_invalid_attempt_are_not_adopted(self) -> None:
        other = self.root / "other.json"
        other.write_bytes(self.install.read_bytes())
        self.install.unlink()
        self.install.symlink_to(other)
        with self.assertRaises(RuntimeError):
            self.pin()
        self.install.unlink()
        self.install.hardlink_to(other)
        with self.assertRaises(RuntimeError):
            self.pin()
        self.install.unlink()
        self.install.write_bytes(other.read_bytes())
        linked = self.root / "linked-attempt"
        linked.symlink_to(self.attempt, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            self.pin(attempt=linked)
        self.assertEqual(self.commands, [])

    def test_frozen_identity_rejects_caller_rebinding(self) -> None:
        binding = self.pin()
        for changes in (
            {"device": self.boot},
            {"run_id": self.boot},
            {"app_id": "other"},
            {"expected_candidate_sha256": "cd" * 32},
        ):
            with self.assertRaises(RuntimeError):
                binding.require_current(
                    driver=self.driver,
                    phone=self.phone,
                    device=changes.get("device", self.device),
                    run_id=changes.get("run_id", self.run_id),
                    app_id=changes.get("app_id", self.app_id),
                    expected_candidate_sha256=changes.get(
                        "expected_candidate_sha256", self.digest
                    ),
                )


if __name__ == "__main__":
    unittest.main()
