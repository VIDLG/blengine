"""Synthetic iOS schemas/SDK streams only. No device, installation or RF proof."""

from __future__ import annotations

import copy
import json
import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from tests.peripherals.nrf52840 import fixture_session
from tests.peripherals.nrf52840 import pending_reload as physical
from tests.peripherals.nrf52840.support.session_receipts import fake_attempt
from tools.react_native import capture_io
from tools.react_native import capture_transport as sdk
from tools.react_native import ios_pending_reload_capture as api
from tools.react_native import pending_read_reload_runner as runner
from tools.react_native import pending_reload_identity as identity
from tools.react_native import pending_reload_ledger as ledger
from tools.react_native import rf_evidence_bundle as evidence
from tools.react_native.host_contract import BUNDLE_ID, SCHEME
from tools.tests.react_native.support.pending_reload_ledger import (
    synthetic_physical,
    synthetic_runtime,
)
from tools.tests.react_native.support.pending_reload_runner import mocked_run
from tools.tests.react_native.support.recovery_capture import (
    BOOT,
    DEVICE,
    IOS_URL,
    RUN,
    SERVICE,
    Clock,
    document,
)

REAL_CAPTURE = api.capture

OWNERS = (
    "33333333-3333-4333-8333-333333333333",
    "44444444-4444-4444-8444-444444444444",
)


