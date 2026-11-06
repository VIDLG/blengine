"""Offline orchestration boundaries, not a full SDK/capture/provider integration.

Session preparation, receipt checks, phone ownership, journal/ledger parsing and
failed-bundle publication stay real. The synthetic capture only calls ready_guard
when its run actually reaches a window; failures never synthesize later callbacks.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import plistlib
import signal
import subprocess
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.peripherals.nrf52840 import fixture_session
from tests.peripherals.nrf52840.support.session_receipts import fake_attempt
from tools.react_native import run_ios_adapter_startup as runner
from tools.react_native.ios_radio_lease import IosRadioLease
from tools.tests.react_native.support.ios_adapter_startup_samples import (
    DEVICE,
    SERVICE,
    fake_app,
    journal_document,
    passed_control,
    put,
    session_identity,
    summary_document,
)


class OfflineRun:
    """One temporary attempt with injectable SDK/hardware failures and real owners."""

    def __init__(self, root: Path) -> None:
        self.root = root
        inputs = root / "inputs"
        inputs.mkdir()
        seed = fake_attempt(inputs, service=SERVICE)
        self.receipt_template = json.loads(seed.plan.receipt_path.read_bytes())
        source = root / "source"
        source.mkdir()
        self.app = fake_app(source)
        self.test_run = source / "input.xctestrun"
        put(
            self.test_run,
            plistlib.dumps(
                {
                    "BleHubNitroHostUITests": {
                        "IsUITestBundle": True,
                        "UITargetAppPath": str(self.app),
                        "DependentProductPaths": [str(self.app)],
                        "EnvironmentVariables": {},
                    }
                }
            ),
        )
        self.contract = inputs / "contract.json"
        self.profile = inputs / "profile.json"
        self.build = root / "attempts"
        self.events: list[str] = []
        self.commands: list[list[str]] = []
        self.plans: list[fixture_session.FixtureSession] = []
        self.phones: list[IosRadioLease] = []
        self.captures: list[SimpleNamespace] = []
        self.finish_primaries: list[BaseException | None] = []
        self.off: dict | None = None
        self.control: dict | None = None
        self.control_raw: bytes | None = None
        self.faults = SimpleNamespace(
            acquire=None,
            start=None,
            receipt_identity=None,
            install=None,
            capture_before=None,
            capture_after=RuntimeError("synthetic XCTest failed after window"),
            off_document=None,
            window_identity=None,
            control=None,
            control_update={},
            summary=None,
            journal=None,
            physical_finish=None,
            physical_errors=[],
        )

    @property
    def attempt(self) -> Path:
        """Return the single created runner attempt, never the template bundle."""
        attempts = list(self.build.iterdir())
        if len(attempts) != 1:
            raise AssertionError(f"expected one attempt, got {attempts}")
        return attempts[0]

    def run(self, device: str = DEVICE, **overrides: object) -> Path:
        """Pass the input identity unchanged; production owns its canonicalization."""
        arguments = {
            "app": self.app,
            "test_run": self.test_run,
            "fixture_contract": self.contract,
            "fixture_profile": self.profile,
            "authorize_radio": True,
        }
        arguments.update(overrides)
        return runner.run(device, **arguments)

    def prepare(
        self, *args: object, **kwargs: object
    ) -> fixture_session.FixtureSession:
        """Keep canonical run/session binding and immutable input snapshots real."""
        self.events.append("prepare")
        plan = fixture_session.prepare_session(*args, **kwargs)
        self.plans.append(plan)
        return plan

    def phone(self, *args: object) -> IosRadioLease:
        """Use the real lease only under the temporary test lock root."""
        phone = IosRadioLease(*args, lock_root=self.root / "phone-locks")
        acquire, finish = phone.acquire, phone.finish

        def acquire_owned() -> None:
            self.events.append("phone.acquire")
            if self.faults.acquire is not None:
                raise self.faults.acquire
            acquire()

        def finish_owned(control: dict | None, digest: str | None) -> list[str]:
            self.events.append("phone.finish")
            return finish(control, digest)

        phone.acquire, phone.finish = acquire_owned, finish_owned
        self.phones.append(phone)
        return phone

    def physical(
        self, plan: fixture_session.FixtureSession, _root: Path
    ) -> SimpleNamespace:
        """Replace physical transport, retaining real current-process receipt checks."""

        def start() -> None:
            self.events.append("physical.start")
            self.phones[0].require_owner()
            if self.faults.start is not None:
                raise self.faults.start
            identity = self.faults.receipt_identity or (os.getpid(), os.getpgid(0))
            put(
                plan.receipt_path,
                {
                    **self.receipt_template,
                    "session_id": plan.session_id,
                    "fixture_pid": identity[0],
                    "fixture_pgid": identity[1],
                    "captured_at_utc": datetime.now(UTC).isoformat(),
                    "port": plan.port,
                    "resource_identity": plan.resource_identity,
                    "contract_sha256": plan.contract_sha256,
                    "profile_sha256": plan.profile_sha256,
                },
            )

        def guard() -> None:
            self.events.append("physical.guard")

        def open_on(off: dict) -> None:
            self.events.append("physical.open_on")
            self.off = copy.deepcopy(off)

        def complete(_runtime: dict) -> None:
            self.events.append("physical.complete")

        def finish(primary: BaseException | None) -> list[str]:
            self.events.append("physical.finish")
            self.finish_primaries.append(primary)
            if self.faults.physical_finish is not None:
                raise self.faults.physical_finish
            return list(self.faults.physical_errors)

        return SimpleNamespace(
            start=start, guard=guard, open_on=open_on, complete=complete, finish=finish
        )

    def capture(
        self,
        device: str,
        log: Path,
        run_id: str,
        service: str,
        local_name: str,
        receipt_hash: str,
        **kwargs: object,
    ) -> SimpleNamespace:
        """Model a single window only; no assertions about the real capture SDK path."""
        if (device, service) != (DEVICE, SERVICE) or not local_name:
            raise AssertionError("foreign capture device/profile")
        journal = journal_document(run_id)
        identity = session_identity(journal)
        control = passed_control(identity, receipt_hash)

        def publish_control(value: dict) -> None:
            value = copy.deepcopy(value)
            value.update(copy.deepcopy(self.faults.control_update))
            self.control = value
            self.control_raw = json.dumps(value, indent=3).encode() + b"\n"

        # A window/guard failure cannot inherit the template's later ON proof.
        publish_control(
            {
                **control,
                "status": "failed",
                "restored": False,
                "same_session": False,
                "primary_error": "synthetic UI has no ON restoration proof",
                "observations": [],
            }
        )
        capture = SimpleNamespace(
            run_called=False,
            window_identity=self.faults.window_identity or identity,
            result=log.with_suffix(".xcresult"),
            arguments=kwargs,
        )

        def run() -> None:
            self.events.append("capture.run")
            capture.run_called = True
            capture.result.mkdir()
            kwargs["guard"]()
            if self.faults.capture_before is not None:
                raise self.faults.capture_before
            kwargs["ready_guard"]()
            self.events.append("capture.ready_returned")
            publish_control(control)
            self.events.append("capture.ui_control_completed")
            if self.faults.capture_after is not None:
                raise self.faults.capture_after

        def snapshot_off_journal() -> Path:
            self.events.append("capture.snapshot_off_journal")
            document = {**journal, "records": journal["records"][:8]}
            if self.faults.off_document is not None:
                document = self.faults.off_document
            path = log.parent / runner.evidence.names()["off_journal"]
            put(path, document)
            return path

        def download_journal() -> Path:
            self.events.append("capture.download_journal")
            if self.faults.journal is not None:
                raise self.faults.journal
            path = log.parent / runner.evidence.names()["journal"]
            put(path, journal)
            return path

        capture.run = run
        capture.snapshot_off_journal = snapshot_off_journal
        capture.download_journal = download_journal
        self.captures.append(capture)
        return capture

    def sdk(self, command: list[str], _log: Path, **_kwargs: object) -> None:
        """Permit exactly the install boundary; never execute an SDK command."""
        self.events.append("sdk.install")
        self.commands.append(command)
        self.phones[0].require_owner()
        if command[:6] != [
            "xcrun",
            "devicectl",
            "device",
            "install",
            "app",
            "--device",
        ]:
            raise AssertionError(f"unexpected SDK command: {command}")
        if self.faults.install is not None:
            raise self.faults.install
        put(Path(command[command.index("--json-output") + 1]), {"synthetic": True})

    def collect_control(
        self, _result: Path, attempt: Path, _device: str
    ) -> tuple[dict, str]:
        """Return raw synthetic control without manufacturing successful callbacks."""
        self.events.append("collect.control")
        if self.faults.control is not None:
            raise self.faults.control
        if self.control_raw is None:
            raise AssertionError("capture control not initialized")
        put(attempt / runner.evidence.names()["control"], self.control_raw)
        return json.loads(self.control_raw), hashlib.sha256(
            self.control_raw
        ).hexdigest()

    def collect_summary(self, _result: Path, attempt: Path) -> None:
        """Publish read-only synthetic summary independently of control collection."""
        self.events.append("collect.summary")
        if self.faults.summary is not None:
            raise self.faults.summary
        put(attempt / runner.evidence.names()["summary"], summary_document())


@contextmanager
def offline_run(root: Path) -> Iterator[OfflineRun]:
    """Block every process/USB escape before constructing synthetic input fixtures."""
    previous = signal.getsignal(signal.SIGTERM)
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(
                subprocess,
                "run",
                side_effect=AssertionError("real subprocess forbidden"),
            )
        )
        stack.enter_context(
            patch.object(
                subprocess, "Popen", side_effect=AssertionError("real child forbidden")
            )
        )
        stack.enter_context(
            patch.object(
                fixture_session, "identity_for_port", return_value="jlink:123456"
            )
        )
        case = OfflineRun(root)
        stack.enter_context(patch.object(runner, "ROOT", root))
        stack.enter_context(patch.object(runner, "BUILD", case.build))
        stack.enter_context(
            patch.object(runner.provider, "PID_FILE", root / "provider.pid")
        )
        stack.enter_context(
            patch.object(runner.provider, "_port", return_value="/dev/fake-only")
        )
        stack.enter_context(
            patch.object(
                runner, "run_handshake", return_value=root / "handshake.capture.json"
            )
        )
        stack.enter_context(
            patch.object(runner, "prepare_session", side_effect=case.prepare)
        )
        stack.enter_context(
            patch.object(runner, "IosRadioLease", side_effect=case.phone)
        )
        stack.enter_context(
            patch.object(runner, "IosAdapterStartup", side_effect=case.physical)
        )
        stack.enter_context(
            patch.object(runner, "StartupWindowCapture", side_effect=case.capture)
        )
        stack.enter_context(patch.object(runner, "_sdk", side_effect=case.sdk))
        stack.enter_context(
            patch.object(runner, "collect_control", side_effect=case.collect_control)
        )
        stack.enter_context(
            patch.object(runner, "collect_summary", side_effect=case.collect_summary)
        )

        def ready(_device: str, **_kwargs: object) -> None:
            case.events.append("sdk.device_ready")
            case.phones[0].require_owner()

        stack.enter_context(
            patch.object(runner, "require_ready_unlocked_device", side_effect=ready)
        )
        try:
            yield case
        finally:
            # Test hygiene only, after assertions have inspected production restore.
            signal.signal(signal.SIGTERM, previous)
