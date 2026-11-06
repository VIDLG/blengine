"""Run a read-only Appium/XCUITest Settings spike on one physical iPhone.

This is deliberately a direct spike, not a second system-loss orchestration
framework. It starts one Appium server, creates one WDA session, opens Settings,
finds the Bluetooth row through accessibility identifiers, scrolls semantically,
and reads the Bluetooth switch without changing it.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib import error, request
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PORT = 4723
DEFAULT_WDA_PORT = 8101
DEFAULT_BUNDLE_ID = "dev.blehub.appium.wda"
SETTINGS_BACK_ID = "BackButton"
BLUETOOTH_ROW_ID = "com.apple.settings.bluetooth"
BLUETOOTH_SWITCH_ID = "BLUETOOTH"


class AppiumDiagnosticError(RuntimeError):
    """The read-only Appium Settings diagnostic could not prove its bounded contract."""


def _json_request(
    base_url: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 30,
) -> tuple[int, dict[str, Any]]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"} if body is not None else {}
    try:
        with request.urlopen(
            request.Request(base_url + path, data=body, headers=headers, method=method),
            timeout=timeout,
        ) as response:
            raw = response.read()
            value = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(value, dict):
                raise AppiumDiagnosticError(
                    f"Appium returned non-object JSON for {path}"
                )
            return response.status, value
    except error.HTTPError as failure:
        raw = failure.read().decode("utf-8", errors="replace")
        raise AppiumDiagnosticError(
            f"Appium {method} {path} returned HTTP {failure.code}: {raw[-1000:]}"
        ) from failure
    except (error.URLError, TimeoutError, OSError, json.JSONDecodeError) as failure:
        raise AppiumDiagnosticError(
            f"Appium {method} {path} failed: {failure}"
        ) from failure


def _value(document: dict[str, Any], *keys: str) -> Any:
    current: Any = document
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _session_id(document: dict[str, Any]) -> str:
    value = document.get("sessionId")
    if not isinstance(value, str) or not value:
        value = _value(document, "value", "sessionId")
    if not isinstance(value, str) or not value:
        raise AppiumDiagnosticError(f"Appium did not return a session id: {document}")
    return value


def _element_id(document: dict[str, Any]) -> str:
    value = _value(document, "value", "element-6066-11e4-a52e-4f735466cecf")
    if not isinstance(value, str) or not value:
        value = _value(document, "value", "ELEMENT")
    if not isinstance(value, str) or not value:
        raise AppiumDiagnosticError(f"Appium did not return an element id: {document}")
    return value


def _wait_for_server(base_url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            status, document = _json_request(base_url, "GET", "/status", timeout=5)
            if status == 200 and _value(document, "value", "ready") is not False:
                return
        except AppiumDiagnosticError as failure:
            last_error = failure
        time.sleep(0.25)
    detail = f": {last_error}" if last_error else ""
    raise AppiumDiagnosticError(f"Appium server did not become ready{detail}")


def _find(base_url: str, session_id: str, value: str) -> str:
    _, document = _json_request(
        base_url,
        "POST",
        f"/session/{session_id}/element",
        {"using": "accessibility id", "value": value},
    )
    return _element_id(document)


def _try_find(base_url: str, session_id: str, value: str) -> str | None:
    try:
        return _find(base_url, session_id, value)
    except AppiumDiagnosticError as failure:
        if (
            "no such element" in str(failure).lower()
            or "http 404" in str(failure).lower()
        ):
            return None
        raise


def _wait_find(base_url: str, session_id: str, value: str, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        element_id = _try_find(base_url, session_id, value)
        if element_id is not None:
            return element_id
        time.sleep(0.25)
    raise AppiumDiagnosticError(
        f"element {value!r} did not appear within {timeout:.1f}s"
    )


def _is_displayed(base_url: str, session_id: str, element_id: str) -> bool:
    _, document = _json_request(
        base_url, "GET", f"/session/{session_id}/element/{element_id}/displayed"
    )
    value = document.get("value")
    if type(value) is not bool:
        raise AppiumDiagnosticError(
            f"Appium returned invalid displayed state: {document}"
        )
    return value


def _attribute(base_url: str, session_id: str, element_id: str, name: str) -> Any:
    _, document = _json_request(
        base_url,
        "GET",
        f"/session/{session_id}/element/{element_id}/attribute/{name}",
    )
    return document.get("value")


def _source_attribute(
    source: str, *, element_type: str, name: str, attribute: str
) -> str | None:
    try:
        root = ElementTree.fromstring(source)
    except ElementTree.ParseError as failure:
        raise AppiumDiagnosticError(
            f"WDA returned invalid XML source: {failure}"
        ) from failure
    for element in root.iter():
        if element.tag == element_type and element.attrib.get("name") == name:
            return element.attrib.get(attribute)
    return None


def _scroll_up(base_url: str, session_id: str) -> None:
    _json_request(
        base_url,
        "POST",
        f"/session/{session_id}/execute/sync",
        {"script": "mobile: scroll", "args": [{"direction": "up"}]},
    )


def _click(base_url: str, session_id: str, element_id: str) -> None:
    _json_request(
        base_url, "POST", f"/session/{session_id}/element/{element_id}/click", {}
    )


def _find_child_switch(base_url: str, session_id: str, parent_id: str) -> str:
    _, document = _json_request(
        base_url,
        "POST",
        f"/session/{session_id}/element/{parent_id}/element",
        {"using": "xpath", "value": ".//XCUIElementTypeSwitch"},
    )
    return _element_id(document)


def _rect(base_url: str, session_id: str, element_id: str) -> dict[str, float]:
    _, document = _json_request(
        base_url,
        "GET",
        f"/session/{session_id}/element/{element_id}/rect",
    )
    value = document.get("value")
    if not isinstance(value, dict):
        raise AppiumDiagnosticError(f"element rect is not an object: {document}")
    try:
        rect = {key: float(value[key]) for key in ("x", "y", "width", "height")}
    except (KeyError, TypeError, ValueError) as failure:
        raise AppiumDiagnosticError(f"element rect is invalid: {document}") from failure
    return rect


def _click_switch(
    base_url: str, session_id: str, parent_id: str, *, tap_mode: str
) -> dict[str, Any]:
    child_id = _find_child_switch(base_url, session_id, parent_id)
    if tap_mode == "coordinate":
        rect = _rect(base_url, session_id, child_id)
        x = rect["x"] + rect["width"] / 2
        y = rect["y"] + rect["height"] / 2
        _json_request(
            base_url,
            "POST",
            f"/session/{session_id}/execute/sync",
            {"script": "mobile: tap", "args": [{"x": x, "y": y}]},
        )
        return {"mode": tap_mode, "element_id": child_id, "rect": rect}
    _json_request(
        base_url,
        "POST",
        f"/session/{session_id}/actions",
        {
            "actions": [
                {
                    "type": "pointer",
                    "id": "blehub-finger",
                    "parameters": {"pointerType": "touch"},
                    "actions": [
                        {
                            "type": "pointerMove",
                            "origin": {"element-6066-11e4-a52e-4f735466cecf": child_id},
                            "duration": 0,
                        },
                        {"type": "pointerDown", "button": 0},
                        {"type": "pointerUp", "button": 0},
                    ],
                }
            ]
        },
    )
    return {"mode": tap_mode, "element_id": child_id}


def _ensure_switch(base_url: str, session_id: str, page_timeout: float) -> str:
    switch_id = _try_find(base_url, session_id, BLUETOOTH_SWITCH_ID)
    if switch_id is not None:
        return switch_id
    row_id = _wait_find(base_url, session_id, BLUETOOTH_ROW_ID, page_timeout)
    try:
        _click(base_url, session_id, row_id)
    except AppiumDiagnosticError:
        if _try_find(base_url, session_id, BLUETOOTH_SWITCH_ID) is None:
            row_id = _wait_find(base_url, session_id, BLUETOOTH_ROW_ID, page_timeout)
            _click(base_url, session_id, row_id)
    return _wait_find(base_url, session_id, BLUETOOTH_SWITCH_ID, page_timeout)


def _read_switch_xml_value(
    base_url: str, session_id: str, page_timeout: float
) -> str | None:
    if _try_find(base_url, session_id, BLUETOOTH_SWITCH_ID) is None:
        _ensure_switch(base_url, session_id, page_timeout)
    _, current = _json_request(
        base_url, "GET", f"/session/{session_id}/source", timeout=30
    )
    current_xml = current.get("value")
    if not isinstance(current_xml, str):
        raise AppiumDiagnosticError(
            "WDA returned no XML source while reading Bluetooth"
        )
    return _source_attribute(
        current_xml,
        element_type="XCUIElementTypeSwitch",
        name=BLUETOOTH_SWITCH_ID,
        attribute="value",
    )


def _create_session(
    base_url: str,
    *,
    device: str,
    team: str,
    wda_bundle_id: str,
    wda_port: int,
    derived_data: Path,
    bundle_id: str = "com.apple.Preferences",
    auto_launch: bool = True,
) -> str:
    capabilities = {
        "platformName": "iOS",
        "appium:automationName": "XCUITest",
        "appium:udid": device,
        "appium:deviceName": "BleHub iPhone Appium Spike",
        "appium:bundleId": bundle_id,
        "appium:autoLaunch": auto_launch,
        "appium:noReset": True,
        "appium:newCommandTimeout": 120,
        "appium:xcodeOrgId": team,
        "appium:xcodeSigningId": "Apple Development",
        "appium:updatedWDABundleId": wda_bundle_id,
        "appium:usePrebuiltWDA": False,
        "appium:showXcodeLog": True,
        "appium:wdaLocalPort": wda_port,
        "appium:derivedDataPath": str(derived_data),
        "appium:shouldUseSingletonTestManager": False,
    }
    _, document = _json_request(
        base_url,
        "POST",
        "/session",
        {"capabilities": {"alwaysMatch": capabilities, "firstMatch": [{}]}},
        timeout=180,
    )
    return _session_id(document)


def run(args: argparse.Namespace) -> Path:
    run_id = str(uuid.uuid4())
    attempt = ROOT / ".work" / "apple-appium-spike" / run_id
    attempt.mkdir(parents=True, exist_ok=False)
    appium_log = attempt / "appium.log"
    result_path = attempt / "result.json"
    derived_data = attempt / "wda-derived-data"
    server_url = f"http://127.0.0.1:{args.port}"
    process: subprocess.Popen[str] | None = None
    session_id: str | None = None
    radio_off_by_spike = False
    result: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "scope": "ios-appium-settings-read-only-spike",
        "device": args.device,
        "team": args.team,
        "appium_port": args.port,
        "wda_local_port": args.wda_port,
        "wda_bundle_id": args.wda_bundle_id,
        "mutation": "not-requested",
        "status": "failed",
    }
    try:
        log = appium_log.open("w", encoding="utf-8")
        process = subprocess.Popen(
            [
                args.node,
                args.appium_entry,
                "--address",
                "127.0.0.1",
                "--port",
                str(args.port),
                "--log-level",
                "info",
            ],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )
        log.close()
        _wait_for_server(server_url, args.server_timeout)
        session_id = _create_session(
            server_url,
            device=args.device,
            team=args.team,
            wda_bundle_id=args.wda_bundle_id,
            wda_port=args.wda_port,
            derived_data=derived_data,
        )
        result["session_id"] = session_id
        back_count = 0
        while back_count < args.max_back_navigation:
            back_id = _try_find(server_url, session_id, SETTINGS_BACK_ID)
            if back_id is None or not _is_displayed(server_url, session_id, back_id):
                break
            _click(server_url, session_id, back_id)
            back_count += 1
            time.sleep(0.5)
        result["settings_back_navigation"] = back_count
        source_status, source = _json_request(
            server_url, "GET", f"/session/{session_id}/source", timeout=30
        )
        result["source_status"] = source_status
        result["source_contains_bluetooth_identifier"] = BLUETOOTH_ROW_ID in json.dumps(
            source, ensure_ascii=False
        )
        source_value = source.get("value")
        if isinstance(source_value, str):
            source_path = attempt / "settings-source.xml"
            source_path.write_text(source_value, encoding="utf-8")
            result["settings_source_path"] = str(source_path)
        switch_id = _try_find(server_url, session_id, BLUETOOTH_SWITCH_ID)
        scrolls = 0
        route = "existing-bluetooth-page"
        if switch_id is None:
            row_id = _try_find(server_url, session_id, BLUETOOTH_ROW_ID)
            if row_id is None:
                raise AppiumDiagnosticError(
                    "Settings exposed neither the Bluetooth page switch nor the root Bluetooth row"
                )
            while not _is_displayed(server_url, session_id, row_id):
                if scrolls >= args.max_scrolls:
                    raise AppiumDiagnosticError(
                        f"Bluetooth row remained undisplayed after {scrolls} semantic scrolls"
                    )
                _scroll_up(server_url, session_id)
                scrolls += 1
                time.sleep(0.5)
                row_id = _find(server_url, session_id, BLUETOOTH_ROW_ID)
            try:
                _click(server_url, session_id, row_id)
            except AppiumDiagnosticError:
                if _try_find(server_url, session_id, BLUETOOTH_SWITCH_ID) is None:
                    row_id = _wait_find(
                        server_url, session_id, BLUETOOTH_ROW_ID, args.page_timeout
                    )
                    _click(server_url, session_id, row_id)
            route = "root-row"
            switch_id = _wait_find(
                server_url, session_id, BLUETOOTH_SWITCH_ID, args.page_timeout
            )
        displayed = _is_displayed(server_url, session_id, switch_id)
        attribute_value = _attribute(server_url, session_id, switch_id, "value")
        _, current_source = _json_request(
            server_url, "GET", f"/session/{session_id}/source", timeout=30
        )
        current_source_value = current_source.get("value")
        xml_value = (
            _source_attribute(
                current_source_value,
                element_type="XCUIElementTypeSwitch",
                name=BLUETOOTH_SWITCH_ID,
                attribute="value",
            )
            if isinstance(current_source_value, str)
            else None
        )
        value = attribute_value if attribute_value in ("0", "1") else xml_value
        if not displayed or value not in ("0", "1"):
            raise AppiumDiagnosticError(
                "Bluetooth switch is not a readable binary control: "
                f"displayed={displayed} attribute_value={attribute_value!r} xml_value={xml_value!r}"
            )
        result.update(
            {
                "status": "passed",
                "settings_row_identifier": BLUETOOTH_ROW_ID,
                "settings_switch_identifier": BLUETOOTH_SWITCH_ID,
                "settings_route": route,
                "semantic_scrolls": scrolls,
                "bluetooth_value": value,
                "webdriver_attribute_value": attribute_value,
                "wda_xml_value": xml_value,
                "settings_source": source.get("value"),
                "mutation": "not-requested",
            }
        )
        if args.mutate_once:
            if value != "1":
                raise AppiumDiagnosticError(
                    f"refusing mutation because Bluetooth is not initially ON: {value}"
                )
            result["mutation"] = "one-off-one-on"
            result["mutation_actions"] = []
            switch_id = _ensure_switch(server_url, session_id, args.page_timeout)
            result["mutation_actions"].append(
                _click_switch(
                    server_url,
                    session_id,
                    switch_id,
                    tap_mode=args.tap_mode,
                )
            )
            result["mutation_actions"].append("off-mobile-tap")
            off_deadline = time.monotonic() + args.mutation_timeout
            while time.monotonic() < off_deadline:
                current_value = _read_switch_xml_value(
                    server_url, session_id, args.page_timeout
                )
                if current_value == "0":
                    break
                time.sleep(0.5)
            else:
                raise AppiumDiagnosticError("Bluetooth did not read back OFF")
            radio_off_by_spike = True
            result["mutation_actions"].append("off-readback")
            switch_id = _ensure_switch(server_url, session_id, args.page_timeout)
            result["mutation_actions"].append(
                _click_switch(
                    server_url,
                    session_id,
                    switch_id,
                    tap_mode=args.tap_mode,
                )
            )
            result["mutation_actions"].append("on-mobile-tap")
            on_deadline = time.monotonic() + args.mutation_timeout
            while time.monotonic() < on_deadline:
                current_value = _read_switch_xml_value(
                    server_url, session_id, args.page_timeout
                )
                if current_value == "1":
                    radio_off_by_spike = False
                    result["mutation_actions"].append("on-readback")
                    break
                time.sleep(0.5)
            else:
                raise AppiumDiagnosticError("Bluetooth did not read back ON")
        return result_path
    except (
        AppiumDiagnosticError,
        OSError,
        subprocess.SubprocessError,
        ValueError,
    ) as failure:
        result["status"] = "failed"
        result["error"] = str(failure)
        raise
    finally:
        result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        if session_id is not None and radio_off_by_spike:
            try:
                restore_id = _ensure_switch(server_url, session_id, args.page_timeout)
                result["cleanup_restore_attempted"] = True
                result["cleanup_restore_tap"] = _click_switch(
                    server_url,
                    session_id,
                    restore_id,
                    tap_mode=args.tap_mode,
                )
                restore_deadline = time.monotonic() + args.mutation_timeout
                while time.monotonic() < restore_deadline:
                    current_value = _read_switch_xml_value(
                        server_url, session_id, args.page_timeout
                    )
                    if current_value == "1":
                        radio_off_by_spike = False
                        result["cleanup_restore_status"] = "on-readback"
                        break
                    time.sleep(0.5)
                else:
                    result["cleanup_restore_status"] = "on-readback-timeout"
            except AppiumDiagnosticError as failure:
                result["cleanup_restore_status"] = f"failed: {failure}"
        if session_id is not None:
            try:
                _json_request(
                    server_url,
                    "DELETE",
                    f"/session/{session_id}",
                    timeout=20,
                )
            except AppiumDiagnosticError as failure:
                result["session_cleanup_error"] = str(failure)
                result_path.write_text(
                    json.dumps(result, indent=2, ensure_ascii=False) + "\n"
                )
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=15)
            except (OSError, subprocess.SubprocessError, TimeoutError) as failure:
                result["server_cleanup_error"] = str(failure)
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
            result_path.write_text(
                json.dumps(result, indent=2, ensure_ascii=False) + "\n"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", default="node")
    parser.add_argument("--appium-entry", type=Path, required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--team", required=True)
    parser.add_argument("--wda-bundle-id", default=DEFAULT_BUNDLE_ID)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--wda-port", type=int, default=DEFAULT_WDA_PORT)
    parser.add_argument("--server-timeout", type=float, default=30)
    parser.add_argument("--max-scrolls", type=int, default=6)
    parser.add_argument("--max-back-navigation", type=int, default=8)
    parser.add_argument("--mutate-once", action="store_true")
    parser.add_argument("--mutation-timeout", type=float, default=90)
    parser.add_argument("--page-timeout", type=float, default=15)
    parser.add_argument(
        "--tap-mode", choices=("pointer", "coordinate"), default="pointer"
    )
    args = parser.parse_args()
    try:
        result = run(args)
    except (
        AppiumDiagnosticError,
        OSError,
        subprocess.SubprocessError,
        ValueError,
    ) as failure:
        print(f"ios_appium_spike=failed error={failure}", file=sys.stderr)
        return 2
    print(f"ios_appium_spike=passed result={result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
