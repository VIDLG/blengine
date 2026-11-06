"""Candidate single-XCTest startup-off window; no live CLI or RF verdict.

The caller must own the phone and fixture from before installation until final
restoration/evidence checks. Both guards are mandatory: guard checks that lifetime;
ready_guard additionally proves the current receipt/readiness/no-peer boundary.
This owner reuses bounded SDK pumping and official install/PID/payload checks. It
never builds, retries OFF, launches a replacement App or restores radio
itself. Xcode test execution may install its staged frozen App and test runner;
the caller must account for that installation under its phone/artifact ownership. XCTest owns UI mutation/restoration; abnormal termination can prevent it.
"""

from __future__ import annotations

import hashlib
import math
import plistlib
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tests.peripherals.nrf52840.cancellation_control import write_document
from tools.react_native.capture_io import PrefixStream, stop_local
from tools.react_native.capture_transport import (
    POLL_SECONDS,
    CaptureTransport,
    ios_executables_match,
)
from tools.react_native.host_contract import BUNDLE_ID
from tools.react_native.ios_adapter_startup_journal import MAX_BYTES, parse_journal
from tools.react_native.ios_adapter_startup_ledger import verify_window_ready
from tools.react_native.ios_device import require_ready_unlocked_device
from tools.react_native.ios_sdk import (
    devicectl_copy_from,
    official_result,
    run_logged,
    xcodebuild_test_without_building,
)
from tools.react_native.rf_evidence_bundle import _read_small, _recovery_json, _require

WINDOW_PREFIX = "BLEHUB_IOS_ADAPTER_WINDOW_READY "
TEST_TARGET = "BleHubNitroHostUITests"
TEST_METHOD = "BleHubNitroHostUITests/testBluetoothStartupOffAndFreshProbeRecovery"
TEST_IDENTIFIER = TEST_TARGET + "/" + TEST_METHOD


def _environment(
    run_id: str, service_uuid: str, local_name: str, receipt_sha256: str
) -> dict[str, str]:
    for value in (run_id, service_uuid):
        _require(
            type(value) is str
            and re.fullmatch(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", value)
            is not None,
            "canonical startup-off run/service UUID required",
        )
    _require(
        type(receipt_sha256) is str
        and re.fullmatch(r"[0-9a-f]{64}", receipt_sha256) is not None,
        "canonical startup-off fixture receipt hash required",
    )
    _require(
        type(local_name) is str
        and bool(local_name)
        and local_name == local_name.strip()
        and not local_name.startswith("--")
        and len(local_name.encode("utf-8")) <= 128
        and not any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in local_name),
        "bounded startup-off local name required",
    )
    return {
        "BLEHUB_IOS_ADAPTER_STARTUP_GATE": "1",
        "BLEHUB_IOS_ADAPTER_RUN_ID": run_id,
        "BLEHUB_IOS_ADAPTER_SERVICE_UUID": service_uuid,
        "BLEHUB_IOS_ADAPTER_LOCAL_NAME": local_name,
        "BLEHUB_IOS_ADAPTER_FIXTURE_RECEIPT_SHA256": receipt_sha256,
    }


