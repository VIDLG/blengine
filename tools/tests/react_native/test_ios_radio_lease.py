"""Real temporary-file lease boundaries only; never touch a phone or launch App."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

from tools.react_native import ios_adapter_startup_evidence as evidence
from tools.react_native import ios_radio_lease as api
from tools.react_native.host_contract import BUNDLE_ID
from tools.react_native.rf_evidence_bundle import EvidenceError
from tools.tests.react_native.support.ios_adapter_startup_samples import (
    DEVICE,
    RUN,
    passed_control,
)

FOREIGN = "22222222-2222-4222-8222-222222222222"


def control() -> dict:
    identity = {
        "run_id": RUN,
        "nonce": "abc-def-123",
        "native": {
            "run_id": RUN,
            "adapter_mode": "startup-off",
            "pid": 1234,
            "boot_id": FOREIGN,
            "bundle_id": BUNDLE_ID,
            "executable": "file:///private/BleHubNitroHost.app/BleHubNitroHost",
            "source": "launch",
        },
    }
    return passed_control(identity, "ab" * 32)


def failed_control(prefix: int, *, mutated: bool = True, cleanup: bool = True) -> dict:
    value = control()
    value.update(
        status="failed",
        mutation_attempted=mutated,
        restored=cleanup,
        same_session=False,
        primary_error="original UI failure; never replace this with a cleanup error",
    )
    value["observations"] = value["observations"][:prefix]
    if cleanup:
        value["observations"].append(
            {
                "action": "cleanup-restored-on",
                "elapsed_seconds": 6,
                "identity": None,
                "switch_value": "1",
            }
        )
    return value


class IosRadioLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # macOS /var is a system symlink; the lease deliberately rejects symlink
        # components, so use the actual directory rather than its /var alias.
        self.root = Path(temporary.name).resolve()
        self.counter = 0

    def lease(self, *, device: str = DEVICE) -> api.IosRadioLease:
        self.counter += 1
        directory = self.root / str(self.counter)
        directory.mkdir()
        return api.IosRadioLease(
            device, RUN, directory / "lease.json", lock_root=directory / "locks"
        )

    def finish(self, lease: api.IosRadioLease, value: dict | None) -> list[str]:
        if value is None:
            return lease.finish(None, None)
        source = lease.path.with_name("control.json")
        # Deliberately not the publisher's serialization: bind the original bytes.
        raw = (json.dumps(value, indent=2) + "\n").encode()
        source.write_bytes(raw)
        return lease.finish(json.loads(raw), hashlib.sha256(raw).hexdigest())

    def journal(self, lease: api.IosRadioLease) -> dict:
        return json.loads(lease.path.read_bytes())

    def quarantined(self, lease: api.IosRadioLease) -> None:
        journal = self.journal(lease)
        self.assertEqual(journal["status"], "quarantined")
        self.assertIsNone(journal["released_monotonic"])
        self.assertTrue(journal["error"])
        self.assertTrue(lease.lock.is_dir())

    def test_full_source_success_binds_raw_hash_and_in_process_pid(self) -> None:
        lease = self.lease()
        with self.assertRaises(RuntimeError):
            lease.require_owner()
        self.assertTrue(lease.finish(None, None))
        self.assertFalse(lease.path.exists())
        self.assertFalse(lease.lock.exists())
        lease.acquire()  # No fixture/App work before this point.
        lease.require_owner()
        with (
            patch.object(api.os, "getpid", return_value=os.getpid() + 1),
            self.assertRaisesRegex(RuntimeError, "process"),
        ):
            lease.require_owner()
        value = control()
        before = copy.deepcopy(value)
        self.assertEqual(self.finish(lease, value), [])
        self.assertEqual(value, before)
        journal = self.journal(lease)
        self.assertEqual(
            set(journal),
            {
                "schema_version",
                "platform",
                "device",
                "run_id",
                "host_pid",
                "status",
                "scope",
                "control_sha256",
                "restoration",
                "error",
                "acquired_monotonic",
                "released_monotonic",
            },
        )
        self.assertEqual(journal["host_pid"], os.getpid())
        self.assertEqual(journal["restoration"], "on-readback")
        actual_hash = hashlib.sha256(
            lease.path.with_name("control.json").read_bytes()
        ).hexdigest()
        evidence._phone(journal, {"device": DEVICE, "run_id": RUN}, actual_hash)
        with self.assertRaises(EvidenceError):
            evidence._phone(journal, {"device": DEVICE, "run_id": RUN}, "ff" * 32)
        self.assertFalse(lease.lock.exists())
        with self.assertRaises(RuntimeError):
            lease.require_owner()
        self.assertTrue(lease.finish(value, actual_hash))
        with self.assertRaises(RuntimeError):
            lease.acquire()

    def test_failed_source_prefixes_release_only_after_last_cleanup_readback(
        self,
    ) -> None:
        for prefix in (2, 3, 4, 10, 11, 12):
            with self.subTest(prefix=prefix):
                lease = self.lease()
                lease.acquire()
                value = failed_control(prefix)
                primary = value["primary_error"]
                self.assertEqual(self.finish(lease, value), [])
                self.assertEqual(value["primary_error"], primary)
                self.assertEqual(self.journal(lease)["restoration"], "on-readback")
                self.assertFalse(lease.lock.exists())

    def test_explicit_pre_settings_failure_is_not_mutated_but_never_bundle_pass(
        self,
    ) -> None:
        for prefix in (0, 1):
            with self.subTest(prefix=prefix):
                lease = self.lease()
                lease.acquire()
                self.assertEqual(
                    self.finish(
                        lease, failed_control(prefix, mutated=False, cleanup=False)
                    ),
                    [],
                )
                journal = self.journal(lease)
                self.assertEqual(journal["restoration"], "not-mutated")
                with self.assertRaises(EvidenceError):
                    evidence._phone(
                        journal,
                        {"device": DEVICE, "run_id": RUN},
                        journal["control_sha256"],
                    )

    def test_missing_readonly_and_no_off_transcript_do_not_prove_unknown_on(
        self,
    ) -> None:
        values = [
            None,
            {},
            failed_control(0),
            failed_control(1),
            failed_control(2, mutated=False, cleanup=False),
            failed_control(1, cleanup=False),
        ]
        # Merely omitting OFF or reporting an intermediate ON cannot prove cleanup.
        value = failed_control(5, cleanup=False)
        value["restored"] = True
        values.append(value)
        for value in values:
            with self.subTest(value=value):
                lease = self.lease()
                lease.acquire()
                self.assertTrue(self.finish(lease, value))
                self.quarantined(lease)
                self.assertEqual(self.journal(lease)["restoration"], "unverified")

    def test_foreign_hash_schema_flags_and_malformed_observations_quarantine(
        self,
    ) -> None:
        mutations = [
            ("run_id", FOREIGN),
            ("scope", "control-only"),
            ("schema_version", True),
            ("fixture_receipt_sha256", "AB" * 32),
            ("mutation_attempted", 1),
            ("restored", "true"),
            ("same_session", 1),
            ("primary_error", None),
            ("cleanup_error", "failed ON"),
            ("status", "Passed"),
            ("extra", 1),
            ("observations", {}),
            ("observations", [None]),
            ("observations", control()["observations"] * 2),
        ]
        for key, bad in mutations:
            with self.subTest(key=key, bad=bad):
                lease = self.lease()
                lease.acquire()
                value = control()
                value[key] = bad
                self.assertTrue(self.finish(lease, value))
                self.quarantined(lease)
        for bad_hash in (None, "", "A" * 64, "a" * 63, True):
            with self.subTest(hash=bad_hash):
                lease = self.lease()
                lease.acquire()
                self.assertTrue(lease.finish(control(), bad_hash))
                self.quarantined(lease)

    def test_success_final_identity_and_failed_cleanup_are_not_interchangeable(
        self,
    ) -> None:
        variants = []
        value = control()
        value["observations"].pop()
        variants.append(value)
        value = control()
        value["observations"][-1], value["observations"][-2] = (
            value["observations"][-2],
            value["observations"][-1],
        )
        variants.append(value)
        for key, bad in (
            ("switch_value", True),
            ("switch_value", 1),
            ("switch_value", "0"),
            ("identity", {}),
            ("elapsed_seconds", float("nan")),
            ("elapsed_seconds", True),
            ("elapsed_seconds", -1),
        ):
            value = failed_control(3)
            value["observations"][-1][key] = bad
            variants.append(value)
        value = control()
        value["observations"][-1]["identity"]["native"]["run_id"] = FOREIGN
        variants.append(value)
        value = control()
        value["observations"][-1]["identity"]["nonce"] = "other-valid-nonce"
        variants.append(value)
        value = failed_control(3)
        value["observations"].append(copy.deepcopy(value["observations"][-1]))
        variants.append(value)
        for index, value in enumerate(variants):
            with self.subTest(index=index):
                lease = self.lease()
                lease.acquire()
                self.assertTrue(self.finish(lease, value))
                self.quarantined(lease)

    def test_udid_alias_competitor_and_stale_owner_are_never_adopted(self) -> None:
        lease = self.lease(device=DEVICE.lower())
        lease.acquire()
        owner_path = lease.lock / "owner.json"
        raw = owner_path.read_bytes()
        self.assertEqual(lease.lock.name, hashlib.sha256(DEVICE.encode()).hexdigest())
        competitor = api.IosRadioLease(
            DEVICE,
            FOREIGN,
            lease.path.with_name("competitor.json"),
            lock_root=lease.lock_root,
        )
        with self.assertRaises(FileExistsError):
            competitor.acquire()
        self.assertEqual(owner_path.read_bytes(), raw)
        self.assertFalse(competitor.path.exists())
        owner_path.write_text(json.dumps({"host_pid": 999999999, "run_id": FOREIGN}))
        stale = owner_path.read_bytes()
        competitor = api.IosRadioLease(
            DEVICE, FOREIGN, competitor.path, lock_root=lease.lock_root
        )
        with self.assertRaises(FileExistsError):
            competitor.acquire()
        self.assertEqual(owner_path.read_bytes(), stale)
        self.assertTrue(self.finish(lease, control()))
        self.quarantined(lease)
        self.assertEqual(owner_path.read_bytes(), stale)

    def test_symlink_roots_locks_evidence_and_foreign_files_are_not_touched(
        self,
    ) -> None:
        for target in (
            "root",
            "ancestor",
            "evidence-ancestor",
            "lock",
            "evidence",
            "hardlink-evidence",
        ):
            with self.subTest(target=target):
                lease = self.lease()
                foreign = lease.path.parent / "foreign"
                foreign.mkdir()
                sentinel = foreign / "sentinel"
                sentinel.write_bytes(b"foreign")
                if target == "root":
                    lease.lock_root.symlink_to(foreign, target_is_directory=True)
                elif target == "ancestor":
                    alias = lease.path.parent / "alias"
                    alias.symlink_to(foreign, target_is_directory=True)
                    lease = api.IosRadioLease(
                        DEVICE, RUN, lease.path, lock_root=alias / "locks"
                    )
                elif target == "evidence-ancestor":
                    alias = lease.path.parent / "alias"
                    alias.symlink_to(foreign, target_is_directory=True)
                    lease = api.IosRadioLease(
                        DEVICE, RUN, alias / "lease.json", lock_root=lease.lock_root
                    )
                elif target == "lock":
                    lease.lock_root.mkdir()
                    lease.lock.symlink_to(foreign, target_is_directory=True)
                elif target == "evidence":
                    lease.path.symlink_to(sentinel)
                else:
                    os.link(sentinel, lease.path)
                with self.assertRaises((OSError, RuntimeError)):
                    lease.acquire()
                self.assertEqual(sentinel.read_bytes(), b"foreign")
                self.assertEqual(
                    sorted(p.name for p in foreign.iterdir()), ["sentinel"]
                )

    def test_inode_raw_symlink_hardlink_and_directory_drift_keep_lock(self) -> None:
        for drift in (
            "raw",
            "inode",
            "symlink",
            "hardlink",
            "directory",
            "root",
            "extra",
        ):
            with self.subTest(drift=drift):
                lease = self.lease()
                lease.acquire()
                owner = lease.lock / "owner.json"
                raw = owner.read_bytes()
                if drift == "raw":
                    owner.write_bytes(raw.replace(RUN.encode(), FOREIGN.encode()))
                elif drift == "inode":
                    old = lease.lock / "old"
                    owner.rename(old)
                    owner.write_bytes(raw)
                    old.unlink()
                elif drift in ("symlink", "hardlink"):
                    foreign = lease.path.with_name("foreign-owner")
                    foreign.write_bytes(raw)
                    owner.unlink()
                    if drift == "symlink":
                        owner.symlink_to(foreign)
                    else:
                        os.link(foreign, owner)
                elif drift in ("directory", "root"):
                    directory = lease.lock if drift == "directory" else lease.lock_root
                    moved = directory.with_name(directory.name + "-old")
                    directory.rename(moved)
                    if drift == "root":
                        lease.lock.mkdir(parents=True)
                    else:
                        lease.lock.mkdir()
                    owner.write_bytes(raw)
                else:
                    (lease.lock / "foreign-child").write_bytes(b"keep")
                if drift != "extra":
                    with self.assertRaises((OSError, RuntimeError)):
                        lease.require_owner()
                self.assertTrue(self.finish(lease, control()))
                self.quarantined(lease)
                self.assertTrue(owner.exists())
                if drift != "raw":
                    self.assertEqual(owner.read_bytes(), raw)

    def test_acquire_publication_failure_cleans_only_unchanged_unmutated_owner(
        self,
    ) -> None:
        lease = self.lease()
        with (
            patch.object(lease, "_publish", side_effect=OSError("publication failed")),
            self.assertRaisesRegex(OSError, "publication failed"),
        ):
            lease.acquire()
        self.assertFalse(lease.lock.exists())
        lease = self.lease()
        foreign = b'{"foreign":true}'

        def replace_owner() -> None:
            (lease.lock / "owner.json").write_bytes(foreign)
            raise OSError("primary publication failed")

        with (
            patch.object(lease, "_publish", side_effect=replace_owner),
            self.assertRaisesRegex(OSError, "primary publication failed") as caught,
        ):
            lease.acquire()
        self.assertTrue(caught.exception.__notes__)
        self.assertEqual((lease.lock / "owner.json").read_bytes(), foreign)

    def test_finish_publication_and_release_failures_never_claim_released(self) -> None:
        for failure in ("publication", "rmdir", "both", "all-publications"):
            with self.subTest(failure=failure):
                lease = self.lease()
                lease.acquire()
                publish = lease._publish

                def failing_publish(
                    *,
                    failure: str = failure,
                    lease: api.IosRadioLease = lease,
                    publish: Callable[[], None] = publish,
                ) -> None:
                    if (
                        failure == "all-publications"
                        or lease.document["status"] == "released"
                        and failure == "publication"
                    ):
                        raise OSError("journal unavailable")
                    if failure == "both" and lease.document["status"] == "quarantined":
                        raise OSError("quarantine unavailable")
                    publish()

                real_rmdir = api.os.rmdir

                def failing_rmdir(
                    path: str,
                    *,
                    dir_fd: int | None = None,
                    lease: api.IosRadioLease = lease,
                    real_rmdir: Callable[..., None] = real_rmdir,
                ) -> None:
                    if path == lease.lock.name:
                        raise OSError("release denied")
                    real_rmdir(path, dir_fd=dir_fd)

                with patch.object(lease, "_publish", side_effect=failing_publish):
                    if failure in ("rmdir", "both"):
                        with patch.object(api.os, "rmdir", side_effect=failing_rmdir):
                            errors = self.finish(lease, control())
                    else:
                        errors = self.finish(lease, control())
                self.assertTrue(errors)
                self.assertEqual(lease.document["status"], "quarantined")
                self.assertTrue((lease.lock / "owner.json").is_file())
                if lease.path.exists():
                    self.assertNotEqual(self.journal(lease)["status"], "released")
                if failure in ("publication", "rmdir"):
                    self.quarantined(lease)

    def test_evidence_drift_is_never_overwritten_even_during_quarantine(self) -> None:
        for drift in ("raw", "inode", "symlink", "hardlink"):
            with self.subTest(drift=drift):
                lease = self.lease()
                lease.acquire()
                raw = lease.path.read_bytes()
                foreign = lease.path.with_name("foreign.json")
                foreign.write_bytes(raw if drift != "raw" else b"foreign")
                if drift == "raw":
                    lease.path.write_bytes(b"foreign")
                elif drift == "inode":
                    foreign.replace(lease.path)
                else:
                    lease.path.unlink()
                    if drift == "symlink":
                        lease.path.symlink_to(foreign)
                    else:
                        os.link(foreign, lease.path)
                before = lease.path.read_bytes()
                self.assertTrue(self.finish(lease, control()))
                self.assertTrue(lease.lock.exists())
                self.assertEqual(lease.path.read_bytes(), before)
