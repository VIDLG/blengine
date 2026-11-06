"""Scoped Android pending-reload runner mocks retaining real receipt validation."""

from __future__ import annotations

import io
import subprocess
import zipfile
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.peripherals.nrf52840 import fixture_session
from tests.peripherals.nrf52840.cancellation_control import read_document
from tests.peripherals.nrf52840.support.session_receipts import fake_attempt
from tools.react_native import android_pending_reload_capture, android_rf_host
from tools.react_native import pending_read_reload_runner as runner
from tools.react_native import rf_evidence_bundle as evidence
from tools.tests.react_native.support.pending_reload_ledger import (
    ACTIONS,
    synthetic_physical,
    synthetic_runtime,
)

IDENTITY = (1234, 1234)


@contextmanager
def mocked_run(root: Path) -> Iterator[SimpleNamespace]:
    """Keep actual immutable input/receipt/bundle validation behind fake hardware."""
    inputs = root / "inputs"
    inputs.mkdir()
    seed = fake_attempt(inputs)
    receipt_template = read_document(seed.plan.receipt_path)
    apk = root / "source.apk"
    with zipfile.ZipFile(apk, "w") as archive:
        for name in (
            "assets/index.android.bundle",
            "classes.dex",
            "lib/arm64-v8a/libblehub_uniffi.so",
            "lib/arm64-v8a/libBleHubReactNative.so",
        ):
            archive.writestr(name, b"synthetic controller input; not executable/RF")
    commands: list[list[str]] = []
    events: list[str] = []
    plans: list[fixture_session.FixtureSession] = []
    faults = SimpleNamespace(
        start_error=None,
        start_stdout="nrf52840_fixture_pid=1234\nnrf52840_fixture_pgid=1234\n",
        foreign_on_start=False,
        install_error=None,
        launch_stdout="Starting: Intent",
        native_error=None,
        stop_error=None,
        permission_action=None,
    )
    prepare = runner.prepare_session
    finish = evidence.EvidenceBundle.finish
    with ExitStack() as stack:
        stack.enter_context(redirect_stdout(io.StringIO()))
        stack.enter_context(patch.object(runner, "PROJECT_ROOT", root))
        pid_file = root / "fixture.pid"
        stack.enter_context(patch.object(runner.provider, "PID_FILE", pid_file))
        stack.enter_context(
            patch.object(runner.provider, "_port", return_value="/dev/synthetic-btp")
        )
        stack.enter_context(
            patch.object(
                fixture_session, "identity_for_port", return_value="jlink:123456"
            )
        )
        identity = stack.enter_context(
            patch.object(runner.provider, "_process_identity", return_value=IDENTITY)
        )
        state = stack.enter_context(
            patch.object(
                runner.provider,
                "_fixture_state",
                return_value=runner.provider._PROCESS_RUNNING,
            )
        )
        stack.enter_context(
            patch.object(android_rf_host, "sdk_root", return_value=root / "sdk")
        )
        previous_handler = object()
        stack.enter_context(
            patch.object(runner.signal, "getsignal", return_value=previous_handler)
        )
        handlers = stack.enter_context(patch.object(runner.signal, "signal"))

        def prepare_plan(
            *args: object, **kwargs: object
        ) -> fixture_session.FixtureSession:
            plan = prepare(*args, **kwargs)
            plans.append(plan)
            events.append("prepare")
            return plan

        preparation = stack.enter_context(
            patch.object(runner, "prepare_session", side_effect=prepare_plan)
        )
        receipt = stack.enter_context(
            patch.object(runner, "verify_receipt", wraps=runner.verify_receipt)
        )

        def command(
            argv: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            commands.append(argv)
            stdout = ""
            if "badging" in argv:
                events.append("badging")
                stdout = (
                    f"package: name='{runner.ANDROID_PACKAGE}' versionCode='1'\n"
                    f"launchable-activity: name='{runner.ANDROID_ACTIVITY}'\n"
                )
            elif "direct-start" in argv:
                events.append("fixture-start")
                plan = fixture_session.load_session(
                    Path(argv[argv.index("--fixture-request") + 1])
                )
                runner.write_document(
                    plan.receipt_path,
                    {
                        **receipt_template,
                        "session_id": plan.session_id,
                        "captured_at_utc": datetime.now(UTC).isoformat(),
                        "fixture_pid": IDENTITY[0],
                        "fixture_pgid": IDENTITY[1],
                        "port": plan.port,
                        "resource_identity": plan.resource_identity,
                        "contract_sha256": plan.contract_sha256,
                        "profile_sha256": plan.profile_sha256,
                    },
                )
                pid_file.write_text("1234 1234\n", encoding="utf-8")
                if faults.foreign_on_start:
                    pid_file.write_text("9999 9999\n", encoding="utf-8")
                    identity.return_value = (9999, 9999)
                if faults.start_error is not None:
                    raise faults.start_error
                stdout = faults.start_stdout
            elif "stop-owned" in argv:
                events.append("fixture-stop")
                if faults.stop_error is not None:
                    raise faults.stop_error
                pid_file.unlink()
            elif "tools.android.install_apk" in argv:
                events.append("install")
                Path(argv[argv.index("--log") + 1]).write_text(
                    "synthetic install receipt\n", encoding="utf-8"
                )
                if faults.install_error is not None:
                    raise faults.install_error
            elif "start" in argv:
                events.append("launch")
                stdout = faults.launch_stdout
            else:
                events.append("adb")
            return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

        command_mock = stack.enter_context(
            patch.object(runner.android_sdk, "run_command", side_effect=command)
        )

        def permissions(_serial: str) -> None:
            events.append("permissions")
            if faults.permission_action is not None:
                faults.permission_action()

        stack.enter_context(
            patch.object(
                runner.android_sdk,
                "grant_bluetooth_permissions",
                side_effect=permissions,
            )
        )

        def capture(
            _serial: str,
            log: Path,
            coordination: Path,
            *,
            run_id: str,
            seconds: float,
            guard: Callable[[], None],
        ) -> tuple[str, tuple[str, ...]]:
            del seconds
            events.append("capture")
            guard()
            if faults.native_error is not None:
                raise faults.native_error
            plan = plans[-1]
            lines = synthetic_runtime(run_id)
            text = "\n".join(lines) + "\n"
            log.write_text(text, encoding="utf-8")
            # These are synthetic capture/physical documents, never a real RF pass.
            runner.write_document(
                log.with_suffix(".identity.json"), {"synthetic": True}
            )
            runner.write_document(log.with_suffix(".capture.json"), {"synthetic": True})
            runner.write_document(
                coordination / runner.PHYSICAL_NAME,
                synthetic_physical(plan, lines, IDENTITY),
            )
            return text, ACTIONS

        capture_mock = stack.enter_context(
            patch.object(android_pending_reload_capture, "capture", side_effect=capture)
        )

        def finalization(bundle: evidence.EvidenceBundle, **kwargs: object) -> None:
            events.append("finish")
            finish(bundle, **kwargs)

        finalizer = stack.enter_context(
            patch.object(
                evidence.EvidenceBundle,
                "finish",
                autospec=True,
                side_effect=finalization,
            )
        )

        def run() -> Path:
            return runner.run(
                "android",
                "synthetic-phone",
                apk,
                fixture_contract=inputs / "contract.json",
                fixture_profile=inputs / "profile.json",
                timeout=2,
            )

        yield SimpleNamespace(
            run=run,
            apk=apk,
            inputs=inputs,
            seed=seed,
            commands=commands,
            events=events,
            plans=plans,
            faults=faults,
            identity=identity,
            state=state,
            preparation=preparation,
            receipt=receipt,
            command=command_mock,
            capture=capture_mock,
            finalizer=finalizer,
            pid_file=pid_file,
            handlers=handlers,
            previous_handler=previous_handler,
        )
