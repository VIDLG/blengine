"""One official console launch, continuously pumped through pending-read reload.

Only local SDK children are owned here. No install/build/flash occurs. Reuse the
bounded SDK pump/process/payload primitives, not the old diagnostic HEX runner.
"""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tests.peripherals.nrf52840.cancellation_control import (
    read_document,
    write_document,
)
from tests.peripherals.nrf52840.fixture_session import read_live_document
from tests.peripherals.nrf52840.pending_reload import (
    PHYSICAL_NAME,
    PREFIX,
    PROGRESS_NAME,
    RuntimeFacts,
)
from tools.react_native import pending_reload_identity
from tools.react_native.capture_io import PrefixStream, stop_local
from tools.react_native.capture_transport import (
    BOOT_SECONDS,
    POLL_SECONDS,
    PROCESS_SECONDS,
    CaptureTransport,
)

RAW_PREFIX = "BLEHUB_NITRO_RUNTIME "
SCHEDULE = (("armed-1", "arm-1"), ("waiting-1", "reload"), ("armed-2", "arm-2"))


def projection(text: str, prefix: str) -> str:
    return "".join(line + "\n" for line in text.splitlines() if line.startswith(prefix))


def hashes(text: str) -> dict[str, str]:
    return {
        key: hashlib.sha256(value.encode()).hexdigest()
        for key, value in {
            "runtime": text,
            "pending": projection(text, PREFIX),
            "native_identity": projection(text, pending_reload_identity.HOST_PREFIX),
        }.items()
    }


