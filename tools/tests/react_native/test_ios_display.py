"""Unit tests for the iOS display observation; no device is touched.

The behaviour under test is the one that cost a whole iOS sequence on 2026-11-26: a sleeping
screen is not "unlocked=false", so the only local signal is the capture itself.
"""

from __future__ import annotations

import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.react_native import ios_display


def _bmp(pixels: list[tuple[int, int, int]], width: int, height: int) -> bytes:
    """A minimal bottom-up 24-bit BMP containing exactly these pixels."""
    stride = ((24 * width + 31) // 32) * 4
    body = bytearray()
    for row in range(height):
        line = bytearray()
        for column in range(width):
            red, green, blue = pixels[row * width + column]
            line += bytes((blue, green, red))
        line += bytes(stride - len(line))
        body += line
    offset = 14 + 40
    header = b"BM" + struct.pack("<IHHI", offset + len(body), 0, 0, offset)
    info = struct.pack(
        "<IiiHHIIiiII", 40, width, height, 1, 24, 0, len(body), 0, 0, 0, 0
    )
    return header + info + bytes(body)


class MeanLuminanceTests(unittest.TestCase):
    def test_black_frame_is_zero(self) -> None:
        self.assertEqual(
            ios_display.mean_luminance(_bmp([(0, 0, 0)] * 4, 2, 2)),
            0.0,
        )

    def test_white_frame_is_full_scale(self) -> None:
        self.assertAlmostEqual(
            ios_display.mean_luminance(_bmp([(255, 255, 255)] * 4, 2, 2)),
            255.0,
        )

    def test_a_lit_pixel_lifts_the_mean_above_black(self) -> None:
        frame = _bmp([(0, 0, 0), (0, 0, 0), (0, 0, 0), (255, 255, 255)], 2, 2)
        self.assertGreater(ios_display.mean_luminance(frame), 60.0)

    def test_refuses_a_sample_it_cannot_read(self) -> None:
        for body in (b"", b"not a bitmap", _bmp([(1, 2, 3)] * 4, 2, 2)[:40]):
            with (
                self.subTest(body=body[:6]),
                self.assertRaises(ios_display.DisplayUnavailableError),
            ):
                ios_display.mean_luminance(body)


class DisplayStateTests(unittest.TestCase):
    def _runner(self, frame: bytes, calls: list[list[str]]):
        def run(command: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
            calls.append(command)
            if command[0] == "xcrun":
                Path(command[-1]).write_bytes(b"png placeholder")
            else:
                Path(command[-1]).write_bytes(frame)
            return subprocess.CompletedProcess(command, 0, b"", b"")

        return run

    def test_awake_when_the_frame_has_light(self) -> None:
        calls: list[list[str]] = []
        state = ios_display.display_state(
            "UDID",
            cwd=Path("."),
            run=self._runner(_bmp([(9, 9, 9)] * 64, 8, 8), calls),
        )
        self.assertTrue(state["awake"])
        self.assertEqual(state["luminance"], 9.0)
        self.assertEqual([call[0] for call in calls], ["xcrun", "sips"])

    def test_sleeping_when_the_frame_is_black(self) -> None:
        calls: list[list[str]] = []
        state = ios_display.display_state(
            "UDID",
            cwd=Path("."),
            run=self._runner(_bmp([(0, 0, 0)] * 64, 8, 8), calls),
        )
        self.assertFalse(state["awake"])
        self.assertEqual(state["luminance"], 0.0)

    def test_require_refuses_and_names_the_fix(self) -> None:
        with (
            tempfile.TemporaryDirectory() as scratch,
            self.assertRaisesRegex(
                ios_display.DisplayUnavailableError, "Auto-Lock to Never"
            ),
        ):
            ios_display.require_awake_display(
                "UDID",
                cwd=Path(scratch),
                run=self._runner(_bmp([(0, 0, 0)] * 64, 8, 8), []),
            )

    def test_a_failed_capture_is_an_error_not_a_verdict(self) -> None:
        def failing(
            command: list[str], **_: object
        ) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(command, 3, b"", b"no device")

        with (
            tempfile.TemporaryDirectory() as scratch,
            self.assertRaisesRegex(ios_display.DisplayUnavailableError, "no device"),
        ):
            ios_display.display_state("UDID", cwd=Path(scratch), run=failing)

    def test_budget_must_be_finite_and_positive(self) -> None:
        for timeout in (0.0, -1.0, float("nan")):
            with (
                self.subTest(timeout=timeout),
                self.assertRaises(ios_display.DisplayUnavailableError),
            ):
                ios_display.display_state("UDID", cwd=Path("."), timeout=timeout)


if __name__ == "__main__":
    unittest.main()
