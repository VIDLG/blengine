"""Synthetic v2 byte/dispatch regressions; no firmware execution or RF claims."""

from __future__ import annotations

import copy
import struct
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from . import fault_control as wire
from .btp_commands import BtpCommandError
from .btp_protocol import BtpFrame
from .btp_session import BtpSession, FrameTransport

RUN = "12345678-1234-4567-89ab-123456789abc"
BOOT = bytes(range(1, 17))
OWNER = bytes.fromhex("123456781234456789ab123456789abc")
PEER = bytes.fromhex("01aabbccddeeff")
TICKET_BYTES = BOOT + OWNER + struct.pack("<QIIIBH", 42, 1, 2, 3, 1, 0x1234)
COMMON_BYTES = (
    b"\x02"
    + BOOT
    + OWNER
    + struct.pack("<III7sHII", 1, 2, 3, PEER, 0x1234, 10000, 20000)
)
LIMITS_BYTES = (
    b"\x02\x00"
    + BOOT
    + struct.pack(
        "<BBBBBHIIIIB7sIII",
        7,
        0,
        1,
        1,
        20,
        0,
        10000,
        20000,
        2000,
        5000,
        1,
        PEER,
        1,
        2,
        3,
    )
)
COMMON = wire.ArmCommon(BOOT, OWNER, 1, 2, 3, PEER, 0x1234, 10000, 20000)
TICKET = wire.Ticket(BOOT, OWNER, 42, 1, 2, 3, wire.Operation.RESPONSE_WRITE, 0x1234)
WRITE = wire.ArmRequest(wire.Operation.RESPONSE_WRITE, COMMON, b"old", b"new")


def ack(ticket: wire.Ticket = TICKET, phase: wire.Phase = wire.Phase.ARMED) -> bytes:
    return b"\x02\x00" + ticket.encode() + bytes([phase])


def status_bytes(
    *,
    ticket: wire.Ticket = TICKET,
    phase: int = 2,
    winner: int = 0,
    flags: int = 1,
    opcode: int = 0x12,
    derived: int = 0,
    offset: int = 0,
    observed: bytes = b"new",
    before: bytes = b"old",
    after: bytes = b"old",
    counters: bytes = b"\x01\x01\x01\x01\x00\x00",
    remaining: tuple[int, int, int, int] = (0, 19999, 0, 0),
    outcome: int = 0,
    infrastructure: int = 0,
    callback: int = 0,
) -> bytes:
    return (
        b"\x02\x00"
        + ticket.encode()
        + struct.pack(
            "<BBHBBH7sBh",
            phase,
            winner,
            flags,
            opcode,
            derived,
            offset,
            PEER,
            0,
            callback,
        )
        + bytes([len(observed)])
        + observed
        + bytes([len(before)])
        + before
        + bytes([len(after)])
        + after
        + counters
        + struct.pack("<IIII", *remaining)
        + bytes([outcome, infrastructure])
    )


def frame(opcode: wire.Opcode, payload: bytes) -> BtpFrame:
    return BtpFrame(0xFF, opcode, 0xFF, payload)


