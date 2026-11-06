"""Maintained Appium/XCUITest infrastructure for physical iOS checks.

Scenario runners own only their artifact, device identity, assertions, and evidence.
This module owns the Appium server process, Python client session setup, bounded
accessibility waits, and the iOS 18+ RemoteXPC tunnel preflight.
"""

from __future__ import annotations

import math
import re
import time
from pathlib import Path
from urllib import error, request
from urllib.parse import urlsplit
from xml.etree import ElementTree

from appium import webdriver
from appium.options.ios import XCUITestOptions
from appium.webdriver.client_config import AppiumClientConfig

from tools.mobile_appium import AppiumError, AppiumServer, wait_displayed, wait_find
from tools.react_native.ios_device import canonical_physical_udid

__all__ = [
    "DEFAULT_TUNNEL_REGISTRY_PORT",
    "AppiumError",
    "AppiumServer",
    "create_ios_attached_driver",
    "create_ios_driver",
    "require_remotexpc_tunnel",
    "source_attribute",
    "source_bundle_id",
    "validate_ios_attach_configuration",
    "wait_displayed",
    "wait_find",
    "wait_for_bundle",
]

DEFAULT_TUNNEL_REGISTRY_PORT = 42314


def require_remotexpc_tunnel(port: int) -> str:
    """Require the host tunnel registry used by real iOS 18+ devices."""
    endpoint = f"http://127.0.0.1:{port}/remotexpc/tunnels"
    try:
        with request.urlopen(endpoint, timeout=3) as response:
            if response.status != 200:
                raise AppiumError(
                    f"RemoteXPC tunnel registry returned HTTP {response.status}"
                )
    except (error.HTTPError, error.URLError, TimeoutError, OSError) as failure:
        raise AppiumError(
            "RemoteXPC tunnel registry is unavailable; for a real iOS 18+ device "
            "run the Appium tunnel-creation command as root before this check: "
            "sudo appium driver run xcuitest tunnel-creation"
        ) from failure
    return endpoint


def create_ios_driver(
    server_url: str,
    *,
    device: str,
    team: str,
    bundle_id: str,
    wda_bundle_id: str,
    wda_port: int,
    derived_data: Path,
    auto_launch: bool = True,
    command_timeout: float | None = None,
) -> webdriver.Remote:
    """Create one real-device XCUITest session through Appium's Python client."""
    options = XCUITestOptions()
    options.platform_name = "iOS"
    options.automation_name = "XCUITest"
    options.device_name = "BleHub iPhone Appium session"
    options.udid = device
    options.bundle_id = bundle_id
    options.use_simulator = False
    options.no_reset = True
    options.new_command_timeout = 120
    options.set_capability("appium:xcodeOrgId", team)
    options.set_capability("appium:xcodeSigningId", "Apple Development")
    options.set_capability("appium:updatedWDABundleId", wda_bundle_id)
    options.set_capability("appium:usePrebuiltWDA", False)
    options.set_capability("appium:showXcodeLog", True)
    options.set_capability("appium:wdaLocalPort", wda_port)
    options.set_capability("appium:derivedDataPath", str(derived_data))
    options.set_capability("appium:shouldUseSingletonTestManager", False)
    options.set_capability("appium:autoLaunch", auto_launch)
    if not auto_launch:
        # WDA may attach, but it must not restart or terminate the host-opened App.
        options.set_capability("appium:forceAppLaunch", False)
        options.set_capability("appium:shouldTerminateApp", False)
    client_config = (
        AppiumClientConfig(remote_server_addr=server_url, timeout=command_timeout)
        if command_timeout is not None
        else None
    )
    return webdriver.Remote(server_url, options=options, client_config=client_config)


