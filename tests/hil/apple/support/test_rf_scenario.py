"""Compile the actual Apple RF scenario and run only its pure event-selection tests.

Run with: pixi run -e apple-ci python tests/hil/apple/support/test_rf_scenario.py
Requires the production package staged by `just apple test`; never runs RF.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path


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
            )
        )
        for source in sources:
            shutil.copy2(source, tests / source.name)
        subprocess.run(
            [
                "swift",
                "test",
                "--quiet",
                "--package-path",
                str(package),
                "--filter",
                "AppleCoreBluetoothRfScenarioTests|ExternalFixtureConfigurationTests",
            ],
            check=True,
            timeout=540,
        )


if __name__ == "__main__":
    main()
