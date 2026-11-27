"""Protect run-ID correlation and terminal status of the Apple backend HIL."""

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from tools.apple import bleng_ios_backend as backend
from tools.apple import rf_fixture, test_ios_runtime
from tools.apple.bleng_ios_backend import (
    classify_central_recovery_spike_result,
    classify_connection_result,
    classify_fresh_owner_lifecycle_result,
    classify_gatt_result,
    classify_indication_result,
    classify_io_result,
    classify_notification_result,
    classify_result,
    classify_rssi_result,
    classify_scan_result,
)


class AppleBlengResultTests(unittest.TestCase):
    def test_requires_current_run_and_complete_initialization_snapshot(self) -> None:
        success = (
            "apple_bleng_corebluetooth_initialize=passed\n"
            "adapter_state=PoweredOn\n"
            "scan_authorization=Authorized\n"
            "connection_authorization=Authorized\n"
        )
        self.assertIsNone(
            classify_result("apple_bleng_corebluetooth_run_id=old\n" + success, "new")
        )
        result = classify_result(
            "apple_bleng_corebluetooth_run_id=new\n" + success, "new"
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["adapter_state"], "PoweredOn")
        with self.assertRaisesRegex(RuntimeError, "reported failure"):
            classify_result(
                "apple_bleng_corebluetooth_run_id=new\n"
                "apple_bleng_corebluetooth_initialize=failed\nerror=test\n",
                "new",
            )
        with self.assertRaisesRegex(RuntimeError, "missing connection_authorization"):
            classify_result(
                "apple_bleng_corebluetooth_run_id=new\n"
                + success.replace("connection_authorization=Authorized\n", ""),
                "new",
            )

    def test_scan_requires_current_run_profile_and_terminal_evidence(self) -> None:
        service = "fd000000-0000-4000-8000-000000000000"
        result = (
            "apple_bleng_corebluetooth_run_id=current\n"
            "apple_bleng_corebluetooth_scan=passed\n"
            "adapter_state=poweredOn\nscan_authorization=authorized\n"
            f"service_uuid={service}\nscan_session_id=1\nperipheral_id=2\n"
        )
        self.assertIsNone(classify_scan_result(result, "old", service))
        self.assertEqual(
            classify_scan_result(result, "current", service)["peripheral_id"], "2"
        )
        # Apple peripheral identity allocation legitimately begins at zero.
        self.assertEqual(
            classify_scan_result(
                result.replace("peripheral_id=2", "peripheral_id=0"),
                "current",
                service,
            )["peripheral_id"],
            "0",
        )
        with self.assertRaisesRegex(RuntimeError, "wrong fixture service"):
            classify_scan_result(
                result, "current", "00000000-0000-4000-8000-000000000000"
            )
        with self.assertRaisesRegex(RuntimeError, "lacks peripheral_id"):
            classify_scan_result(
                result.replace("peripheral_id=2\n", ""), "current", service
            )
        with self.assertRaisesRegex(RuntimeError, "RF scan failed"):
            classify_scan_result(
                "apple_bleng_corebluetooth_run_id=current\n"
                "apple_bleng_corebluetooth_scan=failed\nerror=Timeout\n",
                "current",
                service,
            )

    def test_central_recovery_spike_is_diagnostic_only_and_accepts_platform_timeout(
        self,
    ) -> None:
        timeout = (
            "apple_bleng_corebluetooth_run_id=current\n"
            "apple_bleng_corebluetooth_central_recovery_spike=attempted\n"
            "central_recovery_spike_outcome=platform-timeout\n"
            "first_scan_session_id=1\nfirst_matching_advertisements=2\n"
            "second_scan_session_id=2\nsecond_matching_advertisements=0\n"
            "production_gate_claim=none\n"
        )
        self.assertEqual(
            classify_central_recovery_spike_result(timeout, "current")[
                "central_recovery_spike_outcome"
            ],
            "platform-timeout",
        )
        observed = timeout.replace(
            "central_recovery_spike_outcome=platform-timeout",
            "central_recovery_spike_outcome=advertisement-observed",
        ).replace(
            "second_matching_advertisements=0", "second_matching_advertisements=1"
        )
        self.assertEqual(
            classify_central_recovery_spike_result(observed, "current")[
                "central_recovery_spike_outcome"
            ],
            "advertisement-observed",
        )
        self.assertIsNone(classify_central_recovery_spike_result(timeout, "old"))
        with self.assertRaisesRegex(RuntimeError, "production gate"):
            classify_central_recovery_spike_result(
                timeout.replace(
                    "production_gate_claim=none", "production_gate_claim=passed"
                ),
                "current",
            )
        with self.assertRaisesRegex(RuntimeError, "second-scan evidence"):
            classify_central_recovery_spike_result(
                observed.replace(
                    "second_matching_advertisements=1",
                    "second_matching_advertisements=0",
                ),
                "current",
            )

    def test_fresh_owner_lifecycle_requires_new_identities_and_no_gate_claim(
        self,
    ) -> None:
        result = (
            "apple_bleng_corebluetooth_run_id=current\n"
            "apple_bleng_fresh_owner_lifecycle=attempted\n"
            "fresh_owner_lifecycle_outcome=fresh-owner-created-and-retired\n"
            "old_owner_id=11111111-1111-4111-8111-111111111111\n"
            "new_owner_id=22222222-2222-4222-8222-222222222222\n"
            "old_backend_id=33333333-3333-4333-8333-333333333333\n"
            "new_backend_id=44444444-4444-4444-8444-444444444444\n"
            "production_gate_claim=none\n"
        )
        self.assertEqual(
            classify_fresh_owner_lifecycle_result(result, "current")[
                "fresh_owner_lifecycle_outcome"
            ],
            "fresh-owner-created-and-retired",
        )
        self.assertIsNone(classify_fresh_owner_lifecycle_result(result, "old"))
        with self.assertRaisesRegex(RuntimeError, "reused owner"):
            classify_fresh_owner_lifecycle_result(
                result.replace(
                    "new_owner_id=22222222-2222-4222-8222-222222222222",
                    "new_owner_id=11111111-1111-4111-8111-111111111111",
                ),
                "current",
            )
        with self.assertRaisesRegex(RuntimeError, "production gate"):
            classify_fresh_owner_lifecycle_result(
                result.replace(
                    "production_gate_claim=none", "production_gate_claim=passed"
                ),
                "current",
            )

    def test_connection_requires_matching_run_scan_and_ordered_active_lifecycle(
        self,
    ) -> None:
        service = "fd000000-0000-4000-8000-000000000000"
        result = (
            "apple_bleng_corebluetooth_run_id=current\n"
            "apple_bleng_corebluetooth_scan=passed\n"
            "adapter_state=poweredOn\nscan_authorization=authorized\n"
            f"service_uuid={service}\nscan_session_id=3\nperipheral_id=0\n"
            "apple_bleng_corebluetooth_connection=passed\n"
            "connection_id=4\n"
            "connection_state_sequence=connecting,connected,disconnected\n"
        )
        self.assertIsNone(classify_connection_result(result, "old", service))
        self.assertEqual(
            classify_connection_result(result, "current", service)["connection_id"],
            "4",
        )
        with self.assertRaisesRegex(RuntimeError, "ordered lifecycle"):
            classify_connection_result(
                result.replace(
                    "connecting,connected,disconnected", "connecting,connected"
                ),
                "current",
                service,
            )
        with self.assertRaisesRegex(RuntimeError, "lacks terminal status"):
            classify_connection_result(
                result.replace("apple_bleng_corebluetooth_connection=passed\n", ""),
                "current",
                service,
            )
        with self.assertRaisesRegex(RuntimeError, "connection failed"):
            classify_connection_result(
                "apple_bleng_corebluetooth_run_id=current\n"
                "apple_bleng_corebluetooth_connection=failed\nerror=Timeout\n",
                "current",
                service,
            )

    def test_gatt_requires_same_run_connection_and_profile_database(self) -> None:
        service = "fd000000-0000-4000-8000-000000000000"
        result = (
            "apple_bleng_corebluetooth_run_id=current\n"
            "apple_bleng_corebluetooth_scan=passed\n"
            "adapter_state=poweredOn\nscan_authorization=authorized\n"
            f"service_uuid={service}\nscan_session_id=3\nperipheral_id=0\n"
            "apple_bleng_corebluetooth_connection=passed\n"
            "connection_id=6\n"
            "connection_state_sequence=connecting,connected,disconnected\n"
            "apple_bleng_corebluetooth_gatt=passed\n"
            "gatt_session_id=7\ngatt_characteristic_count=5\ngatt_cccd_count=2\n"
        )
        self.assertIsNone(
            classify_gatt_result(result, "old", service, indication_only=True)
        )
        self.assertEqual(
            classify_gatt_result(result, "current", service, indication_only=True)[
                "gatt_session_id"
            ],
            "7",
        )
        with self.assertRaisesRegex(RuntimeError, "CCCD evidence"):
            classify_gatt_result(
                result.replace("gatt_cccd_count=2", "gatt_cccd_count=1"),
                "current",
                service,
                indication_only=True,
            )
        with self.assertRaisesRegex(RuntimeError, "RF GATT failed"):
            classify_gatt_result(
                "apple_bleng_corebluetooth_run_id=current\n"
                "apple_bleng_corebluetooth_gatt=failed\nerror=Timeout\n",
                "current",
                service,
                indication_only=True,
            )

    def test_io_requires_exact_readbacks_and_same_run_gatt(self) -> None:
        fixture = rf_fixture.load_profile(
            test_ios_runtime.NRF_PROFILE_PATH,
            write_with_response="10",
            write_without_response="ab",
        )
        result = (
            "apple_bleng_corebluetooth_run_id=current\n"
            "apple_bleng_corebluetooth_scan=passed\n"
            "adapter_state=poweredOn\nscan_authorization=authorized\n"
            f"service_uuid={fixture.serviceUuid}\nscan_session_id=3\nperipheral_id=0\n"
            "apple_bleng_corebluetooth_connection=passed\nconnection_id=6\n"
            "connection_state_sequence=connecting,connected,disconnected\n"
            "apple_bleng_corebluetooth_gatt=passed\n"
            "gatt_session_id=8\ngatt_characteristic_count=5\ngatt_cccd_count=2\n"
            "apple_bleng_corebluetooth_io=passed\n"
            "initial_read_hex=00\nwith_response_readback_hex=10\n"
            "without_response_delayed_readback_hex=ab\n"
        )
        self.assertIsNone(classify_io_result(result, "old", fixture))
        self.assertEqual(
            classify_io_result(result, "current", fixture)["gatt_session_id"], "8"
        )
        with self.assertRaisesRegex(RuntimeError, "exact without_response"):
            classify_io_result(
                result.replace(
                    "without_response_delayed_readback_hex=ab",
                    "without_response_delayed_readback_hex=00",
                ),
                "current",
                fixture,
            )
        with self.assertRaisesRegex(RuntimeError, "RF I/O failed"):
            classify_io_result(
                "apple_bleng_corebluetooth_run_id=current\n"
                "apple_bleng_corebluetooth_io=failed\nerror=Timeout\n",
                "current",
                fixture,
            )

    def test_notification_requires_exact_value_mode_subscription_and_gatt(self) -> None:
        fixture = rf_fixture.load_profile(test_ios_runtime.NRF_PROFILE_PATH)
        result = (
            "apple_bleng_corebluetooth_run_id=current\n"
            "apple_bleng_corebluetooth_scan=passed\n"
            "adapter_state=poweredOn\nscan_authorization=authorized\n"
            f"service_uuid={fixture.serviceUuid}\nscan_session_id=3\nperipheral_id=0\n"
            "apple_bleng_corebluetooth_connection=passed\nconnection_id=6\n"
            "connection_state_sequence=connecting,connected,disconnected\n"
            "apple_bleng_corebluetooth_gatt=passed\n"
            "gatt_session_id=8\ngatt_characteristic_count=5\ngatt_cccd_count=2\n"
            "apple_bleng_corebluetooth_notification=passed\n"
            f"notification_value_hex={fixture.notificationValueHex}\n"
            "actual_mode=notification\nsubscription_id=11\n"
            "notification_enable_disable_order=passed\n"
            "notification_post_disable_silence=passed\n"
        )
        self.assertIsNone(classify_notification_result(result, "old", fixture))
        values = classify_notification_result(result, "current", fixture)
        self.assertIsNotNone(values)
        assert values is not None
        self.assertEqual(values["subscription_id"], "11")
        with self.assertRaisesRegex(RuntimeError, "exact fixture value"):
            classify_notification_result(
                result.replace(
                    f"notification_value_hex={fixture.notificationValueHex}",
                    "notification_value_hex=0000",
                ),
                "current",
                fixture,
            )
        with self.assertRaisesRegex(RuntimeError, "notification mode"):
            classify_notification_result(
                result.replace("actual_mode=notification", "actual_mode=indication"),
                "current",
                fixture,
            )
        with self.assertRaisesRegex(RuntimeError, "subscription_id"):
            classify_notification_result(
                result.replace("subscription_id=11", "subscription_id=unknown"),
                "current",
                fixture,
            )
        with self.assertRaisesRegex(RuntimeError, "notification_post_disable_silence"):
            classify_notification_result(
                result.replace("notification_post_disable_silence=passed\n", ""),
                "current",
                fixture,
            )
        with self.assertRaisesRegex(RuntimeError, "notification failed"):
            classify_notification_result(
                "apple_bleng_corebluetooth_run_id=current\n"
                "apple_bleng_corebluetooth_notification=failed\nerror=Timeout\n",
                "current",
                fixture,
            )

    def test_indication_requires_strict_rejection_mode_value_and_silence(self) -> None:
        fixture = rf_fixture.load_profile(test_ios_runtime.NRF_PROFILE_PATH)
        result = (
            "apple_bleng_corebluetooth_run_id=current\n"
            "apple_bleng_corebluetooth_scan=passed\n"
            "adapter_state=poweredOn\nscan_authorization=authorized\n"
            f"service_uuid={fixture.serviceUuid}\nscan_session_id=3\nperipheral_id=0\n"
            "apple_bleng_corebluetooth_connection=passed\nconnection_id=6\n"
            "connection_state_sequence=connecting,connected,disconnected\n"
            "apple_bleng_corebluetooth_gatt=passed\n"
            "gatt_session_id=8\ngatt_characteristic_count=5\ngatt_cccd_count=2\n"
            "apple_bleng_corebluetooth_indication=passed\n"
            "indication_value_hex=ef\nactual_mode=indication\nsubscription_id=11\n"
            "indication_enable_disable_order=passed\n"
            "indication_post_disable_silence=passed\n"
            "strict_dual_indication_rejected=passed\n"
            "strict_dual_no_active_route=passed\n"
        )
        self.assertIsNone(classify_indication_result(result, "old", fixture))
        self.assertEqual(
            classify_indication_result(result, "current", fixture)["subscription_id"],
            "11",
        )
        for old, new, message in (
            (
                "indication_value_hex=ef",
                "indication_value_hex=00",
                "exact fixture value",
            ),
            ("actual_mode=indication", "actual_mode=notification", "indication mode"),
            ("subscription_id=11", "subscription_id=unknown", "subscription_id"),
            ("indication_post_disable_silence=passed", "", "post_disable_silence"),
            ("strict_dual_indication_rejected=passed", "", "strict_dual_indication"),
            ("strict_dual_no_active_route=passed", "", "strict_dual_no_active_route"),
            ("gatt_cccd_count=2", "gatt_cccd_count=1", "CCCD evidence"),
        ):
            with self.subTest(old=old), self.assertRaisesRegex(RuntimeError, message):
                classify_indication_result(result.replace(old, new), "current", fixture)
        with self.assertRaisesRegex(RuntimeError, "indication failed"):
            classify_indication_result(
                "apple_bleng_corebluetooth_run_id=current\n"
                "apple_bleng_corebluetooth_indication=failed\nerror=Timeout\n",
                "current",
                fixture,
            )
        with self.assertRaisesRegex(RuntimeError, "terminal status"):
            classify_indication_result(
                result.replace("apple_bleng_corebluetooth_indication=passed\n", ""),
                "current",
                fixture,
            )
        with self.assertRaisesRegex(RuntimeError, "terminal status"):
            classify_indication_result(
                result.replace(
                    "apple_bleng_corebluetooth_indication=passed",
                    "apple_bleng_corebluetooth_notification=passed",
                ),
                "current",
                fixture,
            )

    def test_indication_provider_uses_direct_start_mode(self) -> None:
        with (
            mock.patch.object(
                backend,
                "_provider_status",
                side_effect=[
                    "nrf52840_fixture=stopped",
                    "nrf52840_fixture_pid=123\nnrf52840_fixture_pgid=123\n",
                ],
            ),
            mock.patch.object(backend.subprocess, "run") as run,
        ):
            run.return_value.returncode = 0
            run.return_value.stdout = (
                "nrf52840_fixture_pid=123\nnrf52840_fixture_pgid=123\n"
            )
            run.return_value.stderr = ""
            self.assertEqual(
                backend._start_owned_provider(
                    Path("/tmp/bleng-test"), mode="indication"
                ),
                ("123", "123"),
            )
            command = run.call_args.args[0]
            self.assertEqual(
                command[-4:],
                [
                    "--coordination-path",
                    "/tmp/bleng-test",
                    "--coordination-mode",
                    "indication",
                ],
            )

    def test_stop_owned_provider_uses_exact_identity_command(self) -> None:
        with mock.patch.object(
            backend.subprocess,
            "run",
            return_value=mock.Mock(returncode=0, stdout="", stderr=""),
        ) as run:
            backend._stop_owned_provider(("123", "456"))

        self.assertEqual(
            run.call_args.args[0][-5:],
            ["stop-owned", "--pid", "123", "--pgid", "456"],
        )

    def test_start_owned_provider_rolls_back_exact_identity_when_status_fails(
        self,
    ) -> None:
        with (
            mock.patch.object(
                backend,
                "_provider_status",
                side_effect=[
                    "nrf52840_fixture=stopped",
                    RuntimeError("status unavailable"),
                ],
            ),
            mock.patch.object(
                backend.subprocess,
                "run",
                return_value=mock.Mock(
                    returncode=0,
                    stdout=("nrf52840_fixture_pid=123\nnrf52840_fixture_pgid=123\n"),
                    stderr="",
                ),
            ),
            mock.patch.object(backend, "_stop_spawned_provider") as stop,
            self.assertRaisesRegex(RuntimeError, "status unavailable"),
        ):
            backend._start_owned_provider(Path("/tmp/bleng-test"), mode="recovery-ios")

        stop.assert_called_once_with("123", "123")

    def test_start_owned_provider_rolls_back_exact_identity_when_status_changes(
        self,
    ) -> None:
        with (
            mock.patch.object(
                backend,
                "_provider_status",
                side_effect=[
                    "nrf52840_fixture=stopped",
                    "nrf52840_fixture_pid=999\nnrf52840_fixture_pgid=999\n",
                ],
            ),
            mock.patch.object(
                backend.subprocess,
                "run",
                return_value=mock.Mock(
                    returncode=0,
                    stdout=("nrf52840_fixture_pid=123\nnrf52840_fixture_pgid=123\n"),
                    stderr="",
                ),
            ),
            mock.patch.object(backend, "_stop_spawned_provider") as stop,
            self.assertRaisesRegex(RuntimeError, "did not retain"),
        ):
            backend._start_owned_provider(Path("/tmp/bleng-test"), mode="recovery-ios")

        stop.assert_called_once_with("123", "123")

    def test_indication_runner_uses_distinct_markers_and_owned_provider(self) -> None:
        fixture = rf_fixture.load_profile(test_ios_runtime.NRF_PROFILE_PATH)
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(backend, "WORK", Path(directory)),
            mock.patch.object(backend.uuid, "uuid4", return_value="current"),
            mock.patch.object(
                backend, "_start_owned_provider", return_value=("123", "123")
            ) as start,
            mock.patch.object(backend, "_stop_owned_provider") as stop,
            mock.patch.object(backend.consumer, "device_json") as launch,
            mock.patch.object(backend, "_wait_for_device_marker") as device_marker,
            mock.patch.object(backend, "_wait_for_host_marker") as host_marker,
            mock.patch.object(backend, "_write_host_marker") as write_marker,
            mock.patch.object(backend, "_copy_marker_to_device") as copy_marker,
            mock.patch.object(
                backend,
                "_wait_for_subscription_result",
                return_value={"subscription_id": "11"},
            ) as result,
        ):
            backend._subscription_device_run(
                "xcrun", "device", "bundle", fixture, mode="indication"
            )
            start.assert_called_once_with(
                Path(directory) / "rf-indication-current", mode="indication"
            )
            self.assertIn("--rf-indication", launch.call_args.args[1])
            self.assertIn("--rf-indication-only", launch.call_args.args[1])
            self.assertNotIn("--rf-notification", launch.call_args.args[1])
            self.assertEqual(
                [call.args[4] for call in device_marker.call_args_list],
                ["indication-enabled", "indication-disabled"],
            )
            self.assertEqual(
                [call.args[1] for call in write_marker.call_args_list],
                ["subscription-enabled", "subscription-disabled"],
            )
            self.assertEqual(
                [call.args[1] for call in host_marker.call_args_list],
                ["value-applied", "post-disable-triggered"],
            )
            copy_marker.assert_called_once_with(
                "xcrun", "device", "bundle", "current", "post-disable-triggered"
            )
            self.assertEqual(result.call_args.kwargs, {"mode": "indication"})
            stop.assert_called_once_with(("123", "123"))

    def test_rssi_requires_current_run_connected_event_and_int16(self) -> None:
        service = "fd000000-0000-4000-8000-000000000000"
        result = (
            "apple_bleng_corebluetooth_run_id=current\n"
            "apple_bleng_corebluetooth_scan=passed\n"
            "adapter_state=poweredOn\nscan_authorization=authorized\n"
            f"service_uuid={service}\nscan_session_id=3\nperipheral_id=0\n"
            "apple_bleng_corebluetooth_connection=passed\nconnection_id=6\n"
            "connection_state_sequence=connecting,connected,disconnected\n"
            "apple_bleng_corebluetooth_rssi=passed\nrssi_dbm=-50\n"
        )
        self.assertIsNone(classify_rssi_result(result, "old", service))
        self.assertEqual(
            classify_rssi_result(result, "current", service)["rssi_dbm"], "-50"
        )
        with self.assertRaisesRegex(RuntimeError, "integer RSSI"):
            classify_rssi_result(
                result.replace("rssi_dbm=-50", "rssi_dbm=unknown"), "current", service
            )
        with self.assertRaisesRegex(RuntimeError, "Int16 range"):
            classify_rssi_result(
                result.replace("rssi_dbm=-50", "rssi_dbm=32768"), "current", service
            )


