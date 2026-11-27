"""Read one scene driver's receipt the same way on every platform.

A driver runs a plan and publishes a receipt; the receipt's *meaning* is platform-neutral and
belongs here, not in each executor. Two things must be decoded identically or a scene's
evidence would depend on which device it ran on:

* which receipt this is (`scope`, and at most the plan's own steps);
* every `run-status` read the plan asked for, decoded strictly — a drifted projection has to
  fail the scene instead of silently stopping being checked.

A **failed** scene stops at the step that failed, so its receipt reports a prefix of the plan.
That receipt is the most valuable evidence in the bundle (it names the step and what the App
showed), so it is admitted as a prefix and its reads are decoded leniently: a read that never
happened is absent rather than an error. A passing receipt must still report every step and
decode strictly.

The drivers themselves match `status` conditions loosely (a frame that does not decode simply
does not satisfy a condition); the strict rejection lives here.
"""

from __future__ import annotations

from typing import Any

from . import run_status
from .scene_plans import STATUS_ID

# Each executor's receipt names its own scope, so an attachment or file from the other lane
# can never be misread as this one's.
IOS_RECEIPT_SCOPE = "ios-scene-driver"
ANDROID_RECEIPT_SCOPE = "android-scene-driver"


def require_receipt(receipt: Any, *, scope: str, steps: int) -> dict[str, Any]:
    """Require this driver's receipt for a [steps]-step plan.

    A passed receipt must report every step. A failed one stops at the failing step, so it
    reports a prefix; that is the evidence that names the step, not a malformed artifact.
    A receipt longer than the plan cannot be trusted either way.
    """
    if type(receipt) is not dict or receipt.get("scope") != scope:
        raise RuntimeError("scene receipt is not this driver's scope")
    reported = receipt.get("steps")
    if type(reported) is not list or len(reported) > steps:
        raise RuntimeError(
            f"scene receipt reports {len(reported) if isinstance(reported, list) else reported} "
            f"steps of a {steps}-step plan"
        )
    if receipt.get("status") == "passed" and len(reported) != steps:
        raise RuntimeError(
            f"a passed scene receipt must report all {steps} steps, not {len(reported)}"
        )
    return receipt


def decode_projection(
    plan: dict[str, Any], receipt: dict[str, Any], *, partial: bool = False
) -> dict[str, dict[str, str]]:
    """Strictly decode every `run-status` read the plan asked for.

    `partial` is for a failed scene's prefix receipt: a read that never happened is simply
    absent, while a read that did happen is still decoded strictly.
    """
    values = receipt.get("values")
    if type(values) is not dict:
        raise RuntimeError("scene receipt carries no values")
    decoded: dict[str, dict[str, str]] = {}
    for step in plan.get("steps") or []:
        if not isinstance(step, dict):
            continue
        if step.get("op") != "read" or step.get("id") != STATUS_ID:
            continue
        name = step.get("as")
        value = values.get(name)
        if value is None and partial:
            continue
        if type(value) is not str:
            raise RuntimeError(f"scene receipt has no {name!r} read")
        try:
            decoded[str(name)] = run_status.parse(value)
        except run_status.RunStatusError as error:
            raise RuntimeError(
                f"run-status read {name!r} is not this contract: {error}"
            ) from error
    return decoded