def create_ios_attached_driver(
    server_url: str,
    *,
    device: str,
    bundle_id: str,
    wda_url: str,
    command_timeout: float,
) -> webdriver.Remote:
    """Attach using a host-prepared running WDA; never fall back to building it.

    The caller owns the phone, WDA forwarding, candidate/App identity and session
    cleanup. This configures a session only: a URL is not WDA ownership proof.
    Unlike create_ios_driver, this path has no signing/build/deployment inputs.
    It does not activate/restart/reset the host-opened App or terminate it on quit.
    Driver creation is itself a remote operation; guard ownership before calling.
    """
    validate_ios_attach_configuration(
        device=device,
        bundle_id=bundle_id,
        wda_url=wda_url,
        command_timeout=command_timeout,
    )
    options = XCUITestOptions()
    options.platform_name = "iOS"
    options.automation_name = "XCUITest"
    options.device_name = "BleHub iPhone Appium session"
    options.udid = device
    options.bundle_id = bundle_id
    options.use_simulator = False
    options.no_reset = True
    options.full_reset = False
    options.new_command_timeout = 120
    # Connect to the caller's existing WDA, without build/deployment modes.
    options.web_driver_agent_url = wda_url.rstrip("/")
    options.set_capability("appium:autoLaunch", False)
    options.set_capability("appium:forceAppLaunch", False)
    options.set_capability("appium:shouldTerminateApp", False)
    client_config = AppiumClientConfig(
        remote_server_addr=server_url, timeout=command_timeout
    )
    return webdriver.Remote(server_url, options=options, client_config=client_config)


def validate_ios_attach_configuration(
    *, device: str, bundle_id: str, wda_url: str, command_timeout: float
) -> None:
    """Validate local attach inputs before a caller starts any owned resources."""
    canonical = canonical_physical_udid(device)
    if canonical != device:
        raise ValueError("device must be the canonical physical UDID")
    if (
        not isinstance(bundle_id, str)
        or re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", bundle_id) is None
    ):
        raise ValueError("an explicit canonical bundle_id is required")
    if (
        type(command_timeout) not in (int, float)
        or not math.isfinite(command_timeout)
        or not 0 < command_timeout <= 300
    ):
        raise ValueError("command_timeout must be finite and in (0, 300] seconds")
    try:
        endpoint = urlsplit(wda_url)
        valid = (
            isinstance(wda_url, str)
            and wda_url == wda_url.strip()
            and all(32 < ord(char) < 127 for char in wda_url)
            and endpoint.scheme == "http"
            and endpoint.hostname in {"127.0.0.1", "localhost", "::1"}
            and endpoint.port is not None
            and 1 <= endpoint.port <= 65535
            and endpoint.username is None
            and endpoint.password is None
            and endpoint.path in {"", "/"}
            and not endpoint.query
            and not endpoint.fragment
        )
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise ValueError("wda_url must be an explicit loopback HTTP endpoint and port")


def _xml_root(source: str) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(source)
    except ElementTree.ParseError as failure:
        raise AppiumError(f"WDA returned invalid XML source: {failure}") from failure


def source_bundle_id(source: str) -> str | None:
    """Read the application bundle identity from a WDA XML accessibility source."""
    root = _xml_root(source)
    for element in root.iter():
        for key in ("bundleId", "bundleID"):
            value = element.attrib.get(key)
            if value:
                return value
    return None


def source_attribute(
    source: str, *, element_type: str, name: str, attribute: str
) -> str | None:
    """Read one WDA XML attribute when WebDriver's value attribute is unreliable."""
    for element in _xml_root(source).iter():
        if element.tag == element_type and element.attrib.get("name") == name:
            return element.attrib.get(attribute)
    return None


def wait_for_bundle(driver: webdriver.Remote, bundle_id: str, timeout: float) -> str:
    """Require WDA to attach to the requested app instead of SpringBoard."""
    deadline = time.monotonic() + timeout
    observed: str | None = None
    while time.monotonic() < deadline:
        source = driver.page_source
        observed = source_bundle_id(source)
        if observed == bundle_id:
            return source
        time.sleep(0.5)
    raise AppiumError(
        f"WDA did not attach to bundle {bundle_id}; observed {observed!r}"
    )
