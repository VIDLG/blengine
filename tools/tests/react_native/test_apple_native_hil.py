"""Synthetic SDK/serial boundaries; these tests never authorize Apple RF."""

from __future__ import annotations

import io
import json
import plistlib
import subprocess
import tempfile
import unittest
from contextlib import ExitStack, contextmanager, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, call, patch

from tests.peripherals.nrf52840 import direct_fixture, fixture_session
from tests.peripherals.nrf52840.fixture_attestation import parse_attestation
from tests.peripherals.nrf52840.profile import load_profile
from tests.peripherals.nrf52840.support.direct_fixture_resources import resources
from tests.peripherals.nrf52840.support.session_receipts import write_v2_inputs
from tools.apple import native_hil as hil
from tools.react_native import ios_radio_lease, run_ios_rf_scan

DEVICE = "00000000-0000000000000001"
BOOT = "12345678-1234-4234-8234-123456789abc"


class AppleNativeHilTests(unittest.TestCase):
    @contextmanager
    def attempt(self, platform="ios", scenario="io", *, fault=""):
        """Keep session, receipt, fixture lifecycle and native result validation real.

        Intercept subprocess.run/Popen (actual SDK/install/launch boundaries), serial
        resources, live vendor response and doctor. No synthetic ID reaches hardware.
        """
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            contract, profile = write_v2_inputs(root)
            app = root / "Input.app"
            name = (
                "BleHubAppleIOSProbe" if platform == "ios" else "BleHubAppleMacOSProbe"
            )
            plist = app / ("Info.plist" if platform == "ios" else "Contents/Info.plist")
            executable = app / (name if platform == "ios" else "Contents/MacOS/" + name)
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"synthetic - never run")
            plist.write_bytes(
                plistlib.dumps(
                    {
                        "BleHubNativeHilProtocolVersion": 2,
                        "CFBundleIdentifier": "dev.blehub.synthetic.probe",
                        "CFBundleExecutable": name,
                    }
                )
            )
            work = root / "work"
            commands = []
            events = []
            fixture = stack.enter_context(resources(events))
            fixture.session.pop_event.return_value = None
            fixture.session.poll_event.return_value = None
            stack.enter_context(
                patch.object(direct_fixture, "load_profile", side_effect=load_profile)
            )
            doctor = stack.enter_context(patch.object(direct_fixture, "NrfDoctor"))
            doctor.return_value.run.return_value.role_handles = {"updates": 909}
            installed = json.loads(contract.read_text())
            installed.pop("fixture_role")
            if fault == "wrong-image":
                installed["image_sha256"] = "cd" * 32
            attest = stack.enter_context(
                patch.object(
                    fixture_session.VendorCommands,
                    "read_image_attestation",
                    return_value=parse_attestation(installed),
                )
            )
            stack.enter_context(
                patch.object(
                    fixture_session, "identity_for_port", return_value="jlink:123456"
                )
            )
            stack.enter_context(patch.object(hil, "WORK", work))
            rn_lock = root / "rn-ios-scan.lock"
            stack.enter_context(patch.object(run_ios_rf_scan, "LOCK", rn_lock))
            stack.enter_context(patch.object(ios_radio_lease, "ROOT", root))
            stack.enter_context(
                patch.object(hil.socket, "gethostname", return_value="synthetic-mac")
            )
            stack.enter_context(patch.object(hil.sys, "platform", "darwin"))
            stack.enter_context(
                patch.object(hil.shutil, "which", return_value="synthetic-xcrun")
            )
            # Even readiness goes through the intercepted SDK subprocess boundary.
            stack.enter_context(
                patch(
                    "tools.react_native.ios_device.shutil.which",
                    return_value="synthetic-xcrun",
                )
            )
            container: dict[str, str] = {}
            state = {"launched": False, "dead": False, "post_stimulus_ack": False}
            stimuli = []
            process = Mock()
            process.pid = 712
            process.poll.side_effect = lambda: 0 if state["dead"] else None
            process.wait.side_effect = lambda **kw: state.update(dead=True) or 0
            identity = {}
            app_path = ""
            owner = None

            def plan():
                return fixture_session.load_session(
                    next(work.glob("*/fixture-session/fixture-request.json"))
                )

            def assert_admitted():
                self.assertEqual(attest.call_count, 1)
                fixture.owner.require.assert_called()
                fixture_session.verify_receipt(
                    plan(), fixture_identity=(hil.os.getpid(), hil.os.getpgid(0))
                )

            def runtime_text():
                prefix = f"apple_{platform}_corebluetooth_"
                rows = [
                    "apple_native_hil_identity=" + json.dumps(identity),
                    "apple_native_hil_close=passed",
                ]
                rows += [
                    prefix + item + "=passed"
                    for item in (
                        "initialize",
                        "scan",
                        "connected",
                        "gatt",
                        "logical_disconnect",
                        "post_disconnect_silence",
                    )
                ]
                config = hil.apple_profile(profile, "10", "ab")
                rows += config.evidence()
                scan_name = load_profile(profile).local_name
                rows += [
                    prefix + "scan_filter=service+name",
                    prefix
                    + "scan_name_prefix="
                    + ("foreign" if fault == "wrong-name-prefix" else scan_name),
                ]
                if scenario == "io":
                    rows += [
                        prefix + item + "=passed"
                        for item in (
                            "write_with_response_readback",
                            "write_without_response_readback",
                            "gatt_io_event_order",
                            "gatt_io",
                        )
                    ]
                else:
                    rows += [
                        prefix + item + "=passed"
                        for item in (
                            "notification_enable_disable_order",
                            "notification_post_disable_silence",
                            "notification",
                        )
                    ]
                    rows += [prefix + "notification_value=ef"]
                if platform == "ios":
                    rows += [
                        f"apple_ios_probe_run_id={owner.name}",
                        f"apple_ios_rf_{scenario}=passed",
                        "apple_ios_rf_ledger=passed",
                        "apple_ios_rf_post_dispose_silence=passed",
                        "apple_ios_corebluetooth_rf=passed",
                    ]
                if fault in {"incomplete", "cleanup-failure"}:
                    rows.remove("apple_native_hil_close=passed")
                return "\n".join(rows) + "\n"

            def publish(name, value):
                container[name] = value
                if platform == "macos":
                    (owner / "coordination" / name).write_text(value)

            def launched(command):
                nonlocal owner, app_path
                assert_admitted()
                self.assertEqual(
                    command[command.index("--scan-name-prefix") + 1],
                    load_profile(profile).local_name,
                )
                if platform == "ios":
                    self.assertTrue(rn_lock.is_dir())
                owner = next(path for path in work.iterdir() if path.name != "locks")
                state["launched"] = True
                app_path = (
                    "/private/var/containers/Bundle/Application/"
                    + BOOT
                    + "/Input.app/"
                    + name
                    if platform == "ios"
                    else str(owner / "Input.app/Contents/MacOS" / name)
                )
                identity.update(
                    run_id=owner.name,
                    boot_id=BOOT,
                    pid=712,
                    bundle_id="dev.blehub.synthetic.probe",
                    executable=app_path,
                )
                if fault == "foreign-boot":
                    identity["pid"] = 999
                publish("native-identity.json", json.dumps(identity))
                if scenario == "notification":
                    if fault != "missing-enabled":
                        publish("notification-enabled", "ready\n")
                    if fault == "early-disabled":
                        publish("notification-disabled", "ready\n")
                    if fault == "early-release":
                        publish("post-disable-triggered", "ready\n")
                elif fault != "no-result":
                    publish("result.txt", runtime_text())
                if fault in {"fixture-boot-drift", "reboot-before-trigger"}:
                    reply = fixture.session.request.return_value
                    fixture.session.request.return_value = replace(
                        reply,
                        payload=reply.payload[:2] + b"\xee" * 16 + reply.payload[18:],
                    )
                if fault == "fixture-cleanup-failure":
                    fixture.gap.stop_advertising.side_effect = RuntimeError(
                        "synthetic advertising cleanup failure"
                    )

            def set_value(handle, value):
                assert_admitted()
                self.assertEqual(handle, 909)
                self.assertEqual(container.get("notification-enabled"), "ready\n")
                self.assertNotIn("post-disable-triggered", container)
                self.assertFalse(
                    (owner / "coordination/post-disable-triggered").exists()
                )
                self.assertEqual(value, b"\xef" if not stimuli else b"\xee")
                self.assertLess(len(stimuli), 2)
                stimuli.append(value)
                if len(stimuli) == 1:
                    self.assertNotIn("notification-disabled", container)
                    if fault != "missing-disabled":
                        publish(
                            "notification-disabled",
                            "ready\nready\n"
                            if fault == "duplicate-disabled"
                            else "ready\n",
                        )
                else:
                    self.assertEqual(container.get("notification-disabled"), "ready\n")
                    if fault == "post-stimulus-rejected":
                        raise RuntimeError("synthetic post-disable SET_VALUE rejected")
                    state["post_stimulus_ack"] = True

            fixture.gatt.set_value.side_effect = set_value

            def sdk(command, **kwargs):
                commands.append(command)
                self.assertNotIn("--terminate-existing", command)
                if command[1:4] == ["devicectl", "list", "devices"]:
                    return subprocess.CompletedProcess(
                        command, 0, DEVICE + " available\n"
                    )
                if "lockState" in command:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        json.dumps(
                            {
                                "result": {
                                    "passcodeRequired": False,
                                    "unlockedSinceBoot": True,
                                }
                            }
                        ),
                    )
                self.assertIn("--device", command)
                self.assertEqual(command[command.index("--device") + 1], DEVICE)
                kind = command[2:5]
                result = {"deviceIdentifier": "synthetic-core-device"}
                if kind == ["device", "info", "processes"]:
                    if fault == "reboot-before-install" and attest.call_count:
                        reply = fixture.session.request.return_value
                        fixture.session.request.return_value = replace(
                            reply,
                            payload=reply.payload[:2]
                            + b"\xee" * 16
                            + reply.payload[18:],
                        )
                    exists = (
                        fault == "existing" or state["launched"] and not state["dead"]
                    )
                    path = (
                        app_path
                        if state["launched"]
                        else "/synthetic/Other.app/" + name
                    )
                    if fault == "foreign-pid" and state["launched"]:
                        path = "/synthetic/Foreign.app/Other"
                    result["runningProcesses"] = (
                        [{"processIdentifier": 712, "executable": "file://" + path}]
                        if exists
                        else []
                    )
                elif kind == ["device", "install", "app"]:
                    assert_admitted()
                    if fault == "receipt-drift":
                        p = plan().receipt_path
                        p.chmod(0o600)
                        p.write_text("{}")
                    if fault == "lease-loss":
                        fixture.owner.require.side_effect = RuntimeError(
                            "synthetic lease loss"
                        )
                    if fault == "reboot-before-launch":
                        reply = fixture.session.request.return_value
                        fixture.session.request.return_value = replace(
                            reply,
                            payload=reply.payload[:2]
                            + b"\xee" * 16
                            + reply.payload[18:],
                        )
                    if fault == "artifact-drift":
                        next(p for p in Path(command[-1]).rglob(name)).write_bytes(
                            b"changed"
                        )
                elif kind == ["device", "process", "launch"]:
                    launched(command)
                    result["process"] = {
                        "processIdentifier": 712,
                        "executable": "file://" + app_path,
                    }
                    if fault == "launch-failure":
                        raise subprocess.TimeoutExpired(command, 1)
                elif kind == ["device", "copy", "from"]:
                    source = command[command.index("--source") + 1].rsplit("/", 1)[-1]
                    if source not in container:
                        return subprocess.CompletedProcess(command, 1)
                    Path(command[command.index("--destination") + 1]).write_text(
                        container[source]
                    )
                elif kind == ["device", "copy", "to"]:
                    path = Path(command[command.index("--source") + 1])
                    container[path.name] = path.read_text()
                    if path.name == "post-disable-triggered":
                        self.assertEqual(stimuli, [b"\xef", b"\xee"])
                        self.assertTrue(state["post_stimulus_ack"])
                        self.assertEqual(
                            container.get("notification-disabled"), "ready\n"
                        )
                        publish("result.txt", runtime_text())
                elif kind == ["device", "notification", "post"]:
                    request = json.loads(container["identity-request.json"])
                    response = {
                        "identity": dict(identity),
                        "challenge": request["challenge"],
                    }
                    if fault == "stale-inspection":
                        response["challenge"] = "old"
                    publish("native-inspection.json", json.dumps(response))
                elif kind == ["device", "process", "terminate"]:
                    self.assertEqual(command[command.index("--pid") + 1], "712")
                    if fault in {"cleanup-failure", "success-close-failure"}:
                        raise OSError("synthetic terminate failure")
                    state["dead"] = True
                else:
                    raise AssertionError(f"unexpected hardware command: {command}")
                receipt = Path(command[command.index("--json-output") + 1])
                receipt.write_text(
                    json.dumps(
                        {
                            "info": {
                                "outcome": "success",
                                "commandType": ".".join(command[1:5]),
                                "arguments": command[1:],
                            },
                            "result": result,
                        }
                    )
                )
                return subprocess.CompletedProcess(command, 0)

            def popen(command, **kwargs):
                commands.append(command)
                self.assertEqual(platform, "macos")
                launched(command)

                def poll():
                    if (
                        scenario == "notification"
                        and state["post_stimulus_ack"]
                        and (owner / "coordination/post-disable-triggered").is_file()
                    ):
                        publish("result.txt", runtime_text())
                    return 0 if state["dead"] else None

                process.poll.side_effect = poll
                return process

            # Actual process boundaries are mocked, never NativeSmoke/DirectFixture itself.
            stack.enter_context(patch.object(hil.subprocess, "run", side_effect=sdk))
            stack.enter_context(
                patch.object(hil.subprocess, "Popen", side_effect=popen)
            )
            stack.enter_context(redirect_stdout(io.StringIO()))
            yield {
                "root": root,
                "work": work,
                "contract": contract,
                "profile": profile,
                "app": app,
                "commands": commands,
                "fixture": fixture,
                "process": process,
                "doctor": doctor,
                "state": state,
                "stimuli": stimuli,
                "rn_lock": rn_lock,
                "radio_root": root / ".work/ios-radio-leases",
            }

    def execute(
        self, case, platform="ios", scenario="io", timeout=5, success_app="keep"
    ):
        return hil.run(
            platform,
            DEVICE if platform == "ios" else "synthetic-mac",
            "test-port",
            case["contract"],
            case["profile"],
            case["app"],
            scenario,
            timeout=timeout,
            success_app=success_app,
        )

    def report(self, case):
        return json.loads(next(case["work"].glob("*/result.json")).read_text())

    def test_ios_io_and_notification_share_exact_owner_and_live_receipt(self):
        for scenario in ("io", "notification"):
            with (
                self.subTest(scenario=scenario),
                self.attempt(scenario=scenario) as case,
            ):
                self.execute(case, scenario=scenario)
                report = self.report(case)
                self.assertEqual(report["status"], "passed")
                self.assertTrue(report["peripheral_disconnected"])
                self.assertEqual(report["native_identity"]["pid"], 712)
                self.assertEqual(
                    report["scan_filter"]["name_prefix"],
                    load_profile(case["profile"]).local_name,
                )
                exclusion = report["device_exclusion"]
                self.assertTrue(
                    exclusion["rn_lock_acquired"] and exclusion["rn_lock_released"]
                )
                self.assertFalse(exclusion["radio_lease_acquired"])
                self.assertFalse(case["rn_lock"].exists())
                kinds = [c[2:5] for c in case["commands"]]
                self.assertIn(["device", "install", "app"], kinds)
                self.assertIn(["device", "process", "launch"], kinds)
                self.assertNotIn(["device", "process", "terminate"], kinds)
                self.assertIn(["device", "notification", "post"], kinds)
                case["fixture"].gatt.reset_server.assert_not_called()
                case["fixture"].serial.close.assert_called_once()
                if scenario == "notification":
                    self.assertEqual(
                        case["fixture"].gatt.set_value.call_args_list,
                        [call(909, b"\xef"), call(909, b"\xee")],
                    )
                    proof = report["notification_stimuli"]
                    self.assertEqual(
                        [row["phase"] for row in proof],
                        ["enabled-baseline", "post-disable"],
                    )
                    self.assertEqual([row["value_hex"] for row in proof], ["ef", "ee"])
                    self.assertTrue(
                        all(row["btp_set_value_acknowledged"] for row in proof)
                    )
                    self.assertTrue(report["notification_marker_released"])

    def test_explicit_success_close_only_stops_this_verified_ios_run(self):
        for scenario in ("io", "notification"):
            with (
                self.subTest(scenario=scenario),
                self.attempt(scenario=scenario) as case,
            ):
                self.execute(case, scenario=scenario, success_app="close-owned")
                report = self.report(case)
                self.assertEqual(report["status"], "passed")
                self.assertIsNone(report["failure"])
                self.assertEqual(report["success_app_policy"], "close-owned")
                self.assertTrue(report["native_identity_verified"])
                self.assertTrue(report["native_termination"]["confirmed"])
                self.assertTrue(case["state"]["dead"])
                kinds = [command[2:5] for command in case["commands"]]
                self.assertEqual(kinds.count(["device", "process", "terminate"]), 1)
                self.assertLess(
                    kinds.index(["device", "notification", "post"]),
                    kinds.index(["device", "process", "terminate"]),
                )
                self.assertFalse(report["permission_mutation_attempted"])
                self.assertFalse(report["radio_mutation_attempted"])
                self.assertFalse(case["rn_lock"].exists())

    def test_success_close_does_not_adopt_existing_app_or_bypass_live_identity(self):
        for fault in (
            "existing",
            "foreign-pid",
            "stale-inspection",
            "success-close-failure",
        ):
            with self.subTest(fault=fault), self.attempt(fault=fault) as case:
                with self.assertRaises(RuntimeError):
                    self.execute(case, success_app="close-owned")
                report = self.report(case)
                self.assertEqual(report["status"], "failed")
                if fault == "existing":
                    self.assertEqual(report["failure"]["stage"], "device-preflight")
                else:
                    self.assertEqual(report["failure"]["stage"], "cleanup.native")
                    self.assertIsNone(report["primary_error"])
                self.assertFalse(report["native_termination"]["confirmed"])
                if fault != "success-close-failure":
                    self.assertFalse(any("terminate" in c for c in case["commands"]))
                if fault == "existing":
                    case["fixture"].open_serial.assert_not_called()

    def test_macos_scenes_consume_prebuilt_without_unlocking_legacy_admission(self):
        for scenario in ("io", "notification"):
            with (
                self.subTest(scenario=scenario),
                self.attempt(platform="macos", scenario=scenario) as case,
            ):
                self.execute(case, platform="macos", scenario=scenario)
                self.assertEqual(self.report(case)["status"], "passed")
                self.assertEqual(len(case["commands"]), 1)
                case["process"].terminate.assert_not_called()
                case["process"].wait.assert_called_once_with(timeout=3)

    def test_missing_notification_markers_never_send_or_release_early(self):
        for platform in ("ios", "macos"):
            for fault, count in (("missing-enabled", 0), ("missing-disabled", 1)):
                with (
                    self.subTest(platform=platform, fault=fault),
                    self.attempt(
                        platform=platform, scenario="notification", fault=fault
                    ) as case,
                ):
                    with self.assertRaises(TimeoutError):
                        self.execute(
                            case, platform=platform, scenario="notification", timeout=1
                        )
                    self.assertEqual(case["fixture"].gatt.set_value.call_count, count)
                    report = self.report(case)
                    self.assertFalse(report["notification_marker_released"])
                    self.assertEqual(len(report["notification_stimuli"]), count)
                    self.assertFalse(
                        any(
                            command[2:5] == ["device", "copy", "to"]
                            and "post-disable-triggered" in str(command)
                            for command in case["commands"]
                        )
                    )

    def test_early_or_duplicate_native_marker_rejects_notification_bridge(self):
        for fault, count in (
            ("early-disabled", 0),
            ("duplicate-disabled", 1),
            ("early-release", 0),
        ):
            with (
                self.subTest(fault=fault),
                self.attempt(scenario="notification", fault=fault) as case,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "ordering|released before stimulus"
                ):
                    self.execute(case, scenario="notification")
                self.assertEqual(case["fixture"].gatt.set_value.call_count, count)
                self.assertFalse(self.report(case)["notification_marker_released"])

    def test_failed_post_disable_ack_never_releases_marker_or_claims_delivery(self):
        for platform in ("ios", "macos"):
            with (
                self.subTest(platform=platform),
                self.attempt(
                    platform=platform,
                    scenario="notification",
                    fault="post-stimulus-rejected",
                ) as case,
            ):
                with self.assertRaisesRegex(RuntimeError, "SET_VALUE rejected"):
                    self.execute(case, platform=platform, scenario="notification")
                report = self.report(case)
                self.assertEqual(
                    [row["value_hex"] for row in report["notification_stimuli"]],
                    ["ef", "ee"],
                )
                self.assertEqual(
                    [
                        row["btp_set_value_acknowledged"]
                        for row in report["notification_stimuli"]
                    ],
                    [True, False],
                )
                self.assertIn("not RF TX or delivery", report["notification_ack_scope"])
                self.assertFalse(report["notification_marker_released"])
                self.assertFalse(
                    any(
                        command[2:5] == ["device", "copy", "to"]
                        and "post-disable-triggered" in str(command)
                        for command in case["commands"]
                    )
                )
                case["fixture"].serial.close.assert_called_once()

    def test_duplicate_notification_dispatch_or_release_is_fail_closed(self):
        trigger = hil.NativeSmoke.trigger_notification
        release = hil.NativeSmoke.release_disable
        for phase in ("baseline", "post-disable", "release"):
            with (
                self.subTest(phase=phase),
                self.attempt(scenario="notification") as case,
            ):

                def repeat_trigger(native, *args, _phase=phase, **kwargs):
                    trigger(native, *args, **kwargs)
                    if (_phase == "post-disable") == kwargs.get("post_disable", False):
                        trigger(native, *args, **kwargs)

                def repeat_release(native, *args, **kwargs):
                    release(native, *args, **kwargs)
                    release(native, *args, **kwargs)

                target = (
                    "release_disable" if phase == "release" else "trigger_notification"
                )
                replacement = repeat_release if phase == "release" else repeat_trigger
                with (
                    patch.object(hil.NativeSmoke, target, new=replacement),
                    self.assertRaisesRegex(RuntimeError, "duplicate"),
                ):
                    self.execute(case, scenario="notification")
                self.assertEqual(
                    case["fixture"].gatt.set_value.call_count,
                    1 if phase == "baseline" else 2,
                )
                self.assertEqual(self.report(case)["status"], "failed")
                copies = [
                    command
                    for command in case["commands"]
                    if command[2:5] == ["device", "copy", "to"]
                    and "post-disable-triggered" in str(command)
                ]
                self.assertEqual(len(copies), 1 if phase == "release" else 0)

    def test_release_without_acknowledged_stimuli_cannot_write_marker(self):
        with self.attempt(platform="macos", scenario="notification") as case:
            native = hil.NativeSmoke(
                "macos", "synthetic-mac", case["app"], case["root"]
            )
            guard = Mock(
                side_effect=AssertionError(
                    "early release must not reach mutation boundary"
                )
            )
            with self.assertRaisesRegex(RuntimeError, "early/duplicate"):
                native.release_disable(case["root"], guard)
            self.assertFalse((case["root"] / "post-disable-triggered").exists())
            self.assertEqual(case["commands"], [])
            guard.assert_not_called()
            case["fixture"].gatt.set_value.assert_not_called()

    def test_reboot_before_launch_or_notification_trigger_blocks_new_boot_mutation(
        self,
    ):
        for fault, scenario in (
            ("reboot-before-install", "io"),
            ("reboot-before-launch", "io"),
            ("reboot-before-trigger", "notification"),
        ):
            with (
                self.subTest(fault=fault),
                self.attempt(scenario=scenario, fault=fault) as case,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "boot changed before mutation"
                ):
                    self.execute(case, scenario=scenario)
                if fault in {"reboot-before-install", "reboot-before-launch"}:
                    self.assertFalse(case["state"]["launched"])
                    if fault == "reboot-before-install":
                        self.assertFalse(
                            any("install" in command for command in case["commands"])
                        )
                else:
                    self.assertTrue(case["state"]["launched"])
                case["fixture"].gatt.set_value.assert_not_called()
                self.assertEqual(case["fixture"].gap.stop_advertising.call_count, 2)
                case["fixture"].serial.close.assert_called_once()

    def test_combined_scan_prefix_must_match_canonical_profile_in_native_result(self):
        with (
            self.attempt(fault="wrong-name-prefix") as case,
            self.assertRaisesRegex(RuntimeError, "incomplete/failed"),
        ):
            self.execute(case)

    def test_rn_and_radio_existing_owners_fail_before_sdk_serial_and_remain_untouched(
        self,
    ):
        for kind in ("rn", "radio", "restoration"):
            with self.subTest(kind=kind), self.attempt() as case:
                if kind == "rn":
                    path = case["rn_lock"]
                else:
                    key = hil.hashlib.sha256(DEVICE.encode()).hexdigest()
                    path = case["radio_root"] / (
                        key + (".restoration-pending" if kind == "restoration" else "")
                    )
                path.mkdir(parents=True)
                marker = path / "owner.json"
                marker.write_text("foreign owner - preserve")
                with self.assertRaisesRegex(RuntimeError, "already occupies"):
                    self.execute(case)
                self.assertEqual(case["commands"], [])
                case["fixture"].open_serial.assert_not_called()
                self.assertEqual(marker.read_text(), "foreign owner - preserve")
                if kind != "rn":
                    self.assertFalse(case["rn_lock"].exists())

    def test_shared_rn_lock_is_mutually_exclusive_with_actual_rn_mkdir_protocol(self):
        with self.attempt() as case:
            attempt = case["work"] / BOOT
            report = {}
            require, release = hil.acquire_ios_exclusion(DEVICE, attempt, report)
            try:
                with self.assertRaises(FileExistsError):
                    run_ios_rf_scan.LOCK.mkdir()
                require()
                radio_path = ios_radio_lease.IosRadioLease(
                    DEVICE, BOOT, case["root"] / "unused.json"
                ).lock
                radio_path.mkdir(parents=True)
                with self.assertRaisesRegex(RuntimeError, "radio owner/restoration"):
                    require()  # A later radio owner is also rejected, never cleared.
            finally:
                release()
            self.assertTrue(radio_path.is_dir())
            run_ios_rf_scan.LOCK.mkdir()
            with self.assertRaisesRegex(RuntimeError, "RN iOS owner"):
                hil.acquire_ios_exclusion(DEVICE, attempt, {})
            run_ios_rf_scan.LOCK.rmdir()
            self.assertEqual(case["commands"], [])

    def test_shared_rn_lock_release_cannot_remove_replaced_owner(self):
        with self.attempt() as case:
            require, release = hil.acquire_ios_exclusion(
                DEVICE, case["work"] / BOOT, {}
            )
            case["rn_lock"].rename(case["root"] / "original-native-lock")
            case["rn_lock"].mkdir()
            foreign = case["rn_lock"] / "owner.json"
            foreign.write_text("foreign")
            with self.assertRaisesRegex(RuntimeError, "ownership changed"):
                require()
            with self.assertRaisesRegex(RuntimeError, "foreign RN iOS lock"):
                release()
            self.assertEqual(foreign.read_text(), "foreign")
            self.assertEqual(case["commands"], [])

    def test_wrong_image_or_existing_app_never_installs_launches_or_takes_over(self):
        for fault in ("wrong-image", "existing"):
            with self.subTest(fault=fault), self.attempt(fault=fault) as case:
                with self.assertRaises((ValueError, RuntimeError)):
                    self.execute(case)
                self.assertFalse(case["state"]["launched"])
                self.assertFalse(
                    any("install" in c or "terminate" in c for c in case["commands"])
                )
                case["doctor"].assert_not_called()

    def test_receipt_lease_and_artifact_drift_after_install_block_launch(self):
        for fault in ("receipt-drift", "lease-loss", "artifact-drift"):
            with self.subTest(fault=fault), self.attempt(fault=fault) as case:
                with self.assertRaises((ValueError, RuntimeError)):
                    self.execute(case)
                self.assertFalse(case["state"]["launched"])
                self.assertEqual(self.report(case)["status"], "failed")
                case["fixture"].serial.close.assert_called_once()

    def test_incomplete_result_terminates_only_pinned_live_boot_and_preserves_primary(
        self,
    ):
        for fault in ("incomplete", "cleanup-failure"):
            with self.subTest(fault=fault), self.attempt(fault=fault) as case:
                with self.assertRaisesRegex(RuntimeError, "incomplete/failed"):
                    self.execute(case)
                report = self.report(case)
                self.assertIn("incomplete/failed", report["primary_error"])
                self.assertEqual(report["failure"]["stage"], "native-result")
                self.assertIn(
                    "not a BLE/TCC diagnosis", report["failure"]["root_cause"]
                )
                self.assertEqual(
                    bool(report["cleanup_errors"]), fault == "cleanup-failure"
                )
                self.assertTrue(any("terminate" in c for c in case["commands"]))
                if fault == "cleanup-failure":
                    self.assertEqual(
                        report["cleanup_failures"][0]["stage"], "cleanup.native"
                    )
                case["fixture"].serial.close.assert_called_once()

    def test_publication_failure_preserves_primary_and_cannot_publish_a_pass(self):
        original = Path.write_text
        for fault in ("incomplete", ""):
            with self.subTest(fault=fault), self.attempt(fault=fault) as case:

                def write(path, *args, **kwargs):
                    if path.name == "result.json":
                        raise OSError("synthetic result disk full")
                    return original(path, *args, **kwargs)

                expected = RuntimeError if fault else OSError
                message = "incomplete/failed" if fault else "result disk full"
                with (
                    patch.object(Path, "write_text", new=write),
                    self.assertRaisesRegex(expected, message) as failure,
                ):
                    self.execute(case)
                self.assertFalse(list(case["work"].glob("*/result.json")))
                if fault:
                    self.assertTrue(
                        any(
                            "native result publication" in note
                            for note in failure.exception.__notes__
                        )
                    )
                case["fixture"].serial.close.assert_called_once()

    def test_foreign_boot_or_executable_refuses_pid_cleanup(self):
        for fault in ("foreign-boot", "foreign-pid", "stale-inspection"):
            with self.subTest(fault=fault), self.attempt(fault=fault) as case:
                if fault != "foreign-boot":
                    # Force a primary failure, then exercise cleanup identity rejection.
                    with (
                        patch.object(
                            hil,
                            "validate_result",
                            side_effect=RuntimeError("primary synthetic failure"),
                        ),
                        self.assertRaisesRegex(RuntimeError, "primary synthetic"),
                    ):
                        self.execute(case)
                else:
                    with self.assertRaisesRegex(RuntimeError, "foreign native"):
                        self.execute(case)
                self.assertFalse(any("terminate" in c for c in case["commands"]))
                self.assertTrue(self.report(case)["cleanup_errors"])
                case["fixture"].serial.close.assert_called_once()

    def test_launch_timeout_does_not_guess_pid_or_stop_existing_app(self):
        with self.attempt(fault="launch-failure") as case:
            with self.assertRaises(subprocess.TimeoutExpired):
                self.execute(case)
            self.assertFalse(any("terminate" in c for c in case["commands"]))
            case["fixture"].serial.close.assert_called_once()

    def test_prebuilt_without_ownership_helper_is_rejected_before_device_or_serial(
        self,
    ):
        with self.attempt() as case:
            plist = case["app"] / "Info.plist"
            info = plistlib.loads(plist.read_bytes())
            info.pop("BleHubNativeHilProtocolVersion")
            plist.write_bytes(plistlib.dumps(info))
            with self.assertRaisesRegex(ValueError, "ownership protocol"):
                self.execute(case)
            self.assertEqual(case["commands"], [])
            case["fixture"].open_serial.assert_not_called()

    def test_runtime_deadline_is_bounded_and_owned_cleanup_still_runs(self):
        for platform in ("ios", "macos"):
            with (
                self.subTest(platform=platform),
                self.attempt(platform=platform, fault="no-result") as case,
            ):
                with self.assertRaisesRegex(TimeoutError, "deadline"):
                    self.execute(case, platform=platform, timeout=1)
                self.assertIn("TimeoutError", self.report(case)["primary_error"])
                # GET_LIMITS only at startup, mutation boundaries and retirement,
                # not on each result/marker polling iteration.
                self.assertEqual(
                    case["fixture"].session.request.call_count,
                    6 if platform == "ios" else 5,
                )
                case["fixture"].serial.close.assert_called_once()
                if platform == "macos":
                    case["process"].terminate.assert_called_once()
                else:
                    self.assertTrue(any("terminate" in c for c in case["commands"]))

    def test_fixture_reboot_rejects_verdict_and_refuses_new_boot_cleanup_mutation(self):
        with self.attempt(fault="fixture-boot-drift") as case:
            with self.assertRaisesRegex(RuntimeError, "fixture boot changed during"):
                self.execute(case)
            report = self.report(case)
            self.assertEqual(report["status"], "failed")
            self.assertTrue(
                any(
                    "refusing cleanup mutation" in item
                    for item in report["cleanup_errors"]
                )
            )
            case["fixture"].serial.close.assert_called_once()
            # Only startup stop-advertising commands ran, none after detecting reboot.
            self.assertEqual(case["fixture"].gap.stop_advertising.call_count, 2)

    def test_fixture_cleanup_failure_cannot_turn_app_success_into_host_pass(self):
        with self.attempt(fault="fixture-cleanup-failure") as case:
            with self.assertRaisesRegex(RuntimeError, "advertising cleanup"):
                self.execute(case)
            report = self.report(case)
            self.assertEqual(report["status"], "failed")
            self.assertIsNone(report["primary_error"])
            self.assertTrue(report["cleanup_errors"])
            self.assertEqual(report["failure"]["stage"], "cleanup.fixture")
            case["fixture"].serial.close.assert_called_once()

    def test_missing_artifact_or_explicit_inputs_fail_before_hardware(self):
        with self.attempt() as case:
            for timeout in (0, float("nan"), 301):
                with self.assertRaises(ValueError):
                    self.execute(case, timeout=timeout)
            for policy in ("force-all", "1", ""):
                with self.assertRaises(ValueError):
                    self.execute(case, success_app=policy)
            with self.assertRaises(ValueError):
                self.execute(case, platform="macos", success_app="close-owned")
            case["app"].rename(case["root"] / "missing.app")
            with self.assertRaises(ValueError):
                self.execute(case)
            self.assertEqual(case["commands"], [])
            case["fixture"].open_serial.assert_not_called()

    def test_missing_contract_is_rejected_before_sdk_or_serial(self):
        with self.attempt() as case:
            case["contract"].unlink()
            with self.assertRaises((OSError, ValueError, RuntimeError)):
                self.execute(case)
            self.assertEqual(case["commands"], [])
            case["fixture"].open_serial.assert_not_called()

    def test_recipe_is_explicit_prebuilt_and_canonical_discovery_includes_test(self):
        recipes = (hil.ROOT / "just/apple/mod.just").read_text()
        section = recipes.split("rf-native-smoke platform ", 1)[1].split("\n\n", 1)[0]
        self.assertNotIn(": prepare", section)
        self.assertNotIn("trap", section)
        self.assertIn("tools.apple.native_hil", section)
        for name in (
            "runtime-ios-android",
            "runtime-ios-android-scenario",
            "rf-ios-external",
            "rf-ios-nrf52840",
            "rf-ios-nrf52840-notification",
            "rf-ios-nrf52840-indication",
            "rf-ios-nrf52840-rssi",
            "rf-ios-nrf52840-passive-disconnect",
            "rf-ios-nrf52840-recovery",
        ):
            header = next(
                line
                for line in recipes.splitlines()
                if line.startswith((name + ":", name + " "))
            )
            body = recipes.split(header + "\n", 1)[1].split("\n\n", 1)[0]
            self.assertNotIn("prepare", header)
            self.assertNotIn("blehub-nrf-indication-v2.json", header)
            self.assertIn("raise SystemExit", body)
            self.assertNotIn("trap", body)
            self.assertNotIn("peripheral", body)
        canonical = (hil.ROOT / "just/react-native/mod.just").read_text()
        self.assertIn(
            "discover -s tools/tests/react_native -t . -p 'test_*.py'", canonical
        )


if __name__ == "__main__":
    unittest.main()
