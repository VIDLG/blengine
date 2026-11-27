"""Attempt-bound live image proof, produced by the RF fixture under its device lease.

A receipt is cooperative evidence, not a signed security attestation. Historical
capture files may constrain expectations but never authorize a fresh RF session.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .btp_commands import VendorCommands
from .btp_session import BtpSession
from .fixture_attestation import (
    FixtureAttestationError,
    assert_attestation,
    load_attestation,
    parse_attestation,
)
from .fixture_contract import (
    MULTIFUNCTION_CAPABILITY_VARIANTS,
    FixtureContract,
    load_contract,
    validate_profile_contract,
)
from .profile import (
    PERMISSION_READ,
    PERMISSION_WRITE,
    ProfileError,
    ProfilePlan,
    load_profile,
)
from .resource_lease import DeviceLease, canonical_identity, identity_for_port

SCHEMA_VERSION = 1
PROOF_METHOD = "btp-vendor-live-under-device-lease"
MAX_DOCUMENT_BYTES = 1024 * 1024
REQUEST_NAME = "fixture-request.json"
CONTRACT_NAME = "contract.json"
PROFILE_NAME = "profile.json"
RECEIPT_NAME = "fixture-receipt.json"


class FixtureSessionError(RuntimeError):
    """The immutable session request or current fixture's live proof is invalid."""


@dataclass(frozen=True)
class FixtureSession:
    session_id: str
    root: Path
    contract: FixtureContract
    contract_sha256: str
    profile_sha256: str
    service_uuid: str
    port: str
    resource_identity: str

    @property
    def request_path(self) -> Path:
        return self.root / REQUEST_NAME

    @property
    def profile_path(self) -> Path:
        return self.root / PROFILE_NAME

    @property
    def receipt_path(self) -> Path:
        return self.root / RECEIPT_NAME


def _read_bytes(path: Path) -> bytes:
    """Reject links, special files, oversized input and cooperative replacement."""
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > MAX_DOCUMENT_BYTES
        ):
            raise FixtureSessionError(
                f"session input is linked, non-regular or too large: {path}"
            )
        flags = (
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
        flags |= getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise FixtureSessionError(f"session input changed type: {path}")
            content = stream.read(MAX_DOCUMENT_BYTES + 1)
            finished = os.fstat(stream.fileno())
        after = path.lstat()
    except OSError as error:
        raise FixtureSessionError(
            f"cannot read session input {path}: {error}"
        ) from error
    signatures = {
        (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_nlink,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )
        for value in (before, opened, finished, after)
    }
    if len(signatures) != 1 or len(content) != before.st_size:
        raise FixtureSessionError(f"session input changed while reading: {path}")
    return content


def _json(content: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise FixtureSessionError(f"duplicate session JSON key: {key}")
            result[key] = value
        return result

    try:
        document = json.loads(content, object_pairs_hook=unique)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise FixtureSessionError(f"malformed session JSON: {error}") from error
    if not isinstance(document, dict):
        raise FixtureSessionError("session JSON must be an object")
    return document


def read_live_snapshot(path: Path) -> bytes:
    """Read one bounded regular-file inode from an atomic live publisher.

    A rename may unlink the inode observed by lstat even before it returns, replace
    the path before open, or unlink the opened inode during read. The opened FD is
    independently validated; the path stat need not describe that FD's inode.
    In-place content/metadata changes, hardlinks and special files still fail closed;
    this is NOT an immutable-input reader. Callers retain their cumulative-prefix
    and phase/deadline checks.
    """
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink not in (0, 1)
            or not 0 <= before.st_size <= MAX_DOCUMENT_BYTES
        ):
            raise FixtureSessionError(
                f"live snapshot is linked, non-regular or too large: {path} "
                f"(mode={before.st_mode:#o}, nlink={before.st_nlink}, "
                f"size={before.st_size}, cap={MAX_DOCUMENT_BYTES})"
            )
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise FixtureSessionError("live snapshots require O_NOFOLLOW support")
        flags = os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink not in (0, 1)
                or not 0 <= opened.st_size <= MAX_DOCUMENT_BYTES
            ):
                raise FixtureSessionError(
                    f"invalid opened live snapshot: {path} "
                    f"(mode={opened.st_mode:#o}, nlink={opened.st_nlink}, "
                    f"size={opened.st_size}, cap={MAX_DOCUMENT_BYTES})"
                )
            content = stream.read(MAX_DOCUMENT_BYTES + 1)
            finished = os.fstat(stream.fileno())
    except OSError as error:
        raise FixtureSessionError(
            f"cannot read live snapshot {path}: {error}"
        ) from error

    # An atomic replacement can change ctime/nlink without touching content. No
    # post-read path lookup: it may already name the next complete publication.
    signatures = {
        (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_uid,
            value.st_gid,
            value.st_size,
            value.st_mtime_ns,
        )
        for value in (opened, finished)
    }
    unlinked = opened.st_nlink == 1 and finished.st_nlink == 0
    if (
        not stat.S_ISREG(finished.st_mode)
        or finished.st_nlink not in (0, 1)
        or finished.st_nlink > opened.st_nlink
        or not 0 <= finished.st_size <= MAX_DOCUMENT_BYTES
        or len(content) > MAX_DOCUMENT_BYTES
        or len(content) != opened.st_size
        or len(signatures) != 1
        or not unlinked
        and opened.st_ctime_ns != finished.st_ctime_ns
    ):
        raise FixtureSessionError(
            f"live snapshot changed in place while reading: {path} "
            f"opened(mode={opened.st_mode:#o}, nlink={opened.st_nlink}, "
            f"size={opened.st_size}) "
            f"finished(mode={finished.st_mode:#o}, nlink={finished.st_nlink}, "
            f"size={finished.st_size}) "
            f"bytes_read={len(content)}, cap={MAX_DOCUMENT_BYTES}, "
            f"same_fd_metadata={len(signatures) == 1}"
        )
    return content


