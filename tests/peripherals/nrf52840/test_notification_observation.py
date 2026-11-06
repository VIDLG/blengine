"""Adversarial pure wire/control tests; no native, installed-image or RF verdict."""

from __future__ import annotations

import copy
import json
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from . import notification_observation as wire
from .btp_protocol import BtpFrame
from .btp_session import BtpSession
from .support.notification_observation_samples import (
    BOOT,
    NEXT_OWNER,
    OWNER,
    PEER,
    RECORD_FIELDS,
    ScriptedTransport,
    changed,
    completed_bytes,
    query_bytes,
    record_bytes,
)


class NotificationObservationCWireTests(unittest.TestCase):
    """Actual source-inclusion C handler bytes, not Python-built responses or RF."""

    def setUp(self) -> None:
        path = (
            Path(__file__).resolve().parents[2]
            / "support/rf_lab/notification_observation_c_wire.json"
        )
        self.fixture = json.loads(path.read_text(encoding="utf-8"))
        self.samples = {sample["name"]: sample for sample in self.fixture["samples"]}

    def sample(self, name: str) -> tuple[int, bytes, bytes]:
        sample = self.samples[name]
        return (
            sample["opcode"],
            bytes.fromhex(sample["request_hex"]),
            bytes.fromhex(sample["response_hex"]),
        )

    def decode(self, name: str, response: bytes | None = None) -> wire.Observation:
        opcode, request, captured = self.sample(name)
        payload = captured if response is None else response
        if opcode == wire.READ:
            return wire.decode_record(
                payload,
                expected_boot=request[1:17],
                record_owner=request[17:33],
                sequence=int.from_bytes(request[33:37], "little"),
            )
        return wire.decode_query(payload, request=request, opcode=opcode)

    def record(self, name: str) -> wire.ObservationRecord:
        parsed = self.decode(name)
        self.assertIsInstance(parsed, wire.ObservationRecord)
        assert isinstance(parsed, wire.ObservationRecord)
        return parsed

    def query(self, name: str) -> wire.ObservationQuery:
        parsed = self.decode(name)
        self.assertIsInstance(parsed, wire.ObservationQuery)
        assert isinstance(parsed, wire.ObservationQuery)
        return parsed

    def test_c_handler_bytes_requests_and_replay_without_synthetic_reencoding(
        self,
    ) -> None:
        self.assertEqual(
            self.fixture["schema"], "zephyr-notification-observation-crossing-v1"
        )
        self.assertEqual(
            self.fixture["producer"], "notification_observation/main.c --wire-fixture"
        )
        self.assertEqual(len(self.samples), 13)
        self.assertEqual(len(self.fixture["samples"]), len(self.samples))
        self.assertIn(
            "tests/bluetooth/tester/notification_observation/main.c",
            self.fixture["sources_sha256"],
        )
        self.assertIn(
            "tests/bluetooth/tester/src/btp/btp_notify_observation.h",
            self.fixture["sources_sha256"],
        )
        for source, digest in self.fixture["sources_sha256"].items():
            self.assertFalse(Path(source).is_absolute())
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
        for name in self.samples:
            with self.subTest(sample=name):
                opcode, request, response = self.sample(name)
                value = self.decode(name)
                self.assertEqual(value.response, response)
                self.assertEqual(value.request, request)
                if opcode == wire.CONFIGURE:
                    encoded = wire.encode_configure(
                        expected_boot=request[1:17],
                        expected_owner=request[17:33],
                        expected_owner_generation=int.from_bytes(
                            request[33:37], "little"
                        ),
                        new_owner=request[37:53],
                        enable=bool(request[-1]),
                    )
                elif opcode == wire.READ:
                    encoded = wire.encode_read(
                        expected_boot=request[1:17],
                        record_owner=request[17:33],
                        sequence=int.from_bytes(request[33:37], "little"),
                    )
                else:
                    self.assertEqual(opcode, wire.QUERY)
                    encoded = wire.encode_query()
                self.assertEqual(encoded, request)
                self.assertEqual(len(response), 259 if opcode == wire.READ else 156)
                replay = (
                    wire.ObserverController.record_from_document
                    if opcode == wire.READ
                    else wire.ObserverController.query_from_document
                )
                self.assertEqual(
                    replay(json.loads(json.dumps(value.document()))), value
                )

    def test_c_record_completion_layers_batch_fanout_and_signed_errors(self) -> None:
        skipped = self.record("ccc-zero")
        self.assertEqual(skipped.decision, wire.Decision.CCC_ZERO)
        self.assertEqual((skipped.cached_ccc, skipped.aggregate_ccc), (0, 1))
        self.assertIsNone(skipped.per_peer_ccc)
        self.assertIsNone(skipped.subscriber_count)
        self.assertIsNone(skipped.api_errno)
        self.assertIsNone(skipped.pdu_sequence)
        self.assertIsNone(skipped.tx_errno)
        direct = self.record("direct-completed")
        self.assertEqual(
            (direct.api_errno, direct.submit_errno, direct.tx_errno), (0, 0, 0)
        )
        self.assertEqual(
            (direct.pdu_opcode, direct.pdu_entries, direct.pdu_tracked), (0x1B, 1, 1)
        )
        self.assertLess(direct.times.tx, direct.times.returned)
        self.assertLess(direct.times.release, direct.times.returned)
        rejected = self.record("api-rejected")
        self.assertEqual(
            rejected.api_errno, -1
        )  # C -EPERM, not an ATT error or TX failure.
        self.assertIsNone(rejected.att_errno)
        self.assertIsNone(rejected.submit_errno)
        self.assertIsNone(rejected.tx_errno)
        pending = self.record("batch-pending")
        self.assertEqual(pending.api_errno, 0)
        self.assertTrue(pending.flags & wire.RecordFlags.BATCH_APPEND)
        self.assertIsNotNone(pending.entry_index)
        self.assertIsNone(pending.pdu_sequence)
        self.assertIsNone(pending.submit_errno)
        self.assertIsNone(pending.tx_errno)
        released = self.record("batch-released-before-tx")
        self.assertEqual(released.buffer_errno, 0)
        self.assertEqual(released.submit_admitted, 1)
        self.assertIsNone(released.tx_errno)
        first = self.record("batch-tx-error-first")
        second = self.record("batch-tx-error-second")
        self.assertEqual(
            (pending.sequence, released.sequence, first.sequence), (4, 4, 4)
        )
        self.assertEqual((first.value, second.value), (b"\x50", b"\x50"))
        self.assertEqual((first.entry_index, second.entry_index), (1, 2))
        self.assertEqual(first.pdu_sequence, second.pdu_sequence)
        for record in (first, second):
            self.assertEqual(
                (record.pdu_opcode, record.pdu_entries, record.pdu_tracked),
                (0x23, 2, 2),
            )
            self.assertEqual(
                (record.api_errno, record.submit_errno, record.buffer_errno), (0, 0, 0)
            )
            self.assertEqual((record.tx_callbacks, record.tx_errno), (1, -5))
            self.assertEqual(
                (record.connection_epoch, record.bearer_epoch, record.database_epoch),
                (11, 12, 13),
            )
            self.assertEqual((record.value_handle, record.selected_local_id), (41, 1))
            self.assertEqual(record.selected_peer, bytes.fromhex("010102030405c6"))

    def test_c_query_cas_rotation_preserves_history_and_observation_counters(
        self,
    ) -> None:
        initial, enabled = self.query("default-off"), self.query("enabled")
        disabled, rotated = self.query("disabled"), self.query("rotated-off")
        final = self.query("final-query")
        self.assertEqual(
            (initial.owner, initial.owner_generation, initial.enabled),
            (bytes(16), 0, False),
        )
        self.assertEqual((enabled.owner_generation, enabled.enabled), (1, True))
        self.assertEqual((disabled.owner_generation, rotated.owner_generation), (1, 2))
        self.assertNotEqual(disabled.owner, rotated.owner)
        self.assertEqual(
            (disabled.enabled, rotated.enabled, final.enabled), (False, False, False)
        )
        self.assertEqual((initial.batch_flags, final.batch_flags), (7, 15))
        self.assertEqual(
            (final.profile, final.flush_ms), (wire.Profile.AUTOMATIC_BATCHING, 1)
        )
        self.assertEqual(
            (
                final.last_sequence,
                final.oldest_sequence,
                final.newest_sequence,
                final.count,
            ),
            (5, 1, 5, 5),
        )
        self.assertEqual(final.counters.records, 5)
        self.assertEqual((final.counters.api_ok, final.counters.api_errors), (3, 1))
        self.assertEqual(
            (final.counters.pdus, final.counters.tx_ok, final.counters.tx_errors),
            (3, 1, 2),
        )
        self.assertEqual(final.counters.owner_rotations, 1)
        old = self.record("old-owner-after-rotation")
        self.assertEqual(old, self.record("batch-tx-error-first"))
        self.assertEqual((old.owner, old.owner_generation), (disabled.owner, 1))
        self.assertEqual(rotated.response, final.response)
        self.assertEqual(disabled.counters.owner_rotations, 0)

    def test_c_payloads_reject_truncation_trailing_bytes_and_identity_drift(
        self,
    ) -> None:
        for name in self.samples:
            opcode, request, response = self.sample(name)
            for length in range(len(response)):
                with (
                    self.subTest(sample=name, length=length),
                    self.assertRaises(wire.ObservationWireError),
                ):
                    self.decode(name, response[:length])
            with (
                self.subTest(sample=name, trailing=True),
                self.assertRaises(wire.ObservationWireError),
            ):
                self.decode(name, response + b"\x00")
            if opcode == wire.READ:
                for boot, owner, sequence in (
                    (bytes.fromhex("43" * 16), request[17:33], 4),
                    (request[1:17], self.query("rotated-off").owner, 4),
                    (request[1:17], request[17:33], 6),
                ):
                    with (
                        self.subTest(sample=name, identity=(boot, owner, sequence)),
                        self.assertRaises(wire.ObservationWireError),
                    ):
                        wire.decode_record(
                            response,
                            expected_boot=boot,
                            record_owner=owner,
                            sequence=sequence,
                        )
        opcode, request, response = self.sample("rotated-off")
        for offset in (1, 33, 37, 53):
            drifted = (
                request[:offset]
                + bytes([request[offset] ^ (2 if offset == 33 else 1)])
                + request[offset + 1 :]
            )
            with (
                self.subTest(cas_offset=offset),
                self.assertRaises(wire.ObservationWireError),
            ):
                wire.decode_query(response, request=drifted, opcode=opcode)


