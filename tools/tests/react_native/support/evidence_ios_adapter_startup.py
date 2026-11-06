"""Complete cooperative synthetic bundle, not physical radio evidence."""

from __future__ import annotations

import hashlib
import json
import plistlib
from pathlib import Path

from tests.peripherals.nrf52840 import ios_adapter_startup as provider
from tools.react_native import ios_adapter_startup_evidence as owner
from tools.react_native import ios_adapter_startup_ledger as ledger
from tools.react_native import rf_evidence_bundle as bundle_api
from tools.react_native.host_contract import BUNDLE_ID, SCHEME
from tools.react_native.ios_adapter_startup_journal import PREFIX, parse_journal
from tools.tests.react_native.support.ios_adapter_startup_samples import (
    DEVICE,
    SERVICE,
    fake_app,
    journal_document,
    passed_control,
    put,
    summary_document,
)


class StartupBundle:
    """Build every proof from source records using real pure verifiers, no mocks."""

    def __init__(
        self,
        root: Path,
        *,
        service_uuid: str = SERVICE,
        native_pid: int = 123,
        boot_id: str | None = None,
        nonce: str | None = None,
        executable: str | None = None,
    ) -> None:
        self.root = root
        self.service = service_uuid
        self.attempt = root / "attempt"
        self.bundle = bundle_api.EvidenceBundle(
            self.attempt,
            platform="ios",
            device=DEVICE,
            project_root=root,
            scenario=owner.SCENARIO,
        )
        self.run = self.bundle.document["run_id"]
        app = fake_app(root)
        self.frozen = self.bundle.prepare_artifact(app)
        self.log = self.attempt / owner.LOG
        self.journal = journal_document(
            self.run,
            service_uuid=self.service,
            native_pid=native_pid,
            boot_id=boot_id,
            nonce=nonce,
            executable=executable,
        )
        raw = self.write("journal", self.journal)
        self.runtime = ledger.verify_runtime(parse_journal(raw, self.run), self.service)
        off_document = {**self.journal, "records": self.journal["records"][:8]}
        off_raw = self.write("off_journal", off_document)
        self.off = ledger.verify_off_journal(parse_journal(off_raw, self.run))
        request = {
            "schema_version": 1,
            "session_id": self.run,
            "service_uuid": self.service,
            "contract_sha256": "ab" * 32,
            "profile_sha256": "cd" * 32,
            "resource_identity": "jlink:synthetic",
            "port": "/dev/fake-only",
        }
        self.write("fixture_request", request)
        installed = {
            "schema_version": 1,
            "image_id": "synthetic",
            "image_version": "2",
            "image_sha256": "ef" * 32,
            "board": "nrf52840dk",
            "profile_id": "synthetic",
            "service_uuid": self.service,
            "local_name": "BleHubNrf52840",
            "capabilities": [],
        }
        receipt = {
            **{k: v for k, v in request.items() if k != "service_uuid"},
            "proof_method": "btp-vendor-live-under-device-lease",
            "installed_identity": installed,
            "fixture_pid": 456,
            "fixture_pgid": 455,
        }
        self.receipt_hash = hashlib.sha256(
            self.write("fixture_receipt", receipt)
        ).hexdigest()
        ready = {
            "schema_version": 1,
            "run_id": self.run,
            "status": "ready",
            "receipt_sha256": self.receipt_hash,
            "native_bootstrap": self.runtime["native_bootstrap"],
            "runtime_nonce": self.runtime["runtime_nonce"],
            "off_journal_sha256": self.off["journal_sha256"],
            "monotonic": 20.0,
        }
        self.write("readiness", ready)
        events = [
            {
                "kind": kind,
                "peer": ["112233445566", 1],
                "payload_hex": "01665544332211" + suffix,
                "monotonic": t,
            }
            for kind, suffix, t in (
                ("connected", "00" * 6, 21.0),
                ("disconnected", "", 22.0),
            )
        ]
        boundaries = [
            {
                "boundary": "prepared",
                "monotonic": 10.0,
                "hold_inactive": True,
                "peer_count": 0,
            },
            {
                "boundary": "on-window-ready",
                "monotonic": 20.0,
                "hold_inactive": True,
                "peer_count": 0,
            },
            *({"boundary": "gap-" + event["kind"], **event} for event in events),
            {
                "boundary": "physical-closed",
                "monotonic": 23.0,
                "hold_inactive": True,
                "peer_count": 0,
            },
        ]
        physical = {
            **request,
            "mode": provider.MODE,
            "run_id": self.run,
            "status": "passed",
            "fixture_pid": 456,
            "fixture_pgid": 455,
            "receipt_sha256": self.receipt_hash,
            "off": self.off,
            "readiness": ready,
            "resources": self.runtime["closed_resources"],
            "runtime_journal_sha256": self.runtime["journal_sha256"],
            "native_bootstrap": self.runtime["native_bootstrap"],
            "runtime_nonce": self.runtime["runtime_nonce"],
            "primary_error": None,
            "cleanup_errors": [],
            "rejected_events": [],
            "cleanup": {
                "advertising_stopped": True,
                "hold_inactive": True,
                "peer_count": 0,
            },
            "hold_status": {
                "protocol_version": 2,
                "engine_flags": 0,
                "boot_nonce": "01" * 16,
            },
            "handles": {"read-write": 51},
            "connections": 1,
            "disconnections": 1,
            "events": events,
            "boundaries": boundaries,
        }
        self.write("physical", physical)
        self.identity = {
            "native": self.runtime["native_bootstrap"],
            "run_id": self.run,
            "nonce": self.runtime["runtime_nonce"],
        }
        control = passed_control(self.identity, self.receipt_hash)
        control_raw = self.write("control", control)
        control_audit = ledger.verify_control(
            control, runtime=self.runtime, receipt_sha256=self.receipt_hash
        )
        physical_audit = provider.verify_physical(
            physical,
            request=request,
            receipt=receipt,
            receipt_sha256=self.receipt_hash,
            runtime=self.runtime,
            readiness=ready,
        )
        self.write(
            "ledger",
            {
                "schema_version": 1,
                "scope": "ios-adapter-startup-host-audit",
                "run_id": self.run,
                "runtime": self.runtime,
                "control": control_audit,
                "physical": physical_audit,
            },
        )
        window = {
            "schema_version": 1,
            "run_id": self.run,
            "fixture_receipt_sha256": self.receipt_hash,
            "identity": self.identity,
        }
        marker = owner.WINDOW_PREFIX + json.dumps(window) + "\n"
        self.write("window_log", marker.encode())
        self.write(
            "raw_runtime", ("XCTest output\n" + marker + "Test passed\n").encode()
        )
        environment = {
            "BLEHUB_IOS_ADAPTER_STARTUP_GATE": "1",
            "BLEHUB_IOS_ADAPTER_RUN_ID": self.run,
            "BLEHUB_IOS_ADAPTER_SERVICE_UUID": self.service,
            "BLEHUB_IOS_ADAPTER_LOCAL_NAME": "BleHubNrf52840",
            "BLEHUB_IOS_ADAPTER_FIXTURE_RECEIPT_SHA256": self.receipt_hash,
        }
        target = {
            "IsUITestBundle": True,
            "OnlyTestIdentifiers": [owner.TEST_METHOD],
            "SkipTestIdentifiers": [],
            "UITargetAppPath": str(self.frozen),
            "UITargetAppBundleIdentifier": BUNDLE_ID,
            "DependentProductPaths": [str(self.frozen), str(root / "UIRunner.app")],
            "EnvironmentVariables": environment,
        }
        staged = self.write(
            "test_run",
            plistlib.dumps({owner.TEST_TARGET: target, "__xctestrun_metadata__": {}}),
        )
        command = [
            "xcrun",
            "xcodebuild",
            "-xctestrun",
            str(self.path("test_run")),
            "-destination",
            "id=" + DEVICE,
            "-resultBundlePath",
            str(self.log.with_suffix(".xcresult")),
            "-parallel-testing-enabled",
            "NO",
            "-collect-test-diagnostics",
            "never",
            "-only-testing:" + owner.TEST_TARGET + "/" + owner.TEST_METHOD,
            "test-without-building",
        ]
        self.write(
            "runtime_capture",
            {
                "schema_version": 1,
                "platform": "ios",
                "device": DEVICE,
                "run_id": self.run,
                "scope": "ios-adapter-startup-window-candidate",
                "fixture_receipt_sha256": self.receipt_hash,
                "status": "window-completed-awaiting-evidence",
                "ready_delivered": True,
                "primary_error": None,
                "cleanup_errors": [],
                "actions": [],
                "window": window,
                "capture_pid": 999,
                "test_run_sha256": hashlib.sha256(staged).hexdigest(),
                "command": command,
            },
        )
        executable, pid = (
            self.runtime["native_bootstrap"]["executable"],
            self.runtime["pid"],
        )
        self.official(
            "device_install",
            "devicectl.device.install.app",
            [str(self.frozen)],
            {
                "installedApplications": [
                    {
                        "bundleID": BUNDLE_ID,
                        "installationURL": executable.removesuffix(SCHEME),
                    }
                ]
            },
        )
        for role in ("ready_before_processes", "startup_ready_after_processes"):
            self.official(
                role,
                "devicectl.device.info.processes",
                [],
                {
                    "runningProcesses": [
                        {"processIdentifier": pid, "executable": executable}
                    ]
                },
            )
        self.official(
            "startup_ready_payload",
            "devicectl.device.process.launch",
            ["--payload-url", self.runtime["native_ready"]["url"], BUNDLE_ID],
            {"process": {"processIdentifier": pid, "executable": executable}},
        )
        for role, destination in (
            ("journal_copy", "journal"),
            ("off_journal_copy", "off_journal"),
        ):
            self.official(
                role,
                "devicectl.device.copy.from",
                [
                    "--source",
                    f"Library/Caches/blehub-ios-adapter/{self.run}.json",
                    "--domain-type",
                    "appDataContainer",
                    "--domain-identifier",
                    BUNDLE_ID,
                    "--destination",
                    str(self.path(destination)),
                ],
                {},
            )
        self.write("summary", summary_document())
        self.attachment = (
            self.attempt
            / "xcresult-attachments/01234567-ABCD-ABCD-ABCD-0123456789AB.json"
        )
        put(self.attachment, control_raw)
        self.write(
            "attachments",
            [
                {
                    "testIdentifier": owner.ATTACHMENT_METHOD,
                    "attachments": [
                        {
                            "deviceId": DEVICE,
                            "isAssociatedWithFailure": False,
                            "exportedFileName": self.attachment.name,
                            "suggestedHumanReadableName": "ios-adapter-startup-control_0_01234567-ABCD-ABCD-ABCD-0123456789AB.json",
                        }
                    ],
                }
            ],
        )
        self.write(
            "phone_lease",
            {
                "schema_version": 1,
                "platform": "ios",
                "device": DEVICE,
                "run_id": self.run,
                "host_pid": 456,
                "status": "released",
                "scope": "ios-adapter-startup-phone-owner",
                "control_sha256": hashlib.sha256(control_raw).hexdigest(),
                "restoration": "on-readback",
                "error": None,
                "acquired_monotonic": 1.0,
                "released_monotonic": 30.0,
            },
        )

    def path(self, role: str) -> Path:
        return self.attempt / owner.names()[role]

    def write(self, role: str, value: object) -> bytes:
        return put(self.path(role), value)

    def load(self, role: str) -> dict:
        return json.loads(self.path(role).read_bytes())

    def official(
        self, role: str, kind: str, arguments: list[str], result: dict
    ) -> None:
        self.write(
            role,
            {
                "info": {
                    "outcome": "success",
                    "commandType": kind,
                    "arguments": [*kind.split("."), "--device", DEVICE, *arguments],
                },
                "result": {
                    "deviceIdentifier": "official-coredevice-identifier",
                    **result,
                },
            },
        )

    def finish(
        self,
        *,
        completed: bool = True,
        error: BaseException | None = None,
        cleanup: list[str] | None = None,
    ) -> None:
        self.bundle.finish(
            completed=completed,
            primary_error=error,
            cleanup_errors=cleanup or [],
            runtime_log=self.log,
        )

    def reindex(self) -> dict:
        """Refresh integrity metadata to challenge semantics rather than stale hashes."""
        document = self.bundle.document
        document["files"] = bundle_api._inventory(self.attempt)
        document["references"] = bundle_api._references(
            document["files"], "ios", owner.LOG, owner.SCENARIO
        )
        document["flow_index"] = bundle_api._flow_index(
            self.log, owner.LOG, owner.SCENARIO
        )
        bundle_api._publish(self.attempt / bundle_api.MANIFEST, document)
        return document

    def edit_journal(self, prefix: str, key: str, value: object) -> None:
        index = next(
            i
            for i, r in enumerate(self.journal["records"])
            if r.startswith(PREFIX + prefix)
        )
        record = json.loads(self.journal["records"][index][len(PREFIX + prefix) :])
        record[key] = value
        self.journal["records"][index] = PREFIX + prefix + json.dumps(record)
        self.write("journal", self.journal)