class AppleBlengRecoveryTests(unittest.TestCase):
    def receipt(
        self, *, mode: str = "recovery", connection_error_cycles: tuple[int, ...] = ()
    ) -> str:
        fixture = rf_fixture.load_profile(test_ios_runtime.NRF_PROFILE_PATH)
        cycles = 3 if mode == "recovery" else 1
        lines = [
            "apple_bleng_corebluetooth_run_id=current",
            "apple_bleng_corebluetooth_scan=passed",
            "adapter_state=poweredOn",
            "scan_authorization=authorized",
            "connection_authorization=authorized",
            f"service_uuid={fixture.serviceUuid}",
            "scan_session_id=3",
            "peripheral_id=0",
            f"cycles={cycles}",
            "owner_count=1",
            "final_disconnect=passed",
            f"apple_bleng_corebluetooth_{mode}=passed",
        ]
        for cycle in range(1, cycles + 1):
            prefix = f"cycle_{cycle}_"
            passive_result = (
                "connection-error"
                if cycle in connection_error_cycles
                else "disconnected"
            )
            evidence = {
                "connection_id": str(cycle * 10),
                "gatt_session_id": str(cycle * 10 + 1),
                "subscription_id": str(cycle * 10 + 2),
                "characteristic_id": str(cycle * 10 + 3),
                "recovered_connection_id": str((cycle + 1) * 10),
                "recovered_gatt_session_id": str((cycle + 1) * 10 + 1),
                "enable_index": str(cycle * 100),
                "enable_terminal_index": str(cycle * 100 + 1),
                "value_index": str(cycle * 100 + 2),
                "disconnect_index": str(cycle * 100 + 3),
                "status": "passed",
                "actual_mode": "notification",
                "notification_value_hex": "ef",
                "passive_command": "absent",
                "active_subscription_at_disconnect": "passed",
                "passive_result": passive_result,
                "connection_state_sequence": f"connecting,connected,{passive_result}",
                "stale_connection": "ConnectionNotActive",
                "stale_gatt": "ConnectionNotActive",
                "stale_subscription": "ConnectionNotActive",
                "stale_gatt_on_reconnect": "GattNotReady",
                "stale_subscription_on_reconnect": "SubscriptionNotActive",
                "post_disconnect_silence": "passed",
                "initial_read_hex": "00",
                "recovered_read_hex": "00",
                "gatt_characteristic_count": "5",
                "gatt_cccd_count": "2",
            }
            if cycle in connection_error_cycles:
                evidence["passive_error_category"] = "connection"
            lines.extend(f"{prefix}{key}={value}" for key, value in evidence.items())
        return "\n".join(lines) + "\n"

    def test_receipt_rejects_missing_wrong_stale_and_ui_only_evidence(self) -> None:
        fixture = rf_fixture.load_profile(test_ios_runtime.NRF_PROFILE_PATH)
        for mode in ("passive-disconnect", "recovery"):
            receipt = self.receipt(mode=mode)
            self.assertIsNone(
                backend.classify_recovery_result(receipt, "old", fixture, mode=mode)
            )
            result = backend.classify_recovery_result(
                receipt, "current", fixture, mode=mode
            )
            assert result is not None
            self.assertEqual(result["cycles"], "3" if mode == "recovery" else "1")
            mutations = [
                receipt.replace(
                    "cycle_1_active_subscription_at_disconnect=passed\n", ""
                ),
                receipt.replace(
                    "cycle_1_passive_command=absent", "cycle_1_passive_command=99"
                ),
                receipt.replace(
                    "cycle_1_notification_value_hex=ef",
                    "cycle_1_notification_value_hex=00",
                ),
                receipt.replace(
                    "cycle_1_actual_mode=notification", "cycle_1_actual_mode=indication"
                ),
                receipt.replace("cycle_1_stale_gatt_on_reconnect=GattNotReady\n", ""),
                receipt.replace(
                    "cycle_1_stale_subscription=ConnectionNotActive",
                    "cycle_1_stale_subscription=Timeout",
                ),
                receipt.replace(
                    "cycle_1_recovered_read_hex=00", "cycle_1_recovered_read_hex=ef"
                ),
                receipt.replace(
                    "cycle_1_post_disconnect_silence=passed",
                    "cycle_1_post_disconnect_silence=failed",
                ),
                receipt.replace("cycle_1_value_index=102", "cycle_1_value_index=104"),
                receipt.replace("owner_count=1", "owner_count=3"),
                receipt + "cycle_1_passive_command=absent\n",
                f"apple_bleng_corebluetooth_run_id=current\napple_bleng_corebluetooth_{mode}=passed\n",
                receipt.replace(
                    f"apple_bleng_corebluetooth_{mode}=passed",
                    f"apple_bleng_corebluetooth_{mode}=failed",
                ),
            ]
            for invalid in mutations:
                with (
                    self.subTest(mode=mode, receipt=invalid),
                    self.assertRaises(RuntimeError),
                ):
                    backend.classify_recovery_result(
                        invalid, "current", fixture, mode=mode
                    )

    def test_passive_result_preserves_error_category_and_matching_lifecycle(
        self,
    ) -> None:
        fixture = rf_fixture.load_profile(test_ios_runtime.NRF_PROFILE_PATH)
        for mode in ("passive-disconnect", "recovery"):
            success = self.receipt(mode=mode)
            error = self.receipt(mode=mode, connection_error_cycles=(1, 3))
            result = backend.classify_recovery_result(
                error, "current", fixture, mode=mode
            )
            assert result is not None
            self.assertEqual(result["cycle_1_passive_result"], "connection-error")
            self.assertEqual(result["cycle_1_passive_error_category"], "connection")
            self.assertEqual(
                result["cycle_1_connection_state_sequence"],
                "connecting,connected,connection-error",
            )
            if mode == "recovery":
                self.assertEqual(result["cycle_2_passive_result"], "disconnected")
                self.assertNotIn("cycle_2_passive_error_category", result)
                self.assertEqual(result["cycle_3_passive_result"], "connection-error")
            invalid_receipts = [
                success.replace("cycle_1_passive_result=disconnected\n", ""),
                success.replace(
                    "cycle_1_passive_result=disconnected",
                    "cycle_1_passive_result=passed",
                ),
                success + "cycle_1_passive_error_category=connection\n",
                error.replace("cycle_1_passive_error_category=connection\n", ""),
                error.replace(
                    "cycle_1_passive_error_category=connection",
                    "cycle_1_passive_error_category=gatt",
                ),
                error.replace(
                    "cycle_1_passive_result=connection-error",
                    "cycle_1_passive_result=disconnected",
                ),
                error.replace(
                    "cycle_1_connection_state_sequence=connecting,connected,connection-error",
                    "cycle_1_connection_state_sequence=connecting,connected,disconnected",
                ),
                error.replace(
                    "cycle_1_passive_command=absent", "cycle_1_passive_command=99"
                ),
                error.replace("cycle_1_value_index=102", "cycle_1_value_index=104"),
                success.replace(
                    "cycle_1_connection_state_sequence=connecting,connected,disconnected",
                    "cycle_1_connection_state_sequence=connecting,connected,connection-error",
                ),
            ]
            for receipt in invalid_receipts:
                with (
                    self.subTest(mode=mode, receipt=receipt),
                    self.assertRaises(RuntimeError),
                ):
                    backend.classify_recovery_result(
                        receipt, "current", fixture, mode=mode
                    )

    def test_three_cycles_require_fresh_ids_and_keep_the_recovered_link(self) -> None:
        fixture = rf_fixture.load_profile(test_ios_runtime.NRF_PROFILE_PATH)
        receipt = self.receipt()
        for old, new in (
            ("cycles=3", "cycles=2"),
            ("cycle_3_status=passed", "cycle_4_status=passed"),
            ("cycle_2_connection_id=20", "cycle_2_connection_id=10"),
            ("cycle_2_connection_id=20", "cycle_2_connection_id=25"),
            ("cycle_2_gatt_session_id=21", "cycle_2_gatt_session_id=11"),
            ("cycle_3_subscription_id=32", "cycle_3_subscription_id=12"),
            (
                "cycle_3_recovered_connection_id=40",
                "cycle_3_recovered_connection_id=10",
            ),
            ("cycle_2_enable_index=200", "cycle_2_enable_index=100"),
        ):
            with self.subTest(field=old), self.assertRaises(RuntimeError):
                backend.classify_recovery_result(
                    receipt.replace(old, new), "current", fixture
                )

    def test_coordinated_runner_bridges_exact_protocol_and_always_cleans_up(
        self,
    ) -> None:
        fixture = rf_fixture.load_profile(test_ios_runtime.NRF_PROFILE_PATH)
        for mode in ("passive-disconnect", "recovery"):
            for fail_at in (None, "fault-triggered", "cycle-passed"):
                with (
                    self.subTest(mode=mode, failure=fail_at),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    events = []

                    def device_wait(*args, events=events, fail_at=fail_at, **kwargs):
                        events.append(("device", args[4]))
                        if args[4].endswith(str(fail_at)):
                            raise RuntimeError("device failure")

                    def host_wait(*args, events=events, fail_at=fail_at, **kwargs):
                        events.append(("host", args[1]))
                        if args[1].endswith(str(fail_at)):
                            raise RuntimeError("provider failure")

                    with (
                        mock.patch.object(backend, "WORK", Path(directory)),
                        mock.patch.object(
                            backend.uuid, "uuid4", return_value="current"
                        ),
                        mock.patch.object(
                            backend,
                            "_start_owned_provider",
                            return_value=("123", "123"),
                        ) as start,
                        mock.patch.object(backend, "_stop_owned_provider") as stop,
                        mock.patch.object(backend.consumer, "device_json") as launch,
                        mock.patch.object(
                            backend, "_wait_for_device_marker", side_effect=device_wait
                        ),
                        mock.patch.object(
                            backend, "_wait_for_host_marker", side_effect=host_wait
                        ),
                        mock.patch.object(
                            backend,
                            "_write_host_marker",
                            side_effect=lambda path, marker, events=events: (
                                events.append(("write", marker))
                            ),
                        ),
                        mock.patch.object(
                            backend,
                            "_copy_marker_to_device",
                            side_effect=lambda *args, events=events: events.append(
                                ("copy", args[4])
                            ),
                        ),
                        mock.patch.object(
                            backend,
                            "_wait_for_subscription_result",
                            return_value={"cycles": "3" if mode == "recovery" else "1"},
                        ) as result,
                    ):
                        if fail_at is None:
                            backend._recovery_device_run(
                                "xcrun", "device", "bundle", fixture, mode=mode
                            )
                        else:
                            with self.assertRaises(RuntimeError):
                                backend._recovery_device_run(
                                    "xcrun", "device", "bundle", fixture, mode=mode
                                )
                        start.assert_called_once_with(
                            Path(directory) / f"rf-{mode}-current",
                            mode="recovery-ios" if mode == "recovery" else mode,
                        )
                        stop.assert_called_once_with(("123", "123"))
                        self.assertIn(f"--rf-{mode}", launch.call_args.args[1])
                        self.assertIn(
                            "--rf-notification-value-hex", launch.call_args.args[1]
                        )
                        self.assertIn("--rf-initial-hex", launch.call_args.args[1])
                        self.assertIn("--rf-indication-only", launch.call_args.args[1])
                        if fail_at is not None:
                            result.assert_not_called()
                            continue
                        expected = (
                            [("device", "coordination-ready")]
                            if mode == "recovery"
                            else []
                        )
                        for cycle in range(1, 4 if mode == "recovery" else 2):
                            prefix = f"cycle-{cycle}/" if mode == "recovery" else ""
                            if mode == "recovery":
                                expected += [
                                    ("host", prefix + "fixture-ready"),
                                    ("copy", prefix + "fixture-ready"),
                                ]
                            expected += [
                                ("device", prefix + "subscription-enabled"),
                                ("write", prefix + "subscription-enabled"),
                                ("host", prefix + "value-applied"),
                                ("device", prefix + "connected"),
                                ("write", prefix + "connected"),
                                ("host", prefix + "fault-triggered"),
                                ("copy", prefix + "fault-triggered"),
                                ("device", prefix + "passive-disconnected"),
                            ]
                            if mode == "recovery":
                                expected += [("write", prefix + "passive-disconnected")]
                            expected += [
                                ("host", prefix + "recovery-ready"),
                                ("copy", prefix + "recovery-ready"),
                                ("device", prefix + "cycle-passed"),
                            ]
                            if mode == "recovery":
                                expected += [("write", prefix + "cycle-passed")]
                        self.assertEqual(events, expected)

    def test_preflight_and_install_finish_before_provider_start(self) -> None:
        for mode in ("passive-disconnect", "recovery"):
            events = []
            with (
                mock.patch.object(
                    backend.consumer_ios, "executable", return_value="xcrun"
                ),
                mock.patch.object(
                    backend.consumer,
                    "connected_device",
                    return_value=("device", "udid", "TEAM"),
                ),
                mock.patch.object(
                    backend,
                    "build",
                    side_effect=lambda *a, events=events, **k: (
                        events.append("build") or Path("app")
                    ),
                ),
                mock.patch.object(
                    backend.consumer,
                    "device_json",
                    side_effect=lambda *a, events=events, **k: events.append("install"),
                ),
                mock.patch.object(backend.consumer, "wait_for_unlocked_device"),
                mock.patch.object(
                    backend,
                    "_recovery_device_run",
                    side_effect=lambda *a, events=events, **k: events.append(
                        "provider"
                    ),
                ),
            ):
                backend.device_run(rf_scenario=mode)
                self.assertEqual(events, ["build", "install", "provider"])
                for initial, notification, profile in (
                    ("01", None, test_ios_runtime.NRF_PROFILE_PATH),
                    (None, "00", test_ios_runtime.NRF_PROFILE_PATH),
                    (None, None, Path("other.json")),
                ):
                    events.clear()
                    with self.assertRaises(ValueError):
                        backend.device_run(
                            rf_scenario=mode,
                            initial_hex=initial,
                            notification_value_hex=notification,
                            profile=profile,
                        )
                    self.assertEqual(events, [])

    def test_final_receipt_ignores_stale_runs_and_archives_only_valid_evidence(
        self,
    ) -> None:
        fixture = rf_fixture.load_profile(test_ios_runtime.NRF_PROFILE_PATH)
        for mode in ("passive-disconnect", "recovery"):
            receipt = self.receipt(mode=mode)
            with (
                tempfile.TemporaryDirectory() as directory,
                mock.patch.object(backend, "WORK", Path(directory)),
                mock.patch.object(backend.time, "sleep"),
                mock.patch.object(
                    backend,
                    "_pull_device_file",
                    side_effect=[
                        receipt.replace("run_id=current", "run_id=old"),
                        receipt,
                    ],
                ),
            ):
                values = backend._wait_for_subscription_result(
                    "xcrun", "device", "bundle", "current", fixture, mode=mode
                )
                self.assertEqual(values["cycle_1_passive_command"], "absent")
                self.assertEqual(
                    (Path(directory) / f"current-{mode}-result.txt").read_text(),
                    receipt,
                )

    def test_host_wait_fails_fast_on_provider_exit_or_scoped_device_failure(
        self,
    ) -> None:
        fixture = rf_fixture.load_profile(test_ios_runtime.NRF_PROFILE_PATH)
        for provider_exit in (False, True):
            with (
                tempfile.TemporaryDirectory() as directory,
                mock.patch.object(backend, "WORK", Path(directory)),
                mock.patch.object(backend.uuid, "uuid4", return_value="current"),
                mock.patch.object(
                    backend,
                    "_start_owned_provider",
                    return_value=("123", "123"),
                ),
                mock.patch.object(backend, "_stop_owned_provider") as stop,
                mock.patch.object(backend.consumer, "device_json"),
                mock.patch.object(backend, "_wait_for_device_marker"),
                mock.patch.object(
                    backend,
                    "_provider_status",
                    return_value="stopped"
                    if provider_exit
                    else "nrf52840_fixture_pid=123\nnrf52840_fixture_pgid=123\n",
                ),
                mock.patch.object(
                    backend,
                    "_pull_device_file",
                    return_value="apple_bleng_corebluetooth_run_id=current\napple_bleng_corebluetooth_recovery=failed\nerror=lost\n",
                ),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "provider exited|failed during coordination"
                ):
                    backend._recovery_device_run(
                        "xcrun", "device", "bundle", fixture, mode="recovery"
                    )
                stop.assert_called_once_with(("123", "123"))


class AppleBlengRecoveryHostTests(unittest.TestCase):
    def _check_host_cli(
        self,
        *,
        stage: str = "summary",
        outcome: int | subprocess.TimeoutExpired = 0,
        raw_summary: bytes | None = None,
        error: str | None = None,
        stale: str | None = None,
        missing_bundle: bool = False,
    ) -> None:
        summary = {
            "result": "Passed",
            "totalTestCount": 3,
            "passedTests": 3,
            "failedTests": 0,
            "skippedTests": 0,
            "expectedFailures": 0,
            "testFailures": [],
        }
        if raw_summary is None:
            raw_summary = json.dumps(summary).encode("utf-8")
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            project = work / backend.PROJECT.name
            run_id = backend.uuid.UUID("11111111-2222-4333-8444-555555555555")
            results = work / "recovery-host/Results"
            bundle = results / f"{run_id}.xcresult"
            attempt = results / str(run_id)
            log = attempt / "xcodebuild.log"
            summary_path = attempt / "summary.json"
            summary_log = attempt / "summary.log"
            results.mkdir(parents=True)
            old_report = results / "old-run.xcresult"
            old_report.mkdir()
            (old_report / "evidence").write_bytes(b"old evidence")
            if stale == "bundle":
                bundle.mkdir()
                (bundle / "evidence").write_bytes(b"do not adopt")
            elif stale == "symlink":
                bundle.symlink_to(results / "missing")
            elif stale == "attempt":
                attempt.mkdir()
                log.write_bytes(b"old log")
            generate_command = [
                "/managed/xcodegen",
                "generate",
                "--quiet",
                "--spec",
                str(backend.SPEC),
                "--project",
                str(work),
            ]
            test_command = [
                "/xcode/xcodebuild",
                "-quiet",
                "-project",
                str(project),
                "-scheme",
                "BlengAppleRecoveryHostTests",
                "-destination",
                "platform=macOS",
                "-derivedDataPath",
                str(work / "recovery-host/DerivedData"),
                "-resultBundlePath",
                str(bundle),
                "CODE_SIGNING_ALLOWED=NO",
                "test",
            ]
            summary_command = [
                "/xcode/xcrun",
                "xcresulttool",
                "get",
                "test-results",
                "summary",
                "--path",
                str(bundle),
                "--compact",
            ]

            def run(
                command: list[str], **kwargs: Any
            ) -> subprocess.CompletedProcess[str]:
                # Only these real process boundaries are permitted, even on failure paths.
                self.assertIn(
                    command, (generate_command, test_command, summary_command)
                )
                current_stage = (
                    "generate"
                    if command == generate_command
                    else "test"
                    if command == test_command
                    else "summary"
                )
                if current_stage == "test":
                    kwargs["stdout"].write("XCTest output retained\n")
                    if not missing_bundle:
                        bundle.mkdir()
                        (bundle / "evidence").write_bytes(b"this run")
                elif current_stage == "summary":
                    kwargs["stdout"].write(raw_summary)
                    kwargs["stderr"].write(b"summary diagnostics retained\n")
                if stage == current_stage:
                    if isinstance(outcome, subprocess.TimeoutExpired):
                        raise outcome
                    if outcome:
                        return subprocess.CompletedProcess(command, outcome, "", "")
                if current_stage == "generate":
                    project.mkdir()
                    (project / "project.pbxproj").write_text("// generated\n")
                return subprocess.CompletedProcess(command, 0, "", "")

            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                mock.patch.object(backend, "WORK", work),
                mock.patch.object(backend, "PROJECT", project),
                mock.patch.object(backend.platform, "system", return_value="Darwin"),
                mock.patch.object(backend.consumer, "verify_package") as verify,
                mock.patch.object(
                    backend.uuid, "uuid4", return_value=run_id
                ) as unique_id,
                mock.patch.object(
                    backend.xcodegen_toolchain,
                    "verify",
                    return_value=Path("/managed/xcodegen"),
                ),
                mock.patch.object(
                    backend.consumer_ios,
                    "executable",
                    side_effect={
                        "xcodebuild": "/xcode/xcodebuild",
                        "xcrun": "/xcode/xcrun",
                    }.__getitem__,
                ),
                mock.patch.object(
                    backend.subprocess, "run", side_effect=run
                ) as process,
                mock.patch.object(
                    backend.sys, "argv", ["bleng_ios_backend", "test-recovery-host"]
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                status = backend.main()

            verify.assert_called_once_with()
            self.assertEqual(status, 0 if error is None else 1)
            self.assertEqual(
                "apple_bleng_recovery_host=passed" in stdout.getvalue(), error is None
            )
            if error:
                self.assertIn(error, stderr.getvalue())
            else:
                self.assertIn(
                    "apple_bleng_recovery_host_test_count=3", stdout.getvalue()
                )
            self.assertEqual((old_report / "evidence").read_bytes(), b"old evidence")
            process.assert_any_call(
                generate_command,
                cwd=backend.ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            if stage == "generate":
                self.assertEqual(process.call_count, 1)
                unique_id.assert_not_called()
                return
            unique_id.assert_called_once_with()
            if stale:
                self.assertEqual(process.call_count, 1)
                if stale == "bundle":
                    self.assertEqual(
                        (bundle / "evidence").read_bytes(), b"do not adopt"
                    )
                    self.assertFalse(attempt.exists())
                elif stale == "symlink":
                    self.assertTrue(bundle.is_symlink())
                    self.assertFalse(attempt.exists())
                else:
                    self.assertEqual(log.read_bytes(), b"old log")
                return
            self.assertEqual(log.read_text(), "XCTest output retained\n")
            for path in (bundle, log, summary_path, summary_log):
                self.assertIn(str(path), stdout.getvalue())
                if error:
                    self.assertIn(str(path), stderr.getvalue())
            test_call = process.call_args_list[1]
            self.assertEqual(
                test_call,
                mock.call(
                    test_command,
                    cwd=backend.ROOT,
                    stdin=subprocess.DEVNULL,
                    stdout=mock.ANY,
                    stderr=subprocess.STDOUT,
                    check=False,
                    timeout=180,
                ),
            )
            if stage == "test" or missing_bundle:
                self.assertEqual(process.call_count, 2)
                self.assertFalse(summary_path.exists())
            else:
                self.assertEqual(process.call_count, 3)
                process.assert_called_with(
                    summary_command,
                    cwd=backend.ROOT,
                    stdin=subprocess.DEVNULL,
                    stdout=mock.ANY,
                    stderr=mock.ANY,
                    check=False,
                    timeout=30,
                )
                self.assertEqual(summary_path.read_bytes(), raw_summary)
                self.assertEqual(
                    summary_log.read_bytes(), b"summary diagnostics retained\n"
                )
            if not missing_bundle:
                self.assertEqual((bundle / "evidence").read_bytes(), b"this run")

    def test_cli_generates_then_runs_only_unsigned_macos_xctest(self) -> None:
        cases = (
            ("summary", 0, None),
            ("test", 65, "exit 65"),
            ("test", subprocess.TimeoutExpired("xcodebuild", 180), "timed out"),
            ("generate", 23, "exit 23"),
            ("generate", subprocess.TimeoutExpired("xcodegen", 60), "timed out"),
            ("summary", 17, "exit 17"),
            ("summary", subprocess.TimeoutExpired("xcresulttool", 30), "timed out"),
        )
        for stage, outcome, error in cases:
            with self.subTest(stage=stage, outcome=outcome):
                self._check_host_cli(stage=stage, outcome=outcome, error=error)

    def test_summary_requires_complete_strict_counts_and_actual_passing_execution(
        self,
    ) -> None:
        passed = {
            "result": "Passed",
            "totalTestCount": 3,
            "passedTests": 3,
            "failedTests": 0,
            "skippedTests": 0,
            "expectedFailures": 0,
            "testFailures": [],
        }
        invalid = [
            b"",
            b"{broken",
            b"\xff",
            b"null",
            b"[]",
            b'"Passed"',
            json.dumps({**passed, "result": "Failed"}).encode(),
            json.dumps({**passed, "totalTestCount": 0, "passedTests": 0}).encode(),
            json.dumps({**passed, "passedTests": 0, "skippedTests": 3}).encode(),
            json.dumps({**passed, "passedTests": 2, "skippedTests": 1}).encode(),
            json.dumps({**passed, "passedTests": 2, "failedTests": 1}).encode(),
            json.dumps({**passed, "passedTests": 2}).encode(),
            json.dumps({**passed, "expectedFailures": 1}).encode(),
            json.dumps(
                {
                    **passed,
                    "testFailures": [{"failureText": "runner initialization failed"}],
                }
            ).encode(),
            json.dumps({**passed, "testFailures": None}).encode(),
        ]
        for key in passed:
            invalid.append(
                json.dumps({k: v for k, v in passed.items() if k != key}).encode()
            )
        for key in (
            "totalTestCount",
            "passedTests",
            "failedTests",
            "skippedTests",
            "expectedFailures",
        ):
            for value in (True, False, 0.0, "0", None):
                invalid.append(json.dumps({**passed, key: value}).encode())
        for raw in invalid:
            with self.subTest(summary=raw):
                self._check_host_cli(raw_summary=raw, error="summary")

    def test_existing_or_missing_result_artifacts_are_never_adopted(self) -> None:
        for stale in ("bundle", "symlink", "attempt"):
            with self.subTest(stale=stale):
                self._check_host_cli(stale=stale, error="existing")
        self._check_host_cli(missing_bundle=True, error="missing")

    def test_host_target_reuses_sources_and_the_sole_staged_product_without_app(
        self,
    ) -> None:
        spec = backend.SPEC.read_text(encoding="utf-8")
        host, scheme = spec.split("  BlengAppleRecoveryHostTests:\n")[1:]
        host = host.split("\nschemes:")[0]
        self.assertIn("    type: bundle.unit-test\n    platform: macOS\n", host)
        self.assertIn('        TEST_HOST: ""\n        BUNDLE_LOADER: ""\n', host)
        sources = host.split("    sources:\n")[1].split("    settings:\n")[0]
        ios = spec.split("  BlengAppleIOSProbeTests:\n")[1]
        self.assertEqual(
            sources, ios.split("    sources:\n")[1].split("    settings:\n")[0]
        )
        for line in sources.splitlines():
            if "- path: " in line:
                self.assertTrue(
                    (backend.SPEC.parent / line.split("- path: ")[1]).exists(), line
                )
        self.assertEqual(
            host.split("    dependencies:\n")[1].strip(),
            "- package: BleHubUniFfi\n        product: BleHubUniFfi",
        )
        self.assertIn("path: ../../../../.work/staged/apple-bleng\n", spec)
        self.assertNotIn("platforms/apple", host)
        self.assertNotIn("ApplePlatformSession", host)
        self.assertNotIn("AppleRustSupervisorOwner", host)
        self.assertEqual(
            scheme,
            "    build:\n      targets:\n        BlengAppleRecoveryHostTests: all\n"
            "    test:\n      targets:\n        - BlengAppleRecoveryHostTests\n",
        )


if __name__ == "__main__":
    unittest.main()
