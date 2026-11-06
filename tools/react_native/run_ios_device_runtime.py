"""Launch the RN iOS host with devicectl and capture its console smoke output."""

from __future__ import annotations

import argparse
import codecs
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time
from collections import deque
from dataclasses import asdict
from pathlib import Path
from typing import cast

from tests.peripherals.nrf52840.cancellation_control import (
    Control,
    SuiteControlRelay,
    abort_control,
)
from tools.react_native import rf_suite_coordination
from tools.react_native.host_contract import BUNDLE_ID
from tools.react_native.rf_control_relay import (
    IosRuntimeIdentity,
    advance_relay,
    relay_ios,
)

FAILURE_TAIL_LINES = 40
RECOVERY_CLOSE_GRACE_SECONDS = 15.0


def main() -> int:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("udid")
    _ = parser.add_argument("log", type=Path)
    _ = parser.add_argument("--seconds", type=float, default=20.0)
    mode = parser.add_mutually_exclusive_group()
    _ = mode.add_argument("--stress", action="store_true")
    _ = mode.add_argument("--reload", action="store_true")
    _ = mode.add_argument("--rf-service-uuid")
    _ = mode.add_argument("--rf-connect-service-uuid")
    _ = mode.add_argument("--rf-gatt-service-uuid")
    _ = mode.add_argument("--rf-io-service-uuid")
    _ = mode.add_argument("--rf-rssi-service-uuid")
    _ = mode.add_argument("--rf-notification-service-uuid")
    _ = mode.add_argument("--rf-indication-service-uuid")
    _ = mode.add_argument("--rf-passive-disconnect-service-uuid")
    _ = mode.add_argument("--rf-cancellation-service-uuid")
    _ = mode.add_argument("--rf-watchdog-service-uuid")
    _ = mode.add_argument("--rf-lab-suite-service-uuid")
    _ = parser.add_argument("--rf-lab-suite-flow")
    _ = parser.add_argument("--rf-expected-local-name")
    _ = parser.add_argument("--rf-control-json")
    _ = parser.add_argument("--rf-lab-suite-coordination-path", type=Path)
    _ = parser.add_argument("--rf-host-install-json", type=Path)
    _ = parser.add_argument(
        "--keep-app",
        action="store_true",
        help="end only the host console after the terminal marker; keep the iOS app running",
    )
    _ = mode.add_argument("--rf-recovery-service-uuid")

    _ = mode.add_argument("--rf-reload-service-uuid")
    _ = mode.add_argument("--rf-multi-service-uuids")
    _ = mode.add_argument("--rf-multi-soak-service-uuids")
    _ = mode.add_argument("--rf-multi-fault-service-uuids")
    _ = parser.add_argument(
        "--rf-multi-subscription-mode",
        choices=("notification", "indication"),
        default="notification",
    )
    _ = parser.add_argument("--rf-multi-coordination-map", type=Path)
    _ = parser.add_argument("--rf-multi-soak-cycles", type=int, default=10)
    _ = parser.add_argument("--rf-pending-read-reload-service-uuid")
    _ = parser.add_argument("--reload-notification-name")
    args = parser.parse_args()
    if args.rf_pending_read_reload_service_uuid == "":
        parser.error("--rf-pending-read-reload-service-uuid must not be empty")
    reload_notification_name = cast(str | None, args.reload_notification_name)
    if reload_notification_name is not None and (
        not (args.reload or args.rf_pending_read_reload_service_uuid)
        or re.fullmatch(
            r"dev\.blehub\.rn\.reload\.[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}",
            reload_notification_name,
        )
        is None
    ):
        parser.error(
            "--reload-notification-name requires --reload or --rf-pending-read-reload-service-uuid and a unique UUID name"
        )
    udid = cast(str, args.udid)
    log = cast(Path, args.log)
    launch_json = log.with_suffix(".launch.json")
    cleanup_receipt = log.with_suffix(".console-cleanup.json")
    seconds = cast(float, args.seconds)
    stress = cast(bool, args.stress)
    reload = cast(bool, args.reload)
    rf_service_uuid = cast(str | None, args.rf_service_uuid)
    rf_connect_service_uuid = cast(str | None, args.rf_connect_service_uuid)
    rf_gatt_service_uuid = cast(str | None, args.rf_gatt_service_uuid)
    rf_io_service_uuid = cast(str | None, args.rf_io_service_uuid)
    rf_rssi_service_uuid = cast(str | None, args.rf_rssi_service_uuid)
    rf_notification_service_uuid = cast(str | None, args.rf_notification_service_uuid)
    rf_indication_service_uuid = cast(str | None, args.rf_indication_service_uuid)
    rf_passive_disconnect_service_uuid = cast(
        str | None, args.rf_passive_disconnect_service_uuid
    )
    rf_cancellation_service_uuid = cast(str | None, args.rf_cancellation_service_uuid)
    rf_watchdog_service_uuid = cast(str | None, args.rf_watchdog_service_uuid)
    if (
        rf_watchdog_service_uuid is not None
        and re.fullmatch(
            r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", rf_watchdog_service_uuid
        )
        is None
    ):
        parser.error("--rf-watchdog-service-uuid requires a strict canonical UUID")
    rf_lab_suite_service_uuid = cast(str | None, args.rf_lab_suite_service_uuid)
    rf_lab_suite_flow = cast(str | None, args.rf_lab_suite_flow)
    rf_expected_local_name = cast(str | None, args.rf_expected_local_name)
    if rf_expected_local_name is not None and (
        not (
            rf_lab_suite_service_uuid
            or rf_watchdog_service_uuid
            or rf_cancellation_service_uuid
        )
        or not rf_expected_local_name
    ):
        parser.error(
            "--rf-expected-local-name requires suite service UUID and a nonempty name"
        )
    single_control = None
    if rf_watchdog_service_uuid or rf_cancellation_service_uuid:
        try:
            value = json.loads(args.rf_control_json or "null")
            if type(value) is not dict or set(value) != {
                "session",
                "flow",
                "mode",
                "token",
            }:
                raise ValueError("single-control metadata required")
            single_control = Control(**value)
            if (
                single_control.mode
                != ("rf-watchdog" if rf_watchdog_service_uuid else "rf-cancellation")
                or not rf_expected_local_name
            ):
                raise ValueError("single-control mode/local name drift")
        except (ValueError, TypeError) as error:
            parser.error(str(error))
    elif args.rf_control_json is not None:
        parser.error("--rf-control-json requires a standalone read-fault mode")
    rf_lab_suite_coordination_path = cast(
        Path | None, args.rf_lab_suite_coordination_path
    )
    control_relay = None
    runtime_identity = None
    if rf_lab_suite_service_uuid and rf_lab_suite_flow is None:
        if rf_lab_suite_coordination_path is None or args.rf_host_install_json is None:
            parser.error(
                "continuous10 requires same-session control metadata and --rf-host-install-json"
            )
        control_relay = SuiteControlRelay(rf_lab_suite_coordination_path)
        runtime_identity = IosRuntimeIdentity(
            udid,
            control_relay.control,
            args.rf_host_install_json,
            log,
            controls=control_relay.controls,
        )
    elif args.rf_host_install_json is not None:
        parser.error("--rf-host-install-json requires continuous10")
    keep_app = cast(bool, args.keep_app)

    if rf_lab_suite_coordination_path is not None and rf_lab_suite_service_uuid is None:
        parser.error("--rf-lab-suite-coordination-path requires suite service UUID")
    if (
        rf_lab_suite_service_uuid is not None
        and rf_lab_suite_flow is not None
        and rf_lab_suite_flow
        not in {
            "rf",
            "rf-connect",
            "rf-gatt",
            "rf-io",
            "rf-rssi",
            "rf-notification",
            "rf-indication",
            "rf-passive-disconnect",
            "rf-cancellation",
        }
    ):
        parser.error("--rf-lab-suite-flow must name one of the nine RF Lab flows")
    rf_recovery_service_uuid = cast(str | None, args.rf_recovery_service_uuid)

    rf_reload_service_uuid = cast(str | None, args.rf_reload_service_uuid)
    rf_multi_service_uuids = cast(str | None, args.rf_multi_service_uuids)
    rf_multi_soak_service_uuids = cast(str | None, args.rf_multi_soak_service_uuids)
    rf_multi_subscription_mode = cast(str, args.rf_multi_subscription_mode)
    rf_multi_soak_cycles = cast(int, args.rf_multi_soak_cycles)
    rf_multi_fault_service_uuids = cast(str | None, args.rf_multi_fault_service_uuids)
    rf_multi_coordination_map = cast(Path | None, args.rf_multi_coordination_map)
    if (
        rf_multi_coordination_map is not None
        and rf_multi_service_uuids is None
        and rf_multi_soak_service_uuids is None
    ):
        parser.error(
            "--rf-multi-coordination-map requires a multi-DK service UUID option"
        )
    rf_pending_read_reload_service_uuid = cast(
        str | None, args.rf_pending_read_reload_service_uuid
    )
    if rf_pending_read_reload_service_uuid and reload_notification_name is None:
        parser.error("pending-read reload requires --reload-notification-name")

    if stress:
        completion_marker = "stress:complete:passed"
        failure_marker = "stress:failed:"
        mode = "stress"
    elif reload:
        completion_marker = "reload:complete:passed"
        failure_marker = "reload:failed:"
        mode = "reload"
    elif rf_service_uuid:
        completion_marker = "rf:passed:"
        failure_marker = "rf:failed:"
        mode = "rf-scan"
    elif rf_connect_service_uuid:
        completion_marker = "rf-connect:passed:"
        failure_marker = "rf-connect:failed:"
        mode = "rf-connect"
    elif rf_io_service_uuid:
        completion_marker = "rf-io:passed:"
        failure_marker = "rf-io:failed:"
        mode = "rf-io"
    elif rf_rssi_service_uuid:
        completion_marker = "rf-rssi:passed:"
        failure_marker = "rf-rssi:failed:"
        mode = "rf-rssi"
    elif rf_notification_service_uuid:
        completion_marker = "rf-notification:passed:"
        failure_marker = "rf-notification:failed:"
        mode = "rf-notification"
    elif rf_indication_service_uuid:
        completion_marker = "rf-indication:passed:"
        failure_marker = "rf-indication:failed:"
        mode = "rf-indication"
    elif rf_passive_disconnect_service_uuid:
        completion_marker = "rf-passive-disconnect:passed:"
        failure_marker = "rf-passive-disconnect:failed:"
        mode = "rf-passive-disconnect"
    elif rf_cancellation_service_uuid:
        completion_marker = "rf-cancellation:passed:"
        failure_marker = "rf-cancellation:failed:"
        mode = "rf-cancellation"
    elif rf_watchdog_service_uuid:
        completion_marker = "rf-watchdog:passed"
        failure_marker = "rf-watchdog:failed:"
        mode = "rf-watchdog"
    elif rf_lab_suite_service_uuid:
        completion_marker = (
            "rf-lab-suite:passed:flows=10:"
            if rf_lab_suite_flow is None
            else f"rf-lab-suite:flow-passed:index=0:mode={rf_lab_suite_flow}"
        )
        failure_marker = (
            "rf-lab-suite:failed:"
            if rf_lab_suite_flow is None
            else "rf-lab-suite:flow-failed:"
        )
        mode = "rf-lab-suite"
    elif rf_pending_read_reload_service_uuid:
        completion_marker = "rf-pending-read-reload:complete:passed"
        failure_marker = "rf-pending-read-reload:failed:"
        mode = "rf-pending-read-reload"
    elif rf_reload_service_uuid:
        completion_marker = "rf-reload:active:"
        failure_marker = "rf-reload:failed:"
        mode = "rf-reload"
    elif rf_multi_fault_service_uuids:
        completion_marker = "rf-multi-fault:passed:"
        failure_marker = "rf-multi-fault:failed:"
        mode = "rf-multi-fault"
    elif rf_multi_soak_service_uuids:
        completion_marker = "rf-multi-soak:passed:"
        failure_marker = "rf-multi-soak:failed:"
        mode = "rf-multi-soak"
    elif rf_multi_service_uuids:
        completion_marker = "rf-multi:passed:"
        failure_marker = "rf-multi:failed:"
        mode = "rf-multi"

    elif rf_recovery_service_uuid:
        completion_marker = "rf-recovery:close:passed:"
        failure_marker = "rf-recovery:failed:"
        mode = "rf-recovery"
    elif rf_gatt_service_uuid:
        completion_marker = "rf-gatt:passed:"
        failure_marker = "rf-gatt:failed:"
        mode = "rf-gatt"
    else:
        completion_marker = "close:resolved"
        failure_marker = "__no_failure_marker__"
        mode = "basic"

    if keep_app and mode not in (
        "basic",
        "stress",
        "reload",
        "rf-lab-suite",
        "rf-watchdog",
        "rf-cancellation",
    ):
        parser.error("--keep-app requires a supported runtime or controlled RF mode")
    xcrun = shutil.which("xcrun")
    if xcrun is None:
        raise SystemExit("xcrun is required")
    command = [
        xcrun,
        "devicectl",
        "device",
        "process",
        "launch",
        "--device",
        udid,
        "--terminate-existing",
        "--activate",
        "--console",
        "--json-output",
        str(launch_json),
        BUNDLE_ID,
    ]
    if mode == "basic":
        command.append("--blehub-nitro-basic")
    if stress:
        command.append("--blehub-nitro-stress")
    elif reload:
        command.append("--blehub-nitro-reload")
        if reload_notification_name is not None:
            command.extend(
                ["--blehub-nitro-reload-notification", reload_notification_name]
            )
    elif rf_service_uuid:
        command.extend(["--blehub-nitro-rf-service", rf_service_uuid])
    elif rf_connect_service_uuid:
        command.extend(["--blehub-nitro-rf-connect-service", rf_connect_service_uuid])
    elif rf_io_service_uuid:
        command.extend(["--blehub-nitro-rf-io-service", rf_io_service_uuid])
    elif rf_rssi_service_uuid:
        command.extend(["--blehub-nitro-rf-rssi-service", rf_rssi_service_uuid])
    elif rf_notification_service_uuid:
        command.extend(
            ["--blehub-nitro-rf-notification-service", rf_notification_service_uuid]
        )
    elif rf_indication_service_uuid:
        command.extend(
            ["--blehub-nitro-rf-indication-service", rf_indication_service_uuid]
        )
    elif rf_passive_disconnect_service_uuid:
        command.extend(
            [
                "--blehub-nitro-rf-passive-disconnect-service",
                rf_passive_disconnect_service_uuid,
            ]
        )
    elif rf_cancellation_service_uuid:
        command.extend(
            ["--blehub-nitro-rf-cancellation-service", rf_cancellation_service_uuid]
        )
    elif rf_watchdog_service_uuid:
        command.extend(["--blehub-nitro-rf-watchdog-service", rf_watchdog_service_uuid])
    elif rf_lab_suite_service_uuid:
        command.extend(
            [
                "--blehub-nitro-rf-lab-suite-service",
                rf_lab_suite_service_uuid,
            ]
        )
        if rf_lab_suite_flow is not None:
            command.extend(["--blehub-nitro-rf-lab-suite-flow", rf_lab_suite_flow])
        if rf_expected_local_name is not None:
            command.extend(
                ["--blehub-nitro-rf-expected-local-name", rf_expected_local_name]
            )
        if control_relay is not None:
            command.extend(
                [
                    "--blehub-nitro-rf-cancellation-control",
                    json.dumps(
                        {
                            "controls": [
                                asdict(control) for control in control_relay.controls
                            ]
                        }
                    ),
                ]
            )
    elif rf_pending_read_reload_service_uuid:
        command.extend(
            [
                "--blehub-nitro-rf-pending-read-reload-service",
                rf_pending_read_reload_service_uuid,
                "--blehub-nitro-rf-pending-read-reload-notification",
                reload_notification_name,
            ]
        )
    elif rf_reload_service_uuid:
        command.extend(["--blehub-nitro-rf-reload-service", rf_reload_service_uuid])
    elif rf_multi_fault_service_uuids:
        command.extend(
            ["--blehub-nitro-rf-multi-fault-service", rf_multi_fault_service_uuids]
        )
    elif rf_multi_soak_service_uuids:
        command.extend(
            [
                "--blehub-nitro-rf-multi-soak-service",
                rf_multi_soak_service_uuids,
                "--blehub-nitro-rf-multi-subscription-mode",
                rf_multi_subscription_mode,
                "--blehub-nitro-rf-multi-cycles",
                str(rf_multi_soak_cycles),
            ]
        )
    elif rf_multi_service_uuids:
        command.extend(
            [
                "--blehub-nitro-rf-multi-service",
                rf_multi_service_uuids,
                "--blehub-nitro-rf-multi-subscription-mode",
                rf_multi_subscription_mode,
            ]
        )

    elif rf_recovery_service_uuid:
        command.extend(["--blehub-nitro-rf-recovery-service", rf_recovery_service_uuid])
    elif rf_gatt_service_uuid:
        command.extend(["--blehub-nitro-rf-gatt-service", rf_gatt_service_uuid])

    if single_control is not None:
        command.extend(
            [
                "--blehub-nitro-rf-cancellation-control",
                json.dumps(asdict(single_control)),
                "--blehub-nitro-rf-expected-local-name",
                rf_expected_local_name,
            ]
        )

    coordination_paths: dict[str, Path] = {}
    if rf_multi_coordination_map is not None:
        try:
            document = json.loads(rf_multi_coordination_map.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                raise TypeError("coordination map must be an object")
            coordination_paths = {
                str(service_uuid): Path(str(path))
                for service_uuid, path in document.items()
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            parser.error(f"invalid --rf-multi-coordination-map: {error}")

    log.parent.mkdir(parents=True, exist_ok=True)
    launch_json.unlink(missing_ok=True)
    cleanup_receipt.unlink(missing_ok=True)
    log_file = log.open("w", encoding="utf-8")
    print(
        f"[rn-device] launch requested ({mode}); waiting for {completion_marker!r} "
        f"for at most {seconds:g}s (streaming {log})",
        file=sys.stderr,
        flush=True,
    )
    launch_started_at = time.monotonic()
    deadline = launch_started_at + seconds
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    if runtime_identity is not None:
        runtime_identity.begin_launch(launch_started_at, deadline)
    output_lines: deque[str] = deque(maxlen=FAILURE_TAIL_LINES)
    marker_seen = False
    completion_count = 0
    failure_marker_seen = False
    bounded_timeout = False
    close_seen = False
    close_grace_seconds = 5.0
    recovery_final_cycle = (
        "rf-recovery:cycle:2:cycle:passed:" if rf_recovery_service_uuid else None
    )
    selector = selectors.DefaultSelector()
    assert process.stdout is not None
    selector.register(process.stdout, selectors.EVENT_READ)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending_line = ""
    completion_on_line = False
    locked_seen = False
    soak_cycle = 1
    suite_coordination = (
        rf_suite_coordination.LineCoordination(rf_lab_suite_coordination_path)
        if rf_lab_suite_coordination_path is not None
        else None
    )
    relay_lines: list[str] = []
    close_deadline = deadline

    def controlled_complete() -> bool:
        if not marker_seen or failure_marker_seen or control_relay is None:
            return marker_seen
        if not control_relay.physical_complete():
            return False
        text = "".join(relay_lines)
        from tools.react_native.check_runtime_log import check_rf_lab_suite

        assert runtime_identity is not None
        if not runtime_identity.complete:
            require_console()
            runtime_identity.finish(text)
            require_console()
        check_rf_lab_suite(
            text,
            rf_lab_suite_service_uuid,
            log,
            expected_local_name=rf_expected_local_name,
            cancellation_evidence=control_relay.directory / "physical-evidence.json",
        )
        return True

    def require_console() -> None:
        if process.poll() is not None:
            raise RuntimeError(
                "owned iOS runtime console exited before identity/ACK verification"
            )
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "iOS suite deadline exceeded during runtime identity/relay verification"
            )

    def forward_control(control: Control, phase: str) -> None:
        assert runtime_identity is not None
        require_console()
        runtime_identity.before_signal(phase, control)
        require_console()
        relay_ios(udid, control, phase)
        runtime_identity.sent_signal(phase, control)
        require_console()

    def fail_control(error: BaseException) -> None:
        if control_relay is None:
            return
        assert runtime_identity is not None
        for cleanup in (
            lambda: runtime_identity.fail(error),
            lambda: abort_control(control_relay.root),
        ):
            try:
                cleanup()
            except (OSError, ValueError, RuntimeError) as cleanup_error:
                message = f"control failure recording/abort failed: {cleanup_error}"
                error.add_note(message)
                print(message, file=sys.stderr)

    def record_console_cleanup(action: str) -> None:
        cleanup_receipt.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "device": udid,
                    "bundle_id": BUNDLE_ID,
                    "helper_pid": os.getpid(),
                    "console_pid": process.pid,
                    "launch_json": str(launch_json),
                    "action": action,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def stop_process() -> None:
        if process.poll() is not None:
            record_console_cleanup("console-exited")
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            record_console_cleanup("console-exited")
            return
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            _ = process.wait()
        # Record forwarding, not a guarantee that the remote app has exited.
        # The watchdog owner must not send a second remote termination afterward.
        record_console_cleanup("signal-forwarded")

    def stop_console_preserving_app() -> None:
        """Detach the console without forwarding termination to RN."""
        if process.poll() is not None:
            record_console_cleanup("console-exited")
            return
        # `devicectl --console` forwards catchable signals to the remote app.
        # SIGKILL cannot be caught or forwarded, so this ends only the local
        # console bridge and leaves the final suite screen running on iOS.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            record_console_cleanup("console-exited")
            return
        _ = process.wait()
        record_console_cleanup("detached")

    def handle_line(line: str) -> None:
        nonlocal close_seen, close_deadline, completion_count

        nonlocal \
            completion_on_line, \
            marker_seen, \
            failure_marker_seen, \
            locked_seen, \
            soak_cycle
        if (
            mode == "rf-multi-soak"
            and "rf-multi-soak:cycle:" in line
            and ":started" in line
        ):
            soak_cycle = int(line.split("rf-multi-soak:cycle:", 1)[1].split(":", 1)[0])
        subscription_label = (
            "indication"
            if rf_multi_subscription_mode == "indication"
            else "notification"
        )
        if control_relay is not None and "BLEHUB_NITRO_RUNTIME " in line:
            assert runtime_identity is not None
            if not runtime_identity.complete:
                require_console()
            runtime_identity.observe(line)
            if not runtime_identity.complete:
                require_console()
            relay_lines.append(line)
            if (
                runtime_identity.sent
                == runtime_identity.acknowledged
                == runtime_identity.js_acknowledged
            ):
                control_relay.observe("".join(relay_lines))
        if suite_coordination is not None:
            try:
                suite_coordination.observe_line(line)
            except rf_suite_coordination.RfSuiteCoordinationError as error:
                raise RuntimeError(str(error)) from error
        for service_uuid, path in coordination_paths.items():
            target = path / f"cycle-{soak_cycle}" if mode == "rf-multi-soak" else path
            if (
                f"rf-multi:step:{subscription_label}-enable:passed:" in line
                and f"serviceUuid={service_uuid}" in line
            ):
                target.mkdir(parents=True, exist_ok=True)
                (target / "subscription-enabled").touch()
            if (
                f"rf-multi:step:{subscription_label}-disable:passed:" in line
                and f"serviceUuid={service_uuid}" in line
            ):
                target.mkdir(parents=True, exist_ok=True)
                (target / "subscription-disabled").touch()
        output_lines.append(line[-8192:])
        locked_seen |= "BSErrorCodeDescription = Locked" in line
        if (
            "BLEHUB_NITRO_RUNTIME" in line
            and (":step:" in line or ":passed:" in line or ":failed:" in line)
            and (
                mode != "rf-recovery"
                or ":cycle:passed:" in line
                or ":failed:" in line
                or "rf-recovery:passed:" in line
            )
        ):
            print(f"[rn-device] {line.strip()[:8192]}", file=sys.stderr, flush=True)
        if recovery_final_cycle and recovery_final_cycle in line and not close_seen:
            close_seen = True
            close_deadline = time.monotonic() + RECOVERY_CLOSE_GRACE_SECONDS
            print(
                f"[rn-device] final cycle observed; waiting at most {RECOVERY_CLOSE_GRACE_SECONDS:g}s for close",
                file=sys.stderr,
                flush=True,
            )
        elif (
            ":step:close:passed:" in line
            and not close_seen
            and mode != "rf-multi-soak"
            and not (mode == "rf-lab-suite" and rf_lab_suite_flow is None)
        ):
            close_seen = True
            close_deadline = time.monotonic() + close_grace_seconds
            print(
                f"[rn-device] close step observed; waiting up to {close_grace_seconds:g}s for final summary",
                file=sys.stderr,
                flush=True,
            )
        completion_matches = (
            re.search(r"BLEHUB_NITRO_RUNTIME rf-watchdog:passed\s*$", line) is not None
            if mode == "rf-watchdog"
            else completion_marker in line
        )
        if completion_matches and not completion_on_line:
            completion_count += 1
            completion_on_line = True
        failure_matches = (
            re.search(r"BLEHUB_NITRO_RUNTIME rf-watchdog:failed:", line) is not None
            if mode == "rf-watchdog"
            else failure_marker in line
        )
        runtime_failed = (
            failure_matches
            if mode in ("rf-lab-suite", "rf-watchdog")
            else "BLEHUB_NITRO_RUNTIME " in line and ":failed:" in line
        )
        if (
            completion_count >= (2 if rf_reload_service_uuid else 1)
            or runtime_failed
            or failure_matches
        ):
            marker_seen = True
            failure_marker_seen |= runtime_failed or failure_matches

    def consume(data: bytes, *, final: bool = False) -> None:
        nonlocal pending_line, completion_on_line
        text = decoder.decode(data, final=final)
        if text:
            log_file.write(text)
            log_file.flush()
            pending_line += text
        while "\n" in pending_line:
            line, pending_line = pending_line.split("\n", 1)
            handle_line(line + "\n")
            completion_on_line = False
        if len(pending_line) > 8192:
            handle_line(pending_line)
            pending_line = pending_line[-256:]
        if final and pending_line:
            handle_line(pending_line)
            pending_line = ""

    try:
        while process.poll() is None:
            remaining = deadline - time.monotonic()
            if (
                close_seen
                and completion_marker != "close:resolved"
                and mode != "rf-multi-soak"
            ):
                remaining = min(remaining, close_deadline - time.monotonic())
            if remaining <= 0:
                if runtime_identity is not None and runtime_identity.bootstrap is None:
                    runtime_identity.ready_for_relay("".join(relay_lines))
                bounded_timeout = True
                break
            events = selector.select(
                timeout=min(0.05 if control_relay is not None else 1.0, remaining)
            )
            for _, _ in events:
                chunk = os.read(process.stdout.fileno(), 4096)
                if chunk:
                    consume(chunk)
            # Preserve a launch/precondition exit and its queued output before
            # diagnosing a missing boot or touching the physical ARM barrier.
            if process.poll() is not None:
                break
            if control_relay is not None:
                assert runtime_identity is not None
                text = "".join(relay_lines)
                if runtime_identity.ready_for_relay(text):
                    advance_relay(control_relay, text, forward_control)
            if controlled_complete():
                break
    except BaseException as error:
        fail_control(error)
        stop_process()
        # Cleanup may flush devicectl launch diagnostics. Preserve them verbatim
        # without processing late runtime markers or replacing the primary error.
        try:
            while chunk := os.read(process.stdout.fileno(), 4096):
                log_file.write(decoder.decode(chunk))
            log_file.write(decoder.decode(b"", final=True))
        except OSError as capture_error:
            error.add_note(f"final devicectl output capture failed: {capture_error}")
        finally:
            process.stdout.close()
            log_file.close()
        raise
    finally:
        selector.close()

    if marker_seen:
        print(
            f"[rn-device] runtime marker observed; ending console capture early ({log})",
            file=sys.stderr,
            flush=True,
        )
        if keep_app and (
            (
                mode in ("rf-lab-suite", "rf-watchdog")
                and not (mode == "rf-watchdog" and failure_marker_seen)
            )
            or (mode in ("basic", "stress", "reload") and not failure_marker_seen)
        ):
            stop_console_preserving_app()
        else:
            stop_process()
    elif bounded_timeout:
        stop_process()
    else:
        _ = process.wait()
        record_console_cleanup("console-exited")

    try:
        was_seen = marker_seen
        while chunk := os.read(process.stdout.fileno(), 4096):
            consume(chunk)
        consume(b"", final=True)
    finally:
        process.stdout.close()
        log_file.close()
    if marker_seen and not was_seen:
        print(
            "[rn-device] final runtime marker recovered while draining console output",
            file=sys.stderr,
            flush=True,
        )
    elif bounded_timeout:
        if close_seen and completion_marker != "close:resolved":
            message = "[rn-device] terminal marker absent after the progress grace window; preserving attempt log for checker"
        else:
            message = f"[rn-device] console capture reached {seconds:g}s; preserving attempt log for checker"
        print(message, file=sys.stderr, flush=True)
    if not marker_seen and not bounded_timeout and process.returncode != 0:
        fail_control(subprocess.CalledProcessError(process.returncode, command))

        if locked_seen:
            print(
                "iOS device is locked; unlock it and rerun the selected RN device gate",
                file=sys.stderr,
            )
        tail = "".join(output_lines).splitlines()[-FAILURE_TAIL_LINES:]
        print(
            f"[rn-device] devicectl failed with exit code {process.returncode}; "
            f"last {len(tail)} lines (full log: {log}):",
            file=sys.stderr,
        )
        print("\n".join(tail), file=sys.stderr)
        return process.returncode or 1
    print(f"iOS RN device console log: {log}")
    return 1 if failure_marker_seen or not controlled_complete() else 0


if __name__ == "__main__":
    raise SystemExit(main())