class NotificationObservationTests(unittest.TestCase):
    def parse(
        self, payload: bytes, *, owner: bytes = OWNER, sequence: int = 11
    ) -> wire.ObservationRecord:
        return wire.decode_record(
            payload, expected_boot=BOOT, record_owner=owner, sequence=sequence
        )

    def controller(
        self, *responses: bytes
    ) -> tuple[wire.ObserverController, ScriptedTransport]:
        transport = ScriptedTransport(list(responses))
        return wire.ObserverController(BtpSession(transport)), transport

    def test_revised_sizes_and_exact_little_endian_cas_requests(self) -> None:
        self.assertEqual(wire.encode_query(), b"\x01")
        request = wire.encode_configure(
            expected_boot=BOOT,
            expected_owner=OWNER,
            expected_owner_generation=0x12345678,
            new_owner=NEXT_OWNER,
            enable=False,
        )
        self.assertEqual(
            request, b"\x01" + BOOT + OWNER + b"\x78\x56\x34\x12" + NEXT_OWNER + b"\x00"
        )
        self.assertEqual(len(request), 54)
        self.assertEqual(
            wire.encode_read(
                expected_boot=BOOT, record_owner=OWNER, sequence=0x12345678
            ),
            b"\x01" + BOOT + OWNER + b"\x78\x56\x34\x12",
        )
        self.assertEqual((len(query_bytes()), len(record_bytes())), (156, 259))
        self.assertEqual((wire.RECORD_SIZE, wire.READ_COMMAND_SIZE), (257, 37))

    def test_request_type_bounds_and_zero_identity_fail_before_session(self) -> None:
        base = {
            "expected_boot": BOOT,
            "expected_owner": OWNER,
            "expected_owner_generation": 7,
            "new_owner": NEXT_OWNER,
            "enable": True,
        }
        bad = [
            ("expected_boot", bytes(16)),
            ("expected_boot", BOOT[:-1]),
            ("expected_boot", bytearray(BOOT)),
            ("expected_boot", BOOT.hex()),
            ("expected_owner", OWNER[:-1]),
            ("expected_owner", bytes(16)),
            ("expected_owner_generation", 0),
            ("expected_owner_generation", True),
            ("expected_owner_generation", -1),
            ("expected_owner_generation", 1 << 32),
            ("expected_owner_generation", 7.0),
            ("new_owner", bytes(16)),
            ("new_owner", bytearray(NEXT_OWNER)),
            ("enable", 1),
            ("enable", 0),
            ("enable", None),
        ]
        controller, transport = self.controller()
        for field, value in bad:
            with (
                self.subTest(field=field, value=value),
                self.assertRaises(wire.ObservationWireError),
            ):
                controller.configure(**(base | {field: value}))
        for sequence in (0, -1, 1 << 32, True, 1.0, "1", None):
            with (
                self.subTest(sequence=sequence),
                self.assertRaises(wire.ObservationWireError),
            ):
                controller.read(
                    expected_boot=BOOT, record_owner=OWNER, sequence=sequence
                )
        for owner in (bytes(16), OWNER[:-1], bytearray(OWNER)):
            with (
                self.subTest(owner=owner),
                self.assertRaises(wire.ObservationWireError),
            ):
                controller.read(expected_boot=BOOT, record_owner=owner, sequence=1)
        self.assertEqual(transport.sent, [])
        self.assertFalse(transport.closed)

    def test_every_truncated_or_extra_reply_and_old_draft_are_rejected(self) -> None:
        for valid, parse in (
            (query_bytes(), wire.decode_query),
            (record_bytes(), self.parse),
        ):
            for length in range(len(valid)):
                with (
                    self.subTest(size=len(valid), truncated=length),
                    self.assertRaises(wire.ObservationWireError),
                ):
                    parse(valid[:length])
            for extra in (b"\x00", bytes(100)):
                with self.assertRaises(wire.ObservationWireError):
                    parse(valid + extra)
            for wrong in (bytearray(valid), valid.hex(), None):
                with self.assertRaises(wire.ObservationWireError):
                    parse(wrong)
        with self.assertRaises(wire.ObservationWireError):
            wire.decode_query(query_bytes()[:142])
        old_request = b"\x01" + BOOT + OWNER + b"\x01"
        with self.assertRaises(wire.ObservationWireError):
            wire.decode_query(query_bytes(), opcode=wire.CONFIGURE, request=old_request)

    def test_all_firmware_errors_are_typed_exact_and_replayable(self) -> None:
        for result in tuple(wire.Result)[1:]:
            for opcode in (wire.QUERY, wire.CONFIGURE, wire.READ):
                controller, transport = self.controller(bytes([1, result]))
                with (
                    self.subTest(result=result, opcode=opcode),
                    self.assertRaises(wire.ObservationCommandError) as caught,
                ):
                    if opcode == wire.QUERY:
                        controller.query()
                    elif opcode == wire.CONFIGURE:
                        controller.configure(
                            expected_boot=BOOT,
                            expected_owner=OWNER,
                            expected_owner_generation=7,
                            new_owner=NEXT_OWNER,
                            enable=False,
                        )
                    else:
                        controller.read(
                            expected_boot=BOOT, record_owner=OWNER, sequence=11
                        )
                error = caught.exception
                self.assertEqual(error.result, result)
                self.assertEqual(
                    error.document()["request_hex"], transport.sent[0].payload.hex()
                )
                self.assertEqual(
                    error.document()["response_hex"], bytes([1, result]).hex()
                )
                self.assertFalse(transport.closed)
        for payload in (b"\x01\xff", b"\x02\x00", b"\x01\x01\x00", b"\x01\x09\x00"):
            with self.assertRaises(wire.ObservationWireError):
                wire.decode_query(payload)

    def test_query_parses_all_counters_and_batching_facts_without_deduplication(
        self,
    ) -> None:
        values = tuple(0x01020300 + i for i in range(23))
        query = wire.decode_query(
            query_bytes(
                counters=values,
                last_sequence=11,
                oldest_sequence=4,
                newest_sequence=11,
                count=8,
            )
        )
        self.assertEqual(tuple(vars(query.counters).values()), values)
        self.assertEqual(query.counters.owner_rotations, values[-1])
        self.assertEqual(query.boot, BOOT)
        self.assertEqual(
            (query.owner, query.owner_generation, query.enabled), (OWNER, 7, True)
        )
        self.assertEqual(
            (query.profile, query.batch_flags, query.flush_ms),
            (wire.Profile.AUTOMATIC_BATCHING, wire.BatchFlags(15), 1),
        )
        self.assertEqual(
            (
                query.capacity,
                query.value_max,
                query.trace_capacity,
                query.tx_group_capacity,
            ),
            (8, 20, 8, 8),
        )
        self.assertEqual(
            (
                query.last_sequence,
                query.oldest_sequence,
                query.newest_sequence,
                query.count,
            ),
            (11, 4, 11, 8),
        )
        with self.assertRaises(FrozenInstanceError):
            query.owner_generation = 8

    def test_batch_configuration_and_live_feature_selection_are_independent(
        self,
    ) -> None:
        for batching, flush, profile in (
            (0, 0, 1),
            (1, 0, 1),
            (3, 20, 2),
            (7, 20, 2),
            (15, 20, 2),
            (4, 0, 1),
        ):
            with self.subTest(batching=batching):
                query = wire.decode_query(
                    query_bytes(batch_flags=batching, flush_ms=flush, profile=profile)
                )
                self.assertEqual(
                    (int(query.batch_flags), query.flush_ms), (batching, flush)
                )
        for changes in (
            {"batch_flags": 2},
            {"batch_flags": 0, "flush_ms": 1, "profile": 1},
            {"batch_flags": 15, "flush_ms": 0},
            {"batch_flags": 11},
            {"batch_flags": 13},
            {"profile": 1},
            {"profile": 3},
            {"batch_flags": 16},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaises(wire.ObservationWireError),
            ):
                wire.decode_query(query_bytes(**changes))

    def test_query_reserved_bits_capacities_ring_and_identity_are_strict(self) -> None:
        for field, value in (
            ("boot", bytes(16)),
            ("enabled", 2),
            ("capacity", 9),
            ("value_max", 21),
            ("trace_capacity", 7),
            ("tx_group_capacity", 9),
            ("counter_count", 22),
            ("trace_loss", 8),
            ("state_flags", 64),
            ("owner_generation", 0),
            ("owner", bytes(16)),
            ("oldest_sequence", 1),
            ("newest_sequence", 1),
            ("count", 1),
            ("last_sequence", 1),
        ):
            with (
                self.subTest(field=field),
                self.assertRaises(wire.ObservationWireError),
            ):
                wire.decode_query(query_bytes(**{field: value}))
        fresh = wire.decode_query(
            query_bytes(owner=bytes(16), owner_generation=0, enabled=0)
        )
        self.assertEqual(fresh.owner_generation, 0)
        with self.assertRaises(wire.ObservationWireError):
            wire.decode_query(query_bytes(), expected_boot=NEXT_OWNER)
        for last in (1, 8, 9, wire.U32_MAX):
            query = wire.decode_query(
                query_bytes(
                    last_sequence=last,
                    newest_sequence=last,
                    oldest_sequence=max(1, last - 7),
                    count=min(8, last),
                )
            )
            self.assertEqual(query.last_sequence, last)

    def test_loss_and_saturation_are_historical_not_current_record_verdicts(
        self,
    ) -> None:
        flags = int(
            wire.StateFlags.HISTORY_INCOMPLETE
            | wire.StateFlags.TRACE_LOSS
            | wire.StateFlags.PDU_EXHAUSTED
            | wire.StateFlags.COUNTER_SATURATED
            | wire.StateFlags.EVICTED
            | wire.StateFlags.EXHAUSTED
        )
        query = wire.decode_query(
            query_bytes(
                state_flags=flags,
                trace_loss=7,
                last_sequence=wire.U32_MAX,
                newest_sequence=wire.U32_MAX,
                oldest_sequence=wire.U32_MAX - 7,
                count=8,
                counters=(wire.U32_MAX,) * 23,
            )
        )
        self.assertEqual(
            query.trace_loss,
            wire.TraceLoss.VECTOR | wire.TraceLoss.GROUP | wire.TraceLoss.TOKEN,
        )
        self.assertEqual(query.counters.unmatched, wire.U32_MAX)
        self.assertEqual(query.counters.trace_losses, wire.U32_MAX)
        # Unknown callback loss need not be attributed to a retained record.
        record = self.parse(record_bytes())
        self.assertEqual(record.trace_loss, 0)
        self.assertIsNone(record.tx_errno)
        for changes in (
            {"trace_loss": 1},
            {"state_flags": 16},
            {"state_flags": 32 | 4},
            {"state_flags": 2 | 4},
            {"state_flags": 8},
            {"trace_loss": 4, "state_flags": 16 | 4},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaises(wire.ObservationWireError),
            ):
                wire.decode_query(query_bytes(**changes))

    def test_record_full_typed_fields_and_original_entry_epochs(self) -> None:
        record = self.parse(completed_bytes())
        self.assertEqual(
            (record.boot, record.owner, record.owner_generation, record.sequence),
            (BOOT, OWNER, 7, 11),
        )
        self.assertEqual(
            (record.connection_epoch, record.bearer_epoch, record.database_epoch),
            (3, 4, 5),
        )
        self.assertEqual(
            (record.live_local_id, record.live_peer_present, record.live_peer),
            (2, True, PEER),
        )
        self.assertEqual(
            (
                record.value_handle,
                record.value_length,
                record.copied_length,
                record.value,
            ),
            (0x1234, 3, 3, b"abc"),
        )
        self.assertEqual(
            (record.cached_ccc, record.aggregate_ccc, record.per_peer_ccc), (1, 1, 1)
        )
        self.assertEqual(
            (record.subscriber_count, record.selected_local_id, record.selected_peer),
            (1, 2, PEER),
        )
        self.assertEqual(record.ccc_flags, wire.CccFlags(63))
        self.assertEqual(record.decision, wire.Decision.NOTIFY_SELECTED)
        self.assertEqual(
            (
                record.selection_errno,
                record.api_errno,
                record.submit_errno,
                record.buffer_errno,
                record.tx_errno,
            ),
            (0, 0, 0, 0, 0),
        )
        self.assertIsNone(record.att_errno)
        self.assertIsNone(record.queue_errno)
        self.assertEqual(
            (
                record.submit_attempts,
                record.submit_admitted,
                record.tx_callbacks,
                record.queue_retries,
            ),
            (1, 1, 1, 0),
        )
        self.assertEqual(
            (
                record.entry_index,
                record.pdu_sequence,
                record.pdu_entries,
                record.pdu_tracked,
                record.pdu_opcode,
            ),
            (1, 19, 1, 1, 0x1B),
        )
        self.assertEqual(
            record.times,
            wire.ObservationTimes(10, 11, 12, 20, 14, 15, 16, 17, 18, None, None, 13),
        )
        self.assertEqual(record.response, completed_bytes())

    def test_callback_release_before_return_are_not_impossible_ordering(self) -> None:
        record = self.parse(
            completed_bytes(api_errno=-12, submit_errno=-5, submit_admitted=0)
        )
        self.assertLess(record.times.tx, record.times.returned)
        self.assertLess(record.times.release, record.times.returned)
        self.assertEqual(
            (
                record.api_errno,
                record.submit_errno,
                record.tx_errno,
                record.buffer_errno,
            ),
            (-12, -5, 0, 0),
        )
        # Pending return: callback and release can already have run inside submission.
        pending = self.parse(
            completed_bytes(
                flags=0x60DB,
                submit_admitted=0,
                times=(10, 11, 12, 0, 14, 15, 16, 17, 18, 0, 0, 13),
            )
        )
        self.assertIsNone(pending.api_errno)
        self.assertIsNone(pending.submit_errno)
        self.assertEqual(pending.tx_errno, 0)

    def test_batched_admission_is_not_att_submit_tx_or_remote_delivery(self) -> None:
        record = self.parse(record_bytes())
        self.assertEqual(record.api_errno, 0)
        self.assertIsNotNone(record.entry_index)
        self.assertIsNone(record.pdu_sequence)
        self.assertIsNone(record.submit_errno)
        self.assertIsNone(record.tx_errno)
        self.assertIsNone(record.buffer_errno)
        self.assertIsNone(record.times.pdu)
        self.assertIsNone(record.times.submit)
        self.assertFalse(hasattr(record, "success"))
        # Cleanup of an unsent batch can release it without ever creating a PDU.
        released = self.parse(
            record_bytes(
                flags=0x6047,
                buffer_errno=-107,
                times=(10, 11, 12, 14, 0, 0, 0, 0, 15, 0, 0, 13),
            )
        )
        self.assertEqual(released.buffer_errno, -107)
        self.assertIsNone(released.pdu_opcode)
        self.assertIsNone(released.tx_errno)

    def test_disconnect_does_not_invent_completion_or_advance_entry_epochs(
        self,
    ) -> None:
        record = self.parse(
            record_bytes(flags=0x6107, times=(10, 11, 12, 14, 0, 0, 0, 0, 0, 25, 0, 13))
        )
        self.assertEqual(record.times.disconnect, 25)
        self.assertIsNone(record.tx_errno)
        self.assertEqual(
            (record.connection_epoch, record.bearer_epoch, record.database_epoch),
            (3, 4, 5),
        )

    def test_unselected_ccc_validity_and_zero_errno_do_not_invent_success(self) -> None:
        record = self.parse(
            record_bytes(
                ccc_flags=7,
                subscriber_count=0,
                selected_local_id=0,
                selected_peer=bytes(7),
                cached_ccc=0,
                aggregate_ccc=0,
                per_peer_ccc=0,
                decision=4,
                flags=1,
                entry_index=0,
                times=(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
            )
        )
        self.assertEqual((record.cached_ccc, record.aggregate_ccc), (0, 0))
        self.assertIsNone(record.per_peer_ccc)
        self.assertIsNone(record.subscriber_count)
        self.assertIsNone(record.selected_peer)
        self.assertIsNone(record.selection_errno)
        self.assertIsNone(record.api_errno)
        self.assertEqual((record.times.begin, record.times.update), (0, 0))
        self.assertIsNone(record.times.call)
        failed = self.parse(
            record_bytes(
                ccc_flags=23,
                selected_local_id=0,
                selected_peer=bytes(7),
                per_peer_ccc=0,
                selection_errno=-2,
                decision=7,
                flags=1,
                entry_index=0,
                times=(1, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
            )
        )
        self.assertEqual(failed.selection_errno, -2)
        self.assertIsNone(failed.api_errno)

    def test_unassociated_ccc_and_selection_metadata_fail_closed(self) -> None:
        base = record_bytes(
            ccc_flags=0,
            cached_ccc=0,
            aggregate_ccc=0,
            per_peer_ccc=0,
            subscriber_count=0,
            selected_local_id=0,
            selected_peer=bytes(7),
            live_local_id=0,
            live_peer_present=0,
            live_peer=bytes(7),
            decision=2,
            flags=1,
            entry_index=0,
            times=(1, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
        )
        record = self.parse(base)
        self.assertIsNone(record.live_peer)
        self.assertIsNone(record.cached_ccc)
        self.assertIsNone(record.aggregate_ccc)
        self.assertIsNone(record.per_peer_ccc)
        for field, value in (
            ("cached_ccc", 1),
            ("aggregate_ccc", 1),
            ("per_peer_ccc", 1),
            ("subscriber_count", 1),
            ("selected_peer", PEER),
            ("selected_local_id", 1),
            ("selection_errno", -1),
            ("api_errno", -1),
            ("live_local_id", 1),
            ("live_peer", PEER),
            ("ccc_flags", 4),
            ("ccc_flags", 8),
            ("ccc_flags", 32),
            ("flags", 0),
        ):
            with (
                self.subTest(field=field, value=value),
                self.assertRaises(wire.ObservationWireError),
            ):
                self.parse(changed(base, RECORD_FIELDS, field, value))
        # Opaque u16 CCC facts are not normalized to a bool or swapped on decode.
        record = self.parse(record_bytes(aggregate_ccc=0x1234, per_peer_ccc=0xABCD))
        self.assertEqual((record.aggregate_ccc, record.per_peer_ccc), (0x1234, 0xABCD))
        for changes in (
            {"selected_peer": bytes(7)},
            {"selected_local_id": 3},
            {"decision": 7, "selection_errno": -1},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaises(wire.ObservationWireError),
            ):
                self.parse(record_bytes(**changes))
        mismatched = self.parse(record_bytes(selected_local_id=3, flags=0x6407))
        self.assertTrue(mismatched.flags & wire.RecordFlags.INCOMPLETE)

    def test_storage_other_mode_and_direct_non_batch_paths_keep_distinct_decisions(
        self,
    ) -> None:
        storage = self.parse(
            record_bytes(
                ccc_flags=7,
                per_peer_ccc=0,
                subscriber_count=0,
                selected_local_id=0,
                selected_peer=bytes(7),
                decision=1,
                flags=0,
                entry_index=0,
                times=(1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
            )
        )
        self.assertIsNone(storage.times.update)
        self.assertIsNone(storage.api_errno)
        other = self.parse(
            record_bytes(
                ccc_flags=7,
                cached_ccc=2,
                aggregate_ccc=2,
                per_peer_ccc=0,
                subscriber_count=0,
                selected_local_id=0,
                selected_peer=bytes(7),
                decision=8,
                flags=1,
                entry_index=0,
                times=(1, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
            )
        )
        self.assertEqual(other.decision, wire.Decision.OTHER_MODE)
        self.assertIsNone(other.pdu_opcode)
        self.assertIsNone(other.tx_errno)
        direct = self.parse(completed_bytes(flags=0x30DF))
        self.assertFalse(direct.flags & wire.RecordFlags.BATCH_APPEND)
        self.assertEqual((direct.pdu_entries, direct.pdu_opcode), (1, 0x1B))
        repeated = self.parse(
            completed_bytes(
                flags=0x74DF,
                tx_callbacks=wire.U32_MAX,
                submit_attempts=wire.U32_MAX,
                submit_admitted=wire.U32_MAX,
            )
        )
        self.assertEqual(repeated.tx_callbacks, wire.U32_MAX)
        self.assertTrue(repeated.flags & wire.RecordFlags.INCOMPLETE)

    def test_all_unobserved_errnos_and_times_must_remain_zero(self) -> None:
        base = record_bytes()
        for field in (
            "att_errno",
            "queue_errno",
            "submit_errno",
            "buffer_errno",
            "tx_errno",
        ):
            with (
                self.subTest(field=field),
                self.assertRaises(wire.ObservationWireError),
            ):
                self.parse(changed(base, RECORD_FIELDS, field, -1))
        for index in (4, 5, 6, 7, 8, 9, 10):
            times = [10, 11, 12, 14, 0, 0, 0, 0, 0, 0, 0, 13]
            times[index] = 1
            with self.subTest(time=index), self.assertRaises(wire.ObservationWireError):
                self.parse(record_bytes(times=tuple(times)))
        for field, value in (
            ("pdu_sequence", 1),
            ("pdu_entries", 1),
            ("pdu_tracked", 1),
            ("pdu_opcode", 0x1B),
        ):
            with (
                self.subTest(field=field),
                self.assertRaises(wire.ObservationWireError),
            ):
                self.parse(changed(base, RECORD_FIELDS, field, value))

    def test_retries_submit_attempts_and_signed_errno_extremes_remain_independent(
        self,
    ) -> None:
        record = self.parse(
            completed_bytes(
                flags=0x70FF,
                att_errno=-(1 << 31),
                api_errno=(1 << 31) - 1,
                queue_retries=3,
                queue_errno=-11,
                submit_attempts=2,
                submit_admitted=1,
                submit_errno=-12,
                buffer_errno=-107,
                tx_errno=-5,
                times=(10, 11, 12, 20, 14, 15, 19, 17, 18, 0, 18, 13),
            )
        )
        self.assertEqual(
            (record.att_errno, record.api_errno), (-(1 << 31), (1 << 31) - 1)
        )
        self.assertEqual(
            (
                record.queue_retries,
                record.queue_errno,
                record.submit_attempts,
                record.submit_admitted,
                record.submit_errno,
            ),
            (3, -11, 2, 1, -12),
        )
        self.assertEqual((record.buffer_errno, record.tx_errno), (-107, -5))
        self.assertEqual((record.times.retry, record.times.submit), (18, 19))
        self.assertLess(record.times.tx, record.times.submit)

    def test_mixed_unobserved_and_cross_owner_identical_batch_entries(self) -> None:
        first = self.parse(
            completed_bytes(
                entry_index=2, pdu_entries=4, pdu_tracked=2, pdu_opcode=0x23
            )
        )
        second = self.parse(
            completed_bytes(
                owner=NEXT_OWNER,
                owner_generation=8,
                sequence=12,
                entry_index=4,
                pdu_entries=4,
                pdu_tracked=2,
                pdu_opcode=0x23,
                database_epoch=6,
            ),
            owner=NEXT_OWNER,
            sequence=12,
        )
        self.assertEqual(first.value, second.value)
        self.assertEqual(first.pdu_sequence, second.pdu_sequence)
        self.assertNotEqual(
            (first.owner, first.sequence), (second.owner, second.sequence)
        )
        self.assertEqual((first.trace_loss, second.trace_loss), (0, 0))
        self.assertEqual((first.database_epoch, second.database_epoch), (5, 6))
        # BATCH_APPEND also applies to one-entry conversion to opcode 0x1b.
        self.assertTrue(
            self.parse(completed_bytes()).flags & wire.RecordFlags.BATCH_APPEND
        )

    def test_vector_group_and_token_loss_preserve_known_vs_unobserved_facts(
        self,
    ) -> None:
        vector = self.parse(record_bytes(flags=0xE407, trace_loss=1))
        self.assertEqual(vector.entry_index, 1)
        self.assertIsNone(vector.pdu_sequence)
        self.assertIsNone(vector.tx_errno)
        group = self.parse(
            completed_bytes(
                flags=0xF45F,
                trace_loss=2,
                tx_callbacks=0,
                times=(10, 11, 12, 20, 14, 15, 16, 0, 18, 0, 0, 13),
            )
        )
        self.assertEqual(group.pdu_sequence, 19)
        self.assertIsNone(group.tx_errno)
        token = self.parse(
            completed_bytes(
                flags=0xF45F,
                trace_loss=4,
                pdu_sequence=0,
                tx_callbacks=0,
                times=(10, 11, 12, 20, 14, 15, 16, 0, 18, 0, 0, 13),
            )
        )
        self.assertEqual(
            token.pdu_sequence, 0
        )  # observed but uncorrelatable, not absent
        self.assertIsNone(token.tx_errno)
        partial = self.parse(
            completed_bytes(
                flags=0xF4DF,
                trace_loss=1,
                pdu_entries=9,
                pdu_tracked=8,
                pdu_opcode=0x23,
            )
        )
        self.assertEqual(
            (partial.pdu_entries, partial.pdu_tracked, partial.tx_errno), (9, 8, 0)
        )

    def test_deep_record_invariants_reject_reserved_and_contradictory_fields(
        self,
    ) -> None:
        invalid = (
            ("boot", NEXT_OWNER),
            ("owner", NEXT_OWNER),
            ("sequence", 0),
            ("sequence", 12),
            ("owner_generation", 0),
            ("live_peer_present", 2),
            ("live_peer_present", 0),
            ("value_handle", 0),
            ("copied_length", 2),
            ("value", b"abc\x01"),
            ("ccc_flags", 64),
            ("decision", 9),
            ("flags", 1 << 16),
            ("trace_loss", 8),
            ("trace_loss", 1),
            ("flags", 0xE007),
            ("flags", 0x6207),
            ("entry_index", 0),
            ("pdu_sequence", 0),
            ("pdu_entries", 0),
            ("pdu_entries", 2),
            ("pdu_tracked", 0),
            ("pdu_tracked", 9),
            ("pdu_opcode", 0x1D),
            ("entry_index", 2),
            ("submit_attempts", 0),
            ("submit_admitted", 2),
            ("tx_callbacks", 0),
            ("tx_callbacks", 2),
            ("decision", 7),
            ("ccc_flags", 7),
        )
        for field, value in invalid:
            with (
                self.subTest(field=field, value=value),
                self.assertRaises(wire.ObservationWireError),
            ):
                self.parse(changed(completed_bytes(), RECORD_FIELDS, field, value))
        for bit in (
            wire.RecordFlags.UPDATED,
            wire.RecordFlags.API_CALLED,
            wire.RecordFlags.BOUND,
            wire.RecordFlags.PDU_SEEN,
            wire.RecordFlags.API_RETURNED,
            wire.RecordFlags.SUBMIT_RETURNED,
            wire.RecordFlags.TX_CALLBACK,
        ):
            with (
                self.subTest(missing=bit),
                self.assertRaises(wire.ObservationWireError),
            ):
                self.parse(completed_bytes(flags=0x70DF & ~int(bit)))

    def test_payload_truncation_keeps_requested_length_and_full_u64_uptime(
        self,
    ) -> None:
        record = self.parse(
            record_bytes(
                value_length=65535,
                copied_length=20,
                value=bytes(range(20)),
                flags=0x6607,
                times=((1 << 64) - 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
            )
        )
        self.assertEqual(
            (record.value_length, record.copied_length, record.value),
            (65535, 20, bytes(range(20))),
        )
        self.assertEqual(record.times.begin, (1 << 64) - 1)
        self.assertTrue(record.flags & wire.RecordFlags.INCOMPLETE)
        for changes in (
            {"value_length": 21},
            {"value_length": 21, "copied_length": 20},
            {"flags": 0x6207},
            {"copied_length": 21},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaises(wire.ObservationWireError),
            ):
                self.parse(record_bytes(**changes))

    def test_fresh_initialization_same_owner_controls_and_max_generation(self) -> None:
        controller, transport = self.controller(query_bytes(owner_generation=1))
        reply = controller.configure(
            expected_boot=BOOT,
            expected_owner=wire.ZERO_OWNER,
            expected_owner_generation=0,
            new_owner=OWNER,
            enable=True,
        )
        self.assertEqual(reply.owner_generation, 1)
        self.assertEqual(transport.sent[0].payload[17:37], bytes(20))
        for enable in (False, True):
            controller, _ = self.controller(
                query_bytes(owner_generation=wire.U32_MAX, enabled=int(enable))
            )
            reply = controller.configure(
                expected_boot=BOOT,
                expected_owner=OWNER,
                expected_owner_generation=wire.U32_MAX,
                new_owner=OWNER,
                enable=enable,
            )
            self.assertEqual(reply.owner_generation, wire.U32_MAX)

    def test_rotation_is_exact_off_rotate_off_enable_and_old_owner_history_is_readable(
        self,
    ) -> None:
        counters = tuple(range(23))
        controller, transport = self.controller(
            query_bytes(enabled=0, counters=counters),
            query_bytes(
                owner=NEXT_OWNER, owner_generation=8, enabled=0, counters=counters
            ),
            query_bytes(owner=NEXT_OWNER, owner_generation=8, counters=counters),
            completed_bytes(),
        )
        replies = controller.rotate(
            expected_boot=BOOT,
            expected_owner=OWNER,
            expected_owner_generation=7,
            new_owner=NEXT_OWNER,
        )
        self.assertEqual([q.enabled for q in replies], [False, False, True])
        self.assertEqual([q.owner_generation for q in replies], [7, 8, 8])
        old = controller.read(expected_boot=BOOT, record_owner=OWNER, sequence=11)
        self.assertEqual(
            (old.owner, old.owner_generation, old.database_epoch), (OWNER, 7, 5)
        )
        self.assertEqual([f.opcode for f in transport.sent], [0x21, 0x21, 0x21, 0x22])
        for frame, expected, generation, new, enable in zip(
            transport.sent[:3],
            (OWNER, OWNER, NEXT_OWNER),
            (7, 7, 8),
            (OWNER, NEXT_OWNER, NEXT_OWNER),
            (False, False, True),
            strict=True,
        ):
            self.assertEqual((frame.service, frame.index), (255, 255))
            self.assertEqual(
                frame.payload,
                wire.encode_configure(
                    expected_boot=BOOT,
                    expected_owner=expected,
                    expected_owner_generation=generation,
                    new_owner=new,
                    enable=enable,
                ),
            )
        self.assertTrue(all(q.counters == replies[0].counters for q in replies))
        self.assertFalse(transport.closed)

    def test_rotation_stops_on_primary_error_no_retry_reset_or_automatic_enable(
        self,
    ) -> None:
        for error_at in range(3):
            responses = [
                query_bytes(enabled=0),
                query_bytes(owner=NEXT_OWNER, owner_generation=8, enabled=0),
            ][:error_at]
            responses.append(bytes([1, wire.Result.NOT_READY]))
            controller, transport = self.controller(*responses)
            with (
                self.subTest(error_at=error_at),
                self.assertRaises(wire.ObservationCommandError) as caught,
            ):
                controller.rotate(
                    expected_boot=BOOT,
                    expected_owner=OWNER,
                    expected_owner_generation=7,
                    new_owner=NEXT_OWNER,
                )
            self.assertEqual(caught.exception.result, wire.Result.NOT_READY)
            self.assertEqual(len(transport.sent), error_at + 1)
            self.assertFalse(transport.closed)
        for owner, generation, new in (
            (OWNER, 7, OWNER),
            (wire.ZERO_OWNER, 0, NEXT_OWNER),
            (OWNER, wire.U32_MAX, NEXT_OWNER),
        ):
            controller, transport = self.controller()
            with self.assertRaises(wire.ObservationWireError):
                controller.rotate(
                    expected_boot=BOOT,
                    expected_owner=owner,
                    expected_owner_generation=generation,
                    new_owner=new,
                )
            self.assertEqual(transport.sent, [])

    def test_configure_acknowledgement_must_match_exact_cas_identity(self) -> None:
        for changes in (
            {"boot": NEXT_OWNER},
            {"owner": OWNER},
            {"owner_generation": 7},
            {"enabled": 1},
        ):
            controller, _ = self.controller(
                query_bytes(
                    **(
                        {"owner": NEXT_OWNER, "owner_generation": 8, "enabled": 0}
                        | changes
                    )
                )
            )
            with (
                self.subTest(changes=changes),
                self.assertRaises(wire.ObservationWireError),
            ):
                controller.configure(
                    expected_boot=BOOT,
                    expected_owner=OWNER,
                    expected_owner_generation=7,
                    new_owner=NEXT_OWNER,
                    enable=False,
                )

    def test_real_session_preserves_send_time_events_and_transport_errors(self) -> None:
        transport = ScriptedTransport([query_bytes()])
        event = BtpFrame(1, 0x81, 0, b"connected")
        transport.during_send = lambda _: transport.frames.append(event)
        session = BtpSession(transport)
        query = wire.ObserverController(session).query(expected_boot=BOOT)
        self.assertEqual(query.boot, BOOT)
        self.assertEqual(session.pop_event(), event)
        self.assertIsNone(session.pop_event())
        self.assertFalse(transport.closed)
        primary = EOFError("primary transport failure")
        with (
            patch.object(session, "request", side_effect=primary),
            self.assertRaises(EOFError) as caught,
        ):
            wire.ObserverController(session).query()
        self.assertIs(caught.exception, primary)
        self.assertEqual(len(transport.sent), 1)

    def test_btp_status_opcode_is_not_an_observer_success(self) -> None:
        controller, transport = self.controller(query_bytes())
        with (
            patch.object(
                controller._session,
                "request",
                return_value=BtpFrame(255, 0, 255, b"\x00"),
            ),
            self.assertRaises(wire.ObservationWireError),
        ):
            controller.query()
        self.assertEqual(transport.sent, [])

    def test_documents_roundtrip_raw_queries_configures_and_records_for_replay(
        self,
    ) -> None:
        controller, _ = self.controller(
            query_bytes(), query_bytes(enabled=0), completed_bytes()
        )
        observations = (
            controller.query(),
            controller.configure(
                expected_boot=BOOT,
                expected_owner=OWNER,
                expected_owner_generation=7,
                new_owner=OWNER,
                enable=False,
            ),
            controller.read(expected_boot=BOOT, record_owner=OWNER, sequence=11),
        )
        for value in observations:
            doc = json.loads(json.dumps(value.document()))
            self.assertEqual(doc["request_hex"], value.request.hex())
            self.assertEqual(doc["response_hex"], value.response.hex())
            replay = (
                wire.ObserverController.record_from_document
                if isinstance(value, wire.ObservationRecord)
                else wire.ObserverController.query_from_document
            )
            self.assertEqual(replay(doc), value)
        self.assertEqual(observations[-1].document()["decoded"]["att_errno"], None)
        self.assertEqual(observations[-1].document()["decoded"]["tx_errno"], 0)

    def test_replay_rejects_tampered_facts_routing_types_identity_and_old_requests(
        self,
    ) -> None:
        docs = (
            (
                wire.decode_query(query_bytes()).document(),
                wire.ObserverController.query_from_document,
            ),
            (
                self.parse(record_bytes()).document(),
                wire.ObserverController.record_from_document,
            ),
        )
        for doc, replay in docs:
            changes = [
                ("schema", "old"),
                ("service", True),
                ("service", 1),
                ("index", 0),
                ("opcode", True),
                ("opcode", 0),
                ("request_hex", "01 "),
                ("request_hex", "xx"),
                ("request_hex", 1),
                ("response_hex", "0100"),
                ("response_hex", doc["response_hex"].upper()),
                ("decoded", {}),
                ("extra", 1),
            ]
            for key, value in changes:
                with (
                    self.subTest(key=key, opcode=doc["opcode"]),
                    self.assertRaises(wire.ObservationWireError),
                ):
                    replay(doc | {key: value})
            for missing in doc:
                wrong = dict(doc)
                del wrong[missing]
                with self.assertRaises(wire.ObservationWireError):
                    replay(wrong)
            wrong = copy.deepcopy(doc)
            wrong["decoded"]["owner_generation"] = True
            with self.assertRaises(wire.ObservationWireError):
                replay(wrong)
        record_doc = self.parse(record_bytes()).document()
        wrong_request = wire.encode_read(
            expected_boot=BOOT, record_owner=NEXT_OWNER, sequence=11
        )
        with self.assertRaises(wire.ObservationWireError):
            wire.ObserverController.record_from_document(
                record_doc | {"request_hex": wrong_request.hex()}
            )
        with self.assertRaises(wire.ObservationWireError):
            wire.ObserverController.query_from_document(record_doc)
        with self.assertRaises(wire.ObservationWireError):
            wire.ObserverController.record_from_document(docs[0][0])


if __name__ == "__main__":
    unittest.main()
