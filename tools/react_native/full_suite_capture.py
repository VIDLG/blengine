"""One capture owner/pump for continuous10, physical handoff and RecoveryTen."""

from __future__ import annotations

import json
import re
import shlex
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from tests.peripherals.nrf52840 import full_suite as protocol
from tests.peripherals.nrf52840.cancellation_control import SuiteControlRelay
from tests.peripherals.nrf52840.fixture_session import read_live_document
from tools.react_native import full_suite_ledger as ledger
from tools.react_native import recovery_ten_capture as recovery
from tools.react_native.capture_io import PrefixStream
from tools.react_native.rf_suite_coordination import advance_prefix

PREFIX = protocol.PREFIX


def _publish_text(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(text)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class _Capture(recovery._Capture):
    def __init__(
        self, *args: Any, fixture_identity: tuple[int, int], **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self.suite_root = self.root
        self.root = self.suite_root / "recovery"
        for path in (
            self.root / "runtime.log",
            self.log.parent / "recovery-ten-runtime.log",
        ):
            if path.is_symlink() or path.exists() and path.stat().st_size:
                raise ValueError(f"full capture refuses stale projection: {path}")
        self.fixture_identity = fixture_identity
        self.relay = SuiteControlRelay(self.suite_root)
        self.raw_lines: list[str] = []
        self.normalized: list[str] = []
        self.control_bootstrap: dict[str, Any] | None = None
        self.control_signals: list[dict[str, Any]] = []
        self.control_actions: list[dict[str, Any]] = []
        self.control_js: list[str] = []
        self.control_pending: tuple[Any, str] | None = None
        self.begin: dict[str, Any] | None = None
        self.begin_native = self.begin_js = False
        self.base_passed = False
        self.handoff: dict[str, Any] | None = None
        self.nonce: str | None = None
        self.full_started = self.full_closed = self.full_passed = self.awaiting = False
        self.suite_phases: list[str] = []
        self.receipt["scenario"] = self.identity["scenario"] = "full-suite"

    def _android_launch_arguments(self) -> list[str]:
        values = super()._android_launch_arguments()
        values[values.index("rf-recovery-ten")] = "rf-lab-suite"
        return [
            *values,
            "--es",
            "blehub.rfCancellationControl",
            shlex.quote(self._controls()),
        ]

    def _ios_launch_arguments(self) -> list[str]:
        values = super()._ios_launch_arguments()
        values[0] = "--blehub-nitro-rf-lab-suite-service"
        return [*values, "--blehub-nitro-rf-cancellation-control", self._controls()]

    def _controls(self) -> str:
        return json.dumps({"controls": [asdict(c) for c in self.relay.controls]})

    def _new_stream(self) -> PrefixStream:
        return PrefixStream(
            self.log.with_suffix(".raw.log"),
            self.pid if self.platform == "android" else None,
            prefix=PREFIX,
            raw=True,
        )

    def _record(self) -> None:
        self.receipt.update(
            base_status="passed" if self.base_passed else "running",
            control_bootstrap=self.control_bootstrap,
            control_actions=self.control_actions,
            control_signals=self.control_signals,
            control_js_acknowledged=self.control_js,
            begin=self.begin,
            handoff=self.handoff,
            fixture_identity={
                "pid": self.fixture_identity[0],
                "pgid": self.fixture_identity[1],
            },
            runtime_nonce=self.nonce,
        )
        super()._record()

    def text(self) -> str:
        return "".join(line + "\n" for line in self.raw_lines)

    def _publish(self) -> None:
        _publish_text(self.log, self.text())
        _publish_text(
            self.suite_root / "runtime.log", "".join(l + "\n" for l in self.normalized)
        )
        _publish_text(self.root / "runtime.log", "".join(l + "\n" for l in self.lines))
        _publish_text(
            self.log.parent / "recovery-ten-runtime.log",
            "".join(l + "\n" for l in self.raw_lines if recovery.PREFIX in l),
        )
        self._record()

    def _request_deadline(self) -> float:
        if (
            self.control_pending is not None
            or self.begin is not None
            and not self.begin_js
        ) and self.sent_at is not None:
            return min(super()._request_deadline(), self.sent_at + recovery.ACK_SECONDS)
        return super()._request_deadline()

    def _ack_deadline(self) -> None:
        super()._ack_deadline()
        if time.monotonic() >= self._request_deadline():
            raise TimeoutError(
                "full-suite native/JS ACK exceeded original hard 2s deadline"
            )

    def _observe(self, raw: str) -> None:
        line = raw[raw.index(PREFIX) :]
        marker = line.removeprefix(PREFIX)
        self.raw_lines.append(raw)
        self.normalized.append(line)
        if re.search(
            r"(?:^|:)(?:failed|flow-failed|host-failed)(?::|$)|:close:rejected:", marker
        ):
            raise RuntimeError(f"full-suite runtime failed: {marker}")
        if marker.startswith("rf-control:") and (
            self.control_bootstrap is None or self.facts.bootstrap is None
        ):
            raise RuntimeError("native shared bootstrap missing before control/ARM")
        if line.startswith(recovery.PREFIX):
            if (
                not marker.startswith("rf-recovery-ten:native:bootstrap:")
                and not self.begin_js
            ):
                raise RuntimeError("recovery began before host begin authorization/ACK")
            super()._observe(line)
            if self.facts.nonce is not None and self.facts.nonce != self.nonce:
                raise RuntimeError("full-suite/recovery JS nonce changed")
        elif marker.startswith("rf-host:bootstrap:"):
            value = json.loads(marker.removeprefix("rf-host:bootstrap:"))
            ledger.require(type(value) is dict, "native control bootstrap object")
            expected = {
                **asdict(self.relay.controls[0]),
                "pid": self.pid,
                "bundle_id": recovery.ANDROID_PACKAGE
                if self.platform == "android"
                else recovery.BUNDLE_ID,
                "executable": self.executable,
                "boot_id": value.get("boot_id"),
            }
            if (
                self.control_bootstrap is not None
                or time.monotonic() >= self.boot_deadline
            ):
                raise RuntimeError("duplicate/late control bootstrap")
            # iOS console is the initial PID authority; recovery bootstrap corroborates it.
            if self.pid is None:
                expected["pid"] = value.get("pid")
            ledger.same(value, expected, "control bootstrap")
            ledger.native_identity(value)
            self.control_bootstrap = value
        elif marker.startswith("rf-host:signal:"):
            self._ack_deadline()
            if (
                self.control_pending is None
                or self.control_bootstrap is None
                or len(self.control_signals) != len(self.control_js)
            ):
                raise RuntimeError("early/duplicate native control ACK")
            control, phase = self.control_pending
            value = json.loads(marker.removeprefix("rf-host:signal:"))
            ledger.same(
                value,
                {**self.control_bootstrap, **asdict(control), "phase": phase},
                "native control ACK",
            )
            self.control_signals.append(value)
            self.control_actions[-1]["native_ack_at"] = time.monotonic()
        elif marker.startswith("rf-control:observed:"):
            self._ack_deadline()
            if (
                self.control_pending is None
                or len(self.control_signals) != len(self.control_js) + 1
            ):
                raise RuntimeError("unsolicited JS control ACK or JS before native ACK")
            control, phase = self.control_pending
            if marker != control.marker("observed", phase):
                raise RuntimeError("foreign/out-of-order JS control ACK")
            self.control_js.append(marker)
            self.control_actions[-1]["js_ack_at"] = time.monotonic()
            self.control_pending = None
        elif marker.startswith("rf-lab-full:native:signal:"):
            self._ack_deadline()
            if self.begin is None or self.begin_native:
                raise RuntimeError("early/duplicate full-suite native begin ACK")
            value = json.loads(marker.removeprefix("rf-lab-full:native:signal:"))
            ledger.same(
                value,
                {
                    **(self.facts.bootstrap or {}),
                    "source": "payload-url",
                    "url": ledger.begin_url(self.run_id),
                },
                "begin ACK",
            )
            self.begin.update(native_signal=value, native_ack_at=time.monotonic())
            self.begin_native = True
        elif marker.startswith("rf-lab-full:recovery-ready:"):
            self._ack_deadline()
            if (
                self.begin is None
                or not self.begin_native
                or self.begin_js
                or marker
                != f"rf-lab-full:recovery-ready:runId={self.run_id}:source=linking"
            ):
                raise RuntimeError("early/foreign/duplicate full-suite JS begin ACK")
            self.begin["js_ack_at"] = time.monotonic()
            self.begin_js = True
        elif marker.startswith("rf-lab-full:recovery-awaiting:"):
            if (
                marker != f"rf-lab-full:recovery-awaiting:runId={self.run_id}"
                or self.awaiting
                or self.begin is not None
                or self.suite_phases != ["start", "close"]
                or self.normalized.count(PREFIX + protocol.BASE_PASSED) != 1
            ):
                raise RuntimeError(
                    "early/duplicate/foreign full-suite recovery awaiting"
                )
            self.awaiting = True
        elif marker.startswith(("rf-lab-full:runtime:", "rf-lab-suite:runtime:")):
            match = re.fullmatch(
                r"rf-lab-(?:full|suite):runtime:session=([^:]+):nonce=([0-9a-z-]+):phase=(start|close)",
                marker,
            )
            if (
                match is None
                or match[1] != self.run_id
                or self.nonce is not None
                and match[2] != self.nonce
            ):
                raise RuntimeError("full-suite session/JS nonce drift")
            if self.facts.bootstrap is None or self.control_bootstrap is None:
                raise RuntimeError(
                    "native bootstraps missing before full-suite runtime"
                )
            if marker.startswith("rf-lab-full:"):
                if match[3] == "start":
                    if self.full_started or self.suite_phases:
                        raise RuntimeError("second full-suite runtime")
                    self.full_started = True
                else:
                    if (
                        not self.full_started
                        or self.full_closed
                        or not self.begin_js
                        or "passed" not in self.facts.seen
                    ):
                        raise RuntimeError("early/duplicate full-suite runtime close")
                    self.full_closed = True
            else:
                expected_phase = "start" if not self.suite_phases else "close"
                if (
                    not self.full_started
                    or self.full_closed
                    or len(self.suite_phases) >= 2
                    or match[3] != expected_phase
                ):
                    raise RuntimeError("base runtime restart/phase ordering drift")
                self.suite_phases.append(match[3])
            self.nonce = match[2]
        elif marker.startswith("rf-lab-full:passed:"):
            if (
                marker != f"rf-lab-full:passed:flows=10:cycles=10:runId={self.run_id}"
                or not self.full_closed
                or self.full_passed
            ):
                raise RuntimeError("early/duplicate/foreign full-suite terminal")
            self.full_passed = True
        elif marker.startswith("rf-lab-full:"):
            raise RuntimeError("unknown full-suite protocol marker")
        if self.facts.bootstrap is not None and self.control_bootstrap is not None:
            ledger.same(
                {
                    k: self.control_bootstrap[k]
                    for k in ("pid", "boot_id", "bundle_id", "executable")
                },
                {
                    k: self.facts.bootstrap[k]
                    for k in ("pid", "boot_id", "bundle_id", "executable")
                },
                "control/recovery shared native boot",
            )

    def _heartbeat(self) -> None:
        latest = next(
            (
                line.removeprefix(PREFIX + "rf-lab-suite:")
                for line in reversed(self.normalized)
                if line.startswith(PREFIX + "rf-lab-suite:flow-")
            ),
            "awaiting-bootstrap",
        )
        stage = "recovery" if self.begin_js else "handoff" if self.awaiting else "base"
        print(
            f"rf-lab-full-capture:{self.platform}:stage={stage}:base={latest}:"
            f"cycle={self.facts.cycle}:phase={self.progress.get('phase') if self.progress else 'pending'}:"
            f"controls={len(self.control_js)}/{sum(len(c.phases) for c in self.relay.controls)}:"
            f"recovery-acks={len(self.js_acks)}/10",
            flush=True,
        )

    def _progress(self) -> None:
        super()._progress()
        path = self.suite_root / protocol.PHYSICAL_NAME
        if path.exists() and read_live_document(path).get("status") == "failed":
            raise RuntimeError("full-suite physical proof failed")
        path = self.suite_root / protocol.HANDOFF_NAME
        if self.handoff is not None:
            ledger.same(read_live_document(path), self.handoff, "immutable handoff")

    def _advance_controls(self) -> None:
        text = "".join(l + "\n" for l in self.normalized)
        advance_prefix(text, self.suite_root)
        self.relay.observe(text)
        if self.control_pending is None and not self.base_passed:
            pending = self.relay.pending_control()
            if pending is not None:
                owner, phase = pending
                self._verify_process(f"control-{len(self.control_actions)}-before")
                self._poll()
                if self.control_bootstrap is None:
                    raise RuntimeError("control bootstrap missing before signal/ARM")
                owner.forwarded(phase)
                self.control_pending = owner.control, phase
                self.sent_at = time.monotonic()
                action = {
                    "control": asdict(owner.control),
                    "phase": phase,
                    "requested_at": self.sent_at,
                }
                self.control_actions.append(action)
                self._relay(
                    owner.control.uri(phase),
                    f"control-{len(self.control_actions) - 1}",
                    notification=owner.control.notification(phase),
                )
                return
        if self.begin is not None:
            return
        awaiting = f"{PREFIX}rf-lab-full:recovery-awaiting:runId={self.run_id}"
        handoff_path = self.suite_root / protocol.HANDOFF_NAME
        if (
            not self.awaiting
            or awaiting not in self.normalized
            or not handoff_path.exists()
        ):
            return
        if not self.relay.physical_complete() or self.control_pending is not None:
            raise RuntimeError("handoff before base physical/control closure")
        self.guard()
        self.handoff = read_live_document(handoff_path)
        ledger.check_handoff(
            self.handoff, self.run_id, self.fixture_identity, self.suite_root
        )
        self.base_passed = True
        self._record()
        ledger.check_base(
            self.text(), self.log, self.suite_root, self.service, self.name
        )
        self._verify_process("begin-before")
        self._poll()
        self.guard()
        self.sent_at = time.monotonic()
        self.begin = {
            "url": ledger.begin_url(self.run_id),
            "requested_at": self.sent_at,
        }
        self._relay(self.begin["url"], "begin")
        self._record()

    def _finished(self) -> bool:
        if not super()._finished():
            return False
        text = "".join(l + "\n" for l in self.normalized)
        if (
            f"{PREFIX}rf-lab-full:passed:flows=10:cycles=10:runId={self.run_id}\n"
            not in text
        ):
            return False
        path = self.suite_root / protocol.PHYSICAL_NAME
        if not path.exists():
            return False
        ledger.check_final_physical(
            read_live_document(path),
            self.handoff,
            self.suite_root,
            self.run_id,
            self.fixture_identity,
        )
        return self.begin_js and self.base_passed


def capture(
    *args: Any, fixture_identity: tuple[int, int], **kwargs: Any
) -> tuple[str, tuple[int, ...]]:
    """Use the strict standalone preconditions with the composed capture owner."""
    # The factory keeps concurrent captures independent; no class substitution.
    return recovery.capture(
        *args,
        owner=lambda *a, **k: _Capture(*a, fixture_identity=fixture_identity, **k),
        **kwargs,
    )
