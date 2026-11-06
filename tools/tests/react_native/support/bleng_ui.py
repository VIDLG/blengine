"""Pure WebDriver UI fakes shared by formal BLeng host regressions."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import contextmanager
from unittest.mock import Mock, patch

from selenium.common.exceptions import NoSuchElementException

from tools.mobile_appium import AppiumError


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Element:
    def __init__(self, text: str = "", *, displayed: bool = True) -> None:
        self.text = text

        self.is_displayed = Mock(return_value=displayed)
        self.is_enabled = Mock(return_value=True)
        self.click = Mock()


class LifecycleDriver:
    """Only a UI tree; no production/session/native internals are simulated."""

    def __init__(self, platform: str) -> None:
        self.platform = platform

        self.app_id = "example.explicit.formal"
        self.foreground = self.app_id
        self.state = "idle"
        self.page = "Tests"
        self.visits: list[tuple[str, str]] = []
        self.navigation = {
            identifier: Element()
            for identifier in (
                "workbench-tests",
                "workbench-current",
                "workbench-evidence",
            )
        }
        for identifier, page in zip(
            self.navigation, ("Tests", "Current run", "Evidence"), strict=True
        ):
            self.navigation[identifier].click.side_effect = lambda page=page: (
                self.navigate(page)
            )
        self.evidence = Element("start: succeeded\nclose: succeeded")
        self.session_id = "mock-webdriver-session"
        self.title = Element("BLEHUB FORMAL RN CONSUMER")
        self.start = Element()
        self.close = Element()
        self.snapshot_nodes = {
            "adapter-state": Element("poweredOn"),
            "scan-authorization": Element("authorized"),
            "connection-authorization": Element("notRequired"),
        }
        self.start.click.side_effect = self.start_session
        self.close.click.side_effect = lambda: setattr(self, "state", "closed")
        self.quit = Mock()
        self.save_screenshot = Mock(return_value=True)
        self.find_calls: list[str] = []
        self.overrides: dict[str, Mock] = {}

    def navigate(self, page: str) -> None:
        self.page = page
        self.visits.append((self.state, page))

    def start_session(self) -> None:
        self.state = "started"
        self.navigate("Current run")

    @property
    def current_package(self) -> str:
        return self.foreground

    @property
    def page_source(self) -> str:
        return f'<Application bundleId="{self.foreground}" />'

    def find_element(self, strategy: str, locator: str) -> Element:
        if self.platform == "ios":
            if strategy != "accessibility id":
                raise AssertionError(strategy)
            identifier = locator
        else:
            if strategy != "-android uiautomator":
                raise AssertionError(strategy)
            identifier = locator.split('resourceId("', 1)[1].split('"', 1)[0]
        self.find_calls.append(identifier)
        if identifier in self.overrides:
            return self.overrides[identifier]()
        if identifier == "formal-host-title":
            return self.title
        if identifier == "session-state":
            return Element(self.state)
        if identifier == "workbench-page":
            return Element(self.page)
        if identifier in self.navigation:
            return self.navigation[identifier]
        if identifier == "lifecycle-evidence" and self.page == "Evidence":
            return self.evidence
        if identifier == "start-session" and self.page == "Tests":
            return self.start
        if identifier == "close-session" and self.page == "Current run":
            return self.close
        if (
            identifier in self.snapshot_nodes
            and self.state == "started"
            and self.page == "Current run"
        ):
            return self.snapshot_nodes[identifier]
        raise NoSuchElementException(identifier)


@contextmanager
def fast_clock():
    clock = Clock()
    # Patch the real helper clocks too: production wait_find/foreground checks run.
    with patch("time.monotonic", clock.monotonic), patch("time.sleep", clock.sleep):
        yield clock


LOCAL_NAME = "BleHub Fault v2 B"
SERVICE_UUID = "12345678-1234-5678-1234-56789abcdef0"


def advertisement(
    name: str = LOCAL_NAME, services: tuple[str, ...] = (SERVICE_UUID,)
) -> str:
    """Match useManualScan.summary's visible advertisement format, not a DTO."""
    visible_services = ", ".join(service[:160] for service in services[:8]) or "none"
    omitted = f" (+{len(services) - 8} omitted)" if len(services) > 8 else ""
    return (
        f"name={json.dumps(name[:160], ensure_ascii=False)}; rssi=-48; connectable=true\n"
        f"services={visible_services}{omitted}\n"
        "manufacturer=none\nservice data=none"
    )


