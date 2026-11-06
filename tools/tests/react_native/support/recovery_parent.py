"""Private-module parent harness with synthetic hardware and capture boundaries."""

from __future__ import annotations

import argparse
import importlib.util
import json
import plistlib
import signal
import subprocess
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import FunctionType, SimpleNamespace
from unittest.mock import Mock, create_autospec

from tests.peripherals.nrf52840 import fixture_session, provider
from tools.react_native import (
    android_rf_host,
    recovery_ten_capture,
    recovery_ten_ledger,
    rf_fixture,
)
from tools.react_native import rf_evidence_bundle as evidence
from tools.react_native import rf_scenario_runner as parent
from tools.react_native import run_rf_recovery_ten as cli
from tools.react_native.host_contract import (
    ANDROID_ACTIVITY,
    ANDROID_PACKAGE,
    BUNDLE_ID,
)
from tools.tests.react_native.support.evidence_runtime import SESSION, recovery_runtime

IDENTITY = (1234, 1234)

FOREIGN = (9999, 9999)

CYCLES = tuple(range(10))

ROOT = Path(parent.__file__).resolve().parents[2]


def local_function(function: FunctionType, **globals_: object) -> FunctionType:
    """Run canonical helper code with private hardware globals, not global patches."""
    local = FunctionType(
        function.__code__,
        {**function.__globals__, **globals_},
        function.__name__,
        function.__defaults__,
        function.__closure__,
    )
    local.__kwdefaults__ = function.__kwdefaults__
    return local


