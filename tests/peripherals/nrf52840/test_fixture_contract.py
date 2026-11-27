"""Contract and installed-image attestation fail-closed tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from .fixture_attestation import (
    FixtureAttestationError,
    InstalledImageAttestation,
    assert_attestation,
    assert_fixture_contract,
    load_attestation,
)
from .fixture_contract import (
    MULTIFUNCTION_CAPABILITIES,
    MULTIFUNCTION_CAPABILITY_VARIANTS,
    NOTIFICATION_OBSERVATION_CAPABILITY,
    FixtureContractError,
    load_contract,
    validate_profile_contract,
)
from .fixture_session import load_session_profile, validate_fixture_inputs
from .historical_profile import (
    load_profile as load_historical_profile,
)
from .profile import load_profile

ROOT = Path(__file__).resolve().parents[3]
CONTRACT = (
    ROOT
    / "tests/conformance/scenarios/fixture-contracts/diagnostic-pending-read-v1.json"
)
PROFILE = ROOT / "tests/conformance/scenarios/blehub-nrf-att-read-hold-v1.json"
MULTIFUNCTION_IMAGE = (
    ROOT / "tests/peripherals/nrf52840/firmware/images/multifunction-rf-v2-b.json"
)
MULTIFUNCTION_PROFILE = (
    ROOT / "tests/peripherals/nrf52840/firmware/profiles/multifunction-rf-v2-b.json"
)


class FixtureContractTests(unittest.TestCase):
    def test_historical_contract_binds_to_its_profile(self) -> None:
        contract = load_contract(CONTRACT)
        validate_profile_contract(
            contract, load_historical_profile(PROFILE, allow_read_authorization=True)
        )
        self.assertEqual(contract.fixture_role, "diagnostic-pending-read")
        self.assertEqual(
            contract.image_sha256,
            "b864ace501523f51eda25cbb7b5538c73cc88ba777544c409c865d2544c872a1",
        )

    def test_historical_normal_images_bind_distinct_profiles_and_reject_cross_selection(
        self,
    ) -> None:
        contracts = []
        profiles = []
        for suffix, profile_suffix in (("a", ""), ("b", "-b")):
            contract = load_contract(
                ROOT
                / f"tests/conformance/scenarios/fixture-contracts/normal-rf-v1-{suffix}.json"
            )
            profile = load_profile(
                ROOT
                / f"tests/conformance/scenarios/blehub-nrf-indication-v2{profile_suffix}.json"
            )
            validate_profile_contract(contract, profile)
            self.assertEqual(contract.fixture_role, "normal-rf")
            self.assertEqual(
                contract.capabilities,
                frozenset(
                    {
                        "normal-rf",
                        "gatt-reset",
                        "notification",
                        "indication",
                        "passive-disconnect",
                    }
                ),
            )
            declaration = json.loads(
                (
                    ROOT
                    / f"tests/peripherals/nrf52840/firmware/images/normal-rf-v1-{suffix}.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                declaration["settings"], {"CONFIG_BTTESTER_GATT_READ_HOLD": "n"}
            )
            for field in (
                "image_id",
                "image_version",
                "board",
                "profile_id",
                "fixture_role",
            ):
                self.assertEqual(declaration[field], getattr(contract, field))
            self.assertEqual(set(declaration["capabilities"]), contract.capabilities)
            contracts.append(contract)
            profiles.append(profile)
        self.assertNotEqual(contracts[0].image_sha256, contracts[1].image_sha256)
        self.assertNotEqual(contracts[0].service_uuid, contracts[1].service_uuid)
        for index in (0, 1):
            with self.subTest(selected=index):
                with self.assertRaisesRegex(
                    FixtureContractError, "contract/profile mismatch"
                ):
                    validate_profile_contract(contracts[index], profiles[1 - index])
                foreign = contracts[1 - index]
                with self.assertRaisesRegex(
                    FixtureAttestationError, "installed fixture mismatch"
                ):
                    assert_attestation(
                        contracts[index],
                        InstalledImageAttestation(
                            **{
                                field: getattr(foreign, field)
                                for field in (
                                    "image_id",
                                    "image_version",
                                    "image_sha256",
                                    "board",
                                    "profile_id",
                                    "service_uuid",
                                    "local_name",
                                    "capabilities",
                                )
                            }
                        ),
                    )

    def test_v2_exports_match_declarations_profiles_and_never_authorize_v1(
        self,
    ) -> None:
        profiles = []
        for variant, prefix in (("a", "fd600000"), ("b", "fd700000")):
            root = ROOT / "tests/peripherals/nrf52840/firmware"
            declaration = json.loads(
                (root / f"images/multifunction-rf-v2-{variant}.json").read_bytes()
            )
            profile = load_profile(
                root / f"profiles/multifunction-rf-v2-{variant}.json"
            )
            self.assertEqual(
                declaration["image_id"], f"nrf-multifunction-rf-v2-{variant}"
            )
            self.assertEqual(declaration["image_version"], "2")
            self.assertEqual(declaration["profile_id"], profile.profile_id)
            self.assertEqual(declaration["fixture_role"], "multifunction-rf")
            self.assertIn(
                tuple(declaration["capabilities"]), MULTIFUNCTION_CAPABILITY_VARIANTS
            )
            expected_settings = {
                "CONFIG_BT_TESTING_ATT_FAULT": "y",
                "CONFIG_BTTESTER_ATT_FAULT": "y",
                "CONFIG_BTTESTER_BLEHUB_ATTESTATION": "y",
                "CONFIG_BT_MAX_CONN": "1",
                "CONFIG_BT_EATT": "n",
            }
            if NOTIFICATION_OBSERVATION_CAPABILITY in declaration["capabilities"]:
                expected_settings.update(
                    CONFIG_BTTESTER_NOTIFY_OBSERVATION="y",
                    CONFIG_BT_GATT_NOTIFY_MULTIPLE="y",
                    CONFIG_BT_GATT_NOTIFY_MULTIPLE_FLUSH_MS="1",
                )
            self.assertEqual(declaration["settings"], expected_settings)
            self.assertNotIn("image_sha256", declaration)
            self.assertEqual(
                profile.service_uuid, prefix + "-0000-4000-8000-000000000000"
            )
            self.assertEqual(profile.local_name, "BleHubMultiRfV2DK" + variant.upper())
            self.assertEqual(profile.characteristics[0].permissions, 3)
            self.assertTrue(
                all(not c.permissions & 0x40 for c in profile.characteristics)
            )
            profiles.append(profile)
            contract = load_contract(
                root / "contracts" / f"multifunction-rf-v2-{variant}.json"
            )
            validate_profile_contract(contract, profile)
            for field in (
                "image_id",
                "image_version",
                "profile_id",
                "board",
                "fixture_role",
            ):
                self.assertEqual(getattr(contract, field), declaration[field])
            self.assertEqual(
                contract.capabilities, frozenset(declaration["capabilities"])
            )
            self.assertNotEqual(contract.image_sha256, "0" * 64)
            with (
                tempfile.TemporaryDirectory() as directory,
                self.assertRaisesRegex(
                    FixtureContractError, "missing fixture contract.*build/export"
                ),
            ):
                load_contract(Path(directory) / "missing-export.json")
        self.assertEqual(profiles[0].characteristics, profiles[1].characteristics)
        self.assertNotEqual(profiles[0].service_uuid, profiles[1].service_uuid)
        for variant in ("a", "b"):
            with self.assertRaisesRegex(FixtureContractError, "exactly the eleven"):
                load_contract(
                    ROOT
                    / f"tests/conformance/scenarios/fixture-contracts/multifunction-rf-v1-{variant}.json"
                )

    def test_restored_historical_contracts_never_admit_a_live_session(self) -> None:
        scenarios = ROOT / "tests/conformance/scenarios"
        for contract_name, profile_name in (
            ("normal-rf-v1-a", "blehub-nrf-indication-v2"),
            ("normal-rf-v1-b", "blehub-nrf-indication-v2-b"),
            ("multifunction-rf-v1-a", "blehub-nrf-multifunction-v1-a"),
            ("multifunction-rf-v1-b", "blehub-nrf-multifunction-v1-b"),
            ("watchdog-v1", "blehub-nrf-watchdog-v1"),
            ("diagnostic-pending-read-v1", "blehub-nrf-att-read-hold-v1"),
        ):
            with self.subTest(contract=contract_name):
                contract_path = (
                    scenarios / "fixture-contracts" / (contract_name + ".json")
                )
                profile_path = scenarios / (profile_name + ".json")
                self.assertTrue(contract_path.is_file())
                self.assertTrue(profile_path.is_file())
                service = json.loads(profile_path.read_bytes())["services"][0]["uuid"]
                with self.assertRaisesRegex(
                    ValueError, "explicit v2|eleven|retired v1"
                ):
                    validate_fixture_inputs(
                        contract_path, profile_path, service_uuid=service
                    )

    def test_multifunction_missing_extra_caps_and_identity_drift_fail_closed(
        self,
    ) -> None:
        document = json.loads(MULTIFUNCTION_IMAGE.read_bytes())
        document.pop("settings")
        profile = load_profile(MULTIFUNCTION_PROFILE)
        # Temporary synthetic identity exercises host policy only, never RF proof.
        document.update(
            image_sha256="ab" * 32,
            service_uuid=profile.service_uuid,
            local_name=profile.local_name,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "contract.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            contract = load_contract(path)
            validate_profile_contract(
                contract, load_session_profile(contract, MULTIFUNCTION_PROFILE)
            )
            installed = InstalledImageAttestation(
                **{
                    field: getattr(contract, field)
                    for field in (
                        "image_id",
                        "image_version",
                        "image_sha256",
                        "board",
                        "profile_id",
                        "service_uuid",
                        "local_name",
                        "capabilities",
                    )
                }
            )
            assert_attestation(contract, installed)
            for capability in MULTIFUNCTION_CAPABILITIES:
                with self.subTest(missing=capability):
                    missing = contract.capabilities - {capability}
                    path.write_text(
                        json.dumps({**document, "capabilities": sorted(missing)}),
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(
                        FixtureContractError, "exactly the eleven"
                    ):
                        load_contract(path)
                    with self.assertRaisesRegex(
                        FixtureAttestationError, "missing required capabilities"
                    ):
                        assert_attestation(
                            contract, replace(installed, capabilities=missing)
                        )
            for extra in ("central-cancellation", "watchdog", "diagnostic-btp"):
                with self.subTest(extra=extra):
                    path.write_text(
                        json.dumps(
                            {
                                **document,
                                "capabilities": [*document["capabilities"], extra],
                            }
                        ),
                        encoding="utf-8",
                    )
                    with self.assertRaises(FixtureContractError):
                        load_contract(path)
            for field in (
                "image_id",
                "image_version",
                "image_sha256",
                "board",
                "profile_id",
                "service_uuid",
                "local_name",
            ):
                with (
                    self.subTest(identity=field),
                    self.assertRaisesRegex(FixtureAttestationError, field),
                ):
                    assert_attestation(
                        contract,
                        replace(
                            installed,
                            **{
                                field: "0" * 64
                                if field == "image_sha256"
                                else "foreign-identity"
                            },
                        ),
                    )
            for normal in ("", "-b"):
                with (
                    self.subTest(normal=normal),
                    self.assertRaises(FixtureContractError),
                ):
                    validate_profile_contract(
                        contract,
                        load_profile(
                            ROOT
                            / f"tests/conformance/scenarios/blehub-nrf-indication-v2{normal}.json"
                        ),
                    )

    def test_observer_contract_is_exact_optional_last_and_requires_live_capability(
        self,
    ) -> None:
        firmware = ROOT / "tests/peripherals/nrf52840/firmware"
        for slot in ("a", "b"):
            profile = firmware / f"profiles/multifunction-rf-v2-{slot}.json"
            document = json.loads(
                (firmware / f"contracts/multifunction-rf-v2-{slot}.json").read_bytes()
            )
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "contract.json"
                # Canonical inputs retain their exact candidate-specific capability variant.
                self.assertEqual(
                    validate_fixture_inputs(
                        firmware / f"contracts/multifunction-rf-v2-{slot}.json",
                        profile,
                        service_uuid=document["service_uuid"],
                    ).capabilities,
                    frozenset(document["capabilities"]),
                )
                path.write_text(
                    json.dumps(
                        {**document, "capabilities": list(MULTIFUNCTION_CAPABILITIES)}
                    ),
                    encoding="utf-8",
                )
                self.assertEqual(
                    validate_fixture_inputs(
                        path, profile, service_uuid=document["service_uuid"]
                    ).capabilities,
                    frozenset(MULTIFUNCTION_CAPABILITIES),
                )
                observed = [
                    *MULTIFUNCTION_CAPABILITIES,
                    NOTIFICATION_OBSERVATION_CAPABILITY,
                ]
                path.write_text(
                    json.dumps({**document, "capabilities": observed}), encoding="utf-8"
                )
                contract = load_contract(path)
                validate_profile_contract(contract, load_profile(profile))
                self.assertEqual(contract.capabilities, frozenset(observed))
                self.assertEqual(len(NOTIFICATION_OBSERVATION_CAPABILITY.encode()), 30)
                installed = InstalledImageAttestation(
                    **{
                        field: getattr(contract, field)
                        for field in (
                            "image_id",
                            "image_version",
                            "image_sha256",
                            "board",
                            "profile_id",
                            "service_uuid",
                            "local_name",
                            "capabilities",
                        )
                    }
                )
                assert_attestation(contract, installed)
                with self.assertRaisesRegex(
                    FixtureAttestationError, "missing required capabilities"
                ):
                    assert_attestation(
                        contract,
                        replace(
                            installed,
                            capabilities=frozenset(MULTIFUNCTION_CAPABILITIES),
                        ),
                    )
                invalid = (
                    [
                        [
                            *MULTIFUNCTION_CAPABILITIES[:index],
                            NOTIFICATION_OBSERVATION_CAPABILITY,
                            *MULTIFUNCTION_CAPABILITIES[index:],
                        ]
                        for index in range(len(MULTIFUNCTION_CAPABILITIES))
                    ]
                    + [
                        [*prefix, extra]
                        for prefix in (list(MULTIFUNCTION_CAPABILITIES), observed)
                        for extra in (
                            "unknown",
                            "notification-tx-observation-v2",
                            "notification-tx-observation-v1x",
                            NOTIFICATION_OBSERVATION_CAPABILITY,
                        )
                        if [*prefix, extra] != observed
                    ]
                    + [
                        list(reversed(MULTIFUNCTION_CAPABILITIES)),
                        list(reversed(observed)),
                    ]
                )
                for capabilities in invalid:
                    with self.subTest(slot=slot, capabilities=capabilities):
                        path.write_text(
                            json.dumps({**document, "capabilities": capabilities}),
                            encoding="utf-8",
                        )
                        with self.assertRaises(FixtureContractError):
                            load_contract(path)

    def test_contract_rejects_missing_or_unknown_keys(self) -> None:
        document = json.loads(CONTRACT.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "contract.json"
            document.pop("image_sha256")
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(FixtureContractError, "keys must be exactly"):
                load_contract(path)

    def test_attestation_must_match_every_identity_field(self) -> None:
        contract_path = (
            ROOT / "tests/conformance/scenarios/fixture-contracts/normal-rf-v1-a.json"
        )
        profile_path = (
            ROOT / "tests/conformance/scenarios/blehub-nrf-indication-v2.json"
        )
        contract = load_contract(contract_path)
        document = {
            "schema_version": 1,
            "image_id": contract.image_id,
            "image_version": contract.image_version,
            "image_sha256": contract.image_sha256,
            "board": contract.board,
            "profile_id": contract.profile_id,
            "service_uuid": contract.service_uuid,
            "local_name": contract.local_name,
            "capabilities": sorted(contract.capabilities),
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "attestation.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            assert_fixture_contract(
                contract_path,
                path,
                profile_path,
                service_uuid=contract.service_uuid,
            )
            with self.assertRaisesRegex(FixtureAttestationError, "service UUID"):
                assert_fixture_contract(
                    contract_path,
                    path,
                    profile_path,
                    service_uuid="fd200000-0000-4000-8000-000000000000",
                )
            document["image_sha256"] = "0" * 64
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(FixtureAttestationError, "image_sha256"):
                assert_fixture_contract(contract_path, path, profile_path)

    def test_missing_capability_and_profile_drift_fail_closed(self) -> None:
        contract = load_contract(CONTRACT)
        document = {
            "schema_version": 1,
            "image_id": contract.image_id,
            "image_version": contract.image_version,
            "image_sha256": contract.image_sha256,
            "board": contract.board,
            "profile_id": contract.profile_id,
            "service_uuid": contract.service_uuid,
            "local_name": contract.local_name,
            "capabilities": ["diagnostic-btp"],
        }
        with self.assertRaises(FixtureAttestationError):
            assert_attestation(contract, load_attestation_from_document(document))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            profile = json.loads(PROFILE.read_text(encoding="utf-8"))
            profile["profile_id"] = "wrong-image-profile"
            path.write_text(json.dumps(profile), encoding="utf-8")
            with self.assertRaises(FixtureContractError):
                validate_profile_contract(
                    contract,
                    load_historical_profile(path, allow_read_authorization=True),
                )


def load_attestation_from_document(
    document: Mapping[str, object],
) -> InstalledImageAttestation:
    """Keep the test focused on the public file parser without a temp-file helper."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "attestation.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return load_attestation(path)


if __name__ == "__main__":
    unittest.main()
