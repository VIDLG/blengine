"""Isolated contracts for the real prepared Android session and scan owners."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from tools.android.automation_observation import parse_active_instrumentation
from tools.android.state_control import AndroidRadioController
from tools.react_native.bleng_android_automation import (
    inspect_prepared_android_automation,
)
from tools.react_native.bleng_android_session import AndroidAutomationOwner
from tools.react_native.bleng_automation import ScanAutomationOwner
from tools.react_native.capture_io import publish_new_json
from tools.tests.react_native.support.android_automation import (
    APPOPS,
    BOOT,
    DEFAULT_IME,
    DEVICE_SESSION_ID,
    EMPTY_SERVICES,
    ENABLED_IMES,
    FORWARDS,
    GLOBAL_SECRET_MARKER,
    GLOBAL_SETTINGS,
    HOST_SESSION_ID,
    LIVE_SERVICES,
    OS_PROCESSES,
    PACKAGES,
    POLICIES,
    PS,
    SERVER,
    SERVICES,
    SETTINGS,
    STORE,
    AndroidAutomationHarness,
    active_processes,
    proc_stat,
)
from tools.tests.react_native.support.automation_observation import (
    COMPONENT,
    history,
    processes,
    record,
)

OEM_TIME = "---- TIME 2026/11/4 17:49:26 ----\n"
MIB = 1024 * 1024


def synthetic_processes(byte_size: int, *, active: bool = False) -> str:
    """Pad only an opaque process section, keeping the real parser envelope."""
    active_body = "  Active instrumentation:\n" + record(app_id=102) if active else ""
    before = "  All known processes:\n    synthetic_opaque="
    after = "\n" + active_body
    padding = byte_size - len(processes(before + after).encode("utf-8"))
    if padding < 0:
        raise ValueError("synthetic processes size is smaller than its envelope")
    return processes(before + "x" * padding + after)


class AndroidAutomationOwnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.scope = self.enterContext(ExitStack())
        self.sequence = 0
        self.new_case()

    def new_case(self) -> None:
        self.scope.close()
        self.sequence += 1
        self.h = self.scope.enter_context(
            AndroidAutomationHarness(self.root / str(self.sequence))
        )

    def owner(self, **changes: object) -> AndroidAutomationOwner:
        h = self.h
        arguments = {
            "phone": h.phone,
            "run_id": h.run_id,
            "app_id": h.app_id,
            "appium_home": h.appium_home,
            "expected_apk_sha256": h.expected,
            "system_port": h.system_port,
            "output": h.output,
            "cwd": h.root,
            "deadline": h.deadline,
        }
        arguments.update(changes)
        return AndroidAutomationOwner(**arguments)

    def require_prepared(self, owner: AndroidAutomationOwner) -> None:
        h = self.h
        self.assertIsNone(
            owner.require_prepared(
                phone=h.phone, run_id=h.run_id, app_id=h.app_id, deadline=150.0
            )
        )

    def bind(self) -> tuple[AndroidAutomationOwner, SimpleNamespace]:
        owner = self.owner()
        self.assertIsNone(owner.prepare())
        self.assertIsNone(
            owner.begin_session(
                server_url=f"http://127.0.0.1:{self.h.host_port}", deadline=150.0
            )
        )
        self.h.active()
        driver = self.h.attached_driver()
        self.assertIsNone(owner.bind_session(driver, deadline=150.0))
        return owner, driver

    def scan_owner(self, android_owner: AndroidAutomationOwner) -> ScanAutomationOwner:
        h = self.h
        return ScanAutomationOwner(
            phone=h.phone,
            run_id=h.run_id,
            app_id=h.app_id,
            node="synthetic-node",
            appium_entry=h.root / "nonexistent-appium.js",
            port=h.host_port,
            output=h.root / "scan-automation",
            cwd=h.root,
            deadline=h.deadline,
            activity=".MainActivity",
            android_owner=android_owner,
        )

    def start_scan(
        self,
    ) -> tuple[AndroidAutomationOwner, ScanAutomationOwner, SimpleNamespace]:
        h = self.h
        android_owner = self.owner()
        android_owner.prepare()
        automation = self.scan_owner(android_owner)
        original_remote = h.sdk.remote.side_effect

        def remote(url, *, options, client_config):
            self.assertEqual(url, automation._base_url)
            h.active()
            return original_remote(url, options=options, client_config=client_config)

        h.sdk.remote.side_effect = remote
        driver = automation.start()
        return android_owner, automation, driver

    def deleted(self, owner: AndroidAutomationOwner) -> None:
        self.assertIsNone(owner.session_deleted(HOST_SESSION_ID, deadline=150.0))
        self.h.idle()

    def refused(self, action, pattern: str | None = None) -> Exception:
        # An allowlist AssertionError, missing API TypeError or ImportError must
        # never count as the product's expected fail-closed result.
        with self.assertRaises(
            (RuntimeError, ValueError, TimeoutError, OSError)
        ) as raised:
            action()
        if pattern is not None:
            self.assertRegex(str(raised.exception), pattern)
        return raised.exception

    def observation_action(
        self, phase: str
    ) -> tuple[AndroidAutomationOwner, Callable[[], None]]:
        if phase == "finalize":
            owner, _ = self.bind()
            self.deleted(owner)
            return owner, lambda: owner.finalize(deadline=180.0)
        owner = self.owner()
        if phase == "prepare":
            return owner, owner.prepare
        if phase != "bind":
            raise AssertionError(f"unknown observation phase: {phase}")
        owner.prepare()
        owner.begin_session(
            server_url=f"http://127.0.0.1:{self.h.host_port}", deadline=150.0
        )
        self.h.active()
        driver = self.h.attached_driver()
        return owner, lambda: owner.bind_session(driver, deadline=150.0)

    def test_oem_time_wrapper_supports_prepare_bind_and_finalize(self) -> None:
        for phase in ("prepare", "bind", "finalize"):
            with self.subTest(phase=phase):
                self.new_case()
                owner, action = self.observation_action(phase)
                h = self.h
                h.replies[OS_PROCESSES] = OEM_TIME + h.replies[OS_PROCESSES]
                h.replies[SERVICES] = OEM_TIME + EMPTY_SERVICES
                before = len(h.calls)
                self.assertIsNone(action())
                self.assertIn(OS_PROCESSES, h.calls[before:])
                self.assertIn(SERVICES, h.calls[before:])
                if phase == "prepare":
                    self.require_prepared(owner)
                elif phase == "bind":
                    owner.require_owner(deadline=150.0)
                    h.driver.get_settings.assert_called_once()
                else:
                    owner.require_closed()
                h.assert_phone_retained()

    def test_wrapped_live_observations_are_not_accepted_as_idle(self) -> None:
        for command, reply, pattern in (
            (
                OS_PROCESSES,
                OEM_TIME + active_processes(),
                "pre-existing or residual active instrumentation",
            ),
            (
                SERVICES,
                OEM_TIME + LIVE_SERVICES,
                "unsupported settings services observation",
            ),
        ):
            with self.subTest(command=command):
                self.new_case()
                h = self.h
                h.replies[command] = reply
                self.refused(lambda: self.owner().prepare(), pattern)
                self.assertIn(command, h.calls)
                h.sdk.remote.assert_not_called()
                h.sdk.spawn.assert_not_called()
                h.assert_phone_retained()

    def test_processes_capture_between_one_and_four_mib_supports_each_phase(
        self,
    ) -> None:
        for phase in ("prepare", "bind", "finalize"):
            for size in (1_405_343, 4 * MIB - 1):
                with self.subTest(phase=phase, utf8_bytes=size):
                    self.new_case()
                    owner, action = self.observation_action(phase)
                    h = self.h
                    output = synthetic_processes(size, active=phase == "bind")
                    self.assertEqual(len(output.encode("utf-8")), size)
                    self.assertGreater(size, MIB)
                    self.assertLess(size, 4 * MIB)
                    h.replies[OS_PROCESSES] = output
                    before = len(h.calls)
                    self.assertIsNone(action())
                    self.assertIn(OS_PROCESSES, h.calls[before:])
                    if phase == "prepare":
                        self.require_prepared(owner)
                    elif phase == "bind":
                        owner.require_owner(deadline=150.0)
                        h.driver.get_settings.assert_called_once()
                    else:
                        owner.require_closed()
                    receipts = [
                        json.loads(path.read_bytes())
                        for path in h.output.glob("*-read.json")
                    ]
                    captured = [
                        receipt
                        for receipt in receipts
                        if receipt["argv"]
                        == [str(h.adb), "-s", h.serial, *OS_PROCESSES]
                        and len(receipt["stdout"].encode("utf-8")) == size
                    ]
                    self.assertTrue(captured, "complete processes capture required")
                    for receipt in captured:
                        self.assertEqual(receipt["stdout"], output)
                        self.assertIsNone(receipt["error"])
                    h.assert_phone_retained()

    def test_processes_capture_at_or_above_four_mib_is_explicitly_refused(self) -> None:
        for phase in ("prepare", "bind", "finalize"):
            for size in (4 * MIB, 4 * MIB + 1):
                with self.subTest(phase=phase, utf8_bytes=size):
                    self.new_case()
                    owner, action = self.observation_action(phase)
                    h = self.h
                    output = synthetic_processes(size, active=phase == "bind")
                    self.assertEqual(len(output.encode("utf-8")), size)
                    h.replies[OS_PROCESSES] = output
                    before = len(h.calls)
                    self.refused(action, "Android automation read failed")
                    self.assertIn(OS_PROCESSES, h.calls[before:])
                    self.assertFalse((h.output / "finalized.json").exists())
                    if phase == "finalize":
                        self.refused(owner.require_closed)
                    elif phase == "bind":
                        h.driver.get_settings.assert_not_called()
                    else:
                        h.sdk.remote.assert_not_called()
                    h.assert_phone_retained()

    def test_only_exact_processes_command_gets_the_larger_capture_budget(self) -> None:
        near_processes = (*OS_PROCESSES, SETTINGS)
        replies = {
            SERVICES: history("    synthetic_opaque=" + "x" * MIB + "\n"),
            GLOBAL_SETTINGS: "synthetic_opaque=" + "x" * MIB + "\n",
            near_processes: synthetic_processes(MIB + 1),
        }
        for command, output in replies.items():
            with self.subTest(command=command):
                self.new_case()
                h = self.h
                owner = self.owner()
                owner.prepare()
                self.assertGreater(len(output.encode("utf-8")), MIB)
                self.assertLess(len(output.encode("utf-8")), 4 * MIB)
                # Extend only this exact synthetic read; the strict SDK allowlist
                # still rejects every unexpected or mutating command.
                h.replies[command] = output
                before = len(h.calls)
                self.refused(
                    lambda owner=owner, command=command: owner._command(
                        150.0, *command
                    ),
                    "Android automation read failed",
                )
                self.assertEqual(h.calls[before:], [command])
                h.sdk.remote.assert_not_called()
                h.sdk.spawn.assert_not_called()
                h.assert_phone_retained()

    def test_prepare_collects_real_inventory_and_extra_os_baselines_before_remote(
        self,
    ) -> None:
        h = self.h
        owner = self.owner()
        self.assertIsNone(owner.prepare())
        self.require_prepared(owner)
        receipt = json.loads((h.output / "prepared/result.json").read_bytes())
        self.assertEqual(receipt["status"], "inventory-verified")
        self.assertEqual(receipt["device"], h.serial)
        self.assertEqual(receipt["run_id"], h.run_id)
        self.assertEqual(set(receipt["helpers"]), set(PACKAGES))
        for command in (
            OS_PROCESSES,
            SERVICES,
            STORE,
            DEFAULT_IME,
            ENABLED_IMES,
            GLOBAL_SETTINGS,
        ):
            self.assertIn(command, h.calls)
        self.assertEqual(receipt["observed_mock_location_scopes"], {"package": "allow"})
        self.assertEqual(set(receipt["observed_hidden_api_policy"].values()), {"null"})
        self.assertEqual(owner.system_port, h.system_port)
        self.assertEqual(owner.appium_home, h.appium_home)
        for name, value in (("system_port", 19999), ("appium_home", h.root)):
            with self.assertRaises(AttributeError):
                setattr(owner, name, value)
        h.sdk.remote.assert_not_called()
        h.sdk.spawn.assert_not_called()
        h.assert_phone_retained()
        # A UID scope is allowed only in addition to package:allow, with every
        # observed scope allow; it is pinned rather than collapsed to one mode.
        self.new_case()
        self.h.replies[APPOPS] = (
            "Uid mode: MOCK_LOCATION: allow\nMOCK_LOCATION: allow\n"
        )
        owner = self.owner()
        owner.prepare()
        self.require_prepared(owner)
        self.h.sdk.remote.assert_not_called()

    def test_global_receipts_redact_unrelated_values_but_retain_full_output_digests(
        self,
    ) -> None:
        h = self.h
        idle_output = h.replies[GLOBAL_SETTINGS]
        self.assertIn(GLOBAL_SECRET_MARKER, idle_output)
        owner, _ = self.bind()
        active_output = h.replies[GLOBAL_SETTINGS]
        self.assertIn(GLOBAL_SECRET_MARKER, active_output)
        self.deleted(owner)
        self.assertEqual(h.replies[GLOBAL_SETTINGS], idle_output)
        owner.finalize(deadline=180.0)
        owner.require_closed()

        paths = sorted(h.output.rglob("*.json"))
        self.assertTrue(paths)
        global_receipts = []
        argv = [str(h.adb), "-s", h.serial, *GLOBAL_SETTINGS]
        for path in paths:
            with self.subTest(receipt=path.relative_to(h.output)):
                raw = path.read_text(encoding="utf-8")
                document = json.loads(raw)
                self.assertNotIn(GLOBAL_SECRET_MARKER, raw)
                self.assertNotIn(GLOBAL_SECRET_MARKER, json.dumps(document))
                if document.get("argv") == argv:
                    global_receipts.append(document)

        self.assertEqual(len(global_receipts), h.calls.count(GLOBAL_SETTINGS))
        expected = {
            hashlib.sha256(idle_output.encode()).hexdigest(): "",
            hashlib.sha256(active_output.encode()).hexdigest(): "\n".join(
                f"{policy}=1" for policy in POLICIES
            ),
        }
        self.assertEqual(
            {receipt["stdout_sha256"] for receipt in global_receipts}, set(expected)
        )
        for receipt in global_receipts:
            self.assertEqual(receipt["stdout"], expected[receipt["stdout_sha256"]])
        h.assert_phone_retained()

    def test_external_inventory_and_edited_json_never_supply_live_authority(
        self,
    ) -> None:
        h = self.h
        external = inspect_prepared_android_automation(
            phone=h.phone,
            appium_home=h.appium_home,
            expected_apk_sha256=h.expected,
            system_port=h.system_port,
            output=h.root / "external-inventory",
            cwd=h.root,
            deadline=150.0,
        )
        # Even a genuine inventory copied into the prescribed location cannot be
        # adopted: the owner's output has to be fresh and prepare has to collect.
        (h.output / "prepared").mkdir(parents=True)
        (h.output / "prepared/result.json").write_bytes(external.read_bytes())
        self.refused(lambda: self.owner().prepare())
        h.sdk.remote.assert_not_called()
        self.new_case()
        owner = self.owner()
        self.refused(lambda: self.require_prepared(owner))
        owner.prepare()
        path = self.h.output / "prepared/result.json"
        path.write_bytes(path.read_bytes() + b" ")
        self.refused(lambda: self.require_prepared(owner))
        self.h.sdk.remote.assert_not_called()

    def test_explicit_identity_hash_triplet_and_real_lease_are_required_before_io(
        self,
    ) -> None:
        h = self.h
        cases = (
            {"phone": SimpleNamespace(device=h.serial, run_id=h.run_id)},
            {"run_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"},
            {"app_id": "invalid-package"},
            {"expected_apk_sha256": {SERVER: h.expected[SERVER]}},
            {"expected_apk_sha256": {**h.expected, "foreign.package": "ab" * 32}},
            {"expected_apk_sha256": {**h.expected, SERVER: "AB" * 32}},
            {"system_port": True},
            {"deadline": float("inf")},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                with self.assertRaises((TypeError, ValueError, RuntimeError)):
                    self.owner(**changes)
                self.assertEqual(h.calls, [])
        self.assertFalse(h.output.exists())
        h.sdk.remote.assert_not_called()
        for boundary in ("phone", "run", "app", "deadline"):
            with self.subTest(boundary=boundary):
                self.new_case()
                h = self.h
                owner = self.owner()
                owner.prepare()
                handoff = {
                    "phone": h.phone,
                    "run_id": h.run_id,
                    "app_id": h.app_id,
                    "deadline": 150.0,
                }
                if boundary == "phone":
                    handoff["phone"] = AndroidRadioController(
                        "synthetic-other-phone",
                        h.run_id,
                        h.root / "other-phone.json",
                        lock_root=h.root / "leases",
                    )
                elif boundary == "run":
                    handoff["run_id"] = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
                elif boundary == "app":
                    handoff["app_id"] = "foreign.app"
                else:
                    h.clock = 161.0
                before = len(h.calls)
                self.refused(
                    lambda owner=owner, handoff=handoff: owner.require_prepared(
                        **handoff
                    )
                )
                self.assertEqual(len(h.calls), before)
                h.sdk.remote.assert_not_called()

    def test_prepared_profile_failures_never_reach_remote_or_repair_the_device(
        self,
    ) -> None:
        cases = [
            (APPOPS, "android:mock_location: default\n"),
            (APPOPS, "No operations.\nDefault mode: deny\n"),
            (APPOPS, "No operations.\nDefault mode: allow\n"),
            (APPOPS, "Uid mode: MOCK_LOCATION: allow\n"),
            (APPOPS, "Uid mode: MOCK_LOCATION: deny\nMOCK_LOCATION: allow\n"),
            (STORE, "not JSON\n"),
            (STORE, json.dumps(["com.example.other"]) + "\n"),
            (STORE, json.dumps([SETTINGS, SETTINGS]) + "\n"),
            (STORE, json.dumps({SETTINGS: True}) + "\n"),
            (SERVICES, LIVE_SERVICES),
            (OS_PROCESSES, active_processes()),
            (OS_PROCESSES, ""),
            (DEFAULT_IME, ""),
            (ENABLED_IMES, ""),
        ]
        cases.extend(
            (("shell", "settings", "get", "global", policy), "1\n")
            for policy in POLICIES
        )
        for command, reply in cases:
            with self.subTest(command=command, reply=reply):
                self.new_case()
                self.h.replies[command] = reply
                self.refused(lambda: self.owner().prepare())
                self.h.sdk.remote.assert_not_called()
                self.h.assert_phone_retained()
        self.new_case()
        self.h.returncodes[STORE] = (
            1  # Missing/unreadable file, no root/write fallback.
        )
        self.refused(lambda: self.owner().prepare())
        self.h.assert_phone_retained()

    def test_prepare_rejects_present_null_hidden_keys_even_when_get_reports_null(
        self,
    ) -> None:
        for policy in POLICIES:
            with self.subTest(policy=policy):
                self.new_case()
                h = self.h
                h.replies[GLOBAL_SETTINGS] += f"{policy}=null\n"
                self.assertEqual(
                    h.replies[("shell", "settings", "get", "global", policy)], "null\n"
                )
                owner = self.owner()
                self.refused(owner.prepare)
                self.assertIn(GLOBAL_SETTINGS, h.calls)
                h.sdk.remote.assert_not_called()
                h.sdk.spawn.assert_not_called()
                h.assert_phone_retained()

    def test_begin_repeats_full_idle_preflight_and_never_adopts_a_changed_baseline(
        self,
    ) -> None:
        cases = (
            (BOOT, "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee\n"),
            (FORWARDS, ""),
            (OS_PROCESSES, active_processes()),
            (SERVICES, LIVE_SERVICES),
            (DEFAULT_IME, "foreign.ime/.Keyboard\n"),
            (STORE, json.dumps([SETTINGS]) + "\n"),
        )
        for command, reply in cases:
            with self.subTest(command=command):
                self.new_case()
                owner = self.owner()
                owner.prepare()
                self.h.replies[command] = reply
                self.refused(
                    lambda owner=owner: owner.begin_session(
                        server_url=f"http://127.0.0.1:{self.h.host_port}",
                        deadline=150.0,
                    )
                )
                self.h.sdk.remote.assert_not_called()
        self.new_case()
        owner = self.owner()
        owner.prepare()
        before = len(self.h.calls)
        owner.begin_session(
            server_url=f"http://127.0.0.1:{self.h.host_port}", deadline=150.0
        )
        reads = self.h.calls[before:]
        for command in (
            BOOT,
            PS,
            FORWARDS,
            GLOBAL_SETTINGS,
            OS_PROCESSES,
            SERVICES,
            APPOPS,
            STORE,
            DEFAULT_IME,
            ENABLED_IMES,
        ):
            self.assertIn(command, reads)
        self.refused(lambda owner=owner: owner.require_owner(deadline=150.0))
        self.refused(
            lambda owner=owner: owner.begin_session(
                server_url=f"http://127.0.0.1:{self.h.host_port}", deadline=150.0
            )
        )

    def test_bind_separates_host_and_device_ids_and_brackets_real_server_identity(
        self,
    ) -> None:
        h = self.h
        owner, driver = self.bind()
        self.assertNotEqual(HOST_SESSION_ID, DEVICE_SESSION_ID)
        self.assertEqual(driver.session_id, HOST_SESSION_ID)
        self.assertEqual(h.device_sessions[0]["id"], DEVICE_SESSION_ID)
        self.assertIn((h.system_port, "/sessions"), h.http_calls)
        driver.get_settings.assert_called_once()
        # A successful host proxy command is correlation evidence, NOT an exposed
        # explicit mapping from Appium's host ID to the device's independent UUID.
        self.assertGreaterEqual(h.identity.call_count, 2)
        for call in h.identity.call_args_list:
            self.assertEqual(call.kwargs["package"], SERVER)
            self.assertEqual(call.kwargs["expected_apk_sha256"], h.expected[SERVER])
        for suffix in ("stat", "status", "cmdline"):
            self.assertGreaterEqual(
                h.calls.count(("shell", "cat", f"/proc/3210/{suffix}")), 4
            )
        self.assertIsNone(owner.require_owner(deadline=150.0))
        self.refused(
            lambda owner=owner, driver=driver: owner.bind_session(
                driver, deadline=150.0
            )
        )
        h.assert_phone_retained()

    def status_socket(self, response: dict) -> MagicMock:
        """Replace only the real socket boundary after the bound-session setup."""
        h = self.h
        body = json.dumps(response).encode("utf-8")
        wire = (
            b"HTTP/1.1 200 OK\r\nContent-Length: "
            + str(len(body)).encode("ascii")
            + b"\r\n\r\n"
            + body
        )
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.__exit__.return_value = False
        connection.recv.side_effect = [wire, b""]

        def connect(address, *, timeout):
            self.assertEqual(address, ("127.0.0.1", h.system_port))
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 2)
            return connection

        h.connection.reset_mock()
        h.connection.side_effect = connect
        return connection

    def test_probe_server_status_uses_original_bound_direct_socket_without_verdict(
        self,
    ) -> None:
        for ready in (True, False):
            with self.subTest(ready=ready):
                self.new_case()
                owner, driver = self.bind()
                h = self.h
                response = {"value": {"ready": ready, "build": {"version": "10.7.6"}}}
                connection = self.status_socket(response)
                identity = asdict(owner._server_identity)
                h.replies[("shell", "pidof", SERVER)] = "9999\n"
                before = len(h.calls), h.identity.call_count
                state = (
                    owner._phase,
                    owner._finalized,
                    owner._failure,
                    owner._close_failure,
                    owner._host_session_id,
                    owner._device_session_id,
                )
                started = h.clock
                with patch.dict("os.environ", {"HTTP_PROXY": "http://invalid:9"}):
                    self.assertIsNone(owner.probe_server_status(deadline=150.0))
                h.connection.assert_called_once_with(
                    ("127.0.0.1", h.system_port), timeout=2.0
                )
                connection.sendall.assert_called_once_with(
                    f"GET /status HTTP/1.1\r\nHost: 127.0.0.1:{h.system_port}\r\n"
                    "Connection: close\r\n\r\n".encode("ascii")
                )
                receipt = json.loads((h.output / "server-status.json").read_bytes())
                self.assertEqual(
                    receipt,
                    {
                        "run_id": h.run_id,
                        "host_session_id": HOST_SESSION_ID,
                        "device_session_id": DEVICE_SESSION_ID,
                        "server_identity": identity,
                        "system_port": h.system_port,
                        "endpoint": f"http://127.0.0.1:{h.system_port}/status",
                        "started_monotonic": started,
                        "finished_monotonic": h.clock,
                        "response": response,
                        "error": None,
                    },
                )
                self.assertEqual((len(h.calls), h.identity.call_count), before)
                self.assertEqual(
                    (
                        owner._phase,
                        owner._finalized,
                        owner._failure,
                        owner._close_failure,
                        owner._host_session_id,
                        owner._device_session_id,
                    ),
                    state,
                )
                self.assertIsNone(owner.require_owner(deadline=150.0))
                self.refused(
                    lambda owner=owner: owner.probe_server_status(deadline=150.0),
                    "one-shot",
                )
                self.assertEqual(h.connection.call_count, 1)
                driver.quit.assert_not_called()
                h.assert_phone_retained()

    def test_probe_server_status_refuses_unbound_drift_and_deadline_before_socket(
        self,
    ) -> None:
        for boundary in (
            "new",
            "prepared",
            "requesting",
            "deleted",
            "finalized",
            "missing-identity",
            "changed-identity",
            "receipt",
            "prepared-receipt",
            "missing-receipt",
            "port",
            "device-session",
            "run",
            "lease",
            "deadline",
            "extended",
        ):
            with self.subTest(boundary=boundary):
                self.new_case()
                h = self.h
                if boundary in {"new", "prepared", "requesting"}:
                    owner = self.owner()
                    if boundary != "new":
                        owner.prepare()
                    if boundary == "requesting":
                        owner.begin_session(
                            server_url=f"http://127.0.0.1:{h.host_port}", deadline=150.0
                        )
                else:
                    owner, _ = self.bind()
                    if boundary == "deleted":
                        self.deleted(owner)
                    elif boundary == "finalized":
                        self.refused(lambda owner=owner: owner.finalize(deadline=150.0))
                    elif boundary == "missing-identity":
                        owner._server_identity = None
                    elif boundary == "changed-identity":
                        owner._server_identity = replace(
                            owner._server_identity, pid=9999
                        )
                    elif boundary == "receipt":
                        path = h.output / "bound-session.json"
                        doc = json.loads(path.read_bytes())
                        doc["server_identity"]["pid"] = 9999
                        path.write_text(json.dumps(doc), encoding="utf-8")
                    elif boundary == "prepared-receipt":
                        path = h.output / "prepared-owner.json"
                        doc = json.loads(path.read_bytes())
                        doc["system_port"] += 1
                        path.write_text(json.dumps(doc), encoding="utf-8")
                    elif boundary == "missing-receipt":
                        (h.output / "bound-session.json").unlink()
                    elif boundary == "port":
                        owner._system_port += 1
                    elif boundary == "device-session":
                        owner._device_session_id = (
                            "bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee"
                        )
                    elif boundary == "run":
                        h.phone.run_id = "bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee"
                    elif boundary == "lease":
                        (h.phone.lock / "owner.json").write_bytes(b"foreign")
                    elif boundary == "deadline":
                        h.clock = 151.0
                self.status_socket({"value": {"ready": True}})
                before = len(h.calls), h.identity.call_count
                self.refused(
                    lambda owner=owner, boundary=boundary: owner.probe_server_status(
                        deadline=170.0 if boundary == "extended" else 150.0
                    )
                )
                h.connection.assert_not_called()
                self.assertFalse((h.output / "server-status.json").exists())
                self.assertEqual((len(h.calls), h.identity.call_count), before)

    def test_probe_server_status_timeout_consumes_attempt_and_two_second_budget(
        self,
    ) -> None:
        for budget in (2.0, 0.25):
            with self.subTest(budget=budget):
                self.new_case()
                owner, driver = self.bind()
                h = self.h
                connection = self.status_socket({})
                failure = TimeoutError("fresh status read timed out")
                started = h.clock

                def timeout(_, *, h=h, budget=budget, failure=failure):
                    h.clock += budget
                    raise failure

                connection.recv.side_effect = timeout
                with self.assertRaises(TimeoutError) as raised:
                    owner.probe_server_status(
                        deadline=started + budget + (5 if budget == 2 else 0)
                    )
                h.connection.assert_called_once_with(
                    ("127.0.0.1", h.system_port), timeout=budget
                )
                connection.recv.assert_called_once()
                receipt = json.loads((h.output / "server-status.json").read_bytes())
                self.assertIsNone(receipt["response"])
                self.assertEqual(receipt["finished_monotonic"] - started, budget)
                self.assertIn("timed out", json.dumps(receipt["error"]))
                if budget == 2:
                    self.assertIs(raised.exception, failure)
                self.assertEqual(owner._phase, "bound")
                self.assertFalse(owner._finalized)
                self.assertIsNone(owner._failure)
                self.assertIsNone(owner._close_failure)
                h.clock = started
                self.refused(
                    lambda owner=owner: owner.probe_server_status(deadline=150.0),
                    "one-shot",
                )
                self.assertEqual(h.connection.call_count, 1)
                driver.quit.assert_not_called()
                h.assert_phone_retained()

    def test_probe_server_status_guard_failure_stays_primary_and_keeps_response(
        self,
    ) -> None:
        owner, _ = self.bind()
        h = self.h
        response = {"value": {"ready": False}}
        connection = self.status_socket(response)

        def drift(_):
            path = h.output / "bound-session.json"
            path.write_text("{}", encoding="utf-8")

        connection.sendall.side_effect = drift
        self.refused(
            lambda: owner.probe_server_status(deadline=150.0), "evidence changed"
        )
        receipt = json.loads((h.output / "server-status.json").read_bytes())
        self.assertEqual(receipt["response"], response)
        self.assertIn("evidence changed", receipt["error"]["message"])
        self.assertEqual(owner._phase, "bound")
        self.assertFalse(owner._finalized)
        self.assertIsNone(owner._close_failure)
        self.assertEqual(h.connection.call_count, 1)

    def test_probe_server_status_http_error_cannot_hide_post_read_selector_drift(
        self,
    ) -> None:
        owner, _ = self.bind()
        h = self.h
        connection = self.status_socket({})

        def drift_and_timeout(_):
            (h.output / "bound-session.json").write_text("{}", encoding="utf-8")
            raise TimeoutError("original HTTP read timed out")

        connection.recv.side_effect = drift_and_timeout
        error = self.refused(
            lambda: owner.probe_server_status(deadline=150.0), "evidence changed"
        )
        self.assertIsInstance(error, RuntimeError)
        self.assertIn("original HTTP read timed out", "\n".join(error.__notes__))
        receipt = json.loads((h.output / "server-status.json").read_bytes())
        self.assertIsNone(receipt["response"])
        self.assertEqual(receipt["error"]["type"], "RuntimeError")
        self.assertIn(
            "original HTTP read timed out", "\n".join(receipt["error"]["notes"])
        )
        self.refused(lambda: owner.probe_server_status(deadline=150.0), "one-shot")
        self.assertEqual(h.connection.call_count, 1)
        self.assertEqual(owner._phase, "bound")
        self.assertFalse(owner._finalized)
        self.assertIsNone(owner._close_failure)

    def test_probe_server_status_publication_failure_is_primary_and_never_overwrites(
        self,
    ) -> None:
        owner, _ = self.bind()
        h = self.h
        connection = self.status_socket({})
        connection.recv.side_effect = TimeoutError("HTTP read timed out")
        path = h.output / "server-status.json"
        path.write_bytes(b"foreign receipt")
        error = self.refused(lambda: owner.probe_server_status(deadline=150.0), "exist")
        self.assertIn("HTTP read timed out", "\n".join(error.__notes__))
        self.assertEqual(path.read_bytes(), b"foreign receipt")
        self.assertEqual(owner._phase, "bound")
        self.assertIsNone(owner._failure)
        self.assertIsNone(owner._close_failure)
        self.refused(lambda: owner.probe_server_status(deadline=150.0), "one-shot")
        self.assertEqual(h.connection.call_count, 1)

    def test_probe_server_status_http_interrupt_survives_guard_or_publication_error(
        self,
    ) -> None:
        for interrupt_type in (KeyboardInterrupt, SystemExit, GeneratorExit):
            for failure in ("deadline", "ownership", "publication"):
                with self.subTest(interrupt=interrupt_type.__name__, failure=failure):
                    self.new_case()
                    owner, driver = self.bind()
                    h = self.h
                    connection = self.status_socket({})
                    interrupt = interrupt_type()
                    started = h.clock
                    identity = owner._server_identity

                    def interrupted_read(
                        _, *, h=h, failure=failure, interrupt=interrupt
                    ):
                        if failure == "deadline":
                            h.clock = 151.0
                        elif failure == "ownership":
                            h.phone.run_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
                        raise interrupt

                    connection.recv.side_effect = interrupted_read
                    with patch(
                        "tools.react_native.bleng_android_session.publish_new_json",
                        wraps=publish_new_json,
                    ) as publication:
                        if failure == "publication":
                            publication.side_effect = OSError("receipt disk failure")
                        with self.assertRaises(interrupt_type) as raised:
                            owner.probe_server_status(deadline=150.0)
                        publication.assert_called_once()
                    self.assertIs(raised.exception, interrupt)
                    expected_type = {
                        "deadline": "TimeoutError",
                        "ownership": "RuntimeError",
                        "publication": "OSError",
                    }[failure]
                    self.assertIn(expected_type + ":", "\n".join(interrupt.__notes__))
                    path = h.output / "server-status.json"
                    if failure == "publication":
                        self.assertFalse(path.exists())
                    else:
                        receipt = json.loads(path.read_bytes())
                        self.assertEqual(
                            receipt["error"]["type"], interrupt_type.__name__
                        )
                        self.assertIn(
                            expected_type + ":", "\n".join(receipt["error"]["notes"])
                        )
                    self.assertEqual(owner._phase, "bound")
                    self.assertFalse(owner._finalized)
                    self.assertIsNone(owner._close_failure)
                    self.assertIs(owner._server_identity, identity)
                    self.assertEqual(owner._host_session_id, HOST_SESSION_ID)
                    self.assertEqual(owner._device_session_id, DEVICE_SESSION_ID)
                    h.clock = started
                    self.refused(
                        lambda owner=owner: owner.probe_server_status(deadline=150.0),
                        "one-shot",
                    )
                    self.assertEqual(h.connection.call_count, 1)
                    connection.recv.assert_called_once()
                    driver.quit.assert_not_called()

    def test_probe_server_status_publication_interrupt_survives_ordinary_boundary(
        self,
    ) -> None:
        owner, driver = self.bind()
        h = self.h
        connection = self.status_socket({"value": {"ready": False}})

        def lose_owner(_):
            h.phone.run_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

        connection.sendall.side_effect = lose_owner
        interrupt = KeyboardInterrupt()
        with patch(
            "tools.react_native.bleng_android_session.publish_new_json",
            side_effect=interrupt,
        ) as publication:
            with self.assertRaises(KeyboardInterrupt) as raised:
                owner.probe_server_status(deadline=150.0)
            publication.assert_called_once()
        self.assertIs(raised.exception, interrupt)
        self.assertIn("RuntimeError:", "\n".join(interrupt.__notes__))
        self.assertFalse((h.output / "server-status.json").exists())
        self.assertEqual(owner._phase, "bound")
        self.assertFalse(owner._finalized)
        self.assertIsNone(owner._close_failure)
        self.refused(lambda: owner.probe_server_status(deadline=150.0), "one-shot")
        self.assertEqual(h.connection.call_count, 1)
        driver.quit.assert_not_called()

    def adb_sockets(self) -> None:
        h = self.h
        h.adb_responses = [
            b"OKAY00040029",
            b"OKAY0006device",
            b"OKAY",
            b"OKAY" + h.boot_id.encode("ascii") + b"\n",
        ]
        h.connection.reset_mock()

    def adb_receipt(self) -> dict:
        return json.loads((self.h.output / "adb-transport.json").read_bytes())

    def test_adb_transport_partial_frames_exact_readonly_services_and_receipt(
        self,
    ) -> None:
        owner, driver = self.bind()
        h = self.h
        self.adb_sockets()
        before = len(h.calls), h.identity.call_count
        with patch.dict(
            "os.environ",
            {
                "ADB_SERVER_SOCKET": "tcp:foreign:9999",
                "ANDROID_ADB_SERVER_PORT": "9999",
            },
        ):
            self.assertIsNone(owner.probe_adb_transport(deadline=150.0))
        self.assertEqual(h.connection.call_count, 3)
        for call in h.connection.call_args_list:
            self.assertEqual(call.args, (("127.0.0.1", 5037),))
            self.assertEqual(call.kwargs, {"timeout": 3.0})
        self.assertEqual(
            h.adb_calls,
            [
                b"host:version",
                f"host-serial:{h.serial}:get-state".encode(),
                f"host:transport:{h.serial}".encode(),
                b"shell:cat /proc/sys/kernel/random/boot_id",
            ],
        )
        self.assertTrue(all(s.closed and not s.requests for s in h.adb_sockets))
        receipt = self.adb_receipt()
        self.assertEqual(receipt["run_id"], h.run_id)
        self.assertEqual(receipt["device"], h.serial)
        self.assertEqual(receipt["server_identity"], asdict(owner._server_identity))
        self.assertEqual(receipt["host_session_id"], HOST_SESSION_ID)
        self.assertEqual(receipt["device_session_id"], DEVICE_SESSION_ID)
        self.assertEqual(receipt["budget_seconds"], 3.0)
        self.assertEqual(receipt["expected_boot_id"], h.boot_id)
        self.assertIsNone(receipt["error"])
        self.assertIn("ownership", " ".join(receipt["limitations"]))
        self.assertEqual(
            [stage["name"] for stage in receipt["stages"]],
            [
                "server_version",
                "cached_state",
                "transport_select",
                "service_accept",
                "boot_id",
            ],
        )
        self.assertEqual(
            [stage["value"] for stage in receipt["stages"]],
            ["0029", "device", "OKAY", "OKAY", h.boot_id],
        )
        for stage in receipt["stages"]:
            self.assertIsNone(stage["error"])
            self.assertTrue(stage["output"])
            self.assertLessEqual(
                stage["started_monotonic"], stage["finished_monotonic"]
            )
        self.assertEqual((len(h.calls), h.identity.call_count), before)
        self.refused(lambda: owner.probe_adb_transport(deadline=150.0), "one-shot")
        self.assertEqual(h.connection.call_count, 3)
        self.assertIsNone(owner._failure)
        self.assertIsNone(owner._close_failure)
        driver.quit.assert_not_called()
        h.assert_phone_retained()

    def test_adb_transport_refuses_phase_identity_session_lease_and_receipt_drift(
        self,
    ) -> None:
        for drift in (
            "new",
            "prepared",
            "requesting",
            "deleted",
            "finalized",
            "identity",
            "missing-selector",
            "selector",
            "host-session",
            "device-session",
            "port",
            "device",
            "run",
            "lease",
            "receipt",
            "deadline",
            "extended",
        ):
            with self.subTest(drift=drift):
                self.new_case()
                h = self.h
                if drift in {"new", "prepared", "requesting"}:
                    owner = self.owner()
                    if drift != "new":
                        owner.prepare()
                    if drift == "requesting":
                        owner.begin_session(
                            server_url=f"http://127.0.0.1:{h.host_port}", deadline=150.0
                        )
                else:
                    owner, _ = self.bind()
                    if drift == "deleted":
                        self.deleted(owner)
                    elif drift == "finalized":
                        owner._finalized = True
                    elif drift == "identity":
                        owner._server_identity = replace(
                            owner._server_identity, pid=9999
                        )
                    elif drift == "missing-selector":
                        owner._bound_server_selector = None
                    elif drift == "selector":
                        owner._bound_server_selector = (None, None, None, None)
                    elif drift == "host-session":
                        owner._host_session_id = "foreign"
                    elif drift == "device-session":
                        owner._device_session_id = "foreign"
                    elif drift == "port":
                        owner._system_port += 1
                    elif drift == "device":
                        h.phone.device = "foreign"
                    elif drift == "run":
                        h.phone.run_id = "foreign"
                    elif drift == "lease":
                        (h.phone.lock / "owner.json").write_bytes(b"foreign")
                    elif drift == "receipt":
                        (h.output / "bound-session.json").write_bytes(b"{}")
                    elif drift == "deadline":
                        h.clock = 151.0
                self.adb_sockets()
                self.refused(
                    lambda owner=owner, drift=drift: owner.probe_adb_transport(
                        deadline=170.0 if drift == "extended" else 150.0
                    )
                )
                h.connection.assert_not_called()
                self.assertFalse((h.output / "adb-transport.json").exists())

    def test_adb_transport_protocol_failures_are_bounded_failclosed_and_one_shot(
        self,
    ) -> None:
        cases = [
            (0, b"", "server_version", "EOF"),
            (0, b"OK", "server_version", "EOF"),
            (0, b"NOPE", "server_version", "status"),
            (0, b"OKAYzzzz", "server_version", "length"),
            (0, b"OKAY0041", "server_version", "limit"),
            (0, b"OKAYffff", "server_version", "limit"),
            (0, b"OKAY000400", "server_version", "EOF"),
            (0, b"OKAY0004nope", "server_version", "version"),
            (0, b"FAIL0004oops", "server_version", "oops"),
            (0, b"FAIL0041", "server_version", "limit"),
            (1, b"OKAY0007offline", "cached_state", "device"),
            (1, b"OKAY0000", "cached_state", "device"),
            (1, b"OKAY0041", "cached_state", "limit"),
            (2, b"FAIL0004gone", "transport_select", "gone"),
            (3, b"FAIL0004deny", "service_accept", "deny"),
            (3, b"OKAY", "boot_id", "boot"),
            (3, b"OKAY" + b"x" * 129, "boot_id", "limit"),
            (3, b"OKAY" + b"0" * 36 + b"\n", "boot_id", "boot"),
            (3, b"OKAYaaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee\n", "boot_id", "expected"),
            (3, b"OKAY11111111-2222-4333-8444-555555555555\nextra", "boot_id", "boot"),
        ]
        for index, reply, stage_name, message in cases:
            with self.subTest(stage=stage_name, reply=reply):
                self.new_case()
                owner, _ = self.bind()
                h = self.h
                self.adb_sockets()
                h.adb_responses[index] = reply
                self.refused(
                    lambda owner=owner: owner.probe_adb_transport(deadline=150.0),
                    message,
                )
                receipt = self.adb_receipt()
                failed = next(s for s in receipt["stages"] if s["name"] == stage_name)
                self.assertIsNotNone(failed["error"])
                self.assertIsNotNone(receipt["error"])
                self.assertEqual(len(h.adb_calls), index + 1)
                self.assertTrue(all(s.closed for s in h.adb_sockets))
                self.assertIsNone(owner._failure)
                self.assertIsNone(owner._close_failure)
                before = h.connection.call_count
                self.refused(
                    lambda owner=owner: owner.probe_adb_transport(deadline=150.0),
                    "one-shot",
                )
                self.assertEqual(h.connection.call_count, before)

    def test_adb_transport_global_budget_renews_every_io_without_retry(self) -> None:
        for budget in (3.0, 0.25):
            with self.subTest(budget=budget):
                self.new_case()
                owner, _ = self.bind()
                h = self.h
                self.adb_sockets()
                started = h.clock
                h.adb_chunk_size = 128

                def tick(operation, *, h=h):
                    if operation != "connect":
                        h.clock += min(0.25, h.adb_sockets[-1].timeouts[-1])

                h.adb_after_io = tick
                self.refused(
                    lambda owner=owner, started=started, budget=budget: (
                        owner.probe_adb_transport(
                            deadline=started + budget + (5 if budget == 3 else 0)
                        )
                    ),
                    "deadline",
                )
                receipt = self.adb_receipt()
                self.assertEqual(receipt["budget_seconds"], budget)
                self.assertEqual(receipt["finished_monotonic"] - started, budget)
                timeouts = [t for s in h.adb_sockets for t in s.timeouts]
                self.assertEqual(timeouts, sorted(timeouts, reverse=True))
                self.assertEqual(timeouts[0], budget)
                self.assertEqual(len(h.adb_sockets), 3 if budget == 3 else 1)
                for index, s in enumerate(h.adb_sockets):
                    self.assertEqual(
                        len(s.timeouts), len(s.read_sizes) + (2 if index == 2 else 1)
                    )
                self.assertIsNone(owner._failure)
                h.clock = started
                self.refused(
                    lambda owner=owner: owner.probe_adb_transport(deadline=150.0),
                    "one-shot",
                )

    def test_adb_transport_connection_refusal_never_launches_sdk(self) -> None:
        owner, _ = self.bind()
        h = self.h
        self.adb_sockets()
        before = len(h.calls)
        h.connection.side_effect = ConnectionRefusedError("no existing daemon")
        self.refused(
            lambda: owner.probe_adb_transport(deadline=150.0), "no existing daemon"
        )
        h.connection.assert_called_once()
        self.assertEqual(len(h.calls), before)
        self.assertEqual(
            self.adb_receipt()["stages"][0]["error"]["type"], "ConnectionRefusedError"
        )
        self.assertIsNone(owner._failure)

    def test_adb_transport_postguard_publication_and_interrupt_precedence(self) -> None:
        for io_error in (OSError, KeyboardInterrupt, SystemExit, GeneratorExit):
            for boundary in (
                "none",
                "selector",
                "receipt",
                "publication",
                "both",
                "publication-interrupt",
            ):
                with self.subTest(io=io_error.__name__, boundary=boundary):
                    self.new_case()
                    owner, _ = self.bind()
                    h = self.h
                    self.adb_sockets()
                    failure = io_error("read failed")

                    def fail(
                        operation,
                        *,
                        boundary=boundary,
                        owner=owner,
                        h=h,
                        failure=failure,
                    ):
                        if operation != "recv":
                            return
                        if boundary in {"selector", "both", "publication-interrupt"}:
                            owner._device_session_id = "foreign"
                        elif boundary == "receipt":
                            (h.output / "bound-session.json").write_bytes(b"{}")
                        raise failure

                    h.adb_after_io = fail
                    publication_error = (
                        KeyboardInterrupt("publication interrupted")
                        if boundary == "publication-interrupt"
                        else OSError("disk failed")
                    )
                    with patch(
                        "tools.react_native.bleng_android_session.publish_new_json",
                        wraps=publish_new_json,
                    ) as publication:
                        if boundary in {"publication", "both", "publication-interrupt"}:
                            publication.side_effect = publication_error
                        expected = (
                            io_error
                            if io_error is not OSError
                            else (
                                KeyboardInterrupt
                                if boundary == "publication-interrupt"
                                else RuntimeError
                                if boundary in {"selector", "receipt", "both"}
                                else OSError
                            )
                        )
                        with self.assertRaises(expected) as raised:
                            owner.probe_adb_transport(deadline=150.0)
                        publication.assert_called_once()
                    if io_error is not OSError:
                        self.assertIs(raised.exception, failure)
                    elif boundary == "publication-interrupt":
                        self.assertIs(raised.exception, publication_error)
                    if boundary != "none":
                        self.assertTrue(raised.exception.__notes__)
                    if boundary not in {"publication", "both", "publication-interrupt"}:
                        receipt = self.adb_receipt()
                        self.assertEqual(
                            receipt["stages"][0]["error"]["type"], io_error.__name__
                        )
                        self.assertEqual(receipt["error"]["type"], expected.__name__)
                    self.refused(
                        lambda owner=owner: owner.probe_adb_transport(deadline=150.0),
                        "one-shot",
                    )
                    self.assertEqual(h.connection.call_count, 1)
                    self.assertIsNone(owner._close_failure)

    def test_adb_transport_successful_read_still_checks_postguard_and_no_overwrite(
        self,
    ) -> None:
        for mode in ("drift", "existing"):
            with self.subTest(mode=mode):
                self.new_case()
                owner, _ = self.bind()
                h = self.h
                self.adb_sockets()
                path = h.output / "adb-transport.json"
                if mode == "existing":
                    path.write_bytes(b"foreign receipt")
                else:

                    def drift(operation, *, h=h, owner=owner):
                        if (
                            operation == "recv"
                            and len(h.adb_calls) == 4
                            and not h.adb_sockets[-1].response
                        ):
                            owner._device_session_id = "foreign"

                    h.adb_after_io = drift
                self.refused(
                    lambda owner=owner: owner.probe_adb_transport(deadline=150.0),
                    "selector" if mode == "drift" else "exist",
                )
                if mode == "existing":
                    self.assertEqual(path.read_bytes(), b"foreign receipt")
                else:
                    self.assertEqual(
                        self.adb_receipt()["stages"][-1]["value"], h.boot_id
                    )
                self.refused(
                    lambda owner=owner: owner.probe_adb_transport(deadline=150.0),
                    "one-shot",
                )
                self.assertEqual(h.connection.call_count, 3)

    def test_capture_server_logs_pins_original_pid_without_refresh_or_verdict(
        self,
    ) -> None:
        owner, driver = self.bind()
        h = self.h
        args = (
            "logcat",
            "-d",
            "-v",
            "threadtime",
            "--pid",
            "3210",
            "-t",
            "1000",
            "appium:I",
            "*:S",
        )
        # A changed live PID must not cause a fresh selection or any shell read.
        h.replies[("shell", "pidof", SERVER)] = "3211\n"
        h.replies[args] = "synthetic: no screenshot response; not a verdict\n"
        before = len(h.calls), len(h.http_calls), h.identity.call_count
        records = set(h.output.glob("*-read.json"))
        state = owner._phase, owner._finalized, owner._failure, owner._close_failure
        self.assertIsNone(owner.capture_server_logs(deadline=h.clock + 0.25))
        self.assertEqual(h.calls[before[0] :], [args])
        self.assertEqual((len(h.http_calls), h.identity.call_count), before[1:])
        self.assertEqual(
            (owner._phase, owner._finalized, owner._failure, owner._close_failure),
            state,
        )
        created = set(h.output.glob("*-read.json")) - records
        self.assertEqual(len(created), 1)
        receipt = json.loads(created.pop().read_bytes())
        self.assertEqual(
            receipt,
            {
                "argv": [str(h.adb), "-s", h.serial, *args],
                "stdout": h.replies[args],
                "returncode": 0,
                "error": None,
            },
        )
        self.assertLessEqual(h.command.call_args.kwargs["timeout"], 0.25)
        self.refused(lambda: owner.capture_server_logs(deadline=150.0), "one-shot")
        self.assertEqual(h.calls[before[0] :], [args])
        driver.get_settings.assert_called_once()
        driver.quit.assert_not_called()
        h.assert_phone_retained()

    def test_capture_server_logs_refuses_scope_and_receipt_drift_before_io(
        self,
    ) -> None:
        for boundary in (
            "new",
            "prepared",
            "requesting",
            "deleted",
            "finalized",
            "identity",
            "receipt",
            "run",
            "lease",
            "deadline",
            "extended",
        ):
            with self.subTest(boundary=boundary):
                self.new_case()
                h = self.h
                if boundary in {"new", "prepared", "requesting"}:
                    owner = self.owner()
                    if boundary != "new":
                        owner.prepare()
                    if boundary == "requesting":
                        owner.begin_session(
                            server_url=f"http://127.0.0.1:{h.host_port}", deadline=150.0
                        )
                else:
                    owner, _ = self.bind()
                    if boundary == "deleted":
                        self.deleted(owner)
                    elif boundary == "finalized":
                        self.refused(lambda owner=owner: owner.finalize(deadline=150.0))
                    elif boundary == "identity":
                        owner._server_identity = None
                    elif boundary == "receipt":
                        path = h.output / "bound-session.json"
                        doc = json.loads(path.read_bytes())
                        doc["server_identity"]["pid"] = 3211
                        path.write_text(json.dumps(doc), encoding="utf-8")
                    elif boundary == "run":
                        h.phone.run_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
                    elif boundary == "lease":
                        (h.phone.lock / "owner.json").write_bytes(b"foreign owner")
                    elif boundary == "deadline":
                        h.clock = 161.0
                before = len(h.calls), len(h.http_calls), h.identity.call_count
                self.refused(
                    lambda owner=owner, boundary=boundary: owner.capture_server_logs(
                        deadline=170.0 if boundary == "extended" else 150.0
                    )
                )
                self.assertEqual(
                    (len(h.calls), len(h.http_calls), h.identity.call_count), before
                )

    def test_capture_server_logs_failed_read_retains_receipt_and_never_replays(
        self,
    ) -> None:
        for failure in ("timeout", "exit", "oversized", "post-guard"):
            with self.subTest(failure=failure):
                self.new_case()
                owner, driver = self.bind()
                h = self.h
                args = (
                    "logcat",
                    "-d",
                    "-v",
                    "threadtime",
                    "--pid",
                    "3210",
                    "-t",
                    "1000",
                    "appium:I",
                    "*:S",
                )
                h.replies[args] = "partial diagnostic\n"
                records = set(h.output.glob("*-read.json"))
                if failure == "timeout":
                    original = h.command.side_effect

                    def timeout(
                        command, *, h=h, args=args, original=original, **kwargs
                    ):
                        if tuple(command[3:]) == args:
                            h.calls.append(args)
                            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
                        return original(command, **kwargs)

                    h.command.side_effect = timeout
                elif failure == "exit":
                    h.returncodes[args] = 1
                elif failure == "oversized":
                    h.replies[args] = "x" * (MIB + 1)
                else:
                    h.after_command = lambda _, h=h: setattr(h, "clock", 151.0)
                before = len(h.calls), len(h.http_calls), h.identity.call_count
                self.refused(
                    lambda owner=owner: owner.capture_server_logs(deadline=150.0)
                )
                created = set(h.output.glob("*-read.json")) - records
                self.assertEqual(len(created), 1)
                receipt = json.loads(created.pop().read_bytes())
                self.assertEqual(receipt["argv"], [str(h.adb), "-s", h.serial, *args])
                self.assertTrue(receipt["error"])
                if failure == "exit":
                    self.assertEqual(receipt["stdout"], h.replies[args])
                    self.assertEqual(receipt["returncode"], 1)
                h.clock = 100.0
                self.refused(
                    lambda owner=owner: owner.capture_server_logs(deadline=150.0),
                    "one-shot",
                )
                self.assertEqual(h.calls[before[0] :], [args])
                self.assertEqual((len(h.http_calls), h.identity.call_count), before[1:])
                self.assertEqual(owner._phase, "bound")
                self.assertFalse(owner._finalized)
                self.assertIsNone(owner._close_failure)
                driver.quit.assert_not_called()
                h.assert_phone_retained()

    def test_bind_rejects_wrong_target_package_even_with_exact_server_process(
        self,
    ) -> None:
        h = self.h
        owner = self.owner()
        owner.prepare()
        owner.begin_session(
            server_url=f"http://127.0.0.1:{self.h.host_port}", deadline=150.0
        )
        h.active()
        h.replies[OS_PROCESSES] = active_processes(target_package="foreign.target")
        instruments = parse_active_instrumentation(h.replies[OS_PROCESSES])
        self.assertEqual(len(instruments), 1)
        self.assertEqual(instruments[0]["process_name"], SERVER)
        self.assertEqual(instruments[0]["target_package"], "foreign.target")
        self.assertEqual(instruments[0]["component"], COMPONENT)
        self.assertEqual(instruments[0]["pid"], 3210)
        self.assertEqual(instruments[0]["uid"], h.uids[SERVER])
        driver = h.attached_driver()
        self.refused(
            lambda: owner.bind_session(driver, deadline=150.0), "active instrumentation"
        )
        driver.get_settings.assert_not_called()
        self.refused(lambda: owner.require_owner(deadline=150.0))
        h.assert_phone_retained()

    def test_bind_requires_one_exact_unfinished_instrumentation_matching_real_pid_uid(
        self,
    ) -> None:
        wrong_component = active_processes().replace(
            COMPONENT, "foreign.test/other.Runner"
        )
        cases = (
            processes(),
            wrong_component,
            active_processes(pid=3211),
            active_processes(uid=10103),
            processes(
                "  Active instrumentation:\n" + record(app_id=102, finished=True)
            ),
            processes(
                "  Active instrumentation:\n"
                + record(app_id=102)
                + record(index=1, pid=3211, app_id=102)
            ),
        )
        for output in cases:
            with self.subTest(output=output):
                self.new_case()
                owner = self.owner()
                owner.prepare()
                owner.begin_session(
                    server_url=f"http://127.0.0.1:{self.h.host_port}", deadline=150.0
                )
                self.h.active()
                self.h.replies[OS_PROCESSES] = output
                driver = self.h.attached_driver()
                self.refused(
                    lambda owner=owner, driver=driver: owner.bind_session(
                        driver, deadline=150.0
                    )
                )
                self.refused(lambda owner=owner: owner.require_owner(deadline=150.0))
                before = len(self.h.calls)
                self.refused(
                    lambda owner=owner, driver=driver: owner.bind_session(
                        driver, deadline=150.0
                    )
                )
                self.assertEqual(len(self.h.calls), before)
                self.h.assert_phone_retained()

    def test_bind_rejects_boot_pid_reuse_forward_and_device_session_drift_across_proxy(
        self,
    ) -> None:
        def change_boot(h):
            h.replies[BOOT] = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee\n"

        def change_ticks(h):
            h.replies[("shell", "cat", "/proc/3210/stat")] = proc_stat(3210, 701)

        def change_pid(h):
            h.replies[("shell", "pidof", SERVER)] = "3211\n"
            h.replies[OS_PROCESSES] = active_processes(pid=3211)
            h.replies[PS] = f"PID UID ARGS\n3211 10102 {SERVER}\n"

        def change_forward(h):
            h.replies[FORWARDS] = h.replies[FORWARDS].replace("tcp:6790", "tcp:6791")

        def change_unrelated_forward(h):
            h.replies[FORWARDS] = h.replies[FORWARDS].replace("tcp:9000", "tcp:9001")

        def change_session(h):
            h.device_sessions[0]["id"] = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"

        for mutate in (
            change_boot,
            change_pid,
            change_ticks,
            change_forward,
            change_unrelated_forward,
            change_session,
        ):
            with self.subTest(mutate=mutate.__name__):
                self.new_case()
                h = self.h
                owner = self.owner()
                owner.prepare()
                owner.begin_session(
                    server_url=f"http://127.0.0.1:{self.h.host_port}", deadline=150.0
                )
                h.active()
                driver = h.attached_driver()

                def proxy(mutate=mutate, h=h):
                    mutate(h)
                    return {"waitForIdleTimeout": 1000}

                driver.get_settings.side_effect = proxy
                self.refused(
                    lambda owner=owner, driver=driver: owner.bind_session(
                        driver, deadline=150.0
                    )
                )
                driver.get_settings.assert_called_once()
                self.refused(lambda owner=owner: owner.require_owner(deadline=150.0))
                h.assert_phone_retained()

    def test_cli_forward_terminator_is_handled_in_idle_active_and_final_reads(self):
        owner = self.owner()
        self.h.replies[FORWARDS] += "\n"
        owner.prepare()
        owner.begin_session(
            server_url=f"http://127.0.0.1:{self.h.host_port}", deadline=150.0
        )
        self.h.active()
        self.h.replies[FORWARDS] += "\n"
        owner.bind_session(self.h.attached_driver(), deadline=150.0)
        self.deleted(owner)
        self.h.replies[FORWARDS] += "\n"
        owner.finalize(deadline=180.0)
        owner.require_closed()
        self.h.assert_phone_retained()

    def test_active_profile_preserves_scope_store_ime_services_and_all_policy_values(
        self,
    ) -> None:
        cases = [
            (APPOPS, "Uid mode: MOCK_LOCATION: allow\nMOCK_LOCATION: allow\n"),
            (APPOPS, "android:mock_location: ignore\n"),
            (STORE, json.dumps([SETTINGS]) + "\n"),
            (DEFAULT_IME, "io.appium.settings/.UnicodeIME\n"),
            (
                ENABLED_IMES,
                "com.android.inputmethod.latin/.LatinIME:io.appium.settings/.UnicodeIME\n",
            ),
            (SERVICES, LIVE_SERVICES),
        ]
        cases.extend(
            (("shell", "settings", "get", "global", policy), "null\n")
            for policy in POLICIES
        )
        for command, reply in cases:
            with self.subTest(command=command):
                self.new_case()
                owner = self.owner()
                owner.prepare()
                owner.begin_session(
                    server_url=f"http://127.0.0.1:{self.h.host_port}", deadline=150.0
                )
                self.h.active()
                self.h.replies[command] = reply
                driver = self.h.attached_driver()
                self.refused(
                    lambda owner=owner, driver=driver: owner.bind_session(
                        driver, deadline=150.0
                    )
                )
                self.h.assert_phone_retained()

        for boundary in ("no-session", "two-sessions", "non-uuid", "proxy-failure"):
            with self.subTest(boundary=boundary):
                self.new_case()
                owner = self.owner()
                owner.prepare()
                owner.begin_session(
                    server_url=f"http://127.0.0.1:{self.h.host_port}", deadline=150.0
                )
                self.h.active()
                driver = self.h.attached_driver()
                if boundary == "no-session":
                    self.h.device_sessions = []
                elif boundary == "two-sessions":
                    self.h.device_sessions.append(
                        {
                            "id": "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff",
                            "capabilities": {},
                        }
                    )
                elif boundary == "non-uuid":
                    self.h.device_sessions[0]["id"] = HOST_SESSION_ID
                else:
                    driver.get_settings.side_effect = RuntimeError(
                        "proxy request failed"
                    )
                self.refused(
                    lambda owner=owner, driver=driver: owner.bind_session(
                        driver, deadline=150.0
                    )
                )
                self.h.assert_phone_retained()

    def test_bind_rejects_foreign_endpoint_before_any_device_or_proxy_read(
        self,
    ) -> None:
        for foreign in (
            f"http://127.0.0.1:{self.h.host_port + 1}",
            f"http://127.0.0.1:{self.h.host_port}/foreign-route",
        ):
            with self.subTest(foreign=foreign):
                self.new_case()
                h = self.h
                owner = self.owner()
                owner.prepare()
                owner.begin_session(
                    server_url=f"http://127.0.0.1:{self.h.host_port}", deadline=150.0
                )
                h.active()
                driver = h.attached_driver()
                config = driver.command_executor.client_config
                config.remote_server_addr = foreign
                before = (len(h.calls), len(h.http_calls))
                self.refused(
                    lambda owner=owner, driver=driver: owner.bind_session(
                        driver, deadline=150.0
                    )
                )
                self.assertEqual((len(h.calls), len(h.http_calls)), before)
                driver.get_settings.assert_not_called()
                h.assert_phone_retained()

    def test_owner_latches_in_place_endpoint_drift_after_restore_without_device_io(
        self,
    ) -> None:
        owner, driver = self.bind()
        h = self.h
        executor = driver.command_executor
        config = executor.client_config
        endpoint = config.remote_server_addr
        before = (len(h.calls), len(h.http_calls), driver.get_settings.call_count)
        owner.require_owner(deadline=150.0)
        config.remote_server_addr = f"http://127.0.0.1:{h.host_port}/foreign-route"
        try:
            self.refused(lambda: owner.require_owner(deadline=150.0))
        finally:
            config.remote_server_addr = endpoint
            self.assertIs(driver.command_executor, executor)
            self.assertIs(executor.client_config, config)
            self.assertEqual(
                (len(h.calls), len(h.http_calls), driver.get_settings.call_count),
                before,
            )
        self.refused(lambda: owner.require_owner(deadline=150.0))
        self.assertEqual(
            (len(h.calls), len(h.http_calls), driver.get_settings.call_count), before
        )
        h.assert_phone_retained()

    def test_scan_parent_pins_actual_endpoint_independently_of_child_guard(
        self,
    ) -> None:
        android_owner, automation, driver = self.start_scan()
        h = self.h
        config = driver.command_executor.client_config
        endpoint = config.remote_server_addr
        self.assertEqual(endpoint, automation._base_url)
        self.assertEqual(
            h.sdk.spawn.call_args.kwargs["env"]["APPIUM_HOME"],
            str(android_owner.appium_home),
        )
        before = (len(h.calls), len(h.http_calls), driver.get_settings.call_count)
        config.remote_server_addr = f"http://127.0.0.1:{h.host_port}/foreign-route"
        try:
            # Exercise the parent's own local boundary so a fixed child guard
            # cannot conceal a parent that still checks only object identity.
            self.refused(automation._require_driver)
            self.refused(lambda: automation.require_running(deadline=150.0))
        finally:
            config.remote_server_addr = endpoint
            self.assertEqual(
                (len(h.calls), len(h.http_calls), driver.get_settings.call_count),
                before,
            )
        self.refused(lambda: automation.require_running(deadline=150.0))
        self.assertEqual(
            (len(h.calls), len(h.http_calls), driver.get_settings.call_count), before
        )
        h.assert_phone_retained()

    def test_bind_rejects_unapproved_side_effect_caps_even_when_false(self) -> None:
        for key, value in (
            ("gpsEnabled", False),
            ("hideKeyboard", False),
            ("unicodeKeyboard", False),
            ("otherApps", []),
            ("uninstallOtherPackages", []),
            ("autoWebview", False),
        ):
            for prefix in ("", "appium:"):
                with self.subTest(key=prefix + key, value=value):
                    self.new_case()
                    owner = self.owner()
                    owner.prepare()
                    owner.begin_session(
                        server_url=f"http://127.0.0.1:{self.h.host_port}",
                        deadline=150.0,
                    )
                    self.h.active()
                    driver = self.h.attached_driver()
                    driver.capabilities[prefix + key] = value
                    before = (len(self.h.calls), len(self.h.http_calls))
                    self.refused(
                        lambda owner=owner, driver=driver: owner.bind_session(
                            driver, deadline=150.0
                        )
                    )
                    self.assertEqual(
                        (len(self.h.calls), len(self.h.http_calls)), before
                    )
                    driver.get_settings.assert_not_called()
                    self.h.assert_phone_retained()

    def test_cheap_guard_reads_no_sdk_or_http_and_latches_driver_transport_caps_loss(
        self,
    ) -> None:
        mutations = (
            lambda d: setattr(d, "session_id", "foreign-host-session"),
            lambda d: d.capabilities.update({"appium:systemPort": 19999}),
            lambda d: d.capabilities.update({"appium:skipServerInstallation": False}),
            lambda d: d.capabilities.update({"appium:autoLaunch": True}),
            lambda d: d.capabilities.update({"appium:skipUnlock": False}),
            lambda d: d.capabilities.update({"appium:suppressKillServer": False}),
            lambda d: d.capabilities.update({"appium:skipLogcatCapture": False}),
            lambda d: d.capabilities.update({"appium:autoGrantPermissions": True}),
            lambda d: d.capabilities.update({"gpsEnabled": False}),
            lambda d: d.capabilities.update({"appium:hideKeyboard": False}),
            lambda d: setattr(
                d.command_executor.client_config, "timeout", float("inf")
            ),
            lambda d: setattr(
                d,
                "command_executor",
                SimpleNamespace(client_config=SimpleNamespace(timeout=5.0)),
            ),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.new_case()
                owner, driver = self.bind()
                h = self.h
                before = (
                    len(h.calls),
                    len(h.http_calls),
                    driver.get_settings.call_count,
                )
                owner.require_owner(deadline=150.0)
                caps = dict(driver.capabilities)
                executor = driver.command_executor
                timeout = executor.client_config.timeout
                mutate(driver)
                self.refused(lambda owner=owner: owner.require_owner(deadline=150.0))
                driver.session_id = HOST_SESSION_ID
                driver.capabilities = caps
                driver.command_executor = executor
                executor.client_config.timeout = timeout
                self.refused(lambda owner=owner: owner.require_owner(deadline=150.0))
                self.assertEqual(
                    before,
                    (len(h.calls), len(h.http_calls), driver.get_settings.call_count),
                )

    def test_cheap_guard_enforces_same_lease_run_and_absolute_lifetime_without_io(
        self,
    ) -> None:
        for boundary in ("lease", "run", "deadline"):
            with self.subTest(boundary=boundary):
                self.new_case()
                owner, driver = self.bind()
                h = self.h
                before = (
                    len(h.calls),
                    len(h.http_calls),
                    driver.get_settings.call_count,
                )
                if boundary == "lease":
                    (h.phone.lock / "owner.json").write_bytes(b"foreign owner")
                elif boundary == "run":
                    h.phone.run_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
                else:
                    h.clock = 161.0
                self.refused(lambda owner=owner: owner.require_owner(deadline=150.0))
                self.assertEqual(
                    before,
                    (len(h.calls), len(h.http_calls), driver.get_settings.call_count),
                )

    def test_finalized_publication_return_fault_retracts_only_its_own_inode(
        self,
    ) -> None:
        for fault in ("deadline", "lease"):
            for external_replacement in (False, True):
                with self.subTest(
                    fault=fault, external_replacement=external_replacement
                ):
                    self.new_case()
                    h = self.h
                    owner, driver = self.bind()
                    self.deleted(owner)
                    cleanup_deadline = 180.0
                    result_path = h.output / "finalized.json"
                    failure_path = h.output / "finalize-failed.json"
                    lease_path = h.phone.lock / "owner.json"
                    foreign_lease = b"foreign phone lease; preserve"
                    original_identities = []
                    replacement_signatures = []
                    replacement_bytes = []
                    failed_publications = []
                    calls_at_publication = []
                    previous = sys.getprofile()

                    def on_return(
                        frame,
                        event,
                        value,
                        *,
                        h=h,
                        driver=driver,
                        fault=fault,
                        external_replacement=external_replacement,
                        cleanup_deadline=cleanup_deadline,
                        result_path=result_path,
                        failure_path=failure_path,
                        lease_path=lease_path,
                        foreign_lease=foreign_lease,
                        original_identities=original_identities,
                        replacement_signatures=replacement_signatures,
                        replacement_bytes=replacement_bytes,
                        failed_publications=failed_publications,
                        calls_at_publication=calls_at_publication,
                    ):
                        if (
                            event != "return"
                            or frame.f_code is not publish_new_json.__code__
                            or not isinstance(value, str)
                        ):
                            return
                        path = frame.f_locals["path"]
                        if path == failure_path:
                            self.assertEqual(
                                frame.f_locals["value"]["status"], "failed"
                            )
                            info = path.lstat()
                            failed_publications.append((info.st_dev, info.st_ino))
                            return
                        if path != result_path or original_identities:
                            return
                        self.assertEqual(frame.f_locals["value"]["status"], "passed")
                        info = path.lstat()
                        original_identities.append((info.st_dev, info.st_ino))
                        calls_at_publication.append(
                            (
                                h.command.call_count,
                                h.connection.call_count,
                                driver.get_settings.call_count,
                            )
                        )
                        if external_replacement:
                            # Identical bytes force rollback to honor the committed
                            # inode; a digest-only check would delete this replacement.
                            replacement = h.root / "foreign-finalized.json"
                            raw = result_path.read_bytes()
                            with replacement.open("xb") as stream:
                                stream.write(raw)
                            replacement.replace(result_path)
                            info = result_path.lstat()
                            replacement_signatures.append(
                                (
                                    info.st_dev,
                                    info.st_ino,
                                    info.st_size,
                                    info.st_mtime_ns,
                                    info.st_ctime_ns,
                                )
                            )
                            replacement_bytes.append(raw)
                        if fault == "deadline":
                            h.clock = cleanup_deadline + 1.0
                        else:
                            lease_path.write_bytes(foreign_lease)

                    try:
                        sys.setprofile(on_return)
                        error = self.refused(
                            lambda owner=owner: owner.finalize(deadline=180.0)
                        )
                    finally:
                        sys.setprofile(previous)
                    self.assertEqual(len(original_identities), 1)
                    self.assertIsInstance(
                        error, TimeoutError if fault == "deadline" else RuntimeError
                    )
                    self.refused(owner.require_closed)
                    failed = json.loads(failure_path.read_bytes())
                    self.assertEqual(failed["status"], "failed")
                    self.assertEqual(failed["run_id"], h.run_id)
                    self.assertIn(type(error).__name__, failed["error"])
                    self.assertIn(str(error), failed["error"])
                    info = failure_path.lstat()
                    self.assertEqual(failed_publications, [(info.st_dev, info.st_ino)])
                    if external_replacement:
                        info = result_path.lstat()
                        self.assertEqual(
                            replacement_signatures,
                            [
                                (
                                    info.st_dev,
                                    info.st_ino,
                                    info.st_size,
                                    info.st_mtime_ns,
                                    info.st_ctime_ns,
                                )
                            ],
                        )
                        self.assertNotEqual(
                            (info.st_dev, info.st_ino), original_identities[0]
                        )
                        self.assertEqual(replacement_bytes, [result_path.read_bytes()])
                    else:
                        self.assertFalse(result_path.exists())
                        self.assertFalse(result_path.is_symlink())
                    again = self.refused(
                        lambda owner=owner: owner.finalize(deadline=190.0)
                    )
                    self.assertIs(again, error)
                    self.assertEqual(
                        calls_at_publication,
                        [
                            (
                                h.command.call_count,
                                h.connection.call_count,
                                driver.get_settings.call_count,
                            )
                        ],
                    )
                    if fault == "lease":
                        self.assertEqual(lease_path.read_bytes(), foreign_lease)
                        self.assertTrue(h.phone.lock.exists())
                        self.assertFalse(h.phone.finished)
                        self.assertEqual(h.phone.path.read_bytes(), h.journal)
                        self.assertEqual(h.phone.document["actions"], [])
                    else:
                        h.assert_phone_retained()

    def test_failed_finalization_permanently_retires_every_public_entry_without_io(
        self,
    ) -> None:
        for stage in ("new", "prepared-services-failure", "requesting-unknown"):
            with self.subTest(stage=stage):
                self.new_case()
                h = self.h
                owner = self.owner()
                driver = h.attached_driver()
                if stage != "new":
                    owner.prepare()
                if stage == "prepared-services-failure":
                    h.returncodes[SERVICES] = 1
                    h.replies[SERVICES] = "synthetic Services read failure\n"
                elif stage == "requesting-unknown":
                    owner.begin_session(
                        server_url=f"http://127.0.0.1:{self.h.host_port}",
                        deadline=150.0,
                    )
                    h.active()
                before_final = len(h.calls)
                self.refused(lambda owner=owner: owner.finalize(deadline=180.0))
                if stage == "prepared-services-failure":
                    self.assertIn(SERVICES, h.calls[before_final:])
                else:
                    self.assertEqual(len(h.calls), before_final)

                h.returncodes.clear()
                h.replies[SERVICES] = EMPTY_SERVICES
                h.idle()
                self.assertLess(h.clock, 150.0)

                def io_counts(h=h, driver=driver):
                    return (
                        h.command.call_count,
                        h.connection.call_count,
                        h.sdk.remote.call_count,
                        h.sdk.spawn.call_count,
                        driver.get_settings.call_count,
                    )

                before = io_counts()
                entries = (
                    ("prepare", owner.prepare),
                    (
                        "require_prepared",
                        lambda owner=owner, h=h: owner.require_prepared(
                            phone=h.phone,
                            run_id=h.run_id,
                            app_id=h.app_id,
                            deadline=150.0,
                        ),
                    ),
                    (
                        "begin_session",
                        lambda owner=owner, h=h: owner.begin_session(
                            server_url=f"http://127.0.0.1:{h.host_port}",
                            deadline=150.0,
                        ),
                    ),
                    (
                        "bind_session",
                        lambda owner=owner, driver=driver: owner.bind_session(
                            driver,
                            deadline=150.0,
                        ),
                    ),
                    (
                        "require_owner",
                        lambda owner=owner: owner.require_owner(deadline=150.0),
                    ),
                )
                for name, entry in entries:
                    with self.subTest(entry=name):
                        if name == "bind_session":
                            h.active()
                        self.refused(entry)
                        self.assertEqual(io_counts(), before)
                self.refused(owner.require_closed)
                self.assertEqual(io_counts(), before)
                h.sdk.kill.assert_not_called()
                h.assert_phone_retained()

    def test_concrete_android_owner_rejects_callback_overrides_before_acquisition(
        self,
    ) -> None:
        for override in ("guard", "finalizer", "both"):
            with self.subTest(override=override):
                self.new_case()
                h = self.h
                owner = self.owner()
                owner.prepare()
                guard = Mock(side_effect=AssertionError("external guard must not run"))
                finalizer = Mock(
                    side_effect=AssertionError("external finalizer must not run")
                )
                callbacks = {}
                if override in ("guard", "both"):
                    callbacks["require_platform_owner"] = guard
                if override in ("finalizer", "both"):
                    callbacks["finalize_platform_owner"] = finalizer
                before = h.command.call_count
                output = h.root / "rejected-scan-automation"
                with self.assertRaisesRegex(ValueError, "derived.*not overridden"):
                    ScanAutomationOwner(
                        phone=h.phone,
                        run_id=h.run_id,
                        app_id=h.app_id,
                        node="synthetic-node",
                        appium_entry=h.root / "nonexistent-appium.js",
                        port=h.host_port,
                        output=output,
                        cwd=h.root,
                        deadline=h.deadline,
                        activity=".MainActivity",
                        android_owner=owner,
                        **callbacks,
                    )
                guard.assert_not_called()
                finalizer.assert_not_called()
                h.sdk.spawn.assert_not_called()
                h.sdk.remote.assert_not_called()
                h.sdk.probe.assert_not_called()
                h.connection.assert_not_called()
                self.assertFalse(output.exists())
                self.require_prepared(owner)
                self.assertEqual(h.command.call_count, before)
                h.assert_phone_retained()

    def test_known_delete_then_independent_finalizer_collects_idle_and_freezes_receipts(
        self,
    ) -> None:
        owner, driver = self.bind()
        h = self.h
        self.deleted(owner)
        before = len(h.calls)
        h.clock = 161.0  # Scenario expired; cleanup has its own explicit budget.
        self.assertIsNone(owner.finalize(deadline=180.0))
        self.assertIsNone(owner.require_closed())
        reads = h.calls[before:]
        for command in (
            BOOT,
            PS,
            FORWARDS,
            GLOBAL_SETTINGS,
            OS_PROCESSES,
            SERVICES,
            APPOPS,
            STORE,
            DEFAULT_IME,
            ENABLED_IMES,
        ):
            self.assertIn(command, reads)
        # Finalization must run the actual inventory again, not only cheap guards.
        final_paths = [
            path
            for path in h.output.rglob("result.json")
            if path != h.output / "prepared/result.json"
        ]
        inventories = [json.loads(path.read_bytes()) for path in final_paths]
        self.assertTrue(
            any(item.get("status") == "inventory-verified" for item in inventories)
        )
        driver.quit.assert_not_called()  # DELETE belongs to ScanAutomationOwner.
        h.assert_phone_retained()
        calls = len(h.calls)
        self.refused(lambda owner=owner: owner.finalize(deadline=190.0))
        self.assertEqual(len(h.calls), calls)
        receipt = final_paths[0]
        receipt.write_bytes(receipt.read_bytes() + b" ")
        self.refused(owner.require_closed)
        self.assertEqual(len(h.calls), calls)

    def test_finalizer_rejects_present_null_hidden_keys_after_known_delete(
        self,
    ) -> None:
        for policy in POLICIES:
            with self.subTest(policy=policy):
                self.new_case()
                owner, automation, driver = self.start_scan()
                h = self.h
                old_quit = driver.quit.side_effect

                def quit_with_literal_null(h=h, policy=policy, old_quit=old_quit):
                    old_quit()
                    h.idle()
                    h.replies[GLOBAL_SETTINGS] += f"{policy}=null\n"
                    self.assertEqual(
                        h.replies[("shell", "settings", "get", "global", policy)],
                        "null\n",
                    )

                # The real Scan owner records its known host DELETE before its
                # real Android finalizer inspects these post-DELETE readbacks.
                driver.quit.side_effect = quit_with_literal_null
                before = len(h.calls)
                self.refused(
                    lambda automation=automation: automation.close(deadline=180.0)
                )
                driver.quit.assert_called_once()
                self.assertIn(GLOBAL_SETTINGS, h.calls[before:])
                self.refused(owner.require_closed)
                self.refused(automation.require_closed)
                calls = len(h.calls)
                h.idle()
                self.refused(lambda owner=owner: owner.finalize(deadline=190.0))
                self.assertEqual(len(h.calls), calls)
                h.assert_phone_retained()

    def test_requesting_unknown_remote_and_wrong_or_missing_delete_never_close_successfully(
        self,
    ) -> None:
        for stage in ("unknown-remote", "missing-delete", "wrong-host-id"):
            with self.subTest(stage=stage):
                self.new_case()
                if stage == "unknown-remote":
                    owner = self.owner()
                    owner.prepare()
                    owner.begin_session(
                        server_url=f"http://127.0.0.1:{self.h.host_port}",
                        deadline=150.0,
                    )
                    # Remote may have timed out after creating device state. Even
                    # apparently idle readbacks cannot turn unknown into a DELETE.
                else:
                    owner, _ = self.bind()
                    if stage == "wrong-host-id":
                        self.refused(
                            lambda owner=owner: owner.session_deleted(
                                DEVICE_SESSION_ID, deadline=150.0
                            )
                        )
                self.h.idle()
                self.refused(lambda owner=owner: owner.finalize(deadline=180.0))
                self.refused(owner.require_closed)
                calls = len(self.h.calls)
                self.refused(lambda owner=owner: owner.finalize(deadline=190.0))
                self.assertEqual(len(self.h.calls), calls)
                self.h.assert_phone_retained()

    def test_failed_finalize_is_single_attempt_preserves_primary_and_never_repairs(
        self,
    ) -> None:
        cases = (
            (SERVICES, LIVE_SERVICES),
            (OS_PROCESSES, active_processes()),
            (FORWARDS, ""),
            (APPOPS, "Uid mode: MOCK_LOCATION: allow\nMOCK_LOCATION: allow\n"),
            (STORE, json.dumps([SETTINGS]) + "\n"),
            (ENABLED_IMES, "io.appium.settings/.UnicodeIME\n"),
            (("shell", "settings", "get", "global", POLICIES[0]), "1\n"),
        )
        for command, reply in cases:
            with self.subTest(command=command):
                self.new_case()
                owner, _ = self.bind()
                self.deleted(owner)
                self.h.replies[command] = reply
                self.refused(lambda owner=owner: owner.finalize(deadline=180.0))
                calls = len(self.h.calls)
                self.h.replies[SERVICES] = EMPTY_SERVICES
                self.refused(lambda owner=owner: owner.finalize(deadline=190.0))
                self.assertEqual(len(self.h.calls), calls)
                self.refused(owner.require_closed)
                self.h.assert_phone_retained()
        self.new_case()
        owner, _ = self.bind()
        self.deleted(owner)
        primary = RuntimeError("primary OS collection failure")
        original = self.h.command.side_effect
        primary_seen = []

        def fail_sdk(command, **kwargs):
            if tuple(command[3:]) == OS_PROCESSES:
                primary_seen.append(True)
                raise primary
            return original(command, **kwargs)

        self.h.command.side_effect = fail_sdk
        original_open = Path.open

        def fail_publication(path, *args, **kwargs):
            mode = args[0] if args else kwargs.get("mode", "r")
            if (
                primary_seen
                and path.is_relative_to(self.h.output)
                and any(flag in mode for flag in "wax+")
            ):
                raise OSError("cleanup receipt publication failure")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", fail_publication):
            error = self.refused(lambda owner=owner: owner.finalize(deadline=180.0))
        self.assertIs(error, primary)
        self.assertIn("cleanup receipt publication failure", " ".join(error.__notes__))
        calls = len(self.h.calls)
        again = self.refused(lambda owner=owner: owner.finalize(deadline=190.0))
        self.assertIn(str(primary), str(again) + str(again.__cause__))
        self.assertEqual(len(self.h.calls), calls)
        self.refused(owner.require_closed)
        self.h.assert_phone_retained()

    def test_real_scan_owner_derives_android_callbacks_and_tracks_unknown_remote_cleanup(
        self,
    ) -> None:
        for remote_timeout in (False, True):
            with self.subTest(remote_timeout=remote_timeout):
                self.new_case()
                h = self.h
                android_owner = self.owner()
                android_owner.prepare()
                automation = self.scan_owner(android_owner)
                begin = self.scope.enter_context(
                    patch.object(
                        android_owner,
                        "begin_session",
                        wraps=android_owner.begin_session,
                    )
                )
                original_remote = h.sdk.remote.side_effect

                def remote(
                    url,
                    *,
                    options,
                    client_config,
                    h=h,
                    remote_timeout=remote_timeout,
                    original_remote=original_remote,
                    android_owner=android_owner,
                    automation=automation,
                    begin=begin,
                ):
                    self.assertEqual(
                        begin.call_args.kwargs,
                        {"server_url": automation._base_url, "deadline": h.deadline},
                    )
                    self.assertEqual(url, automation._base_url)
                    self.assertEqual(
                        h.sdk.spawn.call_args.kwargs["env"]["APPIUM_HOME"],
                        str(android_owner.appium_home),
                    )
                    # No patch of create_android_driver or admission. Its actual
                    # options must carry the protected port and attach flags.
                    caps = options.to_capabilities()
                    self.assertEqual(caps["appium:systemPort"], h.system_port)
                    for key in (
                        "skipServerInstallation",
                        "skipDeviceInitialization",
                        "noReset",
                    ):
                        self.assertIs(caps["appium:" + key], True)
                    for key in ("autoLaunch", "forceAppLaunch", "shouldTerminateApp"):
                        self.assertIs(caps["appium:" + key], False)
                    h.active()
                    if remote_timeout:
                        raise TimeoutError("Remote outcome unknown")
                    driver = original_remote(
                        url, options=options, client_config=client_config
                    )
                    old_quit = driver.quit.side_effect

                    def quit_session():
                        h.assert_phone_retained()
                        old_quit()
                        h.idle()

                    driver.quit.side_effect = quit_session
                    return driver

                h.sdk.remote.side_effect = remote
                if remote_timeout:
                    self.refused(automation.start, "Remote outcome unknown")
                    self.refused(
                        lambda automation=automation: automation.close(deadline=180.0),
                        "unknown",
                    )
                    self.refused(automation.require_closed)
                    self.refused(android_owner.require_closed)
                else:
                    driver = automation.start()
                    self.assertEqual(driver.session_id, HOST_SESSION_ID)
                    automation.require_handoff(
                        driver=driver,
                        phone=h.phone,
                        run_id=h.run_id,
                        app_id=h.app_id,
                        deadline=150.0,
                    )
                    automation.require_running(deadline=150.0)
                    automation.close(deadline=180.0)
                    automation.require_closed()
                    android_owner.require_closed()
                    driver.quit.assert_called_once()
                    self.assertLess(
                        h.sdk.events.index("session-quit"),
                        h.sdk.events.index("server-signal"),
                    )
                self.assertIn((h.host_port, h.host_base_path + "/status"), h.http_calls)
                self.assertEqual(h.sdk.remote.call_count, 1)
                self.assertEqual(h.sdk.events.count("server-reaped"), 1)
                h.assert_phone_retained()


if __name__ == "__main__":
    unittest.main()
