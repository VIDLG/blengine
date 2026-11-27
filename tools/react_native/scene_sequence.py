"""Run one platform's managed scenes in dependency order, as one sequence.

Each scene is the same producer the single-scene entry runs: its own run id, its own phone
lease, its own evidence root (`<output>/<scene>`) and its own verified bundle. Nothing is
shared between scenes except the arguments a caller states once.

Three rules make a sequence meaningful rather than unrelated runs:

* The platform states what must be true of the device before a scene is admitted. iOS
  releases the App a passing scene keeps as evidence; Android's producer requires that no
  process of its package is running and replaces the installation itself, so it needs
  nothing.
* The sequence stops at the first scene that does not pass. A later scene admitted after a
  failed one would be measured against a device state the failure never cleaned up.
* A repeating scene states the budget it needs (`SceneEvidence.min_seconds`). The sequence
  raises that scene's run budget to its own floor, so one declared `--timeout` fits every
  scene in the order instead of being refused by the one scene that repeats.

* The sequence names one progress journal and hands it to every scene, so a harness polls
  one small file for the whole run instead of tailing a log. The engine adds its own lines
  to it: one per scene and a final one that always appears, even when the run is interrupted.

The engine owns the argument contract, order, budgets and the summary line. It owns no
device action, no platform knowledge and no scene semantics: a platform entry supplies its
order, its single-scene producer and, where it has one, its pre-admission release.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NamedTuple

from .bleng_scan_evidence import SCENES, SceneEvidence
from .rf_runner import JOURNAL_FLAG, JOURNAL_NAME

SCENE_FLAG = "--scene"
OUTPUT_FLAG = "--output"
TIMEOUT_FLAG = "--timeout"

# Every managed scene, in dependency order: discovery before a subscription, a subscription
# before the read/write path, the normal path before the scenes that arm a fault, and one
# recovery cycle before ten. A platform's sequence runs exactly this order, so a scene added
# to `SCENES` without a place here fails its test instead of being forgotten.
ORDER: tuple[str, ...] = (
    "scan",
    "notification",
    "indication",
    # The fault-free subscription scenes come before the fault-armed ones: a background
    # cycle proves the plain path still works before anything parks a write.
    "background",
    "io",
    # Facts about the live link: MTU query/request, connection priority, cache invalidation.
    # No peer fault and no product surface beyond the connection itself.
    "link-facts",
    # The operator's own end over a live subscription is still a fault-free path: nothing is
    # armed in the peer, so it runs before any scene that parks a request in the firmware.
    "disconnect",
    # The peer's CCCD across a subscription's life: same fault-free shape, one process.
    "descriptor-io",
    # The platform's own inventory: no fault, no subscription; it only needs a live link whose
    # peer this adapter must therefore report as connected.
    "devices-query",
    # The inventories the main line never needs: `bonded` and `companion`, no peer required.
    "devices-kinds",
    # Radio power, both directions: another adapter fact with no peer. It runs here so a
    # platform that really can change the radio leaves it ON before the peer scenes start.
    "adapter-control",
    # A connect the peer cannot answer: nothing is armed either, but it needs a peer that is
    # *absent*, so it runs after every scene that expects a discoverable peer has passed.
    "connect-failure",
    # A standing request answered against a peer the host had withdrawn: still fault-free (the
    # peer itself may not fail), but it needs a peer that can be taken away and restored.
    "auto-connect",
    "cancellation",
    "write-reject",
    "ccc-enable-reject",
    "ccc-disable-reject",
    "passive-disconnect",
    "recovery",
    "recovery-ten",
)


class SceneSequenceError(RuntimeError):
    """The sequence could not even be started; no scene was admitted."""


class SceneOutcome(NamedTuple):
    """One scene's exit code and the evidence root it published under."""

    scene: str
    code: int
    root: Path


def flag_value(arguments: Sequence[str], flag: str) -> str | None:
    """The value the caller stated for one flag, in either of argparse's two spellings."""
    for index, item in enumerate(arguments):
        if item == flag and index + 1 < len(arguments):
            return arguments[index + 1]
        if item.startswith(flag + "="):
            return item.split("=", 1)[1]
    return None


def parse(argv: Sequence[str] | None, *, description: str) -> tuple[Path, list[str]]:
    """The argument contract every platform's sequence entry shares.

    The sequence owns `--output` (its root) and rejects `--scene`; everything else is the
    single-scene producer's and is passed through untouched, in the caller's own spelling.
    """
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="sequence root; each scene publishes under <output>/<scene>",
    )
    known, arguments = parser.parse_known_args(argv)
    # argparse reads a repeated option last-wins, so a second --output would silently move
    # the sequence root; it has to be counted in the caller's own argv.
    spellings = (OUTPUT_FLAG, OUTPUT_FLAG + "=")
    if (
        sum(
            item.startswith(spelling) for item in (argv or ()) for spelling in spellings
        )
        > 1
    ):
        parser.error(
            "the sequence owns --output; each scene publishes under <output>/<scene>"
        )
    for flag, reason in (
        (
            SCENE_FLAG,
            "the sequence runs every managed scene; --scene belongs to a single-scene run",
        ),
        (
            JOURNAL_FLAG,
            "the sequence names one progress journal for all its scenes",
        ),
    ):
        if any(item == flag or item.startswith(flag + "=") for item in arguments):
            parser.error(reason)
    return known.output.absolute(), arguments


def create_root(output: Path) -> None:
    """Create the sequence root exclusively, so no run appends to another run's evidence."""
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise SceneSequenceError(
            f"refusing an existing sequence root: {output}"
        ) from None


