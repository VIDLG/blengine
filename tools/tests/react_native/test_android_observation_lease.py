"""App-only Android finalization with real temporary locks and no device I/O."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest.mock import Mock, patch

from tools.android import state_control as api

RUN = "11111111-1111-4111-8111-111111111111"
DEVICE = "test-physical-serial"


class AndroidObservationLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.counter = 0
        self.commands = self.enterContext(
            patch.object(
                api.subprocess, "run", side_effect=AssertionError("device I/O")
            )
        )
        self.actions = self.enterContext(
            patch.object(
                api.AndroidRadioController,
                "_action",
                side_effect=AssertionError("radio action"),
            )
        )
        self.addCleanup(self.commands.assert_not_called)
        self.addCleanup(self.actions.assert_not_called)

    def controller(self, *, acquire: bool = True) -> api.AndroidRadioController:
        self.counter += 1
        directory = self.root / str(self.counter)
        directory.mkdir()
        controller = api.AndroidRadioController(
            DEVICE, RUN, directory / "journal.json", lock_root=directory / "locks"
        )
        if acquire:
            controller.acquire()
        return controller

    def journal(self, controller: api.AndroidRadioController) -> dict:
        return json.loads(controller.path.read_bytes())

    def assert_terminal(self, controller: api.AndroidRadioController) -> None:
        self.assertTrue(controller.finished)
        before = copy.deepcopy(controller.document)
        raw = controller.path.read_bytes() if controller.path.exists() else None
        finalizer = Mock()
        self.assertTrue(
            controller.finish_app_observation(
                ValueError("late"), finalize_owned=finalizer
            )
        )
        self.assertTrue(controller.finish_observation(None))
        self.assertTrue(controller.finish(None))
        finalizer.assert_not_called()
        self.assertEqual(controller.document, before)
        self.assertEqual(
            controller.path.read_bytes() if controller.path.exists() else None, raw
        )
        with self.assertRaises(RuntimeError):
            controller.require_owner()
        with self.assertRaises(RuntimeError):
            controller.acquire()

    def test_finalizer_and_publication_precede_release_with_primary_separate(
        self,
    ) -> None:
        for primary in (None, ValueError("scan failed"), KeyboardInterrupt()):
            with self.subTest(primary=primary):
                controller = self.controller()
                events = []
                publish = controller._publish
                rmdir = Path.rmdir

                def finalize(
                    *,
                    controller: api.AndroidRadioController = controller,
                    events: list[str] = events,
                ) -> None:
                    controller.require_owner()
                    self.assertEqual(self.journal(controller)["status"], "running")
                    events.append("finalize")

                def publication(
                    *,
                    controller: api.AndroidRadioController = controller,
                    events: list[str] = events,
                    publish: Callable[[], None] = publish,
                ) -> None:
                    controller.require_owner()
                    events.append("publish")
                    publish()

                def release(
                    path: Path,
                    *,
                    controller: api.AndroidRadioController = controller,
                    events: list[str] = events,
                    rmdir: Callable[[Path], None] = rmdir,
                ) -> None:
                    self.assertEqual(path, controller.lock)
                    self.assertEqual(self.journal(controller), controller.document)
                    events.append("release")
                    rmdir(path)

                with (
                    patch.object(controller, "_publish", side_effect=publication),
                    patch.object(Path, "rmdir", release),
                ):
                    self.assertEqual(
                        controller.finish_app_observation(
                            primary, finalize_owned=finalize
                        ),
                        [],
                    )
                self.assertEqual(events, ["finalize", "publish", "release"])
                self.assertFalse(controller.lock.exists())
                self.assertIsNone(controller.owner)
                journal = self.journal(controller)
                self.assertEqual(
                    journal["status"], "passed" if primary is None else "failed"
                )
                self.assertEqual(
                    journal["primary_error"],
                    None if primary is None else f"{type(primary).__name__}: {primary}",
                )
                self.assertEqual(journal["cleanup_errors"], [])
                self.assertEqual(journal["actions"], [])
                self.assertIsNone(journal["initial_state"])
                self.assertIsNone(journal["final_state"])
                self.assertIsNone(controller.last_state)
                self.assert_terminal(controller)

    def test_required_callback_and_unacquired_controller_do_not_consume_attempt(
        self,
    ) -> None:
        controller = self.controller(acquire=False)
        finalizer = Mock()
        with self.assertRaises(TypeError):
            controller.finish_app_observation(None)
        self.assertTrue(
            controller.finish_app_observation(None, finalize_owned=finalizer)
        )
        finalizer.assert_not_called()
        self.assertFalse(controller.finished)
        self.assertFalse(controller.path.exists())
        controller.acquire()
        self.assertEqual(
            controller.finish_app_observation(None, finalize_owned=finalizer), []
        )
        finalizer.assert_called_once_with()

    def test_radio_history_is_refused_without_finalizer_or_release(self) -> None:
        for history in ("actions", "initial_state", "last_state"):
            with self.subTest(history=history):
                controller = self.controller()
                owner = (controller.lock / "owner.json").read_bytes()
                if history == "actions":
                    controller.document["actions"].append({"label": "attempted"})
                else:
                    setattr(controller, history, "ON")
                finalizer = Mock()
                errors = controller.finish_app_observation(
                    None, finalize_owned=finalizer
                )
                self.assertTrue(errors)
                finalizer.assert_not_called()
                self.assertEqual(self.journal(controller)["status"], "quarantined")
                self.assertEqual((controller.lock / "owner.json").read_bytes(), owner)
                self.assert_terminal(controller)

    def test_finalizer_failures_and_interruptions_preserve_notes_and_lock(self) -> None:
        for failure in (RuntimeError("cleanup failed"), KeyboardInterrupt()):
            with self.subTest(failure=failure):
                controller = self.controller()
                owner = (controller.lock / "owner.json").read_bytes()
                failure.add_note("owned App still alive")
                errors = controller.finish_app_observation(
                    ValueError("primary"), finalize_owned=Mock(side_effect=failure)
                )
                self.assertIn(type(failure).__name__, "\n".join(errors))
                self.assertIn("owned App still alive", "\n".join(errors))
                self.assertNotIn("primary", "\n".join(errors))
                self.assertEqual(
                    self.journal(controller)["primary_error"], "ValueError: primary"
                )
                self.assertEqual(self.journal(controller)["cleanup_errors"], errors)
                self.assertEqual(self.journal(controller)["status"], "quarantined")
                self.assertEqual((controller.lock / "owner.json").read_bytes(), owner)
                self.assert_terminal(controller)

    def test_owner_drift_before_after_and_during_publication_is_not_deleted(
        self,
    ) -> None:
        for timing in ("before", "after", "publication"):
            for drift in ("content", "directory"):
                with self.subTest(timing=timing, drift=drift):
                    controller = self.controller()
                    owner = controller.lock / "owner.json"
                    raw = owner.read_bytes()

                    def replace_owner(
                        *,
                        drift: str = drift,
                        controller: api.AndroidRadioController = controller,
                        owner: Path = owner,
                        raw: bytes = raw,
                    ) -> None:
                        if drift == "directory":
                            controller.lock.rename(controller.lock.with_name("old"))
                            controller.lock.mkdir()
                            owner.write_bytes(raw)
                        else:
                            owner.write_bytes(b"foreign-owner")

                    if timing == "before":
                        replace_owner()
                    finalizer = Mock(
                        side_effect=replace_owner if timing == "after" else None
                    )
                    publish = controller._publish

                    def publication(
                        *,
                        publish: Callable[[], None] = publish,
                        timing: str = timing,
                        controller: api.AndroidRadioController = controller,
                        replace_owner: Callable[[], None] = replace_owner,
                    ) -> None:
                        publish()
                        if (
                            timing == "publication"
                            and controller.document["status"] != "quarantined"
                        ):
                            replace_owner()

                    with patch.object(controller, "_publish", side_effect=publication):
                        errors = controller.finish_app_observation(
                            None, finalize_owned=finalizer
                        )
                    self.assertTrue(errors)
                    self.assertEqual(finalizer.call_count, int(timing != "before"))
                    self.assertEqual(
                        owner.read_bytes(),
                        raw if drift == "directory" else b"foreign-owner",
                    )
                    self.assertEqual(self.journal(controller)["status"], "quarantined")
                    self.assert_terminal(controller)

    def test_publication_and_release_failures_quarantine_and_restore_owner(
        self,
    ) -> None:
        for stage in (
            "publish",
            "publish-after-write",
            "unlink",
            "rmdir",
            "rmdir-and-quarantine",
            "all-publications",
        ):
            for failure_type in (OSError, KeyboardInterrupt):
                with self.subTest(stage=stage, failure_type=failure_type):
                    controller = self.controller()
                    owner = (controller.lock / "owner.json").read_bytes()
                    publish = controller._publish
                    unlink = Path.unlink
                    rmdir = Path.rmdir
                    failure = failure_type("injected failure")
                    failure.add_note("failure detail")

                    def publication(
                        *,
                        controller: api.AndroidRadioController = controller,
                        stage: str = stage,
                        failure: BaseException = failure,
                        publish: Callable[[], None] = publish,
                    ) -> None:
                        quarantine = controller.document["status"] == "quarantined"
                        if (
                            stage == "all-publications"
                            or (stage == "publish" and not quarantine)
                            or (stage == "rmdir-and-quarantine" and quarantine)
                        ):
                            raise failure
                        publish()
                        if stage == "publish-after-write" and not quarantine:
                            raise failure

                    def remove_file(
                        path: Path,
                        *args: object,
                        stage: str = stage,
                        controller: api.AndroidRadioController = controller,
                        failure: BaseException = failure,
                        unlink: Callable[..., None] = unlink,
                        **kwargs: object,
                    ) -> None:
                        if stage == "unlink" and path == controller.lock / "owner.json":
                            raise failure
                        unlink(path, *args, **kwargs)

                    def remove_directory(
                        path: Path,
                        *,
                        stage: str = stage,
                        controller: api.AndroidRadioController = controller,
                        failure: BaseException = failure,
                        rmdir: Callable[[Path], None] = rmdir,
                    ) -> None:
                        if (
                            stage in ("rmdir", "rmdir-and-quarantine")
                            and path == controller.lock
                        ):
                            raise failure
                        rmdir(path)

                    finalizer = Mock()
                    with (
                        patch.object(controller, "_publish", side_effect=publication),
                        patch.object(Path, "unlink", remove_file),
                        patch.object(Path, "rmdir", remove_directory),
                    ):
                        errors = controller.finish_app_observation(
                            ValueError("original"), finalize_owned=finalizer
                        )
                    finalizer.assert_called_once_with()
                    self.assertIn("failure detail", "\n".join(errors))
                    self.assertEqual(controller.document["status"], "quarantined")
                    self.assertEqual(controller.document["cleanup_errors"], errors)
                    self.assertEqual(
                        controller.document["primary_error"], "ValueError: original"
                    )
                    self.assertEqual(
                        (controller.lock / "owner.json").read_bytes(), owner
                    )
                    if stage == "rmdir-and-quarantine":
                        self.assertFalse(controller.path.exists())
                        self.assertGreaterEqual(len(errors), 2)
                    elif stage != "all-publications":
                        self.assertEqual(self.journal(controller), controller.document)
                    else:
                        self.assertEqual(self.journal(controller)["status"], "running")
                    self.assert_terminal(controller)

    def test_failed_release_does_not_restore_over_foreign_owner(self) -> None:
        controller = self.controller()
        foreign = b"new-owner"

        def replace_and_fail(path: Path) -> None:
            self.assertEqual(path, controller.lock)
            (path / "owner.json").write_bytes(foreign)
            raise OSError("release raced")

        with patch.object(Path, "rmdir", replace_and_fail):
            errors = controller.finish_app_observation(
                None, finalize_owned=lambda: None
            )
        self.assertTrue(errors)
        self.assertEqual((controller.lock / "owner.json").read_bytes(), foreign)
        self.assertEqual(self.journal(controller)["status"], "quarantined")
        self.assert_terminal(controller)

    def test_quarantine_still_blocks_another_real_controller(self) -> None:
        controller = self.controller()
        errors = controller.finish_app_observation(
            None, finalize_owned=Mock(side_effect=RuntimeError("App still alive"))
        )
        self.assertTrue(errors)
        owner = (controller.lock / "owner.json").read_bytes()
        contender = api.AndroidRadioController(
            DEVICE,
            "22222222-2222-4222-8222-222222222222",
            controller.path.with_name("contender.json"),
            lock_root=controller.lock_root,
        )
        with self.assertRaises(FileExistsError):
            contender.acquire()
        self.assertFalse(contender.path.exists())
        self.assertEqual((controller.lock / "owner.json").read_bytes(), owner)

    def test_owner_retention_failure_preserves_both_errors_and_notes(self) -> None:
        controller = self.controller()
        original_open = Path.open
        retention_error = OSError("cannot retain owner")
        retention_error.add_note("owner storage unavailable")

        def fail_owner_write(path: Path, *args: object, **kwargs: object):
            if path == controller.lock / "owner.json" and args == ("xb",):
                raise retention_error
            return original_open(path, *args, **kwargs)

        with (
            patch.object(Path, "rmdir", side_effect=OSError("release denied")),
            patch.object(Path, "open", fail_owner_write),
        ):
            errors = controller.finish_app_observation(
                None, finalize_owned=lambda: None
            )
        self.assertTrue(controller.lock.is_dir())
        self.assertIn("release denied", "\n".join(errors))
        self.assertIn("cannot retain owner", "\n".join(errors))
        self.assertIn("owner storage unavailable", "\n".join(errors))
        self.assertEqual(self.journal(controller)["status"], "quarantined")
        self.assertEqual(self.journal(controller)["cleanup_errors"], errors)
        self.assert_terminal(controller)

    def test_retraction_never_deletes_a_replaced_journal(self) -> None:
        controller = self.controller()
        publish = controller._publish
        foreign = b"foreign journal"
        failure = OSError("quarantine write failed")
        failure.add_note("quarantine failure detail")

        def publication() -> None:
            if controller.document["status"] == "quarantined":
                controller.path.write_bytes(foreign)
                raise failure
            publish()

        with (
            patch.object(controller, "_publish", side_effect=publication),
            patch.object(Path, "rmdir", side_effect=OSError("release failed")),
        ):
            errors = controller.finish_app_observation(
                None, finalize_owned=lambda: None
            )
        self.assertIn("quarantine failure detail", "\n".join(errors))
        self.assertEqual(controller.document["status"], "quarantined")
        self.assertEqual(controller.path.read_bytes(), foreign)
        self.assertEqual(
            (controller.lock / "owner.json").read_bytes(), controller.owner
        )
        self.assert_terminal(controller)

    def test_reentrant_finish_does_not_call_another_finalizer_or_rewrite_primary(
        self,
    ) -> None:
        controller = self.controller()
        nested = Mock()

        def finalize() -> None:
            self.assertTrue(
                controller.finish_app_observation(
                    ValueError("nested"), finalize_owned=nested
                )
            )
            controller.require_owner()

        self.assertEqual(
            controller.finish_app_observation(None, finalize_owned=finalize), []
        )
        nested.assert_not_called()
        self.assertIsNone(self.journal(controller)["primary_error"])
        self.assert_terminal(controller)


if __name__ == "__main__":
    unittest.main()
