"""Hardware-free tests whose input is what a real device once answered.

They are device-observed regressions: every test here is named for what was seen on a phone
and replays the answer that phone actually gave. When one fails, assume the code is broken.
The rules for this directory are in `tools/tests/README.md` and are enforced by
`tools/tests/observed/test_population.py`.
"""
