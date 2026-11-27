"""Restoration-only archival boundaries; no device, SDK or radio operations."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.react_native import ios_radio_lease as api
from tools.tests.react_native.support.ios_adapter_startup_samples import DEVICE, RUN


class QuarantineRestorationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.output = self.root / "recovery"
        self.output.mkdir()
        self.lease = api.IosRadioLease(
            DEVICE, RUN, self.root / "journal.json", lock_root=self.root / "locks"
        )
        self.lease.acquire()
        self.assertTrue(self.lease.finish(None, None))
        self.owner_path = self.lease.lock / "owner.json"
        owner = json.loads(self.owner_path.read_bytes())
        owner["host_pid"] = 999999999
        self.owner_path.write_text(json.dumps(owner))
        journal = json.loads(self.lease.path.read_bytes())
        journal["host_pid"] = owner["host_pid"]
        self.lease.path.write_text(json.dumps(journal))
        stamp = time.time() - 60
        os.utime(self.lease.path, (stamp, stamp))
        self.owner_raw = self.owner_path.read_bytes()
        self.journal_raw = self.lease.path.read_bytes()
        self.owner_hash = hashlib.sha256(self.owner_raw).hexdigest()
        self.journal_hash = hashlib.sha256(self.journal_raw).hexdigest()
        self.proof = {
            "scope": "ios-radio-restoration-inspection-only",
            "device": DEVICE,
            "owner_sha256": self.owner_hash,
            "original_journal_sha256": self.journal_hash,
            "inspected_switch_value": "1",
            "observed_start_time": time.time() - 10,
            "observed_finish_time": time.time() - 5,
            "files": [],
        }
        self.collect = self.enterContext(
            patch.object(api, "collect_inspection", return_value=self.proof)
        )
        self.verify = self.enterContext(patch.object(api, "verify_inspection"))
        self.kill = self.enterContext(
            patch.object(api.os, "kill", side_effect=ProcessLookupError)
        )
        self.killpg = self.enterContext(
            patch.object(api.os, "killpg", side_effect=ProcessLookupError)
        )

    def restore(self, **changes: object) -> Path:
        arguments = {
            "original_status": "quarantined",
            "journal": self.lease.path,
            "inspection_input": self.root / "input.json",
            "output": self.output,
            "owner_sha256": self.owner_hash,
            "journal_sha256": self.journal_hash,
            "authorize_restoration_only": True,
            "lock_root": self.lease.lock_root,
        }
        arguments.update(changes)
        return api.archive_phone_owner(DEVICE, RUN, **arguments)

    def orphaned_held(self, **changes: object) -> dict:
        """Construct an interrupted synthetic attempt, never rewrite a live journal."""
        document = json.loads(self.journal_raw)
        document.update(
            schema_version=1,
            status="held",
            error=None,
            restoration="unverified",
            released_monotonic=None,
        )
        self.lease.path.write_text(json.dumps(document))
        stamp = time.time() - 60
        os.utime(self.lease.path, (stamp, stamp))
        self.journal_raw = self.lease.path.read_bytes()
        self.journal_hash = hashlib.sha256(self.journal_raw).hexdigest()
        self.proof["original_journal_sha256"] = self.journal_hash
        capture = self.root / "original.capture.json"
        document = {
            "schema_version": 1,
            "scope": "ios-adapter-startup-window-candidate",
            "platform": "ios",
            "device": DEVICE,
            "run_id": RUN,
            "status": "running",
            "capture_pid": 999999998,
        }
        document.update(changes)
        capture.write_text(json.dumps(document))
        os.utime(capture, (stamp, stamp))
        capture_hash = hashlib.sha256(capture.read_bytes()).hexdigest()
        preflight = self.root / "orphaned-held-preflight.json"
        boundary = time.time() - 30
        preflight.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "scope": "ios-radio-orphaned-held-preflight-only",
                    "device": DEVICE,
                    "original_run_id": RUN,
                    "owner_sha256": self.owner_hash,
                    "journal_sha256": self.journal_hash,
                    "capture_sha256": capture_hash,
                    "host_pid": 999999999,
                    "capture_pid": document["capture_pid"],
                    "checked_at": boundary,
                }
            )
        )
        os.utime(preflight, (boundary, boundary))
        return {
            "original_status": "held",
            "orphaned_capture": capture,
            "orphaned_capture_sha256": capture_hash,
            "orphaned_preflight": preflight,
            "orphaned_preflight_sha256": hashlib.sha256(
                preflight.read_bytes()
            ).hexdigest(),
        }

    def test_orphaned_held_archival_preserves_running_evidence_and_distinct_scope(
        self,
    ) -> None:
        arguments = self.orphaned_held()
        capture_raw = arguments["orphaned_capture"].read_bytes()
        with self.assertRaisesRegex(RuntimeError, "explicitly selected"):
            self.restore()  # No implicit admission of held owners via quarantine mode.
        self.collect.assert_not_called()
        self.assert_held()
        receipt = json.loads(self.restore(**arguments).read_bytes())
        archive = Path(receipt["archive_path"])
        self.assertEqual((archive / "owner.json").read_bytes(), self.owner_raw)
        self.assertEqual(self.lease.path.read_bytes(), self.journal_raw)
        self.assertEqual(arguments["orphaned_capture"].read_bytes(), capture_raw)
        self.assertEqual(
            (self.output / "original-capture.json").read_bytes(), capture_raw
        )
        self.assertEqual(receipt["scope"], "ios-radio-orphaned-held-restoration-only")
        self.assertEqual(receipt["original_journal_status"], "held")
        self.assertIs(receipt["original_attempt_cleanup_proven"], False)
        self.assertIs(receipt["rf_gate_passed"], False)
        self.assertEqual(
            receipt["original_capture"]["sha256"], arguments["orphaned_capture_sha256"]
        )
        self.killpg.assert_called_with(999999998, 0)

    def test_held_preparation_precedes_inspection_and_does_not_release_owner(
        self,
    ) -> None:
        arguments = self.orphaned_held()
        arguments["orphaned_preflight"].unlink()
        arguments.pop("orphaned_preflight_sha256")
        path = self.restore(**arguments, prepare_only=True, inspection_input=None)
        self.collect.assert_not_called()
        self.assert_held()
        self.assertFalse(any(self.output.iterdir()))
        self.assertEqual(path, arguments["orphaned_preflight"])
        with self.assertRaises(FileExistsError):
            self.restore(**arguments, prepare_only=True, inspection_input=None)
        arguments["orphaned_preflight_sha256"] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        with self.assertRaisesRegex(RuntimeError, "fresh and later"):
            self.restore(
                **arguments
            )  # ON while old executors could still act is insufficient.
        self.assert_held()
        self.assertFalse(any(self.output.iterdir()))
        now = time.time()
        self.proof.update(
            observed_start_time=now + 0.01, observed_finish_time=now + 0.02
        )
        with patch.object(api.time, "time", return_value=now + 0.03):
            receipt = json.loads(self.restore(**arguments).read_bytes())
        self.assertEqual(
            receipt["orphaned_preflight"]["sha256"],
            arguments["orphaned_preflight_sha256"],
        )
        self.assertEqual(
            (self.output / "orphaned-held-preflight.json").read_bytes(),
            path.read_bytes(),
        )

    def test_held_requires_explicit_authorization_and_matching_capture(self) -> None:
        arguments = self.orphaned_held()
        for changes in (
            {"authorize_restoration_only": False},
            {"original_status": "released"},
            {"orphaned_capture": None},
            {"orphaned_capture_sha256": None},
            {"orphaned_capture_sha256": "00" * 32},
            {"orphaned_preflight": None},
            {"orphaned_preflight_sha256": "00" * 32},
        ):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                self.restore(**(arguments | changes))
            self.assert_held()
        for changes in (
            {"run_id": "22222222-2222-4222-8222-222222222222"},
            {"device": "00008110-0000000000000000"},
            {"capture_pid": True},
            {"capture_pid": 999999999},
            {"scope": "ios-adapter-handshake"},
            {"status": "passed"},
        ):
            arguments = self.orphaned_held(**changes)
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(RuntimeError, "capture identity/status"),
            ):
                self.restore(**arguments)
            self.assert_held()
        self.collect.assert_not_called()

    def test_held_partial_finalization_is_not_reclassified_as_an_orphan(self) -> None:
        for changes in (
            {"error": "interrupted finalization"},
            {"restoration": "on-readback"},
            {"released_monotonic": 123.0},
            {"schema_version": True},
        ):
            arguments = self.orphaned_held()
            document = json.loads(self.journal_raw)
            document.update(changes)
            self.lease.path.write_text(json.dumps(document))
            digest = hashlib.sha256(self.lease.path.read_bytes()).hexdigest()
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(RuntimeError, "explicitly selected"),
            ):
                self.restore(**arguments, journal_sha256=digest)
            self.assertTrue(self.lease.lock.is_dir())
        self.collect.assert_not_called()

    def test_held_live_capture_or_descendant_group_blocks_before_inspection(
        self,
    ) -> None:
        arguments = self.orphaned_held()
        for role in ("capture", "group", "unqueryable"):
            self.kill.side_effect = (
                [ProcessLookupError, None] if role == "capture" else ProcessLookupError
            )
            self.killpg.side_effect = (
                PermissionError("unqueryable") if role == "unqueryable" else None
            )
            with (
                self.subTest(role=role),
                self.assertRaises((RuntimeError, PermissionError)),
            ):
                self.restore(**arguments)
            self.assert_held()
        self.collect.assert_not_called()

    def test_held_capture_drift_or_late_live_group_blocks_archival_commit(self) -> None:
        arguments = self.orphaned_held()

        def drift(*_args: object, **_kwargs: object) -> dict:
            os.utime(arguments["orphaned_capture"], None)
            return self.proof

        self.collect.side_effect = drift
        with self.assertRaisesRegex(RuntimeError, "capture timestamp drift"):
            self.restore(**arguments)
        self.assert_held()
        self.assertFalse(any(self.lease.lock_root.glob("restored-*")))
        arguments = self.orphaned_held()
        self.collect.side_effect = None

        def change(*_args: object, **kwargs: object) -> None:
            if kwargs.get("added_files") is not None:
                self.killpg.side_effect = None

        self.verify.side_effect = change
        with self.assertRaisesRegex(RuntimeError, "process group is still alive"):
            self.restore(**arguments)
        self.assert_held()
        self.assertFalse(any(self.lease.lock_root.glob("restored-*")))
        self.assertTrue(
            (
                self.lease.lock_root / (self.lease.lock.name + ".restoration-pending")
            ).exists()
        )
        self.assertEqual(
            (self.output / "original-capture.json").read_bytes(),
            arguments["orphaned_capture"].read_bytes(),
        )

    def assert_held(self) -> None:
        self.assertTrue(self.lease.lock.is_dir())
        self.assertEqual(self.lease.path.read_bytes(), self.journal_raw)

    def test_on_evidence_archives_original_owner_without_rewriting_failed_journal(
        self,
    ) -> None:
        receipt = json.loads(self.restore().read_bytes())
        archive = Path(receipt["archive_path"])
        self.assertFalse(self.lease.lock.exists())
        self.assertEqual((archive / "owner.json").read_bytes(), self.owner_raw)
        self.assertEqual(self.lease.path.read_bytes(), self.journal_raw)
        self.assertEqual(list(api._inode(archive.stat())), receipt["archive_identity"])
        self.assertEqual(receipt["status"], "verified-on-awaiting-archive-commit")
        self.assertIs(receipt["rf_gate_passed"], False)
        self.assertEqual(receipt["original_journal_status"], "quarantined")
        self.assertEqual(
            (self.output / "original-owner.json").read_bytes(), self.owner_raw
        )
        self.assertEqual(
            (self.output / "original-phone-journal.json").read_bytes(), self.journal_raw
        )
        fresh = api.IosRadioLease(
            DEVICE, RUN, self.root / "new-journal.json", lock_root=self.lease.lock_root
        )
        fresh.acquire()
        new_owner = (fresh.lock / "owner.json").read_bytes()
        with self.assertRaises(RuntimeError):
            self.restore(output=self.root / "second-recovery")
        self.assertEqual((fresh.lock / "owner.json").read_bytes(), new_owner)
        self.assertEqual((archive / "owner.json").read_bytes(), self.owner_raw)

    def test_native_system_loss_quarantine_can_archive_after_independent_on_proof(
        self,
    ) -> None:
        owner = json.loads(self.owner_path.read_bytes())
        owner["scope"] = api.NATIVE_SYSTEM_LOSS_SCOPE
        self.owner_path.write_text(json.dumps(owner))
        journal = json.loads(self.lease.path.read_bytes())
        journal["scope"] = api.NATIVE_SYSTEM_LOSS_SCOPE
        self.lease.path.write_text(json.dumps(journal))
        self.owner_raw = self.owner_path.read_bytes()
        self.journal_raw = self.lease.path.read_bytes()
        self.owner_hash = hashlib.sha256(self.owner_raw).hexdigest()
        self.journal_hash = hashlib.sha256(self.journal_raw).hexdigest()
        self.proof["owner_sha256"] = self.owner_hash
        self.proof["original_journal_sha256"] = self.journal_hash
        stamp = time.time() - 60
        os.utime(self.lease.path, (stamp, stamp))
        self.proof["observed_start_time"] = time.time() - 10
        self.proof["observed_finish_time"] = time.time() - 5
        receipt = json.loads(self.restore(native_system_loss=True).read_bytes())
        self.assertFalse(self.lease.lock.exists())
        self.assertEqual(receipt["scope"], "ios-native-system-loss-restoration-only")
        self.assertIs(receipt["rf_gate_passed"], False)

    def test_authorization_hashes_and_original_identity_fail_before_collection(
        self,
    ) -> None:
        for changes in (
            {"authorize_restoration_only": False},
            {"owner_sha256": "00" * 32},
            {"journal_sha256": "00" * 32},
            {"owner_sha256": "not-a-hash"},
        ):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                self.restore(**changes)
            self.assert_held()
        self.collect.assert_not_called()
        owner = json.loads(self.owner_raw)
        owner["run_id"] = "22222222-2222-4222-8222-222222222222"
        self.owner_path.write_text(json.dumps(owner))
        digest = hashlib.sha256(self.owner_path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(RuntimeError, "foreign quarantine"):
            self.restore(owner_sha256=digest)
        self.collect.assert_not_called()

    def test_live_or_unqueryable_original_owner_cannot_be_taken_over(self) -> None:
        for effect in (None, PermissionError("not ours")):
            self.kill.side_effect = effect
            with (
                self.subTest(effect=effect),
                self.assertRaises((RuntimeError, PermissionError)),
            ):
                self.restore()
            self.assert_held()
        self.collect.assert_not_called()

    def test_proof_must_be_on_for_exact_owner_and_later_than_quarantine(self) -> None:
        mutations = (
            ("scope", "rf-pass"),
            ("device", "00008110-0000000000000000"),
            ("owner_sha256", "00" * 32),
            ("original_journal_sha256", "00" * 32),
            ("inspected_switch_value", "0"),
            ("observed_start_time", time.time() - 120),
            ("observed_finish_time", time.time() - 4000),
            ("observed_finish_time", float("nan")),
            ("observed_finish_time", time.time() + 30),
        )
        for key, value in mutations:
            original = self.proof[key]
            self.proof[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(RuntimeError):
                self.restore()
            self.assert_held()
            self.proof[key] = original
        self.assertFalse((self.output / "restoration-release.json").exists())

    def test_recovery_root_mutex_blocks_new_acquisition_and_second_restorer(
        self,
    ) -> None:
        def during_collection(*_args: object, **_kwargs: object) -> dict:
            competitor = api.IosRadioLease(
                DEVICE,
                RUN,
                self.root / "competitor.json",
                lock_root=self.lease.lock_root,
            )
            with self.assertRaises(BlockingIOError):
                competitor.acquire()
            with self.assertRaises(BlockingIOError):
                self.restore()
            self.assertFalse(competitor.path.exists())
            return self.proof

        self.collect.side_effect = during_collection
        self.restore()
        self.assertFalse(self.lease.lock.exists())

    def test_collection_or_receipt_or_commit_failure_keeps_original_quarantine(
        self,
    ) -> None:
        self.collect.side_effect = RuntimeError("invalid original XCTest evidence")
        with self.assertRaisesRegex(RuntimeError, "invalid original"):
            self.restore()
        self.assert_held()
        self.collect.side_effect = None
        with (
            patch.object(api.os, "fsync", side_effect=OSError("disk full")),
            self.assertRaisesRegex(OSError, "disk full"),
        ):
            self.restore()
        self.assert_held()
        for path in self.output.iterdir():
            path.unlink()
        with (
            patch.object(api.os, "rename", side_effect=OSError("archive failed")),
            self.assertRaisesRegex(OSError, "archive failed"),
        ):
            self.restore()
        self.assert_held()
        receipt = json.loads((self.output / "restoration-release.json").read_bytes())
        self.assertFalse(Path(receipt["archive_path"]).exists())
        self.assertEqual(receipt["status"], "verified-on-awaiting-archive-commit")

    def test_commit_rechecks_exports_and_expiry_after_receipt_io(self) -> None:
        self.verify.side_effect = [None, RuntimeError("post-collection proof drift")]
        with self.assertRaisesRegex(RuntimeError, "post-collection proof drift"):
            self.restore()
        self.assert_held()
        self.assertEqual(self.verify.call_count, 2)
        added = self.verify.call_args.kwargs["added_files"]
        self.assertEqual(
            set(added),
            {
                "original-owner.json",
                "original-phone-journal.json",
                "restoration-release.json",
            },
        )
        for name, raw in added.items():
            self.assertEqual((self.output / name).read_bytes(), raw)
        for path in self.output.iterdir():
            path.unlink()
        pending = self.lease.lock_root / (self.lease.lock.name + ".restoration-pending")
        self.assertTrue(pending.exists())
        pending.unlink()  # Reset only this synthetic failure fixture, never a live lease.
        self.verify.side_effect = None
        now = time.time()
        with (
            patch.object(api.time, "time", side_effect=[now, now + 4000]),
            self.assertRaisesRegex(RuntimeError, "expired before archival commit"),
        ):
            self.restore()
        self.assert_held()

    def test_failed_root_sync_rolls_archive_back_before_new_owner_can_enter(
        self,
    ) -> None:
        original_sync = api.os.fsync
        failed = False

        def sync(descriptor: int) -> None:
            nonlocal failed
            if (
                api._inode(os.fstat(descriptor))
                == api._inode(self.lease.lock_root.stat())
                and not self.lease.lock.exists()
                and not failed
            ):
                failed = True
                raise OSError("root commit failed")
            original_sync(descriptor)

        with (
            patch.object(api.os, "fsync", side_effect=sync),
            self.assertRaisesRegex(OSError, "root commit failed"),
        ):
            self.restore()
        self.assertTrue(failed)
        self.assert_held()
        self.assertEqual(self.owner_path.read_bytes(), self.owner_raw)
        receipt = json.loads((self.output / "restoration-release.json").read_bytes())
        self.assertFalse(Path(receipt["archive_path"]).exists())

    def test_owner_change_during_final_verification_blocks_commit(self) -> None:
        def change(*_args: object, **kwargs: object) -> None:
            if kwargs.get("added_files") is not None:
                self.owner_path.write_bytes(b"changed during final proof scan")

        self.verify.side_effect = change
        with self.assertRaisesRegex(
            RuntimeError, "inode/type/link/owner changed|raw owner/content changed"
        ):
            self.restore()
        self.assert_held()
        self.assertEqual(
            self.owner_path.read_bytes(), b"changed during final proof scan"
        )
        self.assertFalse(any(self.lease.lock_root.glob("restored-*")))

    def test_live_pid_during_final_verification_blocks_commit(self) -> None:
        def change(*_args: object, **kwargs: object) -> None:
            if kwargs.get("added_files") is not None:
                self.kill.side_effect = None

        self.verify.side_effect = change
        with self.assertRaisesRegex(RuntimeError, "still alive"):
            self.restore()
        self.assert_held()
        self.assertFalse(any(self.lease.lock_root.glob("restored-*")))

    def test_failed_sync_and_failed_rollback_leave_acquisition_visible_blocker(
        self,
    ) -> None:
        original_sync, original_rename = api.os.fsync, api.os.rename

        def sync(descriptor: int) -> None:
            if (
                api._inode(os.fstat(descriptor))
                == api._inode(self.lease.lock_root.stat())
                and not self.lease.lock.exists()
            ):
                raise OSError("commit sync failed")
            original_sync(descriptor)

        def rename(source: str, destination: str, **kwargs: object) -> None:
            if source.startswith("restored-"):
                raise OSError("rollback failed")
            original_rename(source, destination, **kwargs)

        with (
            patch.object(api.os, "fsync", side_effect=sync),
            patch.object(api.os, "rename", side_effect=rename),
            self.assertRaisesRegex(OSError, "commit sync failed") as failure,
        ):
            self.restore()
        self.assertTrue(
            any("rollback failed" in note for note in failure.exception.__notes__)
        )
        self.assertFalse(self.lease.lock.exists())
        self.assertTrue(
            (
                self.lease.lock_root / (self.lease.lock.name + ".restoration-pending")
            ).exists()
        )
        competitor = api.IosRadioLease(
            DEVICE, RUN, self.root / "competitor.json", lock_root=self.lease.lock_root
        )
        with self.assertRaisesRegex(RuntimeError, "pending restoration blocks"):
            competitor.acquire()
        self.assertFalse(competitor.path.exists())
        self.assertEqual(self.lease.path.read_bytes(), self.journal_raw)

    def test_owner_raw_inode_and_foreign_child_drift_are_not_archived(self) -> None:
        for drift in ("raw", "inode", "hardlink", "symlink", "extra"):
            with self.subTest(drift=drift):

                def change(
                    *_args: object, drift: str = drift, **_kwargs: object
                ) -> dict:
                    if drift == "raw":
                        self.owner_path.write_bytes(b"foreign")
                    elif drift == "extra":
                        (self.lease.lock / "foreign").write_bytes(b"keep")
                    else:
                        previous = self.root / ("previous-" + drift)
                        self.owner_path.rename(previous)
                        if drift == "inode":
                            self.owner_path.write_bytes(self.owner_raw)
                        elif drift == "hardlink":
                            os.link(previous, self.owner_path)
                        else:
                            self.owner_path.symlink_to(previous)
                    return self.proof

                self.collect.side_effect = change
                with self.assertRaises((RuntimeError, OSError)):
                    self.restore()
                self.assert_held()
                for path in self.lease.lock.iterdir():
                    path.unlink()
                self.owner_path.write_bytes(self.owner_raw)

    def test_journal_directory_or_timestamp_drift_is_not_overwritten(self) -> None:
        def change(*_args: object, **_kwargs: object) -> dict:
            os.utime(self.lease.path, None)
            return self.proof

        self.collect.side_effect = change
        with self.assertRaisesRegex(RuntimeError, "timestamp drift"):
            self.restore()
        self.assert_held()
        self.assertEqual(self.owner_path.read_bytes(), self.owner_raw)


if __name__ == "__main__":
    unittest.main()
