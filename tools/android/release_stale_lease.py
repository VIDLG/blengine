"""Release an Android phone lease whose owning host process is provably gone.

A managed run that is interrupted (or whose App finalization cannot be verified) leaves its
lock in place on purpose: adopting an unknown owner is how one run kills another run's phone.
That fail-closed rule is right, but it also means a dead host leaves a lock nothing can hold
again, and removing it by hand is an unrecorded mutation of the very evidence the lock exists
to protect.

This entry is the bounded, audited way out. It refuses unless

* the lock and its `owner.json` are the canonical single-owner shape;
* the recorded owner run id is the one the operator names;
* the recorded host pid is not running (a live pid is another run, never adopted here).

It then writes `stale-release.json` next to the owner before removing the lock, so the release
is evidence rather than an unrecorded `rm`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

from .state_control import ROOT

LOCK_ROOT = ROOT / ".work/android-radio-leases"


def _lock_directory(device: str, lock_root: Path) -> Path:
    return lock_root / hashlib.sha256(device.encode()).hexdigest()


def release_stale(
    *, device: str, expected_run_id: str, lock_root: Path = LOCK_ROOT
) -> dict:
    """Remove one dead owner's lease and return the release receipt."""
    if type(device) is not str or not device.strip():
        raise ValueError("an explicit physical device serial is required")
    if type(expected_run_id) is not str or not expected_run_id.strip():
        raise ValueError("the exact owner run id is required")
    lock = _lock_directory(device, lock_root)
    if not lock.exists() or lock.is_symlink():
        raise RuntimeError("no phone lease to release for this device")
    info = lock.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("phone lease is not a directory")
    owner_path = lock / "owner.json"
    owner_info = owner_path.lstat()
    if not stat.S_ISREG(owner_info.st_mode) or owner_info.st_nlink != 1:
        raise RuntimeError("phone lease owner file is not a single regular file")
    raw = owner_path.read_bytes()
    owner = json.loads(raw)
    if (
        type(owner) is not dict
        or owner.get("device") != device
        or type(owner.get("host_pid")) is not int
        or owner.get("host_pid") <= 0
        or type(owner.get("run_id")) is not str
    ):
        raise RuntimeError("phone lease owner record is not this device's")
    if owner["run_id"] != expected_run_id:
        raise RuntimeError(
            "phone lease owner run id differs from the one declared for release"
        )
    pid = owner["host_pid"]
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pass
    except PermissionError as error:
        raise RuntimeError(
            f"cannot prove host pid {pid} is gone; refusing to release a live owner"
        ) from error
    else:
        raise RuntimeError(
            f"host pid {pid} is still running; a live run owns this phone"
        )
    receipt = {
        "schema_version": 1,
        "scope": "android-stale-phone-lease-release",
        "device": device,
        "run_id": owner["run_id"],
        "host_pid": pid,
        "owner_sha256": hashlib.sha256(raw).hexdigest(),
        "status": "released",
    }
    # The lock may hold only the owner record; anything else is another owner's state and
    # must be inspected by a human rather than deleted here.
    unexpected = sorted(entry.name for entry in lock.iterdir() if entry != owner_path)
    if unexpected:
        raise RuntimeError(
            "phone lease contains unexpected entries: " + ", ".join(unexpected)
        )
    # The receipt outlives the lock, so the release stays evidence rather than an `rm`.
    log = lock_root / f"stale-release-{owner['run_id']}.json"
    with log.open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, sort_keys=True)
        stream.write("\n")
    try:
        owner_path.unlink()
        lock.rmdir()
    except BaseException as error:
        error.add_note(f"stale lease release receipt: {log}")
        raise
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True)
    parser.add_argument(
        "--expect-run-id",
        required=True,
        help="the run id recorded in the dead owner's lock; a mismatch releases nothing",
    )
    args = parser.parse_args(argv)
    try:
        receipt = release_stale(device=args.device, expected_run_id=args.expect_run_id)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"android_stale_lease=failed error={error}", file=sys.stderr)
        return 2
    print(
        "android_stale_lease=released "
        f"device={receipt['device']} run_id={receipt['run_id']} "
        f"host_pid={receipt['host_pid']}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
