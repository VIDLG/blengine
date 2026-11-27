"""Scene targets derived from the fixture profile, never from a scene constant.

Every managed scene has to name the characteristic it acts on and, where it writes, the
payload it sends. Both are facts of the profile the host already leased: a scene that
declared its own UUID, mode or byte would be asserting something the fixture may refuse,
and a host that could not prove which role it drove.

Nothing here touches a device, a driver or an evidence bundle. The drivers consume these
derivations; the profile stays the only source.
"""

from __future__ import annotations

from tests.peripherals.nrf52840 import fixture_session
from tests.peripherals.nrf52840.fixture_session import FixtureSession
from tests.peripherals.nrf52840.profile import PROPERTY_BITS


def subscription_target(
    plan: FixtureSession, characteristic_role: str
) -> tuple[str, str, str]:
    """Derive one subscription target from the fixture profile alone.

    The scene never hardcodes a characteristic UUID, a mode or a payload: the
    profile is the only source. The App requests exactly the mode a characteristic
    advertises, so the mode this scene must see follows those same properties.
    """
    profile = fixture_session.load_session_profile(plan.contract, plan.profile_path)
    characteristic = next(
        (item for item in profile.characteristics if item.role == characteristic_role),
        None,
    )
    if characteristic is None:
        raise ValueError(f"fixture profile has no {characteristic_role!r} role")
    if characteristic.properties & PROPERTY_BITS["notify"]:
        mode = "notification"
    elif characteristic.properties & PROPERTY_BITS["indicate"]:
        mode = "indication"
    else:
        raise ValueError(
            f"fixture {characteristic_role!r} characteristic is neither notifiable "
            "nor indicatable"
        )
    if not profile.notification_value:
        raise ValueError("fixture profile has no notification value to emit")
    return characteristic.uuid, mode, profile.notification_value.hex()


def read_write_target(
    plan: FixtureSession, characteristic_role: str
) -> tuple[str, bytes]:
    """Derive the read/write target and its provisioned value from the profile alone.

    The scene never names a characteristic: the profile is the only source, and a role that
    is not both readable and writable is a profile defect rather than something the scene
    works around.

    The provisioned value is returned for its *length*, not as an expected reading. The
    tester stores an attribute value of one length and rejects a longer write with
    `invalid attribute length`, so the fixture — not the scene — decides how many bytes a
    legal write has. What the peer reports when read stays an observation.
    """
    profile = fixture_session.load_session_profile(plan.contract, plan.profile_path)
    characteristic = next(
        (item for item in profile.characteristics if item.role == characteristic_role),
        None,
    )
    if characteristic is None:
        raise ValueError(f"fixture profile has no {characteristic_role!r} role")
    if not characteristic.properties & PROPERTY_BITS["read"]:
        raise ValueError(
            f"fixture {characteristic_role!r} characteristic is not readable"
        )
    if not characteristic.properties & (
        PROPERTY_BITS["write"] | PROPERTY_BITS["write_without_response"]
    ):
        raise ValueError(
            f"fixture {characteristic_role!r} characteristic is not writable"
        )
    if not characteristic.initial_value:
        raise ValueError(
            f"fixture {characteristic_role!r} characteristic provisions no value length"
        )
    return characteristic.uuid, characteristic.initial_value


# Printable ASCII the App's write input can carry, in preference order. The payload byte is
# chosen from this set so the value written is never the value the peer already holds.
_WRITE_BYTES = b"789abcdef"


def _io_write_payload(initial_value: bytes) -> bytes:
    """A legal, printable payload of exactly the length this fixture provisions.

    The length is a fixture fact: the tester rejects a write longer than the value it
    currently holds, and a scene that chose its own length would be guessing. The byte is
    chosen to differ from the peer's provisioned value so the read-back proves that the
    write reached the peer's store rather than echoing it.
    """
    if not initial_value:
        raise ValueError("fixture provisions no value to write over")
    if len(initial_value) > 16:
        raise ValueError(
            f"fixture provisions {len(initial_value)} bytes; this scene's declared write "
            "input is bounded to 16 bytes"
        )
    payload = bytes([_WRITE_BYTES[0]]) * len(initial_value)
    if payload != initial_value:
        return payload
    for byte in _WRITE_BYTES[1:]:
        candidate = bytes([byte]) * len(initial_value)
        if candidate != initial_value:
            return candidate
    raise ValueError("fixture value leaves no printable payload that differs from it")


def io_target(plan: FixtureSession, characteristic_role: str) -> tuple[str, bytes]:
    """The read/write target and the exact payload a scene writes to it.

    The characteristic is named by the profile, the write length is what the fixture
    provisions, and the bytes differ from the provisioned value so a read-back can prove the
    peer stored the write. A lane that repeated any part of this would be inventing a
    payload the fixture may refuse.
    """
    characteristic_uuid, provisioned_value = read_write_target(
        plan, characteristic_role
    )
    return characteristic_uuid, _io_write_payload(provisioned_value)


__all__ = ["io_target", "read_write_target", "subscription_target"]
