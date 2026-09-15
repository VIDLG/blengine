from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Any

PROFILE_ID = "blehub-qemu-basic-gatt-v1"
DEFAULT_CONTROL_REQUEST_PATH = Path("/run/blehub-peripheral/control-request.json")
DEFAULT_CONTROL_RESPONSE_PATH = Path("/run/blehub-peripheral/control-response.json")
KNOWN_PROPERTIES = {
    "read",
    "write",
    "write_without_response",
    "notify",
    "indicate",
}


def require_keys(document: dict[str, Any], expected: set[str], context: str) -> None:
    missing = expected - document.keys()
    unknown = document.keys() - expected
    if missing:
        raise ValueError(f"{context} is missing fields: {sorted(missing)}")
    if unknown:
        raise ValueError(f"{context} has unknown fields: {sorted(unknown)}")


def canonical_uuid(value: Any, context: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{context} must be a UUID string")
    try:
        parsed = str(uuid.UUID(value))
    except ValueError as error:
        raise ValueError(f"{context} is not a valid UUID: {value!r}") from error
    if parsed != value:
        raise ValueError(f"{context} must be lowercase canonical UUID text")
    return parsed


def initial_value(characteristic: dict[str, Any], context: str) -> bytearray:
    value = characteristic.get("initial_value_hex")
    if value is None:
        return bytearray()
    if not isinstance(value, str):
        raise TypeError(f"{context}.initial_value_hex must be a string")
    try:
        return bytearray.fromhex(value)
    except ValueError as error:
        raise ValueError(f"{context}.initial_value_hex is not valid hex") from error


def updates_target(scenario: dict[str, Any]) -> tuple[str, str]:
    for service in scenario["services"]:
        for characteristic in service["characteristics"]:
            if characteristic["role"] == "updates":
                return service["uuid"], characteristic["uuid"]
    raise ValueError("scenario must contain a characteristic with role 'updates'")


def strict_control_value(value: Any) -> bytearray:
    if not isinstance(value, str):
        raise TypeError("control.value_hex must be a string")
    try:
        parsed = bytearray.fromhex(value)
    except ValueError as error:
        raise ValueError("control.value_hex is not valid hex") from error
    if len(value) != len(parsed) * 2 or parsed.hex() != value.lower():
        raise ValueError("control.value_hex must contain only pairs of hex digits")
    return parsed


def strict_burst_id(value: Any) -> bytes:
    if not isinstance(value, str):
        raise TypeError("control.burst_id must be a string")
    try:
        parsed = bytes.fromhex(value)
    except ValueError as error:
        raise ValueError("control.burst_id must be valid hex") from error
    if len(value) != 16 or len(parsed) != 8 or parsed.hex() != value:
        raise ValueError("control.burst_id must be 16 lowercase hex digits")
    return parsed


def strict_burst_count(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= 10_000:
        raise ValueError("control.count must be an integer from 1 through 10000")
    return value


def strict_payload_bytes(value: Any) -> int:
    if type(value) is not int or not 20 <= value <= 512:
        raise ValueError("control.payload_bytes must be an integer from 20 through 512")
    return value


def strict_burst_interval_us(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= 1_000_000:
        raise ValueError(
            "control.interval_us must be an integer from 0 through 1000000"
        )
    return value


def notification_burst_payload(
    burst_id: bytes, sequence: int, payload_bytes: int
) -> bytearray:
    payload = bytearray(b"BHN1" + burst_id + sequence.to_bytes(8, "little"))
    payload.extend(
        burst_id[index % len(burst_id)] ^ ((index * 17) & 0xFF)
        for index in range(len(payload), payload_bytes)
    )
    return payload


class WriteBatchLedger:
    def __init__(self) -> None:
        self.batch_id: bytes | None = None
        self.expected_count = 0
        self.seen_sequences: set[int] = set()
        self.next_sequence = 0
        self.received_count = 0
        self.duplicate_count = 0
        self.out_of_order_count = 0
        self.out_of_range_count = 0
        self.foreign_batch_count = 0
        self.malformed_count = 0
        self.first_callback_ns: int | None = None
        self.last_callback_ns: int | None = None

    def start(self, batch_id: bytes, expected_count: int) -> None:
        self.batch_id = batch_id
        self.expected_count = expected_count
        self.seen_sequences.clear()
        self.next_sequence = 0
        self.received_count = 0
        self.duplicate_count = 0
        self.out_of_order_count = 0
        self.out_of_range_count = 0
        self.foreign_batch_count = 0
        self.malformed_count = 0
        self.first_callback_ns = None
        self.last_callback_ns = None

    def record(self, value: bytearray) -> bool:
        raw = bytes(value)
        if not raw.startswith(b"BHW1"):
            return False
        if self.batch_id is None:
            return True
        if not 20 <= len(raw) <= 512:
            self.malformed_count += 1
            return True
        if raw[4:12] != self.batch_id:
            self.foreign_batch_count += 1
            return True

        now = time.perf_counter_ns()
        if self.first_callback_ns is None:
            self.first_callback_ns = now
        self.last_callback_ns = now
        self.received_count += 1
        sequence = int.from_bytes(raw[12:20], "little")
        if sequence >= self.expected_count:
            self.out_of_range_count += 1
        elif sequence in self.seen_sequences:
            self.duplicate_count += 1
        else:
            self.seen_sequences.add(sequence)
            if sequence != self.next_sequence:
                self.out_of_order_count += 1
            while self.next_sequence in self.seen_sequences:
                self.next_sequence += 1
        return True

    def snapshot(self, batch_id: bytes) -> dict[str, int]:
        if self.batch_id is None:
            raise RuntimeError("no write batch is active")
        if batch_id != self.batch_id:
            raise RuntimeError("write batch ID does not match")
        callback_span_ns = 0
        if self.first_callback_ns is not None and self.last_callback_ns is not None:
            callback_span_ns = self.last_callback_ns - self.first_callback_ns
        unique_count = len(self.seen_sequences)
        return {
            "expected_count": self.expected_count,
            "received_count": self.received_count,
            "unique_count": unique_count,
            "missing_count": self.expected_count - unique_count,
            "duplicate_count": self.duplicate_count,
            "out_of_order_count": self.out_of_order_count,
            "out_of_range_count": self.out_of_range_count,
            "foreign_batch_count": self.foreign_batch_count,
            "malformed_count": self.malformed_count,
            "callback_span_ns": callback_span_ns,
        }


def load_scenario(
    path: Path,
    expected_profile_id: str = PROFILE_ID,
) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise TypeError("scenario root must be an object")
    require_keys(
        document,
        {"schema_version", "profile_id", "advertising", "services"},
        "scenario",
    )
    if document["schema_version"] != 1:
        raise ValueError("scenario.schema_version must be 1")
    if document["profile_id"] != expected_profile_id:
        raise ValueError(
            f"scenario.profile_id must be {expected_profile_id!r}"
        )

    advertising = document["advertising"]
    if not isinstance(advertising, dict):
        raise TypeError("scenario.advertising must be an object")
    require_keys(
        advertising,
        {"local_name", "require_local_name", "service_uuids"},
        "scenario.advertising",
    )
    if not isinstance(advertising["local_name"], str):
        raise TypeError("scenario.advertising.local_name must be a string")
    if not advertising["local_name"]:
        raise ValueError("scenario.advertising.local_name must not be empty")
    if not isinstance(advertising["require_local_name"], bool):
        raise TypeError("scenario.advertising.require_local_name must be boolean")
    advertised_uuids = advertising["service_uuids"]
    if not isinstance(advertised_uuids, list):
        raise TypeError("scenario.advertising.service_uuids must be an array")
    advertised_uuids = [
        canonical_uuid(value, f"scenario.advertising.service_uuids[{index}]")
        for index, value in enumerate(advertised_uuids)
    ]

    services = document["services"]
    if not isinstance(services, list):
        raise TypeError("scenario.services must be an array")
    if not services:
        raise ValueError("scenario.services must not be empty")
    service_uuids: set[str] = set()
    advertised_services: list[str] = []
    characteristic_uuids: set[str] = set()
    roles: set[str] = set()
    for service_index, service in enumerate(services):
        context = f"scenario.services[{service_index}]"
        if not isinstance(service, dict):
            raise TypeError(f"{context} must be an object")
        require_keys(
            service, {"uuid", "primary", "advertise", "characteristics"}, context
        )
        service_uuid = canonical_uuid(service["uuid"], f"{context}.uuid")
        if service_uuid in service_uuids:
            raise ValueError(f"{context}.uuid is duplicated")
        service_uuids.add(service_uuid)
        if service["primary"] is not True:
            raise ValueError(f"{context}.primary must be true for the BLESS fixture")
        if not isinstance(service["advertise"], bool):
            raise TypeError(f"{context}.advertise must be boolean")
        if service["advertise"]:
            advertised_services.append(service_uuid)

        characteristics = service["characteristics"]
        if not isinstance(characteristics, list):
            raise TypeError(f"{context}.characteristics must be an array")
        if not characteristics:
            raise ValueError(f"{context}.characteristics must not be empty")
        for characteristic_index, characteristic in enumerate(characteristics):
            char_context = f"{context}.characteristics[{characteristic_index}]"
            if not isinstance(characteristic, dict):
                raise TypeError(f"{char_context} must be an object")
            allowed = {"role", "uuid", "properties", "initial_value_hex"}
            required = {"role", "uuid", "properties"}
            missing = required - characteristic.keys()
            unknown = characteristic.keys() - allowed
            if missing:
                raise ValueError(f"{char_context} is missing fields: {sorted(missing)}")
            if unknown:
                raise ValueError(
                    f"{char_context} has unknown fields: {sorted(unknown)}"
                )
            role = characteristic["role"]
            if not isinstance(role, str):
                raise TypeError(f"{char_context}.role must be a string")
            if not role:
                raise ValueError(f"{char_context}.role must not be empty")
            if role in roles:
                raise ValueError(f"{char_context}.role is duplicated")
            roles.add(role)
            char_uuid = canonical_uuid(characteristic["uuid"], f"{char_context}.uuid")
            if char_uuid in characteristic_uuids:
                raise ValueError(f"{char_context}.uuid is duplicated")
            characteristic_uuids.add(char_uuid)
            properties = characteristic["properties"]
            if not isinstance(properties, list):
                raise TypeError(f"{char_context}.properties must be an array")
            if not properties:
                raise ValueError(f"{char_context}.properties must not be empty")
            if not all(isinstance(value, str) for value in properties):
                raise TypeError(f"{char_context}.properties values must be strings")
            if len(set(properties)) != len(properties):
                raise ValueError(f"{char_context}.properties contains duplicates")
            unknown_properties = set(properties) - KNOWN_PROPERTIES
            if unknown_properties:
                raise ValueError(
                    f"{char_context}.properties contains unsupported values: "
                    f"{sorted(unknown_properties)}"
                )
            initial_value(characteristic, char_context)

    if advertised_uuids != advertised_services:
        raise ValueError(
            "scenario.advertising.service_uuids must exactly match services marked advertise"
        )
    updates_target(document)
    return document


def emit(event: str, **fields: Any) -> None:
    print(
        json.dumps({"event": event, **fields}, separators=(",", ":"), sort_keys=True),
        flush=True,
    )


def write_status(path: Path, **fields: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(fields, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def parse_control_request(document: Any, expected_run_id: str) -> tuple[str, bytearray]:
    if not isinstance(document, dict):
        raise TypeError("control request root must be an object")
    require_keys(
        document,
        {"schema_version", "control_id", "run_id", "value_hex"},
        "control request",
    )
    if document["schema_version"] != 1:
        raise ValueError("control.schema_version must be 1")
    control_id = document["control_id"]
    if not isinstance(control_id, str) or not control_id:
        raise ValueError("control.control_id must be a non-empty string")
    request_run_id = document["run_id"]
    if not isinstance(request_run_id, str) or not request_run_id:
        raise ValueError("control.run_id must be a non-empty string")
    if request_run_id != expected_run_id:
        raise ValueError("control.run_id does not match the active fixture run")
    return control_id, strict_control_value(document["value_hex"])


def parse_write_batch_request(
    document: Any, expected_run_id: str
) -> tuple[str, str, str, bytes, int | None]:
    if not isinstance(document, dict):
        raise TypeError("control request root must be an object")
    operation = document.get("operation")
    if operation == "write_batch_start":
        expected_keys = {
            "schema_version",
            "operation",
            "control_id",
            "run_id",
            "batch_id",
            "count",
        }
    elif operation == "write_batch_status":
        expected_keys = {
            "schema_version",
            "operation",
            "control_id",
            "run_id",
            "batch_id",
        }
    else:
        raise ValueError("control.operation must be a write batch operation")
    require_keys(document, expected_keys, "control request")
    if document["schema_version"] != 1:
        raise ValueError("control.schema_version must be 1")
    control_id = document["control_id"]
    if not isinstance(control_id, str) or not control_id:
        raise ValueError("control.control_id must be a non-empty string")
    request_run_id = document["run_id"]
    if not isinstance(request_run_id, str) or not request_run_id:
        raise ValueError("control.run_id must be a non-empty string")
    if request_run_id != expected_run_id:
        raise ValueError("control.run_id does not match the active fixture run")
    batch_id_text = document["batch_id"]
    batch_id = strict_burst_id(batch_id_text)
    count = strict_burst_count(document["count"]) if operation == "write_batch_start" else None
    return control_id, operation, batch_id_text, batch_id, count


def parse_notification_burst_request(
    document: Any, expected_run_id: str
) -> tuple[str, str, bytes, int, int, int]:
    if not isinstance(document, dict):
        raise TypeError("control request root must be an object")
    require_keys(
        document,
        {
            "schema_version",
            "operation",
            "control_id",
            "run_id",
            "burst_id",
            "count",
            "interval_us",
            "payload_bytes",
        },
        "control request",
    )
    if document["schema_version"] != 1:
        raise ValueError("control.schema_version must be 1")
    if document["operation"] != "notification_burst":
        raise ValueError("control.operation must be 'notification_burst'")
    control_id = document["control_id"]
    if not isinstance(control_id, str) or not control_id:
        raise ValueError("control.control_id must be a non-empty string")
    request_run_id = document["run_id"]
    if not isinstance(request_run_id, str) or not request_run_id:
        raise ValueError("control.run_id must be a non-empty string")
    if request_run_id != expected_run_id:
        raise ValueError("control.run_id does not match the active fixture run")
    burst_id_text = document["burst_id"]
    burst_id = strict_burst_id(burst_id_text)
    count = strict_burst_count(document["count"])
    interval_us = strict_burst_interval_us(document["interval_us"])
    payload_bytes = strict_payload_bytes(document["payload_bytes"])
    return control_id, burst_id_text, burst_id, count, interval_us, payload_bytes


def validate_control_protocol() -> None:
    expected_run_id = "validation-run"
    control_id, value = parse_control_request(
        {
            "schema_version": 1,
            "control_id": "validation-control",
            "run_id": expected_run_id,
            "value_hex": "deadbeef",
        },
        expected_run_id,
    )
    if control_id != "validation-control" or value != bytearray.fromhex("deadbeef"):
        raise ValueError("control protocol validation returned unexpected values")
    try:
        parse_control_request(
            {
                "schema_version": 1,
                "control_id": "stale-control",
                "run_id": "stale-run",
                "value_hex": "deadbeef",
            },
            expected_run_id,
        )
    except ValueError as error:
        if "active fixture run" not in str(error):
            raise
    else:
        raise ValueError("control protocol accepted a stale fixture run")

    ledger = WriteBatchLedger()
    ledger.start(bytes.fromhex("0123456789abcdef"), 3)
    for sequence in (0, 1, 2):
        if not ledger.record(
            bytearray(b"BHW1" + bytes.fromhex("0123456789abcdef") + sequence.to_bytes(8, "little"))
        ):
            raise ValueError("write batch ledger rejected a benchmark payload")
    expected_ledger = {
        "expected_count": 3,
        "received_count": 3,
        "unique_count": 3,
        "missing_count": 0,
        "duplicate_count": 0,
        "out_of_order_count": 0,
        "out_of_range_count": 0,
        "foreign_batch_count": 0,
        "malformed_count": 0,
    }
    actual_ledger = ledger.snapshot(bytes.fromhex("0123456789abcdef"))
    if any(actual_ledger[field] != value for field, value in expected_ledger.items()):
        raise ValueError("write batch ledger validation returned unexpected counters")

    ledger.start(bytes.fromhex("0123456789abcdef"), 4)
    for sequence in (0, 2, 2, 1, 5):
        ledger.record(
            bytearray(b"BHW1" + bytes.fromhex("0123456789abcdef") + sequence.to_bytes(8, "little"))
        )
    ledger.record(bytearray(b"BHW1" + bytes.fromhex("fedcba9876543210") + bytes(8)))
    ledger.record(bytearray(b"BHW1-short"))
    violation_snapshot = ledger.snapshot(bytes.fromhex("0123456789abcdef"))
    expected_violations = {
        "received_count": 5,
        "unique_count": 3,
        "missing_count": 1,
        "duplicate_count": 1,
        "out_of_order_count": 1,
        "out_of_range_count": 1,
        "foreign_batch_count": 1,
        "malformed_count": 1,
    }
    if any(
        violation_snapshot[field] != value
        for field, value in expected_violations.items()
    ):
        raise ValueError("write batch ledger did not detect sequence violations")
    if ledger.record(bytearray(b"ordinary-write")):
        raise ValueError("write batch ledger consumed an ordinary payload")

    start_control_id, operation, batch_id_text, batch_id, batch_count = (
        parse_write_batch_request(
            {
                "schema_version": 1,
                "operation": "write_batch_start",
                "control_id": "validation-write-control",
                "run_id": expected_run_id,
                "batch_id": "0123456789abcdef",
                "count": 10_000,
            },
            expected_run_id,
        )
    )
    if (
        start_control_id != "validation-write-control"
        or operation != "write_batch_start"
        or batch_id_text != "0123456789abcdef"
        or batch_id != bytes.fromhex("0123456789abcdef")
        or batch_count != 10_000
    ):
        raise ValueError("write batch control validation returned unexpected values")

    burst_control_id, burst_id_text, burst_id, count, interval_us, payload_bytes = (
        parse_notification_burst_request(
            {
                "schema_version": 1,
                "operation": "notification_burst",
                "control_id": "validation-burst-control",
                "run_id": expected_run_id,
                "burst_id": "0123456789abcdef",
                "count": 10_000,
                "interval_us": 1_000_000,
                "payload_bytes": 512,
            },
            expected_run_id,
        )
    )
    if (
        burst_control_id != "validation-burst-control"
        or burst_id_text != "0123456789abcdef"
        or burst_id != bytes.fromhex("0123456789abcdef")
        or count != 10_000
        or interval_us != 1_000_000
        or payload_bytes != 512
    ):
        raise ValueError("burst control validation returned unexpected values")
    expected_payload = bytearray(b"BHN1" + burst_id + (42).to_bytes(8, "little"))
    if notification_burst_payload(burst_id, 42, 20) != expected_payload:
        raise ValueError("burst payload validation returned unexpected bytes")
    for field, invalid_value in (
        ("burst_id", "0123456789abcdeF"),
        ("count", 0),
        ("count", 10_001),
        ("count", True),
        ("interval_us", -1),
        ("interval_us", 1_000_001),
        ("interval_us", True),
        ("payload_bytes", 19),
        ("payload_bytes", 513),
        ("payload_bytes", True),
    ):
        invalid_request = {
            "schema_version": 1,
            "operation": "notification_burst",
            "control_id": "invalid-burst-control",
            "run_id": expected_run_id,
            "burst_id": "0123456789abcdef",
            "count": 1,
            "interval_us": 0,
            "payload_bytes": 20,
        }
        invalid_request[field] = invalid_value
        try:
            parse_notification_burst_request(invalid_request, expected_run_id)
        except ValueError:
            pass
        else:
            raise ValueError(f"control protocol accepted invalid burst {field}")


def handle_write_batch_control(
    response_path: Path,
    document: dict[str, Any],
    run_id: str,
    ledger: WriteBatchLedger,
) -> None:
    control_id, operation, batch_id_text, batch_id, count = parse_write_batch_request(
        document, run_id
    )
    if operation == "write_batch_start":
        if count is None:
            raise RuntimeError("write batch start has no count")
        ledger.start(batch_id, count)
        write_status(
            response_path,
            schema_version=1,
            state="started",
            control_id=control_id,
            run_id=run_id,
            operation=operation,
            batch_id=batch_id_text,
            expected_count=count,
        )
        return

    snapshot = ledger.snapshot(batch_id)
    write_status(
        response_path,
        schema_version=1,
        state="reported",
        control_id=control_id,
        run_id=run_id,
        operation=operation,
        batch_id=batch_id_text,
        **snapshot,
    )


async def handle_notification_burst_control(
    server: Any,
    response_path: Path,
    document: dict[str, Any],
    run_id: str,
    service_uuid: str,
    characteristic_uuid: str,
) -> None:
    control_id = document.get("control_id")
    burst_id_text = document.get("burst_id")
    count: Any = document.get("count")
    interval_us: Any = document.get("interval_us")
    payload_bytes: Any = document.get("payload_bytes")
    accepted_count = 0
    sequence = 0
    started_ns = time.perf_counter_ns()
    try:
        control_id, burst_id_text, burst_id, count, interval_us, payload_bytes = (
            parse_notification_burst_request(document, run_id)
        )
        characteristic = server.get_characteristic(characteristic_uuid)
        if characteristic is None:
            raise RuntimeError(
                f"BLESS characteristic is unavailable: {characteristic_uuid}"
            )
        for sequence in range(count):
            previous_value = bytearray(characteristic.value)
            characteristic.value = notification_burst_payload(
                burst_id, sequence, payload_bytes
            )
            try:
                updated = server.update_value(service_uuid, characteristic_uuid)
            except Exception:
                characteristic.value = previous_value
                raise
            if updated is not True:
                characteristic.value = previous_value
                raise RuntimeError("BLESS update_value returned false")
            accepted_count += 1
            if accepted_count < count:
                await asyncio.sleep(interval_us / 1_000_000)
        local_elapsed_ns = time.perf_counter_ns() - started_ns
        write_status(
            response_path,
            schema_version=1,
            state="completed",
            control_id=control_id,
            run_id=run_id,
            burst_id=burst_id_text,
            count=count,
            interval_us=interval_us,
            payload_bytes=payload_bytes,
            accepted_count=accepted_count,
            local_elapsed_ns=local_elapsed_ns,
        )
        emit(
            "control_notification_burst",
            control_id=control_id,
            run_id=run_id,
            burst_id=burst_id_text,
            count=count,
            interval_us=interval_us,
            payload_bytes=payload_bytes,
            accepted_count=accepted_count,
            local_elapsed_ns=local_elapsed_ns,
        )
    except Exception as error:  # noqa: BLE001
        local_elapsed_ns = time.perf_counter_ns() - started_ns
        if isinstance(control_id, str) and control_id:
            write_status(
                response_path,
                schema_version=1,
                state="failed",
                control_id=control_id,
                run_id=run_id,
                burst_id=burst_id_text,
                count=count,
                interval_us=interval_us,
                payload_bytes=payload_bytes,
                accepted_count=accepted_count,
                sequence=sequence,
                local_elapsed_ns=local_elapsed_ns,
                error=str(error),
            )
        emit(
            "control_notification_burst_failed",
            control_id=control_id,
            run_id=run_id,
            burst_id=burst_id_text,
            count=count,
            interval_us=interval_us,
            payload_bytes=payload_bytes,
            accepted_count=accepted_count,
            sequence=sequence,
            local_elapsed_ns=local_elapsed_ns,
            error=str(error),
        )


async def handle_control_request(
    server: Any,
    request_path: Path,
    response_path: Path,
    run_id: str,
    service_uuid: str,
    characteristic_uuid: str,
    write_batch_ledger: WriteBatchLedger,
) -> None:
    control_id: str | None = None
    try:
        document = json.loads(request_path.read_text(encoding="utf-8"))
        if isinstance(document, dict):
            candidate_id = document.get("control_id")
            if isinstance(candidate_id, str) and candidate_id:
                control_id = candidate_id
            operation = document.get("operation")
            if operation == "notification_burst":
                await handle_notification_burst_control(
                    server,
                    response_path,
                    document,
                    run_id,
                    service_uuid,
                    characteristic_uuid,
                )
                return
            if operation in {"write_batch_start", "write_batch_status"}:
                handle_write_batch_control(
                    response_path,
                    document,
                    run_id,
                    write_batch_ledger,
                )
                return
        control_id, value = parse_control_request(document, run_id)
        characteristic = server.get_characteristic(characteristic_uuid)
        if characteristic is None:
            raise RuntimeError(
                f"BLESS characteristic is unavailable: {characteristic_uuid}"
            )
        previous_value = bytearray(characteristic.value)
        characteristic.value = value
        try:
            updated = server.update_value(service_uuid, characteristic_uuid)
        except Exception:
            characteristic.value = previous_value
            raise
        if updated is not True:
            characteristic.value = previous_value
            raise RuntimeError("BLESS update_value returned false")
        value_hex = value.hex()
        write_status(
            response_path,
            schema_version=1,
            state="updated",
            control_id=control_id,
            run_id=run_id,
            service_uuid=service_uuid,
            characteristic_uuid=characteristic_uuid,
            value_hex=value_hex,
        )
        emit(
            "control_update",
            control_id=control_id,
            run_id=run_id,
            service_uuid=service_uuid,
            characteristic_uuid=characteristic_uuid,
            value_hex=value_hex,
        )
    except Exception as error:  # noqa: BLE001
        if control_id is not None:
            write_status(
                response_path,
                schema_version=1,
                state="failed",
                control_id=control_id,
                run_id=run_id,
                error=str(error),
            )
        emit(
            "control_update_failed",
            control_id=control_id,
            run_id=run_id,
            error=str(error),
        )


async def serve_controls(
    control_event: asyncio.Event,
    server: Any,
    request_path: Path,
    response_path: Path,
    run_id: str,
    service_uuid: str,
    characteristic_uuid: str,
    write_batch_ledger: WriteBatchLedger,
) -> None:
    while True:
        await control_event.wait()
        control_event.clear()
        await handle_control_request(
            server,
            request_path,
            response_path,
            run_id,
            service_uuid,
            characteristic_uuid,
            write_batch_ledger,
        )


async def serve(args: argparse.Namespace) -> None:
    from bless import (
        BlessServer,
        GATTAttributePermissions,
        GATTCharacteristicProperties,
    )

    scenario = load_scenario(args.scenario, args.expected_profile_id)
    updates_service_uuid, updates_characteristic_uuid = updates_target(scenario)
    run_id = args.run_id or str(uuid.uuid4())
    write_batch_ledger = WriteBatchLedger()
    status_path = args.status_file
    server = BlessServer(
        name=scenario["advertising"]["local_name"],
        loop=asyncio.get_running_loop(),
        adapter=args.adapter,
    )

    property_flags = {
        "read": GATTCharacteristicProperties.read,
        "write": GATTCharacteristicProperties.write,
        "write_without_response": GATTCharacteristicProperties.write_without_response,
        "notify": GATTCharacteristicProperties.notify,
        "indicate": GATTCharacteristicProperties.indicate,
    }
    gatt_tree: dict[str, dict[str, dict[str, Any]]] = {}
    for service in scenario["services"]:
        characteristics: dict[str, dict[str, Any]] = {}
        for characteristic in service["characteristics"]:
            properties = GATTCharacteristicProperties(0)
            permissions = GATTAttributePermissions(0)
            for property_name in characteristic["properties"]:
                properties |= property_flags[property_name]
            if "read" in characteristic["properties"]:
                permissions |= GATTAttributePermissions.readable
            if any(
                value in characteristic["properties"]
                for value in ("write", "write_without_response")
            ):
                permissions |= GATTAttributePermissions.writeable
            characteristics[characteristic["uuid"]] = {
                "Properties": properties,
                "Permissions": permissions,
                "Value": initial_value(characteristic, characteristic["uuid"]),
            }
        gatt_tree[service["uuid"]] = characteristics

    def read_request(characteristic: Any) -> bytearray:
        value = bytearray(characteristic.value)
        emit(
            "read",
            run_id=run_id,
            characteristic_uuid=characteristic.uuid,
            value_hex=value.hex(),
        )
        return value

    def write_request(characteristic: Any, value: bytearray) -> None:
        characteristic.value = value
        if not write_batch_ledger.record(value):
            emit(
                "write",
                run_id=run_id,
                characteristic_uuid=characteristic.uuid,
                value_hex=bytes(value).hex(),
            )

    server.read_request_func = read_request
    server.write_request_func = write_request
    try:
        await asyncio.wait_for(server.add_gatt(gatt_tree), timeout=10)
    except asyncio.TimeoutError as error:
        raise RuntimeError(
            "BLESS CoreBluetooth service registration did not complete within 10 seconds"
        ) from error

    stop_event = asyncio.Event()
    control_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop_event.set)
    loop.add_signal_handler(signal.SIGUSR1, control_event.set)

    control_task: asyncio.Task[None] | None = None
    try:
        await server.start()
        control_task = asyncio.create_task(
            serve_controls(
                control_event,
                server,
                args.control_request_file,
                args.control_response_file,
                run_id,
                updates_service_uuid,
                updates_characteristic_uuid,
                write_batch_ledger,
            )
        )
        write_status(
            status_path,
            schema_version=1,
            state="ready",
            profile_id=scenario["profile_id"],
            run_id=run_id,
            adapter=args.adapter,
            service_uuids=scenario["advertising"]["service_uuids"],
        )
        emit(
            "ready",
            profile_id=scenario["profile_id"],
            run_id=run_id,
            adapter=args.adapter,
            service_uuids=scenario["advertising"]["service_uuids"],
        )
        await stop_event.wait()
    except Exception as error:
        write_status(
            status_path,
            schema_version=1,
            state="failed",
            profile_id=scenario["profile_id"],
            run_id=run_id,
            error=str(error),
        )
        emit("failed", run_id=run_id, error=str(error))
        raise
    finally:
        if control_task is not None:
            control_task.cancel()
            try:
                await control_task
            except asyncio.CancelledError:
                pass
        try:
            await asyncio.wait_for(server.stop(), timeout=10)
        except Exception as error:  # noqa: BLE001
            emit("stop_failed", run_id=run_id, error=str(error))
        else:
            emit("stopped", run_id=run_id)
        status_path.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the managed BLESS fixture")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--scenario", required=True, type=Path)
    validate_parser.add_argument("--expected-profile-id", default=PROFILE_ID)

    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--scenario", required=True, type=Path)
    serve_parser.add_argument("--expected-profile-id", default=PROFILE_ID)
    serve_parser.add_argument(
        "--status-file",
        type=Path,
        default=Path("/run/blehub-peripheral/status.json"),
    )
    serve_parser.add_argument("--run-id")
    serve_parser.add_argument("--adapter")
    serve_parser.add_argument(
        "--control-request-file",
        type=Path,
        default=DEFAULT_CONTROL_REQUEST_PATH,
    )
    serve_parser.add_argument(
        "--control-response-file",
        type=Path,
        default=DEFAULT_CONTROL_RESPONSE_PATH,
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "validate":
            scenario = load_scenario(args.scenario, args.expected_profile_id)
            validate_control_protocol()
            emit("valid", profile_id=scenario["profile_id"])
        else:
            asyncio.run(serve(args))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
