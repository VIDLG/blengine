"""Local SDK-child ownership tests; no device process is signalled here."""

from __future__ import annotations

import hashlib
import json
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from tools.react_native import capture_io as api


class CapturePublicationTests(unittest.TestCase):
    def test_complete_bytes_are_exclusive_and_original_result_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            digest = api.publish_new_json(path, {"status": "pending"})
            raw = path.read_bytes()
            self.assertEqual(digest, hashlib.sha256(raw).hexdigest())
            with self.assertRaises(FileExistsError):
                api.publish_new_json(path, {"status": "passed"})
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_on_commit_observes_published_inode_and_rolls_back_only_its_own(self):
        def exercise(error: BaseException | None, replace: bool) -> None:
            with tempfile.TemporaryDirectory() as directory:
                parent = Path(directory)
                path = parent / "result.json"
                observed = []
                replacement_identities = []
                foreign_bytes = b'{"status":"foreign","evidence":"preserve"}\n'

                def on_commit(identity: tuple[int, int]) -> None:
                    info = path.lstat()
                    self.assertIsInstance(identity, tuple)
                    self.assertEqual(identity, (info.st_dev, info.st_ino))
                    self.assertEqual(
                        json.loads(path.read_bytes()), {"status": "pending"}
                    )
                    temporary = list(parent.glob(".result.json.*.tmp"))
                    self.assertEqual(len(temporary), 1)
                    staged = temporary[0].lstat()
                    self.assertEqual(identity, (staged.st_dev, staged.st_ino))
                    self.assertEqual(temporary[0].read_bytes(), path.read_bytes())
                    observed.append(identity)
                    if replace:
                        replacement = parent / "foreign.json"
                        with replacement.open("xb") as stream:
                            stream.write(foreign_bytes)
                        info = replacement.lstat()
                        replacement_identities.append((info.st_dev, info.st_ino))
                        self.assertNotEqual(identity, replacement_identities[0])
                        replacement.replace(path)
                    if error is not None:
                        raise error

                if error is None:
                    digest = api.publish_new_json(
                        path, {"status": "pending"}, on_commit=on_commit
                    )
                    self.assertEqual(
                        digest, hashlib.sha256(path.read_bytes()).hexdigest()
                    )
                else:
                    with self.assertRaises(type(error)) as caught:
                        api.publish_new_json(
                            path, {"status": "pending"}, on_commit=on_commit
                        )
                    self.assertIs(caught.exception, error)
                self.assertEqual(len(observed), 1)
                if error is not None and not replace:
                    self.assertFalse(path.exists())
                    self.assertFalse(path.is_symlink())
                    self.assertEqual(list(parent.iterdir()), [])
                else:
                    info = path.lstat()
                    identity = (info.st_dev, info.st_ino)
                    if replace:
                        self.assertEqual(path.read_bytes(), foreign_bytes)
                        self.assertEqual(replacement_identities, [identity])
                    else:
                        self.assertEqual(observed, [identity])
                    self.assertEqual(list(parent.iterdir()), [path])

        for error_type, replace in (
            (None, False),
            (RuntimeError, False),
            (KeyboardInterrupt, False),
            (RuntimeError, True),
            (KeyboardInterrupt, True),
        ):
            with self.subTest(error_type=error_type, replace=replace):
                exercise(
                    error_type("on_commit interrupted") if error_type else None, replace
                )

    def test_commit_error_is_not_replaced_by_temporary_cleanup_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            with (
                patch.object(
                    api.os, "link", side_effect=RuntimeError("primary commit error")
                ),
                patch.object(
                    Path, "unlink", side_effect=OSError("temporary cleanup failed")
                ),
                self.assertRaisesRegex(RuntimeError, "primary commit error") as caught,
            ):
                api.publish_new_json(path, {"status": "pending"})
            self.assertIn(
                "temporary cleanup failed", " ".join(caught.exception.__notes__)
            )
            self.assertFalse(path.exists())

    def test_interruption_after_link_rolls_back_the_just_published_result(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            original = Path.unlink
            interrupted = False

            def unlink(current, *args, **kwargs):
                nonlocal interrupted
                if current.name.endswith(".tmp") and not interrupted:
                    interrupted = True
                    raise KeyboardInterrupt("publication interrupted")
                return original(current, *args, **kwargs)

            with (
                patch.object(Path, "unlink", unlink),
                self.assertRaises(KeyboardInterrupt),
            ):
                api.publish_new_json(path, {"status": "passed"})
            self.assertFalse(path.exists())
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_commit_failure_and_nonfinite_json_never_publish_a_result(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            with (
                patch.object(api.os, "link", side_effect=OSError("commit failed")),
                self.assertRaisesRegex(OSError, "commit failed"),
            ):
                api.publish_new_json(path, {"status": "passed"})
            self.assertFalse(path.exists())
            self.assertEqual(list(Path(directory).iterdir()), [])
            with self.assertRaises(ValueError):
                api.publish_new_json(path, {"value": float("nan")})
            self.assertFalse(path.exists())


class CaptureChildTests(unittest.TestCase):
    @unittest.skipUnless(
        hasattr(api.os, "killpg"), "console detach requires POSIX process groups"
    )
    def test_console_detach_and_command_stop_escalate_only_owned_local_children(
        self,
    ) -> None:
        for console in (True, False):
            with self.subTest(console=console):
                child = Mock(pid=1234)
                child.poll.return_value = None
                child.wait.side_effect = [subprocess.TimeoutExpired("sdk-child", 2), 0]
                with patch.object(api.os, "killpg") as killpg:
                    self.assertEqual(
                        api.stop_local(child, console=console),
                        "detached" if console else "stopped",
                    )
                if console:
                    killpg.assert_called_once_with(1234, signal.SIGKILL)
                    child.terminate.assert_not_called()
                else:
                    killpg.assert_not_called()
                    child.terminate.assert_called_once_with()
                child.kill.assert_called_once_with()
                self.assertEqual(
                    child.wait.call_args_list, [call(timeout=2), call(timeout=2)]
                )

        child = Mock(pid=1234)
        child.poll.return_value = 0
        with patch.object(api.os, "killpg") as killpg:
            self.assertEqual(api.stop_local(child, console=True), "console-exited")
        child.wait.assert_called_once_with(timeout=2)
        killpg.assert_not_called()
        child.terminate.assert_not_called()
        child.kill.assert_not_called()
