"""Fake-only host watchdog orchestration, live-proof gating and ledger tests."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.peripherals.nrf52840 import fixture_session
from tests.peripherals.nrf52840.btp_protocol import BtpFrame
from tests.peripherals.nrf52840.btp_session import BtpSession, FrameTransport
from tests.peripherals.nrf52840.fixture_attestation import (
    FixtureAttestationError,
    InstalledImageAttestation,
)
from tests.peripherals.nrf52840.fixture_attestation_wire import (
    INDEX_NONE,
    READ_IMAGE_ATTESTATION,
    VENDOR,
    encode_attestation,
)
from tests.peripherals.nrf52840.resource_lease import DeviceLease, ResourceLeaseError
from tests.peripherals.nrf52840.support.session_receipts import write_v2_inputs
from tests.support.rf_lab.markers import (
    completed,
    event,
    log_parts,
    marker,
)
from tools.react_native import run_rf_watchdog as runner
from tools.react_native import watchdog_ledger as ledger

IDENTITY = "jlink:123456"
PORT = "/dev/cu.fake-only"
PEER = b"\x01\x02\x03\x04\x05\x06\x07"
MULTIFUNCTION_SERVICE = "fd700000-0000-4000-8000-000000000000"
LEGACY_CAPABILITIES = frozenset(
    {"att-read-hold", "att-read-hold-timeout-30000", "gatt-reset", "diagnostic-btp"}
)


class WatchdogTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.contract, self.profile = write_v2_inputs(self.root)
        self.plan = runner.validate_inputs(self.contract, self.profile)
        self.clock = 100.0

    def runtime(self, *args, **kwargs):
        kwargs.setdefault("service_uuid", ledger.SERVICE)
        value = runner.Runtime(*args, **kwargs)
        value.control = runner.Control(
            "12345678-1234-4234-8234-123456789abc",
            9,
            "rf-watchdog",
            "11111111-1111-4111-8111-111111111111",
        )
        value.expected_local_name = self.plan.local_name
        if value.platform == "ios":
            value.ios_identity = Mock()
        return value

    def sleep(self, seconds: float) -> None:
        self.clock += seconds

    def use_multifunction(self) -> None:
        self.contract, self.profile = write_v2_inputs(self.root)
        self.plan = runner.validate_inputs(self.contract, self.profile)

    def test_multifunction_exact_role_capabilities_and_unguarded_profile_preflight(
        self,
    ) -> None:
        self.use_multifunction()
        contract = json.loads(self.contract.read_bytes())
        profile = json.loads(self.profile.read_bytes())
        changes = [
            {"capabilities": [c for c in contract["capabilities"] if c != missing]}
            for missing in contract["capabilities"]
        ] + [
            {"capabilities": contract["capabilities"] + ["diagnostic-btp"]},
            {"capabilities": sorted(LEGACY_CAPABILITIES)},
            {"fixture_role": "normal-rf"},
            {"fixture_role": "diagnostic-pending-read"},
            {"fixture_role": "diagnostic-watchdog"},
            {"fixture_role": "foreign-role"},
            {"service_uuid": ledger.SERVICE},
        ]
        for change in changes + ["auth", "auth-false", "wrong-value", "missing-char"]:
            with self.subTest(change=change):
                self.contract.write_text(json.dumps(contract))
                document = json.loads(json.dumps(profile))
                rw = next(
                    c
                    for c in document["services"][0]["characteristics"]
                    if c["role"] == "read-write"
                )
                if isinstance(change, dict):
                    self.contract.write_text(json.dumps({**contract, **change}))
                elif change in {"auth", "auth-false"}:
                    rw["read_authorization"] = change == "auth"
                elif change == "wrong-value":
                    rw["initial_value_hex"] = "01"
                else:
                    document["services"][0]["characteristics"].remove(rw)
                self.profile.write_text(json.dumps(document))
                with (
                    patch.object(runner, "open_serial") as opened,
                    patch.object(runner, "port_lease") as leased,
                    patch.object(runner, "Runtime") as runtime,
                    redirect_stdout(io.StringIO()),
                    self.assertRaises((ValueError, RuntimeError)),
                ):
                    runner.run(
                        "android",
                        "explicit-device",
                        self.contract,
                        self.profile,
                        port=PORT,
                        work=self.root / "attempts",
                    )
                opened.assert_not_called()
                leased.assert_not_called()
                runtime.assert_not_called()

    def test_frozen_role_or_capability_drift_is_rejected_before_opening_btp(
        self,
    ) -> None:
        self.use_multifunction()
        prepare = runner.prepare_session
        for change in (
            {"fixture_role": "normal-rf"},
            {"capabilities": list(LEGACY_CAPABILITIES)},
        ):

            def prepare_changed(
                contract: Path,
                profile: Path,
                directory: Path,
                *,
                change: dict[str, object] = change,
                **kwargs: object,
            ) -> fixture_session.FixtureSession:
                plan = prepare(contract, profile, directory, **kwargs)
                frozen = plan.root / "contract.json"
                document = json.loads(frozen.read_bytes())
                frozen.chmod(0o600)
                frozen.write_text(json.dumps({**document, **change}))
                return plan

            with (
                self.subTest(change=change),
                patch.object(
                    fixture_session, "identity_for_port", return_value=IDENTITY
                ),
                patch.object(runner, "prepare_session", side_effect=prepare_changed),
                patch.object(runner, "open_serial") as opened,
                patch.object(runner, "port_lease") as leased,
                patch.object(runner, "Runtime") as runtime,
                redirect_stdout(io.StringIO()),
                self.assertRaises((ValueError, RuntimeError)),
            ):
                runner.run(
                    "android",
                    "explicit-device",
                    self.contract,
                    self.profile,
                    port=PORT,
                    work=self.root / "attempts",
                )
            opened.assert_not_called()
            leased.assert_not_called()
            runtime.assert_not_called()

    def test_multifunction_live_proof_gates_mutation_and_preserves_full_capabilities(
        self,
    ) -> None:
        self.use_multifunction()
        self._run_gated(wrong_image=False)
        self._run_gated(wrong_image=True)
        for capabilities in (
            LEGACY_CAPABILITIES,
            frozenset(runner.MULTIFUNCTION_CAPABILITIES) - {"att-read-fault-v2"},
            frozenset(runner.MULTIFUNCTION_CAPABILITIES) | {"diagnostic-btp"},
        ):
            with self.subTest(capabilities=capabilities):
                self._run_gated(wrong_image=True, installed_capabilities=capabilities)

    def test_preflight_rejects_role_capabilities_profile_without_opening_control(
        self,
    ) -> None:
        original = self.contract.read_text()
        for change in (
            {"fixture_role": "normal-rf", "capabilities": ["normal-rf"]},
            {
                "capabilities": sorted(
                    LEGACY_CAPABILITIES - {"att-read-hold-timeout-30000"}
                )
            },
            {"service_uuid": "fd100000-0000-4000-8000-000000000000"},
        ):
            with self.subTest(change=change):
                self.contract.write_text(json.dumps({**json.loads(original), **change}))
                with (
                    patch.object(runner, "open_serial") as opened,
                    patch.object(runner, "port_lease") as leased,
                    patch.object(runner, "Runtime") as runtime,
                ):
                    with (
                        redirect_stdout(io.StringIO()),
                        self.assertRaises((ValueError, RuntimeError)),
                    ):
                        runner.run(
                            "android",
                            "explicit-device",
                            self.contract,
                            self.profile,
                            port=PORT,
                            work=self.root / "attempts",
                        )
                    opened.assert_not_called()
                    leased.assert_not_called()
                    runtime.assert_not_called()
        reports = list((self.root / "attempts").glob("*/evidence.json"))
        self.assertEqual(len(reports), 3)
        self.assertTrue(
            all(json.loads(p.read_bytes())["status"] == "failed" for p in reports)
        )

    def test_wrong_live_image_has_no_reset_install_launch_or_physical_cleanup(
        self,
    ) -> None:
        self._run_gated(wrong_image=True)
        self._run_gated(wrong_image=True, replace_app=True)

    def test_same_session_live_proof_precedes_every_mutation_and_retains_receipt(
        self,
    ) -> None:
        self._run_gated(wrong_image=False)

    def _run_gated(
        self,
        *,
        wrong_image: bool,
        failure_stage: str | None = None,
        replace_app: bool = False,
        installed_capabilities: frozenset[str] | None = None,
    ) -> None:
        self.use_multifunction()
        calls: list[str] = []
        contract = runner.load_contract(self.contract)
        installed = InstalledImageAttestation(
            image_id=contract.image_id,
            image_version=contract.image_version,
            image_sha256=contract.image_sha256,
            board=contract.board,
            profile_id=contract.profile_id,
            service_uuid=contract.service_uuid,
            local_name=self.plan.local_name,
            capabilities=contract.capabilities,
        )
        if installed_capabilities is not None:
            installed = replace(installed, capabilities=installed_capabilities)
        elif wrong_image:
            installed = replace(installed, image_sha256="cd" * 32)
        frames = Mock(spec=FrameTransport)
        frames.receive.return_value = BtpFrame(
            VENDOR, READ_IMAGE_ATTESTATION, INDEX_NONE, encode_attestation(installed)
        )
        frames.send.side_effect = lambda _: calls.append("attest")
        btp = BtpSession(frames)
        lease = Mock(spec=DeviceLease)
        lease.identity = IDENTITY
        leased = Mock()
        leased.__enter__ = Mock(return_value=lease)
        leased.__exit__ = Mock(return_value=False)
        runtime = Mock(spec=runner.Runtime)
        runtime.install.side_effect = lambda _: (
            calls.append("install") or {"fake": True}
        )
        runtime.stop.side_effect = lambda **_: calls.append("runtime-stop")
        experiment = Mock(spec=runner.Experiment)
        experiment.mutation_allowed = False
        experiment.configure_v2.side_effect = (
            runner.WatchdogError("failed command")
            if failure_stage == "configure"
            else lambda: calls.append("reset") or 42
        )
        experiment.run.side_effect = lambda *_: calls.append("launch")
        experiment.cleanup_v2.side_effect = lambda: (
            calls.append("physical-cleanup") if experiment.mutation_allowed else None
        )
        with (
            patch.object(fixture_session, "identity_for_port", return_value=IDENTITY),
            patch.object(runner, "port_lease", return_value=leased),
            patch.object(runner, "open_serial") as opened,
            patch.object(runner, "BtpSession", return_value=btp),
            patch.object(runner, "Runtime", return_value=runtime) as runtime_factory,
            patch.object(runner, "Experiment", return_value=experiment),
        ):
            output = io.StringIO()
            if failure_stage:
                with (
                    redirect_stdout(output),
                    self.assertRaisesRegex(runner.WatchdogError, "failed command"),
                ):
                    runner.run(
                        "android",
                        "explicit-device",
                        self.contract,
                        self.profile,
                        port=PORT,
                        work=self.root / "attempts",
                    )
                runtime.install.assert_not_called()
                experiment.run.assert_not_called()
                self.assertEqual(calls, ["attest", "physical-cleanup", "runtime-stop"])
                runtime.stop.assert_called_once_with(success=False)
                self.assertNotIn("rf-watchdog:passed", output.getvalue())
            elif wrong_image:
                with (
                    redirect_stdout(output),
                    self.assertRaises((FixtureAttestationError, runner.WatchdogError)),
                ):
                    runner.run(
                        "android",
                        "explicit-device",
                        self.contract,
                        self.profile,
                        port=PORT,
                        replace_app=replace_app,
                        work=self.root / "attempts",
                    )
                runtime.install.assert_not_called()
                experiment.configure_v2.assert_not_called()
                experiment.run.assert_not_called()
                self.assertNotIn("physical-cleanup", calls)
                self.assertNotIn("rf-watchdog:passed", output.getvalue())
            else:
                with redirect_stdout(output):
                    path = runner.run(
                        "android",
                        "explicit-device",
                        self.contract,
                        self.profile,
                        port=PORT,
                        work=self.root / "attempts",
                    )
                evidence = json.loads(path.read_bytes())
                self.assertEqual(evidence["status"], "passed")
                self.assertEqual(
                    runtime_factory.call_args.kwargs["service_uuid"],
                    self.plan.service_uuid,
                )
                self.assertEqual(
                    frozenset(
                        evidence["fixture_receipt"]["installed_identity"][
                            "capabilities"
                        ]
                    ),
                    contract.capabilities,
                )
                self.assertEqual(evidence["peripheral_hold_timeout_ms"], 20000)
                self.assertEqual(evidence["backend_watchdog_expected_ms"], 10000)
                self.assertNotIn("native_timeout_ms", evidence)
                self.assertEqual(
                    evidence["fixture_receipt"]["proof_method"],
                    fixture_session.PROOF_METHOD,
                )
                self.assertEqual(
                    calls,
                    [
                        "attest",
                        "reset",
                        "install",
                        "launch",
                        "physical-cleanup",
                        "runtime-stop",
                    ],
                )
            opened.assert_called_once_with(PORT)
            frames.close.assert_called_once()
            leased.__exit__.assert_called_once()

    def test_lease_loss_blocks_serial_operations_and_host_sleep(self) -> None:
        lease = Mock(spec=DeviceLease)
        lease.identity = IDENTITY
        lease.require.side_effect = ResourceLeaseError("lease lost")
        raw = Mock()
        transport = runner.LeasedTransport(raw, lease)
        for action in (lambda: transport.read(1), lambda: transport.write(b"x")):
            with self.assertRaises(ResourceLeaseError):
                action()
        raw.read.assert_not_called()
        raw.write.assert_not_called()
        experiment = runner.Experiment(Mock(spec=BtpSession), self.plan, Mock(), {})
        experiment.guard.side_effect = ResourceLeaseError("lease lost")
        with patch.object(runner.time, "sleep") as sleep:
            with self.assertRaises(ResourceLeaseError):
                experiment.pause()
            sleep.assert_not_called()
        self.assertFalse(hasattr(experiment, "hold"))

    def test_full_projection_supports_native_terminal_reordering_but_rejects_duplicates_and_late_domains(
        self,
    ) -> None:
        for android in (False, True):
            setup, gate, final = log_parts(android)
            text = setup + gate + final
            self.assertEqual(ledger.release_gate(setup + gate), "30")
            self.assertEqual(
                ledger.verify_ledger(text, characteristic="30")[
                    "recovery_characteristic"
                ],
                "31",
            )
            bad = [
                text + marker(ledger.PASSED),
                text + completed("21024"),
                text
                + event(
                    "characteristicRead",
                    "21010",
                    connectionId="21007",
                    gattSessionId="21009",
                    characteristicId="30",
                    value=[0],
                ),
                text.replace('"operation_aborted"', '"cancelled"'),
                text.replace(
                    "Apple GATT operation watchdog expired:", "JS outer timeout:"
                )
                if not android
                else text.replace("AndroidBleBackend", "fake"),
                text.replace('"value": [0]', '"value": [1]'),
                text + marker("rf-watchdog:unknown"),
            ]
            for index, altered in enumerate(bad):
                with (
                    self.subTest(android=android, case=index),
                    self.assertRaises(ledger.LedgerError),
                ):
                    ledger.verify_ledger(altered, characteristic="30")

    def test_ledger_matches_validated_service_in_both_scan_windows(self) -> None:
        for service in (ledger.SERVICE, MULTIFUNCTION_SERVICE):
            for android in (False, True):
                setup, gate, final = (
                    part.replace(ledger.SERVICE, service) for part in log_parts(android)
                )
                text = setup + gate + final
                with self.subTest(service=service, android=android):
                    ledger.verify_ledger(
                        text, characteristic="30", service_uuid=service
                    )
                    for scan in ("21004", "21014"):
                        advertisement = event(
                            "advertisementObserved",
                            scanSessionId=scan,
                            peripheralId="876",
                            advertisedServices=[service],
                        )
                        wrong = advertisement.replace(
                            service, "fd000000-0000-4000-8000-000000000000"
                        )
                        with self.assertRaises(ledger.LedgerError):
                            ledger.verify_ledger(
                                text.replace(advertisement, wrong),
                                characteristic="30",
                                service_uuid=service,
                            )
                    with self.assertRaises(ledger.LedgerError):
                        ledger.verify_ledger(
                            text,
                            characteristic="30",
                            service_uuid=(
                                MULTIFUNCTION_SERVICE
                                if service == ledger.SERVICE
                                else ledger.SERVICE
                            ),
                        )

    def test_explicit_service_is_forwarded_by_both_runtime_launch_protocols(
        self,
    ) -> None:
        android = self.runtime(
            "android",
            "explicit-android",
            self.root,
            Mock(),
            service_uuid=MULTIFUNCTION_SERVICE,
        )
        android.adb = Mock(
            side_effect=[
                SimpleNamespace(stdout="Starting: Intent"),
                SimpleNamespace(stdout="123\n"),
                SimpleNamespace(stdout="123\n"),
            ]
        )
        with patch.object(runner.subprocess, "Popen") as capture:
            capture.return_value.poll.return_value = None
            android.launch()
            android.stop(success=True)
        self.assertIn(MULTIFUNCTION_SERVICE, android.adb.call_args_list[0].args)
        ios = self.runtime(
            "ios",
            "explicit-ios",
            self.root,
            Mock(),
            service_uuid=MULTIFUNCTION_SERVICE,
        )
        with patch.object(runner.subprocess, "Popen") as launch:
            launch.return_value.poll.return_value = 0
            ios.launch()
            self.assertEqual(
                launch.call_args.args[0][-7:],
                [
                    "--rf-watchdog-service-uuid",
                    MULTIFUNCTION_SERVICE,
                    "--rf-control-json",
                    json.dumps(runner.asdict(ios.control)),
                    "--rf-expected-local-name",
                    self.plan.local_name,
                    "--keep-app",
                ],
            )
            ios.stop(success=True)

    def test_finished_ui_projection_includes_recovery_scan_and_exact_native_errors(
        self,
    ) -> None:
        for android in (False, True):
            with self.subTest(android=android):
                setup, gate, final = log_parts(android)
                # Android threadtime prefixes and iOS console prefixes must not
                # change the JSON arrays or erase duplicate structured markers.
                prefix = (
                    "10-13 12:00:00.001 123 456 I ReactNativeJS: "
                    if android
                    else "[iPhone console] "
                )
                text = "\n".join(
                    prefix + line for line in (setup + gate + final).splitlines()
                )
                result = ledger.verify_ledger(text, characteristic="30")
                terminals = result["native_terminals"]
                self.assertEqual(len(terminals), 13)
                self.assertIn("21012", terminals)
                self.assertIn("21013", terminals)
                errors = {
                    command: ledger.category(e)
                    for command, e in terminals.items()
                    if ledger.category(e)
                }
                self.assertEqual(
                    errors, {"21010": "timeout", "21011": "operation_aborted"}
                )
                self.assertEqual(result["recovery_peripheral"], "876")
                self.assertEqual(result["recovery_scan_session"], "21014")
                self.assertEqual(
                    [
                        e["value"]
                        for _, e in ledger.events(ledger.markers(text))
                        if e["kind"] == "characteristicRead"
                    ],
                    [[0]],
                )

    def test_recovery_scan_remains_strict_about_windows_identity_unknowns_and_duplicates(
        self,
    ) -> None:
        setup, gate, final = log_parts()
        text = setup + gate + final
        advertisement = event(
            "advertisementObserved",
            scanSessionId="21014",
            peripheralId="876",
            advertisedServices=[ledger.SERVICE],
        )
        start = event("scanStarted", "21012", scanSessionId="21014")
        stop = event("scanStopped", "21013", scanSessionId="21014")
        mutations = {
            "missing recovery advertisement": text.replace(advertisement + "\n", ""),
            "wrong recovery peripheral": text.replace(
                advertisement, advertisement.replace('"876"', '"877"')
            ),
            "wrong service": text.replace(
                advertisement,
                advertisement.replace(
                    ledger.SERVICE, "fd000000-0000-4000-8000-000000000000"
                ),
            ),
            "wrong scan session": text.replace(
                advertisement, advertisement.replace('"21014"', '"21004"')
            ),
            "advertisement before scan": text.replace(
                start, advertisement + "\n" + start
            ),
            "advertisement after stop": text.replace(stop, stop + "\n" + advertisement),
            "duplicate start domain": text.replace(start, start + "\n" + start),
            "duplicate stop domain": text.replace(stop, stop + "\n" + stop),
            "duplicate terminal": text.replace(
                completed("21012"), completed("21012") + "\n" + completed("21012")
            ),
            "unknown command": text + completed("21015"),
            "unknown domain": text
            + event("unexpectedKind", "21012", scanSessionId="21014"),
            "unknown marker": text + marker("rf-watchdog:recovery-scan:passed"),
            "failed scan": text.replace(
                completed("21012"),
                completed("21012", {"category": "gatt", "message": "failed scan"}),
            ),
            "missing start": text.replace(start + "\n", ""),
            "missing stop terminal": text.replace(completed("21013") + "\n", ""),
            "recovery before silence": text.replace(
                marker(ledger.SILENCE), start + "\n" + marker(ledger.SILENCE)
            ).replace(start + "\n" + completed("21012"), completed("21012")),
            "opaque value": text.replace('"value": [0]', '"value": {}'),
            "boolean is not byte": text.replace('"value": [0]', '"value": [false]'),
        }
        for label, altered in mutations.items():
            with self.subTest(case=label), self.assertRaises(ledger.LedgerError):
                ledger.verify_ledger(altered, characteristic="30")

    def test_android_app_replacement_is_explicit_scoped_and_verified(self) -> None:
        runtime = self.runtime("android", "explicit-android", self.root, Mock())
        runtime.adb = Mock()
        with (
            patch.object(runner.android_sdk, "runtime_alive", return_value=True),
            patch.object(runner.android_sdk, "grant_bluetooth_permissions") as grant,
        ):
            with self.assertRaisesRegex(runner.WatchdogError, "--replace-app"):
                runtime.install(None)
            runtime.adb.assert_not_called()
            grant.assert_not_called()
        runtime.replace_app = True
        with (
            patch.object(
                runner.android_sdk, "runtime_alive", side_effect=[True, False]
            ),
            patch.object(runner.android_sdk, "grant_bluetooth_permissions"),
        ):
            artifact = runtime.install(None)
        self.assertTrue(artifact["replaced_running_app"])
        self.assertEqual(
            runtime.adb.call_args_list[0].args,
            ("shell", "am", "force-stop", "com.blehubnitrohost"),
        )
        self.assertEqual(
            runtime.adb.call_args_list[1].args, ("logcat", "-b", "main", "-c")
        )
        runtime.adb.reset_mock()
        with (
            patch.object(runner.android_sdk, "runtime_alive", return_value=True),
            patch.object(runner.android_sdk, "grant_bluetooth_permissions") as grant,
        ):
            with self.assertRaisesRegex(runner.WatchdogError, "did not stop"):
                runtime.install(None)
            runtime.adb.assert_called_once_with(
                "shell", "am", "force-stop", "com.blehubnitrohost"
            )
            grant.assert_not_called()

    def test_both_runtime_launch_protocols_and_android_snapshot_preserve_json_arrays(
        self,
    ) -> None:
        guard = Mock()
        android = self.runtime("android", "explicit-android", self.root, guard)
        android.adb = Mock(
            side_effect=[
                SimpleNamespace(stdout="Starting: Intent", returncode=0),
                SimpleNamespace(stdout="123\n", returncode=0),
                SimpleNamespace(stdout="123\n", returncode=0),
            ]
        )
        with patch.object(runner.subprocess, "Popen") as capture:
            capture.return_value.poll.return_value = None
            android.launch()
            self.assertEqual(
                capture.call_args.args[0],
                runner.android_sdk.adb_command(
                    "explicit-android",
                    "logcat",
                    "-b",
                    "main",
                    "--pid",
                    "123",
                    "-v",
                    "threadtime",
                    "*:V",
                ),
            )
            self.assertIs(capture.call_args.kwargs["stdout"], android.android_output)
            self.assertIs(capture.call_args.kwargs["stderr"], android.android_errors)
            self.assertEqual(
                capture.call_args.kwargs["stdin"], runner.subprocess.DEVNULL
            )
            self.addCleanup(android.stop, success=True)
        self.assertEqual(
            android.adb.call_args_list[0].args,
            (
                "shell",
                "am",
                "start",
                "-W",
                "-n",
                "com.blehubnitrohost/.MainActivity",
                "--es",
                "blehub.runtimeMode",
                "rf-watchdog",
                "--es",
                "blehub.rfServiceUuid",
                ledger.SERVICE,
                "--es",
                "blehub.rfCancellationControl",
                json.dumps(runner.asdict(android.control)),
                "--es",
                "blehub.rfExpectedLocalName",
                self.plan.local_name,
            ),
        )
        self.assertEqual(android.adb.call_args_list[0].kwargs, {"timeout": 20})
        self.assertEqual(
            android.adb.call_args_list[1].args,
            ("shell", "pidof", "com.blehubnitrohost"),
        )
        self.assertEqual(android.android_pid, "123")
        self.assertEqual(
            android.android_raw_log, self.root / "android-logcat-snapshots.log"
        )
        identity = json.loads((self.root / "android-runtime-identity.json").read_text())
        self.assertEqual(
            identity,
            {
                "device": "explicit-android",
                "package": "com.blehubnitrohost",
                "pid": "123",
            },
        )
        setup, gate, final = log_parts(True)
        text = (
            "\n".join(
                "10-13 12:00:00.001 123 456 I ReactNativeJS: " + line
                for line in (setup + gate + final).splitlines()
            )
            + "\n"
        )
        android.android_raw_log.write_text(text, encoding="utf-8")
        android.adb.side_effect = [
            SimpleNamespace(stdout="123\n"),
            SimpleNamespace(stdout="123\n"),
        ]
        self.assertEqual(android.snapshot(), text)
        self.assertEqual(
            [call.args for call in android.adb.call_args_list[-2:]],
            [("shell", "pidof", "com.blehubnitrohost")] * 2,
        )
        self.assertEqual(
            ledger.verify_ledger(android.log.read_text(), characteristic="30")[
                "recovery_scan_session"
            ],
            "21014",
        )
        ios = self.runtime("ios", "explicit-ios", self.root, guard)
        with patch.object(runner.subprocess, "Popen") as launch:
            launch.return_value.poll.return_value = 0
            ios.launch()
            command = launch.call_args.args[0]
            self.assertEqual(
                command[:5],
                [
                    runner.sys.executable,
                    "-m",
                    "tools.react_native.run_ios_device_runtime",
                    "explicit-ios",
                    str(ios.log),
                ],
            )
            self.assertEqual(
                command[-7:],
                [
                    "--rf-watchdog-service-uuid",
                    ledger.SERVICE,
                    "--rf-control-json",
                    json.dumps(runner.asdict(ios.control)),
                    "--rf-expected-local-name",
                    self.plan.local_name,
                    "--keep-app",
                ],
            )
            self.assertTrue(launch.call_args.kwargs["start_new_session"])
            ios.stop(success=True)
        self.assertGreaterEqual(guard.call_count, 6)

    def android_capture(
        self, raw_snapshots: list[str], pids: list[str] | None = None
    ) -> runner.Runtime:
        attempt = Path(tempfile.mkdtemp(dir=self.root))
        runtime = self.runtime("android", "explicit-android", attempt, Mock())
        runtime.android_pid = "123"  # synthetic launch identity, never a real process
        runtime.android_capture = Mock()
        runtime.android_capture.poll.return_value = None
        snapshots = iter(raw_snapshots)
        identities = iter(pids) if pids is not None else None
        pid_checks = 0

        def adb(*arguments: str, **_: object) -> SimpleNamespace:
            nonlocal pid_checks
            self.assertEqual(arguments, ("shell", "pidof", "com.blehubnitrohost"))
            if pid_checks % 2 == 0:
                # Simulate the continuous writer's current local file, not an
                # adb dump and never a union of earlier snapshots.
                raw = next(snapshots, None)
                if raw is not None:
                    runtime.android_raw_log.write_text(raw, encoding="utf-8")
            pid_checks += 1
            return SimpleNamespace(
                stdout=next(identities) if identities is not None else "123\n"
            )

        runtime.adb = Mock(side_effect=adb)
        return runtime

    def test_android_capture_keeps_appended_headers_noise_and_partial_records(
        self,
    ) -> None:
        first = (
            "10-13 12:00:00.001 123 456 I ReactNativeJS: "
            + marker(ledger.TIMEOUT)
            + "\n"
        )
        second = (
            "10-13 12:00:00.002 123 456 I ReactNativeJS: "
            + marker(ledger.ABORTED)
            + "\n"
        )
        partial = "10-13 12:00:00.003 123 456 I ReactNativeJS: " + marker(
            ledger.AWAITING
        )
        initial = (
            "--------- beginning of main\nold unrelated log\n" + first + "other log\n"
        )
        progress = (
            initial
            + "--------- beginning of system\nnew unrelated log\n"
            + second
            + partial
        )
        raws = [initial, progress, progress + "\nmore noise\n"]
        runtime = self.android_capture(raws)
        self.assertEqual(runtime.snapshot(), first)
        self.assertEqual(runtime.snapshot(), first + second)
        self.assertEqual(runtime.snapshot(), first + second + partial + "\n")
        self.assertEqual(runtime.android_records, [first, second, partial + "\n"])
        raw_artifact = runtime.android_raw_log.read_text()
        self.assertEqual(raw_artifact, raws[-1])
        self.assertEqual(raw_artifact.count(first), 1)
        self.assertNotIn("--- snapshot", raw_artifact)
        self.assertNotIn("unrelated", runtime.log.read_text())
        self.assertNotIn("beginning of", runtime.log.read_text())

    def test_android_stream_capture_survives_noise_without_polling_or_logcat_dumps(
        self,
    ) -> None:
        first = (
            "10-13 12:00:00.001 123 456 I ReactNativeJS: "
            + marker("rf-watchdog:listener:registered-before-initialize")
            + "\n"
        )
        last = "10-13 12:00:00.002 123 456 I ReactNativeJS: " + marker(ledger.TIMEOUT)
        noise = "10-13 12:00:00.001 123 456 D Native: unrelated log\n" * 12000
        runtime = self.runtime("android", "synthetic-device", self.root, Mock())
        runtime.android_pid = "123"
        runtime.adb = Mock(return_value=SimpleNamespace(stdout="123\n"))
        ready, advance = self.root / "capture-ready", self.root / "capture-advance"
        child = (
            "import os, pathlib, sys, time\n"
            "sys.stdout.buffer.write((sys.argv[1] + sys.argv[2] * 12000 + sys.argv[3]).encode())\n"
            "sys.stdout.buffer.flush()\n"
            "os.write(2, b'capture diagnostic\\n')\n"
            "pathlib.Path(sys.argv[4]).touch()\n"
            "while not pathlib.Path(sys.argv[5]).exists(): time.sleep(0.01)\n"
            "os.write(1, b'\\n')\n"
            "time.sleep(30)\n"
        )
        # Exercise actual OS file redirection and reaping, but never ADB/hardware.
        with patch.object(
            runner.android_sdk,
            "adb_command",
            return_value=[
                runner.sys.executable,
                "-u",
                "-c",
                child,
                first,
                "10-13 12:00:00.001 123 456 D Native: unrelated log\n",
                last,
                str(ready),
                str(advance),
            ],
        ) as command:
            self.addCleanup(runtime.stop, success=True)
            runtime.start_android_capture()
            deadline = runner.time.monotonic() + 5
            while not ready.exists() and runner.time.monotonic() < deadline:
                runner.time.sleep(0.01)
            self.assertTrue(
                ready.exists(), "synthetic logcat did not write its backlog"
            )
            # Even without host snapshots throughout this large burst, startup
            # markers and raw noise remain in one complete local artifact.
            self.assertEqual(runtime.snapshot(), first)
            self.assertEqual(runtime.android_raw_log.read_text(), first + noise + last)
            advance.touch()
            deadline = runner.time.monotonic() + 5
            while (
                not runtime.android_raw_log.read_bytes().endswith(b"\n")
                and runner.time.monotonic() < deadline
            ):
                runner.time.sleep(0.01)
            self.assertEqual(runtime.snapshot(), first + last + "\n")
            command.assert_called_once_with(
                "synthetic-device",
                "logcat",
                "-b",
                "main",
                "--pid",
                "123",
                "-v",
                "threadtime",
                "*:V",
            )
            runtime.stop(success=True)
            self.assertIsNotNone(runtime.android_capture.poll())
            self.assertTrue(runtime.android_output.closed)
            self.assertTrue(runtime.android_errors.closed)
            self.assertEqual(
                runtime.android_raw_log.read_text(), first + noise + last + "\n"
            )
            self.assertEqual(
                (self.root / "android-logcat-stderr.log").read_text(),
                "capture diagnostic\n",
            )

    def test_android_capture_rejects_marker_loss_change_reorder_and_insertion(
        self,
    ) -> None:
        first = (
            "10-13 12:00:00.001 123 456 I ReactNativeJS: "
            + marker(ledger.TIMEOUT)
            + "\n"
        )
        second = (
            "10-13 12:00:00.002 123 456 I ReactNativeJS: "
            + marker(ledger.ABORTED)
            + "\n"
        )
        baseline = first + second
        mutations = {
            "first dropped": second,
            "last dropped": first,
            "all markers dropped": "--------- beginning of main\nother logs\n",
            "body replaced": baseline.replace("error=timeout", "error=cancelled"),
            "timestamp replaced": baseline.replace("12:00:00.001", "12:00:01.001"),
            "order changed": second + first,
            "duplicate inserted in prefix": first + first + second,
            "known record became partial": first + second.rstrip("\n"),
            "unrelated prefix replaced": "changed noise\n" + baseline,
        }
        for label, changed in mutations.items():
            with self.subTest(case=label):
                runtime = self.android_capture([baseline, changed])
                self.assertEqual(runtime.snapshot(), baseline)
                with self.assertRaisesRegex(
                    runner.WatchdogError, "records dropped or changed"
                ):
                    runtime.snapshot()
                self.assertEqual(runtime.log.read_text(), baseline)
                self.assertIn(changed, runtime.android_raw_log.read_text())

    def test_android_capture_preserves_identical_duplicates_for_the_final_checker(
        self,
    ) -> None:
        setup, gate, final = log_parts(True)
        records = [
            "10-13 12:00:00.001 123 456 I ReactNativeJS: " + line + "\n"
            for line in (setup + gate + final).splitlines()
        ]
        baseline = "".join(records)
        duplicate = next(
            record
            for record in records
            if '"commandId": "21024"' in record
            and '"kind": "commandCompleted"' in record
        )
        changed = baseline + duplicate  # identical body, timestamp, PID and thread
        runtime = self.android_capture([baseline, changed, baseline])
        self.assertEqual(runtime.snapshot(), baseline)
        ledger.verify_ledger(runtime.log.read_text(), characteristic="30")
        self.assertEqual(runtime.snapshot(), changed)
        self.assertEqual(runtime.android_records.count(duplicate), 2)
        with self.assertRaises(ledger.LedgerError):
            ledger.verify_ledger(runtime.log.read_text(), characteristic="30")
        with self.assertRaisesRegex(runner.WatchdogError, "records dropped or changed"):
            runtime.snapshot()  # losing the duplicate is also a continuity failure
        unknown = (
            baseline
            + "10-13 12:00:00.002 123 456 I ReactNativeJS: "
            + marker("rf-watchdog:unknown")
            + "\n"
        )
        runtime = self.android_capture([unknown])
        with self.assertRaises(ledger.LedgerError):
            ledger.verify_ledger(runtime.snapshot(), characteristic="30")

    def test_android_capture_waits_for_partial_utf8_and_rejects_partial_byte_changes(
        self,
    ) -> None:
        record = (
            "10-13 12:00:00.001 123 456 I ReactNativeJS: "
            + marker("rf-watchdog:unknown:值")
            + "\n"
        ).encode("utf-8")
        partial = record[:-3]  # stop inside the three-byte UTF-8 character
        runtime = self.android_capture([])
        runtime.adb = Mock(return_value=SimpleNamespace(stdout="123\n"))
        runtime.android_raw_log.write_bytes(partial)
        self.assertEqual(runtime.snapshot(), "")
        self.assertEqual(runtime.android_raw_log.read_bytes(), partial)
        with runtime.android_raw_log.open("ab") as stream:
            stream.write(record[len(partial) :])
        self.assertEqual(runtime.snapshot(), record.decode("utf-8"))
        runtime = self.android_capture([])
        runtime.adb = Mock(return_value=SimpleNamespace(stdout="123\n"))
        runtime.android_raw_log.write_bytes(partial)
        self.assertEqual(runtime.snapshot(), "")
        changed = partial[:-1] + b"x"
        runtime.android_raw_log.write_bytes(changed)
        with self.assertRaisesRegex(
            runner.WatchdogError, "raw log records dropped or changed"
        ):
            runtime.snapshot()
        self.assertEqual(runtime.android_raw_log.read_bytes(), changed)
        self.assertEqual(runtime.log.read_text(), "")

    def test_android_capture_requires_correct_headers_and_retains_failed_raw_log(
        self,
    ) -> None:
        body = marker(ledger.TIMEOUT) + "\n"
        for raw in (
            body,
            "I/ReactNativeJS(123): " + body,
            "invalid-timestamp 123 456 I ReactNativeJS: " + body,
        ):
            with self.subTest(raw=raw):
                runtime = self.android_capture([raw])
                with self.assertRaisesRegex(
                    runner.WatchdogError, "threadtime record header"
                ):
                    runtime.snapshot()
                self.assertEqual(runtime.android_raw_log.read_text(), raw)
                self.assertFalse(runtime.log.exists())
        raw = "10-13 12:00:00.001 123 456 I ReactNativeJS: BLEHUB_NITRO_RUNTIME failure:synthetic\n"
        runtime = self.android_capture([raw])
        with self.assertRaisesRegex(runner.WatchdogError, "runtime reported failure"):
            runtime.snapshot()
        runtime.stop(success=True)
        self.assertEqual(runtime.android_raw_log.read_text(), raw)

    def test_android_capture_fails_closed_on_exit_even_with_a_passed_ledger(
        self,
    ) -> None:
        raw = (
            "10-13 12:00:00.001 123 456 I ReactNativeJS: "
            + marker(ledger.PASSED)
            + "\n"
        )
        for code in (0, 1):
            for allow_exit in (False, True):
                with self.subTest(code=code, allow_exit=allow_exit):
                    runtime = self.android_capture([raw])
                    runtime.snapshot()
                    runtime.android_capture.poll.return_value = code
                    with self.assertRaisesRegex(
                        runner.WatchdogError, "logcat capture exited early"
                    ):
                        runtime.snapshot(allow_exit=allow_exit)
                    runtime.stop(success=True)
                    runtime.android_capture.wait.assert_called_once_with(timeout=5)
                    self.assertEqual(runtime.android_raw_log.read_text(), raw)
        runtime = self.android_capture([raw])
        runtime.android_capture.poll.side_effect = [None, 0]
        with self.assertRaisesRegex(
            runner.WatchdogError, "logcat capture exited early"
        ):
            runtime.snapshot()
        self.assertFalse(runtime.log.exists())
        self.assertEqual(runtime.android_raw_log.read_text(), raw)
        runtime.android_capture = None
        with self.assertRaisesRegex(runner.WatchdogError, "capture was not started"):
            runtime.snapshot()

    def test_android_pid_ownership_is_checked_before_and_after_capture(self) -> None:
        record = (
            "10-13 12:00:00.001 123 456 I ReactNativeJS: "
            + marker(ledger.PASSED)
            + "\n"
        )
        for pids in (["124"], ["123", "124"], [""], ["123 124"]):
            with self.subTest(pids=pids):
                runtime = self.android_capture([record], pids)
                with self.assertRaises(runner.WatchdogError):
                    runtime.snapshot(allow_exit=True)
                self.assertFalse(runtime.log.exists())
        runtime = self.android_capture([record.replace(" 123 456 ", " 124 456 ")])
        with self.assertRaisesRegex(runner.WatchdogError, "does not belong"):
            runtime.snapshot()
        self.assertIn(" 124 456 ", runtime.android_raw_log.read_text())
        runtime = self.android_capture([record])
        runtime.android_pid = None
        with self.assertRaisesRegex(runner.WatchdogError, "not pinned"):
            runtime.snapshot()
        runtime.adb.assert_not_called()
        for pid in ("", "123 124", "not-a-pid"):
            runtime = self.runtime("android", "explicit-android", self.root, Mock())
            runtime.adb = Mock(
                side_effect=[
                    SimpleNamespace(stdout="Starting: Intent"),
                    SimpleNamespace(stdout=pid),
                ]
            )
            with self.assertRaisesRegex(runner.WatchdogError, "exactly one live PID"):
                runtime.launch()
            self.assertIsNone(runtime.android_pid)

    def test_android_failure_cleanup_never_stops_a_replacement_pid(self) -> None:
        for pid, code, should_stop in (
            ("123\n", 0, True),
            ("124\n", 0, False),
            ("", 1, False),
            ("123 124", 0, False),
        ):
            with self.subTest(pid=pid):
                runtime = self.runtime("android", "explicit-android", self.root, Mock())
                runtime.launched = True
                runtime.android_pid = "123"
                runtime.android_capture = Mock()
                runtime.android_capture.poll.return_value = None
                runtime.android_output = runtime.android_raw_log.open("wb")
                runtime.android_output.write(b"raw failure evidence\n")
                runtime.android_output.flush()
                runtime.android_errors = (self.root / "android-logcat-stderr.log").open(
                    "wb"
                )
                runtime.guard.side_effect = runner.WatchdogError("fixture lease lost")
                with patch.object(runner.android_sdk, "run_command") as command:
                    command.return_value = SimpleNamespace(stdout=pid, returncode=code)
                    if pid.strip() not in ("123", ""):
                        with self.assertRaisesRegex(
                            runner.WatchdogError, "refusing app termination"
                        ):
                            runtime.stop(success=False)
                    else:
                        runtime.stop(success=False)
                    self.assertEqual(command.call_count, 2 if should_stop else 1)
                    if should_stop:
                        self.assertIn("force-stop", command.call_args.args[0])
                    runtime.android_capture.terminate.assert_called_once()
                    runtime.android_capture.wait.assert_called_once_with(timeout=5)
                    self.assertTrue(runtime.android_output.closed)
                    self.assertTrue(runtime.android_errors.closed)
                    self.assertEqual(
                        runtime.android_raw_log.read_bytes(), b"raw failure evidence\n"
                    )
        runtime.android_pid = None
        with (
            patch.object(runner.android_sdk, "run_command") as command,
            self.assertRaisesRegex(runner.WatchdogError, "no pinned Android PID"),
        ):
            runtime.stop(success=False)
        command.assert_not_called()

    def test_android_capture_cleanup_kills_reaps_and_closes_after_app_stop_failure(
        self,
    ) -> None:
        runtime = self.runtime("android", "synthetic-device", self.root, Mock())
        runtime.launched = True
        runtime.android_pid = "123"
        runtime.android_capture = Mock()
        runtime.android_capture.poll.return_value = None
        runtime.android_capture.wait.side_effect = [
            runner.subprocess.TimeoutExpired("synthetic-logcat", 5),
            0,
        ]
        runtime.android_output = runtime.android_raw_log.open("wb")
        runtime.android_output.write(b"raw failure evidence\n")
        runtime.android_errors = (self.root / "android-logcat-stderr.log").open("wb")
        with (
            patch.object(runner.android_sdk, "run_command") as command,
            self.assertRaisesRegex(runner.WatchdogError, "app stop failed"),
        ):
            command.side_effect = [
                SimpleNamespace(stdout="123\n", returncode=0),
                runner.WatchdogError("app stop failed"),
            ]
            runtime.stop(success=False)
        runtime.android_capture.terminate.assert_called_once()
        runtime.android_capture.kill.assert_called_once()
        self.assertEqual(runtime.android_capture.wait.call_count, 2)
        self.assertTrue(runtime.android_output.closed)
        self.assertTrue(runtime.android_errors.closed)
        self.assertEqual(
            runtime.android_raw_log.read_bytes(), b"raw failure evidence\n"
        )

    def test_android_capture_start_failure_still_closes_partial_capture_artifacts(
        self,
    ) -> None:
        runtime = self.runtime("android", "synthetic-device", self.root, Mock())
        runtime.adb = Mock(
            side_effect=[
                SimpleNamespace(stdout="Starting: Intent"),
                SimpleNamespace(stdout="123\n"),
            ]
        )
        with (
            patch.object(
                runner.subprocess, "Popen", side_effect=OSError("capture start failed")
            ),
            self.assertRaisesRegex(OSError, "capture start failed"),
        ):
            runtime.launch()
        self.assertTrue(runtime.launched)
        with patch.object(runner.android_sdk, "run_command") as command:
            command.return_value = SimpleNamespace(stdout="123\n", returncode=0)
            runtime.stop(success=False)
            self.assertIn("force-stop", command.call_args.args[0])
        self.assertTrue(runtime.android_output.closed)
        self.assertTrue(runtime.android_errors.closed)
        self.assertTrue(runtime.android_raw_log.exists())
        self.assertTrue((self.root / "android-logcat-stderr.log").exists())

    def test_ios_poll_waits_for_complete_json_records_without_truncating_artifact(
        self,
    ) -> None:
        ios = self.runtime("ios", "explicit-ios", self.root, Mock())
        ios.ios_identity = None  # Parser-only snapshots are not one cumulative run.
        ios.process = Mock()
        ios.process.poll.return_value = None
        setup, gate, final = log_parts()
        record = event(
            "characteristicRead",
            "21024",
            connectionId="21021",
            gattSessionId="21023",
            characteristicId="31",
            value=[0],
        )
        split = record.index('"value"')
        ios.log.write_text(setup + record[:split])
        self.assertEqual(ios.snapshot(), setup)
        self.assertEqual(ios.log.read_text(), setup + record[:split])
        ios.log.write_text(setup + record + "\n")
        self.assertEqual(ios.snapshot(), setup + record + "\n")
        ios.log.write_text(setup + gate + final)
        self.assertEqual(
            ledger.verify_ledger(ios.snapshot(), characteristic="30")[
                "recovery_peripheral"
            ],
            "876",
        )

    def test_runtime_only_gates_on_standalone_final_failure_and_pass_markers(
        self,
    ) -> None:
        ios = self.runtime("ios", "explicit-ios", self.root, Mock())
        ios.process = Mock()
        ios.process.poll.return_value = None
        embedded = (
            event(
                "commandCompleted",
                "21010",
                error={
                    "category": "timeout",
                    "message": "rf-watchdog:failed: embedded diagnostic text rf-watchdog:passed",
                },
            )
            + "\n"
        )
        ios.log.write_text(embedded)
        self.assertEqual(ios.snapshot(), embedded)
        ios.process.poll.return_value = 0
        with self.assertRaisesRegex(runner.WatchdogError, "exited early"):
            ios.snapshot(allow_exit=True)
        ios.process.poll.return_value = None
        for failure in (
            "rf-watchdog:failed:missing native evidence",
            "rf-watchdog:close:failed:dispose failed",
        ):
            ios.log.write_text(marker(failure) + "\n")
            with self.assertRaisesRegex(runner.WatchdogError, "reported failure"):
                ios.snapshot()

    def test_failed_configuration_cannot_install_or_launch_and_runs_cleanup(
        self,
    ) -> None:
        self._run_gated(wrong_image=False, failure_stage="configure")

    def test_ios_owner_waits_for_keep_app_helper_and_interrupts_failure_unwind(
        self,
    ) -> None:
        runtime = self.runtime("ios", "explicit-device", self.root, Mock())
        runtime.process = Mock()
        runtime.process.pid = 12345
        runtime.process.poll.side_effect = [None, 0]
        with patch.object(runner.os, "killpg") as kill:
            runtime.stop(success=True)
            runtime.process.wait.assert_called_once_with(timeout=15)
            kill.assert_not_called()
        runtime.process = Mock()
        runtime.process.pid = 12345
        runtime.process.poll.return_value = None
        runtime.launched = True
        runtime.process.wait.side_effect = lambda **_: (
            runtime.ios_cleanup_receipt.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "device": runtime.device,
                        "bundle_id": runner.BUNDLE_ID,
                        "helper_pid": 12345,
                        "launch_json": str(runtime.ios_launch_json),
                        "action": "signal-forwarded",
                    }
                ),
                encoding="utf-8",
            )
        )
        with (
            patch.object(runner.os, "killpg") as kill,
            patch.object(runner.ios_sdk, "run_command") as terminate,
        ):
            runtime.stop(success=False)
            kill.assert_called_once_with(12345, runner.signal.SIGINT)
            terminate.assert_not_called()  # helper already signalled the attached app

    def ios_cleanup_identity(
        self,
    ) -> tuple[runner.Runtime, dict[str, object], dict[str, object]]:
        attempt = Path(tempfile.mkdtemp(dir=self.root))
        runtime = self.runtime("ios", "synthetic-ios", attempt, Mock())
        runtime.launched = True
        runtime.process = Mock()
        runtime.process.pid = 12345
        runtime.process.poll.return_value = 0
        receipt = {
            "schema_version": 1,
            "device": runtime.device,
            "bundle_id": runner.BUNDLE_ID,
            "helper_pid": 12345,
            "launch_json": str(runtime.ios_launch_json),
            "action": "detached",
        }
        runtime.ios_cleanup_receipt.write_text(json.dumps(receipt), encoding="utf-8")
        executable = "file:///private/var/containers/Bundle/Application/synthetic/BleHubNitroHost.app/BleHubNitroHost"
        launch = {
            "info": {
                "outcome": "success",
                "commandType": "devicectl.device.process.launch",
                "arguments": [
                    "devicectl",
                    "device",
                    "process",
                    "launch",
                    "--device",
                    runtime.device,
                    "--console",
                    runner.BUNDLE_ID,
                ],
            },
            "result": {
                "deviceIdentifier": "synthetic-coredevice",
                "process": {"processIdentifier": 43210, "executable": executable},
            },
        }
        runtime.ios_launch_json.write_text(json.dumps(launch), encoding="utf-8")
        listing = {
            "info": {
                "outcome": "success",
                "commandType": "devicectl.device.info.processes",
                "arguments": [
                    "devicectl",
                    "device",
                    "info",
                    "processes",
                    "--device",
                    runtime.device,
                ],
            },
            "result": {
                "deviceIdentifier": "synthetic-coredevice",
                "runningProcesses": [
                    {"processIdentifier": 43210, "executable": executable}
                ],
            },
        }
        return runtime, launch, listing

    def test_ios_detached_failure_terminates_only_the_verified_launch_pid(self) -> None:
        runtime, _, listing = self.ios_cleanup_identity()
        runtime.guard.side_effect = runner.WatchdogError("fixture lease lost")

        def command(arguments: list[str], **_: object) -> SimpleNamespace:
            if arguments[2:5] == ["device", "info", "processes"]:
                Path(arguments[arguments.index("--json-output") + 1]).write_text(
                    json.dumps(listing), encoding="utf-8"
                )
            return SimpleNamespace(stdout="synthetic result", returncode=0)

        with (
            patch.object(runner.ios_sdk, "run_command", side_effect=command) as invoked,
            patch.object(runner.os, "killpg") as kill,
        ):
            runtime.stop(success=False)
            self.assertEqual(invoked.call_count, 2)
            self.assertEqual(
                invoked.call_args_list[0].args[0][:5],
                ["xcrun", "devicectl", "device", "info", "processes"],
            )
            terminate = invoked.call_args_list[1].args[0]
            self.assertEqual(
                terminate[:9],
                [
                    "xcrun",
                    "devicectl",
                    "device",
                    "process",
                    "terminate",
                    "--device",
                    runtime.device,
                    "--pid",
                    "43210",
                ],
            )
            self.assertNotIn(runner.BUNDLE_ID, terminate)
            kill.assert_not_called()
        self.assertEqual(
            json.loads((runtime.attempt / "ios-cleanup-processes.json").read_text()),
            listing,
        )

    def test_ios_failure_never_selects_a_later_process_with_the_same_bundle(
        self,
    ) -> None:
        runtime, _, listing = self.ios_cleanup_identity()
        listing["result"]["runningProcesses"][0]["processIdentifier"] = 43211

        def query(arguments: list[str], **_: object) -> SimpleNamespace:
            self.assertEqual(arguments[2:5], ["device", "info", "processes"])
            Path(arguments[arguments.index("--json-output") + 1]).write_text(
                json.dumps(listing), encoding="utf-8"
            )
            return SimpleNamespace(stdout="", returncode=0)

        with patch.object(runner.ios_sdk, "run_command", side_effect=query) as invoked:
            runtime.stop(success=False)
            invoked.assert_called_once()

    def test_ios_cleanup_rejects_missing_foreign_or_malformed_launch_identity(
        self,
    ) -> None:
        for case in (
            "missing receipt",
            "malformed receipt",
            "foreign helper",
            "foreign device",
            "foreign bundle",
            "foreign launch path",
            "unknown cleanup action",
            "missing launch",
            "malformed launch",
            "failed launch",
            "foreign launch device",
            "foreign launch bundle",
            "missing pid",
            "boolean pid",
            "invalid executable",
        ):
            with self.subTest(case=case):
                runtime, launch, _ = self.ios_cleanup_identity()
                receipt = json.loads(runtime.ios_cleanup_receipt.read_text())
                if case == "missing receipt":
                    runtime.ios_cleanup_receipt.unlink()
                elif case == "malformed receipt":
                    runtime.ios_cleanup_receipt.write_text("{", encoding="utf-8")
                elif case in {
                    "foreign helper",
                    "foreign device",
                    "foreign bundle",
                    "foreign launch path",
                    "unknown cleanup action",
                }:
                    field, value = {
                        "foreign helper": ("helper_pid", 12346),
                        "foreign device": ("device", "another-ios"),
                        "foreign bundle": ("bundle_id", "another.bundle"),
                        "foreign launch path": ("launch_json", "previous.launch.json"),
                        "unknown cleanup action": ("action", "unknown"),
                    }[case]
                    receipt[field] = value
                    runtime.ios_cleanup_receipt.write_text(
                        json.dumps(receipt), encoding="utf-8"
                    )
                elif case == "missing launch":
                    runtime.ios_launch_json.unlink()
                elif case == "malformed launch":
                    runtime.ios_launch_json.write_text("[]", encoding="utf-8")
                else:
                    if case == "failed launch":
                        launch["info"]["outcome"] = "failure"
                    elif case == "foreign launch device":
                        launch["info"]["arguments"][5] = "another-ios"
                    elif case == "foreign launch bundle":
                        launch["info"]["arguments"][-1] = "another.bundle"
                    elif case == "missing pid":
                        del launch["result"]["process"]["processIdentifier"]
                    elif case == "boolean pid":
                        launch["result"]["process"]["processIdentifier"] = True
                    elif case == "invalid executable":
                        launch["result"]["process"]["executable"] = "BleHubNitroHost"
                    runtime.ios_launch_json.write_text(
                        json.dumps(launch), encoding="utf-8"
                    )
                with (
                    patch.object(runner.ios_sdk, "run_command") as invoked,
                    self.assertRaisesRegex(
                        runner.WatchdogError, "refusing app termination"
                    ),
                ):
                    runtime.stop(success=False)
                invoked.assert_not_called()

    def test_ios_cleanup_rejects_changed_pid_identity_and_unverifiable_process_lists(
        self,
    ) -> None:
        for case in (
            "changed executable",
            "duplicate pid",
            "foreign device",
            "invalid rows",
            "invalid pid",
            "missing rows",
            "failed query",
        ):
            with self.subTest(case=case):
                runtime, _, listing = self.ios_cleanup_identity()
                if case == "changed executable":
                    listing["result"]["runningProcesses"][0]["executable"] = (
                        "file:///another.app/another"
                    )
                elif case == "duplicate pid":
                    listing["result"]["runningProcesses"] *= 2
                elif case == "foreign device":
                    listing["result"]["deviceIdentifier"] = "another-device"
                elif case == "invalid rows":
                    listing["result"]["runningProcesses"] = ["malformed"]
                elif case == "invalid pid":
                    listing["result"]["runningProcesses"][0]["processIdentifier"] = True
                elif case == "missing rows":
                    del listing["result"]["runningProcesses"]
                elif case == "failed query":
                    listing["info"]["outcome"] = "failure"

                def query(
                    arguments: list[str],
                    *,
                    listing: dict[str, object] = listing,
                    **_: object,
                ) -> SimpleNamespace:
                    self.assertEqual(arguments[2:5], ["device", "info", "processes"])
                    Path(arguments[arguments.index("--json-output") + 1]).write_text(
                        json.dumps(listing), encoding="utf-8"
                    )
                    return SimpleNamespace(stdout="", returncode=0)

                with (
                    patch.object(
                        runner.ios_sdk, "run_command", side_effect=query
                    ) as invoked,
                    self.assertRaisesRegex(
                        runner.WatchdogError, "refusing app termination"
                    ),
                ):
                    runtime.stop(success=False)
                invoked.assert_called_once()

    def test_ios_query_failure_cannot_reuse_a_stale_process_snapshot(self) -> None:
        runtime, _, listing = self.ios_cleanup_identity()
        path = runtime.attempt / "ios-cleanup-processes.json"
        path.write_text(json.dumps(listing), encoding="utf-8")
        with (
            patch.object(
                runner.ios_sdk,
                "run_command",
                side_effect=OSError("query failed"),
            ) as invoked,
            self.assertRaisesRegex(OSError, "query failed"),
        ):
            runtime.stop(success=False)
        invoked.assert_called_once()
        self.assertFalse(path.exists())

    def test_ios_launch_failure_without_a_helper_never_terminates_an_app(self) -> None:
        runtime = self.runtime("ios", "synthetic-ios", self.root, Mock())
        with (
            patch.object(
                runner.subprocess, "Popen", side_effect=OSError("helper failed")
            ),
            self.assertRaisesRegex(OSError, "helper failed"),
        ):
            runtime.launch()
        with patch.object(runner.ios_sdk, "run_command") as invoked:
            runtime.stop(success=False)
        invoked.assert_not_called()
        self.assertTrue(runtime.output.closed)

    def test_zombie_runtime_poll_and_failed_launch_cleanup_only_owned_process(
        self,
    ) -> None:
        guard = Mock()
        runtime = self.runtime("ios", "explicit-device", self.root, guard)
        runtime.process = Mock()
        runtime.process.poll.return_value = 1
        with self.assertRaisesRegex(runner.WatchdogError, "exited early"):
            runtime.snapshot()
        runtime.process.poll.assert_called_once()
        runtime.process.poll.return_value = 0
        with self.assertRaisesRegex(runner.WatchdogError, "exited early"):
            runtime.snapshot(allow_exit=True)
        with patch.object(runner.os, "killpg") as kill:
            runtime.stop(success=True)
            kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
