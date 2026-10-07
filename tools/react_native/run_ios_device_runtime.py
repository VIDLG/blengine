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
from pathlib import Path
from typing import cast

from tools.react_native.host_contract import BUNDLE_ID

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
    _ = mode.add_argument("--rf-recovery-service-uuid")
    _ = mode.add_argument("--rf-recovery-ten-service-uuid")
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
    rf_recovery_service_uuid = cast(str | None, args.rf_recovery_service_uuid)
    rf_recovery_ten_service_uuid = cast(str | None, args.rf_recovery_ten_service_uuid)
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
    elif rf_recovery_ten_service_uuid:
        completion_marker = "rf-recovery-ten:close:passed:"
        failure_marker = "rf-recovery-ten:failed:"
        mode = "rf-recovery-ten"
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
        BUNDLE_ID,
    ]
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
    elif rf_recovery_ten_service_uuid:
        command.extend(
            ["--blehub-nitro-rf-recovery-ten-service", rf_recovery_ten_service_uuid]
        )
    elif rf_recovery_service_uuid:
        command.extend(["--blehub-nitro-rf-recovery-service", rf_recovery_service_uuid])
    elif rf_gatt_service_uuid:
        command.extend(["--blehub-nitro-rf-gatt-service", rf_gatt_service_uuid])

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
    log_file = log.open("w", encoding="utf-8")
    print(
        f"[rn-device] launch requested ({mode}); waiting for {completion_marker!r} "
        f"for at most {seconds:g}s (streaming {log})",
        file=sys.stderr,
        flush=True,
    )
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    output_lines: deque[str] = deque(maxlen=FAILURE_TAIL_LINES)
    marker_seen = False
    completion_count = 0
    failure_marker_seen = False
    bounded_timeout = False
    close_seen = False
    close_grace_seconds = 5.0
    recovery_final_cycle = (
        "rf-recovery-ten:cycle:9:cycle:passed:"
        if rf_recovery_ten_service_uuid
        else "rf-recovery:cycle:2:cycle:passed:"
        if rf_recovery_service_uuid
        else None
    )
    selector = selectors.DefaultSelector()
    assert process.stdout is not None
    selector.register(process.stdout, selectors.EVENT_READ)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending_line = ""
    completion_on_line = False
    locked_seen = False
    soak_cycle = 1
    deadline = time.monotonic() + seconds
    close_deadline = deadline

    def stop_process() -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            _ = process.wait()

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
                mode not in ("rf-recovery", "rf-recovery-ten")
                or ":cycle:passed:" in line
                or ":failed:" in line
                or "rf-recovery:passed:" in line
                or "rf-recovery-ten:passed:" in line
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
            ":step:close:passed:" in line and not close_seen and mode != "rf-multi-soak"
        ):
            close_seen = True
            close_deadline = time.monotonic() + close_grace_seconds
            print(
                f"[rn-device] close step observed; waiting up to {close_grace_seconds:g}s for final summary",
                file=sys.stderr,
                flush=True,
            )
        if completion_marker in line and not completion_on_line:
            completion_count += 1
            completion_on_line = True
        runtime_failed = "BLEHUB_NITRO_RUNTIME " in line and ":failed:" in line
        if (
            completion_count >= (2 if rf_reload_service_uuid else 1)
            or runtime_failed
            or failure_marker in line
        ):
            marker_seen = True
            failure_marker_seen |= runtime_failed or failure_marker in line

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
                bounded_timeout = True
                break
            events = selector.select(timeout=min(1.0, remaining))
            if not events:
                continue
            for _, _ in events:
                chunk = os.read(process.stdout.fileno(), 4096)
                if chunk:
                    consume(chunk)
            if marker_seen:
                break
    except BaseException:
        stop_process()
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
        stop_process()
    elif bounded_timeout:
        stop_process()
    else:
        _ = process.wait()

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
    return 1 if failure_marker_seen or not marker_seen else 0


if __name__ == "__main__":
    raise SystemExit(main())
