"""Run the single-owner active iOS BLeng system-loss HIL attempt.

The runner is the only host coordinator. The provider owns the DK and GAP
receipt, the signed XCTest target owns Settings, and the App owns BLeng. The
runner only relays bounded files between the provider's local coordination
folder and the App's Documents folder; it never invents BLE IDs or RF events.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Protocol

from tests.peripherals.nrf52840 import provider, system_loss
from tests.peripherals.nrf52840.cancellation_control import write_document
from tests.peripherals.nrf52840.fixture_session import prepare_session, verify_receipt
from tests.peripherals.nrf52840.profile import load_profile
from tools import ble_sniffer
from tools.apple import ios_preflight, system_loss_host, test_ios_runtime
from tools.local_config import resolve_local_value
from tools.react_native import ios_sdk
from tools.react_native.ios_radio_lease import NATIVE_SYSTEM_LOSS_SCOPE, IosRadioLease

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / ".work" / "apple-bleng-ios-system-loss"
UI_TARGET = "BlengAppleIOSProbeUITests"
UI_ACTIVE = (
    UI_TARGET
    + "/SystemLossBluetoothSettingsUITests/testSystemLossBluetoothOffAndAuthorizedOn"
)
UI_PREFLIGHT = (
    UI_TARGET
    + "/SystemLossBluetoothSettingsUITests/testSystemLossSettingsAutomationPreflight"
)
REMOTE_PREFIX = "Documents/bleng-rf-"
# The provider keeps its independent 8s ARM-originated RF deadline. iOS Settings
# can take longer to complete a real radio readback, so the host mutation/receipt
# budget is separate and remains bounded.
HOST_OFF_BUDGET_SECONDS = 30.0


class SystemLossRunnerError(RuntimeError):
    """The attempt could not satisfy its identity or phase contract."""


class Relay(Protocol):
    """Bounded file relay used by the live runner and no-device tests."""

    def push(self, name: str) -> None:
        """Copy a local coordination file into the App Documents directory."""

    def pull(self, name: str, *, required: bool = True) -> bool:
        """Copy one App Documents file into the local coordination directory."""

    def push_ui(self, name: str) -> None:
        """Copy one host marker into the UI test runner container."""

    def pull_ui(self, name: str, *, required: bool = True) -> bool:
        """Copy one UI test ledger into the local coordination directory."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("ascii")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SystemLossRunnerError(f"cannot read JSON document: {path}") from error
    if not isinstance(value, dict):
        raise SystemLossRunnerError(f"JSON document is not an object: {path}")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    write_document(path, value)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemLossRunnerError(message)


def _resolve_sniffer_match(
    devices: list[dict[str, Any]], *, local_name: str, service_uuid: str
) -> tuple[dict[str, Any], str]:
    """Resolve one sniffer candidate without assuming a Peripheral address.

    Some captures decode the same service UUID on several noisy/randomized
    addresses and omit the advertising name. A strongly dominant packet count is
    sufficient to bind the live advertising source; a weak tie remains a hard
    failure rather than selecting an arbitrary address.
    """
    name_matches = [
        device for device in devices if local_name in device.get("names", [])
    ]
    candidates = name_matches or [
        device
        for device in devices
        if service_uuid.casefold() in device.get("service_uuids", [])
    ]
    if not candidates:
        raise SystemLossRunnerError(
            "sniffer discovery did not resolve one Peripheral address"
        )
    if len(candidates) == 1:
        return candidates[0], "local-name" if name_matches else "attested-service-uuid"
    ranked = sorted(
        candidates,
        key=lambda device: int(device.get("packets", 0)),
        reverse=True,
    )
    top_packets = int(ranked[0].get("packets", 0))
    next_packets = int(ranked[1].get("packets", 0))
    if top_packets >= 8 and top_packets >= next_packets * 4:
        return ranked[0], "attested-service-uuid-dominant-packet-count"
    raise SystemLossRunnerError(
        "sniffer discovery resolved multiple non-dominant Peripheral addresses"
    )


_RELAY_MARKERS = {"settings-ready", "off-authorized", "restore-on-authorized"}


def _relay_name(name: str) -> str:
    _require(
        name.endswith((".json", ".txt")) or name in _RELAY_MARKERS,
        f"unsupported relay filename: {name}",
    )
    _require("/" not in name and "\\" not in name, "relay filename must be flat")
    return name


_FAILURE_DIAGNOSTIC_FILES = (
    "failure-receipt.json",
    "files-init-failure-receipt.json",
    "function-entered-receipt.json",
    "witness-created-receipt.json",
    "attempt-created-receipt.json",
    "files-init-started-receipt.json",
    "files-init-complete-receipt.json",
    "supervisor-started-receipt.json",
    "supervisor-complete-receipt.json",
    "supervisor-failure-receipt.json",
    "launch-receipt.json",
    "scene-receipt.json",
    "task-receipt.json",
    "entered-receipt.json",
    "system-loss-loss-started.json",
    "system-loss-diagnostic.json",
    "system-loss-progress.json",
    "system-loss-recovery-diagnostic.json",
    "system-loss-fresh-owner-recovery.json",
)


def _pull_failure_diagnostics(relay: Relay | None) -> None:
    """Pull optional App diagnostics when a relay was created before failure."""
    if relay is None:
        return
    for name in _FAILURE_DIAGNOSTIC_FILES:
        relay.pull(name, required=False)


def _wait_until(
    label: str, timeout: float, check: Any, *, interval: float = 0.1
) -> Any:
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            result = check()
            if result is not None and result is not False:
                return result
        except (OSError, ValueError, json.JSONDecodeError) as error:
            last_error = error
        time.sleep(interval)
    detail = f": {last_error}" if last_error is not None else ""
    raise TimeoutError(f"timed out waiting for {label}{detail}")


