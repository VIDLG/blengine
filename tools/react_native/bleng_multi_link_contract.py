"""The multi-link scene contract, shared by both platform lanes.

The claim, the stage vocabulary, the per-link payloads and the per-peer session identity are
platform-independent: Android drives them through `am instrument` + logcat, iOS through a hosted
XCTest bundle + syslog. Keeping them in one module is what makes "the same scene on two
platforms" a fact rather than an aspiration — a change here is a change to both lanes.

Nothing in this module touches a device, a fixture, a phone or a build.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

CLAIM = "multi-link-isolation"
# The smallest scene this lane can express: one link to disturb and one to survive.
MINIMUM_PEERS = 2
MAXIMUM_PEERS = 4
READ_WRITE_ROLE = "read-write"
UPDATES_ROLE = "updates"
# The marker tag both platform tests emit and both hosts parse. One token, declared once.
MARKER_TAG = "BLEHUB_RN_MULTI"
# Diagnostics-only tag of the iOS scene's opening probe burst. `idevicesyslog` attaches
# asynchronously and can drop the first lines a just-launched process writes; a burst on this tag
# spans that window without ever being a scene stage, because it is not `MARKER_TAG` followed by a
# delimiter. It is deliberately outside `expected_stages`, so absorbing a loss there cannot hide a
# missing claim stage.
PROBE_TAG = f"{MARKER_TAG}_SYNC"
PROBE_COUNT = 8
PROBE_GAP_SECONDS = 0.25
# A soak is a repeat of the same claim, not a second scene: each round is one complete run of
# `expected_stages`, which is why the vocabulary does not grow with the round count.
MAXIMUM_ROUNDS = 10
FAULT_WRITE_REJECT = "write-reject"
FAULT_KINDS = (FAULT_WRITE_REJECT,)


@dataclass(frozen=True)
class Fault:
    """One declared link-level fault: what the host injects, and on which declared peer.

    The fault is part of the scene's declaration, not something the App infers: the host owns
    the peer, so only the host can refuse its write. `peer` is an index into the declaration,
    never a name or a characteristic.
    """

    kind: str
    peer: int

    def __post_init__(self) -> None:
        if self.kind not in FAULT_KINDS:
            raise ValueError(f"fault kind must be one of {FAULT_KINDS}")
        if not 0 <= self.peer < MAXIMUM_PEERS:
            raise ValueError(
                f"fault peer {self.peer} is outside 0..{MAXIMUM_PEERS - 1}"
            )

    def declaration(self) -> str:
        """The test-process form: `<kind>:<peer>`, the same spelling the CLI accepts."""
        return f"{self.kind}:{self.peer}"


def parse_fault(text: str | None) -> Fault | None:
    """Parse a declared fault; `None`, empty and `none` all mean no fault.

    An unknown spelling is refused rather than ignored: a run that asked for a fault and got a
    clean scene would otherwise pass as if it had proven the refusal.
    """
    if text is None or not text.strip() or text.strip() == "none":
        return None
    kind, separator, peer = text.strip().partition(":")
    if not separator or not peer.isdigit():
        raise ValueError(f"fault must be spelled <kind>:<peer>, not {text!r}")
    return Fault(kind=kind, peer=int(peer))


def require_rounds(rounds: int) -> int:
    """Validate a declared round count; repetition is a parameter of the one runner."""
    if type(rounds) is not int or not 1 <= rounds <= MAXIMUM_ROUNDS:
        raise ValueError(f"rounds must be an integer in 1..{MAXIMUM_ROUNDS}")
    return rounds


# The test-process opt-in token: a run that does not declare it must not touch the radio.
OPT_IN_TOKEN = "multi-link"
# One link is disturbed, every other declared peer is a survivor, and the first peer is the one
# the host ends. Those three facts are what the vocabulary below encodes.
DISTURBED_PEER = 0


def expected_stages(peer_count: int, *, fault: Fault | None = None) -> tuple[str, ...]:
    """The ordered stage vocabulary of one multi-link run, derived from the peer count.

    The claim the lane exists for is that disturbing one link changes nothing about the others,
    so every survivor is exercised rather than one hardcoded spare. A declared fault changes
    only its own link's I/O pair: that link must report a refusal and a read-back equal to its
    own pre-write value, while every other link keeps the normal write/read-back pair.
    """
    if not MINIMUM_PEERS <= peer_count <= MAXIMUM_PEERS:
        raise ValueError(
            f"a multi-link scene needs {MINIMUM_PEERS}..{MAXIMUM_PEERS} peers, not {peer_count}"
        )
    stages = ["started", "scan-submitted", "scan-started"]
    stages += [f"advertisement:{index}" for index in range(peer_count)]
    stages += ["scan-stopped"]
    stages += [f"connected:{index}" for index in range(peer_count)]
    stages += [f"gatt:{index}" for index in range(peer_count)]
    for index in range(peer_count):
        if fault is not None and fault.peer == index:
            stages += [f"write-rejected:{index}", f"unchanged:{index}"]
        else:
            stages += [f"written:{index}", f"io:{index}"]
    stages += [f"subscribed:{index}" for index in range(peer_count)]
    stages += [f"value:{index}" for index in range(peer_count)]
    stages += [f"drop-requested:{DISTURBED_PEER}", f"dropped:{DISTURBED_PEER}"]
    for index in range(DISTURBED_PEER + 1, peer_count):
        stages += [f"survivor-requested:{index}", f"survivor:{index}"]
    stages += [
        f"disconnected:{index}" for index in range(DISTURBED_PEER + 1, peer_count)
    ]
    stages += ["done"]
    return tuple(stages)


def write_payload(index: int, provisioned: bytes) -> bytes:
    """A legal-length payload that no link could have produced for another one.

    The tester stores a value of one length and rejects a longer write, so the fixture decides
    the length and this only chooses bytes: distinct per link, and never the provisioned value,
    so a stale read cannot pass as a per-link round trip.
    """
    if not provisioned:
        raise ValueError("fixture read/write characteristic provisions no value")
    if not 0 <= index < MAXIMUM_PEERS:
        raise ValueError(f"peer index {index} is outside 0..{MAXIMUM_PEERS - 1}")
    payload = bytes([0xA0 + index]) + provisioned[1:]
    if payload == provisioned:
        raise ValueError("declared write must differ from the provisioned value")
    return payload


def peer_session_id(run_id: str, index: int) -> str:
    """A per-peer session identity derived from the run, never a second random draw.

    Each declared peer is a different physical board with its own session, so sharing one id
    would make two receipts indistinguishable in the published evidence.
    """
    return str(uuid.uuid5(uuid.UUID(run_id), f"multi-link-peer-{index}"))


def peer_identity(stage: str) -> str:
    """Per-link stage identity: `subscribed:0` and `subscribed:1` are different stages."""
    return stage


PEER_FIELDS = ("port", "contract", "profile", "identity")


def parse_peer(text: str) -> dict[str, str]:
    """Parse one `key=value,key=value,...` peer declaration, refusing any extra key.

    Both platform command lines use this: a declaration is a contract, so it must not have two
    spellings either.
    """
    fields: dict[str, str] = {}
    for item in text.split(","):
        key, separator, value = item.partition("=")
        key = key.strip()
        if not separator or not key or not value.strip():
            raise ValueError(f"peer item {item!r} is not key=value")
        if key not in PEER_FIELDS:
            raise ValueError(
                f"unknown peer field {key!r}; expected {', '.join(PEER_FIELDS)}"
            )
        if key in fields:
            raise ValueError(f"peer field {key!r} declared twice")
        fields[key] = value.strip()
    missing = [key for key in PEER_FIELDS if key not in fields]
    if missing:
        raise ValueError(f"peer is missing {', '.join(missing)}")
    return fields
