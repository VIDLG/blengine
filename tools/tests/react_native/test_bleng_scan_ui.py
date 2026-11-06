"""Hardware-free regressions for the host-injected formal RN scan UI flow."""

from __future__ import annotations

import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    WebDriverException,
)

from tools.react_native import run_bleng_ui as ui
from tools.tests.react_native.support.bleng_ui import (
    LOCAL_NAME,
    SERVICE_UUID,
    Element,
    ScanDriver,
    advertisement,
    fast_clock,
)


class ScanTests(unittest.TestCase):
    def exercise(
        self, driver: ScanDriver, observations: list[str], **overrides
    ) -> None:
        options = {
            "platform": driver.platform,
            "app_id": driver.app_id,
            "timeout": 20,
            "observations": observations,
            "expected_local_name": LOCAL_NAME,
            "expected_service_uuid": SERVICE_UUID,
            "require_authority": driver.require_authority,
        }
        options.update(overrides)
        ui.exercise_scan(driver, **options)

    def test_identity_checks_are_bounded_by_actions_not_remote_reads(self) -> None:
        for platform in ("ios", "android"):
            for pulls in (1, 3):
                for deferred in (False, True):
                    with (
                        self.subTest(platform=platform, pulls=pulls, deferred=deferred),
                        fast_clock(),
                    ):
                        driver = ScanDriver(
                            platform,
                            [advertisement("other", ())] * (pulls - 1)
                            + [advertisement()],
                        )
                        driver.defer_pull_until_count_read = deferred
                        checkpoints: list[tuple[str | None, str, str, int]] = []

                        def identity(driver=driver, checkpoints=checkpoints) -> None:
                            remote = [
                                event
                                for event in driver.trace
                                if event not in {"guard", "identity"}
                            ]
                            checkpoints.append(
                                (
                                    remote[-1] if remote else None,
                                    driver.state,
                                    driver.scan_state,
                                    driver.total,
                                )
                            )
                            driver.trace.append("identity")

                        self.exercise(driver, [], require_app_identity=identity)
                        self.assert_guarded(driver)
                        for index, event in enumerate(driver.trace):
                            if event != "guard":
                                self.assertEqual(
                                    driver.trace[index + 1], "guard", event
                                )
                        events = [event for event in driver.trace if event != "guard"]
                        click_indices = [
                            index
                            for index, event in enumerate(events)
                            if event.startswith("click:")
                        ]
                        click_checks = set()
                        for index in click_indices:
                            self.assertEqual(events[index - 1], "identity")
                            self.assertEqual(events[index + 1], "identity")
                            click_checks.update((index - 1, index + 1))
                        remaining = [
                            checkpoint
                            for index, checkpoint in zip(
                                (
                                    index
                                    for index, event in enumerate(events)
                                    if event == "identity"
                                ),
                                checkpoints,
                                strict=True,
                            )
                            if index not in click_checks
                        ]
                        self.assertEqual(
                            remaining,
                            [
                                (None, "idle", "idle", 0),
                                ("text:session-state", "started", "idle", 0),
                                ("text:scan-state", "started", "running", 0),
                                *[
                                    ("text:scan-count", "started", "running", count)
                                    for count in range(1, pulls + 1)
                                ],
                                ("text:scan-state", "started", "stopped", pulls),
                                ("text:session-state", "closed", "ownerClosed", pulls),
                                (
                                    "text:evidence-connection-authorization",
                                    "closed",
                                    "ownerClosed",
                                    pulls,
                                ),
                            ],
                        )
                        self.assertEqual(
                            len(checkpoints), 2 * len(click_indices) + 6 + pulls
                        )
                        self.assertGreater(
                            driver.trace.count("guard"), 10 * len(checkpoints)
                        )
                        self.assertEqual(events[0], "identity")
                        self.assertEqual(events[-1], "identity")
                        self.assertEqual(driver.scan_next.click.call_count, pulls)

    def test_identity_failure_before_or_after_click_never_retries(self) -> None:
        for platform in ("ios", "android"):
            for identifier in (
                "workbench-evidence",
                "start-session",
                "start-scan",
                "next-scan-event",
                "stop-scan",
                "close-session",
            ):
                for boundary in ("enabled", "click"):
                    with (
                        self.subTest(
                            platform=platform, identifier=identifier, boundary=boundary
                        ),
                        fast_clock(),
                    ):
                        driver = ScanDriver(platform)
                        failure = ui.AppiumError("fresh identity lost")
                        revoked: list[str] = []

                        def after_action(
                            action: str,
                            boundary=boundary,
                            identifier=identifier,
                            revoked=revoked,
                        ) -> None:
                            if action == f"{boundary}:{identifier}":
                                revoked.append(action)

                        def identity(
                            driver=driver, revoked=revoked, failure=failure
                        ) -> None:
                            driver.trace.append("identity")
                            if revoked:
                                raise failure

                        driver.after_action = after_action
                        with self.assertRaises(ui.AppiumError) as caught:
                            self.exercise(driver, [], require_app_identity=identity)
                        self.assertIs(caught.exception, failure)
                        self.assertEqual(
                            driver.nodes[identifier].click.call_count,
                            int(boundary == "click"),
                        )
                        self.assertEqual(driver.trace[-1], "identity")
                        self.assert_no_input_changes(driver)

    def test_identity_loss_at_scene_and_completion_checks_aborts(self) -> None:
        for platform in ("ios", "android"):
            with fast_clock():
                baseline = ScanDriver(platform)
                self.exercise(
                    baseline,
                    [],
                    require_app_identity=lambda baseline=baseline: (
                        baseline.trace.append("identity")
                    ),
                )
            events = [event for event in baseline.trace if event != "guard"]
            for index, event in enumerate(events):
                if event != "identity" or (
                    (index > 0 and events[index - 1].startswith("click:"))
                    or (
                        index + 1 < len(events)
                        and events[index + 1].startswith("click:")
                    )
                ):
                    continue
                with self.subTest(platform=platform, checkpoint=index), fast_clock():
                    driver = ScanDriver(platform)
                    driver.defer_pull_until_count_read = True
                    failure = ui.AppiumError("completion identity lost")
                    target = events[: index + 1].count("identity")

                    def identity(driver=driver, target=target, failure=failure) -> None:
                        driver.trace.append("identity")
                        if driver.trace.count("identity") == target:
                            raise failure

                    with self.assertRaises(ui.AppiumError) as caught:
                        self.exercise(driver, [], require_app_identity=identity)
                    self.assertIs(caught.exception, failure)
                    self.assertEqual(driver.trace.count("identity"), target)
                    self.assertEqual(driver.trace[-1], "identity")
                    self.assertEqual(
                        [event for event in driver.trace if event.startswith("click:")],
                        [
                            event
                            for event in events[:index]
                            if event.startswith("click:")
                        ],
                    )
                    self.assert_no_input_changes(driver)

    def test_omitted_identity_callback_uses_authority_at_checkpoints(self) -> None:
        for platform in ("ios", "android"):
            with self.subTest(platform=platform), fast_clock():
                explicit = ScanDriver(platform)
                self.exercise(
                    explicit, [], require_app_identity=explicit.require_authority
                )
                fallback = ScanDriver(platform)
                self.exercise(fallback, [])
                self.assertEqual(fallback.trace, explicit.trace)

    def test_identity_callback_is_validated_before_remote_work(self) -> None:
        for invalid in (False, 0, "identity", object()):
            with self.subTest(invalid=invalid), fast_clock():
                driver = ScanDriver("android")
                with self.assertRaisesRegex(TypeError, "require_app_identity"):
                    self.exercise(driver, [], require_app_identity=invalid)
                self.assertEqual(driver.trace, [])

    def test_identity_check_cannot_outlive_authority_or_deadline_before_click(
        self,
    ) -> None:
        for loss in ("authority", "deadline"):
            with self.subTest(loss=loss), fast_clock() as clock:
                driver = ScanDriver("android")

                def identity(driver=driver, clock=clock, loss=loss) -> None:
                    if "enabled:start-scan" not in driver.trace:
                        return
                    if loss == "authority":
                        driver.authorized = False
                    else:
                        clock.now += 20

                with self.assertRaises(ui.AppiumError) as caught:
                    self.exercise(driver, [], require_app_identity=identity)
                if loss == "authority":
                    self.assertIs(caught.exception, driver.guard_error)
                else:
                    self.assertRegex(str(caught.exception), "Timeout")
                driver.scan_start.click.assert_not_called()
                self.assert_no_cleanup(driver)

    def test_scan_success_on_both_platforms(self) -> None:
        for platform in ("ios", "android"):
            with self.subTest(platform=platform), fast_clock():
                driver = ScanDriver(platform)
                observations: list[str] = []
                snapshot: dict[str, str] = {}
                with ExitStack() as stack:
                    boundaries = [
                        stack.enter_context(patch.object(ui, name))
                        for name in (
                            "create_ios_driver",
                            "create_android_driver",
                            "AppiumServer",
                            "require_remotexpc_tunnel",
                        )
                    ]
                    self.exercise(driver, observations, startup_snapshot=snapshot)
                    for boundary in boundaries:
                        boundary.assert_not_called()
                self.assertEqual(
                    observations,
                    [
                        "idle",
                        "started",
                        "scan:running",
                        "scan:advertisement-matched",
                        "scan:stopped",
                        "closed",
                    ],
                )
                self.assertEqual(
                    snapshot,
                    {
                        "state": "poweredOn",
                        "scan_authorization": "authorized",
                        "connection_authorization": "notRequired",
                    },
                )
                driver.start.click.assert_called_once_with()
                driver.scan_start.click.assert_called_once_with()
                driver.scan_next.click.assert_called_once_with()
                driver.scan_stop.click.assert_called_once_with()
                driver.close.click.assert_called_once_with()
                self.assertEqual(driver.page, "Evidence")
                self.assertEqual(
                    [action for action, _, _ in driver.actions],
                    [
                        "start",
                        "begin-scan",
                        "scan-start-wait",
                        "scan-pull",
                        "scan-stop",
                        "close",
                    ],
                )
                self.assert_guarded(driver)
                self.assert_no_input_changes(driver)
                # Lifecycle browsing occurs before scan; scan browsing must also
                # preserve a populated sample/count, not merely an empty run.
                for page in ("Tests", "Evidence", "Current run"):
                    self.assertIn(
                        (page, "running", 1, (advertisement(),)), driver.scan_visits
                    )
                self.assertIn(("idle", "Evidence"), driver.visits)
                self.assertIn(("started", "Tests"), driver.visits)

    def test_async_pull_settles_between_count_and_samples_without_another_pull(
        self,
    ) -> None:
        for platform in ("ios", "android"):
            with self.subTest(platform=platform), fast_clock() as clock:
                driver = ScanDriver(platform)
                driver.defer_pull_until_count_read = True
                observations: list[str] = []
                self.exercise(driver, observations, max_pulls=1)
                self.assertEqual(
                    observations,
                    [
                        "idle",
                        "started",
                        "scan:running",
                        "scan:advertisement-matched",
                        "scan:stopped",
                        "closed",
                    ],
                )
                self.assertIn(0, driver.count_reads)
                self.assertIn(1, driver.count_reads)
                self.assertIsNone(driver.pending_pull)
                self.assertEqual(driver.samples, [advertisement()])
                self.assertEqual(
                    driver.actions,
                    [
                        ("start", "succeeded", ""),
                        ("begin-scan", "succeeded", ""),
                        ("scan-start-wait", "succeeded", ""),
                        ("scan-pull", "succeeded", ""),
                        ("scan-stop", "succeeded", ""),
                        ("close", "succeeded", ""),
                    ],
                )
                driver.start.click.assert_called_once_with()
                driver.scan_start.click.assert_called_once_with()
                driver.scan_next.click.assert_called_once_with()
                driver.scan_stop.click.assert_called_once_with()
                driver.close.click.assert_called_once_with()
                self.assertLess(clock.now, 20)
                self.assert_guarded(driver)
                self.assert_no_input_changes(driver)

    def test_unstable_summary_retries_reads_until_total_deadline_not_another_pull(
        self,
    ) -> None:
        for platform in ("ios", "android"):
            with self.subTest(platform=platform), fast_clock() as clock:
                driver = ScanDriver(platform)
                count_text = driver.nodes["scan-count"].value
                changing_reads: list[int] = []

                def changing_count(
                    driver=driver,
                    count_text=count_text,
                    changing_reads=changing_reads,
                    clock=clock,
                ) -> str:
                    value = count_text()
                    if driver.scan_next.click.call_count:
                        changing_reads.append(driver.total)
                        clock.now += 0.1
                        driver.total += 1
                        driver.samples = [advertisement()] * min(driver.total, 8)
                    return value

                driver.nodes["scan-count"].value = changing_count
                observations: list[str] = []
                with self.assertRaisesRegex(ui.AppiumError, "[Tt]imeout"):
                    self.exercise(driver, observations, timeout=10, max_pulls=1)
                self.assertGreater(len(changing_reads), 2)
                self.assertGreaterEqual(clock.now, 10)
                self.assertLessEqual(clock.now, 10.2)
                self.assertEqual(observations, ["idle", "started", "scan:running"])
                driver.scan_next.click.assert_called_once_with()
                self.assert_no_cleanup(driver)
                self.assert_guarded(driver)

    def test_stable_corrupt_summary_is_rejected_not_retried_until_deadline(
        self,
    ) -> None:
        for case in (
            "missing-sample",
            "count-sample-disagreement",
            "wrong-omitted-count",
        ):
            with self.subTest(case=case), fast_clock() as clock:
                driver = ScanDriver("android")
                original = driver.scan_next.click.side_effect

                def corrupt(original=original, driver=driver, case=case) -> None:
                    original()
                    if case == "missing-sample":
                        driver.samples = []
                    elif case == "count-sample-disagreement":
                        driver.total = 2
                    else:
                        driver.total = 9
                        driver.samples = [advertisement()] * 8
                        driver.nodes["scan-count"].value = lambda: (
                            "9 advertisements observed; 0 samples omitted (latest 8 only)."
                        )

                driver.scan_next.click.side_effect = corrupt
                with self.assertRaisesRegex(ui.AppiumError, "summary|disagree"):
                    self.exercise(driver, [], max_pulls=1)
                self.assertLess(clock.now, 1)
                driver.scan_next.click.assert_called_once_with()
                self.assert_no_cleanup(driver)

    def test_final_evidence_cannot_lose_or_replace_scan_summary(self) -> None:
        for platform in ("ios", "android"):
            for case in (
                "cleared",
                "replaced-first",
                "replaced-last",
                "changed-count",
                "running",
                "stopped",
                "idle",
            ):
                with self.subTest(platform=platform, case=case), fast_clock():
                    driver = ScanDriver(
                        platform, [advertisement("other", ()), advertisement()]
                    )
                    original = driver.close.click.side_effect

                    def close(original=original, driver=driver, case=case) -> None:
                        original()
                        if case == "cleared":
                            driver.total = 0
                            driver.samples = []
                        elif case == "replaced-first":
                            driver.samples[0] = advertisement("replacement", ())
                        elif case == "replaced-last":
                            # Still matches the target, but is not the same evidence.
                            driver.samples[-1] = driver.samples[-1].replace(
                                "rssi=-48", "rssi=-49"
                            )
                        elif case == "changed-count":
                            driver.total += 1
                            driver.samples.append(advertisement("extra", ()))
                        else:
                            driver.scan_state = case

                    driver.close.click.side_effect = close
                    with self.assertRaises(ui.AppiumError):
                        self.exercise(driver, [], max_pulls=2)
                    self.assertEqual(driver.state, "closed")
                    self.assertEqual(driver.page, "Evidence")
                    driver.start.click.assert_called_once_with()
                    driver.scan_start.click.assert_called_once_with()
                    self.assertEqual(driver.scan_next.click.call_count, 2)
                    driver.scan_stop.click.assert_called_once_with()
                    driver.close.click.assert_called_once_with()
                    self.assert_no_input_changes(driver)

    def test_final_evidence_snapshot_must_equal_original_startup_snapshot(self) -> None:
        for platform in ("ios", "android"):
            for field, changed in (
                ("adapter-state", "poweredOff"),
                ("scan-authorization", "denied"),
                ("connection-authorization", "authorized"),
            ):
                for failure in ("changed", "missing", "hidden"):
                    with (
                        self.subTest(platform=platform, field=field, failure=failure),
                        fast_clock(),
                    ):
                        driver = ScanDriver(platform)
                        original = driver.close.click.side_effect

                        def close(
                            original=original,
                            driver=driver,
                            field=field,
                            changed=changed,
                            failure=failure,
                        ) -> None:
                            original()
                            if failure == "changed":
                                driver.evidence_snapshot[field] = changed
                            else:
                                identifier = f"evidence-{field}"
                                driver.overrides[identifier] = (
                                    Mock(side_effect=NoSuchElementException(identifier))
                                    if failure == "missing"
                                    else Mock(
                                        return_value=Element(
                                            driver.evidence_snapshot[field],
                                            displayed=False,
                                        )
                                    )
                                )

                        driver.close.click.side_effect = close
                        snapshot: dict[str, str] = {}
                        with self.assertRaises(ui.AppiumError):
                            self.exercise(driver, [], startup_snapshot=snapshot)
                        self.assertEqual(
                            snapshot,
                            {
                                "state": "poweredOn",
                                "scan_authorization": "authorized",
                                "connection_authorization": "notRequired",
                            },
                        )
                        self.assertEqual(driver.page, "Evidence")
                        driver.scan_stop.click.assert_called_once_with()
                        driver.close.click.assert_called_once_with()
                        self.assert_no_input_changes(driver)

    def test_evidence_tree_shares_readonly_summary_and_retains_prefixed_snapshot(
        self,
    ) -> None:
        driver = ScanDriver("ios")
        driver.start_session()
        driver.start_scan()
        driver.pull_scan()
        for page in ("Current run", "Evidence"):
            driver.navigate(page)
            for identifier in ("scan-state", "scan-count", "scan-samples"):
                self.assertIs(
                    driver.find_element("accessibility id", identifier),
                    driver.nodes[identifier],
                )
            for identifier in driver.snapshot_nodes:
                visible = (
                    identifier if page == "Current run" else f"evidence-{identifier}"
                )
                absent = (
                    f"evidence-{identifier}" if page == "Current run" else identifier
                )
                self.assertIs(
                    driver.find_element("accessibility id", visible),
                    driver.nodes[visible],
                )
                with self.assertRaises(NoSuchElementException):
                    driver.find_element("accessibility id", absent)
        driver.close_session()
        for identifier in driver.snapshot_nodes:
            node = driver.find_element("accessibility id", f"evidence-{identifier}")
            self.assertEqual(node.text, driver.evidence_snapshot[identifier])
        self.assertEqual(
            driver.find_element("accessibility id", "scan-state").text, "ownerClosed"
        )
        self.assertEqual(
            driver.find_element("accessibility id", "scan-samples").text,
            advertisement(),
        )
        for identifier in (
            "scan-observation-timeout",
            "start-scan",
            "next-scan-event",
            "stop-scan",
            "close-session",
        ):
            with self.assertRaises(NoSuchElementException):
                driver.find_element("accessibility id", identifier)

    def test_eighth_advertised_service_matches_before_exact_omitted_suffix(
        self,
    ) -> None:
        others = tuple(
            f"000000{index:02x}-0000-1000-8000-00805f9b34fb" for index in range(1, 9)
        )
        sample = advertisement(services=(*others[:7], SERVICE_UUID, others[7]))
        self.assertEqual(
            sample.splitlines()[1],
            f"services={', '.join((*others[:7], SERVICE_UUID))} (+1 omitted)",
        )
        for platform in ("ios", "android"):
            with self.subTest(platform=platform), fast_clock():
                driver = ScanDriver(platform, [sample])
                observations: list[str] = []
                self.exercise(driver, observations, max_pulls=1)
                self.assertIn("scan:advertisement-matched", observations)
                driver.scan_next.click.assert_called_once_with()
                driver.scan_stop.click.assert_called_once_with()
                driver.close.click.assert_called_once_with()
                self.assert_guarded(driver)

    def test_omitted_ninth_service_is_not_guessed_from_count_or_other_fields(
        self,
    ) -> None:
        others = tuple(
            f"000000{index:02x}-0000-1000-8000-00805f9b34fb" for index in range(1, 9)
        )
        sample = advertisement(services=(*others, SERVICE_UUID))
        self.assertNotIn(SERVICE_UUID, sample.splitlines()[1])
        self.assertTrue(sample.splitlines()[1].endswith(" (+1 omitted)"))
        for extra_field in ("manufacturer", "service data"):
            with self.subTest(extra_field=extra_field), fast_clock():
                driver = ScanDriver(
                    "android",
                    [
                        sample.replace(
                            f"{extra_field}=none",
                            f"{extra_field}=service={SERVICE_UUID}: 0 bytes []",
                        )
                    ],
                )
                observations: list[str] = []
                with self.assertRaises(ui.AppiumError):
                    self.exercise(driver, observations, max_pulls=1)
                self.assertNotIn("scan:advertisement-matched", observations)
                driver.scan_next.click.assert_called_once_with()
                self.assert_no_cleanup(driver)

    def test_malformed_service_omission_suffix_never_matches_any_visible_item(
        self,
    ) -> None:
        others = tuple(
            f"000000{index:02x}-0000-1000-8000-00805f9b34fb" for index in range(1, 9)
        )
        for target_index in (0, 7):
            services = list(others)
            services.insert(target_index, SERVICE_UUID)
            valid = advertisement(services=tuple(services))
            for suffix in (
                " (+0 omitted)",
                " (+01 omitted)",
                " (+-1 omitted)",
                " (+x omitted)",
                " (+1 omitted) trailing",
                " (+1 omitted",
                "(+1 omitted)",
                " (+1 omitted) (+1 omitted)",
            ):
                with (
                    self.subTest(target_index=target_index, suffix=suffix),
                    fast_clock(),
                ):
                    driver = ScanDriver("ios", [valid.replace(" (+1 omitted)", suffix)])
                    observations: list[str] = []
                    with self.assertRaises(ui.AppiumError):
                        self.exercise(driver, observations, max_pulls=1)
                    self.assertNotIn("scan:advertisement-matched", observations)
                    driver.scan_next.click.assert_called_once_with()
                    self.assert_no_cleanup(driver)

    def assert_guarded(self, driver: ScanDriver) -> None:
        for index, action in enumerate(driver.trace):
            if action != "guard":
                self.assertGreater(index, 0, action)
                self.assertEqual(
                    driver.trace[index - 1],
                    "guard",
                    driver.trace[max(0, index - 3) : index + 1],
                )

    def assert_no_input_changes(self, driver: ScanDriver) -> None:
        for node in driver.nodes.values():
            node.clear.assert_not_called()
            node.send_keys.assert_not_called()
        driver.quit.assert_not_called()
        driver.save_screenshot.assert_not_called()

    def assert_no_cleanup(self, driver: ScanDriver) -> None:
        driver.scan_stop.click.assert_not_called()
        driver.close.click.assert_not_called()
        self.assertLessEqual(driver.start.click.call_count, 1)
        self.assertLessEqual(driver.scan_start.click.call_count, 1)
        self.assert_no_input_changes(driver)

    def test_match_on_last_allowed_pull_and_stop_immediately(self) -> None:
        for platform in ("ios", "android"):
            for count in (1, 3, 8):
                with self.subTest(platform=platform, count=count), fast_clock():
                    driver = ScanDriver(
                        platform,
                        [advertisement("other", ())] * (count - 1) + [advertisement()],
                    )
                    observations: list[str] = []
                    self.exercise(driver, observations, max_pulls=count)
                    self.assertEqual(driver.scan_next.click.call_count, count)
                    self.assertEqual(driver.total, count)
                    self.assertLessEqual(len(driver.samples), 8)
                    self.assertEqual(
                        observations[-3:],
                        ["scan:advertisement-matched", "scan:stopped", "closed"],
                    )
                    driver.scan_start.click.assert_called_once_with()
                    driver.scan_stop.click.assert_called_once_with()
                    driver.close.click.assert_called_once_with()
                    self.assertEqual(
                        [action for action, _, _ in driver.actions],
                        [
                            "start",
                            "begin-scan",
                            "scan-start-wait",
                            *(["scan-pull"] * count),
                            "scan-stop",
                            "close",
                        ],
                    )
                    self.assert_guarded(driver)

    def test_no_match_consumes_exact_pull_budget_without_cleanup(self) -> None:
        for platform in ("ios", "android"):
            for count in (1, 3, 8):
                with (
                    self.subTest(platform=platform, count=count),
                    fast_clock() as clock,
                ):
                    driver = ScanDriver(platform, [])
                    observations: list[str] = []
                    with self.assertRaises(ui.AppiumError):
                        self.exercise(driver, observations, max_pulls=count)
                    self.assertEqual(driver.scan_next.click.call_count, count)
                    self.assertEqual(observations, ["idle", "started", "scan:running"])
                    self.assertLessEqual(clock.now, 20.1)
                    self.assert_no_cleanup(driver)

    def test_default_pull_budget_is_eight_not_an_unbounded_wait(self) -> None:
        with fast_clock() as clock:
            driver = ScanDriver("android", [])
            with self.assertRaises(ui.AppiumError):
                self.exercise(driver, [])
            self.assertEqual(driver.scan_next.click.call_count, 8)
            self.assertLessEqual(clock.now, 20.1)
            self.assert_no_cleanup(driver)

    def test_name_and_service_must_match_same_new_advertisement(self) -> None:
        samples = {
            "different-pulls": [advertisement(LOCAL_NAME, ()), advertisement("other")],
            "service-data-only": [
                advertisement(LOCAL_NAME, ()).replace(
                    "service data=none",
                    f"service data=service={SERVICE_UUID}: 0 bytes []",
                )
            ],
            "name-prefix": [advertisement(LOCAL_NAME + " extra")],
            "service-prefix": [advertisement(services=(SERVICE_UUID + "0",))],
            "service-in-name": [advertisement(LOCAL_NAME + SERVICE_UUID, ())],
        }
        for case, pulls in samples.items():
            with self.subTest(case=case), fast_clock():
                driver = ScanDriver("android", pulls)
                observations: list[str] = []
                with self.assertRaises(ui.AppiumError):
                    self.exercise(driver, observations, max_pulls=len(pulls))
                self.assertNotIn("scan:advertisement-matched", observations)
                self.assertEqual(driver.scan_next.click.call_count, len(pulls))
                self.assert_no_cleanup(driver)

    def test_json_escaped_name_and_multiple_advertised_services_match(self) -> None:
        name = 'DK "quoted"; services=not-a-field \\ 名称'
        with fast_clock():
            driver = ScanDriver(
                "ios",
                [
                    advertisement(
                        name, ("0000180d-0000-1000-8000-00805f9b34fb", SERVICE_UUID)
                    )
                ],
            )
            observations: list[str] = []
            self.exercise(driver, observations, expected_local_name=name)
            self.assertIn("scan:advertisement-matched", observations)

    def test_old_matching_sample_cannot_satisfy_new_pull(self) -> None:
        with fast_clock():
            driver = ScanDriver("ios", [advertisement("other", ())] * 2)
            original = driver.scan_start.click.side_effect

            def start() -> None:
                original()
                # A previously visible advertisement is not evidence from the
                # host's next pull, even if it has the desired name and service.
                driver.total = 1
                driver.samples = [advertisement()]

            driver.scan_start.click.side_effect = start
            observations: list[str] = []
            with self.assertRaises(ui.AppiumError):
                self.exercise(driver, observations, max_pulls=1)
            self.assertNotIn("scan:advertisement-matched", observations)
            self.assert_no_cleanup(driver)

    def test_each_pull_requires_count_exactly_plus_one_and_bounded_samples(
        self,
    ) -> None:
        for case in (
            "unchanged",
            "jump",
            "decrease",
            "over-eight",
            "no-sample",
            "malformed-count",
        ):
            with self.subTest(case=case), fast_clock() as clock:
                driver = ScanDriver("android")
                original = driver.pull_scan

                def pull(case=case, driver=driver, original=original) -> None:
                    original()
                    if case == "unchanged":
                        driver.total = 0
                    elif case == "jump":
                        driver.total = 2
                    elif case == "decrease":
                        driver.total = -1
                    elif case == "over-eight":
                        driver.samples = [advertisement()] * 9
                    elif case == "no-sample":
                        driver.samples = []
                    else:
                        driver.nodes["scan-count"].value = lambda: "old count: 1?"

                driver.scan_next.click.side_effect = pull
                observations: list[str] = []
                with self.assertRaises(ui.AppiumError):
                    self.exercise(driver, observations, max_pulls=2)
                self.assertNotIn("scan:advertisement-matched", observations)
                driver.scan_next.click.assert_called_once_with()
                self.assertLessEqual(clock.now, 20.1)
                self.assert_no_cleanup(driver)

    def test_validation_happens_before_any_ui_action(self) -> None:
        cases = (
            [
                {"timeout": value}
                for value in (0, -1, float("nan"), float("inf"), 300.01)
            ]
            + [{"max_pulls": value} for value in (0, 9, -1, 1.5, True)]
            + [{"observation_timeout_ms": value} for value in (0, -1, 60001, 1.5, True)]
            + [
                {"observation_timeout_ms": 20000},
                {"observation_timeout_ms": 20001},
                {"expected_local_name": ""},
                {"expected_local_name": "   "},
                {"expected_local_name": "n" * 161},
                {"expected_local_name": "🦀" * 81},
                {"expected_service_uuid": "180d"},
                {"expected_service_uuid": SERVICE_UUID.replace("-", "")},
                {"expected_service_uuid": "z" * 36},
                {"expected_service_uuid": " " + SERVICE_UUID},
                {"app_id": " "},
                {"platform": "windows"},
                {"require_authority": None},
            ]
        )
        for options in cases:
            with self.subTest(options=options), fast_clock():
                driver = ScanDriver("ios")
                with self.assertRaises((ValueError, TypeError)):
                    self.exercise(driver, [], **options)
                self.assertFalse(driver.find_calls)
                self.assertTrue(all(action == "guard" for action in driver.trace))
                driver.start.click.assert_not_called()

    def test_guard_parameter_is_required_not_an_implicit_noop(self) -> None:
        driver = ScanDriver("ios")
        with self.assertRaises(TypeError):
            ui.exercise_scan(
                driver,
                platform="ios",
                app_id=driver.app_id,
                timeout=20,
                observations=[],
                expected_local_name=LOCAL_NAME,
                expected_service_uuid=SERVICE_UUID,
            )
        self.assertEqual(driver.trace, [])

    def test_limits_accept_minimum_and_maximum_without_modifying_timeout_input(
        self,
    ) -> None:
        for value, timeout in ((1, 20), (5000, 300), (60000, 70)):
            with self.subTest(value=value), fast_clock():
                driver = ScanDriver("android", [advertisement("n" * 160)])
                driver.timeout_text = str(value)
                self.exercise(
                    driver,
                    [],
                    timeout=timeout,
                    observation_timeout_ms=value,
                    expected_local_name="n" * 160,
                )
                self.assert_no_input_changes(driver)

    def test_timeout_control_must_equal_requested_value_before_scan(self) -> None:
        for value in ("6000", "", "5000 ms", "invalid"):
            with self.subTest(value=value), fast_clock():
                driver = ScanDriver("ios")
                driver.timeout_text = value
                with self.assertRaises(ui.AppiumError):
                    self.exercise(driver, [])
                driver.scan_start.click.assert_not_called()
                self.assert_no_cleanup(driver)

    def test_guard_loss_before_first_action_and_after_every_remote_action(self) -> None:
        # Derive checkpoints from an actual successful traversal, not a guessed
        # number of helper reads; both platform foreground boundaries are real.
        for platform in ("ios", "android"):
            with fast_clock():
                baseline = ScanDriver(platform)
                self.exercise(baseline, [], require_app_identity=Mock())
                checkpoints = [action for action in baseline.trace if action != "guard"]
            for checkpoint in range(len(checkpoints) + 1):
                with (
                    self.subTest(platform=platform, checkpoint=checkpoint),
                    fast_clock(),
                ):
                    driver = ScanDriver(platform)
                    actions: list[str] = []
                    revoked_at: list[int] = []
                    driver.authorized = checkpoint != 0

                    def after_action(
                        action: str,
                        actions=actions,
                        checkpoint=checkpoint,
                        driver=driver,
                        revoked_at=revoked_at,
                    ) -> None:
                        actions.append(action)
                        if len(actions) == checkpoint:
                            driver.authorized = False
                            revoked_at.append(len(driver.trace))

                    driver.after_action = after_action

                    with self.assertRaises(ui.AppiumError) as caught:
                        self.exercise(driver, [], require_app_identity=Mock())
                    self.assertIs(caught.exception, driver.guard_error)
                    self.assertEqual(len(actions), checkpoint)
                    if revoked_at:
                        self.assertTrue(
                            all(
                                event == "guard"
                                for event in driver.trace[revoked_at[0] :]
                            )
                        )
                    self.assert_no_input_changes(driver)

    def test_guard_is_last_action_before_click_after_resolution(self) -> None:
        for identifier in (
            "start-session",
            "start-scan",
            "next-scan-event",
            "stop-scan",
            "close-session",
            "workbench-evidence",
        ):
            with self.subTest(identifier=identifier), fast_clock():
                driver = ScanDriver("android")

                def revoke(action: str, driver=driver, identifier=identifier) -> None:
                    if action == f"enabled:{identifier}":
                        driver.authorized = False

                driver.after_action = revoke
                with self.assertRaises(ui.AppiumError) as caught:
                    self.exercise(driver, [])
                self.assertIs(caught.exception, driver.guard_error)
                driver.nodes[identifier].click.assert_not_called()
                self.assertEqual(driver.trace[-1], "guard")

    def test_guard_is_checked_during_missing_element_polling_not_swallowed(
        self,
    ) -> None:
        for failure in (NoSuchElementException, StaleElementReferenceException):
            with self.subTest(failure=failure), fast_clock() as clock:
                driver = ScanDriver("ios")
                driver.overrides["scan-state"] = Mock(side_effect=failure("not ready"))
                polls: list[float] = []
                original_sleep = clock.sleep

                def sleep(
                    seconds: float,
                    original_sleep=original_sleep,
                    polls=polls,
                    driver=driver,
                    clock=clock,
                ) -> None:
                    original_sleep(seconds)
                    polls.append(clock.now)
                    driver.authorized = False

                with (
                    patch("time.sleep", sleep),
                    self.assertRaises(ui.AppiumError) as caught,
                ):
                    self.exercise(driver, [])
                self.assertIs(caught.exception, driver.guard_error)
                self.assertTrue(polls)
                self.assertEqual(driver.trace[-1], "guard")
                self.assert_no_cleanup(driver)

    def test_stale_missing_hidden_scan_reads_refetch_without_repeating_commands(
        self,
    ) -> None:
        for platform in ("ios", "android"):
            for identifier in (
                "scan-state",
                "scan-count",
                "scan-samples",
                "scan-observation-timeout",
            ):
                with (
                    self.subTest(platform=platform, identifier=identifier),
                    fast_clock(),
                ):
                    driver = ScanDriver(platform)
                    node = driver.nodes[identifier]
                    stale = Element()
                    stale.is_displayed.side_effect = StaleElementReferenceException(
                        "stale display"
                    )
                    early = iter(
                        [
                            NoSuchElementException(),
                            StaleElementReferenceException(),
                            Element("wrong hidden", displayed=False),
                            stale,
                        ]
                    )

                    def lookup(early=early, node=node):
                        value = next(early, node)
                        if isinstance(value, Exception):
                            raise value
                        return value

                    driver.overrides[identifier] = Mock(side_effect=lookup)
                    self.exercise(driver, [])
                    driver.scan_start.click.assert_called_once_with()
                    driver.scan_next.click.assert_called_once_with()
                    driver.scan_stop.click.assert_called_once_with()
                    driver.close.click.assert_called_once_with()

    def test_scan_text_read_can_go_stale_and_must_be_refetched(self) -> None:
        for identifier in ("scan-state", "scan-count", "scan-samples"):
            with self.subTest(identifier=identifier), fast_clock():
                driver = ScanDriver("ios")
                node = driver.nodes[identifier]
                value = node.value
                reads: list[str] = []

                def stale_once(value=value, reads=reads, identifier=identifier) -> str:
                    reads.append(identifier)
                    if len(reads) == 1:
                        raise StaleElementReferenceException(
                            "stale text, not stale lookup"
                        )
                    return value()

                node.value = stale_once
                self.exercise(driver, [])
                self.assertGreater(len(reads), 1)
                driver.scan_start.click.assert_called_once_with()
                driver.scan_next.click.assert_called_once_with()
                driver.scan_stop.click.assert_called_once_with()
                driver.close.click.assert_called_once_with()

    def test_disabled_scan_action_times_out_without_click_or_cleanup(self) -> None:
        for identifier in ("start-scan", "next-scan-event", "stop-scan"):
            with self.subTest(identifier=identifier), fast_clock() as clock:
                driver = ScanDriver("android")
                node = driver.nodes[identifier]
                node.is_enabled.side_effect = lambda node=node: node.read(
                    "enabled", False
                )
                with self.assertRaises(ui.AppiumError):
                    self.exercise(driver, [])
                node.click.assert_not_called()
                self.assertLessEqual(clock.now, 20.1)
                driver.close.click.assert_not_called()
                if identifier != "stop-scan":
                    driver.scan_stop.click.assert_not_called()

    def test_permanently_missing_hidden_or_stale_scan_data_never_passes(self) -> None:
        for identifier in (
            "scan-state",
            "scan-count",
            "scan-samples",
            "scan-observation-timeout",
        ):
            for failure in ("missing", "hidden", "stale"):
                with (
                    self.subTest(identifier=identifier, failure=failure),
                    fast_clock() as clock,
                ):
                    driver = ScanDriver("android")
                    if failure == "hidden":
                        driver.overrides[identifier] = Mock(
                            return_value=Element("running", displayed=False)
                        )
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
                    self.assertNotIn("scan:advertisement-matched", observations)
                    self.assertLessEqual(clock.now, 20.1)
                    self.assert_no_cleanup(driver)

    def test_late_scan_read_and_foreground_loss_abort_without_later_actions(
        self,
    ) -> None:
        for platform in ("ios", "android"):
            for identifier in (
                "scan-observation-timeout",
                "start-scan",
                "scan-count",
                "next-scan-event",
                "stop-scan",
                "close-session",
            ):
                for failure in ("late", "foreign"):
                    with (
                        self.subTest(
                            platform=platform, identifier=identifier, failure=failure
                        ),
                        fast_clock() as clock,
                    ):
                        driver = ScanDriver(platform)

                        def lookup(
                            driver=driver,
                            failure=failure,
                            identifier=identifier,
                            clock=clock,
                        ):
                            if failure == "late":
                                clock.now += 21
                            else:
                                driver.foreground = "foreign.app"
                            return driver.nodes[identifier]

                        driver.overrides[identifier] = Mock(side_effect=lookup)
                        observations: list[str] = []
                        with self.assertRaises(ui.AppiumError):
                            self.exercise(driver, observations)
                        self.assertNotIn("closed", observations)
                        if identifier in (
                            "start-scan",
                            "next-scan-event",
                            "stop-scan",
                            "close-session",
                        ):
                            driver.nodes[identifier].click.assert_not_called()
                        if identifier != "close-session":
                            driver.close.click.assert_not_called()

    def test_total_deadline_not_reset_per_stage_or_pull(self) -> None:
        with fast_clock() as clock:
            driver = ScanDriver(
                "ios", [advertisement("other", ())] * 7 + [advertisement()]
            )
            original = driver.scan_next.click.side_effect

            def slow_pull() -> None:
                original()
                clock.now += 3

            driver.scan_next.click.side_effect = slow_pull
            observations: list[str] = []
            with self.assertRaises(ui.AppiumError):
                self.exercise(
                    driver, observations, timeout=10, observation_timeout_ms=5000
                )
            self.assertLessEqual(driver.scan_next.click.call_count, 4)
            self.assertLessEqual(clock.now, 12.1)
            self.assertNotIn("scan:advertisement-matched", observations)
            self.assert_no_cleanup(driver)

    def test_total_budget_includes_lifecycle_startup_and_scan_start(self) -> None:
        with fast_clock() as clock:
            driver = ScanDriver("android")
            for node in (driver.start, driver.scan_start):
                original = node.click.side_effect

                def slow_start(original=original) -> None:
                    original()
                    clock.now += 6

                node.click.side_effect = slow_start
            observations: list[str] = []
            with self.assertRaises(ui.AppiumError):
                self.exercise(driver, observations, timeout=10)
            self.assertEqual(observations, ["idle", "started"])
            driver.scan_next.click.assert_not_called()
            self.assert_no_cleanup(driver)

    def test_late_clicks_and_ambiguous_clicks_are_not_retried_or_cleaned_up(
        self,
    ) -> None:
        for identifier in (
            "start-session",
            "start-scan",
            "next-scan-event",
            "stop-scan",
            "close-session",
            "workbench-evidence",
        ):
            for failure in ("late", "stale", "transport"):
                with (
                    self.subTest(identifier=identifier, failure=failure),
                    fast_clock() as clock,
                ):
                    driver = ScanDriver("ios")
                    node = driver.nodes[identifier]
                    original = node.click.side_effect

                    def click(original=original, failure=failure, clock=clock) -> None:
                        original()
                        if failure == "late":
                            clock.now += 21
                        elif failure == "stale":
                            raise StaleElementReferenceException("uncertain click")
                        else:
                            raise WebDriverException("uncertain click")

                    node.click.side_effect = click
                    observations: list[str] = []
                    error = (
                        ui.AppiumError
                        if failure == "late"
                        else (
                            StaleElementReferenceException
                            if failure == "stale"
                            else WebDriverException
                        )
                    )
                    with self.assertRaises(error):
                        self.exercise(driver, observations)
                    node.click.assert_called_once_with()
                    self.assertNotIn("closed", observations)
                    if identifier != "close-session":
                        driver.close.click.assert_not_called()
                    if identifier not in ("stop-scan", "close-session"):
                        driver.scan_stop.click.assert_not_called()
                    self.assert_no_input_changes(driver)

    def test_start_pull_stop_and_close_errors_preserve_real_visible_detail(
        self,
    ) -> None:
        cases = (
            (
                "start-session",
                "failed",
                "operation/start_failed: owner initialization rejected",
            ),
            (
                "start-scan",
                "failed",
                "operation/start_failed: radio rejected [apple/7 CBError: original detail]",
            ),
            (
                "next-scan-event",
                "failed",
                "observation/timeout: original pull timed out",
            ),
            ("stop-scan", "failed", "operation/stop_failed: native stop rejected"),
            ("close-session", "failed", "cleanup_failed: backend retirement failed"),
            (
                "close-session",
                "closedWithError",
                "cleanup_failed: owner closed but cleanup failed",
            ),
        )
        for platform in ("ios", "android"):
            for identifier, state, detail in cases:
                with (
                    self.subTest(platform=platform, identifier=identifier, state=state),
                    fast_clock(),
                ):
                    driver = ScanDriver(platform)
                    original = driver.nodes[identifier].click.side_effect

                    def click(
                        original=original,
                        identifier=identifier,
                        driver=driver,
                        state=state,
                        detail=detail,
                    ) -> None:
                        original()
                        if identifier in ("start-session", "close-session"):
                            driver.state = state
                            driver.session_error = detail
                        else:
                            driver.scan_state = state
                            driver.scan_error = detail

                    driver.nodes[identifier].click.side_effect = click
                    observations: list[str] = []
                    with self.assertRaises(ui.AppiumError) as caught:
                        self.exercise(driver, observations)
                    self.assertIn(detail, str(caught.exception))
                    driver.nodes[identifier].click.assert_called_once_with()
                    self.assertNotIn("closed", observations)
                    if identifier != "close-session":
                        driver.close.click.assert_not_called()
                    if identifier not in ("stop-scan", "close-session"):
                        driver.scan_stop.click.assert_not_called()

    def test_browsing_cannot_replace_running_scan_count_samples_snapshot_or_run(
        self,
    ) -> None:
        for field in (
            "scan-state",
            "scan-count",
            "scan-samples",
            "evidence-run",
            "session-state",
            "adapter-state",
            "title",
        ):
            with self.subTest(field=field), fast_clock():
                driver = ScanDriver("android")
                original = driver.navigate
                changed: list[str] = []

                def navigate(
                    page: str,
                    original=original,
                    driver=driver,
                    changed=changed,
                    field=field,
                ) -> None:
                    original(page)
                    if (
                        driver.scan_state == "running"
                        and driver.total
                        and page in ("Tests", "Evidence")
                        and not changed
                    ):
                        changed.append(field)
                        if field == "scan-state":
                            driver.scan_state = "idle"
                        elif field == "scan-count":
                            driver.total += 1
                        elif field == "scan-samples":
                            driver.samples = [advertisement("replaced", ())]
                        elif field == "evidence-run":
                            driver.run = "Run 2"
                        elif field == "session-state":
                            driver.state = "idle"
                        elif field == "adapter-state":
                            driver.snapshot_nodes["adapter-state"].text = "poweredOff"
                        else:
                            driver.nodes["formal-host-title"].value = lambda: (
                                "other host"
                            )

                driver.navigate = navigate
                with self.assertRaises(ui.AppiumError):
                    self.exercise(driver, [])
                self.assertEqual(changed, [field])
                self.assert_no_cleanup(driver)

    def test_final_evidence_requires_exact_numbered_success_actions(self) -> None:
        corruptions = {
            "missing-start": lambda text: text.replace("1. start: succeeded\n", ""),
            "start-wait-not-start": lambda text: text.replace(
                "1. start:", "1. scan-start-wait:"
            ),
            "missing-begin": lambda text: text.replace(
                "2. begin-scan: succeeded\n", ""
            ),
            "missing-wait": lambda text: text.replace(
                "3. scan-start-wait: succeeded\n", ""
            ),
            "missing-pull": lambda text: text.replace("4. scan-pull: succeeded\n", ""),
            "missing-stop": lambda text: text.replace("5. scan-stop: succeeded\n", ""),
            "missing-close": lambda text: text.replace("6. close: succeeded", ""),
            "duplicate-number": lambda text: text.replace(
                "4. scan-pull", "3. scan-pull"
            ),
            "wrong-order": lambda text: text.replace(
                "2. begin-scan", "2. scan-start-wait"
            ).replace("3. scan-start-wait", "3. begin-scan"),
            "extra-action": lambda text: text + "\n7. scan-stop: succeeded",
            "no-numbers": lambda text: "\n".join(
                line.split(". ", 1)[1] for line in text.splitlines()
            ),
            "empty": lambda text: "",
        }
        for action in (
            "start",
            "begin-scan",
            "scan-start-wait",
            "scan-pull",
            "scan-stop",
            "close",
        ):
            for result in ("failed", "pending"):
                corruptions[f"{action}-{result}"] = (
                    lambda text, action=action, result=result: text.replace(
                        f"{action}: succeeded", f"{action}: {result}"
                    )
                )
        for case, corrupt in corruptions.items():
            with self.subTest(case=case), fast_clock() as clock:
                driver = ScanDriver("ios")
                driver.final_evidence = corrupt
                with self.assertRaises(ui.AppiumError):
                    self.exercise(driver, [])
                driver.close.click.assert_called_once_with()
                driver.scan_stop.click.assert_called_once_with()
                self.assertEqual(driver.page, "Evidence")
                self.assertLessEqual(clock.now, 20.1)

    def test_final_evidence_all_pulls_and_same_run_are_required(self) -> None:
        for failure in (
            "missing-middle-pull",
            "changed-run",
            "missing-run",
            "hidden-evidence",
        ):
            with self.subTest(failure=failure), fast_clock():
                driver = ScanDriver(
                    "android", [advertisement("other", ()), advertisement()]
                )
                if failure == "missing-middle-pull":
                    driver.final_evidence = lambda value: value.replace(
                        "4. scan-pull: succeeded\n", ""
                    )
                elif failure == "changed-run":
                    original = driver.close.click.side_effect

                    def close(original=original, driver=driver) -> None:
                        original()
                        driver.run = "Run 2"

                    driver.close.click.side_effect = close
                elif failure == "missing-run":
                    driver.overrides["evidence-run"] = Mock(
                        side_effect=NoSuchElementException()
                    )
                else:
                    driver.overrides["lifecycle-evidence"] = Mock(
                        return_value=Element(
                            "1. start: succeeded\n2. begin-scan: succeeded\n3. scan-start-wait: succeeded\n4. scan-pull: succeeded\n5. scan-pull: succeeded\n6. scan-stop: succeeded\n7. close: succeeded",
                            displayed=False,
                        )
                    )
                with self.assertRaises(ui.AppiumError):
                    self.exercise(driver, [], max_pulls=2)
                self.assertLessEqual(driver.close.click.call_count, 1)
                self.assertLessEqual(driver.scan_stop.click.call_count, 1)


if __name__ == "__main__":
    unittest.main()
