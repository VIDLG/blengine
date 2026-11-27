"""Read whether the iPhone display is awake, because a sleeping screen is silent.

`devicectl ... lockState` answers "is the lock screen dismissed", not "is the screen lit". A
phone that auto-locked with the display off still reports `passcodeRequired: false`, and every
UI interaction then does nothing: `activate` never becomes hittable, taps are dispatched into a
black screen, and text entry has no keyboard to land in. Measured on 2026-11-26 — iOS scene runs
went from `link-facts` at 121 s per press to `setText` never landing, all while `lockState` said
the device was unlocked, and a device screenshot taken at that moment was entirely black
(mean luminance 0).

A screenshot is the direct answer, so it is what this reads. The PNG is downsampled with `sips`
and only its mean luminance is inspected: no OCR, no image comparison, no claim about content.
"""

from __future__ import annotations

import math
import struct
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

# A lit screen showing anything at all is far above this; a display that is off captures as
# exact black, so the distinction does not need a tuned threshold.
AWAKE_LUMINANCE = 1.0
SAMPLE_EDGE = 8


class DisplayUnavailableError(RuntimeError):
    """Raised when the screenshot or its luminance sample cannot be taken."""


class DisplayAsleepError(DisplayUnavailableError):
    """The display is measurably off; carries the measurement for the receipt."""

    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        super().__init__(
            "the iPhone display is off (device screenshot is entirely black: "
            f"luminance {state['luminance']}); wake the phone and set "
            "Settings > Display & Brightness > Auto-Lock to Never — a UI scene cannot "
            "interact with a sleeping screen"
        )


def screenshot_command(device: str, destination: Path) -> list[str]:
    """The read-only capture this module admits; it never changes device state."""
    return [
        "xcrun",
        "devicectl",
        "device",
        "capture",
        "screenshot",
        "--device",
        device,
        "--destination",
        str(destination),
    ]


def downsample_command(source: Path, destination: Path) -> list[str]:
    """`sips` is shipped with macOS and reads PNG without a Python image dependency."""
    return [
        "sips",
        "-s",
        "format",
        "bmp",
        "-z",
        str(SAMPLE_EDGE),
        str(SAMPLE_EDGE),
        str(source),
        "--out",
        str(destination),
    ]


def mean_luminance(bitmap: bytes) -> float:
    """Mean perceived luminance of an uncompressed BMP, 0.0 for an all-black image.

    Only the fields this needs are read, and an unsupported layout is refused rather than
    guessed, so a malformed sample can never be mistaken for a lit screen.
    """
    if len(bitmap) < 54 or bitmap[:2] != b"BM":
        raise DisplayUnavailableError("luminance sample is not a BMP")
    (offset,) = struct.unpack("<I", bitmap[10:14])
    (header_size,) = struct.unpack("<I", bitmap[14:18])
    (width,) = struct.unpack("<i", bitmap[18:22])
    (height,) = struct.unpack("<i", bitmap[22:26])
    (bits,) = struct.unpack("<H", bitmap[28:30])
    if header_size < 40 or width <= 0 or height == 0:
        raise DisplayUnavailableError("luminance sample has an unsupported BMP header")
    if bits not in (24, 32):
        raise DisplayUnavailableError(f"luminance sample has unsupported depth {bits}")
    stride = ((bits * width + 31) // 32) * 4
    rows = abs(height)
    end = offset + stride * rows
    if offset < header_size + 14 or end > len(bitmap):
        raise DisplayUnavailableError("luminance sample is truncated")
    step = bits // 8
    total = 0.0
    count = 0
    for row in range(offset, end, stride):
        for column in range(width):
            index = row + column * step
            blue, green, red = bitmap[index], bitmap[index + 1], bitmap[index + 2]
            total += 0.299 * red + 0.587 * green + 0.114 * blue
            count += 1
    if count == 0:
        raise DisplayUnavailableError("luminance sample has no pixels")
    return total / count


def display_state(
    device: str,
    *,
    cwd: Path,
    timeout: float = 90.0,
    run: Callable[..., subprocess.CompletedProcess[bytes]] | None = None,
) -> dict[str, Any]:
    """Capture the screen once and report whether it is lit.

    The screenshot and the downsample share one finite budget; either failing is an error, not
    a quiet "probably asleep", because the caller decides what an unreadable device means.
    """
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise DisplayUnavailableError("display timeout must be finite and positive")
    runner = subprocess.run if run is None else run
    with tempfile.TemporaryDirectory(prefix="blehub-ios-display-") as scratch:
        root = Path(scratch)
        shot, sample = root / "screen.png", root / "sample.bmp"
        remaining = max(1.0, float(timeout) / 2)
        for command in (
            screenshot_command(device, shot),
            downsample_command(shot, sample),
        ):
            completed = runner(
                command,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=remaining,
            )
            if completed.returncode:
                detail = (completed.stderr or b"").decode("utf-8", "replace").strip()
                raise DisplayUnavailableError(
                    f"read-only screen capture failed (exit {completed.returncode}): {detail}"
                )
        luminance = mean_luminance(sample.read_bytes())
    return {
        "awake": luminance >= AWAKE_LUMINANCE,
        "luminance": round(luminance, 3),
        "sample": f"{SAMPLE_EDGE}x{SAMPLE_EDGE}",
    }


def require_awake_display(
    device: str,
    *,
    cwd: Path,
    timeout: float = 90.0,
    run: Callable[..., subprocess.CompletedProcess[bytes]] | None = None,
) -> dict[str, Any]:
    """Refuse a device whose screen is off, naming the one action that fixes it."""
    state = display_state(device, cwd=cwd, timeout=timeout, run=run)
    if not state["awake"]:
        raise DisplayAsleepError(state)
    return state
