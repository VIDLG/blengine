"""Deterministic tests for maintained Appium/XCUITest helpers."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from appium.webdriver.common.appiumby import AppiumBy
from selenium.common.exceptions import NoSuchElementException

from tools.apple.ios_appium import (
    AppiumError,
    create_ios_attached_driver,
    create_ios_driver,
    source_bundle_id,
    wait_displayed,
    wait_find,
    wait_for_bundle,
)


class AppiumHelperTests(unittest.TestCase):
    def test_existing_wda_attach_has_no_build_signing_or_app_lifecycle_options(
        self,
    ) -> None:
        with patch("tools.apple.ios_appium.webdriver.Remote") as remote:
            actual = create_ios_attached_driver(
                "http://appium.invalid",
                device="00008110-001234567890ABCD",
                bundle_id="example.formal",
                wda_url="http://127.0.0.1:18101",
                command_timeout=12,
            )
        self.assertIs(actual, remote.return_value)
        options = remote.call_args.kwargs["options"].to_capabilities()
        self.assertEqual(options["appium:webDriverAgentUrl"], "http://127.0.0.1:18101")
        self.assertEqual(options["appium:udid"], "00008110-001234567890ABCD")
        for capability in (
            "autoLaunch",
            "forceAppLaunch",
            "shouldTerminateApp",
            "fullReset",
        ):
            self.assertIs(options[f"appium:{capability}"], False)
        self.assertIs(options["appium:noReset"], True)
        for capability in (
            "xcodeOrgId",
            "xcodeSigningId",
            "updatedWDABundleId",
            "usePrebuiltWDA",
            "usePreinstalledWDA",
            "useNewWDA",
            "derivedDataPath",
            "app",
        ):
            self.assertNotIn(f"appium:{capability}", options)
        self.assertEqual(remote.call_args.kwargs["client_config"].timeout, 12)

    def test_existing_wda_attach_rejects_ambiguous_endpoint_or_unbounded_input_before_remote(
        self,
    ) -> None:
        values = {
            "device": ("mock-device", "", "00008110-001234567890ABCD "),
            "bundle_id": ("", "example.formal ", "example/formal"),
            "command_timeout": (None, True, 0, -1, float("nan"), float("inf"), 300.01),
            "wda_url": (
                "http://wda.example:8100",
                "http://127.0.0.1",
                "http://127.0.0.1:0",
                "http://127.0.0.1:65536",
                "https://127.0.0.1:8100",
                "file:///tmp/wda",
                "http://user:secret@127.0.0.1:8100",
                "http://127.0.0.1:8100/path",
                "http://127.0.0.1:8100?ready=true",
                "http://127.0.0.1:8100#cached",
                "http://127.0.0.1:8100 ",
                "http://127.0.0.1:\n8100",
                "http://127.0.0.1:\t8100",
            ),
        }
        for name, cases in values.items():
            for value in cases:
                with (
                    self.subTest(name=name, value=value),
                    patch("tools.apple.ios_appium.webdriver.Remote") as remote,
                ):
                    options = {
                        "device": "00008110-001234567890ABCD",
                        "bundle_id": "example.formal",
                        "wda_url": "http://127.0.0.1:18101",
                        "command_timeout": 12,
                    }
                    options[name] = value
                    with self.assertRaises((ValueError, TypeError)):
                        create_ios_attached_driver("http://appium.invalid", **options)
                    remote.assert_not_called()

    def test_existing_wda_attach_failure_never_falls_back_to_build_or_relaunch(
        self,
    ) -> None:
        primary = RuntimeError("prepared WDA is unavailable")
        with (
            patch(
                "tools.apple.ios_appium.webdriver.Remote", side_effect=primary
            ) as remote,
            self.assertRaises(RuntimeError) as caught,
        ):
            create_ios_attached_driver(
                "http://appium.invalid",
                device="00008110-001234567890ABCD",
                bundle_id="example.formal",
                wda_url="http://127.0.0.1:18101",
                command_timeout=12,
            )
        self.assertIs(caught.exception, primary)
        remote.assert_called_once()

    def test_attach_only_session_preserves_app_and_bounds_http_commands(self) -> None:
        with patch("tools.apple.ios_appium.webdriver.Remote") as remote:
            create_ios_driver(
                "http://appium.invalid",
                device="mock-device",
                team="MOCKTEAM",
                bundle_id="example.formal",
                wda_bundle_id="example.mock.wda",
                wda_port=8101,
                derived_data=Path("mock-derived"),
                auto_launch=False,
                command_timeout=12,
            )
        options = remote.call_args.kwargs["options"].to_capabilities()
        for capability in ("autoLaunch", "forceAppLaunch", "shouldTerminateApp"):
            self.assertIs(options[f"appium:{capability}"], False)
        self.assertIs(options["appium:noReset"], True)
        self.assertEqual(remote.call_args.kwargs["client_config"].timeout, 12)

    def test_source_bundle_id_reads_wda_application_identity(self) -> None:
        source = (
            '<AppiumAUT type="Application">'
            '<XCUIElementTypeApplication bundleId="org.example.host" />'
            "</AppiumAUT>"
        )
        self.assertEqual(source_bundle_id(source), "org.example.host")

    def test_source_bundle_id_returns_none_without_application_identity(self) -> None:
        self.assertIsNone(source_bundle_id('<AppiumAUT type="Application" />'))

    def test_wait_find_retries_after_a_refreshing_accessibility_tree(self) -> None:
        element = MagicMock()
        driver = MagicMock()
        driver.find_element.side_effect = [NoSuchElementException(), element]
        actual = wait_find(driver, "rf-workbench-home", timeout=1)
        self.assertIs(actual, element)
        driver.find_element.assert_called_with(
            AppiumBy.ACCESSIBILITY_ID, "rf-workbench-home"
        )

    def test_wait_displayed_refinds_hidden_element_after_page_transition(self) -> None:
        hidden = MagicMock()
        hidden.is_displayed.return_value = False
        visible = MagicMock()
        visible.is_displayed.return_value = True
        driver = MagicMock()
        driver.find_element.side_effect = [hidden, visible]
        wait_displayed(driver, "rf-workbench-failure", timeout=1)
        self.assertEqual(driver.find_element.call_count, 2)

    def test_wait_for_bundle_rejects_a_foreign_wda_target(self) -> None:
        driver = MagicMock()
        driver.page_source = (
            '<AppiumAUT><XCUIElementTypeApplication bundleId="com.apple.springboard" />'
            "</AppiumAUT>"
        )
        with self.assertRaisesRegex(AppiumError, "org.example.host"):
            wait_for_bundle(driver, "org.example.host", timeout=0)


if __name__ == "__main__":
    unittest.main()
