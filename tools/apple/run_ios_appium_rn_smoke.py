"""Run a real-device Appium smoke for the RN workbench pages only.

This does not open Settings, mutate Bluetooth, start a BLE probe, or start RF.
It installs the signed RN host through the shared iOS SDK API, lets
Appium/XCUITest launch it, and verifies the three workbench tabs through the
real iOS accessibility tree.
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

from tools.apple.ios_appium import (
    DEFAULT_TUNNEL_REGISTRY_PORT,
    AppiumError,
    AppiumServer,
    create_ios_driver,
    require_remotexpc_tunnel,
    wait_displayed,
    wait_find,
    wait_for_bundle,
)
from tools.react_native import ios_sdk

ROOT = Path(__file__).resolve().parents[2]
BUNDLE_ID = "org.reactjs.native.example.BleHubNitroHost"
WORK = ROOT / ".work" / "apple-appium-rn-smoke"


def _install(device: str, app: Path, output: Path) -> None:
    command = ios_sdk.devicectl_install_app(device, app, output)
    completed = ios_sdk.run_command(command, cwd=ROOT, timeout=120)
    if completed.returncode:
        raise AppiumError(f"RN App install failed: {completed.stdout[-1000:]}")
    ios_sdk.official_result(output, "devicectl.device.install.app", device)


def run(args: argparse.Namespace) -> Path:
    run_id = str(uuid.uuid4())
    attempt = WORK / run_id
    attempt.mkdir(parents=True, exist_ok=False)
    result_path = attempt / "result.json"
    derived_data = attempt / "wda-derived-data"
    server: AppiumServer | None = None
    driver: webdriver.Remote | None = None
    result: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "scope": "ios-appium-rn-workbench-smoke",
        "device": args.device,
        "bundle_id": BUNDLE_ID,
        "app": str(args.app),
        "ble": "not-started",
        "rf": "not-started",
        "status": "failed",
    }
    try:
        result["remotexpc_tunnel_registry"] = require_remotexpc_tunnel(
            args.tunnel_registry_port
        )
        _install(args.device, args.app, attempt / "install.json")
        server = AppiumServer(
            node=args.node,
            entry=args.appium_entry,
            port=args.port,
            log=attempt / "appium.log",
            timeout=args.server_timeout,
        )
        server.start()
        driver = create_ios_driver(
            server.base_url,
            device=args.device,
            team=args.team,
            bundle_id=BUNDLE_ID,
            wda_bundle_id=args.wda_bundle_id,
            wda_port=args.wda_port,
            derived_data=derived_data,
        )
        result["session_id"] = driver.session_id
        source = wait_for_bundle(driver, BUNDLE_ID, args.page_timeout)
        (attempt / "rn-workbench-source.xml").write_text(source, encoding="utf-8")
        tabs = {}
        elements: dict[str, Any] = {}
        for name in ("home", "current", "evidence"):
            element = wait_find(
                driver,
                f"rf-workbench-{name}",
                args.page_timeout,
            )
            elements[name] = element
            tabs[name] = {
                "element_id": element.id,
                "displayed": element.is_displayed(),
            }
        tabs["current"]["enabled"] = elements["current"].is_enabled()
        if tabs["current"]["enabled"]:
            elements["current"].click()
            wait_find(driver, "rf-workbench-active", args.page_timeout)
            wait_displayed(driver, "rf-workbench-active", args.page_timeout)
            tabs["current"]["page_visible"] = True
        else:
            tabs["current"]["page_visible"] = False

        evidence = wait_find(driver, "rf-workbench-evidence", args.page_timeout)
        evidence.click()
        wait_find(driver, "rf-workbench-failure", args.page_timeout)
        wait_displayed(driver, "rf-workbench-failure", args.page_timeout)
        tabs["evidence"]["page_visible"] = True

        home = wait_find(driver, "rf-workbench-home", args.page_timeout)
        home.click()
        wait_find(driver, "rf-workbench-open-interactive", args.page_timeout)
        wait_displayed(driver, "rf-workbench-open-interactive", args.page_timeout)
        tabs["home"]["page_visible"] = True
        result.update({"status": "passed", "tabs": tabs})
        return result_path
    except (
        AppiumError,
        WebDriverException,
        ios_sdk.IosSdkError,
        OSError,
        ValueError,
    ) as failure:
        result["error"] = str(failure)
        raise
    finally:
        result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        if driver is not None:
            try:
                source = driver.page_source
                (attempt / "rn-workbench-failure-source.xml").write_text(
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
        result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--node", default="node")
    parser.add_argument("--appium-entry", type=Path, required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--team", required=True)
    parser.add_argument("--wda-bundle-id", default="dev.blehub.appium.wda")
    parser.add_argument("--port", type=int, default=4723)
    parser.add_argument("--wda-port", type=int, default=8101)
    parser.add_argument(
        "--tunnel-registry-port", type=int, default=DEFAULT_TUNNEL_REGISTRY_PORT
    )
    parser.add_argument("--server-timeout", type=float, default=30)
    parser.add_argument("--page-timeout", type=float, default=20)
    args = parser.parse_args()
    try:
        result = run(args)
    except (
        AppiumError,
        WebDriverException,
        ios_sdk.IosSdkError,
        OSError,
        ValueError,
    ) as failure:
        print(f"ios_appium_rn_smoke=failed error={failure}", file=sys.stderr)
        return 2
    print(f"ios_appium_rn_smoke=passed result={result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