class DeviceRelay:
    """Relay regular files through the App data container using devicectl."""

    def __init__(
        self,
        device: test_ios_runtime.IOSDevice,
        bundle_id: str,
        ui_bundle_id: str,
        run_id: str,
        local: Path,
        output: Path,
    ) -> None:
        self.device = device
        self.bundle_id = bundle_id
        self.ui_bundle_id = ui_bundle_id
        self.run_id = run_id
        self.local = local
        self.output = output
        self.remote_root = f"{REMOTE_PREFIX}{run_id}"
        self.local.mkdir(parents=True, exist_ok=True)
        self.output.mkdir(parents=True, exist_ok=True)

    def _remote(self, name: str) -> str:
        return f"{self.remote_root}/{_relay_name(name)}"

    def _copy_to(self, name: str, bundle_id: str, label: str) -> None:
        source = self.local / name
        _require(
            source.is_file() and not source.is_symlink(),
            f"missing relay input: {source}",
        )
        result = self.output / f"push-{label}-{name}.json"
        command = ios_sdk.devicectl_copy_to(
            self.device.core_device_id,
            source,
            self._remote(name),
            bundle_id,
            result,
            timeout=30,
        )
        completed = ios_sdk.run_command(command, cwd=ROOT, timeout=30, check=False)
        if completed.returncode:
            raise SystemLossRunnerError(
                f"devicectl copy-to failed for {name}: {completed.stdout[-1000:]}"
            )
        ios_sdk.official_result(
            result, "devicectl.device.copy.to", self.device.core_device_id
        )

    def push(self, name: str) -> None:
        self._copy_to(name, self.bundle_id, "app")

    def push_ui(self, name: str) -> None:
        self._copy_to(name, self.ui_bundle_id, "ui")

    def _copy_from(
        self, name: str, bundle_id: str, label: str, *, required: bool
    ) -> bool:
        destination = self.local / name
        temporary = self.local / f".{name}.{uuid.uuid4().hex}.tmp"
        result = self.output / f"pull-{label}-{name}.json"
        temporary.unlink(missing_ok=True)
        command = ios_sdk.devicectl_copy_from(
            self.device.core_device_id,
            self._remote(name),
            bundle_id,
            temporary,
            result,
            timeout=30,
        )
        completed = ios_sdk.run_command(command, cwd=ROOT, timeout=30, check=False)
        if completed.returncode != 0 or not temporary.is_file():
            temporary.unlink(missing_ok=True)
            if required:
                raise SystemLossRunnerError(
                    f"devicectl could not pull {name}: {completed.stdout[-1000:]}"
                )
            return False
        ios_sdk.official_result(
            result, "devicectl.device.copy.from", self.device.core_device_id
        )
        temporary.replace(destination)
        return True

    def pull(self, name: str, *, required: bool = True) -> bool:
        return self._copy_from(name, self.bundle_id, "app", required=required)

    def pull_ui(self, name: str, *, required: bool = True) -> bool:
        return self._copy_from(name, self.ui_bundle_id, "ui", required=required)


class LocalRelay:
    """Filesystem relay used to test phase mapping without a device or xcrun."""

    def __init__(self, local: Path, remote: Path) -> None:
        self.local = local
        self.remote = remote
        self.local.mkdir(parents=True, exist_ok=True)
        self.remote.mkdir(parents=True, exist_ok=True)

    def push(self, name: str) -> None:
        _relay_name(name)
        source = self.local / name
        _require(source.is_file(), f"missing local relay input: {source}")
        temporary = self.remote / f".{name}.{uuid.uuid4().hex}.tmp"
        shutil.copyfile(source, temporary)
        temporary.replace(self.remote / name)

    def pull(self, name: str, *, required: bool = True) -> bool:
        _relay_name(name)
        source = self.remote / name
        if not source.is_file():
            if required:
                raise SystemLossRunnerError(f"missing remote relay input: {source}")
            return False
        temporary = self.local / f".{name}.{uuid.uuid4().hex}.tmp"
        shutil.copyfile(source, temporary)
        temporary.replace(self.local / name)
        return True

    def push_ui(self, name: str) -> None:
        self.push(name)

    def pull_ui(self, name: str, *, required: bool = True) -> bool:
        return self.pull(name, required=required)


def stage_xctestrun(
    source: Path,
    destination: Path,
    *,
    app: Path,
    bundle_id: str,
    environment: dict[str, str],
) -> Path:
    """Pair a signed UI-test manifest with the exact signed HIL App.

    Only the existing UI target is accepted. Existing values for runner-owned
    environment keys must agree; a stale manifest is never silently repaired.
    """
    _require(
        source.is_file() and not source.is_symlink(), f"missing xctestrun: {source}"
    )
    _require(app.is_dir() and not app.is_symlink(), f"missing signed App: {app}")
    _require((app / "embedded.mobileprovision").is_file(), "App is not signed")
    try:
        document = plistlib.loads(source.read_bytes())
    except (OSError, plistlib.InvalidFileException) as error:
        raise SystemLossRunnerError(f"invalid xctestrun plist: {source}") from error
    target = document.get(UI_TARGET)
    _require(isinstance(target, dict), f"xctestrun lacks {UI_TARGET}")
    target = dict(target)
    products_root = source.parent.resolve()
    test_host = (
        products_root / "Debug-iphoneos" / "BlengAppleIOSProbeUITests-Runner.app"
    )

    def resolve_product_path(value: object, *, host: bool = False) -> object:
        if not isinstance(value, str):
            return value
        if host:
            return value.replace("__TESTHOST__", str(test_host))
        return value.replace("__TESTROOT__", str(products_root))

    if "TestBundlePath" in target:
        target["TestBundlePath"] = resolve_product_path(
            target["TestBundlePath"], host=True
        )
    if "TestHostPath" in target:
        target["TestHostPath"] = resolve_product_path(target["TestHostPath"])
    target["UITargetAppPath"] = str(app.resolve())
    target["UITargetAppBundleIdentifier"] = bundle_id
    target["UseUITargetAppProvidedByTests"] = False
    if isinstance(target.get("DependentProductPaths"), list):
        target["DependentProductPaths"] = [
            resolve_product_path(value) for value in target["DependentProductPaths"]
        ]
    current = target.get("EnvironmentVariables", {})
    _require(isinstance(current, dict), "xctestrun runner environment is not an object")
    merged = dict(current)
    for key, value in environment.items():
        _require(
            key not in merged or merged[key] == value,
            f"stale xctestrun value for {key}",
        )
        merged[key] = value
    target["EnvironmentVariables"] = merged
    document[UI_TARGET] = target
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(plistlib.dumps(document, sort_keys=False))
    return destination


def _xctestrun_test_host_bundle_id(source: Path) -> str:
    try:
        document = plistlib.loads(source.read_bytes())
        target = document.get(UI_TARGET)
        value = (
            target.get("TestHostBundleIdentifier") if isinstance(target, dict) else None
        )
    except (OSError, plistlib.InvalidFileException, AttributeError) as error:
        raise SystemLossRunnerError("invalid xctestrun test-host identity") from error
    _require(
        isinstance(value, str) and value, "xctestrun test-host bundle ID is required"
    )
    return value


