"""Protect synthetic/live ownership and canonical tooling test dependencies."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RN = ROOT / "tools/react_native"
TESTS = ROOT / "tools/tests/react_native"
PERIPHERAL = ROOT / "tests/peripherals/nrf52840"
ANDROID = ROOT / "tools/android"
SUPPORT = (
    TESTS / "support",
    ROOT / "tests/support/rf_lab",
    PERIPHERAL / "support",
)
CLI = {
    "tools.react_native.run_android_pending_read_reload",
    "tools.react_native.run_ios_pending_read_reload_gate",
    "tools.react_native.run_rf_lab_full",
    "tools.react_native.run_rf_recovery_ten",
    "tools.react_native.run_android_permission_transition",
    "tools.react_native.run_android_adapter_transition",
    "tools.react_native.run_ios_adapter_startup",
}
SHARED_IMPLEMENTATIONS = {
    "pending_read_reload_runner",
    "rf_scenario_runner",
    "rf_fixture",
    "android_rf_host",
    "host_paths",
    "rf_suite_coordination",
    "rf_attempt_finalizer",
    "capture_io",
    "capture_transport",
    "recovery_ten_capture",
    "full_suite_capture",
    "ios_pending_reload_capture",
    "adapter_transition_capture",
    "permission_transition_capture",
    "permission_transition_ledger",
    "ios_adapter_startup_capture",
    "ios_adapter_startup_journal",
    "ios_adapter_startup_ledger",
    "ios_adapter_startup_evidence",
    "ios_radio_lease",
}


def imports(tree: ast.AST) -> set[str]:
    """Include from-import members so importing a test via its package is visible."""
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
            result.update(f"{node.module}.{alias.name}" for alias in node.names)
    return result


def is_test_module(name: str) -> bool:
    return any(part.startswith("test_") for part in name.split("."))


def is_support(name: str) -> bool:
    return any(
        name == prefix or name.startswith(prefix + ".")
        for prefix in (
            "tools.tests",
            "tests.support",
            "tests.peripherals.nrf52840.support",
        )
    )


class ToolingLayoutTests(unittest.TestCase):
    def test_live_modules_cannot_depend_on_synthetic_support_or_test_cases(
        self,
    ) -> None:
        paths = [*RN.glob("*.py"), *ANDROID.glob("*.py"), *PERIPHERAL.rglob("*.py")]
        for path in paths:
            if path.name.startswith("test_") or "support" in path.parts:
                continue
            with self.subTest(path=path.relative_to(ROOT)):
                dependencies = imports(ast.parse(path.read_text(encoding="utf-8")))
                self.assertFalse(
                    {
                        name
                        for name in dependencies
                        if is_support(name) or is_test_module(name)
                    }
                )
                if path.stem in SHARED_IMPLEMENTATIONS:
                    self.assertFalse(
                        dependencies & CLI, "shared implementation imports its CLI"
                    )
                if path.stem in {
                    "capture_io",
                    "capture_transport",
                    "ios_pending_reload_capture",
                    "ios_adapter_startup_capture",
                    "adapter_transition_capture",
                    "permission_transition_capture",
                }:
                    self.assertNotIn(
                        "tools.react_native.recovery_ten_capture",
                        dependencies,
                        "mechanics and non-recovery protocols must not inherit recovery state",
                    )
                if path.stem in {"capture_io", "capture_transport"}:
                    self.assertNotIn(
                        "tests.peripherals.nrf52840.recovery_ten", dependencies
                    )
                    self.assertNotIn(
                        "tests.peripherals.nrf52840.pending_reload", dependencies
                    )

    def test_shared_support_and_tests_do_not_depend_on_other_test_modules(self) -> None:
        paths = [
            *TESTS.glob("test_*.py"),
            *ANDROID.glob("test_*.py"),
            *PERIPHERAL.rglob("test_*.py"),
        ]
        helpers = {path for root in SUPPORT for path in root.glob("*.py")}
        for path in [*paths, *sorted(helpers)]:
            with self.subTest(path=path.relative_to(ROOT)):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                dependencies = imports(tree)
                self.assertFalse(
                    {name for name in dependencies if is_test_module(name)}
                )
                if path in helpers:
                    self.assertFalse(
                        any(
                            isinstance(node, ast.Attribute)
                            and node.attr == "TestCase"
                            or isinstance(node, ast.Name)
                            and node.id == "TestCase"
                            for node in ast.walk(tree)
                        ),
                        "support must not instantiate or inherit a test case",
                    )
                if path.parent in SUPPORT[1:]:
                    self.assertFalse(
                        {
                            name
                            for name in dependencies
                            if name.startswith("tools.tests")
                        },
                        "shared/provider support must not depend on RN-tooling-only support",
                    )
