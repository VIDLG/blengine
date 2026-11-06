"""Owned local Appium server and one attach-only formal scan session.

Platform preparation stays explicit: the mandatory platform-owner guard must
validate the caller's existing WDA/forwarder or UiAutomator2 preparation under the
same phone lease. Its separate mandatory finalizer must close that platform
owner before phone release. This class never installs helpers, signs/builds WDA
or grants permissions; concrete platform preparation remains outside this owner.
A newly created temporary Android helper scope must be explicitly handed to this
parent for rollback. None means externally provisioned helpers with no temporary
mutations owned here; it is never inferred from installed helpers or a JSON report.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from collections.abc import Callable
from pathlib import Path
from traceback import format_exception_only

from appium import webdriver

from tests.peripherals.nrf52840.fixture_session import _read_bytes
from tools.android import android_appium
from tools.android.android_appium import create_android_driver
from tools.android.state_control import AndroidRadioController
from tools.apple.ios_appium import (
    create_ios_attached_driver,
    validate_ios_attach_configuration,
)
from tools.mobile_appium import AppiumServer, require_attached_driver

from .bleng_android_preparation import AndroidHelperPreparation
from .bleng_android_session import AndroidAutomationOwner
from .capture_io import publish_new_json
from .ios_radio_lease import SCAN_OBSERVATION_SCOPE, IosRadioLease


class ScanAutomationOwner:
    """One-shot server/session owner. Caller must close after *any* start attempt.

    ``require_platform_owner(deadline)`` is a required, bounded external ownership
    check, not a readiness URL or a remembered success flag. The required
    ``finalize_platform_owner(deadline)`` verifies and closes that same scope;
    there is no always-success default. Startup and normal guards use the scenario
    budget; close uses a separate budget. At most two
    extra seconds may be used to reap this owner's local server after an exhausted
    cleanup deadline, and such late cleanup remains failed. Android requires a
    concrete prepared AndroidAutomationOwner; its guards/finalizer are derived
    from that owner, never caller-overridden callbacks. Bare Android callers stay
    refused. Synthetic downstream callback tests explicitly simulate admission;
    only the concrete-owner integration tests exercise this real handoff.
    """

    def __init__(
        self,
        *,
        phone: AndroidRadioController | IosRadioLease,
        run_id: str,
        app_id: str,
        node: str,
        appium_entry: Path,
        port: int,
        output: Path,
        cwd: Path,
        deadline: float,
        require_platform_owner: Callable[[float], None] | None = None,
        finalize_platform_owner: Callable[[float], None] | None = None,
        android_owner: AndroidAutomationOwner | None = None,
        android_preparation: AndroidHelperPreparation | None = None,
        activity: str | None = None,
        wda_url: str | None = None,
        debug_logging: bool = False,
    ) -> None:
        if type(debug_logging) is not bool:
            raise ValueError("debug_logging requires an explicit boolean")
        self._remaining(deadline)
        if android_preparation is not None and (
            type(android_preparation) is not AndroidHelperPreparation
            or type(android_owner) is not AndroidAutomationOwner
            or type(phone) is not AndroidRadioController
        ):
            raise TypeError(
                "temporary helpers require actual Android preparation and platform owners"
            )
        if android_owner is not None:
            if (
                type(android_owner) is not AndroidAutomationOwner
                or type(phone) is not AndroidRadioController
            ):
                raise TypeError(
                    "concrete Android automation owner and Android phone required"
                )
            if (
                require_platform_owner is not None
                or finalize_platform_owner is not None
            ):
                raise ValueError(
                    "Android callbacks are derived from its concrete owner, not overridden"
                )
            if android_owner.system_port == port:
                raise ValueError("Appium and Android forwarding ports must differ")
            require_platform_owner = android_owner.require_owner
            finalize_platform_owner = android_owner.finalize
        self._android_owner = android_owner
        self._android_preparation = android_preparation
        self._helper_restored = False
        if not callable(require_platform_owner) or not callable(
            finalize_platform_owner
        ):
            raise TypeError("a live platform-owner guard is required")
        if type(phone) is AndroidRadioController:
            platform = "android"
            if not isinstance(activity, str) or not activity or wda_url is not None:
                raise ValueError("Android requires an explicit activity, not a WDA URL")
        elif type(phone) is IosRadioLease:
            platform = "ios"
            if not isinstance(wda_url, str) or not wda_url or activity is not None:
                raise ValueError(
                    "iOS requires an explicit prepared WDA URL, not an activity"
                )
        else:
            raise TypeError("an actual supported phone lease is required")
        if (
            not isinstance(app_id, str)
            or re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+", app_id) is None
        ):
            raise ValueError("explicit canonical App identity required")
        if platform == "ios":
            validate_ios_attach_configuration(
                device=phone.device,
                bundle_id=app_id,
                wda_url=wda_url,
                command_timeout=min(30.0, self._remaining(deadline)),
            )
        elif (
            re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", app_id)
            is None
        ):
            raise ValueError("explicit canonical Android package required")
        elif (
            re.fullmatch(
                r"\.?[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)*", activity
            )
            is None
        ):
            raise ValueError("explicit canonical Android activity required")
        self._phone, self._run_id, self._device = phone, run_id, phone.device
        self._app_id, self._platform = app_id, platform
        self._activity, self._wda_url = activity, wda_url
        self._deadline = deadline
        self._platform_owner = require_platform_owner
        self._platform_finalizer = finalize_platform_owner
        self._platform_admitted = False
        self._output = output.absolute()
        self._started = self._ready = self._closed = False
        self._request_attempted = self._validated = False
        self._driver: webdriver.Remote | None = None
        self._session_id: str | None = None
        self._executor = self._config = None
        self._authority_failure: Exception | None = None
        self._close_failure: BaseException | None = None
        self._records: dict[Path, str] = {}
        self._require_phone(deadline)
        if android_preparation is not None:
            android_preparation.require_prepared(
                phone=phone, run_id=run_id, deadline=deadline
            )
            self._require_helper(deadline)
        self._output.mkdir(mode=0o700)
        self._server = AppiumServer(
            node=node,
            entry=appium_entry,
            port=port,
            log=self._output / "appium.log",
            timeout=min(30.0, self._remaining(deadline)),
            cwd=cwd,
            debug_logging=debug_logging,
            **(
                {"appium_home": android_owner.appium_home}
                if android_owner is not None
                else {}
            ),
        )
        self._base_url = self._server.base_url

    @staticmethod
    def _remaining(deadline: float) -> float:
        if type(deadline) not in (float, int) or not math.isfinite(deadline):
            raise ValueError("finite absolute automation deadline required")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("automation deadline exceeded")
        return remaining

    def _require_phone(self, deadline: float) -> None:
        self._remaining(deadline)
        phone = self._phone
        if (
            phone.device != self._device
            or phone.run_id != self._run_id
            or phone.document.get("device") != self._device
            or phone.document.get("run_id") != self._run_id
        ):
            raise RuntimeError("automation phone/run identity changed")
        if self._platform == "ios":
            if (
                phone.scope != SCAN_OBSERVATION_SCOPE
                or phone.document.get("scope") != SCAN_OBSERVATION_SCOPE
            ):
                raise RuntimeError("automation requires the scan observation scope")
        elif (
            phone.document.get("actions") != []
            or phone.initial_state is not None
            or phone.last_state is not None
        ):
            raise RuntimeError("automation requires App-only Android ownership")
        phone.require_owner()
        self._remaining(deadline)

    def _require_helper(self, deadline: float) -> None:
        if self._android_preparation is not None:
            self._android_preparation.require_handoff(
                automation=self._android_owner,
                phone=self._phone,
                run_id=self._run_id,
                deadline=deadline,
            )

    def _require_platform(self, deadline: float) -> None:
        if self._authority_failure is not None:
            raise RuntimeError(
                f"automation authority invalidated: {self._authority_failure}"
            ) from self._authority_failure
        try:
            self._require_phone(deadline)
            self._require_helper(deadline)
            self._platform_owner(deadline)
            self._require_phone(deadline)
        except TimeoutError:
            # A deadline is not evidence of foreign ownership. Close still needs
            # a fresh successful guard using its independent cleanup deadline.
            raise
        except Exception as error:
            self._authority_failure = error
            raise

    def _require_driver(self) -> None:
        if self._driver is None:
            raise RuntimeError("automation session has no known driver")
        require_attached_driver(
            self._driver,
            session_id=self._session_id,
            platform=self._platform,
            device=self._device,
            app_id=self._app_id,
        )
        if (
            getattr(
                self._driver.command_executor.client_config, "remote_server_addr", None
            )
            != self._base_url
        ):
            raise RuntimeError(
                "automation driver no longer targets the owned Appium endpoint"
            )
        if self._executor is not None and (
            self._driver.command_executor is not self._executor
            or self._executor.client_config is not self._config
        ):
            raise RuntimeError("automation driver transport changed")

    def start(self) -> webdriver.Remote:
        if self._started or self._closed:
            raise RuntimeError("automation start is one-shot")
        self._started = True
        result = {
            "run_id": self._run_id,
            "platform": self._platform,
            "device": self._device,
            "app_id": self._app_id,
            "server_url": self._base_url,
            "status": "failed",
            "session_id": None,
            "error": None,
            "platform_backend_owner": "external",
        }
        try:
            # Only a live same-run owner may authorize the protected Android
            # profile. Installed helpers or a passive report are not authority.
            self._require_helper(self._deadline)
            if self._platform == "android":
                android_appium.require_prepared_driver_contract(
                    self._android_owner,
                    phone=self._phone,
                    run_id=self._run_id,
                    app_id=self._app_id,
                    deadline=min(self._deadline, time.monotonic() + 30.0),
                )
            startup_deadline = min(self._deadline, time.monotonic() + 30.0)
            self._require_platform(startup_deadline)
            self._platform_admitted = True
            self._server.start(
                require_owner=lambda: self._require_platform(startup_deadline),
                deadline=startup_deadline,
            )
            self._require_platform(self._deadline)
            self._server.require_running()
            if self._android_owner is not None:
                self._android_owner.begin_session(
                    server_url=self._base_url, deadline=self._deadline
                )
            self._request_attempted = True
            timeout = min(30.0, self._remaining(self._deadline))
            if self._platform == "ios":
                self._driver = create_ios_attached_driver(
                    self._base_url,
                    device=self._device,
                    bundle_id=self._app_id,
                    wda_url=self._wda_url,
                    command_timeout=timeout,
                )
            else:
                self._driver = create_android_driver(
                    self._base_url,
                    device=self._device,
                    package=self._app_id,
                    activity=self._activity,
                    auto_launch=False,
                    command_timeout=timeout,
                    **(
                        {"system_port": self._android_owner.system_port}
                        if self._android_owner is not None
                        else {}
                    ),
                )
            self._session_id = self._driver.session_id
            result["session_id"] = self._session_id
            self._require_driver()
            self._executor = self._driver.command_executor
            self._config = self._executor.client_config
            if (
                type(self._config.timeout) not in (float, int)
                or not math.isfinite(self._config.timeout)
                or self._config.timeout <= 0
            ):
                raise RuntimeError("automation driver has no finite HTTP timeout")
            self._validated = True
            if self._android_owner is not None:
                self._android_owner.bind_session(self._driver, deadline=self._deadline)
            self._require_platform(self._deadline)
            self._server.require_running()
            result["status"] = "passed"
            path = self._output / "start.json"
            self._records[path] = publish_new_json(path, result)
            self._ready = True
            return self._driver
        except BaseException as error:
            result["error"] = "".join(
                format_exception_only(error, show_group=True)
            ).strip()
            result["status"] = "failed"
            try:
                publish_new_json(self._output / "start-failed.json", result)
            except Exception as publication:  # noqa: BLE001 -- preserve initialization and publication errors.
                error.add_note(f"automation start evidence: {publication}")
            raise

    def require_running(self, *, deadline: float) -> None:
        if not self._ready or self._closed:
            raise RuntimeError("automation session is not active")
        self._remaining(deadline)
        if deadline > self._deadline:
            raise ValueError("automation guard cannot extend the scenario deadline")
        try:
            self._require_platform(deadline)
            self._server.require_running()
            self._require_driver()
            self._require_records()
            if self._server.base_url != self._base_url:
                raise RuntimeError("automation server endpoint changed")
            self._remaining(deadline)
        except TimeoutError:
            raise
        except Exception as error:
            self._authority_failure = error
            raise

    def _require_records(self) -> None:
        for path, digest in self._records.items():
            if hashlib.sha256(_read_bytes(path)).hexdigest() != digest:
                raise RuntimeError("automation evidence changed")

    def require_closed(self) -> None:
        """Recheck completed local cleanup evidence before the phone can release."""
        if (
            not self._closed
            or (not self._platform_admitted and not self._helper_restored)
            or self._close_failure is not None
        ):
            raise RuntimeError(
                "automation cleanup is not confirmed"
            ) from self._close_failure
        self._require_records()
        if self._android_owner is not None and self._platform_admitted:
            self._android_owner.require_closed()
        if self._android_preparation is not None:
            self._android_preparation.require_restored()

    def require_handoff(
        self, *, driver, phone, run_id: str, app_id: str, deadline: float
    ) -> dict[str, str]:
        if (
            driver is not self._driver
            or phone is not self._phone
            or run_id != self._run_id
            or app_id != self._app_id
        ):
            raise RuntimeError("automation does not match the prepared scan handoff")
        self.require_running(deadline=deadline)
        return {
            "evidence_directory": str(self._output),
            "server_url": self._base_url,
            "platform_backend_owner": "external",
        }

    def close(self, *, deadline: float) -> None:
        if type(deadline) not in (float, int) or not math.isfinite(deadline):
            raise ValueError("finite absolute cleanup deadline required")
        if self._closed:
            if self._close_failure is not None:
                raise self._close_failure
            return
        self._closed = True
        self._ready = False
        errors: list[BaseException] = []
        result = {
            "run_id": self._run_id,
            "session_id": self._session_id,
            "session_closed": False,
            "server_closed": False,
            "status": "failed",
            "platform_backend_cleanup": "not_acquired",
            "helper_restoration": "not_owned"
            if self._android_preparation is None
            else "pending",
            "errors": [],
        }
        if self._validated:
            try:
                self._require_platform(deadline)
                self._server.require_running()
                self._require_driver()
                self._require_records()
                self._config.timeout = min(5.0, self._remaining(deadline))
                self._driver.quit()
                if self._android_owner is not None:
                    self._android_owner.session_deleted(
                        self._session_id, deadline=deadline
                    )
                result["session_closed"] = True
                self._require_phone(deadline)
            except BaseException as error:  # noqa: BLE001 -- reap the local server before propagating interruption.
                error.add_note("automation session cleanup")
                errors.append(error)
        elif self._request_attempted:
            errors.append(
                RuntimeError(
                    "remote session outcome unknown; refusing adoption or blind DELETE"
                )
            )
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                errors.append(
                    TimeoutError(
                        "automation cleanup deadline expired before local reap"
                    )
                )
            self._server.stop(timeout=min(5.0, remaining) if remaining > 0 else 2.0)
            result["server_closed"] = True
            self._remaining(deadline)
        except BaseException as error:  # noqa: BLE001 -- retain every cleanup failure for the final receipt.
            error.add_note("owned local Appium server cleanup")
            errors.append(error)
        if self._platform_admitted:
            result["platform_backend_cleanup"] = "failed"
            try:
                if self._authority_failure is not None:
                    raise RuntimeError(
                        "platform ownership was invalidated; refusing cleanup adoption"
                    ) from self._authority_failure
                self._require_phone(deadline)
                self._platform_finalizer(deadline)
                self._require_phone(deadline)
                result["platform_backend_cleanup"] = "passed"
            except BaseException as error:  # noqa: BLE001 -- preserve platform cleanup independently of local-server cleanup.
                error.add_note("platform automation owner finalization")
                errors.append(error)
        if self._android_preparation is not None:
            result["helper_restoration"] = "failed"
            try:
                self._require_phone(deadline)
                self._android_preparation.restore(
                    automation=self._android_owner, deadline=deadline
                )
                self._android_preparation.require_restored()
                self._require_phone(deadline)
                self._helper_restored = True
                result["helper_restoration"] = "passed"
                if not self._platform_admitted:
                    result["platform_backend_cleanup"] = "not_submitted"
            except BaseException as error:  # noqa: BLE001 -- helper rollback is attempted even after earlier cleanup failures.
                error.add_note("temporary Android helper restoration")
                errors.append(error)
        result["errors"] = [
            "".join(format_exception_only(error, show_group=True)).strip()
            for error in errors
        ]
        result["status"] = "failed" if errors else "passed"
        try:
            path = self._output / "close.json"
            self._records[path] = publish_new_json(path, result)
        except BaseException as error:  # noqa: BLE001 -- publication cannot mask cleanup or interruption.
            error.add_note("automation close evidence")
            errors.append(error)
        if errors:
            # Keep interruptions observable after independent local cleanup.
            interruption = next(
                (error for error in errors if not isinstance(error, Exception)), None
            )
            detail = "; ".join(
                "".join(format_exception_only(error, show_group=True)).strip()
                for error in errors
            )
            self._close_failure = interruption or RuntimeError(detail)
            if interruption is not None:
                interruption.add_note(detail)
            raise self._close_failure