def _validate_setup(setup: dict[str, Any], plan: Any, case: str) -> None:
    _require(
        setup.get("schema_version") == 1
        and setup.get("session_id") == plan.session_id
        and setup.get("case") == case,
        "App setup has the wrong session or case",
    )
    _require(isinstance(setup.get("identity"), dict), "App setup identity is missing")
    _require(isinstance(setup.get("route"), dict), "App setup route is missing")
    for key in ("write_before", "write_requested"):
        _require(isinstance(setup.get(key), str), f"App setup lacks {key}")


def _validate_witness(witness: dict[str, Any], control: dict[str, Any]) -> None:
    _require(
        witness.get("schema_version") == 1
        and witness.get("control_sha256") == system_loss.control_digest(control),
        "App witness has the wrong control digest",
    )
    _require(
        isinstance(witness.get("identity"), dict), "App witness identity is missing"
    )
    events = witness.get("events")
    _require(isinstance(events, list) and 1 <= len(events) <= 6, "witness event bound")


def _provider_ready(
    physical: dict[str, Any], control: dict[str, Any], state: str
) -> dict[str, Any]:
    _require(
        physical.get("schema_version") == 1
        and physical.get("scope") == "system-loss-provider-only"
        and physical.get("session_id") == control["session_id"]
        and physical.get("control_sha256") == system_loss.control_digest(control)
        and physical.get("state") == state
        and physical.get("radio_authorized") is False
        and physical.get("recovery") == "not-evaluated"
        and physical.get("primary_error") is None,
        f"provider did not publish fresh {state} evidence",
    )
    binding = physical.get("binding")
    _require(isinstance(binding, dict), "provider binding is missing")
    _require(
        physical.get("binding_sha256") == system_loss.binding_digest(binding),
        "provider binding digest drift",
    )
    _require(
        set(binding)
        >= {
            "head_command_id",
            "sibling_command_id",
            "subscription_id",
            "witness_sha256",
        },
        "provider binding fields are incomplete",
    )
    return binding


def _project_receipt(
    receipt: dict[str, Any], binding: dict[str, Any], control: dict[str, Any]
) -> dict[str, Any]:
    status = receipt.get("status")
    _require(
        receipt.get("schema_version") == 1
        and receipt.get("outcome") == "disconnected-uncommitted"
        and receipt.get("control_sha256") == system_loss.control_digest(control)
        and receipt.get("binding_sha256") == system_loss.binding_digest(binding)
        and receipt.get("binding") == binding
        and isinstance(status, dict),
        "provider disconnect receipt identity drift",
    )
    # poll_disconnect writes this file only after asserting DISCONNECT and the
    # exact DISCONNECTED_UNCOMMITTED status. The relay still receives a narrow,
    # independently hashed host receipt rather than trusting a local state name.
    return {
        "schema_version": 1,
        "state": "disconnected-retired",
        "outcome": "disconnected-uncommitted",
        "control_sha256": system_loss.control_digest(control),
        "binding_sha256": system_loss.binding_digest(binding),
        "witness_sha256": binding["witness_sha256"],
    }


def _validate_loss_started(
    loss_started: dict[str, Any],
    run_id: str,
    control: dict[str, Any],
    witness: dict[str, Any],
    identity: dict[str, Any],
) -> None:
    """Validate App authorization observation before any radio mutation.

    This is a phase witness, not an operation terminal. Requiring the App to
    export it before the UI test receives OFF authorization keeps the App in
    the foreground while it consumes the loss command and prevents Settings
    backgrounding from racing that handoff.
    """
    _require(
        set(loss_started)
        == {
            "schema_version",
            "state",
            "session_id",
            "control_sha256",
            "witness_sha256",
            "identity",
            "authorization",
        },
        "App loss-started schema",
    )
    _require(
        loss_started["schema_version"] == 1
        and loss_started["state"] == "loss-started"
        and loss_started["session_id"] == run_id
        and loss_started["authorization"] == "host-authorized",
        "App loss-started phase or session drift",
    )
    _require(
        loss_started["control_sha256"] == system_loss.control_digest(control),
        "App loss-started control digest drift",
    )
    _require(
        loss_started["witness_sha256"] == system_loss.witness_digest(witness),
        "App loss-started witness digest drift",
    )
    observed_identity = loss_started["identity"]
    _require(
        isinstance(observed_identity, dict)
        and observed_identity == witness.get("identity")
        and {
            key: observed_identity.get(key)
            for key in ("pid", "boot_id", "owner_id", "backend_id")
        }
        == identity,
        "App loss-started owner identity drift",
    )


def _validate_terminals(
    terminals: dict[str, Any],
    run_id: str,
    control: dict[str, Any],
    witness: dict[str, Any],
) -> list[dict[str, Any]]:
    _require(
        terminals.get("schema_version") == 1
        and terminals.get("state") == "loss-terminals"
        and terminals.get("session_id") == run_id
        and terminals.get("control_sha256") == system_loss.control_digest(control)
        and terminals.get("witness_sha256") == system_loss.witness_digest(witness),
        "App terminal document identity drift",
    )
    values = terminals.get("terminals")
    _require(isinstance(values, list) and values, "App terminal document is empty")
    for value in values:
        _require(
            isinstance(value, dict)
            and set(value) == {"command_id", "result"}
            and isinstance(value["command_id"], str)
            and value["command_id"].isdigit()
            and int(value["command_id"]) > 0
            and value["result"]
            in {
                "connection-lost",
                "watchdog-timeout",
                "operation-aborted",
                "backend-error",
            },
            "invalid App terminal document",
        )
    return values


def _lease_control_without_host(
    run_id: str, fixture_receipt_sha256: str, error: BaseException
) -> dict[str, Any]:
    """Close a pre-mutation failure before SystemLossHost could be created."""
    return {
        "schema_version": 1,
        "scope": "ios-bleng-system-loss-control",
        "run_id": run_id,
        "fixture_receipt_sha256": fixture_receipt_sha256,
        "status": "failed",
        "mutation_attempted": False,
        "restored": False,
        "same_session": False,
        "observations": [],
        "primary_error": str(error),
        "cleanup_error": "",
    }


