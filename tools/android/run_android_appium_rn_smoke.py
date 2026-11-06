"""Run a real-device Appium/UiAutomator2 smoke for the RN workbench pages.

This verifies only the app's black-box UI. It does not open Settings, mutate
Bluetooth, start a BLE probe, or start RF. APK installation and device commands
remain behind ``android_sdk``; Appium owns the page/elements interaction.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from appium import webdriver
from selenium.common.exceptions import WebDriverException

from tools.android.android_appium import (
    AppiumError,
    AppiumServer,
    create_android_driver,
    install_uiautomator2_support,
    wait_displayed,
    wait_find,
    wait_for_package,
)
from tools.react_native import android_sdk
from tools.react_native.host_contract import ANDROID_ACTIVITY, ANDROID_PACKAGE

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / ".work" / "android-appium-rn-smoke"


def _install(device: str, apk: Path, output: Path) -> bool:
    with output.with_suffix(".log").open("w", encoding="utf-8") as log:
        use_root = android_sdk.install_apk(device, apk, log=log)
    output.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "device": device,
                "apk": str(apk),
                "status": "passed",
                "root_install": use_root,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return use_root


def run(args: argparse.Namespace) -> Path:
    run_id = str(uuid.uuid4())
    attempt = WORK / run_id
    attempt.mkdir(parents=True, exist_ok=False)
    result_path = attempt / "result.json"
    server: AppiumServer | None = None
    driver: webdriver.Remote | None = None
    result: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "scope": "android-appium-rn-workbench-smoke",
        "device": args.device,
        "package": args.package,
        "activity": args.activity,
        "apk": str(args.apk),
        "ble": "not-started",
        "rf": "not-started",
        "status": "failed",
    }
    try:
        android_sdk.require_device(args.device)
        result["root_install"] = _install(
            args.device, args.apk, attempt / "install.json"
        )
        with (attempt / "appium-support.log").open("w", encoding="utf-8") as log:
            support = install_uiautomator2_support(
                args.device,
                log=log,
                driver_root=args.uiautomator2_driver_root,
            )
        result["uiautomator2_support"] = [str(path) for path in support]
        server = AppiumServer(
            node=args.node,
            entry=args.appium_entry,
            port=args.port,
            log=attempt / "appium.log",
            timeout=args.server_timeout,
        )
        server.start()
        driver = create_android_driver(
            server.base_url,
            device=args.device,
            package=args.package,
            activity=args.activity,
        )
        result["session_id"] = driver.session_id
        wait_for_package(driver, args.package, args.page_timeout)
        source = driver.page_source
        (attempt / "rn-workbench-source.xml").write_text(source, encoding="utf-8")
        if "Unable to load script." in source or "rn_redbox_stack" in source:
            raise AppiumError(
                "RN redbox is visible; use a release-shaped APK with a packaged "
                "JavaScript bundle or provide the maintained Metro setup"
            )

        tabs: dict[str, Any] = {}
        elements: dict[str, Any] = {}
        for name in ("home", "current", "evidence"):
            element = wait_find(driver, f"rf-workbench-{name}", args.page_timeout)
            elements[name] = element
            tabs[name] = {
                "element_id": element.id,
                "displayed": element.is_displayed(),
            }
        tabs["current"]["enabled"] = elements["current"].is_enabled()
        if tabs["current"]["enabled"]:
            elements["current"].click()
            wait_displayed(driver, "rf-workbench-active", args.page_timeout)
            tabs["current"]["page_visible"] = True
        else:
            tabs["current"]["page_visible"] = False

        evidence = wait_find(driver, "rf-workbench-evidence", args.page_timeout)
        evidence.click()
        wait_displayed(driver, "rf-workbench-failure", args.page_timeout)
        tabs["evidence"]["page_visible"] = True

        home = wait_find(driver, "rf-workbench-home", args.page_timeout)
        home.click()
        # Android may omit a disabled Pressable from UiAutomator2's tree. The
        # selected Tests tab is the stable proof that navigation returned home.
        wait_displayed(driver, "rf-workbench-home", args.page_timeout)
        tabs["home"]["page_visible"] = True
        result.update({"status": "passed", "tabs": tabs})
        return result_path
    except (
        AppiumError,
        WebDriverException,
        android_sdk.AndroidSdkError,
        OSError,
        ValueError,
    ) as failure:
        result["error"] = str(failure)
        raise
    finally:
        if driver is not None:
            try:
                source = driver.page_source
                (attempt / "rn-workbench-final-source.xml").write_text(
                    source, encoding="utf-8"
                )
            except WebDriverException as failure:
                result["source_dump_error"] = str(failure)
            try:
                driver.quit()
            except WebDriverException as failure:
                result["session_cleanup_error"] = str(failure)
        if server is not None:
            try:
                server.stop()
            except AppiumError as failure:
                result["server_cleanup_error"] = str(failure)
        result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apk", type=Path, required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--appium-entry", type=Path, required=True)
    parser.add_argument("--node", default="node")
    parser.add_argument("--package", default=ANDROID_PACKAGE)
    parser.add_argument("--activity", default=ANDROID_ACTIVITY)
    parser.add_argument("--uiautomator2-driver-root", type=Path)
    parser.add_argument("--port", type=int, default=4723)
    parser.add_argument("--server-timeout", type=float, default=30)
    parser.add_argument("--page-timeout", type=float, default=20)
    args = parser.parse_args()
    try:
        result = run(args)
    except (
        AppiumError,
        WebDriverException,
        android_sdk.AndroidSdkError,
        OSError,
        ValueError,
    ) as failure:
        print(f"android_appium_rn_smoke=failed error={failure}", file=sys.stderr)
        return 2
    print(f"android_appium_rn_smoke=passed result={result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
