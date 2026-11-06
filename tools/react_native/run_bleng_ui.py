"""Attach to an already-open formal RN App and verify its lifecycle UI.

The CLI exercises only public createBlengSessionOwner/start/close through UI.
The separate scan function requires a trusted host ownership guard and an existing
driver; it is deliberately not a CLI scenario until phone/fixture admission is wired.
Neither flow installs or launches the target App, changes permissions/radio, or
proves RF identity, artifact provenance, or native resource reclamation.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from appium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
)
from selenium.webdriver.remote.webelement import WebElement

from tools.android import android_appium
from tools.android.android_appium import create_android_driver
from tools.apple import ios_appium
from tools.apple.ios_appium import (
    DEFAULT_TUNNEL_REGISTRY_PORT,
    create_ios_driver,
    require_remotexpc_tunnel,
)
from tools.mobile_appium import AppiumError, AppiumServer

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / ".work" / "react-native-bleng-ui"
POLL_SECONDS = 0.1
START_HOLD_QUIET_SECONDS = 5


class _UiDeadline(AppiumError):
    """Local UI deadline, distinct from a host authority failure of the same base type."""


def _remaining(deadline: float, description: str) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _UiDeadline(f"Timeout waiting for {description}")
    return remaining


def _foreground(
    driver: webdriver.Remote, platform: str, app_id: str, timeout: float
) -> None:
    # Never activate an App to repair a missing/foreign foreground. A failed
    # foreground check aborts the attempt rather than retrying a UI action.
    if platform == "ios":
        ios_appium.wait_for_bundle(driver, app_id, timeout)
    else:
        android_appium.wait_for_package(driver, app_id, timeout)


class _WorkbenchUi:
    """Shared formal-App UI mechanics, not a BLE runner or device owner."""

    def __init__(
        self,
        driver: webdriver.Remote,
        *,
        platform: str,
        app_id: str,
        timeout: float,
        require_authority: Callable[[], None] | None = None,
        require_app_identity: Callable[[], None] | None = None,
        deadline: float | None = None,
        before_close: Callable[[WebElement, float], None] | None = None,
    ) -> None:
        if platform not in {"ios", "android"}:
            raise ValueError("platform must be ios or android")
        _validate_timeout(timeout, "timeout")
        if not app_id.strip():
            raise ValueError("app_id must not be empty")
        self.driver = driver
        self.platform = platform
        self.app_id = app_id
        self.timeout = timeout
        self.require_authority = require_authority
        self.require_app_identity = require_app_identity
        self.total_deadline = deadline
        self.before_close = before_close
        self.helper = ios_appium if platform == "ios" else android_appium

    def deadline(self) -> float:
        deadline = time.monotonic() + self.timeout
        return min(deadline, self.total_deadline) if self.total_deadline else deadline

    def guard(self, deadline: float, description: str = "formal App UI") -> None:
        _remaining(deadline, description)
        if self.require_authority is not None:
            self.require_authority()
        _remaining(deadline, description)

    def identity(self, deadline: float) -> None:
        # Fresh host checks belong to action/completion boundaries, never UI polling.
        # Lifecycle callers without the scan ownership seam retain their old behavior.
        if self.require_app_identity is not None:
            self.guard(deadline)
            self.require_app_identity()
            self.guard(deadline)

    def foreground(self, deadline: float) -> None:
        self.guard(deadline)
        # The existing helpers sleep at least 0.25s between lookups. A smaller
        # budget allows one remote query per guard, never unguarded helper retries.
        _foreground(
            self.driver,
            self.platform,
            self.app_id,
            min(_remaining(deadline, "foreground App"), 0.1),
        )
        self.guard(deadline)

    def visible(self, identifier: str, deadline: float):
        self.guard(deadline, identifier)
        try:
            element = self.helper.wait_find(
                self.driver, identifier, min(_remaining(deadline, identifier), 0.1)
            )
        except (NoSuchElementException, StaleElementReferenceException, AppiumError):
            self.guard(deadline, identifier)
            return None
        # Authority errors must stay outside missing/stale-element retry handlers.
        self.guard(deadline, identifier)
        try:
            displayed = element.is_displayed()
        except StaleElementReferenceException:
            self.guard(deadline, identifier)
            return None
        self.guard(deadline, identifier)
        return element if displayed else None

    def text(self, identifier: str, deadline: float) -> str | None:
        element = self.visible(identifier, deadline)
        if element is None:
            return None
        self.guard(deadline)
        try:
            value = element.text
        except StaleElementReferenceException:
            self.guard(deadline)
            return None
        self.guard(deadline)
        return value.strip()

    def required_text(self, identifier: str) -> str:
        deadline = self.deadline()
        while True:
            self.foreground(deadline)
            value = self.text(identifier, deadline)
            if value is not None:
                return value
            self.pause(deadline, identifier)

    def pause(self, deadline: float, description: str) -> None:
        self.guard(deadline, description)
        time.sleep(min(POLL_SECONDS, _remaining(deadline, description)))
        self.guard(deadline, description)

    def failed(self, field: str, error_field: str, deadline: float) -> None:
        error_deadline = min(deadline, time.monotonic() + 1)
        try:
            while time.monotonic() < error_deadline:
                self.foreground(deadline)
                detail = self.text(error_field, deadline)
                if detail:
                    raise AppiumError(f"{field} failed: {detail}")
                self.guard(deadline)
                time.sleep(min(POLL_SECONDS, max(0, error_deadline - time.monotonic())))
            self.guard(deadline)
        except _UiDeadline:
            # Keep the known failed phase when its diagnostic text arrives too late.
            # Never catch host authority errors merely because they are AppiumError.
            pass
        raise AppiumError(f"{field} failed; visible {error_field} unavailable")

    def state(self, deadline: float) -> str | None:
        value = self.text("session-state", deadline)
        if value in {"failed", "closedWithError"}:
            self.failed("session-state", "session-error", deadline)
        return value

    def wait_state(self, expected: str, observations: list[str]) -> None:
        deadline = self.deadline()
        while True:
            self.foreground(deadline)
            value = self.state(deadline)
            if value == expected:
                self.guard(deadline)
                if expected in {"started", "closed"}:
                    self.identity(deadline)
                observations.append(expected)
                return
            self.pause(deadline, f"session-state {expected!r}; observed {value!r}")

    def verify_startup_snapshot(self, *, prefix: str = "") -> dict[str, str]:
        deadline = self.deadline()
        authorizations = {
            "notRequired",
            "notDetermined",
            "denied",
            "restricted",
            "authorized",
        }
        fields = (
            (
                "adapter-state",
                "state",
                {"unknown", "unavailable", "poweredOff", "poweredOn"},
            ),
            ("scan-authorization", "scan_authorization", authorizations),
            ("connection-authorization", "connection_authorization", authorizations),
        )
        verified: dict[str, str] = {}
        for identifier, field, allowed in fields:
            identifier = prefix + identifier
            while True:
                self.foreground(deadline)
                value = self.text(identifier, deadline)
                if value is not None:
                    if value not in allowed:
                        raise AppiumError(f"Invalid {identifier}: {value!r}")
                    verified[field] = value
                    break
                self.pause(deadline, identifier)
        return verified

    def verify_page(
        self, page: str, expected_state: str, *, require_evidence: bool = False
    ) -> None:
        deadline = self.deadline()
        description = f"workbench-page {page!r}"
        if require_evidence:
            description += " with lifecycle-evidence start/close succeeded"
        while True:
            self.foreground(deadline)
            value = self.state(deadline)
            if value is not None and value != expected_state:
                raise AppiumError(
                    f"session-state {value!r}; expected {expected_state!r} on {page}"
                )
            title = self.text("formal-host-title", deadline)
            current_page = self.text("workbench-page", deadline)
            if (
                value == expected_state
                and title == "BLEHUB FORMAL RN CONSUMER"
                and current_page == page
            ):
                evidence = (
                    self.text("lifecycle-evidence", deadline)
                    if require_evidence
                    else ""
                )
                if not require_evidence or (
                    evidence is not None
                    and all(
                        re.search(rf"(?m)^(?:[0-9]+\. )?{action}: succeeded$", evidence)
                        for action in ("start", "close")
                    )
                ):
                    self.guard(deadline)
                    return
            self.pause(deadline, description)

    def click(self, identifier: str, expected_state: str, expected_page: str) -> None:
        deadline = self.deadline()
        while True:
            self.foreground(deadline)
            if (
                self.state(deadline) == expected_state
                and self.text("workbench-page", deadline) == expected_page
            ):
                element = self.visible(identifier, deadline)
                self.guard(deadline)
                try:
                    ready = element is not None and element.is_enabled()
                except StaleElementReferenceException:
                    ready = False
                self.guard(deadline)
                if ready:
                    self.foreground(deadline)
                    self.identity(deadline)
                    self.guard(deadline)
                    if identifier == "close-session" and self.before_close is not None:
                        chosen_id = element.id
                        self.before_close(element, deadline)
                        self.guard(deadline)
                        self.foreground(deadline)
                        self.identity(deadline)
                        if (
                            element.id != chosen_id
                            or self.state(deadline) != expected_state
                            or self.text("workbench-page", deadline) != expected_page
                        ):
                            raise AppiumError(
                                "Close target or workbench changed during capture"
                            )
                        self.guard(deadline)
                        if not element.is_displayed():
                            raise AppiumError(
                                "Chosen Close element is no longer visible"
                            )
                        self.guard(deadline)
                        if not element.is_enabled():
                            raise AppiumError(
                                "Chosen Close element is no longer enabled"
                            )
                        self.guard(deadline)
                    # Delivery may already have happened on any click error. Never retry.
                    element.click()
                    self.guard(deadline)
                    self.identity(deadline)
                    return
            self.pause(deadline, identifier)


def _open_session(
    ui: _WorkbenchUi,
    observations: list[str],
    startup_snapshot: dict[str, str] | None,
    *,
    browse: bool = True,
) -> dict[str, str]:
    deadline = ui.deadline()
    while True:
        ui.foreground(deadline)
        if ui.text("formal-host-title", deadline) == "BLEHUB FORMAL RN CONSUMER":
            break
        ui.pause(deadline, "formal-host-title")
    ui.wait_state("idle", observations)
    ui.verify_page("Tests", "idle")
    if browse:
        ui.click("workbench-evidence", "idle", "Tests")
        ui.verify_page("Evidence", "idle")
        ui.click("workbench-tests", "idle", "Evidence")
        ui.verify_page("Tests", "idle")
    ui.click("start-session", "idle", "Tests")
    ui.wait_state("started", observations)
    ui.verify_page("Current run", "started")
    snapshot = ui.verify_startup_snapshot()
    if startup_snapshot is not None:
        startup_snapshot.update(snapshot)
    if browse:
        ui.click("workbench-tests", "started", "Current run")
        ui.verify_page("Tests", "started")
        ui.click("workbench-evidence", "started", "Tests")
        ui.verify_page("Evidence", "started")
        ui.click("workbench-current", "started", "Evidence")
        ui.verify_page("Current run", "started")
        _verify_unchanged_snapshot(ui, snapshot)
    return snapshot


def _verify_unchanged_snapshot(ui: _WorkbenchUi, expected: dict[str, str]) -> None:
    observed = ui.verify_startup_snapshot()
    if observed != expected:
        raise AppiumError(
            f"Startup snapshot changed after browsing: expected {expected!r}; observed {observed!r}"
        )


def _close_session(
    ui: _WorkbenchUi, observations: list[str], *, browse: bool = True
) -> None:
    ui.click("close-session", "started", "Current run")
    ui.wait_state("closed", observations)
    ui.verify_page("Current run", "closed")
    if browse:
        ui.click("workbench-evidence", "closed", "Current run")
        ui.verify_page("Evidence", "closed", require_evidence=True)


def _hold_current_run(ui: _WorkbenchUi, observation: dict[str, float]) -> None:
    """Two bounded reads separated by a quiet window, never navigation/polling."""
    deadline = ui.deadline()
    for field in ("started_monotonic", "finished_monotonic"):
        ui.foreground(deadline)
        if (
            ui.state(deadline) != "started"
            or ui.text("formal-host-title", deadline) != "BLEHUB FORMAL RN CONSUMER"
            or ui.text("workbench-page", deadline) != "Current run"
        ):
            raise AppiumError("Start-hold requires Current run/started throughout")
        ui.guard(deadline)
        observation[field] = time.monotonic()
        if field == "started_monotonic":
            if (
                _remaining(deadline, "start-hold quiet window")
                <= START_HOLD_QUIET_SECONDS
            ):
                raise _UiDeadline("Insufficient deadline for start-hold quiet window")
            time.sleep(START_HOLD_QUIET_SECONDS)
            ui.guard(deadline, "start-hold quiet window")


def exercise_idle_control(
    driver: webdriver.Remote,
    *,
    platform: str,
    app_id: str,
    timeout: float,
    observations: list[str],
    require_authority: Callable[[], None] | None = None,
    require_app_identity: Callable[[], None] | None = None,
    deadline: float | None = None,
) -> None:
    """Browse idle controls only; success is not a lifecycle or BLE/Nitro pass.

    No session is started/closed and no initialization snapshot is read. All clicks
    use the existing guarded, non-retrying UI path; leave Evidence/idle visible.
    """
    ui = _WorkbenchUi(
        driver,
        platform=platform,
        app_id=app_id,
        timeout=timeout,
        require_authority=require_authority,
        require_app_identity=require_app_identity,
        deadline=deadline,
    )
    ui.identity(ui.deadline())
    ui.verify_page("Tests", "idle")
    ui.wait_state("idle", observations)
    for _ in range(2):
        ui.click("workbench-evidence", "idle", "Tests")
        ui.verify_page("Evidence", "idle")
        ui.click("workbench-current", "idle", "Evidence")
        ui.verify_page("Current run", "idle")
        ui.click("workbench-tests", "idle", "Current run")
        ui.verify_page("Tests", "idle")
    ui.click("workbench-evidence", "idle", "Tests")
    ui.verify_page("Evidence", "idle")
    ui.identity(ui.deadline())


def exercise_lifecycle(
    driver: webdriver.Remote,
    *,
    platform: str,
    app_id: str,
    timeout: float,
    observations: list[str],
    startup_snapshot: dict[str, str] | None = None,
    require_authority: Callable[[], None] | None = None,
    require_app_identity: Callable[[], None] | None = None,
    deadline: float | None = None,
    before_close: Callable[[WebElement, float], None] | None = None,
    browse: bool = True,
    hold_observation: dict[str, float] | None = None,
) -> None:
    """Observe initialization/navigation/close only; no scan or RF readiness inference.

    Each phase is bounded. A click is never retried. The final successful Evidence
    page stays visible; device ownership and native cleanup proof belong to the host.
    With browse=False, take one startup snapshot, hold Current run quietly for five
    seconds between two bounded reads, then Close without navigating. This control
    leaves Current run/closed visible and is not lifecycle navigation evidence.
    The optional host-only hook observes the exact chosen Close element before its
    one click, under the same deadline; failure aborts, never reselects or retries.
    """
    if type(browse) is not bool:
        raise TypeError("browse must be an explicit bool")
    if before_close is not None and not callable(before_close):
        raise TypeError("before_close must be a host diagnostic callback")
    ui = _WorkbenchUi(
        driver,
        platform=platform,
        app_id=app_id,
        timeout=timeout,
        require_authority=require_authority,
        require_app_identity=require_app_identity,
        deadline=deadline,
        before_close=before_close,
    )
    _open_session(ui, observations, startup_snapshot, browse=browse)
    if not browse:
        _hold_current_run(ui, hold_observation if hold_observation is not None else {})
    _close_session(ui, observations, browse=browse)


def _wait_scan(ui: _WorkbenchUi, expected: str) -> None:
    deadline = ui.deadline()
    while True:
        ui.foreground(deadline)
        if ui.state(deadline) != "started":
            raise AppiumError("Scan lost its started owner")
        value = ui.text("scan-state", deadline)
        if value == "failed":
            ui.failed("scan-state", "scan-error", deadline)
        if value == expected:
            return
        if value in {"ended", "ownerClosed", "stopped", "idle"} and value != expected:
            raise AppiumError(f"Unexpected scan-state {value!r}; expected {expected!r}")
        ui.pause(deadline, f"scan-state {expected!r}")


def _scan_summary(ui: _WorkbenchUi) -> tuple[int, str]:
    deadline = ui.deadline()
    while True:
        text = ui.required_text("scan-count")
        samples = ui.required_text("scan-samples")
        # Pull completion can publish between separate remote reads. Retry only
        # the observation, never the click; a stable but invalid snapshot still fails.
        if ui.required_text("scan-count") != text:
            ui.pause(deadline, "stable scan summary")
            continue
        count = re.fullmatch(
            r"([0-9]+) advertisements observed; ([0-9]+) samples omitted \(latest 8 only\)\.",
            text,
        )
        entries = samples.split("\n\n") if samples else []
        if count is None or len(entries) > 8:
            raise AppiumError("Invalid bounded scan summary")
        total, omitted = map(int, count.groups())
        if len(entries) != min(total, 8) or omitted != max(0, total - 8):
            raise AppiumError("Scan count and bounded samples disagree")
        return total, samples


def _matches_advertisement(sample: str, name: str, service_uuid: str) -> bool:
    lines = sample.splitlines()
    if (
        len(lines) != 4
        or not lines[0].startswith("name=")
        or not lines[1].startswith("services=")
    ):
        return False
    try:
        observed_name, end = json.JSONDecoder().raw_decode(lines[0][5:])
    except ValueError:
        return False
    if observed_name != name or not lines[0][5 + end :].startswith("; rssi="):
        return False
    # Advertised services only, never a UUID in the name, manufacturer or service data.
    services = re.fullmatch(
        r"(?P<values>none|[0-9a-fA-F-]+(?:, [0-9a-fA-F-]+)*)(?P<omitted> \(\+[1-9][0-9]* omitted\))?",
        lines[1][9:],
    )
    if services is None:
        return False
    values = services["values"].lower().split(", ")
    if len(values) > 8 or (services["omitted"] and len(values) != 8):
        return False
    return service_uuid in values


def _verify_scan_evidence(
    ui: _WorkbenchUi, run: str, pulls: int, *, closed: bool
) -> None:
    if ui.required_text("evidence-run") != run:
        raise AppiumError("Evidence run changed during scan")
    actions = ["start", "begin-scan", "scan-start-wait", *(["scan-pull"] * pulls)]
    if closed:
        actions += ["scan-stop", "close"]
    expected = [
        f"{index}. {action}: succeeded" for index, action in enumerate(actions, 1)
    ]
    if ui.required_text("lifecycle-evidence").splitlines() != expected:
        raise AppiumError(
            "Scan lifecycle-evidence does not contain the exact ordered successful actions"
        )


def exercise_scan(
    driver: webdriver.Remote,
    *,
    platform: str,
    app_id: str,
    timeout: float,
    observations: list[str],
    expected_local_name: str,
    expected_service_uuid: str,
    require_authority: Callable[[], None],
    require_app_identity: Callable[[], None] | None = None,
    startup_snapshot: dict[str, str] | None = None,
    max_pulls: int = 8,
    observation_timeout_ms: int = 5000,
) -> None:
    """Host-injected, bounded scan UI flow; deliberately not reachable from the CLI.

    The host supplies an existing driver and a cheap authority guard for every
    remote boundary (lease, fixture, driver and deadline). Fresh App identity is
    checked at scene entry/exit, before/after each click and after native operation
    completion, not during text/visibility/foreground polling. If no separate
    require_app_identity callback is supplied, require_authority also performs
    those checks; it is never replaced by a no-op or a cached startup snapshot.
    Expected advertisement facts come from the host's validated contract, not a
    ready JSON. These callbacks are trusted ownership seams, not an RF verdict.

    The total timeout includes startup, browsing, all pulls, stop and close. It is
    checked around each remote call; the supplied driver's HTTP timeout must also
    be bounded, since this function cannot interrupt an in-flight WebDriver call.
    No input editing, implicit retry, failure cleanup, lease acquisition, driver
    construction, provisioning, installation or App relaunch is performed here.
    """
    _validate_timeout(timeout, "timeout")
    if not callable(require_authority):
        raise TypeError("require_authority must be a host ownership callback")
    if require_app_identity is not None and not callable(require_app_identity):
        raise TypeError("require_app_identity must be a host identity callback")
    if type(max_pulls) is not int or not 1 <= max_pulls <= 8:
        raise ValueError("max_pulls must be an integer in [1, 8]")
    if (
        type(observation_timeout_ms) is not int
        or not 1 <= observation_timeout_ms <= 60000
        or observation_timeout_ms >= timeout * 1000
    ):
        raise ValueError(
            "observation_timeout_ms must be in [1, 60000] and below the total budget"
        )
    # JavaScript slice(0, 160) counts UTF-16 code units, not Python code points.
    if (
        not expected_local_name.strip()
        or len(expected_local_name.encode("utf-16-le")) > 320
    ):
        raise ValueError(
            "expected_local_name must be nonempty and fit the untruncated UI name"
        )
    try:
        canonical_service = str(uuid.UUID(expected_service_uuid))
    except (ValueError, AttributeError) as error:
        raise ValueError(
            "expected_service_uuid must be a full canonical UUID"
        ) from error
    if canonical_service != expected_service_uuid.lower():
        raise ValueError("expected_service_uuid must be a full canonical UUID")
    deadline = time.monotonic() + timeout
    ui = _WorkbenchUi(
        driver,
        platform=platform,
        app_id=app_id,
        timeout=timeout,
        require_authority=require_authority,
        require_app_identity=(
            require_authority if require_app_identity is None else require_app_identity
        ),
        deadline=deadline,
    )
    ui.guard(deadline)
    ui.identity(deadline)
    snapshot = _open_session(ui, observations, startup_snapshot)
    if snapshot["state"] != "poweredOn" or snapshot["scan_authorization"] not in {
        "authorized",
        "notRequired",
    }:
        raise AppiumError(
            "Initialization observation is not ready for this scan; it is not live authorization"
        )
    if ui.required_text("scan-observation-timeout") != str(observation_timeout_ms):
        raise AppiumError(
            "Scan observation timeout control differs from the host budget"
        )
    _wait_scan(ui, "idle")
    if _scan_summary(ui) != (0, ""):
        raise AppiumError("Scan has pre-existing observations")
    ui.click("start-scan", "started", "Current run")
    _wait_scan(ui, "running")
    ui.identity(ui.deadline())
    observations.append("scan:running")
    if _scan_summary(ui) != (0, ""):
        raise AppiumError("Scan observation changed without an explicit pull")
    ui.click("workbench-evidence", "started", "Current run")
    ui.verify_page("Evidence", "started")
    run = ui.required_text("evidence-run")
    if re.fullmatch(r"Run [1-9][0-9]*", run) is None:
        raise AppiumError("Invalid scan evidence run")
    _verify_scan_evidence(ui, run, 0, closed=False)
    ui.click("workbench-current", "started", "Evidence")
    ui.verify_page("Current run", "started")
    samples = ""
    for pulls in range(1, max_pulls + 1):
        ui.click("next-scan-event", "started", "Current run")
        # The running state alone does not settle a pending pull. Its action
        # evidence and exact count increment must both become visible before proceeding.
        _wait_scan(ui, "running")
        pull_deadline = ui.deadline()
        while True:
            ui.foreground(pull_deadline)
            total, samples = _scan_summary(ui)
            if total == pulls:
                break
            if total != pulls - 1:
                raise AppiumError("Scan count did not advance by exactly one pull")
            # A real pull failure changes phase, not necessarily session-state.
            _wait_scan(ui, "running")
            ui.pause(pull_deadline, "single scan pull completion")
        ui.identity(pull_deadline)
        # Only the newly returned sample can satisfy this pull's target observation.
        if _matches_advertisement(
            samples.split("\n\n")[-1], expected_local_name, canonical_service
        ):
            observations.append("scan:advertisement-matched")
            break
    else:
        raise AppiumError("No matching advertisement within the explicit pull budget")
    # Browsing a populated run cannot replace scan ownership, observations or evidence.
    ui.click("workbench-tests", "started", "Current run")
    ui.verify_page("Tests", "started")
    ui.click("workbench-evidence", "started", "Tests")
    ui.verify_page("Evidence", "started")
    _verify_scan_evidence(ui, run, pulls, closed=False)
    ui.click("workbench-current", "started", "Evidence")
    ui.verify_page("Current run", "started")
    _verify_unchanged_snapshot(ui, snapshot)
    _wait_scan(ui, "running")
    if _scan_summary(ui) != (pulls, samples):
        raise AppiumError("Scan observations changed after browsing")
    ui.click("stop-scan", "started", "Current run")
    _wait_scan(ui, "stopped")
    ui.identity(ui.deadline())
    observations.append("scan:stopped")
    _close_session(ui, observations)
    _verify_scan_evidence(ui, run, pulls, closed=True)
    if ui.required_text("scan-state") != "ownerClosed":
        raise AppiumError("Final Evidence did not retire scan controls")
    if _scan_summary(ui) != (pulls, samples):
        raise AppiumError("Final Evidence lost or replaced scan observations")
    if ui.verify_startup_snapshot(prefix="evidence-") != snapshot:
        raise AppiumError("Final Evidence lost or replaced the initialization snapshot")
    ui.guard(deadline)
    ui.identity(deadline)


def _capture_diagnostics(
    driver: webdriver.Remote, attempt: Path, errors: list[str]
) -> None:
    """Collect best-effort UI evidence without masking a scenario/cleanup error."""
    try:
        (attempt / "page-source.xml").write_text(driver.page_source, encoding="utf-8")
    except Exception as failure:  # noqa: BLE001 -- diagnostics must not mask primary errors
        errors.append(f"page source: {type(failure).__name__}: {failure}")
    try:
        if not driver.save_screenshot(str(attempt / "screenshot.png")):
            raise AppiumError("WebDriver did not save screenshot")
    except Exception as failure:  # noqa: BLE001 -- diagnostics must not mask primary errors
        errors.append(f"screenshot: {type(failure).__name__}: {failure}")


def run(args: argparse.Namespace) -> Path:
    """Return this attempt's result.json, including initialization/cleanup failure.

    Callers must inspect status; the CLI converts a failed status to exit code 2.
    The only owned resources are the Appium server and WebDriver session. The
    installed, host-opened App is never started, stopped, or reset by this runner.
    """
    _validate(args)
    run_id = str(uuid.uuid4())
    attempt = args.output / run_id
    attempt.mkdir(parents=True, exist_ok=False)
    result_path = attempt / "result.json"
    phases: list[str] = []
    startup_snapshot: dict[str, str] = {}
    cleanup_errors: list[str] = []
    diagnostic_errors: list[str] = []
    result: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "scope": "formal-rn-lifecycle-ui",
        "platform": args.platform,
        "device": args.device,
        "app_id": args.app_id,
        "session_id": None,
        "phases": phases,
        "startup_snapshot": startup_snapshot,
        "status": "failed",
        "stage": "initialization",
        "scenario_executed": False,
        "error": None,
        "cleanup_errors": cleanup_errors,
        "diagnostic_errors": diagnostic_errors,
    }
    server: AppiumServer | None = None
    driver: webdriver.Remote | None = None
    try:
        if args.platform == "android":
            result["stage"] = "driver-admission"
            android_appium.require_prepared_driver_contract()
        if args.platform == "ios":
            result["stage"] = "tunnel"
            require_remotexpc_tunnel(args.tunnel_registry_port)
        result["stage"] = "server"
        server = AppiumServer(
            node=args.node,
            entry=args.appium_entry,
            port=args.port,
            log=attempt / "appium.log",
            timeout=args.server_timeout,
        )
        server.start()
        result["stage"] = "driver"
        if args.platform == "ios":
            driver = create_ios_driver(
                server.base_url,
                device=args.device,
                team=args.team,
                bundle_id=args.app_id,
                wda_bundle_id=args.wda_bundle_id,
                wda_port=args.wda_port,
                derived_data=attempt / "wda-derived-data",
                auto_launch=False,
                command_timeout=args.timeout,
            )
        else:
            driver = create_android_driver(
                server.base_url,
                device=args.device,
                package=args.app_id,
                activity=args.activity,
                auto_launch=False,
                command_timeout=args.timeout,
            )
        result["session_id"] = driver.session_id
        result["stage"] = "foreground"
        _foreground(driver, args.platform, args.app_id, args.timeout)
        result["stage"] = "scenario"
        result["scenario_executed"] = True
        exercise_lifecycle(
            driver,
            platform=args.platform,
            app_id=args.app_id,
            timeout=args.timeout,
            observations=phases,
            startup_snapshot=startup_snapshot,
        )
        result["status"] = "passed"
        result["stage"] = "complete"
    except Exception as failure:  # noqa: BLE001 -- persist unexpected initialization/UI failures
        result["error"] = f"{type(failure).__name__}: {failure}"
    finally:
        if driver is not None:
            _capture_diagnostics(driver, attempt, diagnostic_errors)
            try:
                driver.quit()
            except Exception as failure:  # noqa: BLE001 -- continue independent server cleanup
                cleanup_errors.append(
                    f"driver.quit: {type(failure).__name__}: {failure}"
                )
        if server is not None:
            try:
                server.stop()
            except Exception as failure:  # noqa: BLE001 -- preserve primary and cleanup failures
                cleanup_errors.append(
                    f"server.stop: {type(failure).__name__}: {failure}"
                )
        if cleanup_errors:
            result["status"] = "failed"
            if result["error"] is None:
                result["stage"] = "cleanup"
        result_path.write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    return result_path


def _validate_timeout(value: float, name: str) -> None:
    if not math.isfinite(value) or not 0 < value <= 300:
        raise ValueError(f"{name} must be finite and in (0, 300] seconds")


def _validate(args: argparse.Namespace) -> None:
    if args.platform not in {"ios", "android"}:
        raise ValueError("platform must be ios or android")
    for name in ("device", "app_id", "node"):
        if not getattr(args, name).strip():
            raise ValueError(f"{name} must not be empty")
    required = "activity" if args.platform == "android" else "team"
    if not (getattr(args, required, None) or "").strip():
        raise ValueError(f"--{required} is required for {args.platform}")
    if args.platform == "ios" and not args.wda_bundle_id.strip():
        raise ValueError("wda_bundle_id must not be empty")
    _validate_timeout(args.timeout, "timeout")
    _validate_timeout(args.server_timeout, "server-timeout")
    for name in ("port", "wda_port", "tunnel_registry_port"):
        if not 1 <= getattr(args, name) <= 65535:
            raise ValueError(f"{name} must be in [1, 65535]")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("android", "ios"), required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--appium-entry", type=Path, required=True)
    parser.add_argument("--activity")
    parser.add_argument("--team")
    parser.add_argument("--node", default="node")
    parser.add_argument("--port", type=int, default=4723)
    parser.add_argument("--wda-port", type=int, default=8101)
    parser.add_argument("--wda-bundle-id", default="dev.blehub.appium.wda")
    parser.add_argument(
        "--tunnel-registry-port", type=int, default=DEFAULT_TUNNEL_REGISTRY_PORT
    )
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--server-timeout", type=float, default=30)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        _validate(args)
    except ValueError as failure:
        parser.error(str(failure))
    try:
        result_path = run(args)
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as failure:
        print(f"formal_rn_lifecycle_ui=failed error={failure}", file=sys.stderr)
        return 2
    passed = result["status"] == "passed"
    print(
        f"formal_rn_lifecycle_ui={result['status']} result={result_path}",
        file=sys.stdout if passed else sys.stderr,
    )
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
