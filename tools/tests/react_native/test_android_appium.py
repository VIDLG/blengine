"""Deterministic tests for maintained Android Appium helpers."""

from __future__ import annotations

import io
import os
import subprocess
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from unittest.mock import MagicMock, patch

from appium.webdriver.appium_connection import AppiumConnection
from appium.webdriver.common.appiumby import AppiumBy
from selenium.common.exceptions import NoSuchElementException
from urllib3.exceptions import MaxRetryError, ReadTimeoutError

from tools.android.android_appium import (
    AppiumError,
    create_android_driver,
    install_uiautomator2_support,
    uiautomator2_support_apks,
    wait_displayed,
    wait_find,
    wait_for_package,
)
from tools.react_native import android_sdk


class AndroidAppiumHelperTests(unittest.TestCase):
    def test_attach_only_session_preserves_app_and_bounds_http_commands(self) -> None:
        with (
            patch("tools.android.android_appium.webdriver.Remote") as remote,
            patch.object(
                android_sdk.subprocess,
                "run",
                side_effect=AssertionError(
                    "ordinary attach must not provision helpers"
                ),
            ) as commands,
        ):
            create_android_driver(
                "http://appium.invalid",
                device="mock-device",
                package="example.formal",
                activity=".MainActivity",
                auto_launch=False,
                command_timeout=12,
            )
        commands.assert_not_called()
        remote.assert_called_once()
        options = remote.call_args.kwargs["options"].to_capabilities()
        for capability in ("autoLaunch", "forceAppLaunch", "shouldTerminateApp"):
            self.assertIs(options[f"appium:{capability}"], False)
        self.assertIs(options["appium:noReset"], True)
        self.assertEqual(remote.call_args.kwargs["client_config"].timeout, 12)

    def test_explicit_timeout_disables_real_sdk_pool_retries(self) -> None:
        for system_port in (None, 8201):
            with self.subTest(system_port=system_port):
                with patch("tools.android.android_appium.webdriver.Remote") as remote:
                    create_android_driver(
                        "http://appium.invalid",
                        device="mock-device",
                        package="example.formal",
                        activity=".MainActivity",
                        auto_launch=False,
                        command_timeout=5,
                        system_port=system_port,
                    )
                remote.assert_called_once()
                options = remote.call_args.kwargs["options"].to_capabilities()
                self.assertEqual(options["appium:udid"], "mock-device")
                self.assertEqual(options["appium:appPackage"], "example.formal")
                self.assertEqual(options["appium:appActivity"], ".MainActivity")
                if system_port is not None:
                    self.assertEqual(options["appium:systemPort"], system_port)
                    self.assertIs(options["appium:autoGrantPermissions"], False)
                    self.assertIs(options["appium:skipServerInstallation"], True)
                config = remote.call_args.kwargs["client_config"]
                self.assertEqual(config.timeout, 5)
                connection = AppiumConnection(client_config=config, ignore_proxy=True)
                try:
                    # Inspect the effective urllib3 pool, not just the input dict.
                    pool = connection._conn.connection_from_url(
                        config.remote_server_addr
                    )
                    self.assertEqual(pool.retries.total, 0)
                finally:
                    connection.close()

    def test_prepared_http_read_timeout_sends_get_and_post_only_once(self) -> None:
        release = Event()
        requests: list[tuple[str, str]] = []

        class StalledHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                requests.append((self.command, self.path))
                release.wait(timeout=2)

            do_POST = do_GET

            def log_message(self, format: str, *args: object) -> None:
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), StalledHandler) as server:
            # server_close must also join the bounded request-handler threads.
            server.daemon_threads = False
            server_url = f"http://127.0.0.1:{server.server_port}"
            thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
            thread.start()
            connection = None
            try:
                with patch("tools.android.android_appium.webdriver.Remote") as remote:
                    create_android_driver(
                        server_url,
                        device="mock-device",
                        package="example.formal",
                        activity=".MainActivity",
                        auto_launch=False,
                        command_timeout=0.1,
                        system_port=8201,
                    )
                remote.assert_called_once()
                connection = AppiumConnection(
                    client_config=remote.call_args.kwargs["client_config"],
                    ignore_proxy=True,
                )
                for method, path in (
                    ("GET", "/session/synthetic/element/close/rect"),
                    ("POST", "/session"),
                ):
                    with self.subTest(method=method):
                        requests.clear()
                        with self.assertRaises((MaxRetryError, ReadTimeoutError)):
                            connection._request(method, server_url + path)
                        self.assertEqual(requests, [(method, path)])
            finally:
                release.set()
                if connection is not None:
                    connection.close()
                server.shutdown()
                thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

    def test_session_submission_timeout_has_no_retry_or_fallback(self) -> None:
        error = ReadTimeoutError(None, "/session", "synthetic submission timeout")
        with (
            patch(
                "tools.android.android_appium.webdriver.Remote", side_effect=error
            ) as remote,
            self.assertRaises(ReadTimeoutError) as raised,
        ):
            create_android_driver(
                "http://appium.invalid",
                device="mock-device",
                package="example.formal",
                activity=".MainActivity",
                auto_launch=False,
                command_timeout=5,
                system_port=8201,
            )
        self.assertIs(raised.exception, error)
        remote.assert_called_once()

    def test_wait_find_uses_cross_platform_accessibility_id(self) -> None:
        element = MagicMock()
        driver = MagicMock()
        driver.find_element.side_effect = [NoSuchElementException(), element]
        actual = wait_find(driver, "rf-workbench-home", timeout=1)
        self.assertIs(actual, element)
        driver.find_element.assert_called_with(
            AppiumBy.ANDROID_UIAUTOMATOR,
            'new UiSelector().resourceId("rf-workbench-home")',
        )

    def test_wait_displayed_retries_after_hidden_element(self) -> None:
        hidden = MagicMock()
        hidden.is_displayed.return_value = False
        visible = MagicMock()
        visible.is_displayed.return_value = True
        driver = MagicMock()
        driver.find_element.side_effect = [hidden, visible]
        wait_displayed(driver, "rf-workbench-failure", timeout=1)
        self.assertEqual(driver.find_element.call_count, 2)

    def test_wait_for_package_accepts_exact_package(self) -> None:
        driver = MagicMock()
        driver.current_package = "com.blehubnitrohost"
        wait_for_package(driver, "com.blehubnitrohost", timeout=1)

    def test_wait_for_package_rejects_foreign_package(self) -> None:
        driver = MagicMock()
        driver.current_package = "com.android.settings"
        with self.assertRaisesRegex(AppiumError, "com.blehubnitrohost"):
            wait_for_package(driver, "com.blehubnitrohost", timeout=0)