def require_order(order: Sequence[str]) -> tuple[str, ...]:
    """Refuse an unknown or repeated scene before any evidence root exists."""
    names = tuple(order)
    unknown = [name for name in names if name not in SCENES]
    if unknown:
        raise ValueError(f"unknown managed scene: {unknown[0]}")
    if len(set(names)) != len(names):
        raise ValueError("a sequence names each managed scene once")
    return names


def scene_budget(
    scene: SceneEvidence, declared: float | None, *, preparation_seconds: float
) -> float | None:
    """The run budget one scene is handed: the caller's, raised to the scene's own floor.

    `None` leaves the single-scene entry's default in charge. A scene floor is its declared
    scenario minimum plus the preparation the producer requires to remain after install and
    fixture startup, so a scene never fails its own budget check inside a sequence.
    """
    if scene.min_seconds is None:
        return declared
    floor = scene.min_seconds + preparation_seconds
    return floor if declared is None else max(declared, floor)


def sequence_arguments(
    arguments: Sequence[str],
    scene: str,
    output: Path,
    *,
    timeout: float | None = None,
    journal: Path | None = None,
) -> list[str]:
    """The exact argv one scene receives: the caller's, plus this scene's own scope."""
    if scene not in SCENES:
        raise ValueError(f"unknown managed scene: {scene}")
    per_scene = list(arguments)
    if timeout is not None:
        per_scene.extend((TIMEOUT_FLAG, str(timeout)))
    if journal is not None:
        per_scene.extend((JOURNAL_FLAG, str(journal)))
    return [*per_scene, SCENE_FLAG, scene, OUTPUT_FLAG, str(output)]


def journal_line(path: Path, **fields: object) -> None:
    """Append one event to a journal; a journal never decides how a run ends."""
    try:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {"scenario": "sequence", "at": time.time(), **fields},
                    ensure_ascii=False,
                    default=str,
                )
                + "\n"
            )
    except (OSError, ValueError, TypeError):
        pass


def run_sequence(
    *,
    output: Path,
    arguments: Sequence[str],
    order: Sequence[str],
    run: Callable[[list[str]], int],
    before_scene: Callable[[str, Path], None] | None = None,
    preparation_seconds: float = 0.0,
) -> list[SceneOutcome]:
    """Run each scene once, stopping at the first failure; return every outcome.

    A scene root is created exclusively, so a re-run of a sequence never appends to or
    overwrites another run's evidence. `before_scene` runs under that root before the
    producer is admitted — the first scene included, because a sequence never assumes the
    device starts clean.
    """
    names = require_order(order)
    declared = flag_value(arguments, TIMEOUT_FLAG)
    try:
        declared_budget = None if declared is None else float(declared)
    except ValueError:
        raise SceneSequenceError(
            f"{TIMEOUT_FLAG} must be a number of seconds"
        ) from None
    journal = output / JOURNAL_NAME
    outcomes: list[SceneOutcome] = []
    try:
        for name in names:
            root = output / name
            root.mkdir(parents=True, exist_ok=False)
            journal_line(journal, event="scene", scene=name, status="started")
            if before_scene is not None:
                before_scene(name, root)
            budget = scene_budget(
                SCENES[name], declared_budget, preparation_seconds=preparation_seconds
            )
            # The caller's own spelling stands unless the scene's floor raised it.
            timeout = None if budget == declared_budget else budget
            code = run(
                sequence_arguments(
                    arguments, name, root, timeout=timeout, journal=journal
                )
            )
            outcomes.append(SceneOutcome(name, code, root))
            journal_line(
                journal,
                event="scene",
                scene=name,
                status="passed" if code == 0 else "failed",
                exit=code,
            )
            if code != 0:
                break
    except BaseException as error:
        journal_line(
            journal, event="finished", status="interrupted", error=type(error).__name__
        )
        raise
    attempted = {outcome.scene for outcome in outcomes}
    journal_line(
        journal,
        event="finished",
        status="passed" if attempted == set(names) else "failed",
        passed=[outcome.scene for outcome in outcomes if outcome.code == 0],
        failed=[outcome.scene for outcome in outcomes if outcome.code != 0],
        skipped=[name for name in names if name not in attempted],
    )
    return outcomes


def report(
    outcomes: Sequence[SceneOutcome],
    order: Sequence[str],
    root: Path,
    *,
    out=None,
    err=None,
) -> int:
    """Print one line per scene and one verdict; return the sequence's exit code."""
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    for scene, code, scene_root in outcomes:
        state = "passed" if code == 0 else f"failed exit={code}"
        print(f"scene={scene} {state} result_root={scene_root}", file=out)
    passed = [scene for scene, code, _ in outcomes if code == 0]
    failed = [(scene, scene_root) for scene, code, scene_root in outcomes if code != 0]
    attempted = {scene for scene, _, _ in outcomes}
    skipped = [scene for scene in order if scene not in attempted]
    if skipped:
        print(f"scene_sequence=stopped skipped={','.join(skipped)}", file=err)
    if failed:
        scene, scene_root = failed[0]
        print(
            f"scene_sequence=failed scene={scene} "
            f"passed={','.join(passed) or 'none'} root={scene_root}",
            file=err,
        )
        return 2
    print(f"scene_sequence=passed scenes={','.join(passed)} root={root}", file=out)
    return 0


__all__ = [
    "ORDER",
    "OUTPUT_FLAG",
    "SCENE_FLAG",
    "TIMEOUT_FLAG",
    "SceneOutcome",
    "SceneSequenceError",
    "create_root",
    "flag_value",
    "parse",
    "report",
    "require_order",
    "run_sequence",
    "scene_budget",
    "sequence_arguments",
]
