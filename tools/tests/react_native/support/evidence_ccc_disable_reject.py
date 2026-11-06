"""Actual CCC provider/capture over synthetic SDK/BTP; never RF evidence."""

from __future__ import annotations

import hashlib
import os
import plistlib
from collections.abc import Callable
from pathlib import Path

from tests.peripherals.nrf52840 import ccc_disable_reject as wire
from tools.react_native import ccc_disable_reject_capture, ccc_disable_reject_ledger
from tools.react_native import rf_evidence_bundle as evidence
from tools.react_native.host_contract import (
    ANDROID_ACTIVITY,
    ANDROID_PACKAGE,
    APP_NAME,
    BUNDLE_ID,
    SCHEME,
)
from tools.tests.react_native.support.ccc_disable_reject_capture import Phone
from tools.tests.react_native.support.evidence_write_reject import artifact, load, write
from tools.tests.react_native.support.write_reject_capture import Replay as WriteReplay

LOG = "ccc-disable-reject-runtime.log"
PHYSICAL = "coordination/ccc-disable-reject-physical.json"
LEDGER = "ccc-disable-reject-ledger.json"


class Replay(WriteReplay):
    """Keep the real ledger boundary; only phone/BTP and SDK children are fake."""

    protocol = wire
    scenario = wire.SCENARIO
    capture_type = ccc_disable_reject_capture.CccDisableRejectCapture

    def _new_phone(self, root, platform):
        return Phone(root, platform, inline=False)

    def _new_owner(self):
        return wire.CccDisableReject(self.phone.fixture, self.root, self.phone.handles)


def ready(root: Path, platform: str) -> tuple[evidence.EvidenceBundle, Replay]:
    root.mkdir()
    project = root / "not-a-git-project"
    project.mkdir()
    bundle = evidence.EvidenceBundle(
        root / "attempt",
        platform=platform,
        device="device",
        project_root=project,
        scenario=wire.SCENARIO,
    )
    package = artifact(root, platform)
    if platform == "ios":
        package.rename(root / APP_NAME)
        package = root / APP_NAME
        (package / "Synthetic").rename(package / SCHEME)
        (package / "Info.plist").write_bytes(
            plistlib.dumps(
                {
                    "CFBundleExecutable": SCHEME,
                    "CFBundleIdentifier": BUNDLE_ID,
                }
            )
        )
    snapshot = bundle.prepare_artifact(package)
    h = Replay(bundle.attempt, platform)
    if platform == "ios":
        install = load(h.install)
        install["info"]["arguments"].append(str(snapshot))
        write(h.install, install)
    text, cycles = h.run()
    assert cycles == () and not h.provider_errors
    ledger = ccc_disable_reject_ledger.verify(
        text,
        platform=platform,
        run_id=h.phone.plan.session_id,
        service_uuid=h.phone.plan.service_uuid,
        physical_path=h.root / wire.PHYSICAL_NAME,
        fixture_request=h.phone.plan.request_path,
        fixture_identity=(os.getpid(), os.getpgid(0)),
    )
    write(bundle.attempt / LEDGER, ledger)
    if platform == "android":
        write(
            bundle.attempt / "artifact-install-identity.json",
            {
                "schema_version": 1,
                "status": "passed",
                "apk": snapshot.relative_to(bundle.attempt).as_posix(),
                "method": "pinned-sdk-aapt2-dump-badging",
                "package_names": [ANDROID_PACKAGE],
                "launchable_activities": [ANDROID_ACTIVITY],
                "expected_package": ANDROID_PACKAGE,
                "expected_activity": ANDROID_ACTIVITY,
            },
        )
    return bundle, h


def rehash(attempt: Path) -> None:
    """Rebind inventory/index, not a verdict, so semantic attacks reach replay."""
    manifest = attempt / evidence.MANIFEST
    doc = load(manifest)
    doc["files"] = evidence._inventory(attempt)
    doc["references"] = evidence._references(
        doc["files"],
        doc["platform"],
        doc["runtime_log"],
        wire.SCENARIO,
    )
    runtime = attempt / doc["runtime_log"]
    doc["flow_index"] = (
        evidence._flow_index(runtime, doc["runtime_log"], wire.SCENARIO)
        if runtime.exists()
        else evidence._empty_index(doc["runtime_log"], wire.SCENARIO)
    )
    write(manifest, doc)


def edit_runtime(attempt: Path, edit: Callable[[str], str]) -> None:
    """Mutate all three streams and rebind each physical prefix witness.

    Provider barriers snapshot prefixes, not only the final runtime. Preserve those
    witnesses under the attack so a missing notification fails for semantics rather
    than merely a stale digest. Native-only attacks remain invisible to the ledger.
    """
    original = (attempt / LOG).read_text()
    physical = load(attempt / PHYSICAL)
    for row in physical["boundaries"]:
        if "runtime_bytes" in row:
            prefix = original.encode()[: row["runtime_bytes"]].decode()
            changed = edit(prefix).encode()
            row.update(
                runtime_bytes=len(changed),
                runtime_sha256=hashlib.sha256(changed).hexdigest(),
            )
    for name in (LOG, "ccc-disable-reject-runtime.raw.log", "coordination/runtime.log"):
        path = attempt / name
        path.write_text(edit(path.read_text()), encoding="utf-8")
    write(attempt / PHYSICAL, physical)
    ledger = load(attempt / LEDGER)
    ledger["runtime_sha256"] = hashlib.sha256((attempt / LOG).read_bytes()).hexdigest()
    ledger["runtime_prefixes"] = [
        {key: row[key] for key in ("boundary", "runtime_bytes", "runtime_sha256")}
        for row in physical["boundaries"]
        if row["boundary"] in (*wire.PHASES, "closed")
    ]
    write(attempt / LEDGER, ledger)
