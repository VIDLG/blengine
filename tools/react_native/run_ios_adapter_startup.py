"""Explicitly authorized iOS startup-off gate in the existing RN workbench.

No builds, firmware provisioning, password input or automatic destructive retries.
Xcode may install the frozen App/test runner. An unknown final radio state retains
our phone lease for human recovery; SDK exit and App Passed are not host Passed.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import signal
import subprocess
import sys
import uuid
from pathlib import Path

from tests.peripherals.nrf52840 import provider
from tests.peripherals.nrf52840.cancellation_control import write_document
from tests.peripherals.nrf52840.fixture_session import (
    load_session_profile,
    prepare_session,
    verify_receipt,
)
from tests.peripherals.nrf52840.ios_adapter_startup import (
    IosAdapterStartup,
    verify_physical,
)
from tools.react_native import ios_adapter_handshake
from tools.react_native import ios_adapter_startup_evidence as evidence
from tools.react_native import ios_adapter_startup_ledger as ledger
from tools.react_native.ios_adapter_startup_capture import StartupWindowCapture
from tools.react_native.ios_adapter_startup_journal import MAX_BYTES, parse_journal
from tools.react_native.ios_device import (
    canonical_physical_udid,
    require_ready_unlocked_device,
)
from tools.react_native.ios_radio_lease import IosRadioLease
from tools.react_native.ios_sdk import devicectl_install_app, run_logged
from tools.react_native.rf_evidence_bundle import (
    EvidenceBundle,
    _read_small,
    _recovery_json,
    _require,
)
from tools.react_native.rf_fixture import preflight_fixture

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "tests/consumers/react-native-internal/ios/build"


def run_handshake(
    device: str, *, app: Path, test_run: Path, log: Path, timeout: float
) -> Path:
    """Run the non-RF automation preflight before any fixture/phone owner exists."""
    return ios_adapter_handshake.run(
        device, app=app, test_run=test_run, log=log, timeout=min(90, timeout)
    )


def _sdk(command: list[str], log: Path, *, timeout: float = 30) -> None:
    """Compatibility facade for one bounded SDK command."""
    run_logged(command, cwd=ROOT, log=log, timeout=timeout)


def collect_control(result: Path, attempt: Path, device: str) -> tuple[dict, str]:
    """Export once and retain the original control bytes, including a failed gate.

    The lease owner validates restoration separately. The final bundle additionally
    binds summary, every exported attachment, runtime and the official device.
    """
    _require(result.is_dir(), "XCTest result unavailable; restoration is unknown")
    directory = attempt / "xcresult-attachments"
    _require(
        not directory.exists() and not directory.is_symlink(),
        "refusing existing XCTest attachment export",
    )
    _sdk(
        [
            "xcrun",
            "xcresulttool",
            "export",
            "attachments",
            "--path",
            str(result),
            "--output-path",
            str(directory),
        ],
        attempt / "xcresult-attachments-export.log",
    )
    wrapped = _recovery_json(
        '{"attachments":'
        + _read_small(directory / "manifest.json", MAX_BYTES).decode("utf-8")
        + "}"
    )
    _require(
        set(wrapped) == {"attachments"}, "invalid XCTest attachment manifest envelope"
    )
    groups = wrapped["attachments"]
    _require(
        type(groups) is list
        and len(groups) == 1
        and type(groups[0]) is dict
        and groups[0].get("testIdentifier") == evidence.ATTACHMENT_METHOD,
        "XCTest control must belong to the one selected method",
    )
    attachments = groups[0].get("attachments")
    _require(
        type(attachments) is list and len(attachments) <= 256,
        "bounded XCTest attachment list required",
    )
    matches = []
    for item in attachments:
        _require(
            type(item) is dict and item.get("deviceId") == device,
            "foreign XCTest attachment device",
        )
        suggested = item.get("suggestedHumanReadableName", "")
        _require(type(suggested) is str, "invalid attachment suggested name")
        if re.fullmatch(
            re.escape(evidence.CONTROL_ATTACHMENT) + r"_[0-9]+_[0-9A-Fa-f-]+\.json",
            suggested,
        ):
            filename = item.get("exportedFileName")
            _require(
                type(filename) is str
                and re.fullmatch(r"[0-9A-Fa-f-]+\.json", filename) is not None,
                "unsafe XCTest control attachment filename",
            )
            matches.append(directory / filename)
    _require(
        len(matches) == 1, "exactly one original XCTest control attachment required"
    )
    raw = _read_small(matches[0], MAX_BYTES)
    control = _recovery_json(raw.decode("utf-8"))
    with (attempt / evidence.names()["control"]).open("xb") as stream:
        stream.write(raw)
    return control, hashlib.sha256(raw).hexdigest()


def collect_summary(result: Path, attempt: Path) -> None:
    destination = attempt / evidence.names()["summary"]
    _require(
        not destination.exists() and not destination.is_symlink(),
        "refusing existing XCTest summary",
    )
    with (
        destination.open("xb") as output,
        (attempt / "xcresult-summary.log").open("xb") as diagnostics,
    ):
        subprocess.run(
            [
                "xcrun",
                "xcresulttool",
                "get",
                "test-results",
                "summary",
                "--path",
                str(result),
                "--compact",
            ],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=diagnostics,
            start_new_session=True,
            timeout=30,
            check=True,
        )


def run(
    device: str,
    *,
    app: Path,
    test_run: Path,
    fixture_contract: Path,
    fixture_profile: Path,
    authorize_radio: bool = False,
    timeout: float = 240,
) -> Path:
    """Own the phone before installation and one live-attested fixture until cleanup."""
    _require(
        authorize_radio is True,
        "explicit --authorize-radio is required; no device operation performed",
    )
    device = canonical_physical_udid(device)
    _require(
        type(timeout) in (int, float) and 0 < timeout <= 240,
        "iOS startup timeout must be finite and in (0, 240]",
    )
    service = preflight_fixture(
        fixture_contract=fixture_contract, fixture_profile=fixture_profile
    )
    _read_small(test_run, MAX_BYTES)  # No build, pod install or provisioning fallback.
    _require(
        app.is_dir() and app.name == "BleHubNitroHost.app",
        "prebuilt signed host App required",
    )
    attempt = BUILD / ("ios-adapter-startup-" + uuid.uuid4().hex)
    attempt.mkdir(parents=True, mode=0o700)
    # This must precede fixture admission and the RF phone lease. A failed
    # automation mode is an environment/preflight failure, not an RF attempt.
    run_handshake(
        device,
        app=app,
        test_run=test_run,
        log=attempt / "ios-adapter-handshake",
        timeout=timeout,
    )
    log = attempt / evidence.LOG
    bundle: EvidenceBundle | None = None
    phone: IosRadioLease | None = None
    physical: IosAdapterStartup | None = None
    capture: StartupWindowCapture | None = None
    control = None
    control_hash = None
    runtime = control_audit = None
    completed = False
    control_attempted = summary_attempted = journal_attempted = False
    primary: BaseException | None = None
    cleanup_errors: list[str] = []
    previous = signal.getsignal(signal.SIGTERM)

    def interrupted(_signal: int, _frame: object) -> None:
        raise InterruptedError("iOS startup-off gate interrupted by SIGTERM")

    signal.signal(signal.SIGTERM, interrupted)
    try:
        bundle = EvidenceBundle(
            attempt,
            platform="ios",
            device=device,
            project_root=ROOT,
            scenario=evidence.SCENARIO,
        )
        _require(
            not provider.PID_FILE.exists(),
            "refusing to overlap an existing nRF provider",
        )
        snapshot = bundle.prepare_artifact(app)
        port = provider._port()
        _require(port is not None, "configured nRF fixture port required")
        plan = prepare_session(
            fixture_contract,
            fixture_profile,
            attempt / "coordination/fixture-session",
            port=port,
            service_uuid=service,
            session_id=bundle.document["run_id"],
        )
        profile = load_session_profile(plan.contract, plan.profile_path)
        phone = IosRadioLease(
            device, plan.session_id, attempt / evidence.names()["phone_lease"]
        )
        phone.acquire()
        phone.require_owner()
        require_ready_unlocked_device(device, cwd=ROOT)
        physical = IosAdapterStartup(plan, attempt / "coordination")
        physical.start()
        receipt_raw = _read_small(plan.receipt_path, MAX_BYTES)
        request_raw = _read_small(plan.request_path, MAX_BYTES)
        receipt_hash = hashlib.sha256(receipt_raw).hexdigest()
        fixture_identity = (os.getpid(), os.getpgid(0))

        def guard() -> None:
            phone.require_owner()
            physical.guard()
            verify_receipt(plan, fixture_identity=fixture_identity)
            _require(
                _read_small(plan.request_path, MAX_BYTES) == request_raw
                and _read_small(plan.receipt_path, MAX_BYTES) == receipt_raw,
                "immutable fixture request/receipt changed",
            )

        guard()
        bundle.verify_artifact()
        install = attempt / evidence.names()["device_install"]
        _sdk(
            devicectl_install_app(device, snapshot, install),
            attempt / "device-install.log",
            timeout=120,
        )
        guard()
        bundle.verify_artifact()

        def ready_guard() -> None:
            guard()
            off_path = capture.snapshot_off_journal()
            off = ledger.verify_off_journal(
                parse_journal(_read_small(off_path, MAX_BYTES), plan.session_id)
            )
            _require(
                capture.window_identity
                == {
                    "native": off["native_bootstrap"],
                    "run_id": plan.session_id,
                    "nonce": off["runtime_nonce"],
                },
                "UI window identity differs from durable OFF journal",
            )
            physical.open_on(off)
            guard()

        capture = StartupWindowCapture(
            device,
            log,
            plan.session_id,
            profile.service_uuid,
            profile.local_name,
            receipt_hash,
            install_json=install,
            test_run=test_run,
            app=snapshot,
            timeout=timeout,
            guard=guard,
            ready_guard=ready_guard,
        )
        capture.run()
        control_attempted = True
        control, control_hash = collect_control(capture.result, attempt, device)
        summary_attempted = True
        collect_summary(capture.result, attempt)
        journal_attempted = True
        full_path = capture.download_journal()
        runtime = ledger.verify_runtime(
            parse_journal(_read_small(full_path, MAX_BYTES), plan.session_id),
            profile.service_uuid,
        )
        control_audit = ledger.verify_control(
            control, runtime=runtime, receipt_sha256=receipt_hash
        )
        physical.complete(runtime)
        bundle.verify_artifact()
        completed = True
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            # Read-only evidence collection may still recover ON proof after an
            # assertion failure. It never retries the XCTest window or radio OFF.
            if capture is not None and capture.run_called:
                for label, needed, action in (
                    (
                        "control export",
                        not control_attempted,
                        lambda: collect_control(capture.result, attempt, device),
                    ),
                    (
                        "summary",
                        not summary_attempted,
                        lambda: collect_summary(capture.result, attempt),
                    ),
                    ("journal", not journal_attempted, capture.download_journal),
                ):
                    if needed:
                        try:
                            value = action()
                            if label == "control export":
                                control, control_hash = value
                        except BaseException as error:  # noqa: BLE001 - interruption during collection must not skip either owner's cleanup
                            cleanup_errors.append(
                                f"{label}: {type(error).__name__}: {error}"
                            )
            for label, action in (
                (
                    "physical finish",
                    lambda: physical.finish(primary) if physical is not None else [],
                ),
                (
                    "phone finish",
                    lambda: (
                        phone.finish(control, control_hash) if phone is not None else []
                    ),
                ),
            ):
                try:
                    cleanup_errors.extend(action())
                except BaseException as error:  # noqa: BLE001 - one cleanup failure must not skip the other owner or overwrite primary
                    cleanup_errors.append(f"{label}: {type(error).__name__}: {error}")
                    if primary is not None:
                        primary.add_note(f"iOS startup {label} failed: {error}")
            if completed and primary is None and not cleanup_errors:
                try:
                    physical_audit = verify_physical(
                        _recovery_json(
                            _read_small(
                                attempt / evidence.names()["physical"], MAX_BYTES
                            ).decode()
                        ),
                        request=_recovery_json(request_raw.decode()),
                        receipt=_recovery_json(receipt_raw.decode()),
                        receipt_sha256=receipt_hash,
                        runtime=runtime,
                        readiness=_recovery_json(
                            _read_small(
                                attempt / evidence.names()["readiness"], MAX_BYTES
                            ).decode()
                        ),
                    )
                    write_document(
                        attempt / evidence.names()["ledger"],
                        {
                            "schema_version": 1,
                            "scope": "ios-adapter-startup-host-audit",
                            "run_id": plan.session_id,
                            "runtime": runtime,
                            "control": control_audit,
                            "physical": physical_audit,
                        },
                    )
                except BaseException as error:  # noqa: BLE001 - evidence interruption cannot skip bundle finalization
                    cleanup_errors.append(f"final evidence: {error}")
            if bundle is not None:
                try:
                    bundle.finish(
                        completed=completed and not cleanup_errors,
                        primary_error=primary,
                        cleanup_errors=cleanup_errors,
                        runtime_log=log,
                    )
                except BaseException as error:
                    if primary is not None:
                        primary.add_note(
                            f"iOS startup bundle finalization failed: {error}"
                        )
                    else:
                        raise
            if cleanup_errors:
                if primary is not None:
                    primary.add_note(
                        "iOS startup cleanup/evidence: " + "; ".join(cleanup_errors)
                    )
                else:
                    raise RuntimeError(
                        "iOS startup cleanup/evidence: " + "; ".join(cleanup_errors)
                    )
        finally:
            signal.signal(signal.SIGTERM, previous)
    return attempt / "evidence-bundle.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("device")
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--xctestrun", type=Path, required=True)
    parser.add_argument("--fixture-contract", type=Path, required=True)
    parser.add_argument("--fixture-profile", type=Path, required=True)
    parser.add_argument("--authorize-radio", action="store_true")
    parser.add_argument("--timeout", type=float, default=240)
    args = parser.parse_args()
    try:
        manifest = run(
            args.device,
            app=args.app,
            test_run=args.xctestrun,
            fixture_contract=args.fixture_contract,
            fixture_profile=args.fixture_profile,
            authorize_radio=args.authorize_radio,
            timeout=args.timeout,
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"ios-adapter-startup:failed {error}", file=sys.stderr)
        return 2
    print(f"ios-adapter-startup:passed bundle={manifest} scope=recorded-candidate-only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
