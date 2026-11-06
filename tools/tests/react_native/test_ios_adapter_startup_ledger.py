"""Swift-journal and synthetic mutation audits, never device or RF evidence."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path
from unittest import mock

from tests.support.rf_lab.markers import SERVICE as SHARED_SERVICE
from tools.react_native import check_runtime_log
from tools.react_native import ios_adapter_startup_ledger as ledger
from tools.react_native.ios_adapter_startup_journal import parse_journal
from tools.react_native.rf_evidence_bundle import RAW_PREFIX, RECOVERY_RESOURCE_FIELDS
from tools.tests.react_native.support.ios_adapter_startup_samples import passed_control

FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "tests/support/rf_lab/ios_adapter_startup_journal.json"
)
FIXTURE_SHA256 = "bc6411ef6486613d785342697fa1eaeed35a922d4a855d1ab7c6245af9d61bc7"
RUN = "12345678-1234-4234-8234-123456789abc"
SERVICE = "11111111-1111-4111-8111-111111111111"
NONCE = "boot-123-abc"


class IOSAdapterStartupLedgerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Consume the original Swift attachment bytes, not a Python reconstruction.
        cls.raw = FIXTURE.read_bytes()
        cls.fixture = parse_journal(cls.raw, RUN)
        cls.source = json.loads(cls.raw)

    def complete(self) -> dict:
        """Isolate mutations of the actual Swift-produced synthetic transcript."""
        return copy.deepcopy(self.source)

    def verify(self, document: dict | None = None) -> dict:
        raw = json.dumps(self.complete() if document is None else document).encode(
            "utf-8"
        )
        return ledger.verify_runtime(parse_journal(raw, RUN), SERVICE)

    def index(self, document: dict, body: str) -> int:
        return next(
            i
            for i, r in enumerate(document["records"])
            if r.startswith(ledger.PREFIX + body)
        )

    def edit_json(
        self, document: dict, body: str, updates: dict, *, remove: str | None = None
    ) -> None:
        index = self.index(document, body)
        value = json.loads(document["records"][index][len(ledger.PREFIX + body) :])
        value.update(updates)
        if remove is not None:
            value.pop(remove)
        document["records"][index] = ledger.PREFIX + body + json.dumps(value)

    def reject(self, document: dict) -> None:
        with self.assertRaises(ledger.EvidenceError):
            self.verify(document)

    def test_off_snapshot_reuses_runtime_checks_and_stops_before_any_ready(
        self,
    ) -> None:
        document = self.complete()
        document["records"] = document["records"][:8]
        raw = json.dumps(document).encode()
        result = ledger.verify_off_journal(parse_journal(raw, RUN))
        full = ledger.verify_runtime(self.fixture, SERVICE)
        self.assertEqual(
            result,
            {
                "schema_version": 1,
                "run_id": RUN,
                "native_bootstrap": full["native_bootstrap"],
                "runtime_nonce": NONCE,
                "journal_sha256": hashlib.sha256(raw).hexdigest(),
                "resources": full["closed_resources"]["off"],
            },
        )
        for records in (
            document["records"][:-1],
            document["records"] + [self.source["records"][8]],
            self.source["records"],
        ):
            with (
                self.subTest(records=len(records)),
                self.assertRaises(ledger.EvidenceError),
            ):
                ledger.verify_off_journal(
                    parse_journal(
                        json.dumps({**document, "records": records}).encode(), RUN
                    )
                )
        document["records"][2] = document["records"][2].replace(
            "PoweredOff", "PoweredOn"
        )
        with self.assertRaises(ledger.EvidenceError):
            ledger.verify_off_journal(parse_journal(json.dumps(document).encode(), RUN))

    def test_exact_swift_fixture_replays_without_repair_or_normalization(self) -> None:
        self.assertEqual(hashlib.sha256(self.raw).hexdigest(), FIXTURE_SHA256)
        self.assertEqual(self.fixture.raw, self.raw)
        self.assertEqual(len(self.fixture.records), 39)
        self.assertIsNone(self.fixture.error)
        self.assertEqual(
            self.fixture.runtime_text, "\n".join(self.fixture.records) + "\n"
        )
        result = ledger.verify_runtime(self.fixture, SERVICE)
        self.assertEqual(result["journal_sha256"], FIXTURE_SHA256)
        self.assertEqual(result["runtime_status"], "passed")

    def test_complete_runtime_scope_hashes_and_unchanged_checker_projection(
        self,
    ) -> None:
        document = self.complete()
        raw = json.dumps(document, indent=2).encode("utf-8")
        journal = parse_journal(raw, RUN)
        original = (journal.raw, journal.records, journal.runtime_text)
        projection = "".join(
            RAW_PREFIX + record[len(ledger.PREFIX + "on:flow:") :] + "\n"
            for record in journal.records
            if record.startswith(ledger.PREFIX + "on:flow:")
        )
        with mock.patch.object(
            check_runtime_log, "check_rf_scan", wraps=check_runtime_log.check_rf_scan
        ) as checker:
            result = ledger.verify_runtime(journal, SERVICE)
        checker.assert_called_once_with(
            projection,
            SERVICE,
            Path("ios-adapter-startup-journal"),
            connect=True,
            gatt=True,
            io=True,
            expected_local_name="BleHubNrf52840",
        )
        self.assertEqual((journal.raw, journal.records, journal.runtime_text), original)
        self.assertEqual(result["scope"], "cooperative-runtime-transcript-only")
        self.assertEqual(result["runtime_status"], "passed")
        self.assertEqual(result["platform"], "ios")
        self.assertEqual(result["adapter_mode"], "startup-off")
        self.assertEqual(result["run_id"], RUN)
        self.assertEqual(result["runtime_nonce"], NONCE)
        self.assertEqual(result["journal_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(
            result["runtime_sha256"], hashlib.sha256(original[2].encode()).hexdigest()
        )
        self.assertEqual(
            result["on_flow_sha256"], hashlib.sha256(projection.encode()).hexdigest()
        )
        self.assertNotIn("status", result)  # No overall host/bundle verdict.
        self.assertEqual(
            set(result["explicit_limits"]),
            {"identity", "control", "rf", "resources", "timing"},
        )
        self.assertEqual(result["counts"]["closed_zero_snapshots"], 2)
        self.assertEqual(
            result["closed_resources"],
            {
                phase: dict.fromkeys(RECOVERY_RESOURCE_FIELDS, 0)
                for phase in ("off", "on")
            },
        )

    def test_launch_name_binding_preserves_colons_and_unicode_record_data(self) -> None:
        for name in ("Lab:Device", "Lab\u2028Device", "Lab\u2029Device", "设备"):
            document = self.complete()
            start = self.index(document, "on:flow:rf:start:")
            document["records"][start] = document["records"][start].replace(
                "BleHubNrf52840", name
            )
            adv = self.index(document, "on:flow:rf:step:advertisement:")
            document["records"][adv] = document["records"][adv].replace(
                "localName=missing:", "localName=" + name + ":"
            )
            with self.subTest(name=name):
                self.verify(document)
            document["records"][adv] = document["records"][adv].replace(
                "localName=" + name + ":", "localName=OtherDevice:"
            )
            with self.subTest(foreign_name=name):
                self.reject(document)

    def test_every_missing_duplicate_and_adjacent_reorder_is_rejected(self) -> None:
        original = self.complete()
        self.verify(original)
        for index in range(len(original["records"])):
            for mutation in ("missing", "duplicate", "reorder"):
                if mutation == "reorder" and index + 1 == len(original["records"]):
                    continue
                document = copy.deepcopy(original)
                records = document["records"]
                if mutation == "missing":
                    records.pop(index)
                elif mutation == "duplicate":
                    records.insert(index, records[index])
                else:
                    records[index : index + 2] = records[index : index + 2][::-1]
                with self.subTest(index=index, mutation=mutation):
                    self.reject(document)

    def test_bootstrap_requires_exact_schema_identity_and_positive_int32(self) -> None:
        for key, values in {
            "run_id": [SERVICE, RUN.upper(), None],
            "boot_id": ["not-a-uuid", "11111111-1111-4111-8111-11111111111A", 1],
            "adapter_mode": ["active-loss", "startup-on", None],
            "pid": [True, False, 123.0, -0.0, 0, -1, 2_147_483_648, "123"],
            "bundle_id": ["com.blehubnitrohost", None],
            "source": ["application-open", "payload-url", None],
            "extra": [None],
        }.items():
            for value in values:
                document = self.complete()
                # Mutate all copies to catch coherently forged invalid boot identity too.
                for body in ("native:bootstrap:", "native:ready:", "native:signal:"):
                    self.edit_json(document, body, {key: value})
                with self.subTest(key=key, value=value):
                    self.reject(document)
        for key in (
            "run_id",
            "adapter_mode",
            "pid",
            "boot_id",
            "bundle_id",
            "executable",
            "source",
        ):
            document = self.complete()
            self.edit_json(document, "native:bootstrap:", {}, remove=key)
            with self.subTest(missing=key):
                self.reject(document)
        for pid in (1, 2_147_483_647):
            document = self.complete()
            for body in ("native:bootstrap:", "native:ready:", "native:signal:"):
                self.edit_json(document, body, {"pid": pid})
            self.assertEqual(self.verify(document)["pid"], pid)

    def test_executable_must_be_an_encoded_local_file_url_without_controls(
        self,
    ) -> None:
        for value in (
            None,
            1,
            "/Applications/Host.app/Host",
            "https://example.com/Host",
            "file:/Applications/Host",
            "file://localhost/Applications/Host",
            "file://remote/Applications/Host",
            "file:///Applications/Host/",
            "file:///Applications/Host?x=1",
            "file:///Applications/Host#fragment",
            "file:///Application Support/Host",
            "file:///Applications/Host\\bad",
            "file:///Applications/Host%00",
            "file:///Applications/Host%0A",
            "file:///Applications/Host%7f",
            "file:///Applications/Host%2F",
            "file:///Applications/Host%",
            "file:///Applications/Host%GG",
            "file:///Applications/Host%FF",
        ):
            document = self.complete()
            for body in ("native:bootstrap:", "native:ready:", "native:signal:"):
                self.edit_json(document, body, {"executable": value})
            with self.subTest(value=value):
                self.reject(document)
        self.verify()  # Swift fixture includes a valid percent-encoded space.

    def test_ready_and_signal_exact_identity_nonce_url_source_and_forward_order(
        self,
    ) -> None:
        for body in ("native:ready:", "native:signal:"):
            for key, value in (
                ("run_id", SERVICE),
                ("boot_id", RUN),
                ("pid", 124),
                ("pid", 123.0),
                ("adapter_mode", "active-loss"),
                ("bundle_id", "foreign"),
                ("executable", "file:///Other.app/Other"),
                ("nonce", "other-js-boot"),
                ("source", "launch"),
                ("source", "payload-url"),
                ("extra", None),
            ):
                document = self.complete()
                self.edit_json(document, body, {key: value})
                with self.subTest(body=body, key=key, value=value):
                    self.reject(document)
            for key in ("nonce", "pid", "source", "url"):
                document = self.complete()
                self.edit_json(document, body, {}, remove=key)
                with self.subTest(body=body, missing=key):
                    self.reject(document)
            path = "ready" if body == "native:ready:" else "on"
            for url in (
                f"blehub-rn-spike://adapter/{RUN}/{path}/",
                f"blehub-rn-spike://adapter/{RUN}/{path}?x=1",
                f"blehub-rn-spike://adapter/{RUN}/{path}#fragment",
                f"blehub-rn-spike://adapter/{SERVICE}/{path}",
                f"blehub-rn-spike://adapter/{RUN}/{'on' if path == 'ready' else 'ready'}",
                f"BLEHUB-RN-SPIKE://adapter/{RUN}/{path}",
            ):
                document = self.complete()
                self.edit_json(document, body, {"url": url})
                with self.subTest(body=body, url=url):
                    self.reject(document)
        document = self.complete()
        ready = self.index(document, "native:ready:")
        request = self.index(document, "on-request:")
        document["records"][ready], document["records"][request] = (
            document["records"][request],
            document["records"][ready],
        )
        self.reject(document)

    def test_runtime_nonce_canonical_bound_and_close_must_match(self) -> None:
        for nonce in (
            "",
            "a-b",
            "a-b-c-d",
            "A-b-c",
            "a_b-c-d",
            "a-b-c ",
            "a" * 125 + "-b-c",
            "a-b-ç",
        ):
            document = self.complete()
            document["records"] = [r.replace(NONCE, nonce) for r in document["records"]]
            with self.subTest(nonce=nonce):
                self.reject(document)
        for nonce in ("0-0-0", "a" * 124 + "-b-c"):
            document = self.complete()
            document["records"] = [r.replace(NONCE, nonce) for r in document["records"]]
            self.assertEqual(self.verify(document)["runtime_nonce"], nonce)
        document = self.complete()
        document["records"][-2] = document["records"][-2].replace(
            NONCE, "other-js-boot"
        )
        self.reject(document)
        for body in (
            "runtime:",
            "awaiting-on:",
            "host-window-ready:",
            "on-request:",
            "on-signal:",
            "passed:",
        ):
            document = self.complete()
            index = self.index(document, body)
            document["records"][index] = document["records"][index].replace(
                RUN, SERVICE
            )
            with self.subTest(foreign_run=body):
                self.reject(document)

    def test_off_actual_dto_allows_null_optionals_not_foreign_routes_or_errors(
        self,
    ) -> None:
        for event_index, changes in (
            (2, {"adapterState": "PoweredOn"}),
            (2, {"scanAuthorization": "Denied"}),
            (2, {"commandId": "31002"}),
            (2, {"error": {"category": "adapter_unavailable", "message": "off"}}),
            (3, {"kind": "adapterSnapshotChanged"}),
            (3, {"commandId": 31000}),
            (3, {"error": {}}),
            (4, {"error": None}),
            (4, {"error": {"category": "permission", "message": "off"}}),
            (4, {"error": {"category": "adapter_unavailable"}}),
            (4, {"error": {"category": "adapter_unavailable", "message": 1}}),
            (
                4,
                {
                    "error": {
                        "category": "adapter_unavailable",
                        "message": "off",
                        "extra": None,
                    }
                },
            ),
        ):
            document = self.complete()
            event = json.loads(
                document["records"][event_index].split("off:event:", 1)[1]
            )
            event.update(changes)
            document["records"][event_index] = (
                ledger.PREFIX + "off:event:" + json.dumps(event)
            )
            with self.subTest(event_index=event_index, changes=changes):
                self.reject(document)
        for key, value in (
            ("scanSessionId", "31001"),
            ("connectionId", "9007"),
            ("adapterState", "PoweredOff"),
            ("foreign", None),
        ):
            document = self.complete()
            event = json.loads(document["records"][3].split("off:event:", 1)[1])
            event[key] = value
            document["records"][3] = ledger.PREFIX + "off:event:" + json.dumps(event)
            with self.subTest(key=key):
                self.reject(document)
        document = self.complete()
        for index in (2, 3, 4):
            event = json.loads(document["records"][index].split("off:event:", 1)[1])
            event.update(scanSessionId=None, connectionId=None, gattServices=None)
            if index == 4:
                event["error"]["platformDetails"] = {
                    "platform": "platform_apple",
                    "code": None,
                    "message": "native",
                }
            document["records"][index] = (
                ledger.PREFIX + "off:event:" + json.dumps(event)
            )
        self.verify(document)
        for details in (
            {},
            {"platform": "platform_android"},
            {"platform": "platform_apple", "code": 1},
            {"platform": "platform_apple", "extra": None},
        ):
            changed = copy.deepcopy(document)
            event = json.loads(changed["records"][4].split("off:event:", 1)[1])
            event["error"]["platformDetails"] = details
            changed["records"][4] = ledger.PREFIX + "off:event:" + json.dumps(event)
            with self.subTest(details=details):
                self.reject(changed)

    def test_both_resources_require_exact_seventeen_lexical_integer_zeros(self) -> None:
        for phase in ("off", "on"):
            for key in RECOVERY_RESOURCE_FIELDS:
                for value in (1, -1, True, 0.0, -0.0, None, "0"):
                    document = self.complete()
                    self.edit_json(document, phase + ":resources:", {key: value})
                    with self.subTest(phase=phase, key=key, value=value):
                        self.reject(document)
                document = self.complete()
                self.edit_json(document, phase + ":resources:", {}, remove=key)
                with self.subTest(phase=phase, missing=key):
                    self.reject(document)
            for suffix in ('"pendingIo":0', '"extra":0'):
                document = self.complete()
                index = self.index(document, phase + ":resources:")
                document["records"][index] = (
                    document["records"][index][:-1] + "," + suffix + "}"
                )
                with self.subTest(phase=phase, suffix=suffix):
                    self.reject(document)
            document = self.complete()
            index = self.index(document, phase + ":resources:")
            document["records"][index] = document["records"][index].replace(
                '"pendingIo":0', '"pendingIo":-0'
            )
            self.reject(document)

    def test_strict_json_duplicates_constants_and_nonobjects(self) -> None:
        for body in (
            "native:bootstrap:",
            "native:ready:",
            "native:signal:",
            "off:event:",
            "on:resources:",
        ):
            for raw in (
                "[]",
                "null",
                "{",
                '{"pid":123,"pid":123}',
                '{"x":NaN}',
                '{"x":Infinity}',
            ):
                document = self.complete()
                index = self.index(document, body)
                document["records"][index] = ledger.PREFIX + body + raw
                with self.subTest(body=body, raw=raw):
                    self.reject(document)

    def test_unknown_foreign_failed_and_late_markers_never_disappear(self) -> None:
        for body in (
            "unknown:passed",
            "failed:late failure",
            "off:failed:late callback",
            "on:flow:rf-connect:passed:foreign-family",
            "on:flow:rf-notification:passed:foreign-family",
            "on:flow:rf:step:read-response:failed:timeout",
            "on:flow:rf:close:rejected:closed",
            "native:bootstrap:{}",
            "active:accepted:32008",
            f"passed:runId={SERVICE}",
        ):
            for index in (0, 8, 20, len(self.complete()["records"])):
                document = self.complete()
                document["records"].insert(index, ledger.PREFIX + body)
                with self.subTest(body=body, index=index):
                    self.reject(document)
        document = self.complete()
        index = self.index(document, "on:flow:rf:step:read-response:")
        document["records"][index] += ":failed:late"
        self.reject(document)
        document = self.complete()
        document["error"] = "late-journal-write-error"
        journal = parse_journal(json.dumps(document).encode(), RUN)
        self.assertEqual(journal.error, document["error"])
        with self.assertRaisesRegex(ledger.EvidenceError, "nonnull error"):
            ledger.verify_runtime(journal, SERVICE)

    def test_existing_io_checker_rejects_field_mutations_without_normalization(
        self,
    ) -> None:
        for body, before, after in (
            ("rf:step:advertisement:", "scanSessionId=9004", "scanSessionId=8"),
            ("rf:step:connected:", "commandId=9005", "commandId=8"),
            ("rf:step:gatt-discovery:", "descriptorCount=2", "descriptorCount=0"),
            ("rf:step:read-initial:", "value=00", "value=ff"),
            ("rf:step:write-response:", "commandId=9011", "commandId=8"),
            ("rf:step:read-response:", "value=10", "value=ff"),
            (
                "rf:step:write-without-response:",
                "terminal=local-submission",
                "terminal=remote-delivery",
            ),
            ("rf:step:read-without-response:", "value=ab", "value=ff"),
            ("rf:step:connection-silence:", "observationMs=1000", "observationMs=0"),
            ("rf-io:passed:", "scanSessionId=9004", "scanSessionId=8"),
        ):
            document = self.complete()
            index = self.index(document, "on:flow:" + body)
            self.assertIn(before, document["records"][index])
            document["records"][index] = document["records"][index].replace(
                before, after
            )
            with (
                self.subTest(body=body),
                self.assertRaisesRegex(ledger.EvidenceError, "on rf-io:"),
            ):
                self.verify(document)
        for service in (SHARED_SERVICE.upper(), "not-a-uuid", SHARED_SERVICE):
            with self.subTest(service=service), self.assertRaises(ledger.EvidenceError):
                ledger.verify_runtime(
                    parse_journal(json.dumps(self.complete()).encode(), RUN), service
                )


class IOSAdapterStartupControlTests(unittest.TestCase):
    """Test-local synthetic UI observations; never a live control fixture."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.runtime_fixture = ledger.verify_runtime(
            parse_journal(FIXTURE.read_bytes(), RUN), SERVICE
        )

    def setUp(self) -> None:
        self.runtime = copy.deepcopy(self.runtime_fixture)
        self.receipt = "ab" * 32
        self.identity = {
            "native": copy.deepcopy(self.runtime["native_bootstrap"]),
            "run_id": RUN,
            "nonce": self.runtime["runtime_nonce"],
        }

    def window(self) -> dict:
        return {
            "schema_version": 1,
            "run_id": RUN,
            "fixture_receipt_sha256": self.receipt,
            "identity": copy.deepcopy(self.identity),
        }

    def control(self) -> dict:
        return passed_control(self.identity, self.receipt)

    def verify(self, control: dict) -> dict:
        return ledger.verify_control(
            control, runtime=self.runtime, receipt_sha256=self.receipt
        )

    def reject(self, control: dict) -> None:
        with self.assertRaises(ledger.EvidenceError):
            self.verify(control)

    def test_source_oracle_does_not_follow_a_changed_verifier_transition_table(
        self,
    ) -> None:
        changed = tuple(reversed(ledger._CONTROL_STEPS))
        with mock.patch.object(ledger, "_CONTROL_STEPS", changed):
            # Build inside the patch: a fixture derived from the implementation
            # would drift with it and silently turn this rejection into a pass.
            control = self.control()
            with self.assertRaises(ledger.EvidenceError):
                self.verify(control)

    def test_window_and_control_bind_without_mutation_or_overall_verdict(self) -> None:
        window, control = self.window(), self.control()
        before = copy.deepcopy((window, control, self.runtime))
        identity = ledger.verify_window_ready(window, RUN, self.receipt)
        result = self.verify(control)
        self.assertEqual(identity, self.identity)
        self.assertEqual(result["identity"], identity)
        self.assertEqual(result["observations"], control["observations"])
        self.assertEqual(result["fixture_receipt_sha256"], self.receipt)
        self.assertEqual(result["control_status"], "passed")
        self.assertEqual(result["scope"], "cooperative-control-observations-only")
        self.assertNotIn("status", result)
        self.assertEqual(
            set(result["explicit_limits"]),
            {"identity", "evidence", "cleanup", "timing"},
        )
        self.assertEqual((window, control, self.runtime), before)
        identity["native"]["pid"] = 1
        result["identity"]["native"]["pid"] = 2
        result["observations"][3]["identity"]["nonce"] = "other-ui-nonce"
        self.assertEqual((window, control, self.runtime), before)

    def test_window_and_control_exact_envelopes_no_alias_fields(self) -> None:
        for kind, original in (("window", self.window()), ("control", self.control())):
            mutations = [{**original, "extra": None}]
            for key in original:
                missing = copy.deepcopy(original)
                missing.pop(key)
                mutations.append(missing)
            for alias in ("receipt_sha256", "fixtureReceiptSha256", "receipt"):
                changed = copy.deepcopy(original)
                changed[alias] = changed.pop("fixture_receipt_sha256")
                mutations.append(changed)
            for document in (None, [], "{}", *mutations):
                with (
                    self.subTest(kind=kind, document=document),
                    self.assertRaises(ledger.EvidenceError),
                ):
                    if kind == "window":
                        ledger.verify_window_ready(document, RUN, self.receipt)
                    else:
                        self.verify(document)
            for key, values in {
                "schema_version": (True, 1.0, "1", 0, 2, None),
                "run_id": (SERVICE, RUN.upper(), "", None),
                "fixture_receipt_sha256": (
                    "cd" * 32,
                    self.receipt.upper(),
                    self.receipt + " ",
                    None,
                    [],
                ),
            }.items():
                for value in values:
                    document = copy.deepcopy(original)
                    document[key] = value
                    with (
                        self.subTest(kind=kind, key=key, value=value),
                        self.assertRaises(ledger.EvidenceError),
                    ):
                        if kind == "window":
                            ledger.verify_window_ready(document, RUN, self.receipt)
                        else:
                            self.verify(document)

    def test_receipt_argument_must_be_canonical_even_if_both_documents_agree(
        self,
    ) -> None:
        for receipt in (
            None,
            True,
            1,
            "",
            "a" * 63,
            "a" * 65,
            "g" * 64,
            "AB" * 32,
            "sha256:" + self.receipt,
            self.receipt + "\n",
        ):
            window, control = self.window(), self.control()
            window["fixture_receipt_sha256"] = receipt
            control["fixture_receipt_sha256"] = receipt
            with self.subTest(receipt=receipt):
                with self.assertRaises(ledger.EvidenceError):
                    ledger.verify_window_ready(window, RUN, receipt)
                with self.assertRaises(ledger.EvidenceError):
                    ledger.verify_control(
                        control, runtime=self.runtime, receipt_sha256=receipt
                    )
        for run_id in (None, True, RUN.upper(), "not-a-uuid"):
            with self.subTest(run_id=run_id), self.assertRaises(ledger.EvidenceError):
                ledger.verify_window_ready(self.window(), run_id, self.receipt)

    def test_identity_and_native_exact_schemas_on_both_apis(self) -> None:
        identities = [None, [], {**self.identity, "extra": None}]
        for key in self.identity:
            changed = copy.deepcopy(self.identity)
            changed.pop(key)
            identities.append(changed)
        for native in (None, [], {**self.identity["native"], "extra": None}):
            identities.append({**self.identity, "native": native})
        for key in self.identity["native"]:
            changed = copy.deepcopy(self.identity)
            changed["native"].pop(key)
            identities.append(changed)
        for identity in identities:
            window, control = self.window(), self.control()
            window["identity"] = identity
            control["observations"][3]["identity"] = identity
            with self.subTest(identity=identity):
                with self.assertRaises(ledger.EvidenceError):
                    ledger.verify_window_ready(window, RUN, self.receipt)
                self.reject(control)

    def test_shared_native_validation_rejects_coherent_invalid_runtime_and_ui(
        self,
    ) -> None:
        for key, values in {
            "run_id": (SERVICE, RUN.upper(), None),
            "adapter_mode": ("active-off", "", None),
            "pid": (True, False, 0, -1, 2_147_483_648, 123.0, "123", None),
            "boot_id": (RUN.upper(), "not-a-uuid", None),
            "bundle_id": ("other.bundle", None),
            "source": ("application-open", None),
            "executable": (
                "Host",
                "https://host/App",
                "file://host/App",
                "file:///App/",
                "file:///App?x=1",
                "file:///App#x",
                "file:///App Space",
                "file:///App%GG",
                "file:///App%FF",
                "file:///App%00",
                "file:///App%E2%80%8B",
                None,
            ),
        }.items():
            for value in values:
                window, control = self.window(), self.control()
                window["identity"]["native"][key] = value
                self.runtime = copy.deepcopy(self.runtime_fixture)
                self.runtime["native_bootstrap"][key] = value
                for observation in control["observations"]:
                    if observation["identity"] is not None:
                        observation["identity"]["native"][key] = value
                with self.subTest(key=key, value=value):
                    with self.assertRaises(ledger.EvidenceError):
                        ledger.verify_window_ready(window, RUN, self.receipt)
                    self.reject(control)

    def test_shared_nonce_and_positive_int32_boundaries(self) -> None:
        for nonce in (
            None,
            True,
            "",
            "a-b",
            "a-b-c-d",
            "A-b-c",
            "a-b-ç",
            "a-b-c\n",
            "a" * 125 + "-b-c",
        ):
            window, control = self.window(), self.control()
            window["identity"]["nonce"] = nonce
            self.runtime["runtime_nonce"] = nonce
            for observation in control["observations"]:
                if observation["identity"] is not None:
                    observation["identity"]["nonce"] = nonce
            with self.subTest(nonce=nonce):
                with self.assertRaises(ledger.EvidenceError):
                    ledger.verify_window_ready(window, RUN, self.receipt)
                self.reject(control)
        for pid in (1, 2_147_483_647):
            for nonce in ("0-0-0", "a" * 124 + "-b-c"):
                window, control = self.window(), self.control()
                window["identity"]["native"]["pid"] = pid
                window["identity"]["nonce"] = nonce
                self.runtime["native_bootstrap"]["pid"] = pid
                self.runtime["runtime_nonce"] = nonce
                for observation in control["observations"]:
                    if observation["identity"] is not None:
                        observation["identity"]["native"]["pid"] = pid
                        observation["identity"]["nonce"] = nonce
                with self.subTest(pid=pid, nonce=nonce):
                    self.assertEqual(
                        ledger.verify_window_ready(window, RUN, self.receipt),
                        window["identity"],
                    )
                    self.verify(control)

    def test_every_control_event_missing_duplicate_reorder_and_unknown(self) -> None:
        original = self.control()
        # Equal times isolate action ordering from the independent elapsed check.
        for observation in original["observations"]:
            observation["elapsed_seconds"] = 0
        for index in range(12):
            for mutation in ("missing", "duplicate", "reorder", "unknown"):
                control = copy.deepcopy(original)
                observations = control["observations"]
                if mutation == "missing":
                    observations.pop(index)
                elif mutation == "duplicate":
                    observations.insert(index, copy.deepcopy(observations[index]))
                elif mutation == "reorder":
                    other = (index + 1) % 12
                    observations[index], observations[other] = (
                        observations[other],
                        observations[index],
                    )
                else:
                    observations[index]["action"] = "unknown-action"
                with self.subTest(index=index, mutation=mutation):
                    self.reject(control)
        for observations in (None, {}, (), "observations"):
            control = self.control()
            control["observations"] = observations
            self.reject(control)

    def test_each_observation_requires_exact_fields_and_switch_identity(self) -> None:
        original = self.control()
        for index, observation in enumerate(original["observations"]):
            mutations = [None, [], {**observation, "extra": None}]
            for key in observation:
                changed = copy.deepcopy(observation)
                changed.pop(key)
                mutations.append(changed)
            for value in (None, "0", "1", 0, 1, True, False, "on", "off", " 1"):
                if (
                    type(value) is type(observation["switch_value"])
                    and value == observation["switch_value"]
                ):
                    continue
                mutations.append({**observation, "switch_value": value})
            mutations.append(
                {
                    **observation,
                    "identity": None
                    if observation["identity"] is not None
                    else self.identity,
                }
            )
            for changed in mutations:
                control = copy.deepcopy(original)
                control["observations"][index] = changed
                with self.subTest(index=index, changed=changed):
                    self.reject(control)

    def test_each_identified_event_binds_every_runtime_identity_field(self) -> None:
        original = self.control()
        for index, observation in enumerate(original["observations"]):
            if observation["identity"] is None:
                continue
            for key, value in (("run_id", SERVICE), ("nonce", "other-js-nonce")):
                control = copy.deepcopy(original)
                control["observations"][index]["identity"][key] = value
                with self.subTest(index=index, field=key):
                    self.reject(control)
            for key, value in {
                "pid": self.identity["native"]["pid"] + 1,
                "boot_id": RUN,
                "run_id": SERVICE,
                "executable": "file:///Other.app/Other",
                "bundle_id": "other.bundle",
                "source": "application-open",
                "adapter_mode": "active-off",
            }.items():
                control = copy.deepcopy(original)
                control["observations"][index]["identity"]["native"][key] = value
                with self.subTest(index=index, native_field=key):
                    self.reject(control)
        for key in ("native_bootstrap", "runtime_nonce", "run_id"):
            self.runtime = copy.deepcopy(self.runtime_fixture)
            self.runtime.pop(key)
            with self.subTest(missing_runtime=key):
                self.reject(original)
        for runtime in (None, [], "runtime"):
            with self.subTest(runtime=runtime), self.assertRaises(ledger.EvidenceError):
                ledger.verify_control(
                    original, runtime=runtime, receipt_sha256=self.receipt
                )

    def test_runtime_binding_is_not_inferred_from_control_or_pid_aliases(self) -> None:
        control = self.control()
        for key, value in (("pid", 1), ("boot_id", RUN)):
            self.runtime = copy.deepcopy(self.runtime_fixture)
            self.runtime["native_bootstrap"][key] = value
            with self.subTest(native_field=key):
                self.reject(control)
        for key, value in (("run_id", SERVICE), ("runtime_nonce", "other-js-nonce")):
            self.runtime = copy.deepcopy(self.runtime_fixture)
            self.runtime[key] = value
            with self.subTest(runtime_field=key):
                self.reject(control)
        self.runtime = copy.deepcopy(self.runtime_fixture)
        # Top-level PID/boot aliases cannot replace native_bootstrap authority.
        self.runtime["pid"] = 1
        self.runtime["boot_id"] = RUN
        self.assertEqual(self.verify(control)["identity"], self.identity)

    def test_elapsed_each_event_finite_nonbool_nonnegative_and_forward_only(
        self,
    ) -> None:
        original = self.control()
        for index in range(12):
            for value in (
                None,
                True,
                False,
                "1",
                -1,
                float("nan"),
                float("inf"),
                -float("inf"),
                [],
                {},
            ):
                control = copy.deepcopy(original)
                control["observations"][index]["elapsed_seconds"] = value
                with self.subTest(index=index, value=value):
                    self.reject(control)
            if index:
                control = copy.deepcopy(original)
                control["observations"][index]["elapsed_seconds"] = (
                    control["observations"][index - 1]["elapsed_seconds"] - 0.1
                )
                with self.subTest(decreasing_at=index):
                    self.reject(control)
        # Equal times and arbitrary gaps are valid: no inferred host deadline/epoch.
        for elapsed in (0, 0.0, 1e100, 10**400):
            control = self.control()
            for observation in control["observations"]:
                observation["elapsed_seconds"] = elapsed
            self.verify(control)

    def test_success_requires_exact_status_booleans_scope_and_empty_errors(
        self,
    ) -> None:
        for key, values in {
            "scope": ("control-only", "", None),
            "status": ("failed", "Passed", "", None, True),
            "mutation_attempted": (False, 1, "true", None),
            "restored": (False, 1, "true", None),
            "same_session": (False, 1, "true", None),
            "primary_error": ("failed", " ", None, [], False),
            "cleanup_error": ("failed", " ", None, [], False),
        }.items():
            for value in values:
                control = self.control()
                control[key] = value
                before = copy.deepcopy(control)
                with self.subTest(key=key, value=value):
                    self.reject(control)
                    self.assertEqual(control, before)

    def test_failed_cleanup_restored_on_cannot_be_upgraded(self) -> None:
        for status in ("failed", "passed"):
            for primary_error in ("", "original failure"):
                control = self.control()
                control["status"] = status
                control["primary_error"] = primary_error
                control["observations"].append(
                    {
                        "action": "cleanup-restored-on",
                        "elapsed_seconds": 6,
                        "identity": None,
                        "switch_value": "1",
                    }
                )
                before = copy.deepcopy(control)
                with self.subTest(status=status, primary_error=primary_error):
                    self.reject(control)
                    self.assertEqual(control, before)
        control = self.control()
        control["observations"][-1]["action"] = "cleanup-restored-on"
        self.reject(control)

    def test_attachment_strict_json_rejects_duplicates_constants_and_nonobjects(
        self,
    ) -> None:
        for kind, document in (("window", self.window()), ("control", self.control())):
            text = json.dumps(document)
            duplicates = [
                text.replace(
                    '"schema_version": 1', '"schema_version": 1, "schema_version": 1'
                ),
                text.replace('"pid":', '"pid": 1, "pid":', 1),
                text.replace('"nonce":', '"nonce": "a-b-c", "nonce":', 1),
                text.replace(
                    '"fixture_receipt_sha256":',
                    '"fixture_receipt_sha256": "'
                    + self.receipt
                    + '", "fixture_receipt_sha256":',
                    1,
                ),
            ]
            if kind == "control":
                duplicates.append(
                    text.replace(
                        '"elapsed_seconds": 0.0',
                        '"elapsed_seconds": 0.0, "elapsed_seconds": 0.0',
                    )
                )
            for raw in (*duplicates, "[]", "null", "{", '{"x":NaN}', '{"x":Infinity}'):
                with (
                    self.subTest(kind=kind, raw=raw),
                    self.assertRaises(ledger.EvidenceError),
                ):
                    parsed = ledger._json(raw)
                    if kind == "window":
                        ledger.verify_window_ready(parsed, RUN, self.receipt)
                    else:
                        self.verify(parsed)
            negative_zero_pid = text.replace(
                '"pid": ' + str(self.identity["native"]["pid"]), '"pid": -0'
            )
            with (
                self.subTest(kind=kind, negative_zero_pid=True),
                self.assertRaises(ledger.EvidenceError),
            ):
                parsed = ledger._json(negative_zero_pid)
                if kind == "window":
                    ledger.verify_window_ready(parsed, RUN, self.receipt)
                else:
                    self.verify(parsed)


if __name__ == "__main__":
    unittest.main()
