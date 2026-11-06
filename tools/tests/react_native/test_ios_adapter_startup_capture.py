"""Hardware-free audits of the real startup window; no device or RF evidence."""

from __future__ import annotations

import json
import os
import plistlib
import signal
import subprocess
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest import mock

from tools.react_native import capture_io, capture_transport, ios_device
from tools.react_native import ios_adapter_startup_capture as capture
from tools.react_native.host_contract import BUNDLE_ID
from tools.react_native.rf_evidence_bundle import EvidenceError
from tools.tests.react_native.support.ios_receipts import (
    DEVICE_IDENTIFIER,
    EXECUTABLE,
    ios_process_document,
    write_ios_install,
)

RUN = "12345678-1234-4234-8234-123456789abc"
SERVICE = "11111111-1111-4111-8111-111111111111"
FOREIGN_RUN = "22222222-2222-4222-8222-222222222222"
DEVICE = "00008110-001234567890001E"
RECEIPT = "a" * 64
NAME = "实验室:Peripheral"


def window() -> dict:
    """Synthetic UI attachment, tied to the shared official install fixture."""
    return {
        "schema_version": 1,
        "run_id": RUN,
        "fixture_receipt_sha256": RECEIPT,
        "identity": {
            "run_id": RUN,
            "nonce": "boot-123-abc",
            "native": {
                "run_id": RUN,
                "adapter_mode": "startup-off",
                "pid": 42,
                "boot_id": FOREIGN_RUN,
                "bundle_id": BUNDLE_ID,
                "executable": EXECUTABLE,
                "source": "launch",
            },
        },
    }


