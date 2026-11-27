"""The one file digest this repository's tools compute.

Lanes freeze an artifact (an APK, an xctestrun, a staged App) and later prove it did not
change under the run. That proof was twelve private copies of the same streaming SHA-256, in
three styles; one function means one answer to "which bytes were reviewed", and a digest is
never confused with its policy: whether a path may be a symlink, how large it may be and when
it is compared stay with the caller.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def sha256_file(path: Path) -> str:
    """The lowercase SHA-256 of a file's bytes, read in bounded chunks."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


__all__ = ["sha256_file"]
