from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.peripherals.nrf52840.gatt_commands import GattAttribute, GattAttributeValue
from tests.peripherals.nrf52840.profile import (
    ProfileError,
    ProfilePlan,
    load_profile,
    provision,
)

PROFILE = (
    Path(__file__).parents[2]
    / "conformance"
    / "scenarios"
    / "blehub-nrf-indication-v2.json"
)
INDICATION_PROFILE = (
    Path(__file__).parents[2]
    / "conformance"
    / "scenarios"
    / "blehub-nrf-indication-v2.json"
)
LIVE_PROFILES = (
    Path(__file__).parents[0] / "firmware" / "profiles" / "multifunction-rf-v2-a.json",
    Path(__file__).parents[0] / "firmware" / "profiles" / "multifunction-rf-v2-b.json",
)


class FakeGatt:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.attached = False
        self.resident = False
        self.handles = {
            "fff10000-0000-4000-8000-000000000000": 0x0025,
            "fff20000-0000-4000-8000-000000000000": 0x0028,
            "fff30000-0000-4000-8000-000000000000": 0x002B,
            "fff40000-0000-4000-8000-000000000000": 0x002E,
            "fff50000-0000-4000-8000-000000000000": 0x0031,
        }

    def add_service(self, service_type: int, service_uuid: str) -> None:
        self.calls.append(("service", (service_type, service_uuid)))

    def add_characteristic(
        self, handle: int, properties: int, permissions: int, characteristic_uuid: str
    ) -> None:
        self.calls.append(
            ("characteristic", (handle, properties, permissions, characteristic_uuid))
        )

    def add_descriptor(
        self, handle: int, permissions: int, descriptor_uuid: str
    ) -> None:
        self.calls.append(("descriptor", (handle, permissions, descriptor_uuid)))

    def set_value(self, handle: int, value: bytes) -> None:
        self.calls.append(("value", (handle, value)))

    def start_server(self) -> None:
        self.calls.append(("start", None))

    def get_attributes(self) -> tuple[object, ...]:
        if not self.attached:
            self.attached = True
            return ()
        return tuple(
            type("Attribute", (), {"uuid": uuid, "handle": handle})()
            for uuid, handle in self.handles.items()
        )


class DirtyGatt(FakeGatt):
    def __init__(self, attributes: tuple[GattAttribute, ...]) -> None:
        super().__init__()
        self.attributes = attributes
        self.service_values = {
            attribute.handle: bytes.fromhex(PROFILE_SERVICE_UUID.replace("-", ""))[::-1]
            for attribute in attributes
            if attribute.uuid == "2800"
        }

    def get_attributes(self) -> tuple[GattAttribute, ...]:
        return self.attributes

    def get_attribute_value(self, handle: int) -> GattAttributeValue:
        return GattAttributeValue(0, self.service_values[handle])


PROFILE_SERVICE_UUID = "fd000000-0000-4000-8000-000000000000"


