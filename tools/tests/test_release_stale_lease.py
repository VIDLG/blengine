"""Contract for the bounded stale-phone-lease release entry."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from tools.android import release_stale_lease as entry

DEVICE = "TESTPHYSICAL42"
RUN = "12345678-1234-4234-8234-123456789abc"


class StaleLeaseReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root.mkdir(exist_ok=True)
        self.lock = self.root / entry.hashlib.sha256(DEVICE.encode()).hexdigest()

    def write_owner(self, *, pid: int, run_id: str = RUN) -> None:
        self.lock.mkdir(mode=0o700)
        (self.lock / "owner.json").write_text(
            json.dumps({"device": DEVICE, "run_id": run_id, "host_pid": pid}),
            encoding="utf-8",
        )

    @staticmethod
    def dead_pid() -> int:
        """A pid that existed and is gone (a reaped child), never a live process."""
        child = os.fork()
        if child == 0:  # pragma: no cover -- the child exits immediately
            os._exit(0)
        os.waitpid(child, 0)
        deadline = 0
        while deadline < 100:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                return child
            deadline += 1
        raise RuntimeError("could not obtain a provably dead pid")

    def test_releases_a_dead_owner_and_keeps_the_receipt(self):
        pid = self.dead_pid()
        self.write_owner(pid=pid)
        owner = (self.lock / "owner.json").read_bytes()
        receipt = entry.release_stale(
            device=DEVICE, expected_run_id=RUN, lock_root=self.root
        )
        self.assertFalse(self.lock.exists())
        self.assertEqual(receipt["host_pid"], pid)
        self.assertEqual(
            receipt["owner_sha256"], entry.hashlib.sha256(owner).hexdigest()
        )
        published = json.loads(
            (self.root / f"stale-release-{RUN}.json").read_text(encoding="utf-8")
        )
        self.assertEqual(published, receipt)

    def test_refuses_a_live_owner(self):
        self.write_owner(pid=os.getpid())
        with self.assertRaisesRegex(RuntimeError, "still running"):
            entry.release_stale(device=DEVICE, expected_run_id=RUN, lock_root=self.root)
        self.assertTrue((self.lock / "owner.json").is_file())

    def test_refuses_a_different_run_id(self):
        self.write_owner(pid=self.dead_pid())
        with self.assertRaisesRegex(RuntimeError, "differs"):
            entry.release_stale(
                device=DEVICE, expected_run_id="other-run", lock_root=self.root
            )
        self.assertTrue((self.lock / "owner.json").is_file())

    def test_refuses_unexpected_entries(self):
        self.write_owner(pid=self.dead_pid())
        (self.lock / "foreign.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "unexpected entries"):
            entry.release_stale(device=DEVICE, expected_run_id=RUN, lock_root=self.root)
        self.assertTrue((self.lock / "owner.json").is_file())

    def test_missing_lease_is_not_a_silent_success(self):
        with self.assertRaisesRegex(RuntimeError, "no phone lease"):
            entry.release_stale(device=DEVICE, expected_run_id=RUN, lock_root=self.root)


if __name__ == "__main__":
    unittest.main()
