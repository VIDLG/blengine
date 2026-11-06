"""Hardware-free session proof tests using real loaders, BTP commands and codec."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from collections.abc import Callable, Iterator
from contextlib import contextmanager, redirect_stdout
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from . import direct_fixture, fixture_session
from .btp_commands import BtpCommandError
from .btp_protocol import BtpFrame
from .btp_session import BtpSession, FrameTransport
from .fixture_attestation import (
    FixtureAttestationError,
    InstalledImageAttestation,
    load_attestation,
)
from .fixture_attestation_wire import (
    INDEX_NONE,
    READ_IMAGE_ATTESTATION,
    VENDOR,
    FixtureAttestationWireError,
    encode_attestation,
)
from .fixture_contract import (
    MULTIFUNCTION_CAPABILITIES,
    MULTIFUNCTION_CAPABILITY_VARIANTS,
    FixtureContractError,
    load_contract,
)
from .fixture_session import (
    CONTRACT_NAME,
    PROFILE_NAME,
    FixtureSession,
    FixtureSessionError,
    attest_session,
    load_session,
    prepare_session,
    verify_receipt,
)
from .profile import ProfileError, load_profile
from .resource_lease import DeviceLease, ResourceLeaseError

ROOT = Path(__file__).resolve().parents[3]
PROFILE = ROOT / "tests/conformance/scenarios/blehub-nrf-indication-v2.json"
MULTIFUNCTION_PROFILE = (
    ROOT / "tests/peripherals/nrf52840/firmware/profiles/multifunction-rf-v2-b.json"
)
MULTIFUNCTION_IMAGE = (
    ROOT / "tests/peripherals/nrf52840/firmware/images/multifunction-rf-v2-b.json"
)
PORT = "/dev/cu.hardware-free-test"
IDENTITY = "jlink:123456"


class FixtureSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile_path = self.root / "source-profile.json"
        self.profile_path.write_bytes(MULTIFUNCTION_PROFILE.read_bytes())
        profile = load_profile(self.profile_path)
        # This digest is deliberately synthetic; no test claims physical image evidence.
        self.contract_path = self.root / "source-contract.json"
        declaration = json.loads(MULTIFUNCTION_IMAGE.read_bytes())
        declaration.pop("settings")
        declaration.update(
            image_sha256="ab" * 32,
            service_uuid=profile.service_uuid,
            local_name=profile.local_name,
        )
        self.contract_path.write_text(json.dumps(declaration), encoding="utf-8")
        self.contract = load_contract(self.contract_path)
        expected = asdict(self.contract)
        expected.pop("fixture_role")
        expected["schema_version"] = 1
        expected["capabilities"] = sorted(self.contract.capabilities)
        self.expected_path = self.root / "historical-expected.json"
        self.expected_path.write_text(json.dumps(expected), encoding="utf-8")
        self.installed = load_attestation(self.expected_path)
        self.lease = Mock(spec=DeviceLease)
        self.lease.identity = IDENTITY

    def prepare(self, name: str = "attempt", **kwargs: object) -> FixtureSession:
        with patch.object(
            fixture_session, "identity_for_port", return_value=IDENTITY
        ) as usb:
            plan = prepare_session(
                self.contract_path,
                self.profile_path,
                self.root / name,
                port=PORT,
                service_uuid=self.contract.service_uuid,
                **kwargs,
            )
        usb.assert_called_once_with(PORT)
        return plan

    def vendor_session(
        self, response: BtpFrame | None = None
    ) -> tuple[BtpSession, Mock]:
        transport = Mock(spec=FrameTransport)
        if response is None:
            response = BtpFrame(
                VENDOR,
                READ_IMAGE_ATTESTATION,
                INDEX_NONE,
                encode_attestation(self.installed),
            )
        transport.receive.return_value = BtpFrame.decode(response.encode())
        return BtpSession(transport), transport

    def attest(self, plan: FixtureSession) -> Mock:
        session, transport = self.vendor_session()
        attest_session(plan, session, lease=self.lease, port=PORT)
        return transport

    def receipt(self, plan: FixtureSession) -> dict[str, object]:
        return verify_receipt(plan, fixture_identity=(os.getpid(), os.getpgid(0)))

    @staticmethod
    def overwrite(path: Path, content: bytes) -> None:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        path.write_bytes(content)

    def rewrite_json(self, path: Path, **changes: object) -> None:
        document = json.loads(path.read_bytes())
        document.update(changes)
        self.overwrite(path, json.dumps(document).encode("utf-8"))

    def configure_multifunction(self) -> None:
        self.profile_path.write_bytes(MULTIFUNCTION_PROFILE.read_bytes())
        profile = load_profile(self.profile_path)
        document = json.loads(MULTIFUNCTION_IMAGE.read_bytes())
        document.pop("settings")
        # This temporary digest is fake-only host input, not a built-image contract.
        document.update(
            image_sha256="ab" * 32,
            service_uuid=profile.service_uuid,
            local_name=profile.local_name,
        )
        self.contract_path.write_text(json.dumps(document), encoding="utf-8")
        self.contract = load_contract(self.contract_path)
        document.pop("fixture_role")
        self.expected_path.write_text(json.dumps(document), encoding="utf-8")
        self.installed = load_attestation(self.expected_path)

    def test_explicit_attempt_uuid_binds_live_receipt_and_rejects_alias_before_usb(self) -> None:
        run_id = "12345678-1234-4234-8234-123456789abc"
        plan = self.prepare(session_id=run_id)
        self.attest(plan)
        self.assertEqual(plan.session_id, run_id)
        self.assertEqual(self.receipt(plan)["session_id"], run_id)
        self.assertEqual(load_session(plan.request_path), plan)
        for index, value in enumerate((run_id.upper(), run_id.replace("-", ""), "foreign", True, 7)):
            path = self.root / f"invalid-{index}"
            with self.subTest(value=value), patch.object(fixture_session, "identity_for_port") as usb:
                with self.assertRaises(ValueError):
                    prepare_session(self.contract_path, self.profile_path, path, port=PORT,
                                    service_uuid=self.contract.service_uuid, session_id=value)
                usb.assert_not_called()
                self.assertFalse(path.exists())

    def test_multifunction_normal_provider_needs_live_proof_but_no_arm(self) -> None:
        """Fake-only: no physical RF or firmware hold/reset behavior is claimed."""
        self.configure_multifunction()
        plan = self.prepare(expected_attestation_path=self.expected_path)
        self.assertEqual(load_session(plan.request_path), plan)
        with self.assertRaises(FixtureSessionError):
            self.receipt(plan)  # Historical identity alone never proves a live session.
        loaded = fixture_session.load_session_profile(plan.contract, plan.profile_path)
        self.assertEqual(
            {c.role: c.permissions for c in loaded.characteristics},
            {
                "read-write": 0x03,
                "updates": 0x01,
                "indications": 0x01,
                "read-only": 0x01,
                "write-only": 0x02,
            },
        )
        self.contract_path.unlink()
        self.profile_path.unlink()
        self.expected_path.unlink()
        with self.direct_resources() as resources:
            handles = {
                c.role: index for index, c in enumerate(loaded.characteristics, 1)
            }
            resources.doctor.run.return_value = SimpleNamespace(role_handles=handles)
            fixture = direct_fixture.DirectFixture(
                PORT, plan.profile_path, fixture_session=plan
            )
            self.addCleanup(fixture.stop)
            self.assertEqual(fixture.start(), handles)
            resources.doctor.run.assert_called_once_with(loaded)
            # Live attestation plus read-only v2 engine inspection; never ARM/reset.
            self.assertEqual(
                [call.args[0] for call in resources.transport.send.call_args_list],
                [
                    BtpFrame(VENDOR, READ_IMAGE_ATTESTATION, INDEX_NONE),
                    BtpFrame(VENDOR, 0x10, INDEX_NONE, b"\x02"),
                ],
            )
            self.receipt(plan)
            original = plan.receipt_path.read_bytes()
            fixture.stop()
            self.assertEqual(plan.receipt_path.read_bytes(), original)
            self.assertEqual(
                resources.events[-3:], ["session-close", "serial-close", "release"]
            )

    def test_reviewed_capability_variants_preserve_frozen_session_and_topology(
        self,
    ) -> None:
        for index, capabilities in enumerate(MULTIFUNCTION_CAPABILITY_VARIANTS):
            with self.subTest(capabilities=capabilities):
                self.rewrite_json(self.contract_path, capabilities=capabilities)
                self.contract = load_contract(self.contract_path)
                plan = self.prepare(f"variant-{index}")
                self.assertEqual(load_session(plan.request_path), plan)
                profile = fixture_session.load_session_profile(
                    plan.contract, plan.profile_path
                )
                self.assertEqual(len(profile.characteristics), 5)
                self.assertEqual(profile, load_profile(MULTIFUNCTION_PROFILE))
                for drift in (
                    capabilities[1:],
                    (*capabilities, "unknown-observer-v2"),
                ):
                    with (
                        self.subTest(drift=drift),
                        self.assertRaisesRegex(ProfileError, "capability variant"),
                    ):
                        fixture_session.load_session_profile(
                            replace(self.contract, capabilities=frozenset(drift)),
                            plan.profile_path,
                        )
        # Preparing the optional candidate must not invalidate an older frozen request.
        base = load_session(self.root / "variant-0" / fixture_session.REQUEST_NAME)
        self.assertEqual(
            base.contract.capabilities,
            frozenset(MULTIFUNCTION_CAPABILITY_VARIANTS[0]),
        )

    def test_multifunction_runtime_permission_and_topology_drift_rejected(self) -> None:
        self.configure_multifunction()
        original = json.loads(self.profile_path.read_bytes())
        for location in ("profile", "advertising", "service"):
            with self.subTest(authorization_location=location):
                document = json.loads(json.dumps(original))
                definition = (
                    document
                    if location == "profile"
                    else document["advertising"]
                    if location == "advertising"
                    else document["services"][0]
                )
                definition["read_authorization"] = True
                self.profile_path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaisesRegex(
                    ProfileError, "must not request authorization"
                ):
                    fixture_session.load_session_profile(
                        self.contract, self.profile_path
                    )
        mutations = (
            ("read-write", "read_authorization", True),
            ("read-write", "read_authorization", False),
            ("read-only", "read_authorization", True),
            ("updates", "write_authorization", True),
            ("read-write", "permissions", 0x43),
            ("read-write", "uuid", "fff90000-0000-4000-8000-000000000000"),
            ("updates", "properties", ["read", "notify"]),
            ("indications", "properties", ["read", "indicate", "indicate"]),
            ("read-write", "initial_value_hex", "01"),
            ("updates", "initial_value_hex", "01"),
            ("indications", "initial_value_hex", "01"),
            ("read-only", "initial_value_hex", "00"),
            ("write-only", "initial_value_hex", "01"),
        )
        for role, field, value in mutations:
            with self.subTest(role=role, field=field, value=value):
                document = json.loads(json.dumps(original))
                item = next(
                    c
                    for c in document["services"][0]["characteristics"]
                    if c["role"] == role
                )
                item[field] = value
                self.profile_path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaises(ProfileError):
                    fixture_session.load_session_profile(
                        self.contract, self.profile_path
                    )
        for change in (
            "missing",
            "extra",
            "primary",
            "advertise",
            "advertised-service",
            "profile_id",
            "local_name",
        ):
            with self.subTest(change=change):
                document = json.loads(json.dumps(original))
                service = document["services"][0]
                if change == "missing":
                    service["characteristics"].pop()
                elif change == "extra":
                    service["characteristics"].append(
                        {
                            "role": "extra",
                            "uuid": "fff60000-0000-4000-8000-000000000000",
                            "properties": ["read"],
                            "initial_value_hex": "00",
                        }
                    )
                elif change in {"primary", "advertise"}:
                    service[change] = False
                elif change == "advertised-service":
                    document["advertising"]["service_uuids"] = []
                elif change == "profile_id":
                    document[change] = "foreign-profile"
                else:
                    document["advertising"][change] = "ForeignName"
                self.profile_path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaises((ProfileError, FixtureContractError)):
                    fixture_session.load_session_profile(
                        self.contract, self.profile_path
                    )
        self.profile_path.write_text(json.dumps(original), encoding="utf-8")
        with (
            patch.object(
                fixture_session,
                "load_profile",
                return_value=replace(
                    load_profile(self.profile_path),
                    characteristics=tuple(
                        replace(c, permissions=0x43) if c.role == "read-write" else c
                        for c in load_profile(self.profile_path).characteristics
                    ),
                ),
            ),
            self.assertRaisesRegex(ProfileError, "ordinary read/write permissions"),
        ):
            fixture_session.load_session_profile(self.contract, self.profile_path)

    def test_hold_caps_and_reserved_services_require_explicit_roles_not_prefixes(
        self,
    ) -> None:
        self.configure_multifunction()
        for role in ("normal-rf", "multifunction-rf-extra", "other-diagnostic"):
            with (
                self.subTest(role=role),
                self.assertRaisesRegex(ProfileError, "explicit"),
            ):
                fixture_session.load_session_profile(
                    replace(self.contract, fixture_role=role), self.profile_path
                )
        for capability in MULTIFUNCTION_CAPABILITIES:
            with self.subTest(missing=capability), self.assertRaises(ProfileError):
                fixture_session.load_session_profile(
                    replace(
                        self.contract,
                        capabilities=self.contract.capabilities - {capability},
                    ),
                    self.profile_path,
                )
        document = json.loads(PROFILE.read_bytes())
        for service_uuid in (
            "fd100000-0000-4000-8000-000000000000",
            "fd300000-0000-4000-8000-000000000000",
            "fd400000-0000-4000-8000-000000000000",
            "fd500000-0000-4000-8000-000000000000",
        ):
            document["services"][0]["uuid"] = service_uuid
            document["advertising"]["service_uuids"] = [service_uuid]
            self.profile_path.write_text(json.dumps(document), encoding="utf-8")
            with (
                self.subTest(service=service_uuid),
                self.assertRaisesRegex(ProfileError, "explicit"),
            ):
                fixture_session.load_session_profile(
                    replace(
                        self.contract,
                        fixture_role="normal-rf",
                        capabilities=frozenset({"normal-rf"}),
                    ),
                    self.profile_path,
                )
        self.profile_path.write_bytes(PROFILE.read_bytes())
        with self.assertRaisesRegex(ProfileError, "explicit"):
            fixture_session.load_session_profile(
                replace(
                    self.contract,
                    fixture_role="normal-rf",
                    capabilities=frozenset({"normal-rf", "diagnostic-btp"}),
                ),
                self.profile_path,
            )

    def test_multifunction_fake_identity_cannot_authorize_rf_mutation(self) -> None:
        self.configure_multifunction()
        plan = self.prepare()
        foreign = replace(self.installed, image_sha256="00" * 32)
        response = BtpFrame(
            VENDOR, READ_IMAGE_ATTESTATION, INDEX_NONE, encode_attestation(foreign)
        )
        with (
            self.direct_resources(response) as resources,
            redirect_stdout(io.StringIO()) as output,
        ):
            with self.assertRaisesRegex(FixtureAttestationError, "image_sha256"):
                direct_fixture.serve(PORT, plan.profile_path, fixture_session=plan)
            resources.doctor.run.assert_not_called()
            self.assertEqual(resources.gap.mock_calls, [])
            self.assertEqual(resources.gatt.mock_calls, [])
            self.assertNotIn("NRF52840_DIRECT_READY", output.getvalue())
        self.assertFalse(plan.receipt_path.exists())

    def test_retired_diagnostic_roles_never_authorize_live_auth_reads(self) -> None:
        document = json.loads(PROFILE.read_bytes())
        items = document["services"][0]["characteristics"]
        target = next(item for item in items if item["role"] == "read-write")
        target["read_authorization"] = True
        self.profile_path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(ProfileError):
            self.prepare("normal-rejects")
        contract = json.loads(self.contract_path.read_bytes())
        for index, role in enumerate(
            ("diagnostic-watchdog", "diagnostic-pending-read")
        ):
            with self.subTest(role=role):
                contract["fixture_role"] = role
                self.contract_path.write_text(json.dumps(contract), encoding="utf-8")
                with self.assertRaisesRegex(ProfileError, "retired v1 diagnostic"):
                    self.prepare(f"diagnostic-{index}")
                # Invalid authorization shapes must fail too, not select another lane.
                for field, value in (
                    ("read_authorization", False),
                    ("read_authorization", 1),
                ):
                    target[field] = value
                    self.profile_path.write_text(json.dumps(document), encoding="utf-8")
                    with self.assertRaises(ProfileError):
                        self.prepare(f"invalid-{index}-{value}")
                target["read_authorization"] = True
                other = next(item for item in items if item["role"] == "read-only")
                other["read_authorization"] = True
                self.profile_path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaises(ProfileError):
                    self.prepare(f"extra-auth-{index}")
                del other["read_authorization"]
                self.profile_path.write_text(json.dumps(document), encoding="utf-8")
        contract["fixture_role"] = "other-diagnostic"
        self.contract_path.write_text(json.dumps(contract), encoding="utf-8")
        with self.assertRaises(ProfileError):
            self.prepare("unknown-role")

    def test_sources_can_change_or_disappear_without_changing_frozen_inputs(
        self,
    ) -> None:
        plan = self.prepare(expected_attestation_path=self.expected_path)
        frozen = {
            path: path.read_bytes()
            for path in (
                plan.request_path,
                plan.root / CONTRACT_NAME,
                plan.profile_path,
            )
        }
        self.contract_path.write_text("not JSON anymore", encoding="utf-8")
        self.profile_path.unlink()
        self.expected_path.unlink()
        self.assertEqual(load_session(plan.request_path), plan)
        self.attest(plan)
        self.receipt(plan)
        for path, original in frozen.items():
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), stat.S_IRUSR)

    def test_child_loads_immutable_sources_and_rejects_each_edited_snapshot(
        self,
    ) -> None:
        plan = self.prepare()
        self.contract_path.unlink()
        self.profile_path.unlink()
        code = (
            "import sys; from pathlib import Path; "
            "from tests.peripherals.nrf52840.fixture_session import load_session; "
            "load_session(Path(sys.argv[1]))"
        )

        def child() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [sys.executable, "-c", code, str(plan.request_path)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )

        self.assertEqual(child().returncode, 0)
        for path in (plan.root / CONTRACT_NAME, plan.profile_path, plan.request_path):
            with self.subTest(snapshot=path.name):
                original = path.read_bytes()
                if path == plan.request_path:
                    self.rewrite_json(path, schema_version=2)
                    message = "keys/schema_version"
                else:
                    self.overwrite(path, original + b"\n")
                    message = "snapshot hash mismatch"
                result = child()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertFalse(plan.receipt_path.exists())
                self.overwrite(path, original)

    def test_fresh_receipt_comes_from_real_vendor_command_and_exact_wire_identity(
        self,
    ) -> None:
        plan = self.prepare(expected_attestation_path=self.expected_path)
        self.assertFalse(
            plan.receipt_path.exists()
        )  # Expected JSON alone is not live proof.
        with self.assertRaises(FixtureSessionError):
            self.receipt(plan)
        transport = self.attest(plan)
        transport.send.assert_called_once_with(
            BtpFrame(VENDOR, READ_IMAGE_ATTESTATION, INDEX_NONE)
        )
        self.assertEqual(
            self.lease.require.call_args_list,
            [
                call("jlink", "123456"),
                call("jlink", "123456"),
            ],
        )
        receipt = self.receipt(plan)
        self.assertEqual(receipt["session_id"], plan.session_id)
        self.assertEqual(receipt["fixture_pid"], os.getpid())
        self.assertEqual(receipt["fixture_pgid"], os.getpgid(0))
        self.assertEqual(receipt["resource_identity"], IDENTITY)
        self.assertEqual(receipt["port"], PORT)
        self.assertEqual(receipt["proof_method"], fixture_session.PROOF_METHOD)
        for filename, field in (
            (CONTRACT_NAME, "contract_sha256"),
            (PROFILE_NAME, "profile_sha256"),
        ):
            self.assertEqual(
                receipt[field],
                hashlib.sha256((plan.root / filename).read_bytes()).hexdigest(),
            )
        self.assertEqual(load_attestation_from_receipt(plan), self.installed)
        captured = datetime.fromisoformat(receipt["captured_at_utc"])
        self.assertGreaterEqual(captured.timestamp(), plan.request_path.stat().st_mtime)
        self.assertLessEqual(captured, datetime.now(UTC))
        # Runners verify before and after RN; verification must not consume evidence.
        original = plan.receipt_path.read_bytes()
        self.assertEqual(self.receipt(plan), receipt)
        self.assertEqual(plan.receipt_path.read_bytes(), original)

    def test_wrong_image_missing_capability_unknown_vendor_and_bad_wire_leave_no_receipt(
        self,
    ) -> None:
        cases = (
            (
                replace(self.installed, image_sha256="00" * 32),
                None,
                FixtureAttestationError,
                "image_sha256",
            ),
            (
                replace(self.installed, capabilities=frozenset({"normal-rf"})),
                None,
                FixtureAttestationError,
                "missing required capabilities",
            ),
            (
                None,
                BtpFrame(VENDOR, 0, INDEX_NONE, b"\x02"),
                BtpCommandError,
                "does not implement",
            ),
            (
                None,
                BtpFrame(VENDOR, READ_IMAGE_ATTESTATION, INDEX_NONE, b"\x01"),
                FixtureAttestationWireError,
                "truncated",
            ),
        )
        for index, (installed, response, error, message) in enumerate(cases):
            with self.subTest(case=index):
                plan = self.prepare(f"failure-{index}")
                if installed is not None:
                    response = BtpFrame(
                        VENDOR,
                        READ_IMAGE_ATTESTATION,
                        INDEX_NONE,
                        encode_attestation(installed),
                    )
                session, transport = self.vendor_session(response)
                with self.assertRaisesRegex(error, message):
                    attest_session(plan, session, lease=self.lease, port=PORT)
                transport.send.assert_called_once()
                self.assertFalse(plan.receipt_path.exists())
                self.assertFalse(list(plan.root.glob(".receipt-*")))

    def test_identity_and_lease_fail_before_vendor_call(self) -> None:
        plan = self.prepare()
        for port, identity, lease_error in (
            ("wrong-port", IDENTITY, None),
            (PORT, "jlink:999999", None),
            (PORT, IDENTITY, ResourceLeaseError("lease expired")),
        ):
            with self.subTest(port=port, identity=identity, lease_error=lease_error):
                lease = Mock(spec=DeviceLease, identity=identity)
                lease.require.side_effect = lease_error
                session, transport = self.vendor_session()
                with self.assertRaises((FixtureSessionError, ResourceLeaseError)):
                    attest_session(plan, session, lease=lease, port=port)
                transport.send.assert_not_called()
                self.assertFalse(plan.receipt_path.exists())

    def test_lease_loss_or_snapshot_edit_during_vendor_read_does_not_publish(
        self,
    ) -> None:
        for cause in ("lease", "snapshot"):
            with self.subTest(cause=cause):
                plan = self.prepare(cause)
                session, transport = self.vendor_session()
                response = transport.receive.return_value
                if cause == "lease":
                    self.lease.require.side_effect = [
                        None,
                        ResourceLeaseError("lease lost"),
                    ]
                else:

                    def edit(
                        _: float,
                        selected: FixtureSession = plan,
                        result: BtpFrame = response,
                    ) -> BtpFrame:
                        self.overwrite(
                            selected.profile_path,
                            selected.profile_path.read_bytes() + b"\n",
                        )
                        return result

                    transport.receive.side_effect = edit
                with self.assertRaises((FixtureSessionError, ResourceLeaseError)):
                    attest_session(plan, session, lease=self.lease, port=PORT)
                transport.send.assert_called_once()
                self.assertFalse(plan.receipt_path.exists())
                self.lease.require.side_effect = None

    def test_snapshot_edit_during_loader_validation_is_rejected(self) -> None:
        plan = self.prepare()
        validate = fixture_session.validate_fixture_inputs

        def edit(*args: object, **kwargs: object) -> object:
            contract = validate(*args, **kwargs)
            self.overwrite(plan.profile_path, plan.profile_path.read_bytes() + b"\n")
            return contract

        with (
            patch.object(fixture_session, "validate_fixture_inputs", side_effect=edit),
            self.assertRaisesRegex(FixtureSessionError, "changed during validation"),
        ):
            load_session(plan.request_path)

    def test_unsupported_process_group_identity_fails_closed_without_vendor_read(
        self,
    ) -> None:
        plan = self.prepare()
        session, transport = self.vendor_session()
        with (
            patch.object(fixture_session.os, "getpgid", None),
            self.assertRaisesRegex(
                FixtureSessionError, "process-group identity support"
            ),
        ):
            attest_session(plan, session, lease=self.lease, port=PORT)
        transport.send.assert_not_called()
        self.assertFalse(plan.receipt_path.exists())

    def test_receipt_rejects_foreign_nonce_process_resource_and_hashes(self) -> None:
        plan = self.prepare()
        self.attest(plan)
        original = plan.receipt_path.read_bytes()
        for field, wrong in (
            ("session_id", "00000000-0000-4000-8000-000000000000"),
            ("fixture_pid", os.getpid() + 1),
            ("fixture_pgid", os.getpgid(0) + 1),
            ("resource_identity", "jlink:999999"),
            ("port", "another-port"),
            ("contract_sha256", "0" * 64),
            ("profile_sha256", "0" * 64),
            ("proof_method", "historical-json"),
            ("schema_version", True),
            ("fixture_pid", str(os.getpid())),
        ):
            with self.subTest(field=field, wrong=wrong):
                self.rewrite_json(plan.receipt_path, **{field: wrong})
                with self.assertRaisesRegex(FixtureSessionError, "not bound"):
                    self.receipt(plan)
                self.overwrite(plan.receipt_path, original)
        with self.assertRaisesRegex(FixtureSessionError, "not bound"):
            verify_receipt(plan, fixture_identity=(os.getpid() + 1, os.getpgid(0)))
        self.assertEqual(plan.receipt_path.read_bytes(), original)

    def test_receipt_rejects_stale_future_naive_and_malformed_capture_times(
        self,
    ) -> None:
        plan = self.prepare()
        self.attest(plan)
        original = plan.receipt_path.read_bytes()
        for timestamp in (
            (datetime.now(UTC) - timedelta(days=1)).isoformat(),
            (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            "2026-01-01T00:00:00",
            "not-a-date",
            123,
        ):
            with self.subTest(timestamp=timestamp):
                self.rewrite_json(plan.receipt_path, captured_at_utc=timestamp)
                with self.assertRaisesRegex(FixtureSessionError, "timestamp"):
                    self.receipt(plan)
                self.overwrite(plan.receipt_path, original)

    def test_replay_from_earlier_attempt_rejected_and_original_receipt_preserved(
        self,
    ) -> None:
        earlier = self.prepare("earlier")
        self.attest(earlier)
        original = earlier.receipt_path.read_bytes()
        later = self.prepare("later")
        later.receipt_path.write_bytes(original)
        self.assertNotEqual(earlier.session_id, later.session_id)
        with self.assertRaisesRegex(FixtureSessionError, "not bound"):
            self.receipt(later)
        session, transport = self.vendor_session()
        with self.assertRaisesRegex(FixtureSessionError, "existing receipt"):
            attest_session(earlier, session, lease=self.lease, port=PORT)
        transport.send.assert_not_called()
        self.assertEqual(earlier.receipt_path.read_bytes(), original)
        self.assertEqual(later.receipt_path.read_bytes(), original)

    def test_receipt_rejects_changed_installed_image_capability_keys_and_snapshots(
        self,
    ) -> None:
        plan = self.prepare()
        self.attest(plan)
        original = plan.receipt_path.read_bytes()
        receipt = self.receipt(plan)
        for changes in (
            {"image_sha256": "00" * 32},
            {"capabilities": ["normal-rf"]},
        ):
            with self.subTest(changes=changes):
                installed = {**receipt["installed_identity"], **changes}
                self.rewrite_json(plan.receipt_path, installed_identity=installed)
                with self.assertRaises(FixtureAttestationError):
                    self.receipt(plan)
                self.overwrite(plan.receipt_path, original)
        self.rewrite_json(plan.receipt_path, unexpected=True)
        with self.assertRaisesRegex(FixtureSessionError, "keys are invalid"):
            self.receipt(plan)
        self.overwrite(plan.receipt_path, original)
        self.overwrite(plan.profile_path, plan.profile_path.read_bytes() + b"\n")
        with self.assertRaisesRegex(FixtureSessionError, "snapshot hash mismatch"):
            self.receipt(plan)
        self.assertEqual(plan.receipt_path.read_bytes(), original)

    def test_duplicate_json_rejected_in_sources_snapshots_request_and_nested_receipt(
        self,
    ) -> None:
        for source in (self.contract_path, self.profile_path):
            with self.subTest(source=source.name):
                original = source.read_bytes()
                self.overwrite(source, b'{"schema_version": 1,' + original.lstrip()[1:])
                with self.assertRaisesRegex(
                    FixtureSessionError, "duplicate session JSON key"
                ):
                    self.prepare(source.stem)
                self.overwrite(source, original)
        plan = self.prepare()
        self.attest(plan)
        for path, duplicate in (
            (plan.root / CONTRACT_NAME, b'"schema_version": 1,'),
            (plan.profile_path, b'"schema_version": 1,'),
            (plan.request_path, b'"schema_version": 1,'),
            (plan.receipt_path, b'"installed_identity": {"board": "a", "board": "b"},'),
        ):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                self.overwrite(path, b"{" + duplicate + original.lstrip()[1:])
                with self.assertRaisesRegex(
                    FixtureSessionError, "duplicate session JSON key"
                ):
                    self.receipt(plan)
                self.overwrite(path, original)

    def test_sources_and_snapshots_refuse_symlinks_and_hardlinks(self) -> None:
        for link_kind in ("symbolic", "hard"):
            for source in (self.contract_path, self.profile_path):
                with self.subTest(link_kind=link_kind, source=source.name):
                    original = source.read_bytes()
                    target = self.root / f"target-{source.name}"
                    source.rename(target)
                    if link_kind == "symbolic":
                        source.symlink_to(target)
                    else:
                        os.link(target, source)
                    with self.assertRaisesRegex(FixtureSessionError, "linked"):
                        self.prepare(f"{link_kind}-{source.stem}")
                    source.unlink()
                    target.rename(source)
                    self.assertEqual(source.read_bytes(), original)
        plan = self.prepare()
        self.attest(plan)
        for path in (
            plan.request_path,
            plan.root / CONTRACT_NAME,
            plan.profile_path,
            plan.receipt_path,
        ):
            for link_kind in ("symbolic", "hard"):
                with self.subTest(path=path.name, link_kind=link_kind):
                    target = self.root / f"target-{path.name}"
                    path.rename(target)
                    if link_kind == "symbolic":
                        path.symlink_to(target)
                    else:
                        os.link(target, path)
                    with self.assertRaisesRegex(FixtureSessionError, "linked"):
                        self.receipt(plan)
                    path.unlink()
                    target.rename(path)

    def test_linked_session_directory_and_existing_output_directory_refused(
        self,
    ) -> None:
        plan = self.prepare()
        original = plan.request_path.read_bytes()
        alias = self.root / "linked-session"
        alias.symlink_to(plan.root, target_is_directory=True)
        with self.assertRaisesRegex(FixtureSessionError, "private session directory"):
            load_session(alias / plan.request_path.name)
        for name in (plan.root.name, alias.name):
            with self.subTest(name=name), self.assertRaises(FileExistsError):
                self.prepare(name)
        self.assertEqual(plan.request_path.read_bytes(), original)

    def test_existing_and_dangling_receipt_links_refused_without_vendor_call(
        self,
    ) -> None:
        for dangling in (False, True):
            with self.subTest(dangling=dangling):
                plan = self.prepare(f"link-{dangling}")
                target = self.root / f"receipt-target-{dangling}"
                if not dangling:
                    target.write_bytes(b"preserve evidence")
                plan.receipt_path.symlink_to(target)
                session, transport = self.vendor_session()
                with self.assertRaisesRegex(FixtureSessionError, "existing receipt"):
                    attest_session(plan, session, lease=self.lease, port=PORT)
                transport.send.assert_not_called()
                self.assertTrue(plan.receipt_path.is_symlink())
                if not dangling:
                    self.assertEqual(target.read_bytes(), b"preserve evidence")

    def test_receipt_created_during_publication_is_never_replaced(self) -> None:
        for replacement in ("file", "link"):
            with self.subTest(replacement=replacement):
                plan = self.prepare(replacement)
                target = self.root / f"retained-{replacement}"
                target.write_bytes(b"earlier evidence")
                link = os.link

                def race(
                    source: Path,
                    destination: Path,
                    mode: str = replacement,
                    retained: Path = target,
                    publish: Callable[[Path, Path], None] = link,
                ) -> None:
                    if mode == "file":
                        destination.write_bytes(retained.read_bytes())
                    else:
                        destination.symlink_to(retained)
                    publish(source, destination)

                with (
                    patch.object(fixture_session.os, "link", side_effect=race),
                    self.assertRaisesRegex(FixtureSessionError, "cannot publish fresh"),
                ):
                    self.attest(plan)
                self.assertEqual(plan.receipt_path.read_bytes(), b"earlier evidence")
                self.assertEqual(target.read_bytes(), b"earlier evidence")
                self.assertFalse(list(plan.root.glob(".receipt-*")))

    def test_nonregular_oversized_and_replaced_during_read_inputs_refused(self) -> None:
        plan = self.prepare()
        path = plan.profile_path
        original = path.read_bytes()
        path.unlink()
        path.mkdir()
        with self.assertRaisesRegex(FixtureSessionError, "non-regular"):
            load_session(plan.request_path)
        path.rmdir()
        path.write_bytes(b" " * (fixture_session.MAX_DOCUMENT_BYTES + 1))
        with self.assertRaisesRegex(FixtureSessionError, "too large"):
            load_session(plan.request_path)
        path.write_bytes(original)
        open_file = os.open

        def replace_before_open(selected: Path, flags: int) -> int:
            if selected == path:
                replacement = self.root / "replacement.json"
                replacement.write_bytes(original)
                replacement.replace(path)
            return open_file(selected, flags)

        with (
            patch.object(fixture_session.os, "open", side_effect=replace_before_open),
            self.assertRaisesRegex(FixtureSessionError, "changed while reading"),
        ):
            load_session(plan.request_path)

    def test_declared_input_drift_rejected_before_usb_selection(self) -> None:
        with patch.object(fixture_session, "identity_for_port") as usb:
            with self.assertRaisesRegex(FixtureAttestationError, "service UUID"):
                prepare_session(
                    self.contract_path,
                    self.profile_path,
                    self.root / "bad-service",
                    port=PORT,
                    service_uuid="fd200000-0000-4000-8000-000000000000",
                )
            usb.assert_not_called()
        self.rewrite_json(self.profile_path, profile_id="different-profile")
        with patch.object(fixture_session, "identity_for_port") as usb:
            with self.assertRaisesRegex(FixtureContractError, "profile_id"):
                prepare_session(
                    self.contract_path,
                    self.profile_path,
                    self.root / "bad-profile",
                    port=PORT,
                    service_uuid=self.contract.service_uuid,
                )
            usb.assert_not_called()

    @contextmanager
    def direct_resources(
        self, response: BtpFrame | None = None
    ) -> Iterator[SimpleNamespace]:
        events: list[str] = []
        session, transport = self.vendor_session(response)
        transport.send.side_effect = lambda _: events.append("vendor")
        attestation_reply = transport.receive.return_value
        from .support.read_fault_emulator import Clock, Wire

        limits_wire = Wire(Clock(), "")
        limits_wire.send(BtpFrame(255, 0x10, 255, b"\x02"))
        limits_reply = limits_wire.frames.pop()
        transport.receive.side_effect = lambda _: (
            limits_reply
            if transport.send.call_args.args[0].opcode == 0x10
            else attestation_reply
        )
        serial = Mock()
        serial.close.side_effect = lambda: events.append("serial-close")
        transport.close.side_effect = lambda: events.append("session-close")

        @contextmanager
        def lease(*args: object, **kwargs: object) -> Iterator[DeviceLease]:
            events.append("acquire")
            try:
                yield self.lease
            finally:
                events.append("release")

        with (
            patch.object(direct_fixture, "port_lease", side_effect=lease),
            patch.object(direct_fixture, "open_serial", return_value=serial),
            patch.object(direct_fixture, "FramedTransport", return_value=transport),
            patch.object(direct_fixture, "GapCommands") as gap,
            patch.object(direct_fixture, "GattCommands") as gatt,
            patch.object(direct_fixture, "NrfDoctor") as doctor,
        ):
            # BtpSession and profile loading stay real, as does the lazy attest_session import.
            yield SimpleNamespace(
                events=events,
                session=session,
                transport=transport,
                gap=gap.return_value,
                gatt=gatt.return_value,
                doctor=doctor.return_value,
            )

    def test_real_direct_fixture_attests_before_mutation_and_retains_receipt_after_stop(
        self,
    ) -> None:
        plan = self.prepare()
        with self.direct_resources() as resources:

            def mutation() -> None:
                self.receipt(plan)
                self.assertNotIn("release", resources.events)
                resources.events.append("mutation")

            resources.gap.stop_advertising.side_effect = mutation
            resources.doctor.run.return_value = SimpleNamespace(
                role_handles={"updates": 37}
            )
            fixture = direct_fixture.DirectFixture(
                PORT, plan.profile_path, fixture_session=plan
            )
            self.addCleanup(fixture.stop)
            self.assertEqual(fixture.start(), {"updates": 37})
            original = plan.receipt_path.read_bytes()
            self.assertLess(
                resources.events.index("vendor"), resources.events.index("mutation")
            )
            self.assertEqual(fixture._profile, load_profile(plan.profile_path))
            resources.doctor.run.assert_called_once_with(
                load_profile(plan.profile_path)
            )
            fixture.stop()
            resources.gatt.reset_server.assert_not_called()
            self.assertEqual(
                resources.events[-3:], ["session-close", "serial-close", "release"]
            )
            self.assertEqual(plan.receipt_path.read_bytes(), original)
            self.receipt(plan)

    def test_real_direct_serve_failed_proof_blocks_native_mutation_ready_and_releases(
        self,
    ) -> None:
        cases = (
            (
                replace(self.installed, image_sha256="00" * 32),
                None,
                FixtureAttestationError,
            ),
            (
                replace(self.installed, capabilities=frozenset({"normal-rf"})),
                None,
                FixtureAttestationError,
            ),
            (None, BtpFrame(VENDOR, 0, INDEX_NONE, b"\x02"), BtpCommandError),
        )
        for index, (installed, response, error) in enumerate(cases):
            with self.subTest(case=index):
                plan = self.prepare(f"direct-failure-{index}")
                if installed is not None:
                    response = BtpFrame(
                        VENDOR,
                        READ_IMAGE_ATTESTATION,
                        INDEX_NONE,
                        encode_attestation(installed),
                    )
                with (
                    self.direct_resources(response) as resources,
                    redirect_stdout(io.StringIO()) as output,
                ):
                    with self.assertRaises(error):
                        direct_fixture.serve(
                            PORT, plan.profile_path, fixture_session=plan
                        )
                    self.assertEqual(resources.gap.mock_calls, [])
                    self.assertEqual(resources.gatt.mock_calls, [])
                    resources.doctor.run.assert_not_called()
                    self.assertNotIn("NRF52840_DIRECT_READY", output.getvalue())
                    self.assertEqual(
                        resources.events,
                        [
                            "acquire",
                            "vendor",
                            "session-close",
                            "serial-close",
                            "release",
                        ],
                    )
                self.assertFalse(plan.receipt_path.exists())

    def test_direct_fixture_later_provision_failure_keeps_successful_live_receipt(
        self,
    ) -> None:
        plan = self.prepare()
        with (
            self.direct_resources() as resources,
            redirect_stdout(io.StringIO()) as output,
        ):
            resources.doctor.run.side_effect = BtpCommandError("provision failed")
            with self.assertRaisesRegex(BtpCommandError, "provision failed"):
                direct_fixture.serve(PORT, plan.profile_path, fixture_session=plan)
            self.assertNotIn("NRF52840_DIRECT_READY", output.getvalue())
            self.assertEqual(
                resources.events[-3:], ["session-close", "serial-close", "release"]
            )
            resources.gatt.reset_server.assert_not_called()
            self.receipt(plan)


class LiveSnapshotAtomicPublicationTests(unittest.TestCase):
    """Filesystem-only races; no BTP, USB, firmware or RF owner is created."""

    def test_lstat_can_observe_unlinked_old_inode_before_opening_new_snapshot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.log"
            path.write_bytes(b"old complete publication\n")
            with path.open("rb") as old:
                temporary = path.with_suffix(".next")
                temporary.write_bytes(b"new complete publication\n")
                temporary.replace(path)
                # Model lstat returning the old vnode's metadata after rename has
                # unlinked it. The actual open still resolves the new pathname.
                unlinked = os.fstat(old.fileno())
                self.assertEqual(unlinked.st_nlink, 0)
                self.assertNotEqual(unlinked.st_ino, path.stat().st_ino)
                with (
                    patch.object(Path, "lstat", return_value=unlinked),
                    patch.object(fixture_session.os, "open", wraps=os.open) as opened,
                ):
                    self.assertEqual(
                        fixture_session.read_live_snapshot(path),
                        b"new complete publication\n",
                    )
                    self.assertTrue(opened.call_args.args[1] & os.O_NOFOLLOW)
                    with self.assertRaisesRegex(
                        FixtureSessionError, "session input is linked"
                    ):
                        fixture_session._read_bytes(path)
                    opened.assert_called_once()

    def test_unlinked_lstat_preserves_independent_fd_checks_and_diagnostics(
        self,
    ) -> None:
        for fault in (
            "hardlink",
            "oversized",
            "fifo",
            "symlink",
            "in-place",
            "late-hardlink",
        ):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "runtime.log"
                path.write_bytes(b"old\n")
                with path.open("rb") as old:
                    temporary = path.with_suffix(".next")
                    temporary.write_bytes(b"new\n")
                    temporary.replace(path)
                    unlinked = os.fstat(old.fileno())
                    self.assertEqual(unlinked.st_nlink, 0)
                    if fault == "hardlink":
                        os.link(path, path.with_suffix(".linked"))
                    elif fault == "oversized":
                        with path.open("wb") as stream:
                            stream.truncate(fixture_session.MAX_DOCUMENT_BYTES + 1)
                    elif fault == "fifo":
                        path.unlink()
                        os.mkfifo(path)
                    elif fault == "symlink":
                        target = path.with_suffix(".target")
                        path.replace(target)
                        path.symlink_to(target)
                    actual = path.stat()
                    observed: list[os.stat_result] = []
                    real_fstat = os.fstat

                    def fstat(
                        fd: int,
                        fault: str = fault,
                        path: Path = path,
                        observed: list[os.stat_result] = observed,
                        real_fstat: Callable[[int], os.stat_result] = real_fstat,
                    ) -> os.stat_result:
                        value = real_fstat(fd)
                        observed.append(value)
                        if len(observed) == 1:
                            if fault == "in-place":
                                path.write_bytes(b"edit\n")
                                os.utime(
                                    path,
                                    ns=(
                                        value.st_atime_ns,
                                        value.st_mtime_ns + 1_000_000,
                                    ),
                                )
                            elif fault == "late-hardlink":
                                os.link(path, path.with_suffix(".linked"))
                        return value

                    with (
                        patch.object(Path, "lstat", return_value=unlinked),
                        patch.object(fixture_session.os, "fstat", side_effect=fstat),
                        self.assertRaises(FixtureSessionError) as caught,
                    ):
                        fixture_session.read_live_snapshot(path)
                    message = str(caught.exception)
                    if fault == "symlink":
                        self.assertEqual(
                            observed, []
                        )  # O_NOFOLLOW refuses before fstat.
                    else:
                        self.assertIn(f"mode={actual.st_mode:#o}", message)
                        self.assertIn(f"nlink={actual.st_nlink}", message)
                        self.assertIn(f"size={actual.st_size}", message)
                        self.assertIn(
                            f"cap={fixture_session.MAX_DOCUMENT_BYTES}", message
                        )
                        if fault in ("in-place", "late-hardlink"):
                            self.assertEqual(len(observed), 2)
                            self.assertIn(f"nlink={observed[1].st_nlink}", message)
                            self.assertIn(f"size={observed[1].st_size}", message)
                            self.assertIn("opened(mode=", message)
                            self.assertIn("finished(mode=", message)
                        else:
                            self.assertEqual(len(observed), 1)
                            self.assertIn("invalid opened live snapshot", message)

    def test_preopen_rejection_diagnostics_report_actual_mode_nlink_and_size(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.log"
            path.write_bytes(b"not a unique link\n")
            os.link(path, path.with_suffix(".linked"))
            actual = path.lstat()
            with (
                patch.object(fixture_session.os, "open") as opened,
                self.assertRaises(FixtureSessionError) as caught,
            ):
                fixture_session.read_live_snapshot(path)
            opened.assert_not_called()
            for expected in (
                f"mode={actual.st_mode:#o}",
                f"nlink={actual.st_nlink}",
                f"size={actual.st_size}",
            ):
                self.assertIn(expected, str(caught.exception))

    def test_concurrent_atomic_publication_returns_only_complete_bounded_snapshots(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.log"
            publications = (b"A" * 10884 + b"\n", b"B" * 10884 + b"\n")
            path.write_bytes(publications[0])
            start, stop = threading.Event(), threading.Event()
            errors: list[OSError] = []
            published = 0
            stats: dict[int, int] = {}
            real_lstat = Path.lstat

            def lstat(target: Path) -> os.stat_result:
                result = real_lstat(target)
                if target == path:
                    stats[result.st_nlink] = stats.get(result.st_nlink, 0) + 1
                return result

            def publish() -> None:
                nonlocal published
                try:
                    if not start.wait(5):
                        raise TimeoutError("snapshot reader did not start")
                    temporary = path.with_suffix(".next")
                    for index in range(10000):
                        if stop.is_set():
                            break
                        temporary.write_bytes(publications[index % 2])
                        temporary.replace(path)
                        published += 1
                except OSError as error:
                    errors.append(error)

            writer = threading.Thread(
                target=publish, name="atomic-snapshot-test", daemon=True
            )
            writer.start()
            start.set()
            try:
                with patch.object(Path, "lstat", lstat):
                    for _ in range(5000):
                        try:
                            self.assertIn(
                                fixture_session.read_live_snapshot(path), publications
                            )
                        except FixtureSessionError as error:
                            self.fail(
                                f"atomic publisher rejected: {error}; lstat_nlinks={stats}; publications={published}"
                            )
            finally:
                stop.set()
                writer.join(timeout=5)
            self.assertFalse(writer.is_alive(), "bounded atomic publisher did not stop")
            self.assertEqual(errors, [])
            self.assertGreater(published, 0)
            self.assertEqual(sum(stats.values()), 5000)


def load_attestation_from_receipt(plan: FixtureSession) -> InstalledImageAttestation:
    """Exercise the public identity loader on the receipt's actual identity bytes."""
    document = json.loads(plan.receipt_path.read_bytes())["installed_identity"]
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "installed.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return load_attestation(path)


if __name__ == "__main__":
    unittest.main()
