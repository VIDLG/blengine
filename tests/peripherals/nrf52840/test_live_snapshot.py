"""Deterministic filesystem races only; no RF, native resources or capture owner."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from collections.abc import Callable
from functools import partial
from pathlib import Path
from unittest.mock import patch

from . import fixture_session as api
from . import pending_reload, recovery_ten

RUN = "11111111-1111-4111-8111-111111111111"
BOOT = "22222222-2222-4222-8222-222222222222"


class LiveSnapshotTests(unittest.TestCase):
    @staticmethod
    def publish(path: Path, content: bytes) -> None:
        temporary = path.with_name(path.name + ".next")
        temporary.write_bytes(content)
        temporary.replace(path)

    def after_first_fstat(
        self, action: Callable[[], None]
    ) -> Callable[[int], os.stat_result]:
        real_fstat = os.fstat
        calls = 0

        def fstat(fd: int) -> os.stat_result:
            nonlocal calls
            value = real_fstat(fd)
            calls += 1
            if calls == 1:
                action()
            return value

        return fstat

    def test_replace_after_open_returns_coherent_old_fd_then_new_publication(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.log"
            old, new = b"first\n", b"first\nsecond\n"
            path.write_bytes(old)
            old_inode = path.stat().st_ino
            real_fstat = os.fstat
            observed: list[os.stat_result] = []

            def fstat(fd: int) -> os.stat_result:
                value = real_fstat(fd)
                observed.append(value)
                if len(observed) == 1:
                    self.publish(path, new)
                return value

            with patch.object(api.os, "fstat", side_effect=fstat):
                self.assertEqual(api.read_live_snapshot(path), old)
            self.assertEqual([value.st_nlink for value in observed], [1, 0])
            self.assertEqual(
                [value.st_ino for value in observed], [old_inode, old_inode]
            )
            self.assertNotEqual(path.stat().st_ino, old_inode)
            self.assertEqual(api.read_live_snapshot(path), new)

    def test_replace_after_open_before_initial_fstat_also_allows_unlinked_fd(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "progress.json"
            path.write_bytes(b'{"phase":"old"}')
            real_open = os.open

            def open_then_replace(name: Path, flags: int) -> int:
                fd = real_open(name, flags)
                self.publish(path, b'{"phase":"new"}')
                return fd

            with patch.object(api.os, "open", side_effect=open_then_replace):
                self.assertEqual(api.read_live_document(path), {"phase": "old"})
            self.assertEqual(api.read_live_document(path), {"phase": "new"})

    def test_replacement_before_open_reads_new_fd_without_binding_old_path_stat(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.log"
            path.write_bytes(b"old\n")
            real_open = os.open

            def replace_then_open(name: Path, flags: int) -> int:
                self.publish(path, b"old\nnew\n")
                return real_open(name, flags)

            with patch.object(api.os, "open", side_effect=replace_then_open):
                self.assertEqual(api.read_live_snapshot(path), b"old\nnew\n")

    def test_in_place_same_size_append_truncate_and_restored_mtime_rejected(
        self,
    ) -> None:
        for mutation in ("same-size", "append", "truncate", "restore-mtime"):
            with (
                self.subTest(mutation=mutation),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = Path(directory) / "runtime.log"
                original = b"original\n"
                path.write_bytes(original)
                metadata = path.stat()

                def mutate(
                    path: Path = path,
                    original: bytes = original,
                    metadata: os.stat_result = metadata,
                    mutation: str = mutation,
                ) -> None:
                    content = {
                        "same-size": b"replaced\n",
                        "append": original + b"extra\n",
                        "truncate": b"short\n",
                        "restore-mtime": b"replaced\n",
                    }[mutation]
                    path.write_bytes(content)
                    os.utime(
                        path,
                        ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000),
                    )
                    if mutation == "restore-mtime":
                        os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))

                with (
                    patch.object(
                        api.os, "fstat", side_effect=self.after_first_fstat(mutate)
                    ),
                    self.assertRaisesRegex(api.FixtureSessionError, "changed in place"),
                ):
                    api.read_live_snapshot(path)

    def test_symlink_fifo_device_and_hardlinks_fail_without_opening(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.write_bytes(b"content\n")
            link = root / "symlink"
            link.symlink_to(target)
            dangling = root / "dangling"
            dangling.symlink_to(root / "absent")
            hardlink = root / "hardlink"
            os.link(target, hardlink)
            fifo = root / "fifo"
            os.mkfifo(fifo)
            for path in (link, dangling, hardlink, target, fifo, Path(os.devnull)):
                with self.subTest(path=path), patch.object(api.os, "open") as opened:
                    with self.assertRaises(api.FixtureSessionError):
                        api.read_live_snapshot(path)
                    opened.assert_not_called()

    def test_fd_type_link_and_size_checks_close_preopen_races(self) -> None:
        for replacement in ("symlink", "device-fd", "hardlink", "oversized"):
            with (
                self.subTest(replacement=replacement),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = Path(directory) / "snapshot"
                path.write_bytes(b"regular\n")
                real_open = os.open

                def race(
                    name: Path,
                    flags: int,
                    *,
                    path: Path = path,
                    replacement: str = replacement,
                    real_open: Callable[[Path | str, int], int] = real_open,
                ) -> int:
                    if replacement == "symlink":
                        path.unlink()
                        path.symlink_to(path.parent / "missing")
                    elif replacement == "device-fd":
                        return real_open(os.devnull, flags)
                    elif replacement == "hardlink":
                        os.link(path, path.with_name("other-link"))
                    else:
                        self.publish(path, b"x" * (api.MAX_DOCUMENT_BYTES + 1))
                    return real_open(name, flags)

                with (
                    patch.object(api.os, "open", side_effect=race),
                    self.assertRaises(api.FixtureSessionError),
                ):
                    api.read_live_snapshot(path)

    def test_hardlink_created_during_read_rejected_and_bounds_are_exact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.log"
            path.write_bytes(b"regular\n")
            with (
                patch.object(
                    api.os,
                    "fstat",
                    side_effect=self.after_first_fstat(
                        lambda: os.link(path, path.with_name("hardlink"))
                    ),
                ),
                self.assertRaises(api.FixtureSessionError),
            ):
                api.read_live_snapshot(path)
            path.with_name("hardlink").unlink()
            for size in (0, api.MAX_DOCUMENT_BYTES, api.MAX_DOCUMENT_BYTES + 1):
                self.publish(path, b"x" * size)
                with self.subTest(size=size):
                    if size <= api.MAX_DOCUMENT_BYTES:
                        self.assertEqual(len(api.read_live_snapshot(path)), size)
                    else:
                        with patch.object(api.os, "open") as opened:
                            with self.assertRaises(api.FixtureSessionError):
                                api.read_live_snapshot(path)
                            opened.assert_not_called()
            self.publish(path, b"small")
            with (
                patch.object(
                    api.os,
                    "fstat",
                    side_effect=self.after_first_fstat(
                        lambda: path.write_bytes(b"x" * (api.MAX_DOCUMENT_BYTES + 1))
                    ),
                ),
                self.assertRaises(api.FixtureSessionError),
            ):
                api.read_live_snapshot(path)

    def test_live_document_keeps_unique_keys_and_object_json_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "progress.json"
            for content in (
                b'{"phase":"old","phase":"new"}',
                b'{"nested":{"cycle":0,"cycle":1}}',
                b"[]",
                b"not json",
                b'"text"',
            ):
                with self.subTest(content=content):
                    self.publish(path, content)
                    with self.assertRaises(api.FixtureSessionError):
                        api.read_live_document(path)
            self.publish(path, b'{"phase":"recovery-ready","cycle":4}')
            self.assertEqual(
                api.read_live_document(path), {"phase": "recovery-ready", "cycle": 4}
            )

    def test_immutable_reader_still_rejects_the_same_legitimate_live_replacement(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "control.json"
            path.write_bytes(b'{"cycles":10}')
            with (
                patch.object(
                    api.os,
                    "fstat",
                    side_effect=self.after_first_fstat(
                        lambda: self.publish(path, b'{"cycles":10}')
                    ),
                ),
                self.assertRaisesRegex(
                    api.FixtureSessionError, "session input changed while reading"
                ),
            ):
                api._read_bytes(path)

    def test_both_runtime_parsers_accept_replaced_prefix_then_reject_rewind_or_edit(
        self,
    ) -> None:
        bootstrap = json.dumps(
            {
                "run_id": RUN,
                "pid": 42,
                "boot_id": BOOT,
                "bundle_id": "com.blehubnitrohost",
                "executable": "/synthetic-only/host",
                "source": "synthetic-only",
            }
        )
        cases = (
            (
                recovery_ten.RuntimeFacts(RUN, BOOT),
                [
                    recovery_ten.PREFIX + "native:bootstrap:" + bootstrap,
                    recovery_ten.PREFIX + f"runtime:runId={RUN}:nonce=test:phase=start",
                ],
            ),
            (
                pending_reload.RuntimeFacts(RUN),
                [
                    pending_reload.PREFIX
                    + f"generation=1:runtime-start:runId={RUN}:nonce=test",
                    pending_reload.PREFIX + "generation=1:native-ready:passed",
                ],
            ),
        )
        for facts, lines in cases:
            with (
                self.subTest(parser=type(facts).__module__),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = Path(directory) / "runtime.log"
                facts.refresh(
                    path
                )  # Not yet published is permitted, never retried here.
                old = (lines[0] + "\n").encode()
                new = ("\n".join(lines) + "\n").encode()
                self.publish(path, old)
                with patch.object(
                    api.os,
                    "fstat",
                    side_effect=self.after_first_fstat(
                        partial(self.publish, path, new)
                    ),
                ):
                    facts.refresh(path)
                self.assertEqual(facts.lines, lines[:1])
                facts.refresh(path)
                self.assertEqual(facts.lines, lines)
                self.publish(path, old)
                with self.assertRaisesRegex(RuntimeError, "rewound or changed"):
                    facts.refresh(path)
                self.publish(path, new.replace(b"nonce=test", b"nonce=evil"))
                with self.assertRaisesRegex(RuntimeError, "rewound or changed"):
                    facts.refresh(path)
                self.assertEqual(facts.lines, lines)
                path.unlink()
                path.symlink_to(Path(directory) / "absent")
                with self.assertRaises(api.FixtureSessionError):
                    facts.refresh(path)


if __name__ == "__main__":
    unittest.main()