def marker(value: dict | None = None) -> bytes:
    return (
        capture.WINDOW_PREFIX
        + json.dumps(window() if value is None else value, ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def flat_test_run() -> dict:
    """Flat build-for-testing input with paths in all recursively staged shapes."""
    return {
        capture.TEST_TARGET: {
            "IsUITestBundle": True,
            "TestBundlePath": "__TESTROOT__/Runner.xctest",
            "TestHostPath": "__TESTROOT__/Runner.app/Runner",
            "DependentProductPaths": [
                "__TESTROOT__/Dependency.framework",
                "__TESTROOT__/NotTheFrozenApp.app",
                "__TESTROOT__/NotTheFrozenApp.app/Frameworks/Embedded.framework",
                "__TESTROOT__/NotTheFrozenApp.app/PlugIns/Embedded.xctest",
                "__TESTROOT__/NotTheFrozenApp.app.other/Unrelated.framework",
            ],
            "Nested": {"paths": ["__TESTROOT__/nested", 7, True]},
            "EnvironmentVariables": {"DYLD_FRAMEWORK_PATH": "__TESTROOT__/Frameworks"},
            "TestingEnvironmentVariables": {"SAFE_TEST": "preserved"},
            "UITargetAppEnvironmentVariables": {"SAFE_APP": "preserved"},
            "UITargetAppPath": "__TESTROOT__/NotTheFrozenApp.app",
            "OnlyTestIdentifiers": ["OldClass/oldMethod"],
            "SkipTestIdentifiers": [capture.TEST_METHOD],
        },
        "BleHubNitroHostNativeTests": {"IsUITestBundle": False},
        "__xctestrun_metadata__": {"FormatVersion": 1},
    }


class Clock:
    """Only elapsed host time advances; no real sleeps or commands."""

    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Child:
    """Local SDK child contract, not an alternative capture implementation."""

    def __init__(self, sdk: SDK, command: list[str], *, runner: bool) -> None:
        self.sdk, self.command, self.runner = sdk, command, runner
        self.pid = 8000 + len(sdk.children)
        self.returncode: int | None = None if runner else 0
        self.waits: list[float] = []
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        if self.runner and self.returncode is None:
            if self.sdk.fault in {"exited-before-ready", "exited-without-window"}:
                self.returncode = 0
            elif (
                self.sdk.relay_at is not None
                and self.sdk.clock.now >= self.sdk.relay_at + 0.2
            ):
                self.returncode = 65 if self.sdk.fault == "failed-runner" else 0
        return self.returncode

    def wait(self, timeout: float) -> int:
        self.waits.append(timeout)
        if self.poll() is None:
            raise subprocess.TimeoutExpired(self.command, timeout)
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        if self.runner:
            raise AssertionError("must not forward SIGTERM to the UI/App runner")
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class SDK:
    """Fake only OS/SDK boundaries; real staging, stream, relay and receipts run."""

    def __init__(self, root: Path, fault: str = "") -> None:
        self.root, self.fault = root, fault
        self.clock = Clock()
        self.children: list[Child] = []
        self.events: list[str] = []
        self.commands: list[tuple[list[str], dict]] = []
        self.signals: list[tuple[int, int]] = []
        self.relay_at: float | None = None
        self.raw = (
            b"XCTest initialization output\n" + marker() + b"unrelated runner output\n"
        )
        self.source = root / "build-products.xctestrun"
        self.source.write_bytes(plistlib.dumps(flat_test_run()))
        self.app = root / "frozen" / "BleHubNitroHost.app"
        self.app.mkdir(parents=True)
        (self.app / "candidate").write_bytes(b"frozen candidate bytes")
        self.install = root / "install.json"
        write_ios_install(self.install)
        installed = json.loads(self.install.read_text())
        installed["info"]["arguments"][-1] = DEVICE
        self.install.write_text(json.dumps(installed))
        self.log = root / "runtime.log"
        self.owner: capture.StartupWindowCapture | None = None
        self.guard_error = RuntimeError("fixture lease lost")
        self.ready_error = RuntimeError("ready guard refused authorization")
        self.journal = {
            "schema_version": 1,
            "run_id": RUN,
            "records": [],
            "error": None,
        }

    def guard(self) -> None:
        self.events.append("guard")
        if self.fault == "guard-failure" and self.children:
            raise self.guard_error

    def ready_guard(self) -> None:
        self.events.append("ready-guard")
        if self.fault == "ready-guard-failure":
            raise self.ready_error
        if self.fault == "exited-in-ready-guard":
            self.children[0].returncode = 0

    def make(self, **overrides) -> capture.StartupWindowCapture:
        arguments = {
            "install_json": self.install,
            "test_run": self.source,
            "app": self.app,
            "timeout": 3,
            "guard": self.guard,
            "ready_guard": self.ready_guard,
        }
        arguments.update(overrides)
        self.owner = capture.StartupWindowCapture(
            DEVICE, self.log, RUN, SERVICE, NAME, RECEIPT, **arguments
        )
        return self.owner

    def popen(self, command: list[str], **kwargs) -> Child:
        self.commands.append((list(command), kwargs))
        runner = command[:2] == ["xcrun", "xcodebuild"]
        child = Child(self, command, runner=runner)
        self.children.append(child)
        if runner:
            self.events.append("runner")
            kwargs["stdout"].write(
                b"runner stalled\n"
                if self.fault in {"timeout", "exited-without-window"}
                else self.raw
            )
            kwargs["stdout"].flush()
            return child
        path = Path(command[command.index("--json-output") + 1])
        if command[2:5] == ["device", "info", "processes"]:
            self.events.append("pid")
            document = ios_process_document()
            if self.fault == "foreign-process":
                document["result"]["runningProcesses"][0]["executable"] += "-foreign"
            if self.fault == "hung-sdk":
                child.returncode = None
        elif command[2:5] == ["device", "process", "launch"]:
            self.events.append("relay")
            self.relay_at = self.clock.now
            document = {
                "info": {
                    "outcome": "success",
                    "commandType": "devicectl.device.process.launch",
                },
                "result": {
                    "deviceIdentifier": DEVICE_IDENTIFIER,
                    "process": {"processIdentifier": 42, "executable": EXECUTABLE},
                },
            }
            if self.fault == "foreign-payload":
                document["result"]["process"]["processIdentifier"] = 43
        else:
            raise AssertionError(f"unexpected SDK command: {command}")
        document["info"]["arguments"] = command[1:]
        path.write_text(json.dumps(document))
        return child

    def killpg(self, pid: int, sig: int) -> None:
        self.signals.append((pid, sig))
        child = next(child for child in self.children if child.pid == pid)
        child.kill()
        if self.fault == "cleanup-failure":
            raise OSError("owned group cleanup failed")

    def copy_journal(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.commands.append((list(command), kwargs))
        if command[:5] != ["xcrun", "devicectl", "device", "copy", "from"]:
            raise AssertionError(f"unexpected evidence command: {command}")
        self.events.append("copy")
        Path(command[command.index("--destination") + 1]).write_text(
            json.dumps(self.journal)
        )
        receipt = {
            "info": {
                "outcome": "success",
                "commandType": "devicectl.device.copy.from",
                "arguments": command[1:],
            },
            "result": {"deviceIdentifier": DEVICE_IDENTIFIER},
        }
        if self.fault == "foreign-copy-device":
            receipt["result"]["deviceIdentifier"] = "foreign"
        if self.fault == "foreign-copy-selector":
            receipt["info"]["arguments"] = [*command[1:]]
            receipt["info"]["arguments"][command[1:].index("--device") + 1] = "foreign"
        Path(command[command.index("--json-output") + 1]).write_text(
            json.dumps(receipt)
        )
        return subprocess.CompletedProcess(command, 0)

    @contextmanager
    def patched(self):
        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(capture.time, "monotonic", self.clock.monotonic)
            )
            stack.enter_context(
                mock.patch.object(capture.time, "sleep", self.clock.sleep)
            )
            stack.enter_context(
                mock.patch.object(capture.subprocess, "Popen", side_effect=self.popen)
            )
            stack.enter_context(
                mock.patch.object(
                    capture.subprocess, "run", side_effect=self.copy_journal
                )
            )
            stack.enter_context(
                mock.patch.object(capture_io.os, "killpg", side_effect=self.killpg)
            )
            ready = stack.enter_context(
                mock.patch.object(capture, "require_ready_unlocked_device")
            )
            yield ready


@contextmanager
def failing_raw_close(path: Path, error: OSError):
    """Inject a close error only at the runner file boundary, closing the real FD."""
    original_open = Path.open
    streams = []

    def open_file(candidate, *args, **kwargs):
        stream = original_open(candidate, *args, **kwargs)
        if candidate != path or args[:1] != ("xb",):
            return stream
        streams.append(stream)
        proxy = mock.Mock(wraps=stream)

        def close() -> None:
            stream.close()
            raise error

        proxy.close.side_effect = close
        return proxy

    with mock.patch.object(Path, "open", open_file):
        yield streams


class PrepareStartupTestRunTests(unittest.TestCase):
    def test_flat_staging_selects_only_ui_method_and_preserves_frozen_inputs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sdk = SDK(Path(directory))
            original = sdk.source.read_bytes()
            staged = sdk.root / "staged.xctestrun"
            with sdk.patched():
                capture.prepare_test_run(
                    Path(os.path.relpath(sdk.source)),
                    staged,
                    sdk.app,
                    run_id=RUN,
                    service_uuid=SERVICE,
                    local_name=NAME,
                    receipt_sha256=RECEIPT,
                )
            document = plistlib.loads(staged.read_bytes())
            self.assertEqual(
                set(document), {capture.TEST_TARGET, "__xctestrun_metadata__"}
            )
            target = document[capture.TEST_TARGET]
            self.assertEqual(target["OnlyTestIdentifiers"], [capture.TEST_METHOD])
            self.assertEqual(target["SkipTestIdentifiers"], [])
            self.assertEqual(target["UITargetAppPath"], str(sdk.app.resolve()))
            self.assertEqual(target["UITargetAppBundleIdentifier"], BUNDLE_ID)
            self.assertEqual(
                target["EnvironmentVariables"],
                {
                    "DYLD_FRAMEWORK_PATH": str(sdk.root / "Frameworks"),
                    "BLEHUB_IOS_ADAPTER_STARTUP_GATE": "1",
                    "BLEHUB_IOS_ADAPTER_RUN_ID": RUN,
                    "BLEHUB_IOS_ADAPTER_SERVICE_UUID": SERVICE,
                    "BLEHUB_IOS_ADAPTER_LOCAL_NAME": NAME,
                    "BLEHUB_IOS_ADAPTER_FIXTURE_RECEIPT_SHA256": RECEIPT,
                },
            )
            self.assertEqual(
                target["TestingEnvironmentVariables"], {"SAFE_TEST": "preserved"}
            )
            self.assertEqual(
                target["UITargetAppEnvironmentVariables"], {"SAFE_APP": "preserved"}
            )
            self.assertEqual(target["TestBundlePath"], str(sdk.root / "Runner.xctest"))
            self.assertEqual(
                target["TestHostPath"], str(sdk.root / "Runner.app/Runner")
            )
            self.assertEqual(
                target["DependentProductPaths"],
                [
                    str(sdk.root / "Dependency.framework"),
                    str(sdk.app.resolve()),
                    str(sdk.app.resolve() / "Frameworks/Embedded.framework"),
                    str(sdk.app.resolve() / "PlugIns/Embedded.xctest"),
                    str(sdk.root / "NotTheFrozenApp.app.other/Unrelated.framework"),
                ],
            )
            self.assertEqual(
                target["Nested"], {"paths": [str(sdk.root / "nested"), 7, True]}
            )
            for key in ("UserAttachmentLifetime", "SystemAttachmentLifetime"):
                self.assertEqual(target[key], "keepAlways")
            self.assertNotIn(b"__TESTROOT__", staged.read_bytes())
            self.assertEqual(sdk.source.read_bytes(), original)
            self.assertEqual(
                (sdk.app / "candidate").read_bytes(), b"frozen candidate bytes"
            )
            self.assertEqual(sdk.commands, [])

    def test_missing_ambiguous_or_malformed_original_app_dependency_is_rejected(
        self,
    ) -> None:
        for dependencies in (
            [],
            ["__TESTROOT__/Other.app"],
            ["__TESTROOT__/NotTheFrozenApp.app/PlugIns/Embedded.xctest"],
            ["__TESTROOT__/NotTheFrozenApp.app"] * 2,
            ["__TESTROOT__/NotTheFrozenApp.app", 7],
            "__TESTROOT__/NotTheFrozenApp.app",
        ):
            with (
                self.subTest(dependencies=dependencies),
                tempfile.TemporaryDirectory() as directory,
            ):
                sdk = SDK(Path(directory))
                document = flat_test_run()
                document[capture.TEST_TARGET]["DependentProductPaths"] = dependencies
                sdk.source.write_bytes(plistlib.dumps(document))
                with (
                    sdk.patched(),
                    self.assertRaisesRegex(EvidenceError, "original App dependency"),
                ):
                    capture.prepare_test_run(
                        sdk.source,
                        sdk.root / "staged.xctestrun",
                        sdk.app,
                        run_id=RUN,
                        service_uuid=SERVICE,
                        local_name=NAME,
                        receipt_sha256=RECEIPT,
                    )
                self.assertFalse((sdk.root / "staged.xctestrun").exists())
                self.assertEqual(sdk.commands, [])

    def test_existing_files_directories_and_dangling_symlinks_are_never_overwritten(
        self,
    ) -> None:
        for kind in ("file", "directory", "symlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                sdk = SDK(Path(directory))
                staged = sdk.root / "staged.xctestrun"
                if kind == "file":
                    staged.write_bytes(b"existing evidence")
                elif kind == "directory":
                    staged.mkdir()
                else:
                    staged.symlink_to(sdk.root / "absent")
                with (
                    sdk.patched(),
                    self.assertRaisesRegex(EvidenceError, "existing staged"),
                ):
                    capture.prepare_test_run(
                        sdk.source,
                        staged,
                        sdk.app,
                        run_id=RUN,
                        service_uuid=SERVICE,
                        local_name=NAME,
                        receipt_sha256=RECEIPT,
                    )
                if kind == "file":
                    self.assertEqual(staged.read_bytes(), b"existing evidence")
                elif kind == "directory":
                    self.assertTrue(staged.is_dir())
                else:
                    self.assertTrue(staged.is_symlink())
                self.assertEqual(sdk.commands, [])

    def test_conflicting_optins_launch_arguments_and_unsupported_formats_fail_closed(
        self,
    ) -> None:
        cases: list[dict | bytes] = [
            b"not a plist",
            [],
            {"TestConfigurations": []},
            {"UnexpectedTarget": {}},
            {capture.TEST_TARGET: {"IsUITestBundle": False}},
        ]
        for key in (
            "EnvironmentVariables",
            "TestingEnvironmentVariables",
            "UITargetAppEnvironmentVariables",
        ):
            for value in ({"BLEHUB_OTHER_GATE": "0"}, {"BAD": 1}, ["BAD"]):
                document = flat_test_run()
                document[capture.TEST_TARGET][key] = value
                cases.append(document)
        for key in ("CommandLineArguments", "UITargetAppCommandLineArguments"):
            document = flat_test_run()
            document[capture.TEST_TARGET][key] = ["--some-other-gate"]
            cases.append(document)
        for index, document in enumerate(cases):
            with self.subTest(case=index), tempfile.TemporaryDirectory() as directory:
                sdk = SDK(Path(directory))
                sdk.source.write_bytes(
                    document
                    if isinstance(document, bytes)
                    else plistlib.dumps(document)
                )
                with (
                    sdk.patched(),
                    self.assertRaises((EvidenceError, plistlib.InvalidFileException)),
                ):
                    capture.prepare_test_run(
                        sdk.source,
                        sdk.root / "staged.xctestrun",
                        sdk.app,
                        run_id=RUN,
                        service_uuid=SERVICE,
                        local_name=NAME,
                        receipt_sha256=RECEIPT,
                    )
                self.assertFalse((sdk.root / "staged.xctestrun").exists())
                self.assertEqual(sdk.commands, [])

    def test_environment_validation_and_frozen_app_are_checked_before_staging(
        self,
    ) -> None:
        bad_values = [
            ("run_id", RUN.upper()),
            ("service_uuid", "not-a-uuid"),
            ("receipt_sha256", RECEIPT.upper()),
            ("receipt_sha256", "a" * 63),
            ("local_name", "--Peripheral"),
            ("local_name", " leading"),
            ("local_name", "line\nbreak"),
            ("local_name", "设" * 43),
            ("local_name", ""),
        ]
        for key, value in bad_values:
            with (
                self.subTest(key=key, value=value),
                tempfile.TemporaryDirectory() as directory,
            ):
                sdk = SDK(Path(directory))
                arguments = {
                    "run_id": RUN,
                    "service_uuid": SERVICE,
                    "local_name": NAME,
                    "receipt_sha256": RECEIPT,
                }
                arguments[key] = value
                with sdk.patched(), self.assertRaises(EvidenceError):
                    capture.prepare_test_run(
                        sdk.source, sdk.root / "staged.xctestrun", sdk.app, **arguments
                    )
                self.assertFalse((sdk.root / "staged.xctestrun").exists())
        with tempfile.TemporaryDirectory() as directory:
            sdk = SDK(Path(directory))
            wrong_app = sdk.root / "Other.app"
            wrong_app.mkdir()
            with (
                sdk.patched(),
                self.assertRaisesRegex(EvidenceError, "frozen host App"),
            ):
                capture.prepare_test_run(
                    sdk.source,
                    sdk.root / "staged.xctestrun",
                    wrong_app,
                    run_id=RUN,
                    service_uuid=SERVICE,
                    local_name=NAME,
                    receipt_sha256=RECEIPT,
                )


class StartupWindowCaptureTests(unittest.TestCase):
    def test_ios_container_uuid_reinstallation_is_allowed_but_app_identity_is_not(
        self,
    ) -> None:
        expected = "file:///private/var/containers/Bundle/Application/11111111-1111-4111-8111-111111111111/BleHubNitroHost.app/BleHubNitroHost"
        reinstalled = expected.replace(
            "11111111-1111-4111-8111-111111111111",
            "22222222-2222-4222-8222-222222222222",
        )
        self.assertTrue(capture_transport.ios_executables_match(expected, reinstalled))
        self.assertTrue(capture_transport.ios_executables_match(expected, expected))
        for foreign in (
            reinstalled.replace(
                "BleHubNitroHost.app/BleHubNitroHost", "Other.app/Other"
            ),
            reinstalled.replace(
                "BleHubNitroHost.app/BleHubNitroHost", "BleHubNitroHost.app/Other"
            ),
            "file:///private/var/containers/Bundle/Application/not-a-uuid/BleHubNitroHost.app/BleHubNitroHost",
        ):
            with self.subTest(foreign=foreign):
                self.assertFalse(
                    capture_transport.ios_executables_match(expected, foreign)
                )

    def test_live_off_snapshot_is_exact_run_once_and_never_authorizes_itself(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sdk = SDK(Path(directory))
            with sdk.patched():
                owner = sdk.make()
                with self.assertRaises(EvidenceError):
                    owner.snapshot_off_journal()
                owner.run_called = True
                owner.window_identity = window()["identity"]
                owner.process = mock.Mock()
                owner.process.poll.return_value = None

                def copy(command, *, budget):
                    self.assertEqual(budget, 3.0)
                    sdk.copy_journal(command)
                    return ""

                with mock.patch.object(owner, "_command", side_effect=copy) as command:
                    path = owner.snapshot_off_journal()
                    self.assertEqual(path, sdk.log.with_suffix(".off-journal.json"))
                    self.assertFalse(owner.delivered)
                    self.assertIn("copy", sdk.events)
                    self.assertNotIn("relay", sdk.events)
                    args = command.call_args.args[0]
                    self.assertIn(f"Library/Caches/blehub-ios-adapter/{RUN}.json", args)
                    self.assertEqual(
                        args[args.index("--json-output") + 1],
                        str(sdk.log.with_suffix(".off-journal-copy.json")),
                    )
                    with self.assertRaises(EvidenceError):
                        owner.snapshot_off_journal()
                    owner.delivered = True
                    with self.assertRaises(EvidenceError):
                        owner.snapshot_off_journal()
                    self.assertEqual(command.call_count, 1)

    def test_real_prefix_stream_preserves_fragmented_utf8_and_raw_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sdk = SDK(Path(directory))
            value = window()
            value["identity"]["native"]["executable"] = EXECUTABLE.replace(
                "/install/", "/安装/"
            )
            installed = json.loads(sdk.install.read_text())
            installed["result"]["installedApplications"][0]["installationURL"] = (
                value["identity"]["native"]["executable"].rsplit("/", 1)[0] + "/"
            )
            sdk.install.write_text(json.dumps(installed))
            raw = b"XCTest noise\n2026 runner: " + marker(value) + b"trailing noise\n"
            split = raw.index("安装".encode()) + 1
            with sdk.patched():
                owner = sdk.make()
                path = sdk.log.with_suffix(".raw.log")
                path.write_bytes(raw[:split])
                owner.stream = capture_io.PrefixStream(
                    path, None, prefix=capture.WINDOW_PREFIX
                )
                owner._poll()
                self.assertIsNone(owner.window)
                self.assertEqual(owner.lines, [])
                with path.open("ab") as output:
                    output.write(raw[split:-1])
                owner._poll()
                self.assertEqual(owner.window, value)
                self.assertEqual(owner.pid, 42)
                self.assertEqual(owner.window_identity, value["identity"])
                with path.open("ab") as output:
                    output.write(b"\n")
                owner._poll()
                self.assertEqual(owner.lines, [marker(value).decode().rstrip("\n")])
                self.assertEqual(sdk.log.read_bytes(), marker(value))
                self.assertEqual(path.read_bytes(), raw)
                self.assertEqual(
                    json.loads(sdk.log.with_suffix(".capture.json").read_text())[
                        "window"
                    ],
                    value,
                )

    def test_duplicate_foreign_run_receipt_executable_and_source_keep_rejected_batch(
        self,
    ) -> None:
        cases = {"duplicate": window()}
        for label, path, value in (
            ("run", ("run_id",), FOREIGN_RUN),
            ("receipt", ("fixture_receipt_sha256",), "b" * 64),
            ("identity-run", ("identity", "run_id"), FOREIGN_RUN),
            ("native-run", ("identity", "native", "run_id"), FOREIGN_RUN),
            ("executable", ("identity", "native", "executable"), EXECUTABLE + "wrong"),
            ("source", ("identity", "native", "source"), "console"),
        ):
            document = window()
            target = document
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            cases[label] = document
        for label, document in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                sdk = SDK(Path(directory))
                rejected = (
                    marker() + marker()
                    if label == "duplicate"
                    else marker(document) + marker()
                )
                raw = b"before\n" + rejected + b"after\n"
                with sdk.patched():
                    owner = sdk.make()
                    path = sdk.log.with_suffix(".raw.log")
                    path.write_bytes(raw)
                    owner.stream = capture_io.PrefixStream(
                        path, None, prefix=capture.WINDOW_PREFIX
                    )
                    with self.assertRaises(EvidenceError):
                        owner._poll()
                    self.assertEqual(sdk.log.read_bytes(), rejected)
                    self.assertEqual(path.read_bytes(), raw)
                    self.assertFalse(owner.delivered)
                    self.assertFalse(
                        json.loads(sdk.log.with_suffix(".capture.json").read_text())[
                            "ready_delivered"
                        ]
                    )

    def test_official_install_receipt_required_and_staged_candidate_cannot_change(
        self,
    ) -> None:
        for fault in (
            "wrong-selector",
            "wrong-type",
            "ambiguous-install",
            "stage-changed",
        ):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                sdk = SDK(Path(directory))
                installed = json.loads(sdk.install.read_text())
                if fault == "wrong-selector":
                    installed["info"]["arguments"][-1] = "foreign"
                elif fault == "wrong-type":
                    installed["info"]["commandType"] = "devicectl.device.process.launch"
                elif fault == "ambiguous-install":
                    installed["result"]["installedApplications"] *= 2
                sdk.install.write_text(json.dumps(installed))
                with sdk.patched():
                    if fault == "stage-changed":
                        owner = sdk.make()
                        owner.staged.write_bytes(owner.staged.read_bytes() + b"changed")
                        with self.assertRaisesRegex(EvidenceError, "test run changed"):
                            owner.run()
                    else:
                        with self.assertRaises(RuntimeError):
                            sdk.make()
                self.assertEqual(sdk.commands, [])

    def test_constructor_rejects_stale_evidence_and_invalid_budgets_without_sdk_calls(
        self,
    ) -> None:
        for suffix in (".log", ".raw.log", ".capture.json", ".xctestrun", ".xcresult"):
            for symlink in (False, True):
                with (
                    self.subTest(suffix=suffix, symlink=symlink),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    sdk = SDK(Path(directory))
                    path = sdk.log.with_suffix(suffix)
                    if symlink:
                        path.symlink_to(sdk.root / "missing-evidence")
                    else:
                        path.write_bytes(b"previous attempt")
                    with (
                        sdk.patched(),
                        self.assertRaisesRegex(
                            EvidenceError, "existing startup-off evidence"
                        ),
                    ):
                        sdk.make()
                    self.assertEqual(sdk.commands, [])
                    if symlink:
                        self.assertTrue(path.is_symlink())
                    else:
                        self.assertEqual(path.read_bytes(), b"previous attempt")
        for timeout in (True, 0, -1, 601, float("inf"), float("nan")):
            with (
                self.subTest(timeout=timeout),
                tempfile.TemporaryDirectory() as directory,
            ):
                sdk = SDK(Path(directory))
                with sdk.patched(), self.assertRaisesRegex(EvidenceError, "timeout"):
                    sdk.make(timeout=timeout)
                self.assertEqual(sdk.commands, [])
                self.assertFalse(sdk.log.with_suffix(".xctestrun").exists())

    def test_run_once_guard_pid_ready_guard_relay_order_and_noninteractive_command(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sdk = SDK(Path(directory))
            with sdk.patched() as ready:
                owner = sdk.make()
                result = owner.run()
                with self.assertRaisesRegex(EvidenceError, "cannot be rerun"):
                    owner.run()
            ready.assert_called_once_with(
                DEVICE,
                cwd=Path(capture.__file__).resolve().parents[2],
                timeout=owner.deadline - owner.started,
            )
            self.assertEqual(result["status"], "window-completed-awaiting-evidence")
            self.assertTrue(result["ready_delivered"])
            self.assertEqual(result["cleanup_errors"], [])
            self.assertEqual(result["rf_verdict"], "parent checker required")
            self.assertEqual(sdk.log.with_suffix(".raw.log").read_bytes(), sdk.raw)
            self.assertEqual(sdk.log.read_bytes(), marker())
            events = sdk.events
            before, authorization, relay = (
                events.index("pid"),
                events.index("ready-guard"),
                events.index("relay"),
            )
            self.assertLess(before, authorization)
            self.assertLess(authorization, relay)
            self.assertIn("guard", events[events.index("runner") + 1 : before])
            self.assertIn("guard", events[authorization + 1 : relay])
            runner_commands = [
                command for command, _ in sdk.commands if "xcodebuild" in command
            ]
            self.assertEqual(len(runner_commands), 1)
            command = runner_commands[0]
            self.assertNotIn("-quiet", command)
            self.assertEqual(
                command,
                [
                    "xcrun",
                    "xcodebuild",
                    "-xctestrun",
                    str(owner.staged),
                    "-destination",
                    f"id={DEVICE}",
                    "-resultBundlePath",
                    str(owner.result),
                    "-parallel-testing-enabled",
                    "NO",
                    "-collect-test-diagnostics",
                    "never",
                    "-only-testing:" + capture.TEST_IDENTIFIER,
                    "test-without-building",
                ],
            )
            payloads = [
                command for command, _ in sdk.commands if "--payload-url" in command
            ]
            self.assertEqual(len(payloads), 1)
            self.assertEqual(
                payloads[0][payloads[0].index("--payload-url") + 1],
                f"blehub-rn-spike://adapter/{RUN}/ready",
            )
            for command, options in sdk.commands:
                self.assertEqual(options["stdin"], subprocess.DEVNULL)
                self.assertTrue(options["start_new_session"])
                self.assertEqual(options["stderr"], subprocess.STDOUT)
                for forbidden in (
                    "--terminate-existing",
                    "--console",
                    "-retry-tests-on-failure",
                    "-run-tests-until-failure",
                    "-test-iterations",
                ):
                    self.assertNotIn(forbidden, command)
            self.assertTrue(all(child.waits for child in sdk.children))
            self.assertTrue(owner.output.closed)

    def test_exited_runner_cannot_receive_ready_even_if_it_exits_during_authorization(
        self,
    ) -> None:
        for fault in ("exited-before-ready", "exited-in-ready-guard"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                sdk = SDK(Path(directory), fault)
                with sdk.patched():
                    owner = sdk.make()
                    with self.assertRaisesRegex(
                        (EvidenceError, RuntimeError), "exited|runner|window"
                    ):
                        owner.run()
                self.assertNotIn("relay", sdk.events)
                self.assertFalse(owner.delivered)

    def test_runner_timeout_guard_failure_sdk_failure_and_child_teardown_are_durable(
        self,
    ) -> None:
        for fault, error_type in (
            ("failed-runner", EvidenceError),
            ("exited-without-window", EvidenceError),
            ("timeout", TimeoutError),
            ("guard-failure", RuntimeError),
            ("ready-guard-failure", RuntimeError),
            ("foreign-process", RuntimeError),
            ("foreign-payload", RuntimeError),
            ("hung-sdk", subprocess.TimeoutExpired),
        ):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                sdk = SDK(Path(directory), fault)
                with sdk.patched():
                    owner = sdk.make()
                    with self.assertRaises(error_type) as caught:
                        owner.run()
                    with self.assertRaisesRegex(EvidenceError, "cannot be rerun"):
                        owner.run()
                result = json.loads(sdk.log.with_suffix(".capture.json").read_text())
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["primary_error"], str(caught.exception))
                self.assertTrue(owner.output.closed)
                self.assertEqual(
                    sum("xcodebuild" in command for command, _ in sdk.commands), 1
                )
                self.assertTrue(
                    all(
                        child.returncode is not None and child.waits
                        for child in sdk.children
                    )
                )
                self.assertFalse(sdk.children[0].terminated)
                if fault not in {"failed-runner", "exited-without-window"}:
                    self.assertIn((sdk.children[0].pid, signal.SIGKILL), sdk.signals)
                if fault in {
                    "guard-failure",
                    "ready-guard-failure",
                    "foreign-process",
                    "hung-sdk",
                    "timeout",
                }:
                    self.assertNotIn("relay", sdk.events)
                if fault == "guard-failure":
                    self.assertIs(caught.exception, sdk.guard_error)
                if fault == "ready-guard-failure":
                    self.assertIs(caught.exception, sdk.ready_error)
                if fault == "hung-sdk":
                    self.assertTrue(sdk.children[1].terminated)
                self.assertLess(sdk.clock.now, owner.started + 4)

    def test_primary_failure_survives_cleanup_failure_and_output_is_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sdk = SDK(Path(directory), "cleanup-failure")
            with (
                sdk.patched(),
                mock.patch.object(sdk, "ready_guard", side_effect=sdk.ready_error),
            ):
                owner = sdk.make()
                with self.assertRaises(RuntimeError) as caught:
                    owner.run()
            self.assertIs(caught.exception, sdk.ready_error)
            self.assertTrue(
                any(
                    "owned group cleanup failed" in note
                    for note in caught.exception.__notes__
                )
            )
            result = json.loads(sdk.log.with_suffix(".capture.json").read_text())
            self.assertEqual(result["primary_error"], str(sdk.ready_error))
            self.assertEqual(result["cleanup_errors"], ["owned group cleanup failed"])
            self.assertTrue(owner.output.closed)
            self.assertEqual(sdk.log.read_bytes(), marker())

    def test_interrupt_and_prelaunch_guard_failure_do_not_retry_or_leak_children(
        self,
    ) -> None:
        for prelaunch in (False, True):
            with (
                self.subTest(prelaunch=prelaunch),
                tempfile.TemporaryDirectory() as directory,
            ):
                sdk = SDK(Path(directory))
                primary = KeyboardInterrupt("human interrupted window")
                with (
                    sdk.patched(),
                    mock.patch.object(
                        sdk,
                        "guard" if prelaunch else "ready_guard",
                        side_effect=primary,
                    ),
                ):
                    owner = sdk.make()
                    with self.assertRaises(KeyboardInterrupt) as caught:
                        owner.run()
                self.assertIs(caught.exception, primary)
                result = json.loads(sdk.log.with_suffix(".capture.json").read_text())
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["primary_error"], str(primary))
                self.assertNotIn("relay", sdk.events)
                if prelaunch:
                    self.assertEqual(sdk.commands, [])
                else:
                    self.assertTrue(owner.output.closed)
                    self.assertTrue(
                        all(
                            child.returncode is not None and child.waits
                            for child in sdk.children
                        )
                    )
                    self.assertEqual(
                        sdk.signals, [(sdk.children[0].pid, signal.SIGKILL)]
                    )

    def test_truncated_window_marker_at_successful_exit_is_not_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sdk = SDK(Path(directory))
            sdk.raw += capture.WINDOW_PREFIX.encode() + b'{"run_id":'
            with sdk.patched():
                owner = sdk.make()
                with self.assertRaisesRegex(EvidenceError, "truncated"):
                    owner.run()
            self.assertEqual(owner.receipt["status"], "failed")
            self.assertEqual(sdk.log.with_suffix(".raw.log").read_bytes(), sdk.raw)


class StartupWindowFailureBoundaryTests(unittest.TestCase):
    def test_expired_initial_budget_and_late_preflight_never_submit_runner(
        self,
    ) -> None:
        for late_preflight in (False, True):
            with (
                self.subTest(late_preflight=late_preflight),
                tempfile.TemporaryDirectory() as directory,
            ):
                sdk = SDK(Path(directory))
                with sdk.patched() as ready:
                    owner = sdk.make()
                    if late_preflight:
                        sdk.clock.sleep(0.25)

                        def preflight(*args, sdk=sdk, **kwargs) -> None:
                            sdk.clock.sleep(kwargs["timeout"] + 0.01)

                        ready.side_effect = preflight
                    else:
                        sdk.clock.now = owner.deadline
                    with self.assertRaises(TimeoutError) as caught:
                        owner.run()
                    if late_preflight:
                        ready.assert_called_once_with(
                            DEVICE,
                            cwd=Path(capture.__file__).resolve().parents[2],
                            timeout=2.75,
                        )
                    else:
                        ready.assert_not_called()
                self.assertEqual(sdk.commands, [])
                self.assertEqual(sdk.children, [])
                self.assertIsNone(owner.output)
                self.assertFalse(sdk.log.with_suffix(".raw.log").exists())
                result = json.loads(sdk.log.with_suffix(".capture.json").read_text())
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["primary_error"], str(caught.exception))

    def test_real_preflight_shares_remaining_budget_and_rejects_late_success(
        self,
    ) -> None:
        for late_query in (None, "list", "lock"):
            with (
                self.subTest(late_query=late_query),
                tempfile.TemporaryDirectory() as directory,
            ):
                sdk = SDK(Path(directory))
                calls = []

                def query(
                    command, sdk=sdk, calls=calls, late_query=late_query, **kwargs
                ):
                    calls.append((command, kwargs))
                    listing = command[2:] == ["list", "devices"]
                    late = late_query == ("list" if listing else "lock")
                    sdk.clock.sleep(kwargs["timeout"] if late else 0.25)
                    output = (
                        f"iPhone {DEVICE} connected\n"
                        if listing
                        else json.dumps(
                            {
                                "result": {
                                    "passcodeRequired": False,
                                    "unlockedSinceBoot": True,
                                }
                            }
                        )
                    )
                    return subprocess.CompletedProcess(command, 0, output, "")

                with (
                    sdk.patched() as ready,
                    mock.patch.object(ios_device.shutil, "which", return_value="xcrun"),
                    mock.patch.object(ios_device.subprocess, "run", side_effect=query),
                ):
                    ready.side_effect = ios_device.require_ready_unlocked_device
                    owner = sdk.make()
                    sdk.clock.sleep(0.5)
                    if late_query is None:
                        owner.run()
                        self.assertEqual(
                            owner.receipt["status"],
                            "window-completed-awaiting-evidence",
                        )
                    else:
                        with self.assertRaisesRegex(
                            ios_device.IosDevicePreconditionError, "overall timeout"
                        ):
                            owner.run()
                        self.assertEqual(sdk.commands, [])
                        self.assertIsNone(owner.process)
                        self.assertEqual(owner.receipt["status"], "failed")
                ready.assert_called_once_with(
                    DEVICE, cwd=Path(capture.__file__).resolve().parents[2], timeout=2.5
                )
                self.assertEqual(calls[0][0], ["xcrun", "devicectl", "list", "devices"])
                self.assertEqual(calls[0][1]["timeout"], 2.5)
                self.assertEqual(len(calls), 1 if late_query == "list" else 2)
                if len(calls) == 2:
                    self.assertEqual(calls[1][1]["timeout"], 2.25)
                    self.assertIn("lockState", calls[1][0])
                    self.assertEqual(
                        calls[1][0][calls[1][0].index("--device") + 1], DEVICE
                    )

    def test_ready_guard_expiry_blocks_relay_for_whole_and_window_deadlines(
        self,
    ) -> None:
        for timeout in (3, 60):
            with (
                self.subTest(timeout=timeout),
                tempfile.TemporaryDirectory() as directory,
            ):
                sdk = SDK(Path(directory))

                def expire_ready(sdk=sdk) -> None:
                    sdk.ready_guard()
                    assert sdk.owner is not None
                    sdk.clock.now = min(sdk.owner.deadline, sdk.owner.window_deadline)

                with sdk.patched():
                    owner = sdk.make(timeout=timeout, ready_guard=expire_ready)
                    with self.assertRaises(TimeoutError):
                        owner.run()
                self.assertIn("ready-guard", sdk.events)
                self.assertNotIn("relay", sdk.events)
                self.assertEqual(len(sdk.commands), 2)
                self.assertEqual(sdk.events.count("pid"), 1)
                self.assertFalse(owner.delivered)
                self.assertFalse(
                    sdk.log.with_suffix(".startup-ready-payload.json").exists()
                )
                self.assertEqual(owner.receipt["status"], "failed")
                self.assertTrue(owner.output.closed)
                self.assertTrue(
                    all(
                        child.returncode is not None and child.waits
                        for child in sdk.children
                    )
                )
                if timeout == 60:
                    self.assertLess(owner.window_deadline, owner.deadline)
                else:
                    self.assertEqual(owner.window_deadline, owner.deadline)

    def test_real_command_rejects_expired_deadlines_before_sdk_popen(self) -> None:
        for limit in ("whole", "window", "zero-command-budget"):
            with self.subTest(limit=limit), tempfile.TemporaryDirectory() as directory:
                sdk = SDK(Path(directory))
                with sdk.patched():
                    owner = sdk.make(timeout=60)
                    raw = sdk.log.with_suffix(".raw.log")
                    raw.write_bytes(marker())
                    owner.stream = capture_io.PrefixStream(
                        raw, None, prefix=capture.WINDOW_PREFIX
                    )
                    owner._poll()
                    if limit != "zero-command-budget":
                        sdk.clock.now = (
                            owner.deadline
                            if limit == "whole"
                            else owner.window_deadline
                        )
                    receipt = sdk.log.with_suffix(".expired-payload.json")
                    command = [
                        "xcrun",
                        "devicectl",
                        "device",
                        "process",
                        "launch",
                        "--device",
                        DEVICE,
                        "--payload-url",
                        f"blehub-rn-spike://adapter/{RUN}/ready",
                        "--json-output",
                        str(receipt),
                        BUNDLE_ID,
                    ]
                    with self.assertRaises(subprocess.TimeoutExpired) as caught:
                        owner._command(
                            command, budget=0 if limit == "zero-command-budget" else 1.5
                        )
                    self.assertEqual(caught.exception.cmd, command)
                self.assertEqual(sdk.commands, [])
                self.assertEqual(sdk.children, [])
                self.assertFalse(receipt.exists())
                self.assertFalse(owner.delivered)

    def test_poll_validation_error_survives_publication_failure_without_losing_raw_batch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sdk = SDK(Path(directory))
            foreign = window()
            foreign["fixture_receipt_sha256"] = "b" * 64
            sdk.raw = b"before\n" + marker(foreign) + marker() + b"after\n"
            publication_error = OSError("normalized log publication failed")
            original_write = Path.write_text
            attempts = []

            def write(path, text, *args, **kwargs):
                if path == sdk.log and text:
                    attempts.append(text)
                    raise publication_error
                return original_write(path, text, *args, **kwargs)

            with sdk.patched(), mock.patch.object(Path, "write_text", write):
                owner = sdk.make()
                with self.assertRaisesRegex(
                    EvidenceError, "window version/run/receipt"
                ) as caught:
                    owner.run()
            self.assertTrue(
                any(
                    str(publication_error) in note
                    for note in caught.exception.__notes__
                )
            )
            self.assertGreaterEqual(len(attempts), 2)
            self.assertEqual(
                owner.lines, (marker(foreign) + marker()).decode().splitlines()
            )
            self.assertEqual(sdk.log.with_suffix(".raw.log").read_bytes(), sdk.raw)
            self.assertEqual(owner.receipt["status"], "failed")
            self.assertEqual(owner.receipt["primary_error"], str(caught.exception))
            self.assertTrue(owner.output.closed)
            self.assertNotIn("relay", sdk.events)

    def test_primary_survives_stop_close_and_final_publication_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sdk = SDK(Path(directory), "cleanup-failure")
            close_error = OSError("raw output close failed")
            publication_error = OSError("final normalized log publication failed")
            original_write = Path.write_text
            failing = False
            attempts = []

            def fail_ready() -> None:
                nonlocal failing
                failing = True
                raise sdk.ready_error

            def write(path, text, *args, **kwargs):
                if path == sdk.log and failing:
                    attempts.append(text)
                    raise publication_error
                return original_write(path, text, *args, **kwargs)

            with (
                sdk.patched(),
                failing_raw_close(
                    sdk.log.with_suffix(".raw.log"), close_error
                ) as streams,
                mock.patch.object(Path, "write_text", write),
            ):
                owner = sdk.make(ready_guard=fail_ready)
                with self.assertRaises(RuntimeError) as caught:
                    owner.run()
            self.assertIs(caught.exception, sdk.ready_error)
            for message in (
                "owned group cleanup failed",
                str(close_error),
                str(publication_error),
            ):
                self.assertTrue(
                    any(message in note for note in caught.exception.__notes__)
                )
                self.assertTrue(
                    any(message in error for error in owner.receipt["cleanup_errors"])
                )
            self.assertTrue(attempts)
            self.assertEqual(len(streams), 1)
            self.assertTrue(streams[0].closed)
            self.assertEqual(owner.receipt["status"], "failed")
            self.assertEqual(owner.receipt["primary_error"], str(sdk.ready_error))
            result = json.loads(sdk.log.with_suffix(".capture.json").read_text())
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["primary_error"], str(sdk.ready_error))
            self.assertEqual(sdk.signals, [(sdk.children[0].pid, signal.SIGKILL)])

    def test_success_cleanup_failure_never_leaves_completed_receipt(self) -> None:
        for fault in ("stop", "close", "publication"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                sdk = SDK(Path(directory))
                error = OSError(f"successful window {fault} failed")
                original_wait, original_write = Child.wait, Path.write_text
                writes = []

                def wait(
                    child,
                    timeout,
                    *,
                    original_wait=original_wait,
                    fault=fault,
                    error=error,
                ):
                    result = original_wait(child, timeout)
                    if child.runner and fault == "stop":
                        raise error
                    return result

                def write(
                    path,
                    text,
                    *args,
                    sdk=sdk,
                    writes=writes,
                    fault=fault,
                    error=error,
                    original_write=original_write,
                    **kwargs,
                ):
                    if path == sdk.log and sdk.children and sdk.children[0].waits:
                        writes.append(text)
                        if fault == "publication":
                            raise error
                    return original_write(path, text, *args, **kwargs)

                with sdk.patched(), ExitStack() as stack:
                    streams = None
                    if fault == "close":
                        streams = stack.enter_context(
                            failing_raw_close(sdk.log.with_suffix(".raw.log"), error)
                        )
                    stack.enter_context(mock.patch.object(Child, "wait", wait))
                    stack.enter_context(mock.patch.object(Path, "write_text", write))
                    owner = sdk.make()
                    with self.assertRaises(OSError) as caught:
                        owner.run()
                self.assertIs(caught.exception, error)
                self.assertTrue(owner.delivered)
                self.assertEqual(owner.receipt["status"], "failed")
                self.assertTrue(
                    any(str(error) in item for item in owner.receipt["cleanup_errors"])
                )
                self.assertTrue(writes)
                self.assertTrue(
                    all(
                        child.returncode is not None and child.waits
                        for child in sdk.children
                    )
                )
                if streams is not None:
                    self.assertTrue(streams[0].closed)
                else:
                    self.assertTrue(owner.output.closed)
                result = json.loads(sdk.log.with_suffix(".capture.json").read_text())
                self.assertEqual(result["status"], "failed")
                self.assertTrue(
                    any(str(error) in item for item in result["cleanup_errors"])
                )


class StartupWindowJournalDownloadTests(unittest.TestCase):
    def test_download_is_postwindow_exact_run_container_without_relaunch_or_expired_clock(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sdk = SDK(Path(directory))

            def active_download() -> None:
                sdk.ready_guard()
                with self.assertRaisesRegex(EvidenceError, "post-window"):
                    owner.download_journal()

            with sdk.patched():
                owner = sdk.make(ready_guard=active_download)
                with self.assertRaisesRegex(EvidenceError, "post-window"):
                    owner.download_journal()
                owner.run()
                sdk.clock.now = owner.deadline + 100
                before = len(sdk.commands)
                path = owner.download_journal()
                self.assertEqual(sdk.events[-3:], ["guard", "copy", "guard"])
                with self.assertRaisesRegex(EvidenceError, "existing"):
                    owner.download_journal()
            self.assertEqual(path, sdk.log.with_suffix(".journal.json"))
            self.assertEqual(json.loads(path.read_text()), sdk.journal)
            self.assertEqual(len(sdk.commands), before + 1)
            command, options = sdk.commands[-1]
            self.assertEqual(
                command,
                [
                    "xcrun",
                    "devicectl",
                    "device",
                    "copy",
                    "from",
                    "--device",
                    DEVICE,
                    "--source",
                    f"Library/Caches/blehub-ios-adapter/{RUN}.json",
                    "--domain-type",
                    "appDataContainer",
                    "--domain-identifier",
                    BUNDLE_ID,
                    "--destination",
                    str(path),
                    "--quiet",
                    "--timeout",
                    "15",
                    "--json-output",
                    str(sdk.log.with_suffix(".journal-copy.json")),
                ],
            )
            self.assertEqual(options["stdin"], subprocess.DEVNULL)
            self.assertTrue(options["start_new_session"])
            self.assertTrue(options["check"])
            self.assertEqual(options["timeout"], 20)
            self.assertEqual(options["stderr"], subprocess.STDOUT)

    def test_download_refuses_existing_outputs_including_dangling_symlinks(
        self,
    ) -> None:
        for suffix in (".journal.json", ".journal-copy.json"):
            for symlink in (False, True):
                with (
                    self.subTest(suffix=suffix, symlink=symlink),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    sdk = SDK(Path(directory))
                    with sdk.patched():
                        owner = sdk.make()
                        owner.run()
                        path = sdk.log.with_suffix(suffix)
                        if symlink:
                            path.symlink_to(sdk.root / "absent-journal")
                        else:
                            path.write_bytes(b"previous evidence")
                        before = len(sdk.commands)
                        with self.assertRaisesRegex(EvidenceError, "existing"):
                            owner.download_journal()
                    self.assertEqual(len(sdk.commands), before)
                    if symlink:
                        self.assertTrue(path.is_symlink())
                    else:
                        self.assertEqual(path.read_bytes(), b"previous evidence")

    def test_failed_window_allows_diagnosis_but_foreign_copy_receipts_and_journals_fail(
        self,
    ) -> None:
        for fault in (
            "valid",
            "foreign-copy-device",
            "foreign-copy-selector",
            "foreign-journal-run",
        ):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                sdk = SDK(Path(directory), "failed-runner")
                with sdk.patched():
                    owner = sdk.make()
                    with self.assertRaises(EvidenceError):
                        owner.run()
                    sdk.fault = fault
                    if fault == "foreign-journal-run":
                        sdk.journal["run_id"] = FOREIGN_RUN
                    if fault == "valid":
                        path = owner.download_journal()
                        self.assertTrue(path.is_file())
                    else:
                        with self.assertRaises((EvidenceError, RuntimeError)):
                            owner.download_journal()
                self.assertEqual(owner.receipt["status"], "failed")
                self.assertEqual(
                    sum("--payload-url" in command for command, _ in sdk.commands), 1
                )
                self.assertEqual(
                    sum("xcodebuild" in command for command, _ in sdk.commands), 1
                )
                self.assertEqual(
                    sdk.log.with_suffix(".journal.json").read_text(),
                    json.dumps(sdk.journal),
                )


if __name__ == "__main__":
    unittest.main()
