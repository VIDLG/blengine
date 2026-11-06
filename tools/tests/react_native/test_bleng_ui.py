"""Hardware-free regressions for the shared formal RN lifecycle UI gate."""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    WebDriverException,
)

from tools.react_native import run_bleng_ui as ui
from tools.tests.react_native.support.bleng_ui import (
    Clock,
    Element,
    LifecycleDriver,
    ScanDriver,
    fast_clock,
)


class IdleControlTests(unittest.TestCase):
    def test_fixed_idle_browsing_reads_only_ui_and_never_starts_or_closes(self) -> None:
        clicks = [
            "workbench-evidence",
            "workbench-current",
            "workbench-tests",
        ] * 2 + ["workbench-evidence"]
        allowed_ids = {
            "formal-host-title",
            "session-state",
            "workbench-page",
            *clicks,
        }
        for platform in ("ios", "android"):
            for managed in (False, True):
                with self.subTest(platform=platform, managed=managed), fast_clock():
                    driver = ScanDriver(platform)
                    observations: list[str] = []
                    authority, identity = Mock(), Mock()
                    ui.exercise_idle_control(
                        driver,
                        platform=platform,
                        app_id=driver.app_id,
                        timeout=2,
                        observations=observations,
                        require_authority=authority if managed else None,
                        require_app_identity=identity if managed else None,
                    )
                    self.assertEqual(observations, ["idle"])
                    self.assertEqual(
                        [item for item in driver.trace if item.startswith("click:")],
                        [f"click:{identifier}" for identifier in clicks],
                    )
                    self.assertEqual(
                        driver.visits,
                        [
                            ("idle", page)
                            for page in ("Evidence", "Current run", "Tests")
                        ]
                        * 2
                        + [("idle", "Evidence")],
                    )
                    self.assertEqual((driver.page, driver.state), ("Evidence", "idle"))
                    self.assertEqual(set(driver.find_calls), allowed_ids)
                    self.assertEqual(driver.actions, [])
                    for node in (
                        driver.start,
                        driver.close,
                        driver.scan_start,
                        driver.scan_next,
                        driver.scan_stop,
                    ):
                        node.click.assert_not_called()
                    driver.quit.assert_not_called()
                    if managed:
                        self.assertGreater(authority.call_count, identity.call_count)
                        # Entry/exit plus fresh identity before and after all seven clicks.
                        self.assertGreaterEqual(identity.call_count, 16)

    def test_entry_requires_idle_tests_before_any_navigation(self) -> None:
        for platform in ("ios", "android"):
            for page, state in (("Evidence", "idle"), ("Tests", "started")):
                with (
                    self.subTest(platform=platform, page=page, state=state),
                    fast_clock(),
                ):
                    driver = ScanDriver(platform)
                    driver.page, driver.state = page, state
                    with self.assertRaises(ui.AppiumError):
                        ui.exercise_idle_control(
                            driver,
                            platform=platform,
                            app_id=driver.app_id,
                            timeout=2,
                            observations=[],
                        )
                    self.assertFalse(
                        any(item.startswith("click:") for item in driver.trace)
                    )
                    self.assertEqual(driver.actions, [])

    def test_authority_and_deadline_abort_at_sdk_boundaries_without_more_io(
        self,
    ) -> None:
        for platform in ("ios", "android"):
            for failure in ("authority", "deadline"):
                for boundary, clicks in (
                    ("entry", 0),
                    ("find:workbench-evidence", 0),
                    ("enabled:workbench-current", 1),
                    ("click:workbench-tests", 3),
                ):
                    with (
                        self.subTest(
                            platform=platform, failure=failure, boundary=boundary
                        ),
                        fast_clock() as clock,
                    ):
                        driver = ScanDriver(platform)
                        observations: list[str] = []
                        stopped: list[int] = []
                        deadline = clock.now + 0.5

                        def invalidate(
                            action: str,
                            *,
                            boundary: str = boundary,
                            stopped: list[int] = stopped,
                            driver: ScanDriver = driver,
                            failure: str = failure,
                            clock: Clock = clock,
                            deadline: float = deadline,
                        ) -> None:
                            if action != boundary:
                                return
                            stopped.append(len(driver.trace))
                            if failure == "authority":
                                driver.authorized = False
                            else:
                                clock.now = deadline

                        driver.after_action = invalidate
                        if boundary == "entry":
                            invalidate("entry")
                        with self.assertRaises(ui.AppiumError) as caught:
                            ui.exercise_idle_control(
                                driver,
                                platform=platform,
                                app_id=driver.app_id,
                                timeout=2,
                                observations=observations,
                                require_authority=driver.require_authority,
                                deadline=deadline,
                            )
                        if failure == "authority":
                            self.assertIs(caught.exception, driver.guard_error)
                            self.assertEqual(clock.now, 0)
                        else:
                            self.assertRegex(str(caught.exception), "Timeout")
                            self.assertEqual(clock.now, deadline)
                        self.assertEqual(len(stopped), 1)
                        self.assertTrue(
                            all(item == "guard" for item in driver.trace[stopped[0] :])
                        )
                        self.assertEqual(
                            sum(item.startswith("click:") for item in driver.trace),
                            clicks,
                        )
                        self.assertEqual(
                            observations, [] if boundary == "entry" else ["idle"]
                        )
                        self.assertEqual(driver.actions, [])

    def test_identity_failure_before_or_after_click_is_not_retried(self) -> None:
        for platform in ("ios", "android"):
            for fail_at, clicks in ((1, 0), (2, 0), (3, 1)):
                with self.subTest(platform=platform, fail_at=fail_at), fast_clock():
                    driver = ScanDriver(platform)
                    error = StaleElementReferenceException("host identity invalidated")
                    stopped: list[int] = []

                    require_identity = Mock()

                    def identity(
                        *,
                        require_identity: Mock = require_identity,
                        fail_at: int = fail_at,
                        stopped: list[int] = stopped,
                        driver: ScanDriver = driver,
                        error: Exception = error,
                    ) -> None:
                        if require_identity.call_count == fail_at:
                            stopped.append(len(driver.trace))
                            raise error

                    require_identity.side_effect = identity
                    with self.assertRaises(StaleElementReferenceException) as caught:
                        ui.exercise_idle_control(
                            driver,
                            platform=platform,
                            app_id=driver.app_id,
                            timeout=2,
                            observations=[],
                            require_authority=driver.require_authority,
                            require_app_identity=require_identity,
                        )
                    self.assertIs(caught.exception, error)
                    self.assertEqual(require_identity.call_count, fail_at)
                    self.assertEqual(len(stopped), 1)
                    self.assertEqual(driver.trace[stopped[0] :], [])
                    self.assertEqual(
                        sum(item.startswith("click:") for item in driver.trace), clicks
                    )
                    self.assertEqual(driver.actions, [])

    def test_browsing_preserves_click_safety_and_never_retries_delivery(self) -> None:
        for platform in ("ios", "android"):
            for failure in (
                "uncertain",
                "late",
                "foreign",
                "disabled",
                "state",
                "page",
            ):
                with (
                    self.subTest(platform=platform, failure=failure),
                    fast_clock() as clock,
                ):
                    driver = ScanDriver(platform)
                    button = driver.navigation["workbench-evidence"]
                    if failure == "disabled":
                        button.is_enabled.side_effect = lambda button=button: (
                            button.read("enabled", False)
                        )
                    elif failure == "foreign":

                        def lookup(driver: ScanDriver = driver, button=button):
                            driver.foreground = "foreign.app"
                            return button

                        driver.overrides["workbench-evidence"] = Mock(
                            side_effect=lookup
                        )
                    else:

                        def click(
                            driver: ScanDriver = driver,
                            failure: str = failure,
                            clock: Clock = clock,
                        ) -> None:
                            driver.record("click:workbench-evidence")
                            driver.navigate("Evidence")
                            if failure == "uncertain":
                                raise StaleElementReferenceException("click uncertain")
                            if failure == "late":
                                clock.now = 0.5
                            elif failure == "state":
                                driver.state = "started"
                            elif failure == "page":
                                driver.page = "Tests"

                        button.click.side_effect = click
                    observations: list[str] = []
                    with self.assertRaises(
                        (ui.AppiumError, StaleElementReferenceException)
                    ):
                        ui.exercise_idle_control(
                            driver,
                            platform=platform,
                            app_id=driver.app_id,
                            timeout=2,
                            observations=observations,
                            deadline=0.5,
                        )
                    self.assertEqual(observations, ["idle"])
                    self.assertEqual(
                        button.click.call_count,
                        int(failure not in {"foreign", "disabled"}),
                    )
                    driver.navigation["workbench-current"].click.assert_not_called()
                    driver.navigation["workbench-tests"].click.assert_not_called()
                    self.assertEqual(driver.actions, [])
                    self.assertLessEqual(clock.now, 0.6)