class ProfileTests(unittest.TestCase):
    def test_loads_canonical_profile_and_maps_roles(self) -> None:
        profile = load_profile(PROFILE)
        self.assertEqual(profile.profile_id, "blehub-nrf52840-indication-v2")
        self.assertEqual(len(profile.characteristics), 5)
        self.assertTrue(profile.characteristics[1].requires_ccc)

        gatt = FakeGatt()
        mapping = provision(gatt, profile)
        self.assertEqual(mapping["read-write"], 0x0025)
        self.assertEqual(sum(call[0] == "descriptor" for call in gatt.calls), 2)
        self.assertEqual(gatt.calls[0][0], "service")
        self.assertEqual(gatt.calls[-1][0], "start")

    def test_profile_projects_recovery_target_from_declared_updates_role(self) -> None:
        profile = load_profile(PROFILE)
        target = profile.recovery_target()

        self.assertEqual(target.service_uuid, profile.service_uuid)
        self.assertEqual(
            target.characteristic_uuid,
            next(
                characteristic.uuid
                for characteristic in profile.characteristics
                if characteristic.role == "updates"
            ),
        )
        self.assertEqual(target.expected_value, profile.notification_value)
        self.assertEqual(target.scan_service_uuids, (profile.service_uuid,))
        self.assertIsNone(target.scan_name_prefix)

    def test_live_v2_profiles_project_distinct_recovery_targets(self) -> None:
        targets = [load_profile(path).recovery_target() for path in LIVE_PROFILES]

        self.assertEqual(
            [target.service_uuid for target in targets],
            [
                "fd600000-0000-4000-8000-000000000000",
                "fd700000-0000-4000-8000-000000000000",
            ],
        )
        self.assertEqual(
            {target.characteristic_uuid for target in targets},
            {"fff20000-0000-4000-8000-000000000000"},
        )
        self.assertEqual(
            [target.expected_value for target in targets], [b"\xef", b"\xef"]
        )

    def test_recovery_target_rejects_missing_updates_role(self) -> None:
        profile = load_profile(PROFILE)
        without_updates = ProfilePlan(
            profile.profile_id,
            profile.local_name,
            profile.service_uuid,
            profile.notification_value,
            tuple(
                characteristic
                for characteristic in profile.characteristics
                if characteristic.role != "updates"
            ),
            profile.require_local_name,
        )

        with self.assertRaisesRegex(ProfileError, "updates characteristic"):
            without_updates.recovery_target()

    def test_indication_profile_is_indication_only(self) -> None:
        profile = load_profile(INDICATION_PROFILE)
        indication = next(
            characteristic
            for characteristic in profile.characteristics
            if characteristic.role == "indications"
        )
        self.assertTrue(indication.requires_ccc)
        self.assertNotEqual(indication.properties & (1 << 5), 0)
        self.assertEqual(indication.properties & (1 << 4), 0)
        self.assertEqual(indication.uuid, "fff50000-0000-4000-8000-000000000000")

    def test_resident_profile_restores_initial_values(self) -> None:
        profile = load_profile(PROFILE)
        gatt = FakeGatt()
        gatt.attached = True
        mapping = provision(gatt, profile)
        self.assertEqual(mapping["read-write"], 0x0025)
        values = [call for call in gatt.calls if call[0] == "value"]
        self.assertEqual(len(values), 5)

    def test_advertising_name_policy_is_strict_bool_and_legacy_plan_compatible(
        self,
    ) -> None:
        source = PROFILE.parent / "blehub-nrf-multifunction-v1-a.json"
        profile = load_profile(source)
        self.assertFalse(profile.require_local_name)
        legacy = ProfilePlan(
            profile.profile_id,
            profile.local_name,
            profile.service_uuid,
            profile.notification_value,
            profile.characteristics,
        )
        self.assertFalse(legacy.require_local_name)
        document = json.loads(source.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            for value in (False, True):
                with self.subTest(valid=value):
                    document["advertising"]["require_local_name"] = value
                    path.write_text(json.dumps(document), encoding="utf-8")
                    self.assertIs(load_profile(path).require_local_name, value)
            for value in (None, 0, 1, "false", "true", [], {}):
                with self.subTest(invalid=value):
                    document["advertising"]["require_local_name"] = value
                    path.write_text(json.dumps(document), encoding="utf-8")
                    with self.assertRaisesRegex(ProfileError, "require_local_name"):
                        load_profile(path)
            del document["advertising"]["require_local_name"]
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ProfileError, "require_local_name"):
                load_profile(path)

    def test_missing_profile_is_rejected(self) -> None:
        with self.assertRaises(ProfileError):
            load_profile(Path("/does/not/exist.json"))

    def test_duplicate_root_service_fails_closed_before_append(self) -> None:
        profile = load_profile(PROFILE)
        gatt = DirtyGatt(
            (
                GattAttribute(39, 0, "2800"),
                GattAttribute(40, 0, "2803"),
                GattAttribute(41, 0, profile.characteristics[0].uuid),
                GattAttribute(49, 0, "2800"),
                GattAttribute(50, 0, "2803"),
                GattAttribute(51, 0, profile.characteristics[0].uuid),
            )
        )
        with self.assertRaisesRegex(ProfileError, "appears 2 times"):
            provision(gatt, profile)
        self.assertEqual(gatt.calls, [])

    def test_incomplete_existing_root_service_fails_closed_before_append(self) -> None:
        profile = load_profile(PROFILE)
        gatt = DirtyGatt(
            (
                GattAttribute(39, 0, "2800"),
                GattAttribute(40, 0, "2803"),
                GattAttribute(41, 0, profile.characteristics[0].uuid),
            )
        )
        with self.assertRaisesRegex(ProfileError, "incomplete characteristic topology"):
            provision(gatt, profile)
        self.assertEqual(gatt.calls, [])


if __name__ == "__main__":
    unittest.main()