class PendingCapture(CaptureTransport):
    """Specialize SDK ownership only; no recovery cycle protocol is reused."""

    def __init__(
        self,
        device: str,
        log: Path,
        coordination: Path,
        run_id: str,
        service_uuid: str,
        local_name: str,
        timeout: float,
        guard: Callable[[], None],
        install_json: Path,
    ) -> None:
        super().__init__(
            "ios",
            device,
            log,
            coordination,
            run_id,
            service_uuid,
            local_name,
            timeout,
            guard,
            install_json,
        )
        self.native = pending_reload_identity.NativeIdentity(run_id)
        self.pending_facts = RuntimeFacts(run_id, platform="ios")
        self.actions: list[str] = []
        self.requested: str | None = None
        self.progress_index = -1
        self.receipt = {
            "schema_version": 1,
            "platform": "ios",
            "run_id": run_id,
            "status": "capturing",
            "actions": [],
            "host_actions": self.actions,
            "budgets_seconds": {
                "whole": timeout,
                "bootstrap": BOOT_SECONDS,
                "command_outer": 1.5,
            },
        }

    def _ios_launch_arguments(self) -> list[str]:
        return [
            "--blehub-nitro-rf-pending-read-reload-service",
            self.service,
            "--blehub-nitro-rf-pending-read-reload-notification",
            f"dev.blehub.rn.reload.{self.run_id}",
            "--blehub-nitro-rf-pending-read-reload-run-id",
            self.run_id,
            "--blehub-nitro-rf-expected-local-name",
            self.name,
        ]

    def _new_stream(self) -> PrefixStream:
        return PrefixStream(self.log.with_suffix(".raw.log"), None, prefix=RAW_PREFIX)

    def _request_deadline(self) -> float:
        return self.deadline

    def _record(self) -> None:
        text = "".join(line + "\n" for line in self.lines)
        self.identity.update(
            pid=self.pid,
            boot_id=self.native.boot_id,
            owners={str(k): v for k, v in self.native.owners.items()},
            records=self.native.records,
            executable=self.executable,
            device_identifier=self.device_identifier,
            projection_sha256=hashes(text),
        )
        self.receipt.update(
            normalized_lines=len(self.lines),
            latest_progress=self.progress,
            projection_sha256=hashes(text),
        )
        write_document(self.log.with_suffix(".identity.json"), self.identity)
        write_document(self.log.with_suffix(".capture.json"), self.receipt)

    def _publish(self) -> None:
        text = "".join(line + "\n" for line in self.lines)
        self.log.write_text(text, encoding="utf-8")
        self.log.with_suffix(".native-identity.log").write_text(
            projection(text, pending_reload_identity.HOST_PREFIX), encoding="utf-8"
        )
        # Physical RuntimeFacts deliberately never sees the extra host JSON prefix.
        temporary = self.root / "runtime.log.tmp"
        temporary.write_text(projection(text, PREFIX), encoding="utf-8")
        temporary.replace(self.root / "runtime.log")
        self._record()

    def _poll(self) -> None:
        now = time.monotonic()
        if now >= self.deadline:
            raise TimeoutError("iOS pending reload absolute capture deadline exceeded")
        if now >= self.next_guard:
            self.guard()
            self.next_guard = now + PROCESS_SECONDS
        if self.stream is not None:
            lines = self.stream.poll()
            if lines:
                # Save the entire batch before validation, including rejected identities.
                self.lines.extend(lines)
                try:
                    for line in lines:
                        self.native.observe(line)
                        if line.startswith(PREFIX):
                            self.pending_facts.observe(line)
                        if line.startswith(pending_reload_identity.HOST_PREFIX):
                            event = self.native.records[-1]["payload"]
                            if event["event"] in (
                                "arm-url-accepted",
                                "reload-url-accepted",
                            ):
                                action = (
                                    "reload"
                                    if event["event"] == "reload-url-accepted"
                                    else f"arm-{event['generation']}"
                                )
                                if action != self.requested:
                                    raise RuntimeError(
                                        "unsolicited native payload URL acknowledgement"
                                    )
                        if self.native.pid is not None:
                            if self.pid is not None and self.pid != self.native.pid:
                                raise RuntimeError("pending reload native PID changed")
                            self.pid = self.native.pid
                finally:
                    self._publish()
        if self.native.pid is None and now >= self.boot_deadline:
            raise TimeoutError(
                "launch-anchored pending native identity deadline exceeded"
            )
        if self.process is not None and self.process.poll() is not None:
            raise RuntimeError("owned iOS pending console exited")
        progress = self.root / PROGRESS_NAME
        if progress.exists():
            value = read_live_document(progress)
            phases = (
                "armed-1",
                "waiting-1",
                "released-1",
                "armed-2",
                "waiting-2",
                "released-2",
                "closed",
            )
            if (
                value.get("run_id") != self.run_id
                or value.get("status") not in ("running", "passed")
                or value.get("phase") not in phases
            ):
                raise RuntimeError("foreign/failed pending physical progress")
            index = phases.index(value["phase"])
            if index < self.progress_index:
                raise RuntimeError("pending physical progress rewound")
            self.progress_index, self.progress = index, value
        physical = self.root / PHYSICAL_NAME
        if physical.exists() and read_document(physical).get("status") != "passed":
            raise RuntimeError("physical pending reload proof failed")

    def _send_pending(self, action: str) -> None:
        if self.pid is None:
            raise RuntimeError("control before native runtime identity")
        self._verify_process(f"{action}-before")
        self._poll()
        generation = 2 if action == "arm-2" else 1
        key = f"{generation}:"
        phase = {a: p for p, a in SCHEDULE}[action]
        if not self.progress or self.progress["phase"] != phase:
            raise RuntimeError("physical progress changed before control")
        if action == "reload":
            if "1:read-dispatched" not in self.pending_facts.seen or self.progress.get(
                "route"
            ) != self.pending_facts.routes.get(1):
                raise RuntimeError("reload without exact WAITING dispatched route")
        else:
            if key + "awaiting-arm" not in self.pending_facts.seen or self.progress.get(
                "route"
            ) != self.pending_facts.routes.get(generation):
                raise RuntimeError("arm without exact native ready route")
            if self.progress.get("arm_url") != f"blehub-rn-spike://arm/{self.run_id}":
                raise RuntimeError("foreign physical arm URL")
        url = f"blehub-rn-spike://{'reload' if action == 'reload' else 'arm'}/{self.run_id}"
        self.requested = action  # authorize before SDK launch can produce stdout
        request: dict[str, Any] = {
            "action": action,
            "url": url,
            "requested_at": time.monotonic(),
            "status": "requested",
        }
        self.receipt["actions"].append(request)
        self._record()
        self._relay(
            url, action
        )  # continuously pumped + official JSON/PID/executable checks
        event = "reload-url-accepted" if action == "reload" else "arm-url-accepted"
        ack_deadline = min(self.deadline, request["requested_at"] + 3)
        while (generation, event) not in self.native.events or (
            action != "reload"
            and key + "arm-js-observed" not in self.pending_facts.seen
        ):
            self._poll()
            if time.monotonic() >= ack_deadline:
                raise TimeoutError(
                    "pending payload native/JS acknowledgement deadline exceeded"
                )
            time.sleep(POLL_SECONDS)
        self.actions.append(action)
        request["status"] = "confirmed"
        self.requested = None
        self._record()

    def run_pending(self) -> tuple[str, tuple[str, ...]]:
        primary: BaseException | None = None
        try:
            self._launch()
            while True:
                self._poll()
                if self.pid is not None and time.monotonic() >= self.next_pid:
                    self._verify_process(
                        f"poll-{len(self.receipt.setdefault('process_checks', []))}"
                    )
                    self.receipt["process_checks"].append(time.monotonic())
                if (
                    len(self.actions) < 3
                    and self.progress
                    and self.progress["phase"] == SCHEDULE[len(self.actions)][0]
                ):
                    self._send_pending(SCHEDULE[len(self.actions)][1])
                if (
                    self.root / PHYSICAL_NAME
                ).exists() and "passed" in self.pending_facts.seen:
                    if self.actions != ["arm-1", "reload", "arm-2"]:
                        raise RuntimeError("incomplete pending host actions")
                    self.native.finish()
                    self._verify_process("completion")
                    self._poll()
                    self.native.finish()
                    self.identity["status"] = self.receipt["status"] = "passed"
                    break
                time.sleep(POLL_SECONDS)
        except BaseException as error:
            primary = error
            self.identity.update(status="failed", primary_error=str(error))
            self.receipt.update(status="failed", primary_error=str(error))
            raise
        finally:
            cleanup_error: BaseException | None = None
            try:
                if self.process is not None:
                    self.receipt["console_cleanup"] = stop_local(
                        self.process, console=True
                    )
            except BaseException as error:  # noqa: BLE001 - defer cancellation until files are closed and evidence published.
                cleanup_error = error
            finally:
                if self.output is not None:
                    try:
                        self.output.close()
                    except OSError as error:
                        if cleanup_error is None:
                            cleanup_error = error
                        else:
                            cleanup_error.add_note(f"output close: {error}")
            try:
                # Drain the final bytes after detaching, not just the last pre-kill poll.
                self.process = None
                if primary is None and cleanup_error is None:
                    self._poll()
                    self.native.finish()
            except BaseException as error:  # noqa: BLE001 - preserve failed final drain alongside the original failure.
                cleanup_error = error
            if cleanup_error is not None:
                self.identity.update(status="failed", cleanup_error=str(cleanup_error))
                self.receipt.update(status="failed", cleanup_error=str(cleanup_error))
            try:
                self._publish()
            except BaseException as error:  # noqa: BLE001 - publication must never replace the primary cancellation/failure.
                if cleanup_error is not None:
                    cleanup_error.add_note(f"publication: {error}")
                else:
                    cleanup_error = error
            if cleanup_error is not None:
                if primary is None:
                    raise cleanup_error
                primary.add_note(
                    f"pending capture cleanup/publication: {cleanup_error}"
                )
        return "".join(line + "\n" for line in self.lines), tuple(self.actions)


def capture(
    device: str,
    log: Path,
    coordination: Path,
    *,
    run_id: str,
    service_uuid: str,
    local_name: str,
    seconds: float,
    guard: Callable[[], None],
    install_json: Path,
) -> tuple[str, tuple[str, ...]]:
    if (
        type(seconds) not in (int, float)
        or not math.isfinite(seconds)
        or not 0 < seconds <= 300
    ):
        raise ValueError("pending capture timeout must be finite in (0, 300]")
    return PendingCapture(
        device,
        log,
        coordination,
        run_id,
        service_uuid,
        local_name,
        seconds,
        guard,
        install_json,
    ).run_pending()
