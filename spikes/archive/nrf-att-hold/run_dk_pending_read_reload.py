"""Isolated DK pending-ATT read / external RN reload experiment (no flashing).

Requires the already verified diagnostic image and signed RN host. Firmware status
proves callback entry, while RN markers independently prove the Central result.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import serial
from read_hold import (
    ABORT,
    GATT_SERVICE,
    RELEASE,
    HoldError,
    HoldState,
    Identity,
    ReadHold,
)
from run_dk_initial_read import (
    APP,
    FIXTURE_PID,
    FIXTURE_WORK,
    GAP,
    GAP_DEVICE_CONNECTED,
    GAP_DEVICE_DISCONNECTED,
    GATT,
    LOCK,
    PROFILE,
    ROOT,
    SERVICE,
    WORK,
    BtpCommandError,
    BtpSession,
    CoreCommands,
    FramedTransport,
    GapCommands,
    GattCommands,
    SerialByteTransport,
    detect_btp_port,
    load_profile,
    open_btp_port,
    provision,
    runtime_exit_reason,
    terminal_hold_state,
    uuid_to_le_bytes,
    validate_image,
    verify_probe,
)

PREFIX = "BLEHUB_NITRO_RUNTIME "
MODE = "rf-pending-read-reload"
OLD = (
    r"generation=1:read-dispatched:commandId=12008:connectionId=12004:"
    r"gattSessionId=12006:characteristicId=([1-9][0-9]*):peripheralId=([0-9]+)"
)
NEW = (
    r"generation=2:read:passed:commandId=13008:connectionId=13004:"
    r"gattSessionId=13006:characteristicId=([1-9][0-9]*):value=00"
)
AWAITING_ARM = (
    r"generation=2:awaiting-arm:commandId=13008:connectionId=13004:"
    r"gattSessionId=13006:characteristicId=([1-9][0-9]*)"
)
READ_DISPATCHED = (
    r"generation=2:read-dispatched:commandId=13008:connectionId=13004:"
    r"gattSessionId=13006:characteristicId=([1-9][0-9]*)"
)
HOLD_SECONDS = 5.0
FRESH_READ_TIMEOUT_SECONDS = 12.0
CAPTURE_SECONDS = 85
UUID_TEXT = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
NATIVE = (
    r"native:(?P<event>read-accepted|dispose-returned|late-submit-rejected):"
    rf"runId=(?P<run_id>{UUID_TEXT}):probeId=(?P<probe_id>{UUID_TEXT}):"
    r"commandId=12008:connectionId=12004:gattSessionId=12006:"
    r"characteristicId=(?P<characteristic>[1-9][0-9]*)"
)
NATIVE_EVENTS = ("read-accepted", "dispose-returned", "late-submit-rejected")


def markers(text: str) -> list[str]:
    return [
        line[len(PREFIX) :] for line in text.splitlines() if line.startswith(PREFIX)
    ]


def one(lines: list[str], expression: str) -> tuple[int, re.Match[str]]:
    matches = [
        (i, match)
        for i, line in enumerate(lines)
        if (match := re.fullmatch(expression, line))
    ]
    if len(matches) != 1:
        raise RuntimeError(f"expected exactly one {expression}, found {len(matches)}")
    return matches[0]


def native_boundaries(
    lines: list[str], *, run_id: str, complete: bool
) -> dict[str, tuple[int, re.Match[str]]]:
    """Parse and correlate the private native pending-read boundary markers."""
    if re.fullmatch(UUID_TEXT, run_id) is None or str(uuid.UUID(run_id)) != run_id:
        raise RuntimeError("run_id must be a canonical lowercase UUID")
    native_lines: list[tuple[int, re.Match[str]]] = []
    for index, line in enumerate(lines):
        if line.startswith(MODE + ":failed:native-boundary-"):
            raise RuntimeError("native boundary reported failure")
        if line.startswith(MODE + ":native:"):
            match = re.fullmatch(MODE + ":" + NATIVE, line)
            if match is None:
                raise RuntimeError("unknown or malformed native marker")
            native_lines.append((index, match))
    by_event: dict[str, list[tuple[int, re.Match[str]]]] = {
        event: [] for event in NATIVE_EVENTS
    }
    for index, match in native_lines:
        event = match.group("event")
        by_event[event].append((index, match))
        if match.group("run_id") != run_id:
            raise RuntimeError("native marker run_id changed")
        if re.fullmatch(UUID_TEXT, match.group("probe_id")) is None or str(
            uuid.UUID(match.group("probe_id"))
        ) != match.group("probe_id"):
            raise RuntimeError("native marker probe_id is not canonical")
        characteristic = match.group("characteristic")
        if str(int(characteristic)) != characteristic:
            raise RuntimeError("native marker characteristic ID is not canonical")
    if complete:
        if len(native_lines) != len(NATIVE_EVENTS) or any(
            len(entries) != 1 for entries in by_event.values()
        ):
            raise RuntimeError("expected exactly one of each native boundary marker")
    elif len(by_event["read-accepted"]) != 1:
        raise RuntimeError("expected exactly one native read-accepted marker")
    if any(len(entries) > 1 for entries in by_event.values()):
        raise RuntimeError("duplicate native boundary marker")
    if native_lines:
        probe_ids = {match.group("probe_id") for _, match in native_lines}
        characteristics = {match.group("characteristic") for _, match in native_lines}
        if len(probe_ids) != 1:
            raise RuntimeError("native marker probe_id changed")
        if len(characteristics) != 1:
            raise RuntimeError("native marker characteristic changed")
    return {event: entries[0] for event, entries in by_event.items() if entries}


def verify_ledger(
    text: str,
    *,
    run_id: str,
    post_steps: tuple[str, ...],
    old_characteristic: str | None = None,
) -> tuple[str, str]:
    """Require two confirmed host posts and exact ordered JS/native boundaries."""
    if post_steps != ("reload", "arm"):
        raise RuntimeError(
            "expected exactly two ordered, confirmed host notification posts"
        )
    lines = markers(text)
    for boundary in (
        "generation=1:scan:",
        "generation=1:connected:",
        "generation=1:gatt:",
        "generation=1:read-dispatched:",
        "external-ready",
        "notification-received",
        "generation=1:cleanup-started",
        "generation=2:listener:registered-before-initialize",
        "generation=2:scan:",
        "generation=2:connected:",
        "generation=2:gatt:",
        "generation=2:awaiting-arm:",
        "arm-ready",
        "arm-notification-received",
        "generation=2:read-dispatched:",
        "generation=2:read:",
        "complete:passed",
    ):
        if sum(line.startswith(MODE + ":" + boundary) for line in lines) != 1:
            raise RuntimeError(f"missing or duplicate RN boundary {boundary}")
    old_index, old = one(lines, MODE + ":" + OLD)
    native = native_boundaries(lines, run_id=run_id, complete=True)
    ready, _ = one(lines, MODE + ":external-ready")
    notified, _ = one(lines, MODE + ":notification-received")
    cleanup, _ = one(lines, MODE + ":generation=1:cleanup-started")
    new_listener, _ = one(
        lines, MODE + ":generation=2:listener:registered-before-initialize"
    )
    awaiting, awaited = one(lines, MODE + ":" + AWAITING_ARM)
    arm_ready, _ = one(lines, MODE + ":arm-ready")
    armed, _ = one(lines, MODE + ":arm-notification-received")
    dispatched, read_dispatch = one(lines, MODE + ":" + READ_DISPATCHED)
    new_index, new = one(lines, MODE + ":" + NEW)
    complete, _ = one(lines, MODE + ":complete:passed")
    first_char, peripheral = old.groups()
    accepted_index, accepted = native["read-accepted"]
    disposed_index, _ = native["dispose-returned"]
    late_index, _ = native["late-submit-rejected"]
    second_char = new.group(1)
    if old_characteristic is not None and first_char != old_characteristic:
        raise RuntimeError("first read characteristic changed after WAITING")
    if int(peripheral) > 0xFFFFFFFFFFFFFFFF:
        raise RuntimeError("invalid old peripheral ID")
    if not (
        accepted_index
        < old_index
        < ready
        < notified
        < cleanup
        < new_listener
        < awaiting
        < arm_ready
        < armed
        < dispatched
        < new_index
        < complete
        and cleanup < disposed_index < late_index < arm_ready
    ):
        raise RuntimeError(
            "reload, arm, read and completion boundaries are out of order"
        )
    if accepted.group("characteristic") != first_char:
        raise RuntimeError("native read characteristic changed before old dispatch")
    if awaited.group(1) != second_char or read_dispatch.group(1) != second_char:
        raise RuntimeError(
            "generation 2 characteristic changed across the arm handshake"
        )
    for generation, command, connection, gatt, before in (
        (1, "12007", "12004", "12006", old_index),
        (2, "13007", "13004", "13006", new_index),
    ):
        index, _ = one(
            lines,
            f"{MODE}:generation={generation}:gatt:passed:commandId={command}:"
            f"connectionId={connection}:gattSessionId={gatt}",
        )
        if index >= before or (generation == 2 and not notified < index < awaiting):
            raise RuntimeError("GATT discovery is not in the matching generation")
        scan, scanned = one(
            lines,
            f"{MODE}:generation={generation}:scan:passed:serviceUuid={re.escape(SERVICE)}:"
            r"peripheralId=([0-9]+):scanSessionId="
            + ("12001" if generation == 1 else "13001"),
        )
        connected, peer = one(
            lines,
            f"{MODE}:generation={generation}:connected:passed:commandId="
            + ("12005" if generation == 1 else "13005")
            + f":connectionId={connection}:peripheralId=([0-9]+)",
        )
        if not scan < connected < index or scanned.group(1) != peer.group(1):
            raise RuntimeError("scan, connection and discovery do not correlate")
        if generation == 1 and peer.group(1) != peripheral:
            raise RuntimeError("first read did not use the scanned peripheral")
    if any(":failed:" in line or ":close-rejected:" in line for line in lines):
        raise RuntimeError("RN reported failure")
    if any(
        line.startswith(f"{MODE}:generation=1:")
        and index > notified
        and not line.endswith(":cleanup-started")
        for index, line in enumerate(lines)
    ):
        raise RuntimeError("late first-generation marker after reload")
    if any(
        line.startswith(f"{MODE}:generation=1:read:")
        or (
            line.startswith(f"{MODE}:generation=2:")
            and re.search(
                r"(?:commandId|connectionId|gattSessionId|scanSessionId|subscriptionId)=1200[0-9](?:\D|$)",
                line,
            )
        )
        for line in lines
    ):
        raise RuntimeError("old read terminal or old ID leaked into generation 2")
    return first_char, second_char


def inspect_log(log: Path, process: subprocess.Popen[str]) -> list[str]:
    lines = markers(
        log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
    )
    if any(":failed:" in line or ":close-rejected:" in line for line in lines):
        raise RuntimeError(f"RN reported a failure: {log}")
    if process.poll() is not None and process.returncode != 0:
        raise RuntimeError(
            f"RN console exited {process.returncode} before gate completion: {log}"
        )
    return lines


def wait_marker(
    log: Path, process: subprocess.Popen[str], expression: str, deadline: float
) -> re.Match[str]:
    while time.monotonic() < deadline:
        lines = inspect_log(log, process)
        matches = [match for line in lines if (match := re.fullmatch(expression, line))]
        if len(matches) > 1:
            raise RuntimeError(f"duplicate RN marker {expression}")
        if matches:
            return matches[0]
        if process.poll() is not None:
            raise RuntimeError(f"RN console exited before {expression}: {log}")
        time.sleep(0.05)
    raise TimeoutError(f"missing RN marker {expression}: {log}")


def post_notification(
    xcrun: str,
    udid: str,
    name: str,
    log: Path,
    process: subprocess.Popen[str],
    stage: str,
    deadline: float,
    *,
    run_id: str,
) -> None:
    """Post the same Darwin name only at its matching RN/native barrier."""
    if stage not in ("reload", "arm"):
        raise ValueError(f"unknown notification post stage: {stage}")
    lines = inspect_log(log, process)
    required = (
        (MODE + ":external-ready", MODE + ":notification-received", 0)
        if stage == "reload"
        else (MODE + ":arm-ready", MODE + ":arm-notification-received", 1)
    )
    ready, received, previous_reload_count = required
    if lines.count(MODE + ":notification-received") != previous_reload_count:
        raise RuntimeError(f"{stage} notification has an unexpected first-post count")
    if lines.count(ready) != 1 or lines.count(received) != 0:
        raise RuntimeError(f"{stage} notification is not at its unique ready boundary")
    native = native_boundaries(lines, run_id=run_id, complete=stage == "arm")
    entered, _ = one(lines, MODE + ":" + (OLD if stage == "reload" else AWAITING_ARM))
    accepted_index, _ = native["read-accepted"]
    if accepted_index >= entered:
        raise RuntimeError("native read-accepted must precede old JS read dispatch")
    if stage == "reload" and any(
        event in native for event in ("dispose-returned", "late-submit-rejected")
    ):
        raise RuntimeError("native cleanup boundary occurred before reload post")
    if stage == "arm":
        disposed_index, _ = native["dispose-returned"]
        late_index, _ = native["late-submit-rejected"]
        ready_index = lines.index(ready)
        if not lines.index(MODE + ":generation=1:cleanup-started") < disposed_index:
            raise RuntimeError("native dispose boundary preceded old cleanup")
        if not lines.index(MODE + ":generation=1:cleanup-started") < late_index:
            raise RuntimeError("native late-submit boundary preceded old cleanup")
        if not disposed_index < late_index < ready_index:
            raise RuntimeError("native cleanup boundaries are out of order")
    if entered >= lines.index(ready):
        raise RuntimeError(
            f"{stage} notification ready before its matching read boundary"
        )
    if stage == "reload" and any(
        line.startswith(MODE + ":generation=2:") for line in lines
    ):
        raise RuntimeError("second generation started before the reload post")
    if stage == "arm" and any(
        line.startswith(
            (MODE + ":generation=2:read-dispatched:", MODE + ":generation=2:read:")
        )
        for line in lines
    ):
        raise RuntimeError("second read dispatched before the arm post")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"{stage} notification missed its ready deadline")
    subprocess.run(
        [
            xcrun,
            "devicectl",
            "device",
            "notification",
            "post",
            "--device",
            udid,
            "--name",
            name,
            "--timeout",
            "12",
            "--json-output",
            "-",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=min(18, remaining),
    )
    wait_marker(log, process, received, deadline)
    if time.monotonic() >= deadline:
        raise TimeoutError(f"{stage} notification received after its deadline")
    observed = inspect_log(log, process)
    if observed.count(received) != 1:
        raise RuntimeError(f"duplicate {stage} notification receipt")
    print(f"notification_post={stage}:name={name}:received=true", flush=True)


def confirm_fresh_waiting(
    hold: ReadHold,
    fresh: Identity,
    log: Path,
    process: subprocess.Popen[str],
    deadline: float,
) -> float:
    """Bound native read-dispatch and callback entry by one post-relative deadline."""
    try:
        wait_marker(log, process, MODE + ":" + READ_DISPATCHED, deadline)
        if hold.wait_until_held(deadline) != fresh:
            raise RuntimeError("generation 2 WAITING identity changed")
    except (TimeoutError, HoldError) as error:
        raise RuntimeError(
            f"generation 2 read did not enter WAITING within {FRESH_READ_TIMEOUT_SECONDS:g}s of arm notification: {error}"
        ) from error
    return time.monotonic()


def release_fresh(hold: ReadHold, fresh: Identity, waiting_at: float) -> None:
    """Recheck the exact held generation with a real elapsed-time guard."""
    if time.monotonic() >= waiting_at + 3.5:
        raise TimeoutError("fresh WAITING recheck started too late for safe RELEASE")
    snapshot = hold.status()
    if snapshot.identity != fresh or snapshot.state != HoldState.WAITING:
        raise RuntimeError("fresh read left its exact WAITING state before RELEASE")
    if time.monotonic() >= waiting_at + 3.5:
        raise TimeoutError("fresh WAITING recheck took too long for safe RELEASE")
    hold.release()
    terminal_hold_state(hold, HoldState.RELEASED, waiting_at + 4)


def gap_events(session: BtpSession) -> list[tuple[int, bytes]]:
    events: list[tuple[int, bytes]] = []
    while (event := session.pop_event()) is not None:
        if event.service == GAP and event.opcode in (
            GAP_DEVICE_CONNECTED,
            GAP_DEVICE_DISCONNECTED,
        ):
            if len(event.payload) < 7:
                raise RuntimeError("truncated BTP GAP peer identity")
            events.append((event.opcode, event.payload[:7]))
    return events


def wait_expired_and_disconnected(
    hold: ReadHold,
    session: BtpSession,
    peer: bytes,
    waiting_at: float,
    process: subprocess.Popen[str],
    log: Path,
) -> None:
    """Never arm a second hold while the first callback can still be live."""
    disconnected = 0
    while time.monotonic() < waiting_at + HOLD_SECONDS + 2:
        inspect_log(log, process)
        state = hold.status().state
        for opcode, address in gap_events(session):
            if opcode == GAP_DEVICE_DISCONNECTED and address == peer:
                disconnected += 1
            else:
                raise RuntimeError(
                    "unexpected or uncorrelated GAP event during old hold teardown"
                )
        if disconnected > 1:
            raise RuntimeError("duplicate old-peer GAP disconnect")
        if state == HoldState.EXPIRED and disconnected == 1:
            return
        if state not in (HoldState.WAITING, HoldState.EXPIRED):
            raise RuntimeError(f"old hold reached {state.name} instead of EXPIRED")
        time.sleep(0.05)
    raise TimeoutError("old hold did not expire and report its exact GAP disconnect")


def reject_stale_controls(
    session: BtpSession, hold: ReadHold, old: Identity, fresh: Identity
) -> None:
    if fresh.sequence == old.sequence or fresh.handle != old.handle:
        raise RuntimeError("fresh hold reused the old identity")
    for opcode in (RELEASE, ABORT):
        response = session.request(
            service=GATT_SERVICE, opcode=opcode, payload=old.encode()
        )
        if (
            response.service != GATT_SERVICE
            or response.index != 0
            or response.opcode != 0
            or response.payload != b"\x01"
        ):
            raise RuntimeError("firmware did not reject stale RELEASE/ABORT")
        if hold.status().state != HoldState.ARMED:
            raise RuntimeError("stale control changed or raced with the fresh arm")


def run(
    udid: str, serial_number: str, confirmed_sha: str, port: str | None = None
) -> None:
    if not re.fullmatch(r"[0-9A-Fa-f-]{25,40}", udid):
        raise ValueError("--udid must be an explicit physical device identifier")
    validate_image(confirmed_sha)
    if not APP.is_dir():
        raise RuntimeError(f"previously built iOS host app is missing: {APP}")
    if FIXTURE_PID.exists():
        raise RuntimeError(
            f"refusing to take over running/stale fixture: {FIXTURE_PID}"
        )
    FIXTURE_WORK.mkdir(parents=True, exist_ok=True)
    LOCK.mkdir()  # Exclusive; never remove a lock owned by another runner.
    process: subprocess.Popen[str] | None = None
    try:
        selected, probe = detect_btp_port(serial_number)
        if port is not None and port != selected:
            raise RuntimeError(
                f"--port {port} is not the uniquely responding BTP VCOM {selected}"
            )
        WORK.mkdir(parents=True, exist_ok=True)
        run_id = str(uuid.uuid4())
        attempt = uuid.UUID(run_id).hex
        log = WORK / f"rn-pending-read-reload-{attempt}.attempt.log"
        console = WORK / f"rn-pending-read-reload-{attempt}.runner.log"
        name = f"dev.blehub.rn.reload.{run_id}"
        print(f"pending_read_reload_attempt_log={log}:runner_log={console}", flush=True)
        xcrun = shutil.which("xcrun")
        if xcrun is None:
            raise RuntimeError("xcrun is unavailable")
        state = json.loads(
            subprocess.run(
                [
                    xcrun,
                    "devicectl",
                    "device",
                    "info",
                    "lockState",
                    "--device",
                    udid,
                    "--timeout",
                    "15",
                    "--json-output",
                    "-",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=25,
            ).stdout
        )["result"]
        if state["passcodeRequired"] or not state["unlockedSinceBoot"]:
            raise RuntimeError("unlock the iPhone before the pending-read experiment")
        subprocess.run(
            [
                xcrun,
                "devicectl",
                "device",
                "install",
                "app",
                "--device",
                udid,
                str(APP),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if verify_probe(serial_number) != probe:
            raise RuntimeError("J-Link USB identity changed before opening BTP session")
        with open_btp_port(selected) as connection:
            session = BtpSession(FramedTransport(SerialByteTransport(connection)))
            core = CoreCommands(session)
            for service in (GAP, GATT):
                try:
                    core.read_supported_commands(service)
                except BtpCommandError:
                    core.register_service(service)
            gap, gatt, hold = (
                GapCommands(session),
                GattCommands(session),
                ReadHold(session),
            )
            try:
                gatt.reset_server()
                profile = load_profile(PROFILE)
                profile = dataclasses.replace(
                    profile,
                    service_uuid=SERVICE,
                    characteristics=tuple(
                        dataclasses.replace(char, permissions=char.permissions | 0x40)
                        if char.role == "read-write"
                        else char
                        for char in profile.characteristics
                    ),
                )
                handles = provision(gatt, profile)
                attributes = gatt.get_attributes(
                    attribute_uuid=profile.characteristics[0].uuid
                )
                if (
                    len(attributes) != 1
                    or attributes[0].handle != handles["read-write"]
                    or attributes[0].permissions != 0x43
                ):
                    raise RuntimeError(
                        "diagnostic read-write handle/permissions did not match"
                    )
                for operation in (
                    lambda: gap.set_powered(True),
                    lambda: gap.set_connectable(True),
                    lambda: gap.set_discoverable(True),
                ):
                    with contextlib.suppress(BtpCommandError):
                        operation()
                gap.start_advertising(
                    {0x01: b"\x06", 0x07: uuid_to_le_bytes(SERVICE)},
                    {0x09: profile.local_name.encode("utf-8")},
                )
                old = hold.arm(handles["read-write"])
                if hold.status().state != HoldState.ARMED:
                    raise RuntimeError("first hold did not reach ARMED")
                if gap_events(session):
                    raise RuntimeError("stale GAP events before RN launch")
                post_steps: list[str] = []
                with console.open("x", encoding="utf-8") as output:
                    process = subprocess.Popen(
                        [
                            sys.executable,
                            str(ROOT / "tools/react_native/run_ios_device_runtime.py"),
                            udid,
                            str(log),
                            "--seconds",
                            str(CAPTURE_SECONDS),
                            "--rf-pending-read-reload-service-uuid",
                            SERVICE,
                            "--reload-notification-name",
                            name,
                        ],
                        cwd=ROOT,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        text=True,
                    )
                    # Require both independent boundaries, not just a JS dispatch marker.
                    wait_marker(log, process, MODE + ":" + OLD, time.monotonic() + 55)
                    try:
                        first_waiting = hold.wait_until_held(
                            time.monotonic() + FRESH_READ_TIMEOUT_SECONDS
                        )
                    except HoldError as error:
                        raise RuntimeError(
                            f"generation 1 read did not enter WAITING within {FRESH_READ_TIMEOUT_SECONDS:g}s of dispatch: {error}"
                        ) from error
                    if first_waiting != old:
                        raise RuntimeError("first WAITING identity changed")
                    waiting_at = time.monotonic()
                    peers = gap_events(session)
                    if len(peers) != 1 or peers[0][0] != GAP_DEVICE_CONNECTED:
                        raise RuntimeError(
                            "expected exactly one old-peer GAP connection"
                        )
                    if hold.status().state != HoldState.WAITING:
                        raise RuntimeError(
                            "first hold left WAITING before external reload"
                        )
                    wait_marker(
                        log, process, MODE + ":external-ready", waiting_at + 3.5
                    )
                    if hold.status().state != HoldState.WAITING:
                        raise RuntimeError(
                            "first hold left WAITING before devicectl post"
                        )
                    post_notification(
                        xcrun,
                        udid,
                        name,
                        log,
                        process,
                        "reload",
                        waiting_at + 3.5,
                        run_id=run_id,
                    )
                    post_steps.append("reload")
                    wait_expired_and_disconnected(
                        hold, session, peers[0][1], waiting_at, process, log
                    )
                    print(
                        f"old_hold=EXPIRED:sequence={old.sequence}:handle={old.handle}:gap_disconnected=true",
                        flush=True,
                    )
                    # Connectable legacy advertising ends on the first connection.
                    # A second JS engine cannot rediscover this Peripheral until
                    # the fixture explicitly starts advertising again.
                    gap.start_advertising(
                        {0x01: b"\x06", 0x07: uuid_to_le_bytes(SERVICE)},
                        {0x09: profile.local_name.encode("utf-8")},
                    )
                    print("fresh_generation_advertising=started", flush=True)
                    awaiting = wait_marker(
                        log, process, MODE + ":" + AWAITING_ARM, time.monotonic() + 30
                    )
                    wait_marker(log, process, MODE + ":arm-ready", time.monotonic() + 5)
                    lines = inspect_log(log, process)
                    if any(
                        line.startswith(
                            (
                                f"{MODE}:arm-notification-received",
                                f"{MODE}:generation=2:read-dispatched:",
                                f"{MODE}:generation=2:read:",
                            )
                        )
                        for line in lines
                    ):
                        raise RuntimeError(
                            "generation 2 read started before the host arm post"
                        )
                    fresh = hold.arm(old.handle)
                    if hold.status().state != HoldState.ARMED:
                        raise RuntimeError("fresh hold did not reach ARMED before read")
                    reject_stale_controls(session, hold, old, fresh)
                    if hold.status().state != HoldState.ARMED:
                        raise RuntimeError("fresh hold changed before arm notification")
                    new_peers = gap_events(session)
                    if len(new_peers) != 1 or new_peers[0][0] != GAP_DEVICE_CONNECTED:
                        raise RuntimeError(
                            "expected exactly one fresh-peer GAP connection"
                        )
                    post_notification(
                        xcrun,
                        udid,
                        name,
                        log,
                        process,
                        "arm",
                        time.monotonic() + 25,
                        run_id=run_id,
                    )
                    post_steps.append("arm")
                    fresh_waiting_at = confirm_fresh_waiting(
                        hold,
                        fresh,
                        log,
                        process,
                        time.monotonic() + FRESH_READ_TIMEOUT_SECONDS,
                    )
                    dispatched = one(
                        inspect_log(log, process), MODE + ":" + READ_DISPATCHED
                    )[1]
                    if dispatched.group(1) != awaiting.group(1):
                        raise RuntimeError(
                            "generation 2 read changed characteristic after arm"
                        )
                    release_fresh(hold, fresh, fresh_waiting_at)
                    print(
                        f"fresh_hold=RELEASED:sequence={fresh.sequence}:handle={fresh.handle}",
                        flush=True,
                    )
                    process.wait(timeout=CAPTURE_SECONDS + 15)
                if process.returncode != 0:
                    raise RuntimeError(runtime_exit_reason(console, log))
                first_char, second_char = verify_ledger(
                    log.read_text(encoding="utf-8"),
                    run_id=run_id,
                    post_steps=tuple(post_steps),
                )
            finally:
                errors: list[str] = []
                try:
                    snapshot = hold.status()
                    if snapshot.state in (HoldState.ARMED, HoldState.WAITING):
                        hold.abort()
                        terminal_hold_state(
                            hold, HoldState.ABORTED, time.monotonic() + 2
                        )
                    elif snapshot.state in (
                        HoldState.PREPARING,
                        HoldState.RELEASE_REQUESTED,
                        HoldState.ABORT_REQUESTED,
                    ):
                        raise RuntimeError(
                            f"unsafe hold cleanup state: {snapshot.state.name}"
                        )
                except Exception as error:  # noqa: BLE001 - cleanup must still stop advertising
                    errors.append(f"hold: {error}")
                try:
                    gap.stop_advertising()
                except Exception as error:  # noqa: BLE001 - preserve both cleanup failures
                    errors.append(f"advertising: {error}")
                if process is not None and process.poll() is None:
                    try:
                        process.wait(timeout=CAPTURE_SECONDS + 20)
                    except subprocess.TimeoutExpired:
                        errors.append(
                            f"runtime still live: pid={process.pid}; retaining fixture lock"
                        )
                if errors:
                    raise RuntimeError("cleanup unverified: " + "; ".join(errors))
            print(
                f"pending_read_reload=passed:old={old.sequence}:fresh={fresh.sequence}:read_characteristics={first_char},{second_char}:log={log}"
            )
    finally:
        if process is None or process.poll() is not None:
            LOCK.rmdir()
        else:
            print(f"runtime still live: retaining {LOCK}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--udid", required=True)
    parser.add_argument("--probe-serial", required=True)
    parser.add_argument("--confirm-hex-sha256", required=True)
    parser.add_argument(
        "--port", help="optional assertion against auto-detected BTP VCOM"
    )
    args = parser.parse_args()
    try:
        run(args.udid, args.probe_serial, args.confirm_hex_sha256, args.port)
    except (
        EOFError,
        OSError,
        RuntimeError,
        ValueError,
        subprocess.SubprocessError,
        serial.SerialException,
    ) as error:
        print(f"DK pending-read reload failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
