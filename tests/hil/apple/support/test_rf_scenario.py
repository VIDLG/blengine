"""Compile Apple RF sources and run deterministic selection/witness tests.

Run with: pixi run -e apple-ci python tests/hil/apple/support/test_rf_scenario.py
Requires the production package staged by `just apple test`; never runs RF.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


def _verify_system_loss_provider(fixtures: Path) -> None:
    """Replay Swift output through the real provider with a synthetic BTP boundary."""
    # This file is also invoked directly by the maintained Apple recipe.
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from tests.peripherals.nrf52840 import system_loss as api
    from tests.peripherals.nrf52840.cancellation_control import read_document
    from tests.peripherals.nrf52840.support.system_loss_emulator import Harness

    for case in (api.WRITE_CCC_ENABLE, api.CCC_DISABLE_WRITE):
        documents = {}
        for role in ("setup", "control", "base", "pending"):
            raw = (fixtures / case / (role + ".json")).read_bytes()
            value = json.loads(raw)
            if raw != api._canonical(value):
                raise AssertionError(
                    f"{case}/{role}: Swift/provider canonical bytes differ"
                )
            documents[role] = value
        control = documents["control"]
        if documents["setup"] != {k: v for k, v in control.items() if k != "token"}:
            raise AssertionError("control changed Swift setup")
        with tempfile.TemporaryDirectory(prefix="blehub-loss-interop-") as temporary:
            h = Harness(Path(temporary), case, native_control=control)
            if h.control != control:
                raise AssertionError("prepare_control changed Swift control")
            with (
                patch.object(api.time, "monotonic", side_effect=h.clock.monotonic),
                patch.object(api.time, "sleep", side_effect=h.clock.sleep),
            ):
                h.owner.arm(documents["base"])
                h.wire.capture()
                pending = documents["pending"]
                # Validate negative translations with the actual provider, not a
                # second schema. Rejected candidates must never create a binding.
                for mutation in ("numeric-id", "rounded-id", "missing-null", "digest"):
                    bad = copy.deepcopy(pending)
                    if mutation == "numeric-id":
                        bad["events"][-3]["command_id"] = int(
                            bad["events"][-3]["command_id"]
                        )
                    elif mutation == "rounded-id":
                        bad["events"][-2]["command_id"] = str(
                            int(float(bad["events"][-2]["command_id"]))
                        )
                    elif mutation == "missing-null":
                        write = next(
                            row
                            for row in bad["events"]
                            if row.get("operation") == "response-write"
                        )
                        del write["subscription_id"]
                    else:
                        bad["control_sha256"] = "0" * 64
                    try:
                        h.owner._witness(bad, pending=True)
                    except ValueError:
                        pass
                    else:
                        raise AssertionError(f"provider accepted {mutation}")
                h.witness = pending
                report = h.owner.observe(pending)
                binding = read_document(h.root / api.BINDING_NAME)
                queue = pending["events"][-1]
                for name in (
                    "head_command_id",
                    "sibling_command_id",
                    "subscription_id",
                ):
                    if binding[name] != queue[name]:
                        raise AssertionError("provider changed an opaque Swift ID")
                if not all(
                    int(binding[name]) > 2**53
                    for name in ("head_command_id", "sibling_command_id")
                ):
                    raise AssertionError(
                        "interop fixture no longer protects full-width IDs"
                    )
                if binding["witness_sha256"] != api.witness_digest(pending):
                    raise AssertionError("Swift/provider witness digest drift")
                h.owner.authorize_off(h.off_request(), pending)
                h.disconnect()
                result = h.owner.poll_disconnect(pending)
                receipt = read_document(h.root / api.RECEIPT_NAME)
                if (
                    result["state"] != "disconnected-retired"
                    or result["radio_authorized"] is not False
                    or result["recovery"] != "not-evaluated"
                    or receipt["binding_sha256"] != report["binding_sha256"]
                ):
                    raise AssertionError("provider receipt/scope drift")
        print(
            f"system_loss_swift_provider_interop={case}:passed (synthetic; no RF)",
            flush=True,
        )


def main() -> None:
    support = Path(__file__).resolve().parent
    root = support.parents[3]
    production = root / ".work/staged/apple-production"
    if not (production / "Package.swift").is_file():
        raise SystemExit("Stage the production package with just apple test first")
    manifest = f"""// swift-tools-version: 5.9
import PackageDescription
let package = Package(
    name: "AppleRfSelectionTests",
    platforms: [.macOS(.v13)],
    dependencies: [.package(name: "BleHubUniFfi", path: {json.dumps(str(production))})],
    targets: [.testTarget(
        name: "AppleRfSelectionTests",
        dependencies: [.product(name: "BleHubUniFfi", package: "BleHubUniFfi")],
        path: "Tests"
    )]
)
"""
    with tempfile.TemporaryDirectory(prefix="blehub-rf-selection-") as temporary:
        package = Path(temporary)
        tests = package / "Tests"
        tests.mkdir()
        (package / "Package.swift").write_text(manifest, encoding="utf-8")
        sources = list((root / "platforms/apple/Sources/BleHubApple").glob("*.swift"))
        sources.extend(
            support / name
            for name in (
                "SwiftProbeHarness.swift",
                "RfFixtureConfiguration.swift",
                "AppleCoreBluetoothRfScenario.swift",
                "AppleCoreBluetoothRfScenarioTests.swift",
                "ExternalFixtureConfigurationTests.swift",
                "SystemLossWitnessTests.swift",
            )
        )
        # BLeng orchestration belongs only to the HIL App; these seams use the
        # canonical thin image and the actual production Apple diagnostic types.
        sources.extend(
            root / "tests/hil/apple/bleng-ios/Sources" / name
            for name in (
                "SystemLossArguments.swift",
                "SystemLossWitness.swift",
                "SystemLossWitnessBackend.swift",
            )
        )
        for source in sources:
            shutil.copy2(source, tests / source.name)
        fixtures = Path(
            os.environ.get(
                "BLEHUB_SYSTEM_LOSS_FIXTURE_EXPORT", str(package / "loss-fixtures")
            )
        )
        environment = {**os.environ, "BLEHUB_SYSTEM_LOSS_FIXTURE_EXPORT": str(fixtures)}
        subprocess.run(
            [
                "swift",
                "test",
                "--package-path",
                str(package),
                "--filter",
                (
                    "AppleCoreBluetoothRfScenarioTests|"
                    "ExternalFixtureConfigurationTests|SystemLossWitnessTests"
                ),
            ],
            check=True,
            timeout=540,
            env=environment,
        )
        _verify_system_loss_provider(fixtures)


if __name__ == "__main__":
    main()