def ios_value(value):
    if isinstance(value, dict):
        return {key: ios_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [ios_value(item) for item in value]
    if not isinstance(value, str):
        return value
    for base in (12000, 13000):
        value = value.replace(f"commandId={base + 8}:", f"commandId={base + 11}:")
        value = value.replace(f"commandId={base + 7}:", f"commandId={base + 9}:")
        value = value.replace(
            f"gattSessionId={base + 6}:", f"gattSessionId={base + 8}:"
        )
        if value.endswith(f"gattSessionId={base + 6}"):
            value = value[: -len(str(base + 6))] + str(base + 8)
    return value.replace("peripheralId=88", "peripheralId=0")


def host(event: str, generation: int, run: str) -> str:
    value = {
        "event": event,
        "runId": run,
        "pid": 42,
        "bootId": BOOT,
        "ownerId": OWNERS[generation - 1],
        "generation": generation,
    }
    if event in ("arm-url-accepted", "reload-url-accepted"):
        action = "arm" if event == "arm-url-accepted" else "reload"
        value["detail"] = f"blehub-rn-spike://{action}/{run}"
    return identity.HOST_PREFIX + json.dumps(value, sort_keys=True)


def ios_runtime(run: str) -> list[str]:
    result = []
    for line in ios_value(synthetic_runtime(run)):
        for generation in (1, 2):
            prefix = physical.PREFIX + f"generation={generation}:"
            for marker, event in (
                ("runtime-start:", "runtime-installed"),
                ("native-ready:passed", "native-ready"),
                ("arm-js-observed:", "arm-url-accepted"),
            ):
                if line.startswith(prefix + marker):
                    result.append(host(event, generation, run))
            if generation == 2 and line == prefix + "close:passed":
                result.append(host("native-close-complete", 2, run))
        if line.startswith(
            physical.PREFIX + "native:engine-invalidation-close-started:"
        ):
            result.extend(
                [
                    host("reload-url-accepted", 1, run),
                    host("module-invalidation-entered", 1, run),
                ]
            )
        if line == physical.PREFIX + f"passed:runId={run}":
            result.append(host("javascript-complete", 2, run))
        result.append(line)
        if line.startswith(physical.PREFIX + "native:engine-close-complete:"):
            result.append(host("native-close-complete", 1, run))
    return result


class IosIdentityLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.attempt = fake_attempt(Path(self.temporary.name))
        self.plan = self.attempt.plan
        self.lines = ios_runtime(self.plan.session_id)
        self.owner = (os.getpid(), os.getpgid(0))
        self.document = ios_value(synthetic_physical(self.plan, [], self.owner))
        self.document["platform"] = "ios"

    def verify(self, lines=None, document=None):
        path = self.attempt.root / physical.PHYSICAL_NAME
        physical.write_document(path, self.document if document is None else document)
        return ledger.verify(
            "\n".join(self.lines if lines is None else lines) + "\n",
            platform="ios",
            run_id=self.plan.session_id,
            service_uuid=self.plan.service_uuid,
            fixture_request=self.plan.request_path,
            fixture_identity=self.owner,
            physical_path=path,
            host_actions=("arm-1", "reload", "arm-2"),
        )

    def test_actual_ios_route_zero_peripheral_two_native_owners_and_synthetic_limit(
        self,
    ):
        result = self.verify()
        self.assertEqual(result["mode"], physical.IOS_MODE)
        self.assertEqual(
            result["native_identity"]["owners"], {"1": OWNERS[0], "2": OWNERS[1]}
        )
        self.assertIn(
            "commandId=12011:connectionId=12004:gattSessionId=12008",
            result["generations"][0]["read_route"],
        )
        self.assertEqual(result["generations"][0]["peripheral_id"], "0")
        self.assertIn("Synthetic", result["explicit_limits"]["late_submit"])
        self.assertNotIn("late_callback", result)

    def test_missing_duplicate_unknown_host_events_pid_boot_owner_run_or_generation_fail(
        self,
    ):
        for index, line in enumerate(self.lines):
            if not line.startswith(identity.HOST_PREFIX):
                continue
            with (
                self.subTest(index=index, fault="missing"),
                self.assertRaises(RuntimeError),
            ):
                self.verify(self.lines[:index] + self.lines[index + 1 :])
            with (
                self.subTest(index=index, fault="duplicate"),
                self.assertRaises(RuntimeError),
            ):
                self.verify(self.lines[:index] + [line] + self.lines[index:])
            record = json.loads(line[len(identity.HOST_PREFIX) :])
            for key, value in (
                ("pid", 43),
                ("pid", True),
                ("bootId", RUN),
                ("ownerId", RUN),
                ("runId", BOOT),
                ("generation", True),
                ("extra", 1),
                ("event", "failed"),
            ):
                changed = {**record, key: value}
                lines = self.lines.copy()
                lines[index] = identity.HOST_PREFIX + json.dumps(changed)
                with (
                    self.subTest(index=index, key=key, value=value),
                    self.assertRaises((RuntimeError, ValueError)),
                ):
                    self.verify(lines)

    def test_cross_prefix_order_android_route_overflow_synthetic_suffix_and_projection_tampering(
        self,
    ):
        mutations = [
            lambda text: text.replace("commandId=12011:", "commandId=12008:"),
            lambda text: text.replace("gattSessionId=13008", "gattSessionId=13006"),
            lambda text: text.replace(
                "characteristicId=98765", "characteristicId=18446744073709551616"
            ),
            lambda text: text.replace("peripheralId=0", "peripheralId=00"),
            lambda text: text.replace(
                "peripheralId=0", "peripheralId=18446744073709551616"
            ),
            lambda text: text.replace("source=synthetic", "source=callback"),
            lambda text: text.replace(OWNERS[1], OWNERS[0]),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(RuntimeError):
                self.verify(mutation("\n".join(self.lines)).splitlines())
        lines = self.lines.copy()
        first = next(
            i
            for i, line in enumerate(lines)
            if '"event": "native-close-complete"' in line
        )
        line = lines.pop(first)
        lines.insert(
            next(
                i
                for i, line in enumerate(lines)
                if "generation=2:native-ready:passed" in line
            )
            + 1,
            line,
        )
        with self.assertRaises(RuntimeError):
            self.verify(lines)
        document = copy.deepcopy(self.document)
        document["boundaries"][1]["route"] = document["boundaries"][1]["route"].replace(
            "12011", "12008"
        )
        with self.assertRaises(RuntimeError):
            self.verify(document=document)


class Child:
    def __init__(self, harness, command, output):
        self.harness, self.command, self.output = harness, command, output
        self.pid = 8000 + len(harness.children)
        self.returncode = None
        self.console = "--console" in command
        self.ready = harness.clock.now + 0.3

    def poll(self):
        if (
            not self.console
            and self.returncode is None
            and self.harness.clock.now >= self.ready
            and self.harness.fault != "stalled-command"
        ):
            self.returncode = 0
        return self.returncode

    def terminate(self):
        if self.console:
            raise AssertionError("must not forward catchable signal to app")
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout):
        if self.returncode is None:
            raise subprocess.TimeoutExpired(self.command, timeout)
        return self.returncode


class Harness:
    def __init__(self, root: Path, fault: str = "", run_id: str = RUN):
        self.root, self.fault, self.run_id = root, fault, run_id
        self.log, self.coordination = root / "runtime.log", root / "coordination"
        self.coordination.mkdir(exist_ok=True)
        self.install = root / "device-install.json"
        physical.write_document(
            self.install,
            document(
                "devicectl.device.install.app",
                {
                    "installedApplications": [
                        {"bundleID": BUNDLE_ID, "installationURL": IOS_URL}
                    ]
                },
            ),
        )
        self.clock, self.children, self.controls = Clock(), [], []
        self.service = SERVICE
        self.console = None
        self.lines = ios_runtime(run_id)
        self.cursor = 0
        self.published_during_command = False

    def emit_until(self, marker: str):
        while self.cursor < len(self.lines):
            line = self.lines[self.cursor]
            self.cursor += 1
            self.console.output.write((line + "\r\n").encode())
            if marker in line:
                break
        self.console.output.flush()

    def progress(self, phase: str, generation: int):
        route = next(
            line.split("awaiting-arm:", 1)[1]
            for line in self.lines
            if f"generation={generation}:awaiting-arm:" in line
        )
        physical.write_document(
            self.coordination / physical.PROGRESS_NAME,
            {
                "schema_version": 1,
                "run_id": self.run_id,
                "status": "running",
                "phase": phase,
                "route": route,
                "arm_url": f"blehub-rn-spike://arm/{self.run_id}",
            },
        )

    def popen(self, command, **kwargs):
        child = Child(self, command, kwargs["stdout"])
        self.children.append(child)
        if child.console:
            self.console = child
            if self.fault != "missing-bootstrap":
                self.emit_until("generation=1:awaiting-arm:")
                self.progress("armed-1", 1)
        else:
            output = Path(command[command.index("--json-output") + 1])
            if "--payload-url" in command:
                action = ("arm-1", "reload", "arm-2")[len(self.controls)]
                self.controls.append(action)
                if action == "arm-1":
                    self.emit_until("generation=1:read-dispatched:")
                    self.progress("waiting-1", 1)
                elif action == "reload":
                    self.emit_until("generation=2:awaiting-arm:")
                    self.progress("armed-2", 2)
                else:
                    self.emit_until("passed:runId=")
                    physical.write_document(
                        self.coordination / physical.PHYSICAL_NAME, {"status": "passed"}
                    )
                record = document(
                    "devicectl.device.process.launch",
                    {
                        "process": {
                            "processIdentifier": 42,
                            "executable": IOS_URL + SCHEME,
                        }
                    },
                )
                if self.fault == "foreign-payload":
                    record["result"]["process"]["processIdentifier"] = 43
            else:
                record = document(
                    "devicectl.device.info.processes",
                    {
                        "runningProcesses": [
                            {"processIdentifier": 42, "executable": IOS_URL + SCHEME}
                        ]
                    },
                )
                if self.fault == "wrong-executable":
                    record["result"]["runningProcesses"][0]["executable"] += "wrong"
            record["info"]["arguments"] = command[1:]
            if self.fault == "foreign-device":
                record["result"]["deviceIdentifier"] = "foreign"
            physical.write_document(output, record)
        return child

    def guard(self):
        if self.fault == "owner-lost" and self.controls:
            raise RuntimeError("fixture receipt replaced")
        if self.controls and self.log.exists():
            self.published_during_command |= "arm-js-observed" in self.log.read_text()

    def run(self):
        with (
            patch.object(sdk.subprocess, "Popen", side_effect=self.popen),
            patch.object(sdk.time, "monotonic", self.clock.monotonic),
            patch.object(sdk.time, "sleep", self.clock.sleep),
            patch.object(
                capture_io.os, "killpg", side_effect=lambda *_: self.console.kill()
            ),
        ):
            return REAL_CAPTURE(
                DEVICE,
                self.log,
                self.coordination,
                run_id=self.run_id,
                service_uuid=self.service,
                local_name="fixture",
                seconds=90,
                guard=self.guard,
                install_json=self.install,
            )


class IosCaptureTests(unittest.TestCase):
    def test_official_single_launch_pumps_all_controls_and_separate_identity_projection(
        self,
    ):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(Path(root))
            text, actions = harness.run()
            self.assertEqual(actions, ("arm-1", "reload", "arm-2"))
            self.assertEqual(text.splitlines(), harness.lines)
            self.assertTrue(harness.published_during_command)
            self.assertEqual(len([c for c in harness.children if c.console]), 1)
            command = harness.console.command
            self.assertIn("--blehub-nitro-rf-pending-read-reload-service", command)
            self.assertIn("--blehub-nitro-rf-pending-read-reload-run-id", command)
            self.assertNotIn("--blehub-nitro-reload", command)
            projection = (harness.coordination / "runtime.log").read_text()
            self.assertNotIn(identity.HOST_PREFIX, projection)
            self.assertIn(
                identity.HOST_PREFIX,
                harness.log.with_suffix(".native-identity.log").read_text(),
            )
            facts = physical.RuntimeFacts(RUN, platform="ios")
            facts.refresh(harness.coordination / "runtime.log")
            self.assertIn("passed", facts.seen)
            self.assertTrue(all(c.returncode is not None for c in harness.children))

    def test_foreign_receipts_missing_identity_owner_loss_and_hung_sdk_are_bounded_and_reaped(
        self,
    ):
        for fault in (
            "foreign-payload",
            "wrong-executable",
            "foreign-device",
            "owner-lost",
            "missing-bootstrap",
            "stalled-command",
        ):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as root:
                harness = Harness(Path(root), fault)
                with self.assertRaises(
                    (RuntimeError, TimeoutError, subprocess.SubprocessError)
                ):
                    harness.run()
                self.assertTrue(all(c.returncode is not None for c in harness.children))
                self.assertLess(harness.clock.now, 195)
                self.assertEqual(
                    json.loads(harness.log.with_suffix(".capture.json").read_text())[
                        "status"
                    ],
                    "failed",
                )


class IosCaptureFinalizationTests(unittest.TestCase):
    def test_final_drain_failure_or_cleanup_cannot_leave_a_passed_capture(self):
        for fault in ("late-failure", "cleanup", "primary-and-cleanup"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temporary:
                h = Harness(
                    Path(temporary),
                    "wrong-executable" if fault == "primary-and-cleanup" else "",
                )
                stop = api.stop_local

                def finish(child, *, console, fault=fault, h=h, stop=stop):
                    result = stop(child, console=console)
                    if fault == "late-failure":
                        child.output.write(
                            (
                                identity.HOST_PREFIX
                                + json.dumps(
                                    {"event": "failed", "detail": "late-native-failure"}
                                )
                                + "\n"
                            ).encode()
                        )
                        child.output.flush()
                        return result
                    raise RuntimeError("synthetic console cleanup failure")

                with (
                    patch.object(api, "stop_local", side_effect=finish),
                    self.assertRaises(RuntimeError) as raised,
                ):
                    h.run()
                value = physical.read_document(h.log.with_suffix(".capture.json"))
                self.assertEqual(value["status"], "failed")
                self.assertTrue(
                    all(child.returncode is not None for child in h.children)
                )
                if fault == "primary-and-cleanup":
                    self.assertIn("PID/executable", str(raised.exception))
                    self.assertIn("cleanup", " ".join(raised.exception.__notes__))


def make_app(root: Path) -> Path:
    app = root / "BleHubNitroHost.app"
    app.mkdir()
    (app / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleIdentifier": BUNDLE_ID, "CFBundleExecutable": SCHEME})
    )
    (app / SCHEME).write_bytes(
        bytes.fromhex("cffaedfe") + b"synthetic native, not executable/RF"
    )
    (app / SCHEME).chmod(0o755)
    (app / "main.jsbundle").write_bytes(b"synthetic JS, not executable/RF")
    return app


class IosPendingEvidenceTests(unittest.TestCase):
    def ready(self, root: Path):
        attempt = root / "attempt"
        bundle = evidence.EvidenceBundle(
            attempt,
            platform="ios",
            device=DEVICE,
            project_root=root,
            scenario="pending-read-reload",
        )
        snapshot = bundle.prepare_artifact(make_app(root))
        fixture = fake_attempt(attempt)
        plan = fixture.plan
        harness = Harness(attempt, run_id=plan.session_id)
        harness.service = plan.service_uuid
        installed = physical.read_document(harness.install)
        installed["info"]["arguments"] += [
            "--json-output",
            str(harness.install),
            str(snapshot),
        ]
        physical.write_document(harness.install, installed)
        text, actions = harness.run()
        proof = ios_value(synthetic_physical(plan, [], (os.getpid(), os.getpgid(0))))
        proof["platform"] = "ios"
        physical.write_document(fixture.root / physical.PHYSICAL_NAME, proof)
        result = ledger.verify(
            text,
            platform="ios",
            run_id=plan.session_id,
            service_uuid=plan.service_uuid,
            physical_path=fixture.root / physical.PHYSICAL_NAME,
            fixture_request=plan.request_path,
            fixture_identity=(os.getpid(), os.getpgid(0)),
            host_actions=actions,
        )
        physical.write_document(attempt / "pending-reload-ledger.json", result)
        return bundle, harness

    def finish(self, bundle, harness):
        bundle.finish(
            completed=True,
            primary_error=None,
            cleanup_errors=[],
            runtime_log=harness.log,
        )
        return physical.read_document(bundle.attempt / evidence.MANIFEST)

    def test_ios_bundle_requires_official_receipts_native_identity_and_both_projection_hashes(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            bundle, harness = self.ready(Path(temporary))
            result = self.finish(bundle, harness)
            self.assertEqual(result["status"], "passed", result["evidence_errors"])
            self.assertTrue(all(result["references"].values()))
            self.assertIn("native_identity_log", result["references"])
            evidence.verify_bundle(bundle.attempt / evidence.MANIFEST)
            original = harness.log.with_suffix(".native-identity.log").read_bytes()
            harness.log.with_suffix(".native-identity.log").write_bytes(
                original + b"extra\n"
            )
            with self.assertRaises(evidence.EvidenceError):
                evidence.verify_bundle(bundle.attempt / evidence.MANIFEST)

    def test_structural_pass_cannot_hide_identity_receipt_lineage_or_projection_tampering(
        self,
    ):
        for fault in (
            "missing-install",
            "owner-record",
            "hash",
            "raw-lineage",
            "projection",
            "payload-pid",
            "payload-url",
            "wrong-installed-candidate",
            "process-executable",
        ):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temporary:
                bundle, h = self.ready(Path(temporary))
                target = None
                if fault == "missing-install":
                    h.install.unlink()
                elif fault == "raw-lineage":
                    with h.log.with_suffix(".raw.log").open("ab") as stream:
                        stream.write(
                            (
                                physical.PREFIX + "generation=1:cleanup-started\n"
                            ).encode()
                        )
                elif fault == "projection":
                    (h.coordination / "runtime.log").write_text("wrong\n")
                elif fault == "wrong-installed-candidate":
                    target = h.install
                    value = physical.read_document(target)
                    value["info"]["arguments"][-1] = "/wrong.app"
                elif fault in ("payload-pid", "payload-url"):
                    target = h.log.with_suffix(".reload-payload.json")
                    value = physical.read_document(target)
                    if fault == "payload-pid":
                        value["result"]["process"]["processIdentifier"] = 43
                    else:
                        args = value["info"]["arguments"]
                        args[args.index("--payload-url") + 1] += "wrong"
                elif fault == "process-executable":
                    target = h.log.with_suffix(".arm-2-after-processes.json")
                    value = physical.read_document(target)
                    value["result"]["runningProcesses"][0]["executable"] += "wrong"
                else:
                    target = h.log.with_suffix(".identity.json")
                    value = physical.read_document(target)
                    if fault == "hash":
                        value["projection_sha256"]["pending"] = "ab" * 32
                    else:
                        value["owners"]["2"] = OWNERS[0]
                if target is not None:
                    physical.write_document(target, value)
                with self.assertRaises(evidence.EvidenceError):
                    self.finish(bundle, h)
                result = physical.read_document(bundle.attempt / evidence.MANIFEST)
                self.assertEqual(result["status"], "failed")
                self.assertTrue(result["evidence_errors"])


class IosPendingRunnerTests(unittest.TestCase):
    def install(self, env, argv, *, timeout, check, failure=None):
        receipt = Path(argv[8])
        snapshot = Path(argv[9])
        self.assertEqual(
            argv,
            [
                "xcrun",
                "devicectl",
                "device",
                "install",
                "app",
                "--device",
                DEVICE,
                "--json-output",
                str(receipt.parent / "device-install.json"),
                str(receipt.parent / "candidate" / "BleHubNitroHost.app"),
            ],
        )
        self.assertEqual(timeout, 120)
        self.assertIs(check, True)
        self.assertTrue(snapshot.is_dir())
        self.assertEqual(env.pid_file.read_text(), "1234 1234\n")
        env.events.append("ios-install")
        if failure is not None:
            raise failure
        value = document(
            "devicectl.device.install.app",
            {
                "installedApplications": [
                    {"bundleID": BUNDLE_ID, "installationURL": IOS_URL}
                ]
            },
        )
        value["info"]["arguments"] = argv[1:]
        physical.write_document(receipt, value)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def test_frozen_app_live_fixture_single_install_native_capture_and_cleanup_bundle(
        self,
    ):
        with (
            tempfile.TemporaryDirectory() as temporary,
            mocked_run(Path(temporary)) as env,
        ):
            root = Path(temporary)
            app = make_app(root)
            captures = []
            order = Mock()
            order.attach_mock(env.receipt, "receipt")

            def command(argv, **kwargs):
                return self.install(env, argv, **kwargs)

            def capture(
                device,
                log,
                coordination,
                *,
                run_id,
                service_uuid,
                local_name,
                seconds,
                guard,
                install_json,
            ):
                env.events.append("ios-capture")
                guard()
                captures.append(
                    (
                        device,
                        log,
                        coordination,
                        run_id,
                        service_uuid,
                        local_name,
                        seconds,
                    )
                )
                receipt = install_json.read_bytes()
                h = Harness(log.parent, run_id=run_id)
                h.log = log
                h.service = service_uuid
                h.guard = guard
                install_json.write_bytes(receipt)
                text, actions = h.run()
                proof = ios_value(synthetic_physical(env.plans[-1], [], (1234, 1234)))
                proof["platform"] = "ios"
                physical.write_document(coordination / physical.PHYSICAL_NAME, proof)
                return text, actions

            with (
                patch.object(
                    runner.ios_sdk, "run_command", side_effect=command
                ) as install,
                patch(
                    "tools.react_native.ios_device.require_ready_unlocked_device"
                ) as ready,
                patch.object(api, "capture", side_effect=capture) as capture_mock,
            ):
                order.attach_mock(install, "install")
                order.attach_mock(capture_mock, "capture")
                manifest = runner.run(
                    "ios",
                    DEVICE,
                    app,
                    fixture_contract=env.inputs / "contract.json",
                    fixture_profile=env.inputs / "profile.json",
                    timeout=90,
                )
            ready.assert_called_once_with(DEVICE, cwd=root)
            snapshot = manifest.parent / "candidate" / app.name
            install.assert_called_once_with(
                [
                    "xcrun",
                    "devicectl",
                    "device",
                    "install",
                    "app",
                    "--device",
                    DEVICE,
                    "--json-output",
                    str(manifest.parent / "device-install.json"),
                    str(snapshot),
                ],
                timeout=120,
                check=True,
            )
            self.assertNotEqual(snapshot, app)
            self.assertEqual(
                (snapshot / SCHEME).read_bytes(), (app / SCHEME).read_bytes()
            )
            self.assertEqual(len(captures), 1)
            self.assertEqual(
                captures[0],
                (
                    DEVICE,
                    manifest.parent / "pending-read-reload-runtime.log",
                    manifest.parent / "coordination",
                    env.plans[-1].session_id,
                    env.plans[-1].service_uuid,
                    runner.load_session_profile(
                        env.plans[-1].contract, env.plans[-1].profile_path
                    ).local_name,
                    90,
                ),
            )
            self.assertEqual(
                [entry[0] for entry in order.mock_calls[:4]],
                ["receipt", "install", "receipt", "capture"],
            )
            self.assertEqual(
                env.receipt.call_args_list[0],
                call(env.plans[-1], fixture_identity=(1234, 1234)),
            )
            self.assertEqual(physical.read_document(manifest)["status"], "passed")
            self.assertEqual(
                env.events,
                [
                    "prepare",
                    "fixture-start",
                    "ios-install",
                    "ios-capture",
                    "fixture-stop",
                    "finish",
                ],
            )
            self.assertFalse(env.pid_file.exists())
            self.assertEqual(len(env.commands), 2)
            self.assertEqual(env.commands[-1][-4:], ["--pid", "1234", "--pgid", "1234"])
            env.capture.assert_not_called()
            evidence.verify_bundle(manifest)

    def test_install_or_capture_failure_stops_only_owned_fixture_and_keeps_failed_bundle(
        self,
    ):
        for stage in (
            "install",
            "post-install-receipt",
            "capture",
            "install-cleanup",
            "capture-cleanup",
        ):
            with (
                self.subTest(stage=stage),
                tempfile.TemporaryDirectory() as temporary,
                mocked_run(Path(temporary)) as env,
            ):
                root = Path(temporary)
                app = make_app(root)
                failure = runner.ios_sdk.IosSdkError("synthetic " + stage + " failure")
                if stage.endswith("-cleanup"):
                    env.faults.stop_error = OSError("synthetic fixture cleanup failure")

                def command(argv, *, stage=stage, failure=failure, **kwargs):
                    return self.install(
                        env,
                        argv,
                        failure=failure if stage.startswith("install") else None,
                        **kwargs,
                    )

                def verify_receipt(plan, *, failure=failure, **kwargs):
                    fixture_session.verify_receipt(plan, **kwargs)
                    if env.events[-1] == "ios-install":
                        raise failure

                if stage == "post-install-receipt":
                    env.receipt.side_effect = verify_receipt

                with (
                    patch.object(
                        runner.ios_sdk, "run_command", side_effect=command
                    ) as install,
                    patch(
                        "tools.react_native.ios_device.require_ready_unlocked_device"
                    ),
                    patch.object(api, "capture", side_effect=failure) as capture,
                    self.assertRaises(RuntimeError) as raised,
                ):
                    runner.run(
                        "ios",
                        DEVICE,
                        app,
                        fixture_contract=env.inputs / "contract.json",
                        fixture_profile=env.inputs / "profile.json",
                        timeout=90,
                    )
                self.assertIs(raised.exception, failure)
                install.assert_called_once()
                if stage.startswith("capture"):
                    capture.assert_called_once()
                    install_json = capture.call_args.kwargs["install_json"]
                    self.assertEqual(
                        physical.read_document(install_json)["info"]["arguments"],
                        install.call_args.args[0][1:],
                    )
                else:
                    capture.assert_not_called()
                env.capture.assert_not_called()
                self.assertEqual(env.events.count("fixture-stop"), 1)
                self.assertEqual(env.events[-2:], ["fixture-stop", "finish"])
                self.assertEqual(
                    env.commands[-1][-4:], ["--pid", "1234", "--pgid", "1234"]
                )
                self.assertIs(env.finalizer.call_args.kwargs["primary_error"], failure)
                self.assertFalse(env.finalizer.call_args.kwargs["completed"])
                if stage.endswith("-cleanup"):
                    self.assertIn(
                        "synthetic fixture cleanup failure", " ".join(failure.__notes__)
                    )
                else:
                    self.assertFalse(env.pid_file.exists())
                manifests = list(
                    root.glob(
                        "tests/consumers/react-native-internal/ios/build/*/evidence-bundle.json"
                    )
                )
                self.assertEqual(len(manifests), 1)
                manifest = physical.read_document(manifests[0])
                self.assertEqual(manifest["status"], "failed")
                self.assertEqual(manifest["primary_error"]["message"], str(failure))
                self.assertEqual(
                    (manifests[0].parent / "coordination/abort").read_text(),
                    "parent-failed\n",
                )


if __name__ == "__main__":
    unittest.main()
