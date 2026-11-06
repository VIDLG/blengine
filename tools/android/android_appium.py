"""Maintained Appium/UiAutomator2 infrastructure for physical Android checks."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import TextIO

from appium import webdriver
from appium.options.android import UiAutomator2Options
from appium.webdriver.client_config import AppiumClientConfig
from appium.webdriver.common.appiumby import AppiumBy
from urllib3.util import Retry

from tools.mobile_appium import (
    AppiumError,
    AppiumServer,
    wait_displayed_by,
    wait_find_by,
    wait_for_package,
)


def uiautomator2_support_apks(driver_root: Path | None = None) -> tuple[Path, ...]:
    """Require one complete local helper triplet before any explicit provisioning.

    ``driver_root`` is the Appium home, matching the historical CLI option. Do not
    choose a newest server APK or install every version found in a stale tree.
    This local check is not installed-image or running-session attestation.
    """
    appium_home = Path(
        driver_root or os.environ.get("APPIUM_HOME", str(Path.home() / ".appium"))
    ).expanduser()
    driver = appium_home / "node_modules" / "appium-uiautomator2-driver"
    settings = driver / "node_modules/io.appium.settings/apks/settings_apk-debug.apk"
    server_apks = sorted(
        path
        for path in (driver / "node_modules/appium-uiautomator2-server/apks").glob(
            "appium-uiautomator2-server-*.apk"
        )
        if not path.name.endswith("-debug-androidTest.apk")
    )
    test_apk = (
        driver
        / "node_modules/appium-uiautomator2-server/apks/appium-uiautomator2-server-debug-androidTest.apk"
    )
    if len(server_apks) != 1:
        raise AppiumError(
            "UiAutomator2 requires exactly one server APK; "
            f"found {len(server_apks)} in {test_apk.parent}"
        )
    paths = (settings, server_apks[0], test_apk)
    for path in paths:
        try:
            info = path.lstat()
        except OSError as error:
            raise AppiumError(
                f"UiAutomator2 helper APK is unavailable: {path}"
            ) from error
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size == 0:
            raise AppiumError(
                f"UiAutomator2 helper APK must be a nonempty unlinked regular file: {path}"
            )
    return paths


def install_uiautomator2_support(
    device: str,
    *,
    log: TextIO,
    driver_root: Path | None = None,
) -> tuple[Path, ...]:
    """Explicitly provision the helpers via the existing rooted-phone installer.

    This may close installer UIs and grant helper permissions. It is a separate
    setup action, never an implicit repair in an ordinary formal scan run.
    """
    from tools.react_native import android_sdk

    paths = uiautomator2_support_apks(driver_root)
    for apk in paths:
        log.write(f"install Appium helper {apk.name}: {apk}\n")
        android_sdk.install_apk(
            device,
            apk,
            log=log,
            use_root=True,
            grant=True,
        )
    return paths


def _resource_id_locator(identifier: str) -> str:
    if '"' in identifier or "\\" in identifier:
        raise ValueError("Android resource ID contains an unsafe selector character")
    return f'new UiSelector().resourceId("{identifier}")'


def wait_find(driver: webdriver.Remote, identifier: str, timeout: float):
    """Find an Android RN resource ID through UiAutomator2."""
    return wait_find_by(
        driver,
        AppiumBy.ANDROID_UIAUTOMATOR,
        _resource_id_locator(identifier),
        timeout,
    )


def wait_displayed(driver: webdriver.Remote, identifier: str, timeout: float) -> None:
    """Wait for a displayed Android RN resource ID."""
    wait_displayed_by(
        driver,
        AppiumBy.ANDROID_UIAUTOMATOR,
        _resource_id_locator(identifier),
        timeout,
    )


def require_prepared_driver_contract(
    owner=None,
    *,
    phone=None,
    run_id=None,
    app_id=None,
    deadline=None,
) -> None:
    """Admit only a live, same-run concrete owner; a report never grants authority.

    The supported owner checks an explicitly prepared stock-Appium profile and
    later verifies the expected transient settings changes were undone. Root and
    installer confirmation are not blockers. Bare lifecycle callers and passive
    inventory JSON remain refused; no bypass installs or repairs anything.
    Legacy explicitly provisioned runners retain their separate contract.
    """
    from tools.react_native.bleng_android_session import AndroidAutomationOwner

    if type(owner) is AndroidAutomationOwner:
        owner.require_prepared(
            phone=phone, run_id=run_id, app_id=app_id, deadline=deadline
        )
        return
    raise AppiumError(
        "Android prepared UiAutomator2 driver contract is not admitted: "
        "a same-run concrete AndroidAutomationOwner must prepare and bind the "
        "session and verify settings finalization. Root availability, helper "
        "files or passive JSON do not authorize old-session cleanup."
    )


def prepared_driver_capabilities(system_port: int) -> dict:
    """One protected profile, shared by session construction and owner rechecks."""
    if type(system_port) is not int or not 1 <= system_port <= 65535:
        raise ValueError("explicit canonical Android forwarding port required")
    return {
        "systemPort": system_port,
        "autoLaunch": False,
        "forceAppLaunch": False,
        "shouldTerminateApp": False,
        "noReset": True,
        "skipDeviceInitialization": True,
        "skipServerInstallation": True,
        "fullReset": False,
        "autoGrantPermissions": False,
        "skipUnlock": True,
        "suppressKillServer": True,
        "skipLogcatCapture": True,
        "disableWindowAnimation": False,
        "disableSuppressAccessibilityService": True,
        "dontStopAppOnReset": True,
        "mockLocationApp": "io.appium.settings",
    }


def create_android_driver(
    server_url: str,
    *,
    device: str,
    package: str,
    activity: str,
    auto_launch: bool = True,
    command_timeout: float | None = None,
    system_port: int | None = None,
) -> webdriver.Remote:
    """Create a session; an explicit system port selects the prepared owner profile."""
    if system_port is not None and (
        type(system_port) is not int or not 1 <= system_port <= 65535 or auto_launch
    ):
        raise ValueError(
            "prepared Android automation requires an explicit port and attach-only mode"
        )
    options = UiAutomator2Options()
    options.platform_name = "Android"
    options.automation_name = "UiAutomator2"
    options.device_name = f"BleHub Android {device}"
    options.udid = device
    options.app_package = package
    options.app_activity = activity
    options.app_wait_package = package
    options.app_wait_activity = activity
    options.no_reset = True
    options.new_command_timeout = 120
    options.skip_device_initialization = True
    options.skip_server_installation = True
    options.set_capability("appium:autoLaunch", auto_launch)
    options.set_capability("appium:skipLogcatCapture", False)
    if system_port is not None:
        for key, value in prepared_driver_capabilities(system_port).items():
            options.set_capability("appium:" + key, value)
    if not auto_launch:
        # Attaching must not replace the host-opened App or hide its result page on quit.
        options.set_capability("appium:forceAppLaunch", False)
        options.set_capability("appium:shouldTerminateApp", False)
    client_config = (
        AppiumClientConfig(
            remote_server_addr=server_url,
            timeout=command_timeout,
            # Selenium unwraps this nested key; SDK retries would multiply the
            # command deadline independently of the helper's explicit polling.
            init_args_for_pool_manager={
                "init_args_for_pool_manager": {"retries": Retry(total=0)}
            },
        )
        if command_timeout is not None
        else None
    )
    return webdriver.Remote(server_url, options=options, client_config=client_config)


__all__ = [
    "AppiumError",
    "AppiumServer",
    "create_android_driver",
    "install_uiautomator2_support",
    "require_prepared_driver_contract",
    "uiautomator2_support_apks",
    "wait_displayed",
    "wait_find",
    "wait_for_package",
]