def _validate_fresh_owner_recovery(
    recovery: dict[str, Any],
    run_id: str,
    control: dict[str, Any],
    terminals: dict[str, Any],
    old_identity: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    _require(
        recovery.get("schema_version") == 1
        and recovery.get("state") == "fresh-owner-recovered"
        and recovery.get("recovery_mode") == "fresh-owner"
        and recovery.get("session_id") == run_id
        and recovery.get("control_sha256") == system_loss.control_digest(control)
        and recovery.get("terminals_sha256") == _digest(terminals),
        "App fresh-owner recovery document identity drift",
    )
    observed_old = recovery.get("old_identity")
    observed_new = recovery.get("new_identity")
    _require(
        isinstance(observed_old, dict)
        and isinstance(observed_new, dict)
        and observed_old == old_identity,
        "App fresh-owner old identity drift",
    )
    for identity in (observed_old, observed_new):
        _require(
            set(identity) == {"pid", "boot_id", "owner_id", "backend_id"},
            "App fresh-owner identity schema",
        )
    _require(
        observed_new["pid"] == old_identity["pid"]
        and observed_new["boot_id"] == old_identity["boot_id"]
        and observed_new["owner_id"] != old_identity["owner_id"]
        and observed_new["backend_id"] != old_identity["backend_id"],
        "App fresh-owner identity was not a same-process new owner",
    )
    values = recovery.get("old_terminals")
    _require(
        isinstance(values, list) and values,
        "App fresh-owner terminal document is empty",
    )
    for value in values:
        _require(
            isinstance(value, dict)
            and set(value) == {"command_id", "result"}
            and isinstance(value["command_id"], str)
            and value["command_id"].isdigit()
            and int(value["command_id"]) > 0
            and value["result"]
            in {
                "connection-lost",
                "watchdog-timeout",
                "operation-aborted",
                "backend-error",
            },
            "invalid App fresh-owner terminal",
        )
    link = recovery.get("new_link")
    _require(
        isinstance(link, dict)
        and set(link) == {"connection_id", "gatt_session_id", "subscription_id"},
        "App fresh-owner recovery link schema",
    )
    for value in link.values():
        _require(
            isinstance(value, str) and value.isdigit() and int(value) > 0,
            "invalid fresh-owner recovery link ID",
        )
    return observed_old, observed_new, link


def _validate_recovery(
    recovery: dict[str, Any],
    run_id: str,
    control: dict[str, Any],
    terminals: dict[str, Any],
) -> dict[str, str]:
    _require(
        recovery.get("schema_version") == 1
        and recovery.get("state") == "same-owner-recovered"
        and recovery.get("session_id") == run_id
        and recovery.get("control_sha256") == system_loss.control_digest(control)
        and recovery.get("terminals_sha256") == _digest(terminals),
        "App recovery document identity drift",
    )
    link = recovery.get("new_link")
    _require(
        isinstance(link, dict)
        and set(link) == {"connection_id", "gatt_session_id", "subscription_id"},
        "App recovery link schema",
    )
    for value in link.values():
        _require(
            isinstance(value, str) and value.isdigit() and int(value) > 0,
            "invalid recovery link ID",
        )
    return link


class RelayedRadioSwitch:
    """FileRadioSwitch backed by the device Documents relay."""

    def __init__(
        self, relay: Relay, coordination: Path, run_id: str, timeout: float
    ) -> None:
        self.relay = relay
        self.local = system_loss_host.FileRadioSwitch(
            coordination,
            run_id=run_id,
            timeout_seconds=timeout,
            refresh=lambda: self.relay.pull_ui("system-loss-ui.json", required=False),
        )

    def read(self) -> str:
        self.relay.pull_ui("system-loss-ui.json", required=False)
        return self.local.read()

    def _authorize_and_wait(self, name: str, action: str, expected: str) -> None:
        self.local._authorize(name, action)
        self.relay.push_ui(name)
        self.local._wait_for(expected)

    def set_off(self) -> None:
        self._authorize_and_wait("off-authorized", "off-authorized", "0")

    def set_on(self) -> None:
        self._authorize_and_wait("restore-on-authorized", "restore-on-authorized", "1")


def _start_provider(
    plan: Any, coordination: Path, port: str, log: Path
) -> tuple[subprocess.Popen[str], tuple[int, int]]:
    log.parent.mkdir(parents=True, exist_ok=True)
    stream = log.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-m",
            "tests.peripherals.nrf52840.provider",
            "direct-start",
            "--port",
            port,
            "--coordination-path",
            str(coordination),
            "--coordination-mode",
            "system-loss-attested",
            "--fixture-request",
            str(plan.request_path),
        ],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=stream,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        text=True,
    )
    stream.close()

    def ready() -> tuple[int, int] | None:
        identity = provider._process_identity()
        if identity is None:
            if process.poll() is not None:
                raise SystemLossRunnerError(
                    f"provider exited before fixture identity; see {log}"
                )
            return None
        if not plan.receipt_path.is_file():
            return None
        verify_receipt(plan, fixture_identity=identity)
        return identity

    identity = _wait_until(
        "nRF provider receipt", provider.DEFAULT_READY_TIMEOUT + 30, ready
    )
    return process, identity


def _stop_provider(identity: tuple[int, int]) -> None:
    if provider._process_identity() != identity:
        raise SystemLossRunnerError("refusing to stop a foreign nRF provider")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.peripherals.nrf52840.provider",
            "stop-owned",
            "--pid",
            str(identity[0]),
            "--pgid",
            str(identity[1]),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=35,
    )
    if result.returncode != 0:
        raise SystemLossRunnerError(
            f"provider cleanup failed: {(result.stdout + result.stderr)[-2000:]}"
        )


def _start_sniffer(
    *,
    port: str,
    nrfutil_home: str,
    output: Path,
    log: Path,
    follow: str | None = None,
    follow_by_name: str | None = None,
) -> subprocess.Popen[str]:
    command = [
        sys.executable,
        "-m",
        "tools.ble_sniffer",
        "capture",
        "--port",
        port,
        "--output-pcap-file",
        str(output),
        "--nrfutil-home",
        nrfutil_home,
    ]
    if follow:
        command.extend(("--follow", follow))
    elif follow_by_name:
        command.extend(("--follow-by-name", follow_by_name, "--scan-follow-rsp"))
    log.parent.mkdir(parents=True, exist_ok=True)
    stream = log.open("w", encoding="utf-8")
    try:
        return subprocess.Popen(
            command,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )
    except BaseException:
        stream.close()
        raise


def _stop_sniffer(process: subprocess.Popen[str]) -> int:
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGINT)
    try:
        return process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        return process.wait(timeout=15)


def _install_app(device: test_ios_runtime.IOSDevice, app: Path, output: Path) -> None:
    command = ios_sdk.devicectl_install_app(device.core_device_id, app, output)
    ios_sdk.run_command(command, cwd=ROOT, timeout=120, check=True)
    ios_sdk.official_result(
        output, "devicectl.device.install.app", device.core_device_id
    )
    print("bleng_ios_system_loss_app_install=passed", flush=True)


