# Evidence-guard cost: identity instead of content — 2026-11-13

## Problem

`pixi run just react-native tools-test` took ~29 minutes for 2020 tests, and the four Android
suites accounted for nearly all of it. The instinct that "a long wait means something is wrong"
was correct, but not in the way it first looks: `time` on the slowest suite reported
`real 7m37 ≈ user 2m33 + sys 5m04`, i.e. **no waiting at all** — every second was system-call
time. `time.sleep` was already faked by the harness.

`cProfile` named the cost precisely:

| function | calls | cumulative |
|---|---|---|
| `bleng_android_session._record_digest` | 642,352 | 18.4 s of a 43 s two-test sample |
| `posix.open` / `posix.stat` | 752,133 / 2,638,775 | — |
| `state_control._require_owner` | 335,880 | 11.6 s |

Two owners were re-reading files on **every** guard, and a guard runs at every UI polling
boundary:

- `_records_current()` re-hashed *every* receipt this run had published. Receipts grow with the
  number of device commands (183 in one lifecycle test) while guards grow with UI polling
  (1214 in the same test), so the work was O(guards × receipts) — quadratic in the run.
- `candidate._guard()` re-hashed the whole frozen APK (47–118 MB) on every guard.
- `_require_owner()` re-read `owner.json` on every guard.

## Fix: the identity a digest was computed under is itself evidence

Every one of these files was created by its owner, is never legitimately rewritten, and its
digest is already recorded. So the owner now records the **lstat identity** it read the digest
under — `(st_dev, st_ino, st_mode, st_nlink, st_size, st_mtime_ns, st_ctime_ns)` — and a later
guard compares that identity with a single `lstat`:

- identical identity ⇒ the bytes are the ones already authenticated, with no read;
- different identity ⇒ the file is not the one that was authenticated.

For published evidence the second case is a failure, and it is **stricter than before**:
re-hashing would have accepted a same-content replacement (same digest, new inode), which the
identity comparison refuses. `test_restored_evidence_guard_permanently_detects_replacement`
already demanded exactly this and now encodes it explicitly.

One deliberate exception: the phone **lease** (`owner.json`) treats its bytes as authoritative,
because a restore of the exact owner bytes must keep the lease working
(`test_shared_close_lease_loss_refuses_all_compensation_io` asserts that a restored lease is
still usable). There the identity is a fast path and a mismatch falls back to one read. Two
different contracts, each written down where it lives.

## Result

| suite | before | after |
|---|---|---|
| `test_bleng_android_lifecycle` | 457 s | **93 s** (−80%) |
| `test_bleng_android_candidate` | ~190 s | **22 s** (−88%) |
| `test_bleng_android_prepared_scan` | 135 s | **23 s** (−83%) |
| `test_bleng_android_scan` | 163 s | **31 s** (−81%) |

Targeted whole-suite checks after the change: 255 tests across the four Android suites pass in
183 s; 177 host/UI tests in 19 s; 513 nRF52840 tests in 29 s; `just fmt`, `just lint` and
`git diff --check` clean.

## What was not done, and why

`_records_current` is still O(receipts) per guard, because 183 receipts × 1214 guards of cheap
`lstat` calls remain. Removing that needs a different evidence model (one append-only receipt log
per run rather than one file per command, or a guard cadence that is not every poll). Both change
what a bundle contains or what a guard promises, so they are an architecture decision for the
plan — not a performance patch. The remaining `lstat` cost is ~1.4 s per test and is not the
binding constraint today.

Two sibling owners (`bleng_android_close_diagnostics`, `bleng_android_device_capture`) still
verify receipts by reading them (`_snapshot(path)[1] != pin`). They did not appear in the hot
path for these suites, so they were left untouched rather than changed speculatively; the same
identity-first shape applies if they ever become hot.
