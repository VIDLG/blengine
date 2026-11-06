"""Native journal envelope mutations; no device, Settings or RF authority."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest import mock

from tools.react_native import ios_adapter_startup_journal as journal
from tools.react_native.rf_evidence_bundle import EvidenceError

FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "tests/support/rf_lab/ios_adapter_startup_journal.json"
)
RUN = "12345678-1234-4234-8234-123456789abc"
SHA256 = "bc6411ef6486613d785342697fa1eaeed35a922d4a855d1ab7c6245af9d61bc7"


class IOSAdapterStartupJournalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = FIXTURE.read_bytes()
        cls.source = json.loads(cls.raw)

    def encode(self, document: dict) -> bytes:
        return json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )

    def reject(self, raw: bytes, run_id: str = RUN) -> None:
        with self.assertRaises(EvidenceError):
            journal.parse_journal(raw, run_id)

    def test_swift_bytes_hash_and_record_projection_are_distinct_and_immutable(
        self,
    ) -> None:
        parsed = journal.read_journal(FIXTURE, RUN)
        self.assertEqual(parsed.raw, self.raw)
        self.assertEqual(parsed.sha256, SHA256)
        self.assertEqual(len(parsed.records), 39)
        self.assertEqual(parsed.runtime_text, "\n".join(self.source["records"]) + "\n")
        self.assertNotEqual(
            parsed.sha256, hashlib.sha256(parsed.runtime_text.encode()).hexdigest()
        )
        with self.assertRaises(FrozenInstanceError):
            parsed.run_id = "foreign"
        # Valid JSON whitespace is preserved, not stripped before hashing.
        spaced = b" \n" + self.raw + b"\n"
        other = journal.parse_journal(spaced, RUN)
        self.assertEqual(other.records, parsed.records)
        self.assertEqual(other.raw, spaced)
        self.assertNotEqual(other.sha256, parsed.sha256)

    def test_envelope_requires_unique_exact_keys_integer_version_and_attempt(
        self,
    ) -> None:
        for field in self.source:
            document = copy.deepcopy(self.source)
            document.pop(field)
            with self.subTest(missing=field):
                self.reject(self.encode(document))
        for field, values in {
            "schema_version": [True, 1.0, "1", -0.0, 2, None],
            "run_id": [
                None,
                False,
                RUN.upper(),
                "12345678123442348234123456789abc",
                "11111111-1111-4111-8111-111111111111",
            ],
            "records": [None, {}, "records", True],
            "error": [True, 0, [], {}, "", "x" * 129],
            "unexpected": [None],
        }.items():
            for value in values:
                document = {**self.source, field: value}
                with self.subTest(field=field, value=value):
                    self.reject(self.encode(document))
        for run in ("", RUN.upper(), RUN.replace("-", ""), "foreign", None, True):
            with self.subTest(run=run):
                self.reject(self.raw, run)
        duplicate = self.raw[:-1] + b',"schema_version":1}'
        self.reject(duplicate)
        self.reject(b'{"a":{"x":1,"x":2}}')

    def test_truncation_extra_documents_invalid_utf8_and_nonstandard_json_fail(
        self,
    ) -> None:
        for raw in (
            b"",
            b"null",
            b"[]",
            b"true",
            b"\xef\xbb\xbf" + self.raw,
            self.raw[:-1],
            self.raw[: len(self.raw) // 2],
            self.raw + b"{}",
            self.raw + b"garbage",
            b"\xff" + self.raw,
            self.raw.replace(b'"error":null', b'"error":NaN'),
            self.raw.replace(b'"error":null', b'"error":Infinity'),
            b'{"error":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}",
        ):
            with self.subTest(size=len(raw), suffix=raw[-12:]):
                self.reject(raw)
        escaped_surrogate = {**self.source, "records": [journal.PREFIX + "\ud800"]}
        self.reject(json.dumps(escaped_surrogate).encode())
        self.reject(bytearray(self.raw))

    def test_marker_prefix_and_embedded_controls_cannot_be_hidden_or_repaired(
        self,
    ) -> None:
        good = self.source["records"][0]
        for record in (
            None,
            1,
            {},
            "",
            journal.PREFIX,
            " " + good,
            "device " + good,
            good.removeprefix("BLEHUB_NITRO_RUNTIME "),
            good.replace("rf-adapter-transition:", "rf-other:", 1),
            good + journal.PREFIX + "passed",
            *(good + char for char in ("\n", "\r", "\t", "\x00", "\x7f", "\x85")),
        ):
            document = {**self.source, "records": [record]}
            with self.subTest(record=repr(record)[:80]):
                self.reject(self.encode(document))

    def test_byte_record_line_and_error_limits_match_native_envelope(self) -> None:
        body = journal.PREFIX + "x" * (journal.MAX_LINE_BYTES - len(journal.PREFIX) - 2)
        document = {**self.source, "records": [body + "é"]}
        self.assertEqual(
            len(journal.parse_journal(self.encode(document), RUN).records), 1
        )
        document["records"] = [body + "éx"]
        self.reject(self.encode(document))
        document = {
            **self.source,
            "records": [journal.PREFIX + "partial"] * journal.MAX_RECORDS,
        }
        self.assertEqual(
            len(journal.parse_journal(self.encode(document), RUN).records),
            journal.MAX_RECORDS,
        )
        document["records"].append(journal.PREFIX + "partial")
        self.reject(self.encode(document))
        self.assertLess(len(self.raw), journal.MAX_BYTES)
        exact = self.raw + b" " * (journal.MAX_BYTES - len(self.raw))
        self.assertEqual(len(journal.parse_journal(exact, RUN).raw), journal.MAX_BYTES)
        self.reject(exact + b" ")
        for error in ("é" * 64, "journal-write-error"):
            document = {**self.source, "error": error}
            self.assertEqual(
                journal.parse_journal(self.encode(document), RUN).error, error
            )
        for error in ("é" * 65, "failed\n", "failed\x00"):
            self.reject(self.encode({**self.source, "error": error}))

    def test_unicode_json_record_data_is_not_mistaken_for_a_line_boundary(self) -> None:
        for text in ("Lab:Device", "Lab\u2028Device", "Lab\u2029Device", "设备"):
            record = journal.PREFIX + "diagnostic:" + text
            raw = self.encode({**self.source, "records": [record]})
            result = journal.parse_journal(raw, RUN)
            self.assertEqual(result.records, (record,))
            self.assertEqual(result.runtime_text, record + "\n")

    def test_partial_and_failed_envelopes_are_retained_not_declared_passed(
        self,
    ) -> None:
        for records in ([], self.source["records"][:3]):
            for error in (None, "journal-write-error"):
                document = {**self.source, "records": records, "error": error}
                raw = self.encode(document)
                result = journal.parse_journal(raw, RUN)
                self.assertEqual(result.records, tuple(records))
                self.assertEqual(result.error, error)
                self.assertEqual(result.raw, raw)
                self.assertFalse(hasattr(result, "status"))
                self.assertEqual(
                    result.runtime_text, "".join(record + "\n" for record in records)
                )

    def test_file_read_is_bounded_even_if_source_exceeds_native_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.json"
            path.write_bytes(self.raw + b" " * journal.MAX_BYTES)
            with self.assertRaises(EvidenceError):
                journal.read_journal(path, RUN)
        stream = mock.MagicMock()
        stream.__enter__.return_value = stream
        stream.read.return_value = self.raw
        path = mock.Mock(spec=Path)
        path.open.return_value = stream
        self.assertEqual(journal.read_journal(path, RUN).raw, self.raw)
        path.open.assert_called_once_with("rb")
        stream.read.assert_called_once_with(journal.MAX_BYTES + 1)