def prepare_test_run(
    source: Path,
    destination: Path,
    app: Path,
    *,
    run_id: str,
    service_uuid: str,
    local_name: str,
    receipt_sha256: str,
) -> None:
    """Stage the current flat Xcode test-run format without changing build outputs.

    Absolute TESTROOT expansion preserves the source's runner/dependency paths
    when staging outside Build/Products. Only the UI target remains discoverable;
    its App path is the caller's frozen candidate, not a second build or package.
    Unsupported formats/conflicting opt-ins fail before any SDK invocation.
    """
    environment = _environment(run_id, service_uuid, local_name, receipt_sha256)
    document = plistlib.loads(_read_small(source, MAX_BYTES))
    _require(
        type(document) is dict
        and set(document)
        <= {TEST_TARGET, "BleHubNitroHostNativeTests", "__xctestrun_metadata__"},
        "unsupported startup-off xctestrun targets/format",
    )
    target = document.get(TEST_TARGET)
    _require(
        type(target) is dict and target.get("IsUITestBundle") is True,
        "startup-off UI test target required",
    )
    _require(
        app.is_dir() and app.name == "BleHubNitroHost.app",
        "frozen host App directory required",
    )
    _require(
        not destination.exists() and not destination.is_symlink(),
        "refusing existing staged test run",
    )

    def relocate(value: Any) -> Any:
        if type(value) is str:
            return value.replace("__TESTROOT__", str(source.resolve().parent))
        if type(value) is list:
            return [relocate(item) for item in value]
        if type(value) is dict:
            return {key: relocate(item) for key, item in value.items()}
        return value

    target = relocate(target)
    for key in (
        "EnvironmentVariables",
        "TestingEnvironmentVariables",
        "UITargetAppEnvironmentVariables",
    ):
        values = target.get(key, {})
        _require(
            type(values) is dict
            and all(type(k) is str and type(v) is str for k, v in values.items()),
            "invalid xctestrun environment",
        )
        _require(
            not any("BLEHUB" in key for key in values),
            "conflicting xctestrun opt-in environment",
        )
    _require(
        target.get("CommandLineArguments", []) == []
        and target.get("UITargetAppCommandLineArguments", []) == [],
        "conflicting xctestrun launch arguments",
    )
    target.setdefault("EnvironmentVariables", {}).update(environment)
    target["OnlyTestIdentifiers"] = [TEST_METHOD]
    target["SkipTestIdentifiers"] = []
    original_app = target.get("UITargetAppPath")
    dependencies = target.get("DependentProductPaths")
    _require(
        type(original_app) is str
        and original_app.endswith(".app")
        and type(dependencies) is list
        and all(type(item) is str for item in dependencies)
        and dependencies.count(original_app) == 1,
        "xctestrun must identify exactly one original App dependency",
    )
    frozen_app = str(app.resolve())
    target["DependentProductPaths"] = [
        frozen_app + item[len(original_app) :]
        if item == original_app or item.startswith(original_app + "/")
        else item
        for item in dependencies
    ]
    target["UITargetAppPath"] = frozen_app
    target["UITargetAppBundleIdentifier"] = BUNDLE_ID
    target["UserAttachmentLifetime"] = "keepAlways"
    target["SystemAttachmentLifetime"] = "keepAlways"
    with destination.open("xb") as stream:
        plistlib.dump(
            {
                TEST_TARGET: target,
                "__xctestrun_metadata__": document.get("__xctestrun_metadata__", {}),
            },
            stream,
        )