class StartHoldControlTests(unittest.TestCase):
    def exercise(self, driver, observations, window, **kwargs):
        ui.exercise_lifecycle(
            driver,
            platform="android",
            app_id=driver.app_id,
            timeout=10,
            observations=observations,
            browse=False,
            hold_observation=window,
            **kwargs,
        )

    def test_start_snapshot_hold_and_same_page_close_without_navigation_or_scan(self):
        with fast_clock() as clock:
            driver = ScanDriver("android")
            observations, window = [], {}
            authority, identity = Mock(), Mock()
            captures = []

            def capture(element, deadline):
                self.assertIs(element, driver.close)
                self.assertEqual(driver.page, "Current run")
                self.assertEqual(driver.state, "started")
                self.assertGreaterEqual(clock.now, 5)
                self.assertLess(clock.now, deadline)
                driver.close.click.assert_not_called()
                captures.append(element)

            driver.close.id = "chosen-close"
            self.exercise(
                driver,
                observations,
                window,
                require_authority=authority,
                require_app_identity=identity,
                before_close=capture,
            )
            self.assertEqual(observations, ["idle", "started", "closed"])
            self.assertEqual(window, {"started_monotonic": 0, "finished_monotonic": 5})
            self.assertEqual(captures, [driver.close])
            driver.start.click.assert_called_once_with()
            driver.close.click.assert_called_once_with()
            for node in (
                *driver.navigation.values(),
                driver.scan_start,
                driver.scan_next,
                driver.scan_stop,
            ):
                node.click.assert_not_called()
            for identifier in driver.snapshot_nodes:
                self.assertEqual(driver.trace.count(f"text:{identifier}"), 1)
            self.assertEqual(driver.visits, [("started", "Current run")])
            self.assertEqual(driver.page, "Current run")
            self.assertEqual(driver.state, "closed")
            self.assertGreater(authority.call_count, identity.call_count)

    def test_quiet_window_deadline_drift_and_authority_failure_refuse_close(self):
        for failure in (
            "deadline",
            "page",
            "state",
            "foreground",
            "authority",
            "interrupt",
            "late-read",
        ):
            with self.subTest(failure=failure), fast_clock() as clock:
                driver = ScanDriver("android")
                observations, window = [], {}
                authority = Mock()

                def sleep(
                    seconds,
                    *,
                    clock=clock,
                    failure=failure,
                    driver=driver,
                    authority=authority,
                ):
                    clock.sleep(seconds)
                    if failure == "page":
                        driver.page = "Evidence"
                    elif failure == "state":
                        driver.state = "closed"
                    elif failure == "foreground":
                        driver.foreground = "foreign.app"
                    elif failure == "authority":
                        authority.side_effect = RuntimeError("lost lease")
                    elif failure == "interrupt":
                        raise KeyboardInterrupt
                    elif failure == "late-read":
                        driver.after_action = lambda action: setattr(clock, "now", 20)

                with (
                    patch("time.sleep", side_effect=sleep),
                    self.assertRaises(
                        (ui.AppiumError, RuntimeError, KeyboardInterrupt)
                    ),
                ):
                    self.exercise(
                        driver,
                        observations,
                        window,
                        deadline=4 if failure == "deadline" else 10,
                        require_authority=authority,
                    )
                self.assertEqual(observations, ["idle", "started"])
                self.assertNotIn("finished_monotonic", window)
                driver.start.click.assert_called_once_with()
                driver.close.click.assert_not_called()
                for node in driver.navigation.values():
                    node.click.assert_not_called()

    def test_hold_reads_are_fixed_and_missing_state_is_not_polled(self):
        with fast_clock() as clock:
            driver = ScanDriver("android")
            observations, window = [], {}
            original_sleep = clock.sleep

            def sleep(seconds):
                original_sleep(seconds)
                if seconds == 5:
                    driver.overrides["session-state"] = Mock(
                        side_effect=NoSuchElementException()
                    )

            with (
                patch("time.sleep", side_effect=sleep) as sleep_mock,
                self.assertRaises(ui.AppiumError),
            ):
                self.exercise(driver, observations, window)
            # No retry/poll loop after the quiet window's missing read.
            self.assertEqual(driver.overrides["session-state"].call_count, 1)
            self.assertEqual(sleep_mock.call_args_list[0].args, (5,))
            self.assertEqual(sleep_mock.call_count, 2)  # One bounded helper lookup.
            driver.close.click.assert_not_called()


