"""Private parent lifecycle harness; capture/checker/bundle are explicit boundaries.

Bundle schema/replay belongs to the parallel parent slice. This test double
checks immutable candidate bytes without publishing a fake passed RF manifest.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

from tools.react_native import rf_evidence_bundle as evidence
from tools.react_native import write_reject_capture, write_reject_ledger
from tools.tests.react_native.support.recovery_parent import IDENTITY
from tools.tests.react_native.support.recovery_parent import Harness as ParentHarness


class Harness(ParentHarness):
    scenario = "write-reject"

    def __init__(self, root: Path, platform: str) -> None:
        super().__init__(root, platform)
        self.capture = create_autospec(
            write_reject_capture.capture, side_effect=self._write_capture
        )
        self.checker = create_autospec(
            write_reject_ledger.verify, side_effect=self._write_checker
        )
        self.api.write_reject_capture = SimpleNamespace(capture=self.capture)
        self.api.write_reject_ledger = SimpleNamespace(verify=self.checker)

    def _bind_bundle(self) -> None:
        h = self

        class BundleBoundary:
            def __init__(self, attempt, **kwargs):
                h.events.append("bundle")
                h.attempt, h.bundle = attempt, self
                assert kwargs["scenario"] == h.scenario
                if h.faults.constructor:
                    raise h.faults.constructor

            def prepare_artifact(self, source):
                h.events.append("freeze")
                if h.faults.freeze:
                    raise h.faults.freeze
                candidate = h.attempt / "candidate"
                candidate.mkdir()
                h.snapshot = candidate / source.name
                if source.is_dir():
                    shutil.copytree(source, h.snapshot)
                else:
                    shutil.copy2(source, h.snapshot)
                self.fingerprint = evidence._artifact(h.snapshot, h.platform)
                return h.snapshot

            def verify_artifact(self):
                h.events.append("artifact-verify")
                if h.faults.before_verify:
                    h.faults.before_verify()
                if evidence._artifact(h.snapshot, h.platform) != self.fingerprint:
                    raise ValueError("candidate drift")

            def finish(self, **kwargs):
                h.events.append("finish")
                h.finish_arguments = {
                    **kwargs,
                    "cleanup_errors": list(kwargs["cleanup_errors"]),
                }
                if h.faults.finish:
                    raise h.faults.finish

        self.api.EvidenceBundle = BundleBoundary

    def _write_capture(
        self,
        platform,
        device,
        log,
        coordination,
        run_id,
        service_uuid,
        expected_local_name,
        *,
        timeout,
        guard,
        install_json=None,
        fixture_identity,
    ):
        self.events.append("capture-enter")
        guard()
        assert fixture_identity == IDENTITY
        if self.faults.capture:
            raise self.faults.capture
        text = "synthetic opaque boundary; actual capture/checker have separate crossing tests\n"
        log.write_text(text)
        self.events.append("capture-return")
        if self.faults.after_capture:
            self.faults.after_capture()
        return text, ()

    def _write_checker(self, text, **kwargs):
        self.events.append("checker")
        assert kwargs["fixture_identity"] == IDENTITY
        assert "host_cycles" not in kwargs
        if self.faults.checker:
            raise self.faults.checker
        return {"scenario": self.scenario, "synthetic_checker_boundary": True}

    def run(self, *, timeout=12):
        return self.api.run(
            self.platform,
            "synthetic-device",
            self.artifact,
            self.contract,
            self.profile,
            timeout=timeout,
            scenario=self.scenario,
        )
