"""Synthetic host/controller/capture peers; never spawn ADB or authorize RF."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.peripherals.nrf52840 import permission_transition as wire
from tests.peripherals.nrf52840.cancellation_control import write_document
from tests.peripherals.nrf52840.fixture_session import _json
from tests.peripherals.nrf52840.support import (
    permission_provider_samples as provider_samples,
)
from tests.support.rf_lab.markers import SERVICE
from tools.android import permission_control as pc
from tools.react_native import capture_transport
from tools.react_native import permission_transition_capture as capture_api
from tools.react_native import permission_transition_ledger as ledger
from tools.tests.react_native.support import permission_os_samples as os_samples

RUN = provider_samples.RUN
NAME = provider_samples.NAME
DEVICE = "synthetic-permission-phone"
URI = "file:/data/app/synthetic/base.apk"


def bootstrap(phase: str) -> dict:
    value = provider_samples.bootstrap(RUN, phase)
    index = wire.PHASES.index(phase)
    value.update(
        pid=os_samples.PID - 1 + index,
        uid=os_samples.UID,
        executable=URI,
        started_unix_ms=os_samples.NOW_MS - 2000 + index * 1000,
    )
    return value


def bodies(phase: str) -> list[str]:
    if phase == "denied":
        lines = provider_samples.denied()
    elif phase == "active":
        lines = provider_samples.baseline() + provider_samples.setup()
    else:
        lines = provider_samples.restored()
    lines[0] = "native:bootstrap:" + json.dumps(bootstrap(phase))
    return lines


def pending() -> list[str]:
    lines = provider_samples.pending()
    lines[0] = "native:signal:" + json.dumps(
        {**bootstrap("active"), "source": "payload-url", "url": wire.arm_url(RUN)}
    )
    return lines


class FakePermissionOS:
    """Only the controller's explicit adb argv; unknown calls never hit a device."""

    def __init__(self) -> None:
        self.now = 100.0
        self.scan = self.connect = True
        self.alive = False
        self.kill_on_revoke = True
        self.bad_record: dict[str, object] = {}
        self.records: list[str] = []
        self.events: list[object] = []

    def guard(self) -> None:
        self.events.append("guard")

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def run(
        self, command: list[str], *, timeout: float, check: bool
    ) -> subprocess.CompletedProcess[str]:
        assert 0 < timeout <= 5 and not check and self.events[-1] == "guard"
        self.events.append(command)
        self.now += 0.02
        args = command[4:]
        for verb in ("grant", "revoke"):
            for permission in pc.PERMISSIONS:
                if tuple(args) == pc.permission_mutation_arguments(
                    verb, permission, use_root=True
                ):
                    args = ["pm", verb, "--user", "0", pc.PACKAGE, permission]
        code = 0
        if tuple(args) == pc.ROOT_ID_ARGS:
            output = "uid=0(root) gid=0(root)\n"
        elif args == ["getprop", "ro.build.version.sdk"]:
            output = "36\n"
        elif args == ["am", "get-current-user"]:
            output = "0\n"
        elif args == ["dumpsys", "package", pc.PACKAGE]:
            output = os_samples.permission_dump(self.scan, self.connect)
        elif args == [
            "cmd",
            "package",
            "list",
            "packages",
            "-U",
            "--user",
            "0",
            pc.PACKAGE,
        ]:
            output = f"package:{pc.PACKAGE} uid:{os_samples.UID}\n"
        elif args == ["date", "+%s%3N,%z"]:
            output = f"{os_samples.NOW_MS + round((self.now - 100) * 1000)},+0000\n"
        elif args == ["dumpsys", "activity", "exit-info", pc.PACKAGE]:
            output = os_samples.exit_dump(*self.records)
        elif args == ["cat", f"/proc/{os_samples.PID}/stat"]:
            if self.alive:
                output = os_samples.proc_stat()
            else:
                code, output = (
                    1,
                    f"cat: /proc/{os_samples.PID}/stat: No such file or directory\n",
                )
        elif args == ["cat", f"/proc/{os_samples.PID}/status"]:
            output = "Uid:\t" + "\t".join([str(os_samples.UID)] * 4) + "\n"
        elif args == ["cat", f"/proc/{os_samples.PID}/cmdline"]:
            output = pc.PACKAGE + "\0"
        elif len(args) == 6 and args[0] == "pm":
            verb, flag, user, package, permission = args[1:]
            assert (flag, user, package) == ("--user", "0", pc.PACKAGE)
            assert verb in ("grant", "revoke") and permission in pc.PERMISSIONS
            self.scan = self.connect = verb == "grant"  # Simulate ROM group coupling.
            if verb == "revoke" and self.alive and self.kill_on_revoke:
                self.alive = False
                unix_ms = os_samples.NOW_MS + round((self.now - 100) * 1000)
                timestamp = datetime.fromtimestamp(unix_ms / 1000, UTC).strftime(
                    "%Y-%m-%d %H:%M:%S.%f"
                )[:-3]
                self.records.insert(
                    0,
                    os_samples.exit_record(
                        **dict(timestamp=timestamp, **self.bad_record)
                    ),
                )
            output = ""
        else:
            raise AssertionError(f"no real controller transport fallthrough: {command}")
        return subprocess.CompletedProcess(command, code, output)