class LifecycleTests(unittest.TestCase):
    def exercise(self, driver: LifecycleDriver, observations: list[str]) -> None:
        ui.exercise_lifecycle(
            driver,
            platform=driver.platform,
            app_id=driver.app_id,
            timeout=2,
            observations=observations,
        )

    def test_preclose_capture_uses_chosen_element_once_and_rechecks_before_click(
        self,
    ) -> None:
        for mode in ("normal", "failure", "expired", "page-drift", "element-drift"):
            with self.subTest(mode=mode), fast_clock() as clock:
                driver = LifecycleDriver("android")
                driver.close.id = "fixture-close-element"
                seen = []

                def capture(
                    element,
                    deadline,
                    *,
                    driver=driver,
                    clock=clock,
                    mode=mode,
                    seen=seen,
                ):
                    self.assertIs(element, driver.close)
                    self.assertEqual(driver.state, "started")
                    self.assertEqual(driver.page, "Current run")
                    self.assertLessEqual(deadline, 2)
                    seen.append(element)
                    driver.close.click.assert_not_called()
                    if mode == "failure":
                        raise ValueError("snapshot unavailable")
                    if mode == "expired":
                        clock.now = deadline
                    if mode == "page-drift":
                        driver.page = "Evidence"
                    if mode == "element-drift":
                        element.id = "replacement-element"

                observations = []
                arguments = {
                    "platform": "android",
                    "app_id": driver.app_id,
                    "timeout": 2,
                    "observations": observations,
                    "deadline": clock.now + 2,
                    "before_close": capture,
                }
                if mode == "normal":
                    ui.exercise_lifecycle(driver, **arguments)
                    driver.close.click.assert_called_once_with()
                    self.assertEqual(observations, ["idle", "started", "closed"])
                else:
                    with self.assertRaises((ValueError, ui.AppiumError)):
                        ui.exercise_lifecycle(driver, **arguments)
                    driver.close.click.assert_not_called()
                    self.assertEqual(observations, ["idle", "started"])
                self.assertEqual(seen, [driver.close])

    def test_managed_lifecycle_checks_authority_and_native_identity(self) -> None:
        with fast_clock() as clock:
            driver = LifecycleDriver("android")
            authority, identity = Mock(), Mock()
            observations: list[str] = []
            ui.exercise_lifecycle(
                driver,
                platform=driver.platform,
                app_id=driver.app_id,
                timeout=2,
                observations=observations,
                require_authority=authority,
                require_app_identity=identity,
                deadline=clock.now + 2,
            )
            self.assertEqual(observations, ["idle", "started", "closed"])
            self.assertGreater(authority.call_count, identity.call_count)
            self.assertGreaterEqual(identity.call_count, 4)
            self.assertEqual(driver.page, "Evidence")

        with fast_clock() as clock:
            driver = LifecycleDriver("android")
            with self.assertRaisesRegex(RuntimeError, "owner invalidated"):
                ui.exercise_lifecycle(
                    driver,
                    platform=driver.platform,
                    app_id=driver.app_id,
                    timeout=2,
                    observations=[],
                    require_authority=Mock(
                        side_effect=RuntimeError("owner invalidated")
                    ),
                    require_app_identity=Mock(),
                    deadline=clock.now + 2,
                )
            driver.start.click.assert_not_called()
            driver.close.click.assert_not_called()
            self.assertEqual(driver.find_calls, [])

    def test_shared_scenario_uses_text_and_platform_testid_locators(self) -> None:
        for platform in ("ios", "android"):
            with self.subTest(platform=platform), fast_clock():
                driver = LifecycleDriver(platform)
                observations: list[str] = []
                self.exercise(driver, observations)
                self.assertEqual(observations, ["idle", "started", "closed"])
                driver.start.click.assert_called_once_with()
                driver.close.click.assert_called_once_with()
                self.assertEqual(driver.state, "closed")
                self.assertEqual(driver.page, "Evidence")
                self.assertEqual(
                    driver.visits,
                    [
                        ("idle", "Evidence"),
                        ("idle", "Tests"),
                        ("started", "Current run"),
                        ("started", "Tests"),
                        ("started", "Evidence"),
                        ("started", "Current run"),
                        ("closed", "Evidence"),
                    ],
                )
                for identifier in driver.snapshot_nodes:
                    self.assertEqual(driver.find_calls.count(identifier), 2)
                driver.quit.assert_not_called()

    def test_wrong_workbench_page_refuses_progress(self) -> None:
        for platform in ("ios", "android"):
            for boundary in ("default", "idle", "start", "started", "closed"):
                with (
                    self.subTest(platform=platform, boundary=boundary),
                    fast_clock() as clock,
                ):
                    driver = LifecycleDriver(platform)
                    if boundary == "default":
                        driver.page = "Evidence"
                    elif boundary == "start":
                        driver.start.click.side_effect = lambda driver=driver: setattr(
                            driver, "state", "started"
                        )
                    else:
                        state = boundary

                        def navigate(
                            driver: LifecycleDriver = driver, state: str = state
                        ) -> None:
                            if driver.state != state:
                                driver.navigate("Evidence")

                        driver.navigation[
                            "workbench-evidence"
                        ].click.side_effect = navigate
                    with self.assertRaisesRegex(ui.AppiumError, "workbench-page"):
                        self.exercise(driver, [])
                    self.assertLessEqual(clock.now, 2.1)
                    self.assertEqual(
                        driver.start.click.call_count,
                        int(boundary in {"start", "started", "closed"}),
                    )
                    self.assertEqual(
                        driver.close.click.call_count, int(boundary == "closed")
                    )

    def test_navigation_cannot_change_session_state(self) -> None:
        for platform in ("ios", "android"):
            for phase in ("idle", "started", "closed"):
                for lost_state in ("idle", "started", "closed", "starting"):
                    if phase == lost_state:
                        continue
                    with (
                        self.subTest(
                            platform=platform, phase=phase, lost_state=lost_state
                        ),
                        fast_clock(),
                    ):
                        driver = LifecycleDriver(platform)

                        def navigate(
                            driver: LifecycleDriver = driver,
                            phase: str = phase,
                            lost_state: str = lost_state,
                        ) -> None:
                            if driver.state == phase:
                                driver.state = lost_state
                            driver.navigate("Evidence")

                        driver.navigation[
                            "workbench-evidence"
                        ].click.side_effect = navigate
                        with self.assertRaisesRegex(
                            ui.AppiumError, "session-state.*expected"
                        ):
                            self.exercise(driver, [])
                        self.assertEqual(
                            driver.start.click.call_count, int(phase != "idle")
                        )
                        self.assertEqual(
                            driver.close.click.call_count, int(phase == "closed")
                        )

    def test_navigation_must_preserve_global_title_and_started_snapshot(self) -> None:
        for platform in ("ios", "android"):
            for missing in ("title", "snapshot"):
                with self.subTest(platform=platform, missing=missing), fast_clock():
                    driver = LifecycleDriver(platform)

                    def navigate(
                        driver: LifecycleDriver = driver, missing: str = missing
                    ) -> None:
                        driver.navigate("Current run")
                        if missing == "title":
                            driver.title.is_displayed.return_value = False
                        else:
                            driver.snapshot_nodes[
                                "adapter-state"
                            ].is_displayed.return_value = False

                    driver.navigation["workbench-current"].click.side_effect = navigate
                    with self.assertRaises(ui.AppiumError):
                        self.exercise(driver, [])
                    driver.start.click.assert_called_once_with()
                    driver.close.click.assert_not_called()

    def test_valid_but_changed_snapshot_after_browsing_refuses_close(self) -> None:
        for platform in ("ios", "android"):
            for identifier, changed_value in (
                ("adapter-state", "poweredOff"),
                ("scan-authorization", "denied"),
                ("connection-authorization", "authorized"),
            ):
                for collect in (False, True):
                    with (
                        self.subTest(
                            platform=platform, field=identifier, collect=collect
                        ),
                        fast_clock(),
                    ):
                        driver = LifecycleDriver(platform)

                        def navigate(
                            driver: LifecycleDriver = driver,
                            identifier: str = identifier,
                            changed_value: str = changed_value,
                        ) -> None:
                            driver.navigate("Current run")
                            driver.snapshot_nodes[identifier].text = changed_value

                        driver.navigation[
                            "workbench-current"
                        ].click.side_effect = navigate
                        observations: list[str] = []
                        snapshot: dict[str, str] = {}
                        with self.assertRaisesRegex(
                            ui.AppiumError, "Startup snapshot changed after browsing"
                        ):
                            ui.exercise_lifecycle(
                                driver,
                                platform=platform,
                                app_id=driver.app_id,
                                timeout=2,
                                observations=observations,
                                startup_snapshot=snapshot if collect else None,
                            )
                        self.assertEqual(observations, ["idle", "started"])
                        self.assertEqual(driver.page, "Current run")
                        driver.start.click.assert_called_once_with()
                        driver.close.click.assert_not_called()
                        if collect:
                            self.assertEqual(
                                snapshot,
                                {
                                    "state": "poweredOn",
                                    "scan_authorization": "authorized",
                                    "connection_authorization": "notRequired",
                                },
                            )

    def test_closed_evidence_requires_both_visible_success_records(self) -> None:
        for platform in ("ios", "android"):
            for failure in ("missing", "hidden", "stale", "start", "close"):
                with self.subTest(platform=platform, failure=failure), fast_clock():
                    driver = LifecycleDriver(platform)
                    if failure == "missing":
                        driver.overrides["lifecycle-evidence"] = Mock(
                            side_effect=NoSuchElementException()
                        )
                    elif failure == "hidden":
                        driver.evidence.is_displayed.return_value = False
                    elif failure == "stale":
                        driver.evidence.is_displayed.side_effect = (
                            StaleElementReferenceException()
                        )
                    else:
                        driver.evidence.text = (
                            "close: succeeded"
                            if failure == "start"
                            else "start: succeeded"
                        )
                    observations: list[str] = []
                    with self.assertRaisesRegex(ui.AppiumError, "lifecycle-evidence"):
                        self.exercise(driver, observations)
                    self.assertEqual(observations, ["idle", "started", "closed"])
                    self.assertEqual(driver.page, "Evidence")
                    driver.start.click.assert_called_once_with()
                    driver.close.click.assert_called_once_with()

    def test_navigation_click_safety_boundaries(self) -> None:
        for platform in ("ios", "android"):
            for failure in ("uncertain", "late", "foreign", "disabled"):
                with (
                    self.subTest(platform=platform, failure=failure),
                    fast_clock() as clock,
                ):
                    driver = LifecycleDriver(platform)
                    button = driver.navigation["workbench-evidence"]
                    if failure == "uncertain":

                        def uncertain_click(driver: LifecycleDriver = driver) -> None:
                            driver.navigate("Evidence")
                            raise StaleElementReferenceException("click uncertain")

                        button.click.side_effect = uncertain_click
                    elif failure == "late":

                        def late_click(clock: Clock = clock) -> None:
                            clock.now += 3

                        button.click.side_effect = late_click
                    elif failure == "foreign":

                        def lookup(
                            driver: LifecycleDriver = driver, button: Element = button
                        ) -> Element:
                            driver.foreground = "foreign.app"
                            return button

                        driver.overrides["workbench-evidence"] = Mock(
                            side_effect=lookup
                        )
                    else:
                        button.is_enabled.return_value = False
                    with self.assertRaises(
                        (ui.AppiumError, StaleElementReferenceException)
                    ):
                        self.exercise(driver, [])
                    self.assertEqual(
                        button.click.call_count, int(failure in {"uncertain", "late"})
                    )
                    driver.start.click.assert_not_called()
                    driver.close.click.assert_not_called()

    def test_startup_snapshot_accepts_all_enums_and_independent_authorizations(
        self,
    ) -> None:
        for platform in ("ios", "android"):
            for state in ("unknown", "unavailable", "poweredOff", "poweredOn"):
                for scan, connection in (
                    ("notRequired", "authorized"),
                    ("notDetermined", "denied"),
                    ("denied", "restricted"),
                    ("restricted", "notDetermined"),
                    ("authorized", "notRequired"),
                ):
                    with (
                        self.subTest(
                            platform=platform,
                            state=state,
                            scan=scan,
                            connection=connection,
                        ),
                        fast_clock(),
                    ):
                        driver = LifecycleDriver(platform)
                        for identifier, value in zip(
                            driver.snapshot_nodes,
                            (state, scan, connection),
                            strict=True,
                        ):
                            driver.snapshot_nodes[identifier].text = value
                        snapshot: dict[str, str] = {}
                        observations: list[str] = []
                        ui.exercise_lifecycle(
                            driver,
                            platform=platform,
                            app_id=driver.app_id,
                            timeout=2,
                            observations=observations,
                            startup_snapshot=snapshot,
                        )
                        self.assertEqual(
                            snapshot,
                            {
                                "state": state,
                                "scan_authorization": scan,
                                "connection_authorization": connection,
                            },
                        )
                        self.assertEqual(observations, ["idle", "started", "closed"])
                        driver.start.click.assert_called_once_with()
                        driver.close.click.assert_called_once_with()
                        self.assertNotIn("adapter-name", driver.find_calls)

    def test_snapshot_is_verified_without_collector_before_close(self) -> None:
        for platform in ("ios", "android"):
            with self.subTest(platform=platform), fast_clock():
                driver = LifecycleDriver(platform)
                seen: list[str] = []

                def snapshot_node(
                    identifier: str,
                    driver: LifecycleDriver = driver,
                    seen: list[str] = seen,
                ) -> Element:
                    self.assertEqual(driver.state, "started")
                    seen.append(identifier)
                    return driver.snapshot_nodes[identifier]

                for identifier in driver.snapshot_nodes:
                    driver.overrides[identifier] = Mock(
                        side_effect=lambda identifier=identifier: snapshot_node(
                            identifier
                        )
                    )

                def close(
                    driver: LifecycleDriver = driver, seen: list[str] = seen
                ) -> None:
                    self.assertEqual(seen, list(driver.snapshot_nodes) * 2)
                    driver.state = "closed"

                driver.close.click.side_effect = close
                self.exercise(driver, [])
                driver.close.click.assert_called_once_with()

    def test_invalid_hidden_missing_and_stale_snapshot_fields_refuse_close(
        self,
    ) -> None:
        for platform in ("ios", "android"):
            for identifier in (
                "adapter-state",
                "scan-authorization",
                "connection-authorization",
            ):
                for failure in ("invalid", "empty", "hidden", "missing", "stale"):
                    with (
                        self.subTest(
                            platform=platform, field=identifier, failure=failure
                        ),
                        fast_clock() as clock,
                    ):
                        driver = LifecycleDriver(platform)
                        node = driver.snapshot_nodes[identifier]
                        if failure == "invalid":
                            # A valid enum from the wrong field is still malformed.
                            node.text = (
                                "authorized"
                                if identifier == "adapter-state"
                                else "poweredOn"
                            )
                        elif failure == "empty":
                            node.text = ""
                        elif failure == "hidden":
                            node.is_displayed.return_value = False
                        else:
                            driver.overrides[identifier] = Mock(
                                side_effect=(
                                    NoSuchElementException(identifier)
                                    if failure == "missing"
                                    else StaleElementReferenceException(identifier)
                                )
                            )
                        observations: list[str] = []
                        with self.assertRaises(ui.AppiumError):
                            self.exercise(driver, observations)
                        self.assertEqual(observations, ["idle", "started"])
                        self.assertLessEqual(clock.now, 2.1)
                        driver.start.click.assert_called_once_with()
                        driver.close.click.assert_not_called()

    def test_snapshot_fields_refetch_missing_hidden_and_stale_nodes(self) -> None:
        for platform in ("ios", "android"):
            for identifier in (
                "adapter-state",
                "scan-authorization",
                "connection-authorization",
            ):
                with self.subTest(platform=platform, field=identifier), fast_clock():
                    driver = LifecycleDriver(platform)
                    node = driver.snapshot_nodes[identifier]
                    stale_display = Element(node.text)
                    stale_display.is_displayed.side_effect = (
                        StaleElementReferenceException()
                    )
                    stale_text = Mock()
                    stale_text.is_displayed.return_value = True
                    type(stale_text).text = unittest.mock.PropertyMock(
                        side_effect=StaleElementReferenceException()
                    )
                    driver.overrides[identifier] = Mock(
                        side_effect=[
                            NoSuchElementException(identifier),
                            Element(node.text, displayed=False),
                            StaleElementReferenceException(),
                            stale_display,
                            stale_text,
                            node,
                            node,
                        ]
                    )
                    self.exercise(driver, [])
                    self.assertEqual(driver.overrides[identifier].call_count, 7)
                    driver.start.click.assert_called_once_with()
                    driver.close.click.assert_called_once_with()

    def test_snapshot_lookup_rejects_late_response_and_foreign_foreground(self) -> None:
        for platform in ("ios", "android"):
            for failure in ("late", "foreign"):
                with (
                    self.subTest(platform=platform, failure=failure),
                    fast_clock() as clock,
                ):
                    driver = LifecycleDriver(platform)

                    def lookup(
                        driver: LifecycleDriver = driver,
                        clock: Clock = clock,
                        failure: str = failure,
                    ) -> Element:
                        if failure == "late":
                            clock.now += 3
                        else:
                            driver.foreground = "foreign.app"
                        return driver.snapshot_nodes["adapter-state"]

                    driver.overrides["adapter-state"] = Mock(side_effect=lookup)
                    with self.assertRaises(ui.AppiumError):
                        self.exercise(driver, [])
                    driver.start.click.assert_called_once_with()
                    driver.close.click.assert_not_called()

    def test_late_states_missing_hidden_and_stale_are_refetched(self) -> None:
        for platform in ("ios", "android"):
            with self.subTest(platform=platform), fast_clock():
                driver = LifecycleDriver(platform)
                early_titles = iter(
                    [
                        NoSuchElementException("tree not ready"),
                        Element("wrong hidden text", displayed=False),
                    ]
                )

                def title(
                    early_titles: Iterator[Element | Exception] = early_titles,
                    driver: LifecycleDriver = driver,
                ) -> Element:
                    value = next(early_titles, driver.title)
                    if isinstance(value, Exception):
                        raise value
                    return value

                driver.overrides["formal-host-title"] = Mock(side_effect=title)
                stale = Element()
                stale.is_displayed.side_effect = StaleElementReferenceException()
                early_states = iter([stale, Element("idle")])
                pending_states = {"started": "starting", "closed": "closing"}

                def state(
                    early_states: Iterator[Element] = early_states,
                    pending_states: dict[str, str] = pending_states,
                    driver: LifecycleDriver = driver,
                ) -> Element:
                    if driver.state == "idle":
                        return next(early_states, Element(driver.state))
                    return Element(pending_states.pop(driver.state, driver.state))

                driver.overrides["session-state"] = Mock(side_effect=state)
                observations: list[str] = []
                self.exercise(driver, observations)
                self.assertEqual(observations, ["idle", "started", "closed"])
                driver.start.click.assert_called_once_with()
                driver.close.click.assert_called_once_with()

    def test_failed_reports_visible_error_without_click_retry(self) -> None:
        for platform in ("ios", "android"):
            with self.subTest(platform=platform), fast_clock() as clock:
                driver = LifecycleDriver(platform)
                driver.start.click.side_effect = lambda driver=driver: setattr(
                    driver, "state", "failed"
                )
                driver.overrides["session-error"] = Mock(
                    return_value=Element("owner start rejected")
                )
                observations: list[str] = []
                with self.assertRaisesRegex(ui.AppiumError, "owner start rejected"):
                    self.exercise(driver, observations)
                self.assertLess(clock.now, 1)
                self.assertEqual(observations, ["idle"])
                driver.start.click.assert_called_once_with()
                driver.close.click.assert_not_called()

    def test_failed_error_can_refresh_and_hidden_error_never_counts(self) -> None:
        for platform in ("ios", "android"):
            for visible in (True, False):
                with self.subTest(platform=platform, visible=visible), fast_clock():
                    driver = LifecycleDriver(platform)
                    driver.state = "failed"
                    error = Element("session failure detail", displayed=visible)
                    driver.overrides["session-error"] = Mock(
                        side_effect=[
                            StaleElementReferenceException(),
                            error,
                            error,
                            error,
                            error,
                            error,
                            error,
                            error,
                            error,
                            error,
                            error,
                            error,
                        ]
                    )
                    expected = (
                        "session failure detail"
                        if visible
                        else "session-error.*unavailable"
                    )
                    with self.assertRaisesRegex(ui.AppiumError, expected):
                        self.exercise(driver, [])
                    driver.start.click.assert_not_called()

    def test_foreign_foreground_before_start_and_close_refuses_click(self) -> None:
        for platform in ("ios", "android"):
            for after_start in (False, True):
                with (
                    self.subTest(platform=platform, after_start=after_start),
                    fast_clock(),
                ):
                    driver = LifecycleDriver(platform)
                    if after_start:

                        def start(driver: LifecycleDriver = driver) -> None:
                            driver.state = "started"
                            driver.foreground = "foreign.app"

                        driver.start.click.side_effect = start
                    else:
                        driver.foreground = "foreign.app"
                    with self.assertRaises(ui.AppiumError):
                        self.exercise(driver, [])
                    self.assertEqual(driver.start.click.call_count, int(after_start))
                    driver.close.click.assert_not_called()

    def test_app_disappears_while_button_is_being_resolved(self) -> None:
        for platform in ("ios", "android"):
            with self.subTest(platform=platform), fast_clock():
                driver = LifecycleDriver(platform)

                def find_button(driver: LifecycleDriver = driver) -> Element:
                    driver.foreground = "foreign.app"
                    return driver.start

                driver.overrides["start-session"] = Mock(side_effect=find_button)
                with self.assertRaises(ui.AppiumError):
                    self.exercise(driver, [])
                driver.start.click.assert_not_called()

    def test_timeouts_never_append_unverified_phase_or_repeat_actions(self) -> None:
        for platform in ("ios", "android"):
            for boundary in (
                "title",
                "state",
                "hidden-state",
                "button",
                "started",
                "closed",
            ):
                with self.subTest(platform=platform, boundary=boundary), fast_clock():
                    driver = LifecycleDriver(platform)
                    if boundary == "title":
                        driver.title.is_displayed.return_value = False
                    elif boundary in ("state", "hidden-state"):
                        driver.overrides["session-state"] = Mock(
                            side_effect=NoSuchElementException()
                            if boundary == "state"
                            else lambda: Element("idle", displayed=False)
                        )
                    elif boundary == "button":
                        driver.start.is_enabled.return_value = False
                    elif boundary == "started":
                        driver.start.click.side_effect = lambda driver=driver: setattr(
                            driver, "state", "starting"
                        )
                    else:
                        driver.close.click.side_effect = lambda driver=driver: setattr(
                            driver, "state", "closing"
                        )
                    observations: list[str] = []
                    with self.assertRaisesRegex(ui.AppiumError, "[Tt]imeout|within"):
                        self.exercise(driver, observations)
                    self.assertNotIn("closed", observations)
                    self.assertLessEqual(driver.start.click.call_count, 1)
                    self.assertLessEqual(driver.close.click.call_count, 1)

    def test_ambiguous_click_failure_is_not_retried(self) -> None:
        for platform in ("ios", "android"):
            with self.subTest(platform=platform), fast_clock():
                driver = LifecycleDriver(platform)
                driver.start.click.side_effect = StaleElementReferenceException(
                    "click uncertain"
                )
                with self.assertRaises(StaleElementReferenceException):
                    self.exercise(driver, [])
                driver.start.click.assert_called_once_with()
                driver.close.click.assert_not_called()

    def test_late_click_response_is_not_counted_as_success(self) -> None:
        for platform in ("ios", "android"):
            with self.subTest(platform=platform), fast_clock() as clock:
                driver = LifecycleDriver(platform)

                def late_click(
                    driver: LifecycleDriver = driver, clock: Clock = clock
                ) -> None:
                    clock.now += 3
                    driver.state = "started"

                driver.start.click.side_effect = late_click
                observations: list[str] = []
                with self.assertRaisesRegex(ui.AppiumError, "Timeout"):
                    self.exercise(driver, observations)
                self.assertEqual(observations, ["idle"])
                driver.start.click.assert_called_once_with()
                driver.close.click.assert_not_called()

    def test_failed_error_lookup_deadline_preserves_failed_state(self) -> None:
        with fast_clock() as clock:
            driver = LifecycleDriver("ios")
            driver.state = "failed"

            def late_error() -> Element:
                clock.now += 2
                return Element("too late to verify")

            driver.overrides["session-error"] = Mock(side_effect=late_error)
            with self.assertRaisesRegex(
                ui.AppiumError, "session-state failed.*session-error.*unavailable"
            ):
                self.exercise(driver, [])
            driver.start.click.assert_not_called()

    def test_failed_without_error_text_still_reports_failed_state(self) -> None:
        for platform in ("ios", "android"):
            with self.subTest(platform=platform), fast_clock():
                driver = LifecycleDriver(platform)
                driver.state = "failed"
                with self.assertRaisesRegex(
                    ui.AppiumError, "session-state failed.*session-error.*unavailable"
                ):
                    self.exercise(driver, [])
                driver.start.click.assert_not_called()
                driver.close.click.assert_not_called()

    def test_late_response_after_deadline_is_not_passed(self) -> None:
        with fast_clock() as clock:
            driver = LifecycleDriver("ios")

            def late_title() -> Element:
                clock.now += 3
                return driver.title

            driver.overrides["formal-host-title"] = Mock(side_effect=late_title)
            with self.assertRaises(ui.AppiumError):
                self.exercise(driver, [])
            driver.start.click.assert_not_called()