class Harness:
    """Actual parent flow with synthetic startup, native capture and checker results."""

    def __init__(self, root: Path, platform: str) -> None:
        self.root, self.platform = root, platform
        spec = importlib.util.spec_from_file_location(
            "synthetic_rf_scenario_runner", parent.__file__
        )
        assert spec is not None and spec.loader is not None
        self.api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.api)
        # Isolate historical parent mechanics; real entry refusal has separate tests.
        self.api.require_v2_coordination = Mock()
        self.api.PROJECT_ROOT = root
        self.api.print = Mock()
        cli_spec = importlib.util.spec_from_file_location(
            "synthetic_recovery_cli", cli.__file__
        )
        assert cli_spec is not None and cli_spec.loader is not None
        self.cli = importlib.util.module_from_spec(cli_spec)
        cli_spec.loader.exec_module(self.cli)
        self.cli.rf_scenario_runner = self.api
        self.cli.print = self.api.print
        self.events: list[str] = []
        self.commands: list[tuple[list[str], dict[str, object]]] = []
        self.attempt: Path | None = None
        self.bundle: evidence.EvidenceBundle | None = None
        self.snapshot: Path | None = None
        self.plan: fixture_session.FixtureSession | None = None
        self.captured_files: dict[Path, bytes] = {}
        self.finish_arguments: dict[str, object] | None = None
        self.faults = SimpleNamespace(
            constructor=None,
            freeze=None,
            prepare=None,
            start=None,
            install=None,
            capture=None,
            checker=None,
            stop=None,
            finish=None,
            start_stdout="nrf52840_fixture_pid=1234\nnrf52840_fixture_pgid=1234\n",
            foreign_on_start=False,
            after_freeze=None,
            before_verify=None,
            permissions=None,
            after_capture=None,
            after_checker=None,
            runtime=None,
        )
        from tests.peripherals.nrf52840.support.session_receipts import write_v2_inputs

        self.contract, self.profile = write_v2_inputs(root)
        self.artifact = self._artifact()
        self.previous_handler = object()
        self.handlers = Mock()
        self.api.signal = SimpleNamespace(
            SIGTERM=signal.SIGTERM,
            SIG_IGN=signal.SIG_IGN,
            signal=self.handlers,
            getsignal=Mock(return_value=self.previous_handler),
        )
        self.pid_file = root / "fixture.pid"
        self.identity = Mock(return_value=IDENTITY)
        self.state = Mock(return_value=provider._PROCESS_RUNNING)
        self.provider = SimpleNamespace(
            PID_FILE=self.pid_file,
            _port=Mock(return_value="/dev/synthetic-btp"),
            _process_identity=self.identity,
            _fixture_state=self.state,
            _PROCESS_RUNNING=provider._PROCESS_RUNNING,
            DEFAULT_READY_TIMEOUT=provider.DEFAULT_READY_TIMEOUT,
        )
        self.api.provider = self.provider
        self.api.guard_fixture = local_function(
            rf_fixture.guard_fixture, provider=self.provider
        )
        self.command = Mock(side_effect=self._command)
        self.permissions = Mock(side_effect=self._permissions)
        self.android = SimpleNamespace(
            run_command=self.command,
            adb_command=lambda device, *args: ["/sdk/adb", "-s", device, *args],
            grant_bluetooth_permissions=self.permissions,
        )
        self.api.android_sdk = self.android
        self.api.ios_sdk = self.android
        self.api.verify_install_identity = local_function(
            android_rf_host.verify_install_identity,
            android_sdk=self.android,
            sdk_root=lambda: root / "synthetic-sdk",
        )
        self._prepare = local_function(
            fixture_session.prepare_session,
            identity_for_port=lambda _port: "jlink:123456",
        )
        self.api.prepare_session = Mock(side_effect=self._prepare_session)
        self.receipt = Mock(side_effect=self._receipt)
        self.api.verify_receipt = self.receipt
        self.ready = create_autospec(
            parent.require_ready_unlocked_device, side_effect=self._ready
        )
        self.api.require_ready_unlocked_device = self.ready
        self.capture = create_autospec(
            recovery_ten_capture.capture, side_effect=self._capture
        )
        self.api.recovery_ten_capture = SimpleNamespace(capture=self.capture)
        self.checker = create_autospec(
            recovery_ten_ledger.verify, side_effect=self._checker
        )
        self.api.recovery_ten_ledger = SimpleNamespace(verify=self.checker)
        self._bind_bundle()

    def _artifact(self) -> Path:
        if self.platform == "android":
            artifact = self.root / "synthetic.apk"
            with zipfile.ZipFile(artifact, "w") as archive:
                for name in (
                    "assets/index.android.bundle",
                    "classes.dex",
                    "lib/arm64-v8a/libblehub_uniffi.so",
                    "lib/arm64-v8a/libBleHubReactNative.so",
                ):
                    archive.writestr(
                        name, b"synthetic parent input; not executable or RF"
                    )
        else:
            artifact = self.root / "Synthetic.app"
            artifact.mkdir()
            (artifact / "Info.plist").write_bytes(
                plistlib.dumps(
                    {
                        "CFBundleExecutable": "Synthetic",
                        "CFBundleIdentifier": BUNDLE_ID,
                    }
                )
            )
            (artifact / "Synthetic").write_bytes(
                bytes.fromhex("cffaedfe") + b"synthetic Mach-O"
            )
            (artifact / "Synthetic").chmod(0o755)
            (artifact / "main.jsbundle").write_bytes(b"synthetic JS; not RF")
            (artifact / "Resources").mkdir()
            (artifact / "Resources/opaque-resource").write_bytes(
                b"opaque resource\x00\xff"
            )
        return artifact

    def _bind_bundle(self) -> None:
        harness = self

        class Bundle(evidence.EvidenceBundle):
            def __init__(self, attempt: Path, **kwargs: object) -> None:
                harness.events.append("bundle")
                harness.attempt = attempt
                if harness.faults.constructor is not None:
                    raise harness.faults.constructor
                super().__init__(attempt, **kwargs)
                harness.bundle = self

            def prepare_artifact(self, source: Path) -> Path:
                harness.events.append("freeze")
                if harness.faults.freeze is not None:
                    raise harness.faults.freeze
                result = super().prepare_artifact(source)
                harness.snapshot = result
                if harness.faults.after_freeze is not None:
                    harness.faults.after_freeze()
                return result

            def verify_artifact(self) -> None:
                harness.events.append("artifact-verify")
                if harness.faults.before_verify is not None:
                    harness.faults.before_verify()
                super().verify_artifact()

            def finish(self, **kwargs: object) -> None:
                harness.events.append("finish")
                harness.finish_arguments = {
                    **kwargs,
                    "cleanup_errors": list(kwargs["cleanup_errors"]),
                }
                if harness.faults.finish is not None:
                    raise harness.faults.finish
                super().finish(**kwargs)

        self.api.EvidenceBundle = Bundle

    def _ready(self, device: str, *, cwd: Path) -> None:
        self.events.append("ios-ready")
        if device != "synthetic-device" or cwd != self.root:
            raise AssertionError(
                "iOS readiness must use the explicit device/project cwd"
            )

    def _prepare_session(
        self, *args: object, **kwargs: object
    ) -> fixture_session.FixtureSession:
        self.events.append("prepare")
        if self.faults.prepare is not None:
            raise self.faults.prepare
        self.plan = self._prepare(*args, **kwargs)
        return self.plan

    def _receipt(
        self, plan: fixture_session.FixtureSession, *, fixture_identity: tuple[int, int]
    ) -> dict:
        self.events.append("receipt")
        return fixture_session.verify_receipt(plan, fixture_identity=fixture_identity)

    def _publish_receipt(self) -> None:
        assert self.plan is not None
        installed = json.loads(self.contract.read_bytes())
        installed.pop("fixture_role")
        self.api.write_document(
            self.plan.receipt_path,
            {
                "schema_version": 1,
                "session_id": self.plan.session_id,
                "proof_method": fixture_session.PROOF_METHOD,
                "fixture_pid": IDENTITY[0],
                "fixture_pgid": IDENTITY[1],
                "port": self.plan.port,
                "resource_identity": self.plan.resource_identity,
                "contract_sha256": self.plan.contract_sha256,
                "profile_sha256": self.plan.profile_sha256,
                "captured_at_utc": datetime.now(UTC).isoformat(),
                "installed_identity": installed,
            },
        )

    def _command(
        self, argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append((argv, kwargs))
        stdout = ""
        if "badging" in argv:
            self.events.append("badging")
            stdout = f"package: name='{ANDROID_PACKAGE}'\nlaunchable-activity: name='{ANDROID_ACTIVITY}'\n"
        elif "direct-start" in argv:
            self.events.append("fixture-start")
            self._publish_receipt()
            self.pid_file.write_text("1234 1234\n", encoding="utf-8")
            if self.faults.foreign_on_start:
                self.replace_owner()
            if self.faults.start is not None:
                raise self.faults.start
            stdout = self.faults.start_stdout
        elif "stop-owned" in argv:
            self.events.append("fixture-stop")
            if self.faults.stop is not None:
                raise self.faults.stop
            self.pid_file.unlink()
        elif "tools.android.install_apk" in argv or "install" in argv:
            self.events.append("install")
            if "--log" in argv:
                Path(argv[argv.index("--log") + 1]).write_text(
                    "synthetic install\n", encoding="utf-8"
                )
            else:
                self.api.write_document(
                    Path(argv[argv.index("--json-output") + 1]),
                    {"synthetic": "not CoreDevice proof"},
                )
            if self.faults.install is not None:
                raise self.faults.install
        elif "force-stop" in argv:
            self.events.append("force-stop")
        elif "logcat" in argv:
            self.events.append("logcat-clear")
        else:
            raise AssertionError(f"unexpected parent command: {argv}")
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    def _permissions(self, _device: str) -> None:
        self.events.append("permissions")
        if self.faults.permissions is not None:
            self.faults.permissions()

    def replace_owner(self) -> None:
        self.pid_file.write_text("9999 9999\n", encoding="utf-8")
        self.identity.return_value = FOREIGN

    def corrupt_receipt(self) -> None:
        assert self.plan is not None
        receipt = json.loads(self.plan.receipt_path.read_bytes())
        receipt["fixture_pid"] = FOREIGN[0]
        self.api.write_document(self.plan.receipt_path, receipt)

    def _capture(
        self,
        platform: str,
        device: str,
        log: Path,
        coordination: Path,
        run_id: str,
        service_uuid: str,
        local_name: str,
        *,
        timeout: float,
        guard: Callable[[], None],
        install_json: Path | None = None,
    ) -> tuple[str, tuple[int, ...]]:
        del device, service_uuid, local_name, timeout, install_json
        self.events.append("capture-enter")
        guard()
        self.events.append("native-launch")
        if self.faults.capture is not None:
            raise self.faults.capture
        # Bundle-shaped opaque data, not input to the independently tested checker.
        raw = recovery_runtime(platform).replace(SESSION.encode(), run_id.encode())
        if self.faults.runtime is not None:
            raw = self.faults.runtime(raw)
        paths = {
            log: raw,
            coordination / "runtime.log": raw,
            log.with_suffix(".raw.log"): b"synthetic native capture\n" + raw,
            log.with_suffix(".identity.json"): b'{"synthetic":"not process proof"}\n',
            log.with_suffix(".capture.json"): b'{"synthetic":"not ACK proof"}\n',
            coordination
            / self.api.PHYSICAL_NAME: b'{"synthetic":"not physical proof"}\n',
        }
        for path, content in paths.items():
            path.write_bytes(content)
        self.captured_files = paths
        self.events.append("capture-return")
        if self.faults.after_capture is not None:
            self.faults.after_capture()
        return raw.decode(), CYCLES

    def _checker(self, text: str, **kwargs: object) -> dict:
        self.events.append("checker")
        if self.faults.checker is not None:
            raise self.faults.checker
        assert self.plan is not None
        if kwargs["fixture_identity"] != IDENTITY or kwargs["host_cycles"] != CYCLES:
            raise AssertionError(
                "checker must receive the authoritative owner and exact controls"
            )
        if (
            text.encode()
            != self.captured_files[self.attempt / "recovery-ten-runtime.log"]
        ):
            raise AssertionError("checker must receive unchanged capture text")
        result = {
            "status": "passed",
            "synthetic": "not RF",
            "run_id": self.plan.session_id,
        }
        if self.faults.after_checker is not None:
            self.faults.after_checker()
        return result

    def run(self, *, timeout: float = 12) -> Path:
        return self.api.run(
            self.platform,
            "synthetic-device",
            self.artifact,
            self.contract,
            self.profile,
            timeout=timeout,
            scenario="recovery-ten",
        )

    def main(self) -> int:
        argv = [
            "--platform",
            self.platform,
            "--device",
            "synthetic-device",
            "--artifact",
            str(self.artifact),
            "--fixture-contract",
            str(self.contract),
            "--fixture-profile",
            str(self.profile),
            "--timeout",
            "12",
        ]

        def parser_factory(*args: object, **kwargs: object) -> argparse.ArgumentParser:
            parser = argparse.ArgumentParser(*args, **kwargs)
            parse = parser.parse_args
            parser.parse_args = lambda: parse(argv)
            return parser

        self.cli.argparse = SimpleNamespace(ArgumentParser=parser_factory)
        return self.cli.main()

    def manifest(self) -> dict | None:
        assert self.attempt is not None
        path = self.attempt / evidence.MANIFEST
        return json.loads(path.read_bytes()) if path.exists() else None
