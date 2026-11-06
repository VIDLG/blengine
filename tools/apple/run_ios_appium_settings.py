"""Run the maintained read-only Appium Settings inspection on one iPhone.

This tool uses Appium's Python client for all Settings navigation and element
interaction. It never changes Bluetooth, starts BLE/RF, or participates in a
system-loss authorization window.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from appium import webdriver
from appium.webdriver.common.appiumby import AppiumBy
from selenium.common.exceptions import NoSuchElementException, WebDriverException

from tools.apple.ios_appium import (
    DEFAULT_TUNNEL_REGISTRY_PORT,
    AppiumError,
    AppiumServer,
    create_ios_driver,
    require_remotexpc_tunnel,
    source_attribute,
    wait_displayed,
    wait_find,
    wait_for_bundle,
)

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / ".work" / "apple-appium-settings"
SETTINGS_BUNDLE_ID = "com.apple.Preferences"
SETTINGS_BACK_ID = "BackButton"
BLUETOOTH_ROW_ID = "com.apple.settings.bluetooth"
BLUETOOTH_SWITCH_ID = "BLUETOOTH"


def _read_switch(driver: webdriver.Remote) -> dict[str, Any]:
    source = driver.page_source
    attribute_value = driver.find_element(
        AppiumBy.ACCESSIBILITY_ID, BLUETOOTH_SWITCH_ID
    ).get_attribute("value")
    xml_value = source_attribute(
        source,
        element_type="XCUIElementTypeSwitch",
        name=BLUETOOTH_SWITCH_ID,
        attribute="value",
    )
    value = attribute_value if attribute_value in ("0", "1") else xml_value
    if value not in ("0", "1"):
        raise AppiumError(
            "Bluetooth switch is not a readable binary control: "
            f"attribute_value={attribute_value!r} xml_value={xml_value!r}"
        )
    return {
        "bluetooth_value": value,
        "webdriver_attribute_value": attribute_value,
        "wda_xml_value": xml_value,
    }


def run(args: argparse.Namespace) -> Path:
    run_id = str(uuid.uuid4())
    attempt = WORK / run_id
    attempt.mkdir(parents=True, exist_ok=False)
    result_path = attempt / "result.json"
    driver: webdriver.Remote | None = None
    server: AppiumServer | None = None
    result: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "scope": "ios-appium-settings-read-only",
        "device": args.device,
        "mutation": "not-requested",
        "ble": "not-started",
        "rf": "not-started",
        "status": "failed",
    }
    try:
        result["remotexpc_tunnel_registry"] = require_remotexpc_tunnel(
            args.tunnel_registry_port
        )
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
            bundle_id=SETTINGS_BUNDLE_ID,
            wda_bundle_id=args.wda_bundle_id,
            wda_port=args.wda_port,
            derived_data=attempt / "wda-derived-data",
        )
        result["session_id"] = driver.session_id
        source = wait_for_bundle(driver, SETTINGS_BUNDLE_ID, args.page_timeout)
        (attempt / "settings-source.xml").write_text(source, encoding="utf-8")

        back_count = 0
        while back_count < args.max_back_navigation:
            try:
                back = driver.find_element(AppiumBy.ACCESSIBILITY_ID, SETTINGS_BACK_ID)
                if not back.is_displayed():
                    break
                back.click()
                back_count += 1
            except NoSuchElementException:
                break
        result["settings_back_navigation"] = back_count

        scrolls = 0
        route = "existing-bluetooth-page"
        try:
            wait_find(driver, BLUETOOTH_SWITCH_ID, args.page_timeout)
        except AppiumError:
            row = wait_find(driver, BLUETOOTH_ROW_ID, args.page_timeout)
            while not row.is_displayed():
                if scrolls >= args.max_scrolls:
                    raise AppiumError(
                        f"Bluetooth row remained undisplayed after {scrolls} semantic scrolls"
                    )
                driver.execute_script("mobile: scroll", {"direction": "up"})
                scrolls += 1
                row = wait_find(driver, BLUETOOTH_ROW_ID, args.page_timeout)
            row.click()
            route = "root-row"
            wait_find(driver, BLUETOOTH_SWITCH_ID, args.page_timeout)

        wait_displayed(driver, BLUETOOTH_SWITCH_ID, args.page_timeout)
        result.update(
            {
                "status": "passed",
                "settings_row_identifier": BLUETOOTH_ROW_ID,
                "settings_switch_identifier": BLUETOOTH_SWITCH_ID,
                "settings_route": route,
                "semantic_scrolls": scrolls,
                **_read_switch(driver),
            }
        )
        return result_path
    except (AppiumError, WebDriverException, OSError, ValueError) as failure:
        result["error"] = str(failure)
        raise
    finally:
        result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        if driver is not None:
            try:
                source = driver.page_source
                (attempt / "settings-final-source.xml").write_text(
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
    parser.add_argument("--max-scrolls", type=int, default=6)
    parser.add_argument("--max-back-navigation", type=int, default=8)
    args = parser.parse_args()
    try:
        result = run(args)
    except (AppiumError, WebDriverException, OSError, ValueError) as failure:
        print(f"ios_appium_settings=failed error={failure}", file=sys.stderr)
        return 2
    print(f"ios_appium_settings=passed result={result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
