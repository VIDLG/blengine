"""Production capture/provider replay with synthetic phone/BTP; never RF evidence."""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import zipfile
from pathlib import Path

from tests.peripherals.nrf52840 import write_reject as wire
from tools.react_native import rf_evidence_bundle as evidence
from tools.react_native import write_reject_ledger
from tools.react_native.host_contract import (
    ANDROID_ACTIVITY,
    ANDROID_PACKAGE,
    BUNDLE_ID,
)
from tools.tests.react_native.support.write_reject_capture import Replay


def ready(root: Path, platform: str) -> tuple[evidence.EvidenceBundle, Replay]:
    """Keep real capture/ledger schemas; replace SDK children only via Replay."""
    root.mkdir()
    project = root / "not-a-git-project"
    project.mkdir()
    bundle = evidence.EvidenceBundle(
        root / "attempt",
        platform=platform,
        device="device",
        project_root=project,
        scenario="write-reject",
    )
    snapshot = bundle.prepare_artifact(artifact(root, platform))
    h = Replay(bundle.attempt, platform)
    text, cycles = h.run()
    assert cycles == ()
    ledger = write_reject_ledger.verify(
        text,
        platform=platform,
        run_id=h.phone.plan.session_id,
        service_uuid=h.phone.plan.service_uuid,
        physical_path=h.root / wire.PHYSICAL_NAME,
        fixture_request=h.phone.plan.request_path,
        fixture_identity=(os.getpid(), os.getpgid(0)),
    )
    write(bundle.attempt / "write-reject-ledger.json", ledger)
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


def artifact(root: Path, platform: str) -> Path:
    """Minimal fake package bytes for integrity checks, not executable or RF proof."""
    if platform == "android":
        path = root / "Synthetic.apk"
        with zipfile.ZipFile(path, "w") as archive:
            for name in (
                "assets/index.android.bundle",
                "classes.dex",
                "lib/arm64-v8a/libblehub_uniffi.so",
                "lib/arm64-v8a/libBleHubReactNative.so",
            ):
                archive.writestr(name, b"synthetic only; not RF evidence")
    else:
        path = root / "Synthetic.app"
        path.mkdir()
        (path / "Info.plist").write_bytes(
            plistlib.dumps(
                {
                    "CFBundleExecutable": "Synthetic",
                    "CFBundleIdentifier": BUNDLE_ID,
                }
            )
        )
        (path / "main.jsbundle").write_bytes(b"synthetic JS")
        (path / "Synthetic").write_bytes(
            bytes.fromhex("cffaedfe") + b"synthetic native"
        )
        (path / "Synthetic").chmod(0o755)
    return path


def write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def load(path: Path) -> dict:
    return json.loads(path.read_bytes())


def rehash(attempt: Path) -> None:
    """Allow semantic attacks past inventory checks, never recompute the verdict."""
    manifest = attempt / evidence.MANIFEST
    document = load(manifest)
    document["files"] = evidence._inventory(attempt)
    document["references"] = evidence._references(
        document["files"], document["platform"], document["runtime_log"], "write-reject"
    )
    runtime = attempt / document["runtime_log"]
    document["flow_index"] = (
        evidence._flow_index(runtime, document["runtime_log"], "write-reject")
        if runtime.exists()
        else evidence._empty_index(document["runtime_log"], "write-reject")
    )
    write(manifest, document)


def replace_runtime(attempt: Path, before: str, after: str) -> None:
    """Rebind all runtime digests to malicious bytes to test semantic replay."""
    for name in (
        "write-reject-runtime.log",
        "write-reject-runtime.raw.log",
        "coordination/runtime.log",
    ):
        path = attempt / name
        text = path.read_text()
        assert before in text
        path.write_text(text.replace(before, after, 1), encoding="utf-8")
    bind_runtime_digest(attempt)


def swap_runtime_records(attempt: Path, first: str, second: str) -> None:
    """Reorder two real records, preserving each platform's original raw headers."""
    for name in (
        "write-reject-runtime.log",
        "write-reject-runtime.raw.log",
        "coordination/runtime.log",
    ):
        path = attempt / name
        lines = path.read_text().splitlines(keepends=True)
        a = next(i for i, line in enumerate(lines) if first in line)
        b = next(i for i, line in enumerate(lines) if second in line)
        lines[a], lines[b] = lines[b], lines[a]
        path.write_text("".join(lines), encoding="utf-8")
    bind_runtime_digest(attempt)


def bind_runtime_digest(attempt: Path) -> None:
    """Keep physical/ledger hashes consistent even when the transcript is invalid."""
    digest = hashlib.sha256(
        (attempt / "write-reject-runtime.log").read_bytes()
    ).hexdigest()
    path = attempt / "coordination/write-reject-physical.json"
    physical = load(path)
    physical["boundaries"][-1]["runtime_sha256"] = digest
    write(path, physical)
    path = attempt / "write-reject-ledger.json"
    ledger = load(path)
    ledger["runtime_sha256"] = digest
    write(path, ledger)