class ScanElement:
    """A UI node whose remote reads and actions are visible in the driver trace."""

    def __init__(
        self, driver: ScanDriver, identifier: str, value: Callable[[], str]
    ) -> None:
        self.driver = driver
        self.identifier = identifier

        self.value = value
        self.is_displayed = Mock(side_effect=lambda: self.read("displayed", True))
        self.is_enabled = Mock(side_effect=lambda: self.read("enabled", True))
        self.click = Mock(side_effect=lambda: self.read("click", None))
        self.clear = Mock(side_effect=lambda: self.read("clear", None))
        self.send_keys = Mock(side_effect=lambda *args: self.read("send_keys", None))
        self.get_attribute = Mock(side_effect=self.attribute)

    def read(self, action: str, value):
        self.driver.record(f"{action}:{self.identifier}")
        return value

    @property
    def text(self) -> str:
        self.driver.record(f"text:{self.identifier}")
        return self.value()

    def attribute(self, name: str) -> str:
        self.driver.record(f"attribute:{self.identifier}:{name}")
        return self.value()


class ScanDriver(LifecycleDriver):
    """UI-only scan tree; no SDK, native session, lease or provider calls."""

    def __init__(self, platform: str, pulls: list[str] | None = None) -> None:
        super().__init__(platform)
        self.trace: list[str] = []
        self.after_action: Callable[[str], None] = lambda action: None
        self.authorized = True
        self.guard_error = AppiumError("test authority revoked")
        self.scan_state = "idle"
        self.total = 0
        self.samples: list[str] = []
        self.pulls = [advertisement()] if pulls is None else pulls
        self.defer_pull_until_count_read = False
        self.pending_pull: tuple[int, str] | None = None
        self.count_reads: list[int] = []
        self.evidence_snapshot: dict[str, str] = {}
        self.actions: list[tuple[str, str, str]] = []
        self.run = "Run 1"
        self.timeout_text = "5000"
        self.scan_error = ""
        self.session_error = ""
        self.final_evidence: Callable[[str], str] = lambda value: value
        self.scan_visits: list[tuple[str, str, int, tuple[str, ...]]] = []
        self.nodes: dict[str, ScanElement] = {}
        values = {
            "formal-host-title": lambda: "BLEHUB FORMAL RN CONSUMER",
            "session-state": lambda: self.state,
            "session-error": lambda: self.session_error,
            "workbench-page": lambda: self.page,
            "evidence-run": lambda: self.run,
            "lifecycle-evidence": self.evidence_text,
            "scan-state": lambda: self.scan_state,
            "scan-count": self.count_text,
            "scan-samples": lambda: "\n\n".join(self.samples),
            "scan-observation-timeout": lambda: self.timeout_text,
            "scan-error": lambda: self.scan_error,
        }
        values.update(
            {
                key: lambda node=node: node.text
                for key, node in self.snapshot_nodes.items()
            }
        )
        values.update(
            {
                f"evidence-{key}": lambda key=key: self.evidence_snapshot[key]
                for key in self.snapshot_nodes
            }
        )
        for identifier, value in values.items():
            self.nodes[identifier] = ScanElement(self, identifier, value)
        handlers = {
            "start-session": self.start_session,
            "close-session": self.close_session,
            "start-scan": self.start_scan,
            "next-scan-event": self.pull_scan,
            "stop-scan": self.stop_scan,
            **{
                key: lambda page=page: self.navigate(page)
                for key, page in zip(
                    self.navigation, ("Tests", "Current run", "Evidence"), strict=True
                )
            },
        }
        for identifier, handler in handlers.items():
            node = ScanElement(self, identifier, lambda: "")

            def click(identifier=identifier, handler=handler):
                self.record(f"click:{identifier}")
                handler()

            node.click.side_effect = click
            self.nodes[identifier] = node
        self.start = self.nodes["start-session"]
        self.close = self.nodes["close-session"]
        self.navigation = {key: self.nodes[key] for key in self.navigation}
        self.scan_start = self.nodes["start-scan"]
        self.scan_next = self.nodes["next-scan-event"]
        self.scan_stop = self.nodes["stop-scan"]

    def record(self, action: str) -> None:
        self.trace.append(action)
        self.after_action(action)

    def require_authority(self) -> None:
        self.trace.append("guard")
        if not self.authorized:
            raise self.guard_error

    @property
    def current_package(self) -> str:
        self.record("current_package")
        return self.foreground

    @property
    def page_source(self) -> str:
        self.record("page_source")
        return f'<Application bundleId="{self.foreground}" />'

    def navigate(self, page: str) -> None:
        super().navigate(page)
        self.scan_visits.append(
            (page, self.scan_state, self.total, tuple(self.samples))
        )

    def start_session(self) -> None:
        super().start_session()
        self.actions.append(("start", "succeeded", ""))
        self.evidence_snapshot = {
            key: node.text for key, node in self.snapshot_nodes.items()
        }

    def close_session(self) -> None:
        self.state = "closed"
        self.scan_state = "ownerClosed"
        self.actions.append(("close", "succeeded", ""))

    def start_scan(self) -> None:
        self.scan_state = "running"
        self.actions.extend(
            [
                ("begin-scan", "succeeded", ""),
                ("scan-start-wait", "succeeded", ""),
            ]
        )

    def pull_scan(self) -> None:
        index = sum(action == "scan-pull" for action, _, _ in self.actions)
        sample = (
            self.pulls[index] if index < len(self.pulls) else advertisement("other", ())
        )
        if self.defer_pull_until_count_read:
            if self.pending_pull is not None:
                raise AssertionError("A pending UI pull must not be submitted again")
            self.pending_pull = (len(self.actions), sample)
            self.actions.append(("scan-pull", "pending", ""))
        else:
            self.actions.append(("scan-pull", "succeeded", ""))
            self.total += 1
            self.samples = [*self.samples, sample][-8:]

    def count_text(self) -> str:
        self.count_reads.append(self.total)
        value = (
            f"{self.total} advertisements observed; "
            f"{max(0, self.total - len(self.samples))} samples omitted (latest 8 only)."
        )
        if self.pending_pull is not None:
            # The remote count response has already been captured. React settles
            # the one pending pull before the next remote samples read arrives.
            index, sample = self.pending_pull
            self.actions[index] = ("scan-pull", "succeeded", "")
            self.total += 1
            self.samples = [*self.samples, sample][-8:]
            self.pending_pull = None
        return value

    def stop_scan(self) -> None:
        self.scan_state = "stopped"
        self.actions.append(("scan-stop", "succeeded", ""))

    def evidence_text(self) -> str:
        value = "\n".join(
            f"{index}. {action}: {result}{' — ' + detail if detail else ''}"
            for index, (action, result, detail) in enumerate(self.actions, 1)
        )
        return self.final_evidence(value) if self.state == "closed" else value

    def find_element(self, strategy: str, locator: str) -> ScanElement:
        if self.platform == "ios":
            if strategy != "accessibility id":
                raise AssertionError(strategy)
            identifier = locator
        else:
            if strategy != "-android uiautomator":
                raise AssertionError(strategy)
            identifier = locator.split('resourceId("', 1)[1].split('"', 1)[0]
        self.record(f"find:{identifier}")
        self.find_calls.append(identifier)
        if identifier in self.overrides:
            return self.overrides[identifier]()
        if identifier in {"evidence-run", "lifecycle-evidence"}:
            present = self.page == "Evidence" and bool(self.actions)
        elif identifier.startswith("evidence-"):
            present = self.page == "Evidence" and bool(self.evidence_snapshot)
        elif identifier in {"scan-state", "scan-count", "scan-samples", "scan-error"}:
            present = self.page == "Current run" or (
                self.page == "Evidence" and bool(self.actions)
            )
            if identifier == "scan-error":
                present = present and bool(self.scan_error)
        elif identifier in self.snapshot_nodes:
            present = self.page == "Current run" and self.state == "started"
        elif identifier.startswith("scan-") or identifier in {
            "start-scan",
            "next-scan-event",
            "stop-scan",
            "close-session",
        }:
            present = self.page == "Current run"
        elif identifier == "session-error":
            present = bool(self.session_error)
        else:
            present = True
        if not present or identifier not in self.nodes:
            raise NoSuchElementException(identifier)
        return self.nodes[identifier]