def read_live_document(path: Path) -> dict[str, Any]:
    """Read atomic live JSON, retaining the unique-key/object parser and 1 MiB cap."""
    return _json(read_live_snapshot(path))


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _write_snapshot(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(content)
    path.chmod(stat.S_IRUSR)


def load_session_profile(contract: FixtureContract, path: Path) -> ProfilePlan:
    """Authorize v2 fault injection only for the exact multifunction contract.

    Normal reads need no ARM or AUTH_READ. Retired diagnostic roles cannot
    opt a live fixture back into the removed authorization/hold implementation.
    """
    multifunction = contract.fixture_role == "multifunction-rf"
    if multifunction and not any(
        contract.capabilities == frozenset(variant)
        for variant in MULTIFUNCTION_CAPABILITY_VARIANTS
    ):
        raise ProfileError(
            "multifunction-rf requires a reviewed exact v2 firmware capability variant"
        )
    if contract.fixture_role in {"diagnostic-watchdog", "diagnostic-pending-read"}:
        raise ProfileError("retired v1 diagnostic role is not authorized for live use")
    if not multifunction:
        raise ProfileError(
            "live fixture requires an explicit v2 multifunction contract role; historical JSON is evidence-only"
        )
    profile = load_profile(path)
    if multifunction:
        role_label = "multifunction"
        document = _json(_read_bytes(path))
        service = document["services"][0]
        items = service["characteristics"]
        if multifunction and any(
            "auth" in field.casefold() and value is True
            for definition in (document, document["advertising"], service, *items)
            for field, value in definition.items()
        ):
            raise ProfileError("multifunction must not request authorization")
        expected = {
            "read-write": (1, {"read", "write", "write_without_response"}),
            "updates": (2, {"read", "notify", "indicate"}),
            "read-only": (3, {"read"}),
            "write-only": (4, {"write", "write_without_response"}),
            "indications": (5, {"read", "indicate"}),
        }
        if len(items) != 5 or {item["role"] for item in items} != set(expected):
            raise ProfileError(
                f"{role_label} requires the normal five-characteristic topology"
            )
        for item in items:
            number, properties = expected[item["role"]]
            if (
                item["uuid"].casefold()
                != f"fff{number}0000-0000-4000-8000-000000000000"
                or len(item["properties"]) != len(properties)
                or set(item["properties"]) != properties
                or set(item) != {"role", "uuid", "properties", "initial_value_hex"}
            ):
                raise ProfileError(
                    "multifunction requires canonical characteristics without authorization"
                )
        if (
            service.get("primary") is not True
            or service.get("advertise") is not True
            or document["advertising"].get("service_uuids") != [profile.service_uuid]
        ):
            raise ProfileError(
                f"{role_label} must advertise its single primary service"
            )
        if multifunction:
            for characteristic in profile.characteristics:
                expected_value = (
                    b"BleHub" if characteristic.role == "read-only" else b"\x00"
                )
                expected_permissions = (
                    PERMISSION_READ
                    if characteristic.role in {"updates", "indications", "read-only"}
                    else PERMISSION_WRITE
                    if characteristic.role == "write-only"
                    else PERMISSION_READ | PERMISSION_WRITE
                )
                if (
                    characteristic.initial_value != expected_value
                    or characteristic.permissions != expected_permissions
                ):
                    raise ProfileError(
                        "multifunction requires canonical initial values and ordinary read/write permissions"
                    )
            validate_profile_contract(contract, profile)
    return profile


def validate_fixture_inputs(
    contract_path: Path,
    profile_path: Path,
    *,
    service_uuid: str,
    expected_attestation_path: Path | None = None,
) -> FixtureContract:
    """Check declared inputs only; this does not claim any installed-device proof."""
    contract = load_contract(contract_path)
    validate_profile_contract(contract, load_session_profile(contract, profile_path))
    if contract.service_uuid != service_uuid.casefold():
        raise FixtureAttestationError(
            "runner service UUID does not match fixture contract"
        )
    if expected_attestation_path is not None:
        assert_attestation(contract, load_attestation(expected_attestation_path))
    return contract


def prepare_session(
    contract_path: Path,
    profile_path: Path,
    directory: Path,
    *,
    port: str,
    service_uuid: str,
    expected_attestation_path: Path | None = None,
    session_id: str | None = None,
) -> FixtureSession:
    """Freeze inputs and pin USB identity; no serial open, reset or flash occurs.

    An explicit canonical attempt UUID binds a caller's artifact/phone evidence
    to the same physical session; ordinary callers still get a fresh UUID here.
    """
    if session_id is not None:
        if type(session_id) is not str or str(uuid.UUID(session_id)) != session_id:
            raise ValueError("explicit fixture session_id must be a canonical UUID")
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    contract_bytes = _read_bytes(contract_path)
    profile_bytes = _read_bytes(profile_path)
    # Decode duplicates before the existing loaders consume these exact bytes.
    _json(contract_bytes)
    _json(profile_bytes)
    _write_snapshot(directory / CONTRACT_NAME, contract_bytes)
    _write_snapshot(directory / PROFILE_NAME, profile_bytes)
    validate_fixture_inputs(
        directory / CONTRACT_NAME,
        directory / PROFILE_NAME,
        service_uuid=service_uuid,
        expected_attestation_path=expected_attestation_path,
    )
    request = {
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id if session_id is not None else str(uuid.uuid4()),
        "contract_sha256": _digest(contract_bytes),
        "profile_sha256": _digest(profile_bytes),
        "service_uuid": service_uuid.casefold(),
        "port": port,
        "resource_identity": identity_for_port(port),
    }
    _write_snapshot(
        directory / REQUEST_NAME, json.dumps(request, sort_keys=True).encode("utf-8")
    )
    return load_session(directory / REQUEST_NAME)


def load_session(request_path: Path) -> FixtureSession:
    """Load only fixed local snapshot names and verify the complete declared tuple."""
    if request_path.name != REQUEST_NAME or request_path.parent.is_symlink():
        raise FixtureSessionError(
            "fixture request must belong to a private session directory"
        )
    request = _json(_read_bytes(request_path))
    keys = {
        "schema_version",
        "session_id",
        "contract_sha256",
        "profile_sha256",
        "service_uuid",
        "port",
        "resource_identity",
    }
    if (
        set(request) != keys
        or type(request["schema_version"]) is not int
        or request["schema_version"] != SCHEMA_VERSION
    ):
        raise FixtureSessionError("fixture request keys/schema_version are invalid")
    try:
        if str(uuid.UUID(request["session_id"])) != request["session_id"]:
            raise ValueError("noncanonical UUID")
        kind, serial = request["resource_identity"].split(":", 1)
        if canonical_identity(kind, serial) != request["resource_identity"]:
            raise ValueError("noncanonical resource identity")
    except (ValueError, TypeError, AttributeError) as error:
        raise FixtureSessionError(
            f"invalid session or resource identity: {error}"
        ) from error
    if not isinstance(request["port"], str) or not request["port"]:
        raise FixtureSessionError("fixture request needs an explicit USB port")
    if not isinstance(request["service_uuid"], str):
        raise FixtureSessionError("fixture request service UUID must be text")
    root = request_path.parent
    snapshots = {}
    for filename, field in (
        (CONTRACT_NAME, "contract_sha256"),
        (PROFILE_NAME, "profile_sha256"),
    ):
        content = _read_bytes(root / filename)
        snapshots[filename] = content
        _json(content)
        if _digest(content) != request[field]:
            raise FixtureSessionError(f"session snapshot hash mismatch: {filename}")
    contract = validate_fixture_inputs(
        root / CONTRACT_NAME, root / PROFILE_NAME, service_uuid=request["service_uuid"]
    )
    if _json(_read_bytes(request_path)) != request or any(
        _read_bytes(root / filename) != content
        for filename, content in snapshots.items()
    ):
        raise FixtureSessionError("session snapshot changed during validation")
    return FixtureSession(
        request["session_id"],
        root,
        contract,
        request["contract_sha256"],
        request["profile_sha256"],
        request["service_uuid"],
        request["port"],
        request["resource_identity"],
    )


def _publish_receipt(path: Path, document: dict[str, Any]) -> None:
    """Publish atomically without replacing any earlier receipt or a symlink."""
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".receipt-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(
                (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
            )
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(stat.S_IRUSR)
        os.link(temporary, path)
    except OSError as error:
        raise FixtureSessionError(
            f"cannot publish fresh fixture receipt {path}: {error}"
        ) from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def attest_session(
    plan: FixtureSession, btp_session: BtpSession, *, lease: DeviceLease, port: str
) -> None:
    """Read actual installed identity while the RF owner holds its physical lease."""
    if load_session(plan.request_path) != plan:
        raise FixtureSessionError(
            "fixture session request changed before live attestation"
        )
    if plan.port != port or plan.resource_identity != lease.identity:
        raise FixtureSessionError(
            "selected fixture does not match the session's physical identity"
        )
    kind, serial = plan.resource_identity.split(":", 1)
    lease.require(kind, serial)
    if plan.receipt_path.exists() or plan.receipt_path.is_symlink():
        raise FixtureSessionError(
            "a live fixture session cannot reuse an existing receipt"
        )
    getpgid = getattr(os, "getpgid", None)
    if getpgid is None:
        raise FixtureSessionError(
            "live fixture proof requires OS process-group identity support"
        )
    fixture_pgid = getpgid(0)
    installed = VendorCommands(btp_session).read_image_attestation()
    lease.require(kind, serial)
    if load_session(plan.request_path) != plan:
        raise FixtureSessionError(
            "fixture session request changed during live attestation"
        )
    assert_attestation(plan.contract, installed)
    document = {"schema_version": SCHEMA_VERSION, **asdict(installed)}
    document["capabilities"] = sorted(installed.capabilities)
    _publish_receipt(
        plan.receipt_path,
        {
            "schema_version": SCHEMA_VERSION,
            "session_id": plan.session_id,
            "proof_method": PROOF_METHOD,
            "captured_at_utc": datetime.now(UTC).isoformat(),
            "fixture_pid": os.getpid(),
            "fixture_pgid": fixture_pgid,
            "port": plan.port,
            "resource_identity": plan.resource_identity,
            "contract_sha256": plan.contract_sha256,
            "profile_sha256": plan.profile_sha256,
            "installed_identity": document,
        },
    )


def verify_receipt_contents(
    plan: FixtureSession,
    receipt: dict[str, Any],
    *,
    fixture_identity: tuple[int, int],
) -> dict[str, Any]:
    """Validate receipt bytes without filesystem freshness or live-time claims."""
    expected = {
        "schema_version": SCHEMA_VERSION,
        "session_id": plan.session_id,
        "proof_method": PROOF_METHOD,
        "fixture_pid": fixture_identity[0],
        "fixture_pgid": fixture_identity[1],
        "port": plan.port,
        "resource_identity": plan.resource_identity,
        "contract_sha256": plan.contract_sha256,
        "profile_sha256": plan.profile_sha256,
    }
    if set(receipt) != set(expected) | {"captured_at_utc", "installed_identity"}:
        raise FixtureSessionError("fixture receipt keys are invalid")
    if any(
        type(receipt[field]) is not type(value) or receipt[field] != value
        for field, value in expected.items()
    ):
        raise FixtureSessionError(
            "fixture receipt is not bound to this run/process/image request"
        )
    if not isinstance(receipt["captured_at_utc"], str):
        raise FixtureSessionError("fixture receipt capture timestamp must be text")
    try:
        timestamp = datetime.fromisoformat(receipt["captured_at_utc"])
        if timestamp.tzinfo is None:
            raise ValueError("missing timezone")
    except ValueError as error:
        raise FixtureSessionError(
            "fixture receipt capture timestamp is invalid"
        ) from error
    if not isinstance(receipt["installed_identity"], dict):
        raise FixtureSessionError("fixture receipt needs an installed identity object")
    assert_attestation(plan.contract, parse_attestation(receipt["installed_identity"]))
    return receipt


def verify_receipt(
    plan: FixtureSession, *, fixture_identity: tuple[int, int]
) -> dict[str, Any]:
    """Reject historical, foreign-process or mismatched proof before RN starts/passes."""
    if load_session(plan.request_path) != plan:
        raise FixtureSessionError("session snapshot changed after preparation")
    receipt = _json(_read_bytes(plan.receipt_path))
    verify_receipt_contents(plan, receipt, fixture_identity=fixture_identity)
    # Both runner checks may reuse this proof, but it must postdate this request.
    timestamp = datetime.fromisoformat(receipt["captured_at_utc"])
    prepared_at = datetime.fromtimestamp(plan.request_path.stat().st_mtime, UTC)
    if timestamp < prepared_at or timestamp > datetime.now(UTC):
        raise FixtureSessionError(
            "fixture receipt capture timestamp is stale or in the future"
        )
    return receipt