class AndroidAppiumPreparationTests(unittest.TestCase):
    """Exercise the existing rooted installer, with only subprocess/SDK paths faked."""

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.driver = self.root / "node_modules/appium-uiautomator2-driver"
        self.settings = (
            self.driver / "node_modules/io.appium.settings/apks/settings_apk-debug.apk"
        )
        self.server = (
            self.driver
            / "node_modules/appium-uiautomator2-server/apks/appium-uiautomator2-server-v10.7.6.apk"
        )
        self.test = self.server.with_name(
            "appium-uiautomator2-server-debug-androidTest.apk"
        )
        for path in (self.settings, self.server, self.test):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic APK, never sent to a device")
        self.adb = self.root / "synthetic-sdk/adb"
        self.serial = "synthetic-rooted-phone"
        self.enterContext(
            patch.object(android_sdk, "adb_executable", return_value=self.adb)
        )
        self.commands: list[list[str]] = []
        self.install_failure = False
        self.sdk = self.enterContext(
            patch(
                "tools.android.run_instrumentation.subprocess.run",
                side_effect=self.device_command,
            )
        )

    def device_command(self, command, **kwargs):
        self.assertEqual(command[:3], [str(self.adb), "-s", self.serial])
        self.assertGreater(kwargs["timeout"], 0)
        self.commands.append(command)
        arguments = command[3:]
        if arguments[:3] == ["shell", "su", "-c"]:
            self.assertTrue(
                arguments[3].startswith(
                    "pm install -r -g /data/local/tmp/blehub-instrumentation-"
                )
            )
            if self.install_failure:
                return subprocess.CompletedProcess(
                    command, 1, "Failure [synthetic denial]\n"
                )
            return subprocess.CompletedProcess(command, 0, "Success\n")
        allowed = (
            arguments[:1] == ["push"]
            or arguments[:3] == ["shell", "rm", "-f"]
            or arguments
            in (
                ["shell", "am", "force-stop", "com.android.packageinstaller"],
                ["shell", "am", "force-stop", "com.oplus.appdetail"],
            )
        )
        self.assertTrue(allowed, f"unexpected device command: {command}")
        return subprocess.CompletedProcess(command, 0, "")

    def test_explicit_helper_provision_uses_rooted_installer_without_adb_install(
        self,
    ) -> None:
        log = io.StringIO()
        paths = install_uiautomator2_support(
            self.serial, log=log, driver_root=self.root
        )
        self.assertEqual(paths, (self.settings, self.server, self.test))
        pushes = [command for command in self.commands if command[3] == "push"]
        self.assertEqual([command[4] for command in pushes], list(map(str, paths)))
        installs = [
            command
            for command in self.commands
            if command[3:6] == ["shell", "su", "-c"]
        ]
        self.assertEqual(len(installs), 3)
        removals = [
            command
            for command in self.commands
            if command[3:6] == ["shell", "rm", "-f"]
        ]
        self.assertEqual(
            [command[-1] for command in removals], [command[-1] for command in pushes]
        )
        self.assertEqual(len(self.commands), 15)
        self.assertIn("settings_apk-debug.apk", log.getvalue())

    def test_root_install_failure_cleans_own_upload_without_nonroot_fallback(
        self,
    ) -> None:
        self.install_failure = True
        with self.assertRaisesRegex(android_sdk.AndroidSdkError, "synthetic denial"):
            install_uiautomator2_support(
                self.serial, log=io.StringIO(), driver_root=self.root
            )
        self.assertEqual(len(self.commands), 5)
        self.assertEqual(self.commands[-1][3:6], ["shell", "rm", "-f"])
        self.assertEqual(self.commands[-1][-1], self.commands[2][-1])

    def test_missing_server_or_multiple_versions_never_begin_partial_install(
        self,
    ) -> None:
        self.server.unlink()
        with self.assertRaisesRegex(AppiumError, "exactly one"):
            install_uiautomator2_support(
                self.serial, log=io.StringIO(), driver_root=self.root
            )
        self.server.write_bytes(b"synthetic APK")
        self.server.with_name("appium-uiautomator2-server-v10.7.5.apk").write_bytes(
            b"old version"
        )
        with self.assertRaisesRegex(AppiumError, "exactly one"):
            install_uiautomator2_support(
                self.serial, log=io.StringIO(), driver_root=self.root
            )
        self.sdk.assert_not_called()

    def test_unusable_helper_files_fail_before_any_device_command(self) -> None:
        target = self.root / "other.apk"
        target.write_bytes(b"synthetic alternate APK")
        for helper in (self.settings, self.server, self.test):
            original = helper.read_bytes()
            for mode in ("missing", "empty", "symlink", "hardlink", "directory"):
                with self.subTest(helper=helper.name, mode=mode):
                    helper.unlink()
                    if mode == "empty":
                        helper.touch()
                    elif mode == "symlink":
                        helper.symlink_to(target)
                    elif mode == "hardlink":
                        os.link(target, helper)
                    elif mode == "directory":
                        helper.mkdir()
                    try:
                        with self.assertRaises(AppiumError):
                            install_uiautomator2_support(
                                self.serial, log=io.StringIO(), driver_root=self.root
                            )
                        self.sdk.assert_not_called()
                    finally:
                        if mode == "directory":
                            helper.rmdir()
                        else:
                            helper.unlink(missing_ok=True)
                        helper.write_bytes(original)

    def test_environment_root_resolves_only_the_complete_triplet_without_device_io(
        self,
    ) -> None:
        with patch.dict(os.environ, {"APPIUM_HOME": str(self.root)}):
            self.assertEqual(
                uiautomator2_support_apks(), (self.settings, self.server, self.test)
            )
        self.sdk.assert_not_called()


if __name__ == "__main__":
    unittest.main()