class FaultWireTests(unittest.TestCase):
    def test_receipt_documents_reject_unknown_fields_ticket_drift_noncanonical_hex_and_trailing_bytes(
        self,
    ) -> None:
        status = wire.decode_status(status_bytes())
        document = wire.status_document(status)
        self.assertEqual(wire.status_from_document(document), status)
        for mutate in (
            lambda d: d.update(protocol_version=True),
            lambda d: d.update(protocol_version=1),
            lambda d: d.update(extra="ignored"),
            lambda d: d["ticket"].update(sequence=43),
            lambda d: d.update(status_wire=d["status_wire"] + "00"),
            lambda d: d.update(status_wire=d["status_wire"].upper()),
        ):
            malformed = copy.deepcopy(document)
            mutate(malformed)
            with (
                self.subTest(malformed=malformed),
                self.assertRaises(wire.FaultWireError),
            ):
                wire.status_from_document(malformed)

    def test_golden_ticket_common_and_all_arm_operations(self) -> None:
        self.assertEqual(TICKET.encode(), TICKET_BYTES)
        self.assertEqual(wire.Ticket.decode(TICKET_BYTES), TICKET)
        self.assertEqual(COMMON.encode(), COMMON_BYTES)
        self.assertEqual(len(TICKET_BYTES), 55)
        self.assertEqual(len(COMMON_BYTES), 62)
        cases = (
            (WRITE, wire.Opcode.ARM_WRITE, COMMON_BYTES + b"\x03old\x03new"),
            (
                wire.ArmRequest(
                    wire.Operation.CCC_WRITE, COMMON, b"\x01\x00", b"\x00\x00"
                ),
                wire.Opcode.ARM_CCC,
                COMMON_BYTES + b"\x01\x00\x00\x00",
            ),
            (
                wire.ArmRequest(wire.Operation.READ, COMMON, b"old"),
                wire.Opcode.ARM_READ,
                COMMON_BYTES + b"\x03old",
            ),
        )
        for request, opcode, expected in cases:
            with self.subTest(opcode=opcode):
                self.assertEqual(request.opcode, opcode)
                self.assertEqual(request.encode(), expected)
                self.assertEqual(wire.ArmRequest.decode(opcode, expected), request)
                for malformed in (
                    expected[:-1],
                    expected + b"\x00",
                    b"\x01" + expected[1:],
                ):
                    with self.assertRaises(wire.FaultWireError):
                        wire.ArmRequest.decode(opcode, malformed)
        maximum = replace(WRITE, before=b"b" * 20, requested=b"r" * 20)
        self.assertEqual(len(maximum.encode()), 104)
        self.assertEqual(len(replace(cases[2][0], before=b"b" * 20).encode()), 83)
        self.assertEqual(len(cases[1][0].encode()), 66)

    def test_arm_budget_exact_boundaries_and_invalid_values(self) -> None:
        for arm, hold in ((100, 100), (10000, 20000)):
            request = replace(
                WRITE, common=replace(COMMON, arm_budget_ms=arm, hold_budget_ms=hold)
            )
            self.assertEqual(
                wire.ArmRequest.decode(request.opcode, request.encode()), request
            )
        for name, values in (
            ("arm_budget_ms", (0, 99, 10001, -1, 2**32, True)),
            ("hold_budget_ms", (99, 20001, 1.5)),
            ("handle", (0, 65536, -1)),
            ("connection_epoch", (0, 2**32)),
            ("bearer_epoch", (0,)),
            ("database_epoch", (0,)),
        ):
            for value in values:
                with (
                    self.subTest(name=name, value=value),
                    self.assertRaises(wire.FaultWireError),
                ):
                    replace(WRITE, common=replace(COMMON, **{name: value})).encode()

    def test_nonce_identity_and_ticket_boundaries(self) -> None:
        self.assertEqual(wire.owner_nonce(RUN), OWNER)
        for value in (
            RUN.upper(),
            RUN.replace("-", ""),
            "{" + RUN + "}",
            "00000000-0000-0000-0000-000000000000",
            "bad",
        ):
            with self.subTest(value=value), self.assertRaises(wire.FaultWireError):
                wire.owner_nonce(value)
        for name, values in (
            ("boot_nonce", (bytes(16), b"short", BOOT + b"x")),
            ("owner_nonce", (bytes(16), b"")),
            ("sequence", (0, 2**64, -1)),
            ("operation", (0, 4, 1)),
            ("handle", (0, 65536)),
            ("connection_epoch", (0,)),
            ("bearer_epoch", (0,)),
            ("database_epoch", (0,)),
        ):
            for value in values:
                with (
                    self.subTest(name=name, value=value),
                    self.assertRaises(wire.FaultWireError),
                ):
                    replace(TICKET, **{name: value}).encode()
        for payload in (
            TICKET_BYTES[:-1],
            TICKET_BYTES + b"x",
            TICKET_BYTES[:52] + b"\x04" + TICKET_BYTES[53:],
        ):
            with self.assertRaises(wire.FaultWireError):
                wire.Ticket.decode(payload)
        self.assertEqual(
            wire.Ticket.decode(replace(TICKET, sequence=2**64 - 1).encode()).sequence,
            2**64 - 1,
        )

    def test_ccc_u16_wire_roundtrip_does_not_enforce_transition_policy(self) -> None:
        ticket = replace(TICKET, operation=wire.Operation.CCC_WRITE)
        for before in (0, 1, 2, 3, 0xFFFF):
            for requested in (0, 1, 2, 3, 0xFFFF):
                with self.subTest(before=before, requested=requested):
                    previous = struct.pack("<H", before)
                    value = struct.pack("<H", requested)
                    request = wire.ArmRequest(
                        wire.Operation.CCC_WRITE, COMMON, previous, value
                    )
                    payload = COMMON_BYTES + previous + value
                    self.assertEqual(request.opcode, wire.Opcode.ARM_CCC)
                    self.assertEqual(request.encode(), payload)
                    self.assertEqual(len(payload), 66)
                    self.assertEqual(
                        wire.ArmRequest.decode(wire.Opcode.ARM_CCC, payload), request
                    )
                    for after in (previous, value):
                        receipt = status_bytes(
                            ticket=ticket, before=previous, observed=value, after=after
                        )
                        status = wire.decode_status(receipt)
                        self.assertEqual(
                            (status.before, status.observed, status.after),
                            (previous, value, after),
                        )
                        self.assertEqual(wire.encode_status(status), receipt)
                        self.assertEqual(
                            wire.status_from_document(wire.status_document(status)),
                            status,
                        )

    def test_value_lengths_fail_closed(self) -> None:
        invalid = (
            replace(WRITE, before=b"a" * 21),
            replace(WRITE, requested=b"a" * 21),
            replace(WRITE, before=b"a", requested=b"ab"),
            replace(WRITE, operation=wire.Operation.READ),
            replace(WRITE, operation=wire.Operation.CCC_WRITE),
            *(replace(WRITE, operation=value) for value in (0, 4, 1)),
        )
        for request in invalid:
            with self.subTest(request=request), self.assertRaises(wire.FaultWireError):
                request.encode()
        ccc = replace(
            WRITE,
            operation=wire.Operation.CCC_WRITE,
            before=b"\x00\x00",
            requested=b"\x01\x00",
        )
        for field in ("before", "requested"):
            for value in (b"", b"\x00", b"\x00" * 3, b"\x00" * 20):
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaises(wire.FaultWireError),
                ):
                    replace(ccc, **{field: value}).encode()
        for body in (b"", b"\x00" * 3, b"\x00" * 5):
            with self.subTest(body=body), self.assertRaises(wire.FaultWireError):
                wire.ArmRequest.decode(wire.Opcode.ARM_CCC, COMMON_BYTES + body)
        with self.assertRaises(wire.FaultWireError):
            wire.ArmRequest.decode(wire.Opcode.ARM_READ, COMMON_BYTES + b"\x03old\x01x")
        for body in (b"\x15" + b"x" * 21, b"\x01a\x02ab", b"\x00\x00x"):
            with self.assertRaises(wire.FaultWireError):
                wire.ArmRequest.decode(wire.Opcode.ARM_WRITE, COMMON_BYTES + body)
        self.assertEqual(
            replace(WRITE, before=b"", requested=b"").encode(),
            COMMON_BYTES + b"\x00\x00",
        )

    def test_limits_exact_layout_reserved_bits_and_absent_peer(self) -> None:
        self.assertEqual(len(LIMITS_BYTES), 61)
        limits = wire.decode_limits(LIMITS_BYTES)
        self.assertEqual(
            (
                limits.boot_nonce,
                limits.peer,
                limits.connection_epoch,
                limits.bearer_epoch,
                limits.database_epoch,
            ),
            (BOOT, PEER, 1, 2, 3),
        )
        self.assertEqual(int(limits.supported_operations), 7)
        absent = LIMITS_BYTES[:41] + bytes(20)
        self.assertFalse(wire.decode_limits(absent).peer_present)
        for offset, value in (
            (18, 8),
            (18, 3),
            (19, 1),
            (20, 2),
            (21, 0),
            (22, 21),
            (23, 2),
            (24, 1),
            (41, 2),
            (42, 4),
        ):
            changed = bytearray(LIMITS_BYTES)
            changed[offset] = value
            with (
                self.subTest(offset=offset, value=value),
                self.assertRaises(wire.FaultWireError),
            ):
                wire.decode_limits(bytes(changed))
        for offset in (25, 29, 33, 37, 49, 53, 57):
            changed = bytearray(LIMITS_BYTES)
            changed[offset : offset + 4] = bytes(4)
            with self.subTest(offset=offset), self.assertRaises(wire.FaultWireError):
                wire.decode_limits(bytes(changed))
        for payload in (
            LIMITS_BYTES[:-1],
            LIMITS_BYTES + b"x",
            LIMITS_BYTES[:41] + b"\x00" + LIMITS_BYTES[42:],
        ):
            with self.assertRaises(wire.FaultWireError):
                wire.decode_limits(payload)

    def test_envelopes_all_results_exact_body_and_enums(self) -> None:
        for decoder, good in (
            (wire.decode_limits, LIMITS_BYTES),
            (wire.decode_ack, ack()),
            (wire.decode_status, status_bytes()),
        ):
            for result in list(wire.Result)[1:]:
                with self.subTest(decoder=decoder, result=result):
                    with self.assertRaises(wire.FaultCommandError) as error:
                        decoder(bytes([2, result]))
                    self.assertIs(error.exception.result, result)
                    with self.assertRaises(wire.FaultWireError):
                        decoder(bytes([2, result, 0]))
            for bad in (
                b"",
                b"\x02",
                b"\x02\x0a",
                b"\x01\x00" + good[2:],
                good[:-1],
                good + b"x",
            ):
                with self.assertRaises(wire.FaultWireError):
                    decoder(bad)
        self.assertEqual(len(ack()), 58)
        with self.assertRaises(wire.FaultWireError):
            wire.decode_ack(ack()[:-1] + b"\x08")

    def test_ticket_commands_and_decision_exact_bytes(self) -> None:
        for opcode, decision in (
            (wire.Opcode.RESOLVE, wire.Decision.COMMIT),
            (wire.Opcode.RESOLVE, wire.Decision.REJECT),
            (wire.Opcode.ABORT, None),
            (wire.Opcode.STATUS, None),
        ):
            expected = b"\x02" + TICKET_BYTES + (bytes([decision]) if decision else b"")
            self.assertEqual(
                wire.encode_ticket_command(opcode, TICKET, decision), expected
            )
            self.assertEqual(
                wire.decode_ticket_command(opcode, expected), (TICKET, decision)
            )
            for bad in (expected[:-1], expected + b"x", b"\x01" + expected[1:]):
                with self.assertRaises(wire.FaultWireError):
                    wire.decode_ticket_command(opcode, bad)
        for decision in (None, 0, 1, 3):
            with self.assertRaises(wire.FaultWireError):
                wire.encode_ticket_command(wire.Opcode.RESOLVE, TICKET, decision)
        for decision in (0, 3):
            with self.assertRaises(wire.FaultWireError):
                wire.decode_ticket_command(
                    wire.Opcode.RESOLVE, b"\x02" + TICKET_BYTES + bytes([decision])
                )

    def test_wrong_command_family_or_untyped_opcode_cannot_encode_an_invalid_request(
        self,
    ) -> None:
        for opcode in (wire.Opcode.ARM_WRITE, wire.Opcode.GET_LIMITS, 0x13, 0x17):
            with self.subTest(opcode=opcode), self.assertRaises(wire.FaultWireError):
                wire.encode_ticket_command(opcode, TICKET)
            with self.subTest(opcode=opcode), self.assertRaises(wire.FaultWireError):
                wire.decode_ticket_command(opcode, b"\x02" + TICKET_BYTES)
        for opcode in (wire.Opcode.STATUS, wire.Opcode.GET_LIMITS, 0x11, 0x17):
            with self.subTest(opcode=opcode), self.assertRaises(wire.FaultWireError):
                wire.ArmRequest.decode(opcode, WRITE.encode())

    def test_status_full_wire_signed_callback_and_bounded_counters(self) -> None:
        payload = status_bytes(
            observed=b"o" * 20,
            before=b"b" * 20,
            after=b"a" * 20,
            callback=-32768,
            counters=bytes([1, 1, 1, 1, 1, 9]),
        )
        self.assertEqual(len(payload), 162)
        status = wire.decode_status(payload)
        self.assertEqual(
            (status.observed, status.before, status.after),
            (b"o" * 20, b"b" * 20, b"a" * 20),
        )
        self.assertEqual(status.callback_result, -32768)
        # Eight queued UART events plus the one currently waiting for UART.
        self.assertEqual(status.refs.evidence_pending, 9)
        for i in range(6):
            counters = bytearray(6)
            counters[i] = 10 if i == 5 else 2
            with self.subTest(counter=i), self.assertRaises(wire.FaultWireError):
                wire.decode_status(status_bytes(counters=bytes(counters)))
        for key in ("observed", "before", "after"):
            with self.subTest(key=key), self.assertRaises(wire.FaultWireError):
                wire.decode_status(status_bytes(**{key: b"x" * 21}))

    def test_status_unknown_enums_reserved_flags_and_target_operation(self) -> None:
        cases = (
            {"phase": 8},
            {"winner": 9},
            {"outcome": 11},
            {"infrastructure": 8},
            {"flags": 1 << 10},
            {"derived": 1},
            {"offset": 1},
            {"opcode": 0x52},
            {"flags": 0},
            {"flags": 1, "opcode": 0x0A},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(wire.FaultWireError):
                wire.decode_status(status_bytes(**kwargs))
        for operation in wire.Operation:
            ticket = replace(TICKET, operation=operation)
            status = wire.decode_status(
                status_bytes(
                    ticket=ticket,
                    opcode=0x0A if operation is wire.Operation.READ else 0x12,
                    observed=b""
                    if operation is wire.Operation.READ
                    else b"\x01\x00"
                    if operation is wire.Operation.CCC_WRITE
                    else b"new",
                    before=b"\x00\x00"
                    if operation is wire.Operation.CCC_WRITE
                    else b"old",
                    after=b"\x00\x00"
                    if operation is wire.Operation.CCC_WRITE
                    else b"old",
                )
            )
            self.assertIs(status.ticket.operation, operation)
        read = replace(TICKET, operation=wire.Operation.READ)
        result = status_bytes(
            ticket=read,
            phase=6,
            winner=1,
            flags=0x31B,
            opcode=0x0A,
            observed=b"",
            counters=bytes(6),
            remaining=(0, 0, 0, 0),
            outcome=8,
        )
        self.assertIs(wire.decode_status(result).outcome, wire.Outcome.NORMAL_NO_COMMIT)
        for kwargs in ({"outcome": 7}, {"flags": 5}, {"observed": b"x"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(wire.FaultWireError):
                wire.decode_status(
                    status_bytes(ticket=read, opcode=0x0A, observed=b"", **kwargs)
                    if "observed" not in kwargs
                    else status_bytes(ticket=read, opcode=0x0A, **kwargs)
                )
        wire.decode_status(status_bytes(flags=0x3FF))

    def test_every_truncation_rejected_and_ccc_snapshot_shape_matches_target(
        self,
    ) -> None:
        decoders = (
            (wire.Ticket.decode, TICKET_BYTES),
            (wire.ArmCommon.decode, COMMON_BYTES),
            (wire.decode_ack, ack()),
            (wire.decode_limits, LIMITS_BYTES),
            (wire.decode_status, status_bytes()),
            (
                lambda payload: wire.ArmRequest.decode(wire.Opcode.ARM_WRITE, payload),
                WRITE.encode(),
            ),
            (
                lambda payload: wire.decode_ticket_command(
                    wire.Opcode.RESOLVE, payload
                ),
                b"\x02" + TICKET_BYTES + b"\x01",
            ),
        )
        for decoder, payload in decoders:
            for length in range(len(payload)):
                with (
                    self.subTest(decoder=decoder, length=length),
                    self.assertRaises(wire.FaultWireError),
                ):
                    decoder(payload[:length])
        ccc = replace(TICKET, operation=wire.Operation.CCC_WRITE)
        for kwargs in (
            {"before": b"x", "observed": b"\x01\x00", "after": b"\x00\x00"},
            {"before": b"\x00\x00", "observed": b"x", "after": b"\x00\x00"},
            {"before": b"\x00\x00", "observed": b"\x01\x00", "after": b"x"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(wire.FaultWireError):
                wire.decode_status(status_bytes(ticket=ccc, **kwargs))

    def test_deadline_snapshots_do_not_imply_renewal_or_phase_progress(self) -> None:
        for phase, index, maximum in (
            (1, 0, 10000),
            (2, 1, 20000),
            (3, 2, 2000),
            (5, 3, 5000),
        ):
            for value in (0, 1, maximum):
                remaining = [0] * 4
                remaining[index] = value
                wire.decode_status(
                    status_bytes(phase=phase, remaining=tuple(remaining))
                )
            remaining[index] = maximum + 1
            with self.assertRaises(wire.FaultWireError):
                wire.decode_status(
                    status_bytes(phase=phase, remaining=tuple(remaining))
                )
            remaining[index] = 1
            with self.assertRaises(wire.FaultWireError):
                wire.decode_status(status_bytes(phase=6, remaining=tuple(remaining)))


class FaultControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = Mock(spec=BtpSession)
        self.controller = wire.FaultController(self.session, RUN)

    def test_dispatch_all_controls_exact_service_opcode_index_and_ticket(self) -> None:
        requests = (
            WRITE,
            replace(
                WRITE,
                operation=wire.Operation.CCC_WRITE,
                before=b"\x00\x00",
                requested=b"\x02\x00",
            ),
            replace(WRITE, operation=wire.Operation.READ, requested=b""),
        )
        self.session.request.return_value = frame(wire.Opcode.GET_LIMITS, LIMITS_BYTES)
        self.controller.get_limits(timeout=0.5)
        self.session.request.assert_called_once_with(
            service=255, opcode=16, index=255, payload=b"\x02", timeout=0.5
        )
        for request in requests:
            ticket = replace(TICKET, operation=request.operation)
            self.session.request.return_value = frame(request.opcode, ack(ticket))
            self.assertEqual(self.controller.arm(request).ticket, ticket)
            self.session.request.assert_called_with(
                service=255,
                opcode=request.opcode,
                index=255,
                payload=request.encode(),
                timeout=1.0,
            )
        self.session.request.return_value = frame(
            wire.Opcode.RESOLVE, ack(phase=wire.Phase.RESOLUTION_QUEUED)
        )
        result = self.controller.resolve(TICKET, wire.Decision.COMMIT)
        self.assertIs(result.phase, wire.Phase.RESOLUTION_QUEUED)
        self.session.request.assert_called_with(
            service=255,
            opcode=19,
            index=255,
            payload=b"\x02" + TICKET_BYTES + b"\x01",
            timeout=1.0,
        )
        self.session.request.return_value = frame(
            wire.Opcode.ABORT, ack(phase=wire.Phase.RETIRING)
        )
        self.controller.abort(TICKET)
        self.session.request.assert_called_with(
            service=255,
            opcode=20,
            index=255,
            payload=b"\x02" + TICKET_BYTES,
            timeout=1.0,
        )
        self.assertEqual(
            self.session.request.call_count, 6
        )  # No hidden STATUS/retries.

    def test_retained_status_after_disconnect_and_firmware_stale_mutations(
        self,
    ) -> None:
        retained = status_bytes(
            phase=6, winner=6, outcome=5, counters=bytes(6), remaining=(0, 0, 0, 0)
        )
        self.session.request.side_effect = [
            frame(wire.Opcode.STATUS, retained),
            frame(wire.Opcode.STATUS, retained),
            frame(wire.Opcode.RESOLVE, b"\x02\x03"),
            frame(wire.Opcode.ABORT, b"\x02\x03"),
            frame(wire.Opcode.STATUS, b"\x02\x03"),
        ]
        for _ in range(2):
            status = self.controller.status(TICKET)
            self.assertIs(status.outcome, wire.Outcome.DISCONNECTED_UNCOMMITTED)
            self.assertEqual(status.refs.conn_refs, 0)
        for operation in (
            lambda: self.controller.resolve(TICKET, wire.Decision.COMMIT),
            lambda: self.controller.abort(TICKET),
            lambda: self.controller.status(TICKET),
        ):
            with self.assertRaises(wire.FaultCommandError) as error:
                operation()
            self.assertIs(error.exception.result, wire.Result.STALE_TICKET)
        self.assertEqual(self.session.request.call_count, 5)

    def test_foreign_owner_invalid_budget_and_timeout_never_dispatch(self) -> None:
        foreign = bytes(range(17, 33))
        for operation in (
            lambda: self.controller.arm(
                replace(WRITE, common=replace(COMMON, owner_nonce=foreign))
            ),
            lambda: self.controller.status(replace(TICKET, owner_nonce=foreign)),
            lambda: self.controller.abort(replace(TICKET, owner_nonce=foreign)),
            lambda: self.controller.resolve(
                replace(TICKET, owner_nonce=foreign), wire.Decision.COMMIT
            ),
            lambda: self.controller.arm(
                replace(WRITE, common=replace(COMMON, arm_budget_ms=99))
            ),
        ):
            with self.assertRaises(wire.FaultWireError):
                operation()
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(wire.FaultWireError):
                self.controller.get_limits(timeout=timeout)
        self.session.request.assert_not_called()

    def test_response_target_ticket_phase_and_btp_errors_fail_closed(self) -> None:
        for changed in (
            replace(TICKET, boot_nonce=OWNER),
            replace(TICKET, owner_nonce=BOOT),
            replace(TICKET, connection_epoch=9),
            replace(TICKET, bearer_epoch=9),
            replace(TICKET, database_epoch=9),
            replace(TICKET, operation=wire.Operation.READ),
            replace(TICKET, handle=99),
        ):
            self.session.request.return_value = frame(
                wire.Opcode.ARM_WRITE, ack(changed)
            )
            with self.subTest(ticket=changed), self.assertRaises(wire.FaultWireError):
                self.controller.arm(WRITE)
        self.session.request.return_value = frame(
            wire.Opcode.ARM_WRITE, ack(phase=wire.Phase.HELD)
        )
        with self.assertRaises(wire.FaultWireError):
            self.controller.arm(WRITE)
        self.session.request.return_value = frame(
            wire.Opcode.STATUS, status_bytes(ticket=replace(TICKET, sequence=43))
        )
        with self.assertRaises(wire.FaultWireError):
            self.controller.status(TICKET)
        self.session.request.return_value = frame(
            wire.Opcode.RESOLVE, ack(phase=wire.Phase.RETIRED)
        )
        with self.assertRaises(wire.FaultWireError):
            self.controller.resolve(TICKET, wire.Decision.COMMIT)
        for response in (
            BtpFrame(255, 0, 255, b"\x02"),
            BtpFrame(2, 0x10, 255, LIMITS_BYTES),
            BtpFrame(255, 0x11, 255, LIMITS_BYTES),
            BtpFrame(255, 0x10, 0, LIMITS_BYTES),
        ):
            self.session.request.return_value = response
            with self.assertRaises((wire.FaultWireError, BtpCommandError)):
                self.controller.get_limits()

    def test_arm_read_binds_live_peer_epochs_before_value_and_uuid_owner(self) -> None:
        ticket = replace(TICKET, operation=wire.Operation.READ)
        self.session.request.side_effect = [
            frame(wire.Opcode.GET_LIMITS, LIMITS_BYTES),
            frame(wire.Opcode.ARM_READ, ack(ticket)),
        ]
        result = self.controller.arm_read(TICKET.handle, b"old", PEER)
        self.assertEqual(result.ticket, ticket)
        calls = self.session.request.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].kwargs["opcode"], wire.Opcode.GET_LIMITS)
        self.assertEqual(calls[1].kwargs["payload"], COMMON_BYTES + b"\x03old")
        for response in (
            LIMITS_BYTES[:23] + b"\x04\x00" + LIMITS_BYTES[25:],
            LIMITS_BYTES[:23] + b"\x01\x00" + LIMITS_BYTES[25:],
            LIMITS_BYTES[:41] + bytes(20),
        ):
            self.session.reset_mock(side_effect=True)
            self.session.request.return_value = frame(wire.Opcode.GET_LIMITS, response)
            with self.assertRaises(wire.FaultWireError):
                self.controller.arm_read(TICKET.handle, b"old", PEER)
            self.assertEqual(self.session.request.call_count, 1)
        self.session.request.return_value = frame(wire.Opcode.GET_LIMITS, LIMITS_BYTES)
        with self.assertRaisesRegex(wire.FaultWireError, "exact live peer"):
            self.controller.arm_read(
                TICKET.handle, b"old", bytes.fromhex("01665544332211")
            )

    def test_retired_receipt_requires_all_zero_owners_and_rearmable_not_write_commit(
        self,
    ) -> None:
        ticket = replace(TICKET, operation=wire.Operation.READ)
        kwargs = {
            "ticket": ticket,
            "phase": 6,
            "winner": 1,
            "flags": 0x399,
            "opcode": 0x0A,
            "observed": b"",
            "outcome": 8,
            "counters": bytes(6),
            "remaining": (0, 0, 0, 0),
        }
        status = wire.decode_status(status_bytes(**kwargs))
        self.assertTrue(wire.cleanup_ready(status))
        self.assertTrue(wire.read_completed(status))
        for field in wire.RefCounters.__dataclass_fields__:
            with self.subTest(field=field):
                held = replace(status, refs=replace(status.refs, **{field: 1}))
                self.assertFalse(wire.cleanup_ready(held))
                self.assertFalse(wire.read_completed(held))
        self.assertFalse(
            wire.cleanup_ready(
                replace(status, flags=status.flags & ~wire.StatusFlags.REARMABLE)
            )
        )
        disconnected = replace(
            status,
            winner=wire.Winner.DISCONNECT,
            outcome=wire.Outcome.DISCONNECTED_UNCOMMITTED,
        )
        self.assertTrue(wire.cleanup_ready(disconnected))
        self.assertFalse(wire.read_completed(disconnected))
        self.assertFalse(
            wire.read_completed(
                replace(status, flags=status.flags & ~wire.StatusFlags.ANSWER_ORDERED)
            )
        )
        document = wire.ticket_document(ticket)
        self.assertEqual(wire.ticket_from_document(document), ticket)
        for bad in (
            {**document, "sequence": 43},
            {**document, "handle": 1},
            {**document, "wire_hex": str(document["wire_hex"]).upper()},
            {"sequence": 42, "handle": ticket.handle},
        ):
            with self.assertRaises(wire.FaultWireError):
                wire.ticket_from_document(bad)

    def test_session_deadline_not_renewed_by_events_and_timeout_not_retried(
        self,
    ) -> None:
        transport = Mock(spec=FrameTransport)
        transport.receive.side_effect = [
            BtpFrame(1, 0x80, 0, b"event"),
            frame(wire.Opcode.GET_LIMITS, LIMITS_BYTES),
        ]
        session = BtpSession(transport)
        controller = wire.FaultController(session, RUN)
        # A second event/response never starts a fresh timeout window.
        with (
            patch(
                "tests.peripherals.nrf52840.btp_session.time.monotonic",
                side_effect=[0.0, 0.0, 0.75, 1.0],
            ),
            self.assertRaises(TimeoutError),
        ):
            controller.get_limits(timeout=1.0)
        self.assertEqual(
            [call.args[0] for call in transport.receive.call_args_list], [1.0, 0.25]
        )
        transport.send.assert_called_once()
        transport.close.assert_called_once()
        self.assertIsNotNone(session.pop_event())


if __name__ == "__main__":
    unittest.main()
