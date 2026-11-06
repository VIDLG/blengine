"""Offline ledger mutation samples from the real CCC provider writer/relay.

Synthetic BTP/App infrastructure stays test-only. Rehash edits deliberately so
ledger regressions challenge semantics rather than merely a stale digest.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from tests.peripherals.nrf52840 import ccc_enable_reject as protocol
from tests.peripherals.nrf52840.cancellation_control import (
    read_document,
    write_document,
)
from tests.peripherals.nrf52840.support.ccc_enable_reject_emulator import Harness


class Evidence:
    def __init__(
        self, root: Path, platform: str = "android", *, inline: bool = True
    ) -> None:
        self.phone = Harness(root, platform, inline=inline)
        self.phone.run()
        self.root, self.plan = self.phone.root, self.phone.plan
        self.control, self.platform = self.phone.control, platform
        self.text = self.phone.text
        self.original = read_document(self.root / protocol.PHYSICAL_NAME)

    def document(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.original))

    def publish(
        self, text: str | None = None, document: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        text = self.text if text is None else text
        document = self.document() if document is None else document
        encoded = text.encode("utf-8")
        for row in document["boundaries"]:
            if row["boundary"] not in (*protocol.PHASES, "closed"):
                continue
            if row["boundary"] == "closed":
                size = len(encoded)
            else:
                request = protocol.REQUESTS[protocol.PHASES.index(row["boundary"])]
                size = 0
                for line in text.splitlines(keepends=True):
                    size += len(line.encode("utf-8"))
                    if "rf-control:ready:" in line and line.rstrip("\n").endswith(
                        ":phase=" + request
                    ):
                        break
                size = min(size, len(encoded))
            row["runtime_bytes"] = size
            row["runtime_sha256"] = hashlib.sha256(encoded[:size]).hexdigest()
        write_document(self.root / protocol.PHYSICAL_NAME, document)
        return document


def row(document: dict[str, Any], name: str) -> dict[str, Any]:
    return next(entry for entry in document["boundaries"] if entry["boundary"] == name)


def event_line(text: str, kind: str, command: str | None = None) -> str:
    for line in text.splitlines():
        if ":event:" not in line:
            continue
        event = json.loads(line.split(":event:", 1)[1])
        if event.get("kind") == kind and event.get("commandId") == command:
            return line
    raise AssertionError(f"missing sample event {kind}/{command}")


def edit_event(
    text: str, event_kind: str, command: str | None = None, **fields: Any
) -> str:
    line = event_line(text, event_kind, command)
    event = json.loads(line.split(":event:", 1)[1])
    event.update(fields)
    replacement = (
        protocol.PREFIX + protocol.RUNTIME_MODE + ":event:" + json.dumps(event)
    )
    return text.replace(line, replacement, 1)