class StartupWindowCapture(CaptureTransport):
    """One bounded UI runner, one readiness delivery, no second App launch path."""

    def __init__(
        self,
        device: str,
        log: Path,
        run_id: str,
        service_uuid: str,
        local_name: str,
        receipt_sha256: str,
        *,
        install_json: Path,
        test_run: Path,
        app: Path,
        timeout: float,
        guard: Callable[[], None],
        ready_guard: Callable[[], None],
    ) -> None:
        _environment(run_id, service_uuid, local_name, receipt_sha256)
        _require(
            type(device) is str
            and re.fullmatch(r"[A-Za-z0-9-]{16,64}", device) is not None,
            "explicit physical iOS device identifier required",
        )
        _require(
            type(timeout) in (int, float)
            and math.isfinite(timeout)
            and 0 < timeout <= 600,
            "startup-off window timeout must be finite and within 600 seconds",
        )
        _require(
            callable(guard) and callable(ready_guard),
            "phone/fixture and ready guards are required",
        )
        self.parent_guard, self.ready_guard = guard, ready_guard
        self.staged = log.with_suffix(".xctestrun")
        self.result = log.with_suffix(".xcresult")
        self.receipt_hash = receipt_sha256
        self.window: dict[str, Any] | None = None
        self.window_identity: dict[str, Any] | None = None
        self.delivered = False
        self.run_called = False
        self.window_deadline = math.inf
        self.stage_hash: str | None = None
        super().__init__(
            "ios",
            device,
            log,
            log.parent,
            run_id,
            service_uuid,
            local_name,
            timeout,
            self._guard,
            install_json,
        )
        for path in (
            log,
            log.with_suffix(".raw.log"),
            log.with_suffix(".capture.json"),
            self.staged,
            self.result,
        ):
            _require(
                not path.exists() and not path.is_symlink(),
                f"refusing existing startup-off evidence: {path}",
            )
        prepare_test_run(
            test_run,
            self.staged,
            app,
            run_id=run_id,
            service_uuid=service_uuid,
            local_name=local_name,
            receipt_sha256=receipt_sha256,
        )
        self.stage_hash = hashlib.sha256(
            _read_small(self.staged, MAX_BYTES)
        ).hexdigest()
        self.receipt.update(
            scope="ios-adapter-startup-window-candidate",
            fixture_receipt_sha256=receipt_sha256,
            test_run_sha256=self.stage_hash,
            primary_error=None,
            cleanup_errors=[],
            restoration="XCTest control attachment must be checked; not guaranteed by this capture",
        )

    def _guard(self) -> None:
        self.parent_guard()
        if self.stage_hash is not None:
            _require(
                hashlib.sha256(_read_small(self.staged, MAX_BYTES)).hexdigest()
                == self.stage_hash,
                "staged startup-off test run changed",
            )

    def _publish(self) -> None:
        self.receipt.update(window=self.window, ready_delivered=self.delivered)
        write_document(self.log.with_suffix(".capture.json"), self.receipt)
        self.log.write_text(
            "".join(line + "\n" for line in self.lines), encoding="utf-8"
        )

    def _publish_preserving(self, primary: BaseException | None) -> None:
        try:
            self._publish()
        except BaseException as error:
            self.receipt["status"] = "failed"
            self.receipt["cleanup_errors"].append(f"evidence publication: {error}")
            # The receipt may already have been replaced before the normalized
            # log failed. Best-effort fail that receipt independently; never
            # replay the capture or hide a persistently unwritable filesystem.
            try:
                write_document(self.log.with_suffix(".capture.json"), self.receipt)
            except BaseException as publication_error:  # noqa: BLE001 - retain the original failure even if cleanup is interrupted
                error.add_note(
                    f"failed-state receipt publication also failed: {publication_error}"
                )
            if primary is None:
                raise
            primary.add_note(f"startup-off evidence publication failed: {error}")

    def _remaining(self) -> float:
        remaining = self._request_deadline() - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("startup-off command submission deadline exceeded")
        return remaining

    def _poll(self) -> None:
        now = time.monotonic()
        if now >= self.deadline:
            raise TimeoutError("startup-off XCTest absolute window deadline exceeded")
        if now >= self.next_guard:
            self.guard()
            self.next_guard = now + 0.5
        if self.stream is None:
            return
        lines = self.stream.poll()
        self.lines.extend(lines)
        try:
            for line in lines:
                _require(self.window is None, "duplicate startup-off window readiness")
                value = _recovery_json(line.removeprefix(WINDOW_PREFIX))
                identity = verify_window_ready(value, self.run_id, self.receipt_hash)
                _require(
                    ios_executables_match(
                        self.executable, identity["native"]["executable"]
                    ),
                    "startup-off identity differs from official installed executable",
                )
                self.window, self.window_identity = value, identity
                self.pid = identity["native"]["pid"]
                self.window_deadline = min(self.deadline, now + 25)
        except BaseException as error:
            self._publish_preserving(error)
            raise
        self._publish()

    def _request_deadline(self) -> float:
        return (
            min(self.deadline, self.window_deadline)
            if not self.delivered
            else self.deadline
        )

    def _deliver_ready(self) -> None:
        _require(
            self.window_identity is not None and not self.delivered,
            "early/duplicate startup-off ready delivery",
        )
        self.guard()
        self._verify_process("ready-before")
        self.ready_guard()
        self.guard()
        _require(
            self.process is not None and self.process.poll() is None,
            "XCTest exited during ready authorization; no URL delivery permitted",
        )
        self._remaining()
        self._relay(f"blehub-rn-spike://adapter/{self.run_id}/ready", "startup-ready")
        self.delivered = True
        self._publish()

    def run(self) -> dict[str, Any]:
        """Run once and retain failures; caller must collect UI/journal/cleanup evidence.

        No retry or fallback launch is attempted. A zero SDK exit is not accepted
        without the one guarded ready delivery, and is never a radio/RF verdict.
        On timeout/interruption only this owned local process group is reaped;
        phone restoration remains unverified until the UI evidence is collected.
        """
        _require(not self.run_called, "startup-off XCTest window cannot be rerun")
        self.run_called = True
        primary: BaseException | None = None
        try:
            self.guard()
            require_ready_unlocked_device(
                self.device,
                cwd=Path(__file__).resolve().parents[2],
                timeout=self._remaining(),
            )
            self.guard()
            self._remaining()
            # Xcode rejects -test-iterations 1. Omit repetition flags entirely:
            # a count above one could repeat the destructive radio window.
            # XCTest stdout carries the live readiness request; -quiet suppresses it.
            command = xcodebuild_test_without_building(
                xctestrun=self.staged,
                device=self.device,
                result=self.result,
                only_testing=TEST_IDENTIFIER,
            )
            self.output = self.log.with_suffix(".raw.log").open("xb")
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=self.output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            self.receipt.update(command=command, capture_pid=self.process.pid)
            self.stream = PrefixStream(
                self.log.with_suffix(".raw.log"), None, prefix=WINDOW_PREFIX
            )
            self._publish()
            while True:
                self._poll()
                if self.window is not None and not self.delivered:
                    _require(
                        self.process.poll() is None,
                        "XCTest exited before ready delivery",
                    )
                    self._deliver_ready()
                code = self.process.poll()
                if code is not None:
                    self._poll()
                    _require(
                        code == 0 and self.delivered,
                        "startup-off XCTest failed/exited without guarded readiness",
                    )
                    tail = self.stream.previous[self.stream.complete :]
                    _require(
                        WINDOW_PREFIX.encode() not in tail,
                        "truncated startup-off readiness marker",
                    )
                    self.receipt["status"] = "window-completed-awaiting-evidence"
                    break
                time.sleep(POLL_SECONDS)
            return self.receipt
        except BaseException as error:
            primary = error
            self.receipt.update(status="failed", primary_error=str(error))
            raise
        finally:
            final_error = primary
            cleanup_error: BaseException | None = None
            actions = []
            if self.process is not None:
                actions.append(lambda: stop_local(self.process, console=True))
            if self.output is not None:
                actions.append(self.output.close)
            for action in actions:
                try:
                    action()
                except BaseException as error:  # noqa: BLE001 - aggregate cleanup without replacing an active primary
                    self.receipt["status"] = "failed"
                    self.receipt["cleanup_errors"].append(str(error))
                    if final_error is not None:
                        final_error.add_note(
                            f"startup-off local runner cleanup failed: {error}"
                        )
                    else:
                        final_error = cleanup_error = error
            self._publish_preserving(final_error)
            if cleanup_error is not None:
                raise cleanup_error

    def download_journal(self) -> Path:
        """Copy the exact run's native journal without relaunching its App.

        This may be used after a failed XCTest for diagnosis. Its own independent
        command budget does not restart the scenario, authorize RF or retry OFF.
        """
        _require(
            self.run_called
            and (self.process is None or self.process.poll() is not None),
            "journal download is post-window evidence collection only",
        )
        return self._copy_journal(live=False)

    def snapshot_off_journal(self) -> Path:
        """Copy the durable OFF prefix only inside the guarded ready window."""
        _require(
            self.run_called
            and self.window_identity is not None
            and not self.delivered
            and self.process is not None
            and self.process.poll() is None,
            "OFF journal requires the live, not-yet-authorized ready window",
        )
        self._remaining()
        return self._copy_journal(live=True)

    def _copy_journal(self, *, live: bool) -> Path:
        self.guard()
        stem = "off-journal" if live else "journal"
        path = self.log.with_suffix(f".{stem}.json")
        receipt = self.log.with_suffix(f".{stem}-copy.json")
        _require(
            not path.exists()
            and not path.is_symlink()
            and not receipt.exists()
            and not receipt.is_symlink(),
            "refusing existing startup-off journal download",
        )
        # Do not use the expired scenario clock or restart its capture to collect
        # post-run evidence. No radio/App command occurs in this file transfer.
        command = devicectl_copy_from(
            self.device,
            f"Library/Caches/blehub-ios-adapter/{self.run_id}.json",
            BUNDLE_ID,
            path,
            receipt,
            timeout=5 if live else 15,
        )
        if live:
            self._command(command, budget=3.0)
        else:
            run_logged(
                command,
                cwd=Path(__file__).resolve().parents[2],
                log=self.log.with_suffix(".journal-copy.log"),
                timeout=20,
            )
        copied = official_result(receipt, "devicectl.device.copy.from", self.device)
        _require(
            copied["deviceIdentifier"] == self.device_identifier,
            "journal download changed official device identity",
        )
        self.guard()
        parse_journal(_read_small(path, MAX_BYTES), self.run_id)
        return path
