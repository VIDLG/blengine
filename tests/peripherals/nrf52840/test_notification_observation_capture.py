"""Synthetic host capture integration; no firmware build/provision, C crossing or RF."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from itertools import pairwise
from pathlib import Path

from tests.peripherals.nrf52840 import ccc_disable_reject as ccc
from tests.peripherals.nrf52840 import notification_observation as wire
from tests.peripherals.nrf52840 import notification_observation_capture as api
from tests.peripherals.nrf52840.cancellation_control import write_document
from tests.peripherals.nrf52840.support import (
    notification_observation_samples as samples,
)
from tests.peripherals.nrf52840.support.ccc_disable_reject_emulator import Harness
from tests.peripherals.nrf52840.support.notification_observation_capture import (
    attach,
    synthetic_profile_admission,
)
from tests.peripherals.nrf52840.support.notification_observation_samples import OWNER


class NotificationObservationCaptureTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(synthetic_profile_admission())

    def make(self, *, fault="", opt=True, owner=OWNER, generation=7):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        h = Harness(Path(temporary.name), fault=fault)
        observer = attach(h, owner=owner, generation=generation) if opt else None
        return h, observer

    def load(self, h):
        return json.loads((h.root / api.CAPTURE_NAME).read_bytes())

    def test_default_off_uses_no_observer_commands_or_files(self):
        h, _ = self.make(opt=False)
        h.run()
        self.assertFalse((h.root / api.CAPTURE_NAME).exists())
        self.assertFalse(
            any(c.service == 255 and c.opcode in (32, 33, 34) for c in h.wire.commands)
        )
        api.verify_capture(h.root, complete=True)

    def test_existing_session_explicit_rotation_deferred_reads_and_final_owned_off(
        self,
    ):
        for owner, generation in ((OWNER, 7), (wire.ZERO_OWNER, 0)):
            with self.subTest(owner=owner.hex()):
                h, observer = self.make(owner=owner, generation=generation)
                session = h.fixture._session
                h.run()
                self.assertIs(session, h.fixture._session)
                doc = self.load(h)
                self.assertEqual(doc["status"], "complete")
                self.assertFalse(observer.enabled)
                self.assertEqual([u["sequence"] for u in doc["updates"]], [1, 2, 3])
                self.assertEqual(
                    h.wire.submissions, [(40, b"\xef"), (40, b"\xe1"), (40, b"\xf0")]
                )
                phases = [e["phase"] for e in doc["events"]]
                if owner != wire.ZERO_OWNER:
                    self.assertEqual(
                        phases[:4],
                        [
                            "initial-query",
                            "handoff-off",
                            "handoff-rotate-off",
                            "enable",
                        ],
                    )
                self.assertIn("FINAL", phases)
                self.assertEqual(phases[-1], "owned-off")
                polls = [e["monotonic"] for e in doc["events"] if e["phase"] == "poll"]
                self.assertTrue(all(b - a >= 0.499 for a, b in pairwise(polls)))
                for update in doc["updates"]:
                    self.assertLessEqual(len(update["reads"]), api.MAX_READS)
                    self.assertTrue(
                        any(
                            doc["events"][r]["phase"].startswith("FINAL:")
                            for r in update["reads"]
                        )
                    )
                api.verify_capture(h.root, complete=True)
                ccc.runtime_facts(h.text, run_id=h.plan.session_id, complete=True)

    def test_failure_final_before_disconnect_and_primary_survives_diagnostic_error(
        self,
    ):
        h, observer = self.make(fault="submission-failure")
        observer.fail_read = True
        with self.assertRaises(RuntimeError) as raised:
            h.run()
        self.assertNotIn("NOT_FOUND", str(raised.exception))
        doc = self.load(h)
        self.assertEqual(
            doc["status"], "unobserved"
        )  # No assigned sequence after failed SET_VALUE.
        self.assertIn("FINAL", [e["phase"] for e in doc["events"]])
        commands = h.wire.commands
        off = max(
            i
            for i, c in enumerate(commands)
            if c.service == 255 and c.opcode == wire.CONFIGURE
        )
        disconnect = next(
            i for i, c in enumerate(commands) if c.service == 1 and c.opcode == 15
        )
        self.assertLess(off, disconnect)
        self.assertEqual(
            json.loads((h.root / ccc.PHYSICAL_NAME).read_bytes())["status"], "failed"
        )
        api.verify_capture(h.root, complete=False)

    def test_partial_rotation_error_retained_without_reset_retry_or_foreign_disable(
        self,
    ):
        h, observer = self.make()
        observer.fail_configure = 2  # Current owner off succeeds, rotation fails.
        with self.assertRaisesRegex(wire.ObservationCommandError, "NOT_READY"):
            h.run()
        doc = self.load(h)
        self.assertEqual(observer.configure_count, 2)
        self.assertEqual(observer.owner, OWNER)
        self.assertFalse(observer.enabled)
        self.assertEqual(doc["status"], "failed")
        self.assertIn("FINAL", [e["phase"] for e in doc["events"]])
        failed = next(e for e in doc["events"] if e["phase"] == "handoff-rotate-off")
        self.assertEqual(failed["wire"]["response_hex"], "0103")
        api.verify_capture(h.root, complete=False)

    def test_runtime_failure_still_finalizes_and_observer_read_error_still_disables_own_generation(
        self,
    ):
        h, observer = self.make()
        wait = h.owner.wait

        def fail(request):
            if request == "arm":
                observer.fail_read = True
                raise RuntimeError("runtime capture failed first")
            return wait(request)

        h.owner.wait = fail
        with self.assertRaisesRegex(
            RuntimeError, "runtime capture failed first"
        ) as raised:
            h.run()
        self.assertIn("NOT_FOUND", str(getattr(raised.exception, "__notes__", [])))
        doc = self.load(h)
        self.assertEqual(doc["status"], "failed")
        self.assertFalse(observer.enabled)
        self.assertEqual(doc["events"][-1]["phase"], "owned-off")
        api.verify_capture(h.root, complete=False)

    def test_unobserved_never_becomes_complete_or_delivery_and_request_drift_fails_closed(
        self,
    ):
        h, observer = self.make()
        observer.omit = True
        h.run()
        self.assertEqual(self.load(h)["status"], "unobserved")
        api.verify_capture(h.root, complete=False)
        with self.assertRaisesRegex(ValueError, "unobserved"):
            api.verify_capture(h.root, complete=True)
        h, observer = self.make()
        original = h.owner.wait

        def drift(request):
            if request == "arm":
                path = h.root / api.REQUEST_NAME
                path.write_bytes(path.read_bytes() + b" ")
            return original(request)

        h.owner.wait = drift
        with self.assertRaisesRegex(ValueError, "request bytes drift"):
            h.run()
        self.assertTrue(observer.enabled)  # Drift never authorizes even an off write.
        self.assertEqual(self.load(h)["status"], "failed")

    def test_offline_wire_and_semantic_drift_rejected_even_with_reencoded_typed_documents(
        self,
    ):
        h, _ = self.make()
        h.run()
        original = self.load(h)
        for field, value in (
            ("sequence", 4),
            ("value_handle", 41),
            ("database_epoch", 99),
            ("owner_generation", 9),
            ("live_peer", "01010203040506"),
        ):
            with self.subTest(field=field):
                doc = copy.deepcopy(original)
                index = doc["updates"][0]["reads"][0]
                doc["events"][index]["wire"]["decoded"][field] = value
                write_document(h.root / api.CAPTURE_NAME, doc)
                with self.assertRaises(ValueError):
                    api.verify_capture(h.root, complete=True)
        # Re-encode internally consistent raw+typed facts: wire replay alone is not
        # enough; the attempt-bound entry snapshot must still reject them.
        for field, value in (
            ("value_handle", 41),
            ("database_epoch", 99),
            ("owner_generation", 9),
        ):
            doc = copy.deepcopy(original)
            update = doc["updates"][0]
            index = update["reads"][0]
            raw = bytes.fromhex(doc["events"][index]["wire"]["response_hex"])
            raw = samples.changed(raw, samples.RECORD_FIELDS, field, value)
            parsed = wire.decode_record(
                raw,
                expected_boot=samples.BOOT,
                record_owner=api.owner_nonce(h.plan.session_id),
                sequence=1,
            )
            doc["events"][index]["wire"] = parsed.document()
            write_document(h.root / api.CAPTURE_NAME, doc)
            with self.assertRaisesRegex(ValueError, "record boot/owner"):
                api.verify_capture(h.root, complete=True)
        for edit in (
            lambda d: d["updates"][1].update(sequence=1),
            lambda d: d["updates"][0]["before"].update(value_handle=41),
            lambda d: d["events"].pop(),
            lambda d: d.update(request_hex="00"),
        ):
            doc = copy.deepcopy(original)
            edit(doc)
            write_document(h.root / api.CAPTURE_NAME, doc)
            with self.assertRaises((ValueError, IndexError)):
                api.verify_capture(h.root, complete=True)

    def test_capability_exact_request_keys_and_same_owner_no_rotation(self):
        h, _ = self.make(opt=False)
        with self.assertRaisesRegex(ValueError, "capability"):
            api.prepare_request(h.root, h.plan)
        for raw in (
            b'{"schema_version":1,"session":"s","request":true,"extra":0}',
            b'{"schema_version":1,"session":"s","request":1}',
            b'{"schema_version":1,"session":"s","request":true,"request":true}',
        ):
            with self.assertRaises((ValueError, RuntimeError)):
                api.request_from_bytes(raw, "s")
        h, observer = self.make()
        observer.owner = api.owner_nonce(h.plan.session_id)
        h.run()
        phases = [e["phase"] for e in self.load(h)["events"]]
        self.assertNotIn("handoff-off", phases)
        self.assertEqual(observer.generation, 7)