def _run_xctest(
    xctestrun: Path,
    device: test_ios_runtime.IOSDevice,
    result: Path,
    log: Path,
    *,
    only_testing: str = UI_TARGET,
) -> subprocess.Popen[str]:
    stream = log.open("w", encoding="utf-8")
    process = subprocess.Popen(
        ios_sdk.xcodebuild_test_without_building(
            xctestrun=xctestrun,
            device=device.core_device_id,
            result=result,
            only_testing=only_testing,
        ),
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=stream,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        text=True,
    )
    stream.close()
    return process


def _run_ui_automation_preflight(
    *,
    source_xctestrun: Path,
    app: Path,
    app_bundle_id: str,
    device: test_ios_runtime.IOSDevice,
    attempt: Path,
) -> None:
    staged = stage_xctestrun(
        source_xctestrun,
        attempt / "ui-automation-preflight.xctestrun",
        app=app,
        bundle_id=app_bundle_id,
        environment={
            "BLEHUB_SYSTEM_LOSS_UI_PREFLIGHT": "1",
            "BLEHUB_SYSTEM_LOSS_UI_APP_BUNDLE_ID": app_bundle_id,
        },
    )
    result = attempt / "ui-automation-preflight.xcresult"
    log = attempt / "ui-automation-preflight.log"
    command = ios_sdk.xcodebuild_test_without_building(
        xctestrun=staged,
        device=device.core_device_id,
        result=result,
        only_testing=UI_PREFLIGHT,
        launcher="xcodebuild",
        quiet=True,
    )
    completed = ios_sdk.run_logged(
        command,
        cwd=ROOT,
        log=log,
        timeout=120,
        check=False,
    )
    _require(
        completed.returncode == 0,
        "iOS UI automation preflight failed; see " + str(log),
    )
    summary_command = [
        "xcrun",
        "xcresulttool",
        "get",
        "test-results",
        "summary",
        "--path",
        str(result),
        "--compact",
    ]
    summary_result = ios_sdk.run_command(
        summary_command, cwd=ROOT, timeout=30, check=False
    )
    _require(summary_result.returncode == 0, "UI preflight result summary failed")
    try:
        summary = json.loads(summary_result.stdout)
    except json.JSONDecodeError as error:
        raise SystemLossRunnerError("UI preflight summary was not JSON") from error
    _require(
        summary.get("totalTestCount") == 1
        and summary.get("passedTests") == 1
        and summary.get("failedTests") == 0
        and summary.get("skippedTests") == 0,
        "UI automation preflight did not pass exactly one test",
    )
    print("bleng_ios_system_loss_ui_automation_preflight=passed", flush=True)


