"""Real temporary artifact trees; no SDK, signing or device commands."""

from __future__ import annotations

import hashlib
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.apple import app_artifact


class AppArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.app = self.root / "Candidate.app"
        self.app.mkdir()

    def write(self, relative: str, content: bytes) -> Path:
        path = self.app / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_historical_digest_order_paths_bytes_and_empty_directories(self) -> None:
        files = {
            "z": b"last\0",
            "a.txt": b"sibling",
            "a/nested": b"nested",
            "a/empty": b"",
            "Resources/中文": bytes(range(256)) * 600,
        }
        for relative, content in files.items():
            self.write(relative, content)
        (self.app / "empty-directory").mkdir()
        historical = hashlib.sha256()
        for relative in sorted(map(Path, files)):
            historical.update(relative.as_posix().encode() + b"\0")
            historical.update(files[relative.as_posix()])
        expected = historical.hexdigest()
        self.assertEqual(app_artifact.fingerprint(self.app), expected)
        for path in self.app.rglob("*"):
            if path.is_file():
                path.chmod(0o700)
        self.assertEqual(app_artifact.fingerprint(self.app), expected)
        self.app.rename(self.root / "Renamed.app")
        self.assertEqual(app_artifact.fingerprint(self.root / "Renamed.app"), expected)

    def test_empty_tree_preserves_historical_digest(self) -> None:
        (self.app / "empty").mkdir()
        self.assertEqual(
            app_artifact.fingerprint(self.app), hashlib.sha256(b"").hexdigest()
        )

    def test_rejects_missing_non_directory_and_linked_roots(self) -> None:
        file = self.root / "file"
        file.write_bytes(b"not a directory")
        hardlink = self.root / "hardlink"
        os.link(file, hardlink)
        linked = self.root / "linked.app"
        linked.symlink_to(self.app, target_is_directory=True)
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        for path in (self.root / "missing", file, hardlink, linked, fifo):
            with self.subTest(path=path), self.assertRaises((ValueError, OSError)):
                app_artifact.fingerprint(path)

    def test_rejects_member_symlinks_hardlinks_and_special_files(self) -> None:
        external = self.root / "external"
        external.write_bytes(b"outside")
        member = self.app / "member"
        for kind in (
            "file-link",
            "directory-link",
            "dangling",
            "hardlink",
            "fifo",
            "socket",
        ):
            with (
                self.subTest(kind=kind),
                socket.socket(socket.AF_UNIX) as endpoint,
                tempfile.TemporaryDirectory(dir="/tmp") as short_directory,
            ):
                if kind == "file-link":
                    member.symlink_to(external)
                elif kind == "directory-link":
                    member.symlink_to(self.root, target_is_directory=True)
                elif kind == "dangling":
                    member.symlink_to(self.root / "absent")
                elif kind == "hardlink":
                    os.link(external, member)
                elif kind == "fifo":
                    os.mkfifo(member)
                else:
                    # Darwin's sockaddr_un cannot hold long TMPDIR paths.
                    socket_path = Path(short_directory) / "socket"
                    endpoint.bind(str(socket_path))
                    socket_path.rename(member)
                try:
                    with self.assertRaises(ValueError):
                        app_artifact.fingerprint(self.app)
                finally:
                    member.unlink()

    def test_guard_can_abort_before_io_and_between_real_read_chunks(self) -> None:
        file = self.write("large", b"x" * (65536 * 4))
        failure = TimeoutError("candidate deadline")
        with patch.object(app_artifact.os, "open") as opened:
            with self.assertRaises(TimeoutError) as caught:
                app_artifact.fingerprint(
                    self.app, require_current=lambda: self.fail_guard(failure)
                )
            self.assertIs(caught.exception, failure)
            opened.assert_not_called()

        read, opened = os.read, os.open
        total = 0
        descriptors: list[int] = []

        def record_open(path, flags, *args, **kwargs):
            fd = opened(path, flags, *args, **kwargs)
            descriptors.append(fd)
            return fd

        def observe(fd: int, size: int) -> bytes:
            nonlocal total
            chunk = read(fd, size)
            total += len(chunk)
            return chunk

        def guard() -> None:
            if total >= 65536:
                raise failure

        with (
            patch.object(app_artifact.os, "read", side_effect=observe),
            patch.object(app_artifact.os, "open", side_effect=record_open),
            self.assertRaises(TimeoutError) as caught,
        ):
            app_artifact.fingerprint(self.app, require_current=guard)
        self.assertIs(caught.exception, failure)
        self.assertGreaterEqual(len(descriptors), 2)
        for fd in descriptors:
            with self.assertRaises(OSError):
                os.fstat(fd)
        self.assertEqual(total, 65536)
        self.assertEqual(file.stat().st_size, 65536 * 4)
        self.assertEqual(
            app_artifact.fingerprint(self.app),
            hashlib.sha256(b"large\0" + file.read_bytes()).hexdigest(),
        )

    @staticmethod
    def fail_guard(error: Exception) -> None:
        raise error

    def test_file_mutation_replacement_and_tree_drift_during_reads(self) -> None:
        for kind in (
            "rewrite",
            "truncate",
            "append",
            "replace",
            "delete",
            "hardlink",
            "add-member",
            "replace-root",
            "replace-directory",
            "previous-file",
        ):
            with (
                self.subTest(kind=kind),
                tempfile.TemporaryDirectory() as directory,
            ):
                app = Path(directory) / "Input.app"
                folder = app / "nested"
                folder.mkdir(parents=True)
                previous = folder / "a"
                previous.write_bytes(b"previous")
                target = folder / "b"
                target.write_bytes(b"x" * (65536 * 2))
                read = os.read
                mutated = False

                def mutate(
                    fd: int,
                    size: int,
                    *,
                    read=read,
                    kind=kind,
                    target=target,
                    directory=directory,
                    folder=folder,
                    app=app,
                    previous=previous,
                ) -> bytes:
                    nonlocal mutated
                    chunk = read(fd, size)
                    if not mutated and chunk.startswith(b"x"):
                        mutated = True
                        if kind == "rewrite":
                            stamp = target.stat()
                            with target.open("r+b") as stream:
                                stream.write(b"y")
                            os.utime(target, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                        elif kind == "truncate":
                            target.write_bytes(b"short")
                        elif kind == "append":
                            with target.open("ab") as stream:
                                stream.write(b"longer")
                        elif kind == "replace":
                            replacement = Path(directory) / "replacement"
                            replacement.write_bytes(target.read_bytes())
                            replacement.replace(target)
                        elif kind == "delete":
                            target.unlink()
                        elif kind == "hardlink":
                            os.link(target, Path(directory) / "alias")
                        elif kind == "add-member":
                            (folder / "new").write_bytes(b"new")
                        elif kind == "replace-root":
                            app.rename(Path(directory) / "old.app")
                            app.mkdir()
                        elif kind == "replace-directory":
                            folder.rename(app / "old")
                            folder.mkdir()
                        else:
                            previous.write_bytes(b"modified")
                    return chunk

                with (
                    patch.object(app_artifact.os, "read", side_effect=mutate),
                    self.assertRaises((RuntimeError, ValueError, OSError)),
                ):
                    app_artifact.fingerprint(app)
                self.assertTrue(mutated)

    def test_open_race_never_follows_replacement_symlink_or_blocks_on_fifo(
        self,
    ) -> None:
        for kind in ("file-symlink", "directory-symlink", "fifo"):
            with (
                self.subTest(kind=kind),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                app = root / "Input.app"
                app.mkdir()
                target = app / "member"
                external = root / "external"
                if kind == "directory-symlink":
                    target.mkdir()
                    external.mkdir()
                    (external / "secret").write_bytes(b"outside")
                else:
                    target.write_bytes(b"inside")
                    external.write_bytes(b"outside")
                opened = os.open
                swapped = False

                def swap(
                    path,
                    flags,
                    *args,
                    kind=kind,
                    target=target,
                    external=external,
                    opened=opened,
                    **kwargs,
                ):
                    nonlocal swapped
                    if os.fspath(path) == "member" and not swapped:
                        swapped = True
                        if target.is_dir():
                            target.rmdir()
                        else:
                            target.unlink()
                        if kind == "fifo":
                            os.mkfifo(target)
                        else:
                            target.symlink_to(external)
                    return opened(path, flags, *args, **kwargs)

                with (
                    patch.object(app_artifact.os, "open", side_effect=swap),
                    self.assertRaises((RuntimeError, ValueError, OSError)),
                ):
                    app_artifact.fingerprint(app)
                self.assertTrue(swapped)


if __name__ == "__main__":
    unittest.main()
