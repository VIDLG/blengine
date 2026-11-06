"""Bounded capture bytes and owned local-child teardown, with no RF protocol state."""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path

MAX_BYTES = 8 * 1024 * 1024
THREADTIME = re.compile(
    r"^\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+\s+(\d+)\s+\d+\s+[VDIWEF]\s+[^:]+:\s*"
)


def publish_new_json(
    path: Path,
    value: dict,
    *,
    on_commit: Callable[[tuple[int, int]], None] | None = None,
) -> str:
    """Publish closed bytes exclusively, optionally handing off their inode identity.

    The callback runs with the temporary link still retained; callback failure
    rolls back only this publication. Callers doing post-return authority checks
    can retain the identity for later retraction without adopting a replacement.
    """
    raw = (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    primary: BaseException | None = None
    try:
        with temporary.open("xb") as stream:
            stream.write(raw)
            info = os.fstat(stream.fileno())
        os.link(temporary, path)
        try:
            if on_commit is not None:
                on_commit((info.st_dev, info.st_ino))
            temporary.unlink()
        except BaseException as error:
            try:
                # Roll back only the inode we published, never a replacement.
                current, staged = path.lstat(), temporary.lstat()
                if (current.st_dev, current.st_ino) == (staged.st_dev, staged.st_ino):
                    path.unlink()
            except OSError as rollback:
                error.add_note(f"JSON publication rollback: {rollback}")
            raise
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError as cleanup:
            if primary is None:
                raise
            primary.add_note(f"JSON temporary cleanup: {cleanup}")
    return hashlib.sha256(raw).hexdigest()


class PrefixStream:
    """Read an append-only bounded file, retaining duplicates and partial UTF-8.

    The caller supplies its protocol prefix and, for Android, the already pinned
    PID. This reader performs framing/lineage checks, not scenario validation.
    """

    def __init__(
        self, path: Path, pid: int | None, *, prefix: str, raw: bool = False
    ) -> None:
        self.path, self.pid = path, pid
        self.prefix, self.raw = prefix, raw
        self.previous = b""
        self.complete = 0

    def poll(self) -> list[str]:
        with self.path.open("rb") as stream:
            content = stream.read(MAX_BYTES + 1)
        if len(content) > MAX_BYTES:
            raise RuntimeError("capture raw log exceeds 8 MiB")
        if not content.startswith(self.previous):
            raise RuntimeError("capture raw log rewound or changed observed prefix")
        end = content.rfind(b"\n") + 1
        text = content[self.complete : end].decode("utf-8", errors="strict")
        self.previous, self.complete = content, end
        lines = []
        for line in text.splitlines():
            if self.prefix not in line:
                continue
            if self.pid is not None:
                header = THREADTIME.match(line)
                if header is None or int(header[1]) != self.pid:
                    raise RuntimeError("foreign Android PID/header runtime marker")
            if line.count(self.prefix) != 1:
                raise RuntimeError("ambiguous recovery runtime line")
            lines.append(line if self.raw else line[line.index(self.prefix) :])
        return lines


def stop_local(process: subprocess.Popen[bytes], *, console: bool) -> str:
    """Reap an owned SDK child without forwarding catchable signals to the App.

    A devicectl console runs in its own host process group. Killing that local
    group detaches the bridge; it must not be replaced with a forwarded SIGTERM.
    """
    if process.poll() is not None:
        process.wait(timeout=2)
        return "console-exited" if console else "exited"
    if console:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)
    return "detached" if console else "stopped"