def run(
    contract: Path,
    profile: Path,
    *,
    port: str,
    app: Path,
    xctestrun: Path,
    case: str,
    device: str | None = None,
    timeout: float = 300.0,
    sniffer_port: str | None = None,
    sniffer_nrfutil_home: str | None = None,
    sniffer_output: Path | None = None,
) -> Path:
    """Run one active system-loss case and return its evidence directory."""
    _require(
        case in {system_loss.WRITE_CCC_ENABLE, system_loss.CCC_DISABLE_WRITE},
        "unsupported system-loss case",
    )
    _require(0 < timeout <= 600, "timeout must be in (0, 600]")
    xcrun = shutil.which("xcrun")
    _require(xcrun is not None, "xcrun is required")
    selected = test_ios_runtime._connected_ios_device(xcrun)
    if device is not None:
        _require(
            device in {selected.core_device_id, selected.udid, selected.name},
            "requested iOS device is not the connected device",
        )
    preflight = ios_preflight.preflight(
        mode="prebuilt",
        app=str(app),
        device=selected.udid,
        xctestrun=str(xctestrun),
    )
    _require(
        preflight["status"] == "verified",
        "iOS UI preflight blocked: " + json.dumps(preflight, sort_keys=True),
    )
    artifact = preflight.get("observations", {}).get("artifact", {})
    app_bundle_id = artifact.get("bundle_id") if isinstance(artifact, dict) else None
    _require(
        isinstance(app_bundle_id, str) and app_bundle_id,
        "iOS preflight did not return the signed App bundle identifier",
    )

    run_id = str(uuid.uuid4())
    attempt = WORK / run_id
    coordination = attempt / "coordination"
    fixture_session = coordination / "fixture-session"
    attempt.mkdir(parents=True, mode=0o700, exist_ok=False)
    coordination.mkdir(mode=0o700)
    profile_data = load_profile(profile)
    plan = prepare_session(
        contract,
        profile,
        fixture_session,
        port=port,
        service_uuid=profile_data.service_uuid,
        session_id=run_id,
    )
    lease = IosRadioLease(
        selected.udid,
        run_id,
        attempt / "ios-phone-lease.json",
        scope=NATIVE_SYSTEM_LOSS_SCOPE,
    )
    lease.acquire()
    provider_process: subprocess.Popen[str] | None = None
    provider_identity: tuple[int, int] | None = None
    sniffer_process: subprocess.Popen[str] | None = None
    sniffer_discovery_process: subprocess.Popen[str] | None = None
    sniffer_output_path: Path | None = None
    sniffer_follow_address: str | None = None
    xctest_process: subprocess.Popen[str] | None = None
    host: system_loss_host.SystemLossHost | None = None
    cleanup_errors: list[str] = []
    primary_error: BaseException | None = None
    relay: Relay | None = None
    try:
        lease.require_owner()
        resolved_port: str | None = None
        resolved_home: Path | None = None
        if sniffer_port is not None and sniffer_port.strip():
            resolved_port = ble_sniffer.resolve_port(sniffer_port)
            resolved_home = ble_sniffer.resolve_nrfutil_home(sniffer_nrfutil_home)
            sniffer_output_path = (
                sniffer_output.absolute()
                if sniffer_output is not None
                else attempt / "air-traffic.pcap"
            )
            if sniffer_output_path.exists() or sniffer_output_path.is_symlink():
                raise SystemLossRunnerError(
                    f"refusing existing sniffer PCAP: {sniffer_output_path}"
                )
        _install_app(selected, app, attempt / "install.json")
        _run_ui_automation_preflight(
            source_xctestrun=xctestrun,
            app=app,
            app_bundle_id=app_bundle_id,
            device=selected,
            attempt=attempt,
        )
        ui_bundle_id = _xctestrun_test_host_bundle_id(xctestrun)
        relay = DeviceRelay(
            selected,
            app_bundle_id,
            ui_bundle_id,
            run_id,
            coordination,
            attempt / "relay",
        )
        ui_environment = {
            "BLEHUB_SYSTEM_LOSS_UI_RUN": "1",
            "BLEHUB_SYSTEM_LOSS_UI_COORDINATION": "__TEST_RUNNER_DOCUMENTS__",
            "BLEHUB_SYSTEM_LOSS_UI_APP_BUNDLE_ID": app_bundle_id,
            "BLEHUB_SYSTEM_LOSS_UI_RUN_ID": run_id,
            "BLEHUB_SYSTEM_LOSS_CASE": case,
            "BLEHUB_SYSTEM_LOSS_SERVICE_UUID": plan.service_uuid,
            "BLEHUB_SYSTEM_LOSS_NAME_PREFIX": profile_data.local_name,
            "BLEHUB_SYSTEM_LOSS_WRITE_BEFORE": "00",
            "BLEHUB_SYSTEM_LOSS_WRITE_REQUESTED": "10",
        }
        staged = stage_xctestrun(
            xctestrun,
            attempt / "system-loss.xctestrun",
            app=app,
            bundle_id=app_bundle_id,
            environment=ui_environment,
        )
        provider_process, provider_identity = _start_provider(
            plan, coordination, port, attempt / "provider.log"
        )
        if resolved_port is not None and resolved_home is not None:
            discovery = attempt / "air-discovery.pcap"
            sniffer_discovery_process = _start_sniffer(
                port=resolved_port,
                nrfutil_home=str(resolved_home),
                output=discovery,
                log=attempt / "sniffer-discovery.log",
            )
            time.sleep(10)
            _stop_sniffer(sniffer_discovery_process)
            sniffer_discovery_process = None
            devices = ble_sniffer.inspect_pcap_devices(discovery)
            selected_device, discovery_match = _resolve_sniffer_match(
                devices,
                local_name=profile_data.local_name,
                service_uuid=profile_data.service_uuid,
            )
            _require(
                isinstance(selected_device.get("address"), str),
                "sniffer discovery candidate has no Peripheral address",
            )
            sniffer_follow_address = str(selected_device["address"])
            sniffer_process = _start_sniffer(
                port=resolved_port,
                nrfutil_home=str(resolved_home),
                output=sniffer_output_path,
                log=attempt / "sniffer.log",
                follow=sniffer_follow_address,
            )
            _write_json(
                attempt / "sniffer-request.json",
                {
                    "schema_version": 1,
                    "run_id": run_id,
                    "port": resolved_port,
                    "nrfutil_home": str(resolved_home),
                    "discovery_pcap": str(discovery),
                    "follow_address": sniffer_follow_address,
                    "discovery_match": discovery_match,
                    "pcap": str(sniffer_output_path),
                    "metadata": str(ble_sniffer.metadata_path(sniffer_output_path)),
                },
            )
        xctest_process = _run_xctest(
            staged,
            selected,
            attempt / "system-loss.xcresult",
            attempt / "xctest.log",
            only_testing=UI_ACTIVE,
        )

        _wait_until(
            "App setup",
            timeout,
            lambda: (
                relay.pull("system-loss-setup.json", required=False)
                and _read_object(coordination / "system-loss-setup.json")
            ),
        )
        setup = _read_object(coordination / "system-loss-setup.json")
        _validate_setup(setup, plan, case)
        native_identity = {
            key: setup["identity"][key]
            for key in ("pid", "boot_id", "owner_id", "backend_id")
        }
        control = system_loss.prepare_control(
            coordination,
            plan,
            case=case,
            identity=setup["identity"],
            route=setup["route"],
            baseline_command_id=setup.get("baseline_command_id"),
            baseline_subscription_id=setup.get("baseline_subscription_id"),
            write_before=bytes.fromhex(setup["write_before"]),
            write_requested=bytes.fromhex(setup["write_requested"]),
        )
        relay.push(system_loss.CONTROL_NAME)

        def pull_base_witness() -> bool | None:
            if not relay.pull("system-loss-witness.json", required=False):
                return None
            _validate_witness(
                _read_object(coordination / "system-loss-witness.json"), control
            )
            return True

        _wait_until("App base witness", timeout, pull_base_witness)
        # The provider's relay has a distinct filename so App and provider can
        # retain their own immutable prefixes without sharing a writer.
        _write_json(
            coordination / "system-loss-base-witness.json",
            _read_object(coordination / "system-loss-witness.json"),
        )

        _wait_until(
            "provider ARMED",
            timeout,
            lambda: (
                _read_object(coordination / system_loss.PHYSICAL_NAME).get("state")
                == "armed"
                if (coordination / system_loss.PHYSICAL_NAME).is_file()
                else None
            ),
        )
        relay.push(system_loss.PHYSICAL_NAME)

        def pull_pending_witness() -> bool | None:
            if not relay.pull("system-loss-witness.json", required=False):
                return None
            witness = _read_object(coordination / "system-loss-witness.json")
            _validate_witness(witness, control)
            expected_events = 6 if case == system_loss.CCC_DISABLE_WRITE else 4
            return True if len(witness.get("events", [])) == expected_events else None

        _wait_until("App pending witness", timeout, pull_pending_witness)
        _write_json(
            coordination / "system-loss-pending-witness.json",
            _read_object(coordination / "system-loss-witness.json"),
        )

        def held() -> dict[str, Any] | None:
            path = coordination / system_loss.PHYSICAL_NAME
            if not path.is_file():
                return None
            physical = _read_object(path)
            if physical.get("state") != "held-pending":
                return None
            _provider_ready(physical, control, "held-pending")
            return physical

        physical = _wait_until("provider HELD", timeout, held)
        # The App has its own matching-HELD gate before it can authorize loss.
        # Relay this exact provider document after local validation; the App must
        # observe the same physical identity, not infer HELD from host state.
        relay.push(system_loss.PHYSICAL_NAME)

        def initial_ui() -> bool | None:
            if not relay.pull_ui("system-loss-ui.json", required=False):
                return None
            ledger = _read_object(coordination / "system-loss-ui.json")
            return (
                True
                if system_loss_host.verify_ui_ledger(ledger, run_id=run_id) == "1"
                and ledger["status"] == "initial-on"
                else None
            )

        _wait_until("initial UI ledger", timeout, initial_ui)
        binding = physical["binding"]
        host = system_loss_host.SystemLossHost(
            run_id=run_id,
            case=case,
            identity=native_identity,
            switch=RelayedRadioSwitch(relay, coordination, run_id, timeout),
        )

        def publish_host() -> None:
            assert host is not None
            _write_json(coordination / "system-loss-host.json", host.document())

        pending = _read_object(coordination / "system-loss-witness.json")
        pending_digest = system_loss.witness_digest(pending)
        host.bind_pending(
            control_sha256=system_loss.control_digest(control),
            binding_sha256=physical["binding_sha256"],
            witness_sha256=pending_digest,
            old_command_ids=[binding["head_command_id"], binding["sibling_command_id"]],
            old_link={
                "connection_id": setup["route"]["connection_id"],
                "gatt_session_id": setup["route"]["gatt_session_id"],
                "subscription_id": binding["subscription_id"],
            },
            off_budget_seconds=HOST_OFF_BUDGET_SECONDS,
        )
        publish_host()
        # This App authorization is not radio authorization. Send it while
        # provider is still HELD so the App can consume it in the foreground;
        # the provider's short OFF retirement window starts only below, after
        # this witness has been observed.
        _write_json(
            coordination / "system-loss-authorize.json",
            host.loss_authorization_command(),
        )
        relay.push("system-loss-authorize.json")

        def app_observed_loss_authorization() -> bool | None:
            if not relay.pull("system-loss-loss-started.json", required=False):
                return None
            loss_started = _read_object(coordination / "system-loss-loss-started.json")
            _validate_loss_started(
                loss_started,
                run_id,
                control,
                pending,
                native_identity,
            )
            return True

        _wait_until(
            "App loss authorization observation",
            timeout,
            app_observed_loss_authorization,
        )

        # Let the UI test activate the already-open Bluetooth page before the
        # provider's short retirement window starts. This phase does not mutate
        # radio state; it only proves the semantic switch is ready to tap.
        settings_ready = {
            "schema_version": 1,
            "run_id": run_id,
            "action": "settings-ready",
        }
        _write_json(coordination / "settings-ready", settings_ready)
        relay.push_ui("settings-ready")

        def ui_settings_ready() -> bool | None:
            if not relay.pull_ui("settings-ready", required=False):
                return None
            observed = _read_object(coordination / "settings-ready")
            _require(observed == settings_ready, "stale/foreign Settings-ready marker")
            return True

        _wait_until("Bluetooth Settings page ready", timeout, ui_settings_ready)

        # Only now authorize the provider's OFF phase. This keeps its bounded
        # disconnect-retirement deadline around the actual UI mutation rather
        # than spending it on foreground App coordination.
        off_request = {
            "schema_version": 1,
            "control_sha256": system_loss.control_digest(control),
            "binding_sha256": physical["binding_sha256"],
            "witness_sha256": pending_digest,
        }
        _write_json(coordination / "system-loss-off-request.json", off_request)
        _wait_until(
            "provider OFF readiness",
            timeout,
            lambda: (
                _provider_ready(
                    _read_object(coordination / system_loss.PHYSICAL_NAME),
                    control,
                    "off-ready",
                )
                if (coordination / system_loss.PHYSICAL_NAME).is_file()
                and _read_object(coordination / system_loss.PHYSICAL_NAME).get("state")
                == "off-ready"
                else None
            ),
        )
        relay.push(system_loss.PHYSICAL_NAME)
        provider_ready = {
            "schema_version": 1,
            "state": "off-ready",
            "radio_authorized": False,
            "control_sha256": system_loss.control_digest(control),
            "binding_sha256": physical["binding_sha256"],
            "witness_sha256": pending_digest,
        }
        host.authorize_off(provider_ready)
        publish_host()
        host.perform_off()
        publish_host()

        receipt_path = coordination / system_loss.RECEIPT_NAME
        _wait_until(
            "provider disconnect receipt",
            timeout,
            lambda: _read_object(receipt_path) if receipt_path.is_file() else None,
        )
        retained = _read_object(receipt_path)
        receipt = _project_receipt(retained, binding, control)
        host.record_disconnect(receipt)
        publish_host()

        def pull_loss_evidence() -> dict[str, Any] | None:
            # Loss-started is written immediately after the App consumes the
            # host authorization. Pull it independently of terminal delivery so
            # a later timeout still proves whether the App entered the loss phase.
            relay.pull("system-loss-loss-started.json", required=False)
            relay.pull("system-loss-diagnostic.json", required=False)
            relay.pull("system-loss-recovery-diagnostic.json", required=False)
            relay.pull("system-loss-fresh-owner-recovery.json", required=False)
            if not relay.pull("system-loss-terminals.json", required=False):
                return None
            return _read_object(coordination / "system-loss-terminals.json")

        _wait_until("App loss terminals", timeout, pull_loss_evidence)
        terminals_document = _read_object(coordination / "system-loss-terminals.json")
        terminals = _validate_terminals(terminals_document, run_id, control, pending)
        host.restore_on()
        publish_host()
        _write_json(
            coordination / "system-loss-restore.json",
            host.restoration_command(_digest(terminals_document)),
        )
        relay.push("system-loss-restore.json")

        def pull_recovery() -> tuple[str, dict[str, Any]] | None:
            if relay.pull("system-loss-recovery.json", required=False):
                return (
                    "same-owner",
                    _read_object(coordination / "system-loss-recovery.json"),
                )
            if relay.pull("system-loss-fresh-owner-recovery.json", required=False):
                return (
                    "fresh-owner",
                    _read_object(
                        coordination / "system-loss-fresh-owner-recovery.json"
                    ),
                )
            if relay.pull("system-loss-recovery-diagnostic.json", required=False):
                diagnostic = _read_object(
                    coordination / "system-loss-recovery-diagnostic.json"
                )
                outcome = diagnostic.get("recovery_outcome")
                if outcome in {"platform-timeout", "manual-reconnect-required"}:
                    return (outcome, diagnostic)
                raise SystemLossRunnerError(
                    "App automatic recovery failed: "
                    + json.dumps(diagnostic, sort_keys=True)
                )
            return None

        recovery_mode, recovery = _wait_until(
            "App BLE recovery", timeout, pull_recovery
        )
        terminal_recovery_error: SystemLossRunnerError | None = None
        if recovery_mode == "same-owner":
            new_link = _validate_recovery(recovery, run_id, control, terminals_document)
            host.record_recovery(
                identity=native_identity, old_terminals=terminals, new_link=new_link
            )
        elif recovery_mode == "fresh-owner":
            old_identity, new_identity, new_link = _validate_fresh_owner_recovery(
                recovery,
                run_id,
                control,
                terminals_document,
                native_identity,
            )
            host.record_fresh_owner_recovery(
                old_identity=old_identity,
                new_identity=new_identity,
                old_terminals=terminals,
                new_link=new_link,
            )
        else:
            reason = recovery.get("recovery_failure_reason")
            _require(
                isinstance(reason, str) and bool(reason),
                "recovery terminal reason is missing",
            )
            host.record_recovery_terminal(
                outcome=recovery_mode,
                reason=reason,
                diagnostic=recovery,
            )
            terminal_recovery_error = SystemLossRunnerError(
                f"system-loss gate not passed: {recovery_mode}: {reason}"
            )
        publish_host()
        _write_json(
            coordination / system_loss_attested_finish_name(),
            {"schema_version": 1, "session_id": run_id, "state": "finished"},
        )
        if xctest_process.wait(timeout=timeout) != 0:
            raise SystemLossRunnerError(
                f"system-loss XCTest failed; see {attempt / 'xctest.log'}"
            )
        _wait_until(
            "restored UI ledger",
            timeout,
            lambda: (
                relay.pull_ui("system-loss-ui.json", required=False)
                and system_loss_host.verify_ui_ledger(
                    _read_object(coordination / "system-loss-ui.json"), run_id=run_id
                )
                == "1"
            ),
        )
        if terminal_recovery_error is not None:
            raise terminal_recovery_error
        host.finish()
        publish_host()
        return attempt
    except Exception as error:
        primary_error = error
        try:
            _pull_failure_diagnostics(relay)
        except (
            OSError,
            subprocess.SubprocessError,
            TimeoutError,
            ValueError,
        ) as diagnostic_error:
            cleanup_errors.append(f"loss diagnostic relay: {diagnostic_error}")
        if host is not None:
            host.fail(error)
            if (
                host.mutation_attempted
                and host.state in {"radio-unknown", "radio-off", "disconnected"}
                and host.on_count == 0
            ):
                try:
                    host.restore_on()
                except (OSError, RuntimeError, TimeoutError, ValueError) as cleanup:
                    cleanup_errors.append(f"radio restoration: {cleanup}")
            publish_host()
        raise
    finally:
        if sniffer_discovery_process is not None:
            try:
                _stop_sniffer(sniffer_discovery_process)
            except (OSError, subprocess.SubprocessError, TimeoutError) as error:
                cleanup_errors.append(f"sniffer discovery cleanup: {error}")
        if sniffer_process is not None:
            try:
                returncode = _stop_sniffer(sniffer_process)
                _write_json(
                    attempt / "sniffer-finish.json",
                    {
                        "schema_version": 1,
                        "run_id": run_id,
                        "returncode": returncode,
                        "pcap": str(sniffer_output_path),
                        "follow_address": sniffer_follow_address,
                        "pcap_exists": bool(
                            sniffer_output_path and sniffer_output_path.is_file()
                        ),
                        "metadata": str(ble_sniffer.metadata_path(sniffer_output_path))
                        if sniffer_output_path is not None
                        else None,
                    },
                )
            except (OSError, subprocess.SubprocessError, TimeoutError) as error:
                cleanup_errors.append(f"sniffer cleanup: {error}")
        if xctest_process is not None and xctest_process.poll() is None:
            try:
                os.killpg(xctest_process.pid, signal.SIGTERM)
                xctest_process.wait(timeout=10)
            except (OSError, subprocess.SubprocessError, TimeoutError) as error:
                cleanup_errors.append(f"XCTest cleanup: {error}")
        if provider_identity is not None:
            try:
                _stop_provider(provider_identity)
            except (
                OSError,
                subprocess.SubprocessError,
                TimeoutError,
                RuntimeError,
            ) as error:
                cleanup_errors.append(f"provider cleanup: {error}")
        if provider_process is not None:
            try:
                provider_process.wait(timeout=10)
            except (OSError, subprocess.SubprocessError, TimeoutError) as error:
                cleanup_errors.append(f"provider launcher cleanup: {error}")
                if provider_process.poll() is None:
                    try:
                        os.killpg(provider_process.pid, signal.SIGTERM)
                        provider_process.wait(timeout=5)
                    except (
                        OSError,
                        subprocess.SubprocessError,
                        TimeoutError,
                    ) as forced:
                        cleanup_errors.append(
                            f"provider launcher forced cleanup: {forced}"
                        )
        if host is not None:
            lease_control = (
                host.lease_control(
                    hashlib.sha256(plan.receipt_path.read_bytes()).hexdigest()
                )
                if plan.receipt_path.is_file()
                else None
            )
        else:
            fixture_receipt_sha256 = (
                hashlib.sha256(plan.receipt_path.read_bytes()).hexdigest()
                if plan.receipt_path.is_file()
                else hashlib.sha256(b"").hexdigest()
            )
            lease_control = _lease_control_without_host(
                run_id,
                fixture_receipt_sha256,
                primary_error
                or RuntimeError("system-loss failed before host creation"),
            )
        try:
            if lease_control is None:
                cleanup_errors.extend(lease.finish(None, None))
            else:
                raw_control = (
                    json.dumps(lease_control, sort_keys=True) + "\n"
                ).encode()
                lease.finish(lease_control, hashlib.sha256(raw_control).hexdigest())
        except (OSError, RuntimeError, TimeoutError, ValueError) as error:
            cleanup_errors.append(f"phone lease cleanup: {error}")


