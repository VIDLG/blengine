"""Strict iOS native-owner projection; never interprets synthetic rejection as RF.

Vocabulary is the authored PendingReloadSession/actual RCTInvalidating module,
not the retired diagnostic notification controller. Indices refer to the combined
normalized stream so native ownership remains auditable beside the five routes.
"""

from __future__ import annotations

import itertools
import json
import uuid
from typing import Any

from tests.peripherals.nrf52840.pending_reload import PREFIX

HOST_PREFIX = "BLEHUB_NITRO_RUNTIME rf-pending-reload-host:"


def _require(ok: bool, message: str) -> None:
    if not ok:
        raise RuntimeError(message)


def _uuid(value: Any) -> None:
    _require(
        type(value) is str and str(uuid.UUID(value)) == value,
        "invalid native identity UUID",
    )


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result, "duplicate native identity JSON key")
        result[key] = value
    return result


class NativeIdentity:
    """Incremental fail-closed host facts, plus final cross-prefix order checks."""

    def __init__(self, run_id: str) -> None:
        _uuid(run_id)
        self.run_id = run_id
        self.pid: int | None = None
        self.boot_id: str | None = None
        self.owners: dict[int, str] = {}
        self.events: dict[tuple[int, str], dict[str, Any]] = {}
        self.lines: list[str] = []
        self.records: list[dict[str, Any]] = []

    def observe(self, line: str) -> None:
        index = len(self.lines)
        self.lines.append(line)
        if line.startswith(PREFIX):
            return
        _require(line.startswith(HOST_PREFIX), "foreign pending identity prefix")
        value = json.loads(line[len(HOST_PREFIX) :], object_pairs_hook=_unique)
        _require(type(value) is dict, "native identity must be object")
        event = value.get("event")
        _require(event != "failed", f"native pending host failed: {value}")
        detail = event in ("arm-url-accepted", "reload-url-accepted")
        _require(
            set(value)
            == {"event", "runId", "pid", "bootId", "ownerId", "generation"}
            | ({"detail"} if detail else set()),
            "native identity fields",
        )
        _require(
            value["runId"] == self.run_id
            and type(value["pid"]) is int
            and value["pid"] > 0,
            "foreign native run/PID",
        )
        _uuid(value["bootId"])
        _uuid(value["ownerId"])
        if self.pid is None:
            self.pid, self.boot_id = value["pid"], value["bootId"]
        _require(
            value["pid"] == self.pid and value["bootId"] == self.boot_id,
            "native PID/boot changed",
        )
        generation = value["generation"]
        _require(type(generation) is int and generation in (1, 2), "native generation")
        key = (generation, event)
        _require(key not in self.events, "duplicate native host event")
        if event == "runtime-installed":
            _require(
                generation == len(self.owners) + 1
                and value["ownerId"] not in self.owners.values(),
                "native runtime installation/owner reused",
            )
            if generation == 2:
                _require(
                    (1, "reload-url-accepted") in self.events,
                    "second runtime installed before reload",
                )
            self.owners[generation] = value["ownerId"]
        else:
            _require(
                self.owners.get(generation) == value["ownerId"], "foreign native owner"
            )
            allowed = {"native-ready", "arm-url-accepted", "native-close-complete"}
            allowed |= (
                {"reload-url-accepted", "module-invalidation-entered"}
                if generation == 1
                else {"javascript-complete"}
            )
            _require(event in allowed, "unexpected native host event")
        if detail:
            action = "arm" if event == "arm-url-accepted" else "reload"
            _require(
                value["detail"] == f"blehub-rn-spike://{action}/{self.run_id}",
                "foreign native control URL",
            )
        record = {"marker_index": index, "payload": value}
        self.events[key] = record
        self.records.append(record)

    def finish(self) -> dict[str, Any]:
        def host(generation: int, event: str) -> int:
            _require(
                (generation, event) in self.events,
                f"missing native host {generation}:{event}",
            )
            return self.events[generation, event]["marker_index"]

        def marker(body: str) -> int:
            matches = [
                i
                for i, line in enumerate(self.lines)
                if line == PREFIX + body or line.startswith(PREFIX + body + ":")
            ]
            _require(
                len(matches) == 1, f"missing/duplicate identity-bound marker {body}"
            )
            return matches[0]

        def ordered(indices: list[int]) -> None:
            _require(
                all(a < b for a, b in itertools.pairwise(indices)),
                "native host/cross-prefix order",
            )

        for generation in (1, 2):
            p = f"generation={generation}:"
            ordered(
                [
                    host(generation, "runtime-installed"),
                    marker(p + "runtime-start"),
                    host(generation, "native-ready"),
                    marker(p + "native-ready:passed"),
                    marker(p + "awaiting-arm"),
                    host(generation, "arm-url-accepted"),
                    marker(p + "arm-js-observed"),
                    marker(p + "read-dispatched"),
                ]
            )
        ordered(
            [
                marker("generation=1:read-dispatched"),
                host(1, "reload-url-accepted"),
                host(1, "module-invalidation-entered"),
                marker("native:engine-invalidation-close-started"),
                marker("native:dispose-returned"),
                marker("native:late-submit-rejected"),
                marker("native:engine-close-complete"),
                host(1, "native-close-complete"),
                host(2, "native-ready"),
            ]
        )
        ordered([host(1, "reload-url-accepted"), host(2, "runtime-installed")])
        ordered(
            [
                marker("generation=2:read:passed"),
                host(2, "native-close-complete"),
                marker("generation=2:close:passed"),
                host(2, "javascript-complete"),
                marker(f"passed:runId={self.run_id}"),
            ]
        )
        return {
            "schema_version": 1,
            "status": "passed",
            "run_id": self.run_id,
            "pid": self.pid,
            "boot_id": self.boot_id,
            "owners": {str(k): v for k, v in self.owners.items()},
            "records": self.records,
        }


def verify(text: str, run_id: str) -> dict[str, Any]:
    facts = NativeIdentity(run_id)
    for line in text.splitlines():
        facts.observe(line)
    return facts.finish()