class RunnerTests(unittest.TestCase):
    @contextmanager
    def boundary(self, platform: str, *, simulate_driver_contract: bool = True):
        with tempfile.TemporaryDirectory() as output, ExitStack() as stack:
            args = argparse.Namespace(
                platform=platform,
                device="mock-device-not-for-sdk",
                app_id="example.explicit.formal",
                activity=".MainActivity",
                team="MOCKTEAM",
                appium_entry=Path("mock-appium.js"),
                node="node",
                port=4723,
                wda_port=8101,
                wda_bundle_id="dev.blehub.appium.wda",
                tunnel_registry_port=42314,
                timeout=2,
                server_timeout=3,
                output=Path(output),
            )
            server_type = stack.enter_context(patch.object(ui, "AppiumServer"))
            ios = stack.enter_context(patch.object(ui, "create_ios_driver"))
            android = stack.enter_context(patch.object(ui, "create_android_driver"))
            if platform == "android" and simulate_driver_contract:
                # This fake-driver scenario is not an installed-driver approval.
                stack.enter_context(
                    patch.object(ui.android_appium, "require_prepared_driver_contract")
                )
            tunnel = stack.enter_context(patch.object(ui, "require_remotexpc_tunnel"))
            driver = LifecycleDriver(platform)
            ios.return_value = driver
            android.return_value = driver
            server_type.return_value.base_url = "http://mock-appium.invalid"
            stack.enter_context(fast_clock())
            yield args, driver, server_type, ios, android, tunnel

    def test_android_driver_admission_blocks_even_when_target_app_is_prepared(
        self,
    ) -> None:
        with self.boundary("android", simulate_driver_contract=False) as env:
            args, driver, server, ios, android, tunnel = env
            result = json.loads(ui.run(args).read_text())
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["stage"], "driver-admission")
            self.assertFalse(result["scenario_executed"])
            self.assertEqual(result["phases"], [])
            self.assertIn("prepared", result["error"])
            self.assertEqual(result["cleanup_errors"], [])
            server.assert_not_called()
            ios.assert_not_called()
            android.assert_not_called()
            tunnel.assert_not_called()
            driver.quit.assert_not_called()
            driver.start.click.assert_not_called()

    def test_run_attaches_without_launching_and_keeps_closed_page(self) -> None:
        for platform in ("ios", "android"):
            with self.subTest(platform=platform), self.boundary(platform) as env:
                args, driver, server, ios, android, tunnel = env
                driver.snapshot_nodes["adapter-state"].text = "poweredOff"
                driver.snapshot_nodes["scan-authorization"].text = "denied"
                path = ui.run(args)
                result = json.loads(path.read_text())
                self.assertEqual(path.name, "result.json")
                self.assertEqual(path.parent.parent, args.output)
                self.assertEqual(result["scope"], "formal-rn-lifecycle-ui")
                self.assertEqual(result["platform"], platform)
                self.assertEqual(result["device"], args.device)
                self.assertEqual(result["app_id"], args.app_id)
                self.assertEqual(result["session_id"], driver.session_id)
                self.assertEqual(result["phases"], ["idle", "started", "closed"])
                self.assertEqual(result["status"], "passed")
                self.assertEqual(driver.page, "Evidence")
                self.assertEqual(
                    result["startup_snapshot"],
                    {
                        "state": "poweredOff",
                        "scan_authorization": "denied",
                        "connection_authorization": "notRequired",
                    },
                )
                self.assertIsNone(result["error"])
                self.assertEqual(result["cleanup_errors"], [])
                factory = ios if platform == "ios" else android
                expected = {
                    "device": args.device,
                    "auto_launch": False,
                    "command_timeout": args.timeout,
                }
                if platform == "ios":
                    expected.update(
                        team=args.team,
                        bundle_id=args.app_id,
                        wda_bundle_id=args.wda_bundle_id,
                        wda_port=args.wda_port,
                        derived_data=path.parent / "wda-derived-data",
                    )
                    tunnel.assert_called_once_with(args.tunnel_registry_port)
                    android.assert_not_called()
                else:
                    expected.update(package=args.app_id, activity=args.activity)
                    tunnel.assert_not_called()
                    ios.assert_not_called()
                factory.assert_called_once_with(
                    server.return_value.base_url, **expected
                )
                driver.quit.assert_called_once_with()
                server.return_value.stop.assert_called_once_with()
                self.assertEqual(driver.state, "closed")
                self.assertFalse(
                    any(
                        identifier
                        in (
                            "scan-state",
                            "scan-count",
                            "scan-samples",
                            "scan-observation-timeout",
                            "start-scan",
                            "next-scan-event",
                            "stop-scan",
                        )
                        for identifier in driver.find_calls
                    ),
                    driver.find_calls,
                )

    def test_run_missing_evidence_fails_despite_closed_phase(self) -> None:
        for platform in ("ios", "android"):
            with self.subTest(platform=platform), self.boundary(platform) as env:
                args, driver, _, _, _, _ = env
                driver.evidence.text = "start: succeeded"
                result = json.loads(ui.run(args).read_text())
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["stage"], "scenario")
                self.assertEqual(result["phases"], ["idle", "started", "closed"])
                self.assertIn("lifecycle-evidence", result["error"])
                self.assertEqual(driver.page, "Evidence")
                driver.quit.assert_called_once_with()

    def test_run_snapshot_validation_failure_is_recorded_without_close(self) -> None:
        for platform in ("ios", "android"):
            with self.subTest(platform=platform), self.boundary(platform) as env:
                args, driver, _, _, _, _ = env
                driver.snapshot_nodes["connection-authorization"].text = "invalid"
                result = json.loads(ui.run(args).read_text())
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["stage"], "scenario")
                self.assertEqual(result["phases"], ["idle", "started"])
                self.assertEqual(result["startup_snapshot"], {})
                self.assertIn("connection-authorization", result["error"])
                driver.close.click.assert_not_called()
                driver.quit.assert_called_once_with()

    def test_initialization_errors_have_stage_and_no_scenario(self) -> None:
        for stage in ("tunnel", "server", "driver", "foreground"):
            with self.subTest(stage=stage), self.boundary("ios") as env:
                args, driver, server, ios, _, tunnel = env
                if stage == "tunnel":
                    tunnel.side_effect = RuntimeError("tunnel unavailable")
                elif stage == "server":
                    server.return_value.start.side_effect = RuntimeError(
                        "server unavailable"
                    )
                elif stage == "driver":
                    ios.side_effect = RuntimeError("driver unavailable")
                else:
                    driver.foreground = "wrong.app"
                result = json.loads(ui.run(args).read_text())
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["stage"], stage)
                self.assertEqual(result["phases"], [])
                self.assertFalse(result["scenario_executed"])
                self.assertTrue(result["error"])
                driver.start.click.assert_not_called()
                if stage != "tunnel":
                    server.return_value.stop.assert_called_once_with()
                if stage == "foreground":
                    driver.quit.assert_called_once_with()

    def test_primary_cleanup_and_diagnostic_errors_are_preserved_separately(
        self,
    ) -> None:
        for platform in ("ios", "android"):
            with self.subTest(platform=platform), self.boundary(platform) as env:
                args, driver, server, _, _, _ = env
                driver.start.click.side_effect = WebDriverException(
                    "primary click failure"
                )
                driver.quit.side_effect = WebDriverException("quit failure")
                server.return_value.stop.side_effect = RuntimeError("stop failure")
                driver.save_screenshot.side_effect = RuntimeError("screenshot failure")
                result = json.loads(ui.run(args).read_text())
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["stage"], "scenario")
                self.assertIn("primary click failure", result["error"])
                self.assertEqual(len(result["cleanup_errors"]), 2)
                self.assertIn("quit failure", result["cleanup_errors"][0])
                self.assertIn("stop failure", result["cleanup_errors"][1])
                self.assertIn("screenshot failure", str(result["diagnostic_errors"]))
                self.assertEqual(result["phases"], ["idle"])
                driver.quit.assert_called_once_with()
                server.return_value.stop.assert_called_once_with()

    def test_cleanup_failure_turns_success_into_failure(self) -> None:
        for owner in ("driver", "server"):
            with self.subTest(owner=owner), self.boundary("android") as env:
                args, driver, server, _, _, _ = env
                cleanup = driver.quit if owner == "driver" else server.return_value.stop
                cleanup.side_effect = RuntimeError("cleanup failed")
                result = json.loads(ui.run(args).read_text())
                self.assertEqual(result["status"], "failed")
                self.assertIsNone(result["error"])
                self.assertEqual(len(result["cleanup_errors"]), 1)
                self.assertEqual(result["phases"], ["idle", "started", "closed"])
                server.return_value.stop.assert_called_once_with()

    def test_source_dump_failure_does_not_replace_primary_error(self) -> None:
        with self.boundary("android") as env:
            args, driver, _, _, _, _ = env
            with (
                patch.object(
                    ui, "exercise_lifecycle", side_effect=RuntimeError("primary")
                ),
                patch.object(
                    type(driver),
                    "page_source",
                    new_callable=unittest.mock.PropertyMock,
                    side_effect=RuntimeError("source unavailable"),
                ),
            ):
                result = json.loads(ui.run(args).read_text())
            self.assertIn("primary", result["error"])
            self.assertIn("source unavailable", str(result["diagnostic_errors"]))

    def test_main_exit_status_tracks_result_including_cleanup_failure(self) -> None:
        for failure in (False, True):
            with self.subTest(failure=failure), self.boundary("android") as env:
                args, driver, _, _, _, _ = env
                if failure:
                    driver.quit.side_effect = RuntimeError("quit failed")
                code = ui.main(
                    [
                        "--platform",
                        "android",
                        "--device",
                        args.device,
                        "--app-id",
                        args.app_id,
                        "--activity",
                        args.activity,
                        "--appium-entry",
                        str(args.appium_entry),
                        "--output",
                        str(args.output),
                    ]
                )
                self.assertEqual(code, 2 if failure else 0)

    def test_validation_precedes_all_live_boundaries(self) -> None:
        base = [
            "--platform",
            "android",
            "--device",
            "mock-device",
            "--app-id",
            "explicit.id",
            "--appium-entry",
            "mock.js",
            "--activity",
            ".MainActivity",
        ]
        invalid = (
            [["--timeout", value] for value in ("0", "-1", "nan", "inf", "301")]
            + [["--server-timeout", value] for value in ("0", "nan", "inf", "301")]
            + [
                ["--device", " "],
                ["--app-id", ""],
                ["--activity", ""],
                ["--platform", "ios"],
                ["--port", "0"],
                ["--wda-port", "65536"],
                ["--tunnel-registry-port", "-1"],
                ["--scenario", "scan"],
            ]
        )
        for extra in invalid:
            with self.subTest(extra=extra), self.boundary("android") as env:
                _, _, server, ios, android, tunnel = env
                with patch("sys.stderr"), self.assertRaises(SystemExit) as caught:
                    ui.main(base + extra)
                self.assertEqual(caught.exception.code, 2)
                server.assert_not_called()
                ios.assert_not_called()
                android.assert_not_called()
                tunnel.assert_not_called()


if __name__ == "__main__":
    unittest.main()