def system_loss_attested_finish_name() -> str:
    return "system-loss-finish.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--port", required=True)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--xctestrun", type=Path, required=True)
    parser.add_argument(
        "--case",
        choices=[system_loss.WRITE_CCC_ENABLE, system_loss.CCC_DISABLE_WRITE],
        required=True,
    )
    parser.add_argument(
        "--device",
        default=resolve_local_value(
            "apple", "device", "BLEHUB_APPLE_DEVICE", None
        ).value,
    )
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--sniffer-port", default="")
    parser.add_argument("--sniffer-nrfutil-home", default="")
    parser.add_argument("--sniffer-output", type=Path)
    args = parser.parse_args()
    try:
        attempt = run(
            args.contract,
            args.profile,
            port=args.port,
            app=args.app,
            xctestrun=args.xctestrun,
            case=args.case,
            device=args.device,
            timeout=args.timeout,
            sniffer_port=args.sniffer_port,
            sniffer_nrfutil_home=args.sniffer_nrfutil_home,
            sniffer_output=args.sniffer_output,
        )
    except (OSError, RuntimeError, TimeoutError, ValueError) as error:
        print(f"bleng_ios_system_loss=failed error={error}", file=sys.stderr)
        return 2
    print(f"bleng_ios_system_loss=finished evidence={attempt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
