"""Deterministic ordering tests for the live system-loss provider relay."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.peripherals.nrf52840 import system_loss_attested as api


class SystemLossAttestedOrderingTests(unittest.TestCase):
    def test_live_ready_precedes_base_wait_and_arm(self) -> None:
        events: list[str] = []

        class Coordinator:
            def __init__(self, *_: object) -> None:
                events.append("coordinator-created")
                self.document = {"state": "disconnected-retired"}

            def arm(self, _: dict[str, object]) -> None:
                events.append("arm")

            def observe(self, _: dict[str, object]) -> None:
                events.append("observe")

            def authorize_off(
                self, _: dict[str, object], __: dict[str, object]
            ) -> None:
                events.append("authorize-off")

            def finish_failure(self, _: BaseException) -> None:
                events.append("finish-failure")

        def wait(path: Path, *, timeout: float = api.MAX_WAIT_SECONDS) -> dict[str, object]:
            del timeout
            events.append(f"wait:{path.name}")
            return {"state": "disconnected-retired"}

        with tempfile.TemporaryDirectory() as temporary, patch.object(
            api, "SystemLossCoordinator", Coordinator
        ), patch.object(api, "_wait", side_effect=wait):
            api.run(
                object(),
                Path(temporary),
                {"read-write": 39},
                on_ready=lambda: events.append("ready"),
            )

        self.assertEqual(
            events,
            [
                "ready",
                "wait:system-loss-base-witness.json",
                "wait:system-loss-control.json",
                "coordinator-created",
                "arm",
                "wait:system-loss-pending-witness.json",
                "observe",
                "wait:system-loss-off-request.json",
                "authorize-off",
                "wait:system-loss-finish.json",
            ],
        )


if __name__ == "__main__":
    unittest.main()