class Child:
    def __init__(self, streaming: bool = False) -> None:
        self.pid = 9876
        self.returncode = None if streaming else 0

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0

    kill = terminate

    def wait(self, timeout: float) -> int:
        return self.returncode or 0


class Harness:
    """Real capture/RuntimeFacts/controller with fake SDK children, OS and clock."""

    def __init__(self, root: Path, stack: ExitStack, *, use_root: bool = False) -> None:
        spec = importlib.util.spec_from_file_location(
            "synthetic_permission_capture", capture_api.__file__
        )
        assert spec is not None and spec.loader is not None
        self.api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.api)
        self.phone = FakePermissionOS()
        self.phone.alive = False
        self.tasks: list[tuple[float, object]] = []
        self.clock = SimpleNamespace(monotonic=lambda: self.phone.now, sleep=self.sleep)
        self.api.time = self.clock
        stack.enter_context(patch.object(capture_transport, "time", self.clock))
        stack.enter_context(patch.object(pc, "time", self.clock))
        stack.enter_context(
            patch.object(pc, "adb_executable", return_value=Path("/sdk/adb"))
        )
        stack.enter_context(
            patch.object(pc.android_sdk, "run_command", side_effect=self.phone.run)
        )
        self.events: list[str] = []
        self.commands: list[list[str]] = []
        self.children: list[Child] = []
        self.phase = None
        self.pid = None
        self.writer = None
        self.marker_pid: int | None = None
        self.stream_child = None
        self.fault: str | None = None
        self.arm_calls = 0
        self.phone_owned = True
        self.root = root / "coordination"
        self.root.mkdir()
        session = self.root / "fixture-session"
        session.mkdir()
        self.receipt = session / "fixture-receipt.json"
        self.receipt.write_bytes(b'{"synthetic": "original bytes"}\n')
        self.log = root / wire.RUNTIME_NAME
        self.permission = pc.AndroidPermissionController(
            DEVICE,
            RUN,
            root / "permission-state-control.json",
            phone_guard=self.guard,
            use_root=use_root,
        )
        self.permission.initial()
        self.api.android_sdk = SimpleNamespace(
            adb_command=lambda d, *a: ["/sdk/adb", "-s", d, *a],
            run_command=self.absence,
        )
        self.api.wait_for_initial_pid = self.wait_pid
        stack.enter_context(
            patch.object(capture_transport.subprocess, "Popen", side_effect=self.popen)
        )
        self.capture = self.api.PermissionCapture(
            DEVICE,
            self.log,
            self.root,
            RUN,
            SERVICE,
            NAME,
            timeout=25,
            guard=Mock(),
            permission=self.permission,
            phone_guard=self.guard,
        )
        original = self.capture._poll

        def poll() -> None:
            original()
            if not self.capture.cleaning:
                self.provider_step()

        self.capture._poll = poll

    def guard(self) -> None:
        if not self.phone_owned:
            raise RuntimeError("phone owner lost")
        self.phone.guard()

    def sleep(self, seconds: float) -> None:
        self.phone.sleep(seconds)
        ready = [task for task in self.tasks if task[0] <= self.phone.now]
        self.tasks = [task for task in self.tasks if task[0] > self.phone.now]
        for _, action in ready:
            action()

    def wait_pid(self, device: str, log: Path, timeout: float, *, guard: object) -> str:
        guard()
        write_document(
            log.with_suffix(".identity.json"),
            {"schema_version": 1, "pid": str(self.pid), "status": "pinned"},
        )
        return str(self.pid)

    def absence(
        self, command: list[str], *, timeout: float, check: bool
    ) -> subprocess.CompletedProcess[str]:
        assert command[4:] == ["pidof", pc.PACKAGE] and not check and 0 < timeout <= 1
        self.commands.append(command)
        return subprocess.CompletedProcess(
            command,
            1 if self.pid is None else 0,
            "" if self.pid is None else str(self.pid),
        )

    def append(self, lines: list[str], *, pid: int | None = None) -> None:
        for line in lines:
            self.writer.write(
                f"10-17 12:00:00.000 {pid or self.marker_pid or self.pid} 5678 I Test: {wire.PREFIX}{line}\n".encode()
            )
        self.writer.flush()

    def popen(self, command: list[str], **kwargs: object) -> Child:
        self.commands.append(command)
        output = kwargs["stdout"]
        if "logcat" in command:
            self.writer = output
            child = Child(True)
            self.stream_child = child
        else:
            child = Child()
            args = command[4:]
            text = ""
            if args == ["pm", "path", pc.PACKAGE]:
                text = "package:/data/app/synthetic/base.apk\n"
            elif args == ["pidof", pc.PACKAGE]:
                text = str(self.pid)
            elif args == ["cat", f"/proc/{self.pid}/stat"]:
                text = os_samples.proc_stat(pid=self.pid)
            elif args == ["cat", f"/proc/{self.pid}/status"]:
                uid = os_samples.UID + (1 if self.fault == "uid" else 0)
                text = f"Uid:\t{uid}\t{uid}\t{uid}\t{uid}\n"
            elif args == ["cat", f"/proc/{self.pid}/cmdline"]:
                text = pc.PACKAGE + "\0"
            elif "rf-permission-transition" in args:
                phase = args[args.index("blehub.rfPermissionPhase") + 1]
                self.phase, self.pid = phase, bootstrap(phase)["pid"]
                self.phone.alive = True
                self.events.append("launch-" + phase)
                self.append(bodies(phase))
                text = "Starting: Intent\n"
            elif "android.intent.action.VIEW" in args:
                self.events.append("arm")
                self.arm_calls += 1
                lines = pending()
                if self.fault == "js-first":
                    lines[0], lines[1] = lines[1], lines[0]
                if self.fault == "late-terminal":
                    lines.append(
                        'active:event:{"kind":"commandCompleted","commandId":"52008"}'
                    )
                self.append(lines)
                text = "Starting: Intent\n" + self.api.DELIVERED_WARNING + "\n"
            elif args == ["am", "force-stop", pc.PACKAGE]:
                self.events.append("retire-" + str(self.phase))
                self.pid = None
                self.phone.alive = False
            else:
                raise AssertionError(f"no real SDK fallthrough: {command}")
            output.write(text.encode())
            output.flush()
        self.children.append(child)
        return child

    def provider_document(self, name: str, phase: str, **extra: object) -> None:
        from tests.peripherals.nrf52840.fault_control import (
            Operation,
            Phase,
            Ticket,
            Winner,
            owner_nonce,
            status_document,
            ticket_document,
        )
        from tests.peripherals.nrf52840.support.read_fault_emulator import read_receipt

        ticket = Ticket(
            bytes(range(1, 17)), owner_nonce(RUN), 7, 2, 2, 1, Operation.READ, 51
        )
        receipt_phase = (
            Phase.ARMED
            if phase == "armed"
            else Phase.RETIRED
            if phase == "restored"
            else Phase.HELD
        )
        status = status_document(
            read_receipt(
                ticket,
                receipt_phase,
                Winner.DISCONNECT if phase == "restored" else Winner.NONE,
            )
        )
        value = {
            "schema_version": 1,
            "run_id": RUN,
            "phase": phase,
            "receipt_sha256": self.capture.receipt_sha256,
            "native_bootstrap": bootstrap("active"),
            "route": dict(provider_samples.ROUTE),
            "hold": ticket_document(ticket),
            "fault_status": status,
            "peer": ["112233445566", 1],
            **extra,
        }
        write_document(self.root / name, value)

    def provider_step(self) -> None:
        facts = self.capture.facts
        if facts.route and not (self.root / wire.ARM_READY_NAME).exists():
            self.provider_document(wire.ARM_READY_NAME, "armed")
        if facts.pending and not (self.root / wire.REVOKE_READY_NAME).exists():
            self.provider_document(wire.REVOKE_READY_NAME, "waiting")
        request = self.root / wire.REVOKE_REQUEST_NAME
        if request.exists() and not (self.root / wire.REVOKE_AUTHORIZED_NAME).exists():
            value = json.loads(request.read_bytes())
            self.provider_document(
                wire.REVOKE_AUTHORIZED_NAME,
                "revoke-authorized",
                request_sha256=ledger.digest(request.read_bytes()),
                ready_sha256=value["ready_sha256"],
                request_nonce=value["request_nonce"],
            )
        if facts.loss_confirmed and not (self.root / wire.RESTORED_READY_NAME).exists():
            self.pid = None  # OS controller proved death; no host kill.
            self.provider_document(
                wire.RESTORED_READY_NAME,
                "restored",
                hold_inactive=True,
                peer_count=0,
                canonical_value_hex="00",
            )
        if facts.passed:
            write_document(
                self.root / wire.PHYSICAL_NAME, {"status": "passed", "synthetic": True}
            )

    def physical(self, runtime: dict) -> tuple[dict, dict, dict]:
        """Provider-format synthetic three GAP pairs, never physical evidence."""
        request = {
            "schema_version": 1,
            "session_id": RUN,
            "contract_sha256": "a" * 64,
            "profile_sha256": "b" * 64,
            "resource_identity": "synthetic:fixture",
            "service_uuid": SERVICE,
        }
        receipt = dict(
            request,
            proof_method="btp-vendor-live-under-device-lease",
            fixture_pid=12345,
            fixture_pgid=12345,
        )
        protocol = {name: _json(raw) for name, raw in self.capture.documents.items()}
        peer = protocol[wire.ARM_READY_NAME]["peer"]
        payload = bytes([peer[1]]) + bytes.fromhex(peer[0])[::-1]
        events = [
            {
                "kind": "disconnected" if i % 2 else "connected",
                "peer": peer,
                "payload_hex": (payload + (b"" if i % 2 else bytes(6))).hex(),
                "monotonic": t,
            }
            for i, t in enumerate((1.0, 2.0, 3.0, 3.4, 4.0, 5.0))
        ]
        hold = protocol[wire.ARM_READY_NAME]["hold"]

        def boundary(name: str, at: float, **fields: object) -> dict:
            return {"boundary": name, "cycle": -1, "monotonic": at, **fields}

        def gap(index: int) -> dict:
            return boundary(
                "gap-" + events[index]["kind"],
                events[index]["monotonic"],
                **{k: v for k, v in events[index].items() if k != "monotonic"},
            )

        physical = dict(
            request,
            mode=wire.MODE,
            run_id=RUN,
            status="passed",
            phase="closed",
            fixture_pid=12345,
            fixture_pgid=12345,
            receipt_sha256=self.capture.receipt_sha256,
            connections=3,
            disconnections=3,
            primary_error=None,
            cleanup_errors=[],
            rejected_events=[],
            aliveProbes=["denied", "baseline", "restored"],
            native_bootstraps=runtime["native_bootstraps"],
            runtime_nonces=runtime["runtime_nonces"],
            resources=runtime["resources"],
            cleanup={
                "advertising_stopped": True,
                "hold_inactive": True,
                "peer_count": 0,
            },
            protocol=protocol,
            hold_status=protocol[wire.RESTORED_READY_NAME]["fault_status"],
            handles={"read-write": hold["handle"]},
            events=events,
            boundaries=[
                gap(0),
                gap(1),
                gap(2),
                boundary(
                    "armed",
                    3.1,
                    ticket=hold,
                    status=protocol[wire.ARM_READY_NAME]["fault_status"],
                ),
                boundary(
                    "waiting",
                    3.2,
                    ticket=hold,
                    status=protocol[wire.REVOKE_READY_NAME]["fault_status"],
                ),
                boundary(
                    "revoke-authorized",
                    3.3,
                    ticket=hold,
                    status=protocol[wire.REVOKE_AUTHORIZED_NAME]["fault_status"],
                ),
                gap(3),
                boundary(
                    "permission-loss-confirmed",
                    3.5,
                    pid=bootstrap("active")["pid"],
                    reason=8,
                ),
                boundary(
                    "retirement-observed",
                    3.6,
                    ticket=hold,
                    status=protocol[wire.RESTORED_READY_NAME]["fault_status"],
                ),
                gap(4),
                gap(5),
                boundary("physical-closed", 5.1),
            ],
        )
        return request, receipt, physical

    def verify(self, text: str | None = None) -> dict:
        result = self.capture.result
        return ledger.verify_runtime(
            self.capture.text() if text is None else text,
            RUN,
            SERVICE,
            self.log.name,
            capture_result=result,
            documents=self.capture.documents,
            receipt_sha256=self.capture.receipt_sha256,
            death_proof=(self.log.parent / result["os_death_proof"]).read_bytes(),
            os_raw=(self.log.parent / result["os_raw_proof"]).read_bytes(),
            local_name=NAME,
        )
