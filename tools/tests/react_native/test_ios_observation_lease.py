"""Observation lease boundaries on temporary files; no phone, App or radio I/O."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest.mock import Mock, patch

from tools.react_native import ios_radio_lease as api
from tools.tests.react_native.support.ios_adapter_startup_samples import (
    DEVICE,
    RUN,
    passed_control,
)


class IosObservationLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.counter = 0

    def lease(self, *, scope: str | None = None) -> api.IosRadioLease:
        self.counter += 1
        directory = self.root / str(self.counter)
        directory.mkdir()
        return api.IosRadioLease(
            DEVICE,
            RUN,
            directory / "lease.json",
            lock_root=directory / "locks",
            scope=api.SCAN_OBSERVATION_SCOPE if scope is None else scope,
        )

    def journal(self, lease: api.IosRadioLease) -> dict:
        return json.loads(lease.path.read_bytes())

    def assert_terminal(self, lease: api.IosRadioLease) -> None:
        with self.assertRaises(RuntimeError):
            lease.require_owner()
        with self.assertRaises(RuntimeError):
            lease.acquire()
        before = copy.deepcopy(lease.document)
        finalizer = Mock()
        self.assertTrue(lease.finish_observation(None, finalize_owned=finalizer))
        self.assertTrue(lease.finish(None, None))
        finalizer.assert_not_called()
        self.assertEqual(lease.document, before)

    def test_successful_owned_finalization_releases_without_erasing_primary(
        self,
    ) -> None:
        self.assertEqual(api.SCAN_OBSERVATION_SCOPE, "ios-formal-scan-phone-owner")
        for primary in (None, RuntimeError("scan failed"), KeyboardInterrupt()):
            with self.subTest(primary=primary):
                lease = self.lease()
                lease.acquire()
                held = self.journal(lease)
                self.assertEqual(held["status"], "held")
                self.assertIsNone(held["primary_error"])
                self.assertEqual(held["cleanup_errors"], [])
                called = []

                def finalize(
                    *, lease: api.IosRadioLease = lease, called: list[bool] = called
                ) -> None:
                    lease.require_owner()
                    self.assertEqual(self.journal(lease)["status"], "held")
                    called.append(True)

                with (
                    patch.object(lease, "_restoration", side_effect=AssertionError),
                    patch.object(
                        lease, "_native_restoration", side_effect=AssertionError
                    ),
                ):
                    self.assertEqual(
                        lease.finish_observation(primary, finalize_owned=finalize), []
                    )
                self.assertEqual(called, [True])
                journal = self.journal(lease)
                self.assertEqual(set(journal), set(held))
                self.assertEqual(journal["status"], "released")
                self.assertIsNotNone(journal["released_monotonic"])
                self.assertEqual(journal["cleanup_errors"], [])
                self.assertIsNone(journal["error"])
                expected = (
                    None if primary is None else f"{type(primary).__name__}: {primary}"
                )
                self.assertEqual(journal["primary_error"], expected)
                self.assertEqual(journal["restoration"], "unverified")
                self.assertIsNone(journal["control_sha256"])
                self.assertNotIn("resource_zero", journal)
                self.assertNotIn("restored", journal)
                self.assertFalse(lease.lock.exists())
                self.assert_terminal(lease)

    def test_callback_is_required_and_missing_argument_preserves_ownership(
        self,
    ) -> None:
        lease = self.lease()
        lease.acquire()
        with self.assertRaises(TypeError):
            lease.finish_observation(None)
        lease.require_owner()
        self.assertFalse(lease.finished)
        self.assertEqual(
            lease.finish_observation(None, finalize_owned=lambda: None), []
        )

    def test_finalizer_failure_quarantines_and_keeps_primary_separate(self) -> None:
        for failure in (RuntimeError("owned cleanup failed"), KeyboardInterrupt()):
            with self.subTest(failure=failure):
                lease = self.lease()
                lease.acquire()
                owner = (lease.lock / "owner.json").read_bytes()
                finalizer = Mock(side_effect=failure)
                errors = lease.finish_observation(
                    ValueError("original scan failure"), finalize_owned=finalizer
                )
                finalizer.assert_called_once_with()
                self.assertTrue(errors)
                self.assertTrue(any(type(failure).__name__ in row for row in errors))
                journal = self.journal(lease)
                self.assertEqual(journal["status"], "quarantined")
                self.assertIsNone(journal["released_monotonic"])
                self.assertEqual(
                    journal["primary_error"], "ValueError: original scan failure"
                )
                self.assertEqual(journal["cleanup_errors"], errors)
                self.assertNotIn("original scan failure", journal["error"])
                self.assertEqual((lease.lock / "owner.json").read_bytes(), owner)
                self.assert_terminal(lease)

    def test_owner_drift_before_and_after_callback_never_deletes_foreign_owner(
        self,
    ) -> None:
        for timing in ("before", "after"):
            for drift in ("raw", "nonce", "inode", "directory", "root"):
                with self.subTest(timing=timing, drift=drift):
                    lease = self.lease()
                    lease.acquire()
                    owner = lease.lock / "owner.json"
                    raw = owner.read_bytes()

                    def replace_owner(
                        *,
                        drift: str = drift,
                        raw: bytes = raw,
                        owner: Path = owner,
                        lease: api.IosRadioLease = lease,
                    ) -> None:
                        if drift in ("raw", "nonce"):
                            value = json.loads(raw)
                            value["owner_nonce" if drift == "nonce" else "run_id"] = (
                                "foreign"
                            )
                            owner.write_text(json.dumps(value))
                        elif drift == "inode":
                            owner.rename(lease.lock / "old-owner")
                            owner.write_bytes(raw)
                            (lease.lock / "old-owner").unlink()
                        else:
                            directory = (
                                lease.lock if drift == "directory" else lease.lock_root
                            )
                            directory.rename(
                                directory.with_name(directory.name + "-old")
                            )
                            lease.lock.mkdir(parents=True)
                            owner.write_bytes(raw)

                    if timing == "before":
                        replace_owner()
                    finalizer = Mock(
                        side_effect=replace_owner if timing == "after" else None
                    )
                    errors = lease.finish_observation(
                        RuntimeError("scan failure"), finalize_owned=finalizer
                    )
                    self.assertTrue(errors)
                    self.assertEqual(finalizer.call_count, int(timing == "after"))
                    self.assertTrue(owner.is_file())
                    foreign = owner.read_bytes()
                    if drift in ("raw", "nonce"):
                        self.assertEqual(
                            json.loads(foreign)[
                                "owner_nonce" if drift == "nonce" else "run_id"
                            ],
                            "foreign",
                        )
                    else:
                        self.assertEqual(foreign, raw)
                    journal = self.journal(lease)
                    self.assertEqual(journal["status"], "quarantined")
                    self.assertIsNone(journal["released_monotonic"])
                    self.assertEqual(
                        journal["primary_error"], "RuntimeError: scan failure"
                    )
                    self.assertEqual(journal["cleanup_errors"], errors)
                    self.assert_terminal(lease)
                    self.assertEqual(owner.read_bytes(), foreign)

    def test_publication_and_rmdir_failures_never_leave_released_evidence(self) -> None:
        for failure in ("publication", "rmdir", "both", "all-publications"):
            with self.subTest(failure=failure):
                lease = self.lease()
                lease.acquire()
                owner = (lease.lock / "owner.json").read_bytes()
                publish = lease._publish
                rmdir = api.os.rmdir
                finalizer = Mock()

                def failing_publish(
                    *,
                    failure: str = failure,
                    lease: api.IosRadioLease = lease,
                    publish: Callable[[], None] = publish,
                ) -> None:
                    if failure == "all-publications" or (
                        failure == "publication"
                        and lease.document["status"] == "released"
                    ):
                        raise OSError("journal unavailable")
                    if failure == "both" and lease.document["status"] == "quarantined":
                        raise OSError("quarantine unavailable")
                    publish()

                def failing_rmdir(
                    path: str,
                    *,
                    dir_fd: int | None = None,
                    failure: str = failure,
                    lease: api.IosRadioLease = lease,
                    rmdir: Callable[..., None] = rmdir,
                ) -> None:
                    if path == lease.lock.name and failure in ("rmdir", "both"):
                        raise OSError("release denied")
                    rmdir(path, dir_fd=dir_fd)

                with (
                    patch.object(lease, "_publish", side_effect=failing_publish),
                    patch.object(api.os, "rmdir", side_effect=failing_rmdir),
                ):
                    errors = lease.finish_observation(
                        RuntimeError("original failure"), finalize_owned=finalizer
                    )
                finalizer.assert_called_once_with()
                self.assertTrue(errors)
                self.assertEqual(lease.document["status"], "quarantined")
                self.assertEqual(lease.document["cleanup_errors"], errors)
                self.assertEqual(
                    lease.document["primary_error"], "RuntimeError: original failure"
                )
                self.assertIsNone(lease.document["released_monotonic"])
                self.assertEqual((lease.lock / "owner.json").read_bytes(), owner)
                if lease.path.exists():
                    self.assertNotEqual(self.journal(lease)["status"], "released")
                if failure in ("publication", "rmdir"):
                    self.assertEqual(self.journal(lease), lease.document)
                if failure == "both":
                    self.assertTrue(any("release denied" in row for row in errors))
                    self.assertTrue(
                        any("quarantine unavailable" in row for row in errors)
                    )
                    self.assertFalse(lease.path.exists())
                self.assert_terminal(lease)

    def test_unacquired_finish_does_not_invoke_callback_or_consume_attempt(
        self,
    ) -> None:
        lease = self.lease()
        finalizer = Mock()
        self.assertTrue(lease.finish_observation(None, finalize_owned=finalizer))
        finalizer.assert_not_called()
        self.assertFalse(lease.path.exists())
        self.assertFalse(lease.lock.exists())
        self.assertFalse(lease.finished)
        with self.assertRaises(RuntimeError):
            lease.require_owner()
        lease.acquire()
        self.assertEqual(lease.finish_observation(None, finalize_owned=finalizer), [])
        finalizer.assert_called_once_with()
        self.assert_terminal(lease)

    def test_radio_finish_cannot_release_observation_scope(self) -> None:
        lease = self.lease()
        lease.acquire()
        before = lease.path.read_bytes()
        self.assertTrue(lease.finish(None, None))
        self.assertEqual(lease.path.read_bytes(), before)
        self.assertFalse(lease.finished)
        lease.require_owner()
        self.assertEqual(
            lease.finish_observation(None, finalize_owned=lambda: None), []
        )

    def test_observation_finish_cannot_bypass_either_radio_restoration(self) -> None:
        for scope in (api.SCOPE, api.NATIVE_SYSTEM_LOSS_SCOPE):
            for proven in (True, False):
                with self.subTest(scope=scope, proven=proven):
                    lease = self.lease(scope=scope)
                    lease.acquire()
                    before = lease.path.read_bytes()
                    finalizer = Mock()
                    self.assertTrue(
                        lease.finish_observation(None, finalize_owned=finalizer)
                    )
                    finalizer.assert_not_called()
                    self.assertEqual(lease.path.read_bytes(), before)
                    self.assertFalse(lease.finished)
                    self.assertNotIn("primary_error", lease.document)
                    self.assertNotIn("cleanup_errors", lease.document)
                    lease.require_owner()
                    value = passed_control({"run_id": RUN}, "ab" * 32)
                    # Pre-mutation failure is a valid radio proof, not observation cleanup.
                    value.update(
                        status="failed",
                        mutation_attempted=False,
                        restored=False,
                        same_session=False,
                        primary_error="before mutation",
                        observations=[],
                    )
                    if scope == api.NATIVE_SYSTEM_LOSS_SCOPE:
                        value["scope"] = "ios-bleng-system-loss-control"
                    raw = json.dumps(value).encode()
                    errors = lease.finish(
                        value if proven else None, hashlib.sha256(raw).hexdigest()
                    )
                    self.assertEqual(bool(errors), not proven)
                    self.assertEqual(lease.lock.exists(), not proven)
                    self.assertEqual(
                        lease.document["status"],
                        "released" if proven else "quarantined",
                    )

    def test_case_aliases_and_all_scopes_contend_for_same_physical_lock(self) -> None:
        for owner_scope in (
            api.SCAN_OBSERVATION_SCOPE,
            api.SCOPE,
            api.NATIVE_SYSTEM_LOSS_SCOPE,
        ):
            for competitor_scope in (
                api.SCAN_OBSERVATION_SCOPE,
                api.SCOPE,
                api.NATIVE_SYSTEM_LOSS_SCOPE,
            ):
                with self.subTest(owner=owner_scope, competitor=competitor_scope):
                    lease = self.lease(scope=owner_scope)
                    lease.acquire()
                    raw = (lease.lock / "owner.json").read_bytes()
                    competitor = api.IosRadioLease(
                        DEVICE.lower(),
                        RUN,
                        lease.path.with_name("competitor.json"),
                        lock_root=lease.lock_root,
                        scope=competitor_scope,
                    )
                    self.assertEqual(lease.device, competitor.device)
                    self.assertEqual(lease.lock, competitor.lock)
                    with self.assertRaises(FileExistsError):
                        competitor.acquire()
                    self.assertEqual((lease.lock / "owner.json").read_bytes(), raw)
                    lease.require_owner()


if __name__ == "__main__":
    unittest.main()
