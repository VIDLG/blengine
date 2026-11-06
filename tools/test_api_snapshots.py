"""Fail-closed declaration and component accounting for BLeng API snapshots."""

from __future__ import annotations

import re
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from tools import api_snapshots

BLENG_FILES = (
    "kotlin/dev/blehub/ffi/blehub.kt",
    "kotlin/uniffi/bleng/bleng.kt",
    "swift/blehub.swift",
    "swift/bleng.swift",
    "swift/blehubFFI.h",
    "swift/blengFFI.h",
    "swift/blehubFFI.modulemap",
    "swift/blengFFI.modulemap",
)

# Reviewed contract, deliberately independent of the extractor's allowlist.
# handle, Rust begin, foreign begin, Rust result, Kotlin result, Swift result
TYPED_OPERATIONS = (
    (
        "InitializeOperation",
        "begin_initialize",
        "beginInitialize",
        "AdapterSnapshot",
        "AdapterSnapshot",
        "AdapterSnapshot",
    ),
    (
        "StartScanOperation",
        "begin_start_scan",
        "beginStartScan",
        "ScanSessionId",
        "ScanSessionId",
        "ScanSessionId",
    ),
    ("StopScanOperation", "begin_stop_scan", "beginStopScan", "()", "", ""),
    (
        "ConnectOperation",
        "begin_connect",
        "beginConnect",
        "ConnectionId",
        "ConnectionId",
        "ConnectionId",
    ),
    ("DisconnectOperation", "begin_disconnect", "beginDisconnect", "()", "", ""),
    (
        "DiscoverGattOperation",
        "begin_discover_gatt",
        "beginDiscoverGatt",
        "GattDatabase",
        "GattDatabase",
        "GattDatabase",
    ),
    (
        "ReadOperation",
        "begin_read_characteristic",
        "beginReadCharacteristic",
        "Vec<u8>",
        "kotlin.ByteArray",
        "Data",
    ),
    (
        "WriteOperation",
        "begin_write_characteristic",
        "beginWriteCharacteristic",
        "()",
        "",
        "",
    ),
    (
        "EnableNotificationsOperation",
        "begin_enable_notifications",
        "beginEnableNotifications",
        "SubscriptionId",
        "SubscriptionId",
        "SubscriptionId",
    ),
    (
        "DisableNotificationsOperation",
        "begin_disable_notifications",
        "beginDisableNotifications",
        "()",
        "",
        "",
    ),
    (
        "ReadRssiOperation",
        "begin_read_rssi",
        "beginReadRssi",
        "i16",
        "kotlin.Short",
        "Int16",
    ),
)
OPERATION_FAILURE_SIGNATURE = (
    "pub fn observe_failure(&self, "
    "observer: std::sync::Weak<dyn crate::OperationFailureObserver>,) "
    "-> Option<crate::OperationFailureRegistration>"
)
OPERATION_FAILURE_DECLARATIONS = {
    "OperationFailureObserver",
    "OperationFailureRegistration",
}
OPERATION_FAILURE_SOURCE = """pub trait OperationFailureObserver: Send + Sync {
    fn operation_failed(&self);
}
pub struct OperationFailureRegistration {
    listener: Arc<Listener>,
}
struct Listener;
"""

SCAN_OBSERVATION_SIGNATURE = (
    "pub async fn observe_with_timeout<T: Send + 'static>(&self, timeout_ms: u64, "
    "observation: impl Future<Output = T> + Send + 'static,) -> Result<T, BlengError>"
)

BASE_DECLARATIONS = {
    "Bleng",
    "BlengBusyReason",
    "BlengContractFailure",
    "BlengError",
    "BlengNotReadyReason",
    "BlengResource",
}


def expected_declarations(language: str) -> set[str]:
    names = BASE_DECLARATIONS | {row[0] for row in TYPED_OPERATIONS}
    if language == "rust":
        names |= OPERATION_FAILURE_DECLARATIONS
    elif language == "kotlin":
        names = (names - {"BlengError"}) | {"BlengException", "BlengInterface"}
        names |= {f"{row[0]}Interface" for row in TYPED_OPERATIONS}
    elif language == "swift":
        names |= {"BlengProtocol"}
        names |= {f"{row[0]}Protocol" for row in TYPED_OPERATIONS}
    return names


# Representative UniFFI source, deliberately independent of BLENG_DECLARATIONS.
KOTLIN_SOURCE = """import dev.blehub.ffi.BleBackend
import dev.blehub.ffi.BleEvent
import dev.blehub.ffi.AdapterSnapshot

public interface BlengInterface {
    fun `beginInitialize`(): InitializeOperation
    fun `beginStartScan`(`options`: ScanOptions): StartScanOperation
    fun `beginStopScan`(`session`: ScanSessionId): StopScanOperation
    fun `beginConnect`(`peripheral`: PeripheralId): ConnectOperation
    fun `beginDisconnect`(`connection`: ConnectionId): DisconnectOperation
    fun `beginDiscoverGatt`(`connection`: ConnectionId): DiscoverGattOperation
    fun `beginWriteCharacteristic`(`connection`: ConnectionId, `session`: GattSessionId, `characteristic`: GattCharacteristicId, `value`: kotlin.ByteArray, `writeType`: WriteType): WriteOperation
    fun `beginEnableNotifications`(`connection`: ConnectionId, `session`: GattSessionId, `characteristic`: GattCharacteristicId, `mode`: NotificationModeRequest): EnableNotificationsOperation
    fun `beginDisableNotifications`(`connection`: ConnectionId, `session`: GattSessionId, `subscription`: SubscriptionId): DisableNotificationsOperation
    fun `beginReadRssi`(`connection`: ConnectionId): ReadRssiOperation
    fun `beginReadCharacteristic`(`connection`: ConnectionId, `session`: GattSessionId, `characteristic`: GattCharacteristicId): ReadOperation
    suspend fun `cancelOperation`(`commandId`: CommandId)
}
open class Bleng : BlengInterface {
    constructor() : this(UniffiLib.uniffi_bleng_fn_constructor_bleng_new())
}
public interface ReadOperationInterface {
    fun `commandId`(): CommandId
    suspend fun `cancel`()
    suspend fun `wait`(`timeoutMs`: kotlin.ULong): kotlin.ByteArray
}
open class ReadOperation : ReadOperationInterface {
}
enum class BlengBusyReason {
    SCAN,
    PERIPHERAL,
    GATT,
    OPERATION,
    BACKEND_SLOT_OCCUPIED,
}
enum class BlengContractFailure {
    INVALID_COMMAND_ID,
    DUPLICATE_HUB_OWNERSHIP,
}
enum class BlengNotReadyReason {
    BACKEND_NOT_ASSEMBLED,
    NOT_INITIALIZED,
    SCAN_NOT_ACTIVE,
    CONNECTION_NOT_ACTIVE,
    GATT_NOT_READY,
    SUBSCRIPTION_NOT_ACTIVE,
}
enum class BlengResource {
    PENDING_WAITERS,
    SUBMISSION_QUEUE,
    IDENTIFIERS,
}
sealed class BlengException {
    class Closed() : BlengException()
    class Cancelled() : BlengException()
    data class OperationNotFound(val `commandId`: CommandId) : BlengException()
    data class OperationAlreadyCompleted(val `commandId`: CommandId) : BlengException()
    data class OperationNotCancellable(val `commandId`: CommandId) : BlengException()
    class OperationResultConsumed() : BlengException()
}
"""

SWIFT_SOURCE = """public protocol BlengProtocol: AnyObject, Sendable {
    func beginInitialize() throws -> InitializeOperation
    func beginStartScan(options: ScanOptions) throws -> StartScanOperation
    func beginStopScan(session: ScanSessionId) throws -> StopScanOperation
    func beginConnect(peripheral: PeripheralId) throws -> ConnectOperation
    func beginDisconnect(connection: ConnectionId) throws -> DisconnectOperation
    func beginDiscoverGatt(connection: ConnectionId) throws -> DiscoverGattOperation
    func beginWriteCharacteristic(connection: ConnectionId, session: GattSessionId, characteristic: GattCharacteristicId, value: Data, writeType: WriteType) throws -> WriteOperation
    func beginEnableNotifications(connection: ConnectionId, session: GattSessionId, characteristic: GattCharacteristicId, mode: NotificationModeRequest) throws -> EnableNotificationsOperation
    func beginDisableNotifications(connection: ConnectionId, session: GattSessionId, subscription: SubscriptionId) throws -> DisableNotificationsOperation
    func beginReadRssi(connection: ConnectionId) throws -> ReadRssiOperation
    func beginReadCharacteristic(connection: ConnectionId, session: GattSessionId, characteristic: GattCharacteristicId) throws -> ReadOperation
    func cancelOperation(commandId: CommandId) async throws
}
open class Bleng: BlengProtocol, @unchecked Sendable {
    public convenience init() throws {}
}
public protocol ReadOperationProtocol: AnyObject, Sendable {
    func commandId() -> CommandId
    func cancel() async throws
    func wait(timeoutMs: UInt64) async throws -> Data
}
open class ReadOperation: ReadOperationProtocol, @unchecked Sendable {
}
public enum BlengBusyReason {
    case scan
    case peripheral
    case gatt
    case operation
    case backendSlotOccupied
}
public enum BlengContractFailure {
    case invalidCommandId
    case duplicateHubOwnership
}
public enum BlengNotReadyReason {
    case backendNotAssembled
    case notInitialized
    case scanNotActive
    case connectionNotActive
    case gattNotReady
    case subscriptionNotActive
}
public enum BlengResource {
    case pendingWaiters
    case submissionQueue
    case identifiers
}
public
    enum BlengError {
    case Closed
    case Cancelled
    case OperationNotFound(commandId: CommandId
    )
    case OperationAlreadyCompleted(commandId: CommandId
    )
    case OperationNotCancellable(commandId: CommandId
    )
    case OperationResultConsumed
}
"""


# The existing ReadOperation snippets above also protect the original regression.
for handle, _, _, _, kotlin_result, swift_result in TYPED_OPERATIONS:
    if handle == "ReadOperation":
        continue
    kotlin_return = f": {kotlin_result}" if kotlin_result else ""
    swift_return = f" -> {swift_result}" if swift_result else ""
    KOTLIN_SOURCE += f"""public interface {handle}Interface {{
    fun `commandId`(): CommandId
    suspend fun `cancel`()
    suspend fun `wait`(`timeoutMs`: kotlin.ULong){kotlin_return}
}}
open class {handle}: Disposable, AutoCloseable, {handle}Interface {{
}}
"""
    SWIFT_SOURCE += f"""public protocol {handle}Protocol: AnyObject, Sendable {{
    func commandId() -> CommandId
    func cancel() async throws
    func wait(timeoutMs: UInt64) async throws{swift_return}
}}
open class {handle}: {handle}Protocol, @unchecked Sendable {{
}}
"""


class BlengComponentTests(unittest.TestCase):
    @contextmanager
    def representative_bindings(self) -> Iterator[Path]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in BLENG_FILES:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("generated fixture", encoding="utf-8")
            kotlin = root / BLENG_FILES[1]
            swift = root / BLENG_FILES[3]
            kotlin.write_text(KOTLIN_SOURCE, encoding="utf-8")
            swift.write_text(SWIFT_SOURCE, encoding="utf-8")
            with (
                patch.object(api_snapshots, "BLENG_GENERATED_ROOT", root),
                patch.object(api_snapshots, "BLENG_KOTLIN_BINDING", kotlin),
                patch.object(api_snapshots, "BLENG_SWIFT_BINDING", swift),
            ):
                yield root

    def test_actual_rust_typed_operation_and_cancellation_are_accounted_for(
        self,
    ) -> None:
        snapshot, declarations = api_snapshots._rust_snapshot(bleng=True)
        self.assertIn("ReadOperation", declarations)
        self.assertIn("pub async fn cancel(&self) -> Result<(), BlengError>;", snapshot)
        self.assertIn(
            "pub async fn wait(&self, timeout_ms: u64) -> Result<Vec<u8>, BlengError>;",
            snapshot,
        )
        self.assertIn("pub fn begin_read_characteristic(", snapshot)
        self.assertIn("pub async fn cancel_operation(", snapshot)
        for variant in (
            "OperationNotFound",
            "OperationAlreadyCompleted",
            "OperationNotCancellable",
            "OperationResultConsumed",
        ):
            self.assertIn(variant, snapshot)
        self.assertSetEqual(declarations, expected_declarations("rust"))
        self.assertSetEqual(api_snapshots.BLENG_DECLARATIONS["rust"], declarations)
        for handle, begin, _, result, _, _ in TYPED_OPERATIONS:
            with self.subTest(handle=handle):
                block = snapshot.split(f"impl {handle} {{\n", 1)[1].split("\n}", 1)[0]
                self.assertEqual(
                    block.splitlines(),
                    (
                        [
                            f"    {OPERATION_FAILURE_SIGNATURE};",
                            f"    {SCAN_OBSERVATION_SIGNATURE};",
                        ]
                        if handle == "StartScanOperation"
                        else []
                    )
                    + [
                        "    pub fn command_id(&self) -> CommandId;",
                        "    pub async fn cancel(&self) -> Result<(), BlengError>;",
                        f"    pub async fn wait(&self, timeout_ms: u64) -> Result<{result}, BlengError>;",
                    ],
                )
                self.assertRegex(
                    snapshot,
                    rf"pub fn {begin}\([^\n]+Result<Arc<{handle}>, BlengError>;",
                )
                self.assertNotIn(
                    "timeout_ms",
                    snapshot.split(f"pub fn {begin}(", 1)[1].split(";", 1)[0],
                )
                self.assertNotIn(
                    f"pub async fn {begin}(",
                    snapshot,
                )

    def test_generated_typed_operation_interfaces_are_extracted_and_accounted_for(
        self,
    ) -> None:
        with self.representative_bindings():
            for language, extractor, interface, wait in (
                (
                    "kotlin",
                    api_snapshots._kotlin_snapshot,
                    "ReadOperationInterface",
                    "suspend fun wait(timeoutMs: ULong): ByteArray",
                ),
                (
                    "swift",
                    api_snapshots._swift_snapshot,
                    "ReadOperationProtocol",
                    "func wait(timeoutMs: UInt64) async throws -> Data",
                ),
            ):
                with self.subTest(language=language):
                    snapshot, declarations = extractor(bleng=True)
                    self.assertIn("ReadOperation", declarations)
                    self.assertIn(interface, declarations)
                    self.assertIn(f"class ReadOperation : {interface}", snapshot)
                    self.assertIn(wait, snapshot)
                    self.assertIn("beginReadCharacteristic(", snapshot)
                    self.assertIn("cancelOperation(", snapshot)
                    self.assertIn(
                        "OperationNotCancellable(commandId: CommandId)", snapshot
                    )
                    self.assertSetEqual(declarations, expected_declarations(language))
                    self.assertSetEqual(
                        declarations, api_snapshots.BLENG_DECLARATIONS[language]
                    )
                    suffix = "Interface" if language == "kotlin" else "Protocol"
                    for (
                        handle,
                        _,
                        begin,
                        _,
                        kotlin_result,
                        swift_result,
                    ) in TYPED_OPERATIONS:
                        with self.subTest(handle=handle):
                            name = handle + suffix
                            self.assertIn(f"class {handle} : {name}", snapshot)
                            block = snapshot.split(f"{name} {{\n", 1)[1].split(
                                "\n}", 1
                            )[0]
                            if language == "kotlin":
                                result = kotlin_result.removeprefix("kotlin.")
                                tail = f": {result}" if result else ""
                                methods = [
                                    "    fun commandId(): CommandId",
                                    "    suspend fun cancel()",
                                    f"    suspend fun wait(timeoutMs: ULong){tail}",
                                ]
                                begin_pattern = (
                                    rf"(?m)^    fun {begin}\([^\n]*\): {handle}\n"
                                )
                            else:
                                tail = f" -> {swift_result}" if swift_result else ""
                                methods = [
                                    "    func commandId() -> CommandId",
                                    "    func cancel() async throws",
                                    f"    func wait(timeoutMs: UInt64) async throws{tail}",
                                ]
                                begin_pattern = (
                                    rf"func {begin}\([^\n]*\) throws -> {handle}\n"
                                )
                            self.assertEqual(block.splitlines(), methods)
                            self.assertRegex(snapshot, begin_pattern)
                            self.assertNotIn(
                                "timeoutMs",
                                snapshot.split(f"{begin}(", 1)[1].split(")", 1)[0],
                            )

    def test_rust_only_generic_observation_merges_with_exported_scan_methods(
        self,
    ) -> None:
        source = (
            "impl StartScanOperation {\n"
            "    pub fn observe_failure(\n"
            "        &self, observer: std::sync::Weak<dyn crate::OperationFailureObserver>,\n"
            "    ) -> Option<crate::OperationFailureRegistration> { todo!() }\n"
            "    pub async fn observe_with_timeout<T: Send + 'static>(\n"
            "        &self, timeout_ms: u64,\n"
            "        observation: impl Future<Output = T> + Send + 'static,\n"
            "    ) -> Result<T, BlengError> { todo!() }\n}\n"
            "#[uniffi::export]\nimpl StartScanOperation {\n"
            "    pub fn command_id(&self) -> CommandId { todo!() }\n"
            "    pub async fn cancel(&self) -> Result<(), BlengError> { todo!() }\n"
            "    pub async fn wait(&self, timeout_ms: u64) -> Result<ScanSessionId, BlengError> { todo!() }\n}\n"
        )
        self.assertEqual(
            api_snapshots._rust_public_methods(source, "StartScanOperation"),
            [
                OPERATION_FAILURE_SIGNATURE,
                SCAN_OBSERVATION_SIGNATURE,
                "pub fn command_id(&self) -> CommandId",
                "pub async fn cancel(&self) -> Result<(), BlengError>",
                "pub async fn wait(&self, timeout_ms: u64) -> Result<ScanSessionId, BlengError>",
            ],
        )
        for extractor in (
            api_snapshots._kotlin_snapshot,
            api_snapshots._swift_snapshot,
        ):
            snapshot, _ = extractor(bleng=True)
            self.assertNotIn("observeWithTimeout", snapshot)
            self.assertNotIn("observe_with_timeout", snapshot)
            for name in OPERATION_FAILURE_DECLARATIONS | {
                "observeFailure",
                "observe_failure",
                "operationFailed",
                "operation_failed",
            }:
                self.assertNotIn(name, snapshot)
        with self.representative_bindings():
            snapshots = api_snapshots._generate(bleng=True)
            self.assertIn(SCAN_OBSERVATION_SIGNATURE + ";", snapshots["rust"])
            self.assertIn(OPERATION_FAILURE_SIGNATURE + ";", snapshots["rust"])
            for language in ("kotlin", "swift"):
                self.assertNotIn("observeWithTimeout", snapshots[language])
                self.assertNotIn("observe_with_timeout", snapshots[language])
                for name in OPERATION_FAILURE_DECLARATIONS | {
                    "observeFailure",
                    "observe_failure",
                    "operationFailed",
                    "operation_failed",
                }:
                    self.assertNotIn(name, snapshots[language])

    def test_rust_only_operation_failure_types_keep_trait_shape_and_opaque_token(
        self,
    ) -> None:
        expected = (
            "pub trait OperationFailureObserver: Send + Sync {\n"
            "    fn operation_failed(&self);\n"
            "}\n\n"
            "pub struct OperationFailureRegistration;"
        )
        snapshot, _ = api_snapshots._rust_snapshot(bleng=True)
        self.assertIn(expected, snapshot)
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "operation_observation.rs"
            fixture.write_text(OPERATION_FAILURE_SOURCE, encoding="utf-8")
            with patch.object(api_snapshots, "BLENG_RUST_SOURCES", (fixture,)):
                snapshot, declarations = api_snapshots._rust_snapshot(bleng=True)
            self.assertSetEqual(declarations, OPERATION_FAILURE_DECLARATIONS)
            self.assertEqual(
                snapshot,
                "# Reviewed BLeng public API shape; ABI remains unfrozen.\n\n"
                + expected
                + "\n",
            )

    def test_missing_or_unknown_operation_failure_types_are_rejected(self) -> None:
        with self.representative_bindings(), tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "operation_observation.rs"
            sources = (*api_snapshots.BLENG_RUST_SOURCES[:-1], fixture)
            with patch.object(api_snapshots, "BLENG_RUST_SOURCES", sources):
                for name in sorted(OPERATION_FAILURE_DECLARATIONS):
                    with self.subTest(missing=name):
                        mutated, count = re.subn(
                            rf"(?m)^pub (?:trait|struct) {name}\b[^{{]*\{{[^}}]*\}}",
                            "",
                            OPERATION_FAILURE_SOURCE,
                        )
                        self.assertEqual(count, 1)
                        fixture.write_text(mutated, encoding="utf-8")
                        with self.assertRaisesRegex(
                            RuntimeError, rf"rust.*missing=\['{name}'\]"
                        ):
                            api_snapshots._generate(bleng=True)
                for name, declaration in (
                    ("UnknownScanObserver", "pub struct UnknownScanObserver;\n"),
                    (
                        "ScanStartFailureObserver",
                        (
                            "pub trait ScanStartFailureObserver: Send + Sync {\n"
                            "    fn start_failed(&self);\n}\n"
                        ),
                    ),
                    (
                        "ScanStartFailureRegistration",
                        "pub struct ScanStartFailureRegistration;\n",
                    ),
                ):
                    with self.subTest(unexpected=name):
                        fixture.write_text(
                            OPERATION_FAILURE_SOURCE + declaration, encoding="utf-8"
                        )
                        with self.assertRaisesRegex(
                            RuntimeError, rf"rust.*unexpected=\['{name}'\]"
                        ):
                            api_snapshots._generate(bleng=True)

    def test_rust_only_operation_failure_types_are_rejected_in_foreign_bindings(
        self,
    ) -> None:
        with self.representative_bindings():
            for language, path, source, declarations in (
                (
                    "kotlin",
                    api_snapshots.BLENG_KOTLIN_BINDING,
                    KOTLIN_SOURCE,
                    (
                        "public interface OperationFailureObserver {\n    fun operationFailed()\n}",
                        "open class OperationFailureRegistration {}",
                    ),
                ),
                (
                    "swift",
                    api_snapshots.BLENG_SWIFT_BINDING,
                    SWIFT_SOURCE,
                    (
                        "public protocol OperationFailureObserver {\n    func operationFailed()\n}",
                        "open class OperationFailureRegistration {}",
                    ),
                ),
            ):
                for declaration in declarations:
                    with self.subTest(language=language, declaration=declaration):
                        path.write_text(source + declaration + "\n", encoding="utf-8")
                        with self.assertRaisesRegex(
                            RuntimeError, rf"{language}.*unexpected=\['OperationFailure"
                        ):
                            api_snapshots._generate(bleng=True)
                        path.write_text(source, encoding="utf-8")

    def test_missing_read_operation_allowlist_entry_is_rejected(self) -> None:
        with self.representative_bindings():
            declarations = {
                language: names - {"ReadOperation"}
                for language, names in api_snapshots.BLENG_DECLARATIONS.items()
            }
            with (
                patch.object(api_snapshots, "BLENG_DECLARATIONS", declarations),
                self.assertRaisesRegex(RuntimeError, r"unexpected=\['ReadOperation'\]"),
            ):
                api_snapshots._generate(bleng=True)

    def test_each_typed_declaration_is_required_and_allowlist_is_exact(self) -> None:
        with self.representative_bindings():
            for language, names in (
                (
                    language,
                    expected_declarations(language)
                    - BASE_DECLARATIONS
                    - {"BlengException", "BlengInterface", "BlengProtocol"},
                )
                for language in ("rust", "kotlin", "swift")
            ):
                for name in sorted(names):
                    with self.subTest(language=language, name=name):
                        allowlist = {
                            key: set(value)
                            for key, value in api_snapshots.BLENG_DECLARATIONS.items()
                        }
                        allowlist[language].discard(name)
                        with (
                            patch.object(
                                api_snapshots, "BLENG_DECLARATIONS", allowlist
                            ),
                            self.assertRaisesRegex(
                                RuntimeError, rf"{language}.*unexpected=\['{name}'\]"
                            ),
                        ):
                            api_snapshots._generate(bleng=True)

    def test_missing_or_unknown_rust_handle_is_rejected(self) -> None:
        with self.representative_bindings(), tempfile.TemporaryDirectory() as directory:
            (
                facade_source,
                operations_source,
                error_source,
                operation_observation_source,
            ) = api_snapshots.BLENG_RUST_SOURCES
            source = operations_source.read_text(encoding="utf-8")
            fixture = Path(directory) / "operations.rs"
            with patch.object(
                api_snapshots,
                "BLENG_RUST_SOURCES",
                (facade_source, fixture, error_source, operation_observation_source),
            ):
                for handle, *_ in TYPED_OPERATIONS:
                    with self.subTest(missing=handle):
                        mutated, count = re.subn(
                            rf"(?m)^pub struct {handle}\s*\{{[^}}]*\}}",
                            "",
                            source,
                        )
                        self.assertEqual(count, 1)
                        fixture.write_text(mutated, encoding="utf-8")
                        with self.assertRaisesRegex(
                            RuntimeError, rf"rust.*missing=\['{handle}'\]"
                        ):
                            api_snapshots._generate(bleng=True)
                fixture.write_text(
                    "pub struct UnknownOperation;\n" + source, encoding="utf-8"
                )
                with self.assertRaisesRegex(
                    RuntimeError, r"rust.*unexpected=\['UnknownOperation'\]"
                ):
                    api_snapshots._generate(bleng=True)

            facade_fixture = Path(directory) / "facade.rs"
            facade, count = re.subn(
                r"(?m)^pub struct Bleng\s*\{[^}]*\}",
                "",
                facade_source.read_text(encoding="utf-8"),
            )
            self.assertEqual(count, 1)
            facade_fixture.write_text(facade, encoding="utf-8")
            with (
                patch.object(
                    api_snapshots,
                    "BLENG_RUST_SOURCES",
                    (
                        facade_fixture,
                        operations_source,
                        error_source,
                        operation_observation_source,
                    ),
                ),
                self.assertRaisesRegex(RuntimeError, r"rust.*missing=\['Bleng'\]"),
            ):
                api_snapshots._generate(bleng=True)

    def test_missing_each_generated_handle_and_interface_is_rejected(self) -> None:
        with self.representative_bindings():
            for language, path, source, suffix in (
                (
                    "kotlin",
                    api_snapshots.BLENG_KOTLIN_BINDING,
                    KOTLIN_SOURCE,
                    "Interface",
                ),
                ("swift", api_snapshots.BLENG_SWIFT_BINDING, SWIFT_SOURCE, "Protocol"),
            ):
                for handle, *_ in TYPED_OPERATIONS:
                    for name in (handle, handle + suffix):
                        with self.subTest(language=language, name=name):
                            mutated, count = re.subn(
                                rf"(?m)^(?:public interface|public protocol|open class) {name}\b[^{{]*\{{[^}}]*\}}\n",
                                "",
                                source,
                            )
                            self.assertEqual(count, 1)
                            path.write_text(mutated, encoding="utf-8")
                            with self.assertRaisesRegex(
                                RuntimeError, rf"{language}.*missing=\['{name}'\]"
                            ):
                                api_snapshots._generate(bleng=True)
                            path.write_text(source, encoding="utf-8")

    def test_unknown_generated_declarations_are_not_silently_filtered(self) -> None:
        with self.representative_bindings():
            for language, path, source, extras in (
                (
                    "kotlin",
                    api_snapshots.BLENG_KOTLIN_BINDING,
                    KOTLIN_SOURCE,
                    (
                        "open class UnknownOperation {}",
                        "public interface UnknownOperationInterface {}",
                        "data class RuntimeLeak(val value: kotlin.ULong) {}",
                    ),
                ),
                (
                    "swift",
                    api_snapshots.BLENG_SWIFT_BINDING,
                    SWIFT_SOURCE,
                    (
                        "open class UnknownOperation {}",
                        "public protocol UnknownOperationProtocol {}",
                        "public struct RuntimeLeak {}",
                    ),
                ),
            ):
                for extra in extras:
                    with self.subTest(language=language, extra=extra):
                        path.write_text(source + extra + "\n", encoding="utf-8")
                        with self.assertRaisesRegex(
                            RuntimeError,
                            rf"{language}.*unexpected=.*(?:UnknownOperation|RuntimeLeak)",
                        ):
                            api_snapshots._generate(bleng=True)
                        path.write_text(source, encoding="utf-8")

    def test_duplicate_canonical_dtos_remain_rejected(self) -> None:
        with self.representative_bindings():
            for path, source, declarations in (
                (
                    api_snapshots.BLENG_KOTLIN_BINDING,
                    KOTLIN_SOURCE,
                    (
                        "public interface BleBackend {}",
                        "sealed class BleEvent {}",
                        "data class AdapterSnapshot() {}",
                        "data class ScanOptions() {}",
                    ),
                ),
                (
                    api_snapshots.BLENG_SWIFT_BINDING,
                    SWIFT_SOURCE,
                    (
                        "public protocol BleBackend {}",
                        "public enum BleEvent {}",
                        "public struct AdapterSnapshot {}",
                        "public struct ScanOptions {}",
                    ),
                ),
            ):
                for declaration in declarations:
                    with self.subTest(declaration=declaration):
                        path.write_text(source + declaration + "\n", encoding="utf-8")
                        with self.assertRaisesRegex(
                            RuntimeError, "duplicates (?:canonical|blehub)"
                        ):
                            api_snapshots._generate(bleng=True)
                        path.write_text(source, encoding="utf-8")

    def test_exact_two_component_surface_and_rejects_extra_or_missing_file(
        self,
    ) -> None:
        with self.representative_bindings() as root:
            snapshots = api_snapshots._generate(bleng=True)
            self.assertEqual(set(snapshots), {"rust", "kotlin", "swift"})
            for snapshot in snapshots.values():
                self.assertIn("ReadOperation", snapshot)

            for relative in (
                "swift/foreign_probe.swift",
                "swift/foreign_probeFFI.h",
                "swift/foreign_probeFFI.modulemap",
                "kotlin/uniffi/foreign_probe/foreign_probe.kt",
            ):
                with self.subTest(extra_native_image_export=relative):
                    extra = root / relative
                    extra.parent.mkdir(parents=True, exist_ok=True)
                    extra.write_text("unexpected", encoding="utf-8")
                    with self.assertRaisesRegex(RuntimeError, "unexpected"):
                        api_snapshots._generate(bleng=True)
                    extra.unlink()
            kotlin = root / BLENG_FILES[1]
            kotlin.write_text(
                KOTLIN_SOURCE + "data class AdapterSnapshot()\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(RuntimeError, "duplicates blehub"):
                api_snapshots._generate(bleng=True)
            kotlin.write_text("generated fixture", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "import canonical"):
                api_snapshots._generate(bleng=True)
            kotlin.write_text(KOTLIN_SOURCE, encoding="utf-8")
            swift = root / BLENG_FILES[3]
            swift.write_text(
                SWIFT_SOURCE + "public struct ScanOptions {}\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(RuntimeError, "duplicates canonical"):
                api_snapshots._generate(bleng=True)
            swift.write_text(SWIFT_SOURCE, encoding="utf-8")
            kotlin.unlink()
            with self.assertRaisesRegex(RuntimeError, "generated bindings are missing"):
                api_snapshots._generate(bleng=True)


class RustDeclarationBoundaryTests(unittest.TestCase):
    def test_doc_and_attribute_delimiters_do_not_define_declaration_boundaries(
        self,
    ) -> None:
        cases = (
            (
                "/// Limit is bounded; this documentation also contains {braces}.\n",
                "pub const MAX_WRITE_VALUE_BYTES: usize = 512;\n",
                "pub const MAX_WRITE_VALUE_BYTES: usize = 512;",
            ),
            (
                "/// Call dispose; do not retain this handle.\n#[derive(uniffi::Object)]\n",
                (
                    "pub struct BackendEventIngressHandle { inner: PrivateState }\n"
                    "impl BackendEventIngressHandle {\n"
                    "    pub fn push(&self, event: BleEvent) -> EventIngressStatus { todo!() }\n"
                    "}\n"
                ),
                (
                    "pub struct BackendEventIngressHandle;\n\n"
                    "impl BackendEventIngressHandle {\n"
                    "    pub fn push(&self, event: BleEvent) -> EventIngressStatus;\n}"
                ),
            ),
            (
                '#[derive(thiserror::Error)]\n#[error("{category:?}: {message}; retry later")]\n',
                (
                    "pub struct BleEventError {\n"
                    "    pub category: BleErrorCategory,\n"
                    "    pub message: String,\n"
                    "    pub platform_details: Option<PlatformErrorDetails>,\n}\n"
                ),
                (
                    "pub struct BleEventError {\n"
                    "    pub category: BleErrorCategory,\n"
                    "    pub message: String,\n"
                    "    pub platform_details: Option<PlatformErrorDetails>,\n}"
                ),
            ),
            (
                '#[doc = "Error {variants}; not a body"]\n#[derive(uniffi::Error)]\n',
                (
                    "pub enum SubmissionError {\n"
                    '    #[error("invalid context: {detail}; rejected")]\n'
                    "    InvalidContext { detail: String },\n"
                    '    #[error("engine failed: {error}")]\n'
                    "    Bleng { error: BlengError },\n}\n"
                ),
                (
                    "pub enum SubmissionError {\n"
                    "    InvalidContext {detail: String},\n"
                    "    Bleng {error: BlengError},\n}"
                ),
            ),
            (
                "/// Callback; its body is not {documentation}.\n#[uniffi::export(foreign)]\n",
                (
                    "pub trait EventSink: Send + Sync {\n"
                    "    fn on_event(&self, event: BleEvent) -> Result<(), EventSinkError>;\n}\n"
                ),
                (
                    "pub trait EventSink: Send + Sync {\n"
                    "    fn on_event(&self, event: BleEvent) -> Result<(), EventSinkError>;\n}"
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "declarations.rs"
            with patch.object(api_snapshots, "RUST_SOURCE_ROOT", root):
                for preamble, declaration, expected in cases:
                    with self.subTest(declaration=declaration.splitlines()[0]):
                        path.write_text(preamble + declaration, encoding="utf-8")
                        decorated, names = api_snapshots._rust_snapshot()
                        path.write_text(declaration, encoding="utf-8")
                        plain, plain_names = api_snapshots._rust_snapshot()
                        self.assertEqual(decorated, plain)
                        self.assertSetEqual(names, plain_names)
                        self.assertEqual(decorated.split("\n\n", 1)[1], expected + "\n")

    def test_actual_blehub_documented_and_annotated_declarations_are_complete(
        self,
    ) -> None:
        snapshot, declarations = api_snapshots._rust_snapshot()
        self.assertTrue(
            {"MAX_WRITE_VALUE_BYTES", "BackendEventIngressHandle", "BleEventError"}
            <= declarations
        )
        self.assertIn("pub const MAX_WRITE_VALUE_BYTES: usize = 512;", snapshot)
        self.assertIn(
            "pub struct BackendEventIngressHandle;\n\n"
            "impl BackendEventIngressHandle {\n"
            "    pub fn push(&self, event: BleEvent) -> EventIngressStatus;\n}",
            snapshot,
        )
        self.assertIn(
            "pub struct BleEventError {\n"
            "    pub category: BleErrorCategory,\n"
            "    pub message: String,\n"
            "    pub platform_details: Option<PlatformErrorDetails>,\n}",
            snapshot,
        )


# Independent of the production allowlist; operation handles remain BLeng-owned.
SUPERVISOR_RECORDS = {
    "ForeignExternalRecoverySignal",
    "ForeignOwnerClose",
    "ForeignOwnerCloseFailure",
    "ForeignOwnerContext",
    "ForeignOwnerStart",
    "ForeignResourceCount",
    "ForeignRecoveryLink",
    "ForeignRecoveryMode",
    "ForeignRecoveryOutcome",
    "ForeignRecoveryTarget",
    "ForeignRecoveryPolicyConfig",
    "ForeignSupervisorConfig",
    "ForeignRecoverySignal",
    "ForeignRecoveryObservation",
    "ForeignRecoveryAction",
    "ForeignPlatformRecoveryAction",
    "ForeignPlatformActionResult",
    "ForeignRecoveryPlan",
    "ForeignScanEventEnvelope",
    "ForeignScanDeliverySnapshot",
}
# Product contract: Rust field, foreign field, Rust/Kotlin/Swift types.
SCAN_VIEW_RECORD_FIELDS = {
    "ForeignScanFailure": (
        (
            "domain",
            "domain",
            "ForeignScanFailureDomain",
            "ForeignScanFailureDomain",
            "ForeignScanFailureDomain",
        ),
        ("code", "code", "String", "String", "String"),
        ("message", "message", "String", "String", "String"),
        (
            "platform_details",
            "platformDetails",
            "Option<PlatformErrorDetails>",
            "PlatformErrorDetails?",
            "PlatformErrorDetails?",
        ),
    ),
    "ForeignScanOptions": (
        ("service_uuids", "serviceUuids", "Vec<String>", "List<String>", "[String]"),
        ("name_prefix", "namePrefix", "Option<String>", "String?", "String?"),
        ("company_id", "companyId", "Option<f64>", "Double?", "Double?"),
        ("allow_duplicates", "allowDuplicates", "bool", "Boolean", "Bool"),
    ),
    "ForeignScanCommandResult": (
        (
            "failure",
            "failure",
            "Option<ForeignScanFailure>",
            "ForeignScanFailure?",
            "ForeignScanFailure?",
        ),
    ),
    "ForeignScanBeginResult": (
        (
            "scan",
            "scan",
            "Option<Arc<SupervisedScan>>",
            "SupervisedScan?",
            "SupervisedScan?",
        ),
        (
            "failure",
            "failure",
            "Option<ForeignScanFailure>",
            "ForeignScanFailure?",
            "ForeignScanFailure?",
        ),
    ),
    "ForeignScanStopResult": (
        (
            "operation",
            "operation",
            "Option<Arc<SupervisedScanStop>>",
            "SupervisedScanStop?",
            "SupervisedScanStop?",
        ),
        (
            "failure",
            "failure",
            "Option<ForeignScanFailure>",
            "ForeignScanFailure?",
            "ForeignScanFailure?",
        ),
    ),
    "ForeignScanObservation": (
        (
            "advertisement",
            "advertisement",
            "Option<ForeignScanAdvertisement>",
            "ForeignScanAdvertisement?",
            "ForeignScanAdvertisement?",
        ),
        (
            "ended",
            "ended",
            "Option<ForeignScanEnd>",
            "ForeignScanEnd?",
            "ForeignScanEnd?",
        ),
        (
            "failure",
            "failure",
            "Option<ForeignScanFailure>",
            "ForeignScanFailure?",
            "ForeignScanFailure?",
        ),
    ),
    "ForeignScanAdvertisement": (
        (
            "peripheral",
            "peripheral",
            "Arc<SupervisedPeripheral>",
            "SupervisedPeripheral",
            "SupervisedPeripheral",
        ),
        ("local_name", "localName", "Option<String>", "String?", "String?"),
        (
            "advertised_services",
            "advertisedServices",
            "Vec<String>",
            "List<String>",
            "[String]",
        ),
        (
            "manufacturer_data",
            "manufacturerData",
            "Vec<ManufacturerData>",
            "List<ManufacturerData>",
            "[ManufacturerData]",
        ),
        (
            "service_data",
            "serviceData",
            "Vec<ServiceData>",
            "List<ServiceData>",
            "[ServiceData]",
        ),
        ("rssi", "rssi", "Option<i16>", "Short?", "Int16?"),
        ("connectable", "connectable", "Option<bool>", "Boolean?", "Bool?"),
    ),
    "ForeignScanEnd": (
        (
            "reason",
            "reason",
            "ForeignScanEndReason",
            "ForeignScanEndReason",
            "ForeignScanEndReason",
        ),
        (
            "failure",
            "failure",
            "Option<ForeignScanFailure>",
            "ForeignScanFailure?",
            "ForeignScanFailure?",
        ),
    ),
}
SCAN_VIEW_ENUM_VARIANTS = {
    "ForeignScanFailureDomain": (
        ("Input", "INPUT", "input"),
        ("Submission", "SUBMISSION", "submission"),
        ("Operation", "OPERATION", "operation"),
        ("Observation", "OBSERVATION", "observation"),
        ("Delivery", "DELIVERY", "delivery"),
        ("Lifecycle", "LIFECYCLE", "lifecycle"),
    ),
    "ForeignScanEndReason": (
        ("Requested", "REQUESTED", "requested"),
        ("SystemSuspended", "SYSTEM_SUSPENDED", "systemSuspended"),
        ("Failed", "FAILED", "failed"),
        ("OwnerClosed", "OWNER_CLOSED", "ownerClosed"),
    ),
}
SCAN_VIEW_OBJECT_METHODS = {
    "SupervisedPeripheral": (
        (
            "pub fn same_peripheral(&self, other: Arc<Self>) -> bool",
            "fun samePeripheral(other: SupervisedPeripheral): Boolean",
            "func samePeripheral(other: SupervisedPeripheral) -> Bool",
        ),
    ),
    "SupervisedScanStop": (
        (
            "pub async fn wait_result(&self, timeout_ms: f64) -> ForeignScanCommandResult",
            "suspend fun waitResult(timeoutMs: Double): ForeignScanCommandResult",
            "func waitResult(timeoutMs: Double) async -> ForeignScanCommandResult",
        ),
        (
            "pub async fn cancel_result(&self) -> ForeignScanCommandResult",
            "suspend fun cancelResult(): ForeignScanCommandResult",
            "func cancelResult() async -> ForeignScanCommandResult",
        ),
    ),
}
SUPERVISED_SCAN_VIEW_METHODS = (
    (
        "pub async fn start_result(&self, timeout_ms: f64) -> ForeignScanCommandResult",
        "suspend fun startResult(timeoutMs: Double): ForeignScanCommandResult",
        "func startResult(timeoutMs: Double) async -> ForeignScanCommandResult",
    ),
    (
        "pub async fn cancel_start_result(&self) -> ForeignScanCommandResult",
        "suspend fun cancelStartResult(): ForeignScanCommandResult",
        "func cancelStartResult() async -> ForeignScanCommandResult",
    ),
    (
        "pub async fn next_observation(&self, timeout_ms: f64) -> ForeignScanObservation",
        "suspend fun nextObservation(timeoutMs: Double): ForeignScanObservation",
        "func nextObservation(timeoutMs: Double) async -> ForeignScanObservation",
    ),
    (
        "pub fn begin_stop_view(&self) -> ForeignScanStopResult",
        "fun beginStopView(): ForeignScanStopResult",
        "func beginStopView() -> ForeignScanStopResult",
    ),
)

SUPERVISOR_SOURCE_FILES = (
    "ffi/adapter.rs",
    "ffi/object.rs",
    "ffi/operations.rs",
    "ffi/types.rs",
    "ffi/scan.rs",
    "ffi/scan_session.rs",
    "ffi/scan_view.rs",
    "error.rs",
    "scan.rs",
    "scan_delivery.rs",
)
SUPERVISOR_CALLBACKS = {
    "ForeignPlatformOwnerSession",
    "ForeignPlatformOwnerSessionFactory",
}
SUPERVISOR_LIFECYCLE = {
    "new",
    "is_active",
    "active_context",
    "active_owner_start",
    "start",
    "last_retired_resources",
    "configure_old_operation_barrier",
    "execute_platform_action",
    "drive_platform_actions_until_boundary",
    "retire_for_recovery",
    "create_fresh_owner",
    "shutdown",
    "shutdown_view",
    "observe",
    "observe_external_signal",
    "observe_signal",
}
SUPERVISOR_PARAMETERS = (
    "options: ScanOptions",
    "session: ScanSessionId",
    "peripheral: PeripheralId",
    "connection: ConnectionId",
    "connection: ConnectionId",
    "connection: ConnectionId, session: GattSessionId, characteristic: GattCharacteristicId",
    "connection: ConnectionId, session: GattSessionId, characteristic: GattCharacteristicId, value: Vec<u8>, write_type: WriteType",
    "connection: ConnectionId, session: GattSessionId, characteristic: GattCharacteristicId, mode: NotificationModeRequest",
    "connection: ConnectionId, session: GattSessionId, subscription: SubscriptionId",
    "connection: ConnectionId",
)
SUPERVISOR_OPERATIONS = TYPED_OPERATIONS[1:]  # Initialization remains lifecycle-owned.
SUPERVISOR_ERROR_VARIANTS = (
    "NoActiveSession",
    "StaleContext",
    "RecoveryNotReady",
    "SupervisorBusy",
    "SupervisorUnavailable",
)
SUPERVISED_SCAN_METHODS = (
    (
        "pub async fn wait_until_started(&self, timeout_ms: u64) -> Result<(), ScanSessionError>",
        "suspend fun waitUntilStarted(timeoutMs: ULong)",
        "func waitUntilStarted(timeoutMs: UInt64) async throws",
    ),
    (
        "pub async fn cancel_start(&self) -> Result<(), ScanSessionError>",
        "suspend fun cancelStart()",
        "func cancelStart() async throws",
    ),
    (
        "pub async fn next_event(&self, timeout_ms: u64) -> Result<ScanEvent, ScanSessionError>",
        "suspend fun nextEvent(timeoutMs: ULong): ScanEvent",
        "func nextEvent(timeoutMs: UInt64) async throws -> ScanEvent",
    ),
    (
        "pub fn begin_stop(&self) -> Result<Arc<StopScanOperation>, ScanSessionError>",
        "fun beginStop(): StopScanOperation",
        "func beginStop() throws -> StopScanOperation",
    ),
    (
        "pub fn detach(&self)",
        "fun detach()",
        "func detach()",
    ),
    (
        "pub fn observation_snapshot(&self) -> ForeignScanDeliverySnapshot",
        "fun observationSnapshot(): ForeignScanDeliverySnapshot",
        "func observationSnapshot() -> ForeignScanDeliverySnapshot",
    ),
) + SUPERVISED_SCAN_VIEW_METHODS
SCAN_DELIVERY_ERRORS = (
    "PullBusy",
    "StartFailed",
    "AdvertisementOverflow",
    "ByteOverflow",
    "StopDataOverflow",
    "PendingEventOverflow",
    "OwnerRetired",
    "Detached",
    "Unavailable",
    "ConflictingStart",
)

SUPERVISOR_ERROR_SOURCE = """#[derive(Debug, Error)]
#[cfg_attr(feature = "ffi", derive(uniffi::Error))]
pub enum SupervisorOperationError {
    NoActiveSession,
    StaleContext,
    RecoveryNotReady,
    SupervisorBusy,
    SupervisorUnavailable,
    InvalidContext { detail: String },
    Bleng {
        #[from]
        error: BlengError,
    },
}
"""


def expected_supervisor_declarations(language: str) -> set[str]:
    names = (
        SUPERVISOR_RECORDS
        | SUPERVISOR_CALLBACKS
        | {"Supervisor", "ScanEventProjection", "ScanEvent", "SupervisedScan"}
        | set(SCAN_VIEW_RECORD_FIELDS)
        | set(SCAN_VIEW_ENUM_VARIANTS)
        | set(SCAN_VIEW_OBJECT_METHODS)
    )
    if language == "rust":
        return names | {
            "ForeignPlatformOwnerSessionFactoryAdapter",
            "SupervisorFfiError",
            "SupervisorOperationError",
            "ScanSessionError",
            "ScanDeliveryError",
        }
    if language == "kotlin":
        return (
            names
            | {
                "SupervisorInterface",
                "ScanEventProjectionInterface",
                "SupervisorFfiException",
                "SupervisorOperationException",
                "SupervisedScanInterface",
                "ScanSessionException",
                "ScanDeliveryException",
            }
            | {f"{name}Interface" for name in SCAN_VIEW_OBJECT_METHODS}
        )
    return (
        names
        | {
            "SupervisorProtocol",
            "ScanEventProjectionProtocol",
            "SupervisorFfiError",
            "SupervisorOperationError",
            "SupervisedScanProtocol",
            "ScanSessionError",
            "ScanDeliveryError",
        }
        | {f"{name}Protocol" for name in SCAN_VIEW_OBJECT_METHODS}
    )


class SupervisorComponentTests(unittest.TestCase):
    @contextmanager
    def representative_bindings(self) -> Iterator[dict[str, Path]]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {}
            # Keep the real lifecycle/DTO source, but do not depend on in-progress
            # forwarding implementation or generated bindings for scanner regressions.
            rust_paths = []
            for relative in SUPERVISOR_SOURCE_FILES:
                path = (
                    api_snapshots.PROJECT_ROOT
                    / "crates/bleng-supervisor/src"
                    / relative
                )
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                key = "core_scan.rs" if relative == "scan.rs" else path.name
                paths[key] = target
                rust_paths.append(target)
                if relative not in {"ffi/operations.rs", "error.rs"}:
                    target.write_text(
                        path.read_text(encoding="utf-8"), encoding="utf-8"
                    )
            operations = [
                (
                    "        pub fn begin_scan(self: Arc<Self>, context: ForeignOwnerContext, "
                    "options: ScanOptions,) -> Result<Arc<SupervisedScan>, ScanSessionError> { todo!() }"
                )
            ]
            for (handle, begin, *_), parameters in zip(
                SUPERVISOR_OPERATIONS, SUPERVISOR_PARAMETERS, strict=True
            ):
                operations.append(
                    f"        pub fn {begin}(&self, context: ForeignOwnerContext, "
                    f"{parameters}) -> Result<Arc<{handle}>, SupervisorOperationError> {{ todo!() }}"
                )
            # Multiple exported impls in one indented module must merge with the
            # independent lifecycle impl in object.rs, not replace one another.
            paths["operations.rs"].write_text(
                '#[cfg(test)]\n#[path = "tests/operations.rs"]\nmod tests;\n\n'
                "mod forwarding {\n    #[uniffi::export]\n    impl Supervisor {\n"
                + "\n".join(operations[:5])
                + "\n    }\n"
                "    #[cfg(test)]\n"
                "    mod inline_tests {\n"
                "        pub struct TestOnlyLeak;\n"
                "        impl Supervisor {\n"
                '            pub fn test_only_method(&self) { let _ = "}"; }\n'
                "        }\n"
                "    }\n"
                "    #[uniffi::export]\n    impl Supervisor {\n"
                + "\n".join(operations[5:])
                + "\n    }\n}\n",
                encoding="utf-8",
            )
            paths["error.rs"].write_text(
                "#[derive(Debug)]\npub enum SupervisorError { CoreOnly }\n"
                + SUPERVISOR_ERROR_SOURCE,
                encoding="utf-8",
            )
            kotlin_methods = [
                line.replace("(", "(`context`: ForeignOwnerContext, ", 1)
                for line in KOTLIN_SOURCE.splitlines()
                if "fun `begin" in line and "beginInitialize" not in line
            ]
            kotlin_methods.append(
                "    fun `beginScan`(`context`: ForeignOwnerContext, `options`: ScanOptions): SupervisedScan"
            )
            kotlin_methods.append(
                "    fun `beginScanView`(`context`: ForeignOwnerContext, `options`: ForeignScanOptions): ForeignScanBeginResult"
            )
            swift_methods = [
                line.replace("(", "(context: ForeignOwnerContext, ", 1)
                for line in SWIFT_SOURCE.splitlines()
                if "func begin" in line and "beginInitialize" not in line
            ]
            swift_methods.append(
                "    func beginScan(context: ForeignOwnerContext, options: ScanOptions) throws -> SupervisedScan"
            )
            swift_methods.append(
                "    func beginScanView(context: ForeignOwnerContext, options: ForeignScanOptions) -> ForeignScanBeginResult"
            )
            kotlin = (
                "\n".join(
                    f"data class {name}(val value: String) {{}}"
                    for name in sorted(
                        SUPERVISOR_RECORDS
                        - {
                            "ForeignOwnerClose",
                            "ForeignOwnerCloseFailure",
                            "ForeignScanEventEnvelope",
                            "ForeignScanDeliverySnapshot",
                        }
                    )
                )
                + "\n"
            )
            swift = (
                "\n".join(
                    f"public struct {name} {{}}"
                    for name in sorted(
                        SUPERVISOR_RECORDS
                        - {
                            "ForeignOwnerClose",
                            "ForeignOwnerCloseFailure",
                            "ForeignScanEventEnvelope",
                            "ForeignScanDeliverySnapshot",
                        }
                    )
                )
                + "\n"
            )
            for name in sorted(SUPERVISOR_CALLBACKS):
                kotlin_reader = swift_reader = ""
                if name == "ForeignPlatformOwnerSession":
                    kotlin_reader = (
                        "\n    fun `startEventReader`(`owner`: Bleng, "
                        "`scanProjection`: ScanEventProjection)\n"
                    )
                    swift_reader = (
                        "\n    func startEventReader(owner: Bleng, "
                        "scanProjection: ScanEventProjection) throws\n"
                    )
                kotlin += f"public interface {name} {{{kotlin_reader}}}\nopen class {name}Impl {{}}\n"
                swift += f"public protocol {name} {{{swift_reader}}}\nopen class {name}Impl {{}}\n"
            kotlin += (
                "data class ForeignOwnerClose(\n"
                "    var `ownerClosed`: Boolean,\n"
                "    var `failure`: ForeignOwnerCloseFailure?\n) {}\n"
                "data class ForeignOwnerCloseFailure(\n"
                "    var `code`: String,\n"
                "    var `message`: String\n) {}\n"
                "data class ForeignScanEventEnvelope(\n"
                "    var `sourceContext`: ForeignOwnerContext,\n"
                "    var `event`: ScanEvent\n) {}\n"
                "sealed class ScanEvent {\n"
                "    data class Started(val `commandId`: CommandId, val `scanId`: ScanSessionId) : ScanEvent()\n"
                "    data class Advertisement(val `advertisement`: AdvertisementObservation) : ScanEvent()\n"
                "    data class Stopped(val `scanId`: ScanSessionId, val `result`: ScanStopResult) : ScanEvent()\n}\n"
                "public interface ScanEventProjectionInterface {\n"
                "    fun `project`(`event`: BleEvent): ForeignScanEventEnvelope?\n}\n"
                "open class ScanEventProjection : ScanEventProjectionInterface {}\n"
            )
            swift += (
                "public struct ForeignOwnerClose {\n"
                "    public var ownerClosed: Bool\n"
                "    public var failure: ForeignOwnerCloseFailure?\n}\n"
                "public struct ForeignOwnerCloseFailure {\n"
                "    public var code: String\n"
                "    public var message: String\n}\n"
                "public struct ForeignScanEventEnvelope {\n"
                "    public var sourceContext: ForeignOwnerContext\n"
                "    public var event: ScanEvent\n}\n"
                "public enum ScanEvent {\n"
                "    case started(commandId: CommandId, scanId: ScanSessionId)\n"
                "    case advertisement(advertisement: AdvertisementObservation)\n"
                "    case stopped(scanId: ScanSessionId, result: ScanStopResult)\n}\n"
                "public protocol ScanEventProjectionProtocol {\n"
                "    func project(event: BleEvent) -> ForeignScanEventEnvelope?\n}\n"
                "open class ScanEventProjection: ScanEventProjectionProtocol {}\n"
            )
            kotlin += (
                "public interface SupervisorInterface {\n"
                "    suspend fun `start`(): ForeignOwnerStart\n"
                "    fun `activeOwnerStart`(): ForeignOwnerStart?\n"
                "    suspend fun `shutdown`(): List<ForeignResourceCount>\n"
                "    suspend fun `shutdownView`(): ForeignOwnerClose\n"
                + "\n".join(kotlin_methods)
                + "\n}\nopen class Supervisor : SupervisorInterface {}\n"
                "sealed class SupervisorFfiException {\n"
                "    class NoActiveContext() : SupervisorFfiException()\n}\n"
                "sealed class SupervisorOperationException {\n"
                + "\n".join(
                    f"    class {name}() : SupervisorOperationException()"
                    for name in SUPERVISOR_ERROR_VARIANTS
                )
                + "\n    data class InvalidContext(val `detail`: String) : SupervisorOperationException()\n"
                "    data class Bleng(val `error`: BlengException) : SupervisorOperationException()\n}\n"
            )
            swift += (
                "public protocol SupervisorProtocol {\n"
                "    func start() async throws -> ForeignOwnerStart\n"
                "    func activeOwnerStart() throws -> ForeignOwnerStart?\n"
                "    func shutdown() async throws -> [ForeignResourceCount]\n"
                "    func shutdownView() async -> ForeignOwnerClose\n"
                + "\n".join(swift_methods)
                + "\n}\nopen class Supervisor: SupervisorProtocol {}\n"
                "public\n    enum SupervisorFfiError {\n    case NoActiveContext\n}\n"
                "public\n    enum SupervisorOperationError {\n"
                + "\n".join(f"    case {name}" for name in SUPERVISOR_ERROR_VARIANTS)
                + "\n    case InvalidContext(detail: String\n    )\n"
                "    case Bleng(error: BlengError\n    )\n}\n"
            )
            kotlin += (
                "data class ForeignScanDeliverySnapshot(\n"
                "    var `queuedAdvertisements`: kotlin.ULong,\n"
                "    var `queuedBytes`: kotlin.ULong,\n"
                "    var `queuedStopBytes`: kotlin.ULong,\n"
                "    var `pullPending`: kotlin.Boolean\n) {}\n"
                "public interface SupervisedScanInterface {\n"
                + "\n".join(f"    {row[1]}" for row in SUPERVISED_SCAN_METHODS)
                + "\n}\nopen class SupervisedScan : SupervisedScanInterface {}\n"
                "sealed class ScanSessionException {\n"
                "    data class Submission(val `error`: SupervisorOperationException) : ScanSessionException()\n"
                "    data class Delivery(val `error`: ScanDeliveryException) : ScanSessionException()\n"
                "    class DeliveryBusy() : ScanSessionException()\n"
                "    class StartNotObserved() : ScanSessionException()\n"
                "    data class Operation(val `error`: BlengException) : ScanSessionException()\n"
                "    data class Observation(val `error`: BlengException) : ScanSessionException()\n}\n"
                "sealed class ScanDeliveryException {\n"
                + "\n".join(
                    f"    class {name}() : ScanDeliveryException()"
                    for name in SCAN_DELIVERY_ERRORS
                )
                + "\n}\n"
            )
            swift += (
                "public struct ForeignScanDeliverySnapshot {\n"
                "    public var queuedAdvertisements: UInt64\n"
                "    public var queuedBytes: UInt64\n"
                "    public var queuedStopBytes: UInt64\n"
                "    public var pullPending: Bool\n}\n"
                "public protocol SupervisedScanProtocol {\n"
                + "\n".join(f"    {row[2]}" for row in SUPERVISED_SCAN_METHODS)
                + "\n}\nopen class SupervisedScan: SupervisedScanProtocol {}\n"
                "public\n    enum ScanSessionError {\n"
                "    case Submission(error: SupervisorOperationError)\n"
                "    case Delivery(error: ScanDeliveryError)\n"
                "    case DeliveryBusy\n"
                "    case StartNotObserved\n"
                "    case Operation(error: BlengError)\n"
                "    case Observation(error: BlengError)\n}\n"
                "public\n    enum ScanDeliveryError {\n"
                + "\n".join(f"    case {name}" for name in SCAN_DELIVERY_ERRORS)
                + "\n}\n"
            )
            for name, fields in SCAN_VIEW_RECORD_FIELDS.items():
                kotlin += (
                    f"data class {name}(\n"
                    + "\n".join(
                        f"    var `{field}`: {kind}," for _, field, _, kind, _ in fields
                    )
                    + "\n) {}\n"
                )
                swift += (
                    f"public struct {name} {{\n"
                    + "\n".join(
                        f"    public var {field}: {kind}"
                        for _, field, _, _, kind in fields
                    )
                    + "\n}\n"
                )
            for name, variants in SCAN_VIEW_ENUM_VARIANTS.items():
                kotlin += (
                    f"enum class {name} {{\n"
                    + "\n".join(f"    {row[1]}," for row in variants)
                    + "\n}\n"
                )
                swift += (
                    f"public enum {name} {{\n"
                    + "\n".join(f"    case {row[2]}" for row in variants)
                    + "\n}\n"
                )
            for name, methods in SCAN_VIEW_OBJECT_METHODS.items():
                kotlin += (
                    f"public interface {name}Interface {{\n"
                    + "\n".join(f"    {row[1]}" for row in methods)
                    + f"\n}}\nopen class {name}: {name}Interface {{}}\n"
                )
                swift += (
                    f"public protocol {name}Protocol {{\n"
                    + "\n".join(f"    {row[2]}" for row in methods)
                    + f"\n}}\nopen class {name}: {name}Protocol {{}}\n"
                )
            paths["kotlin"] = root / "supervisor.kt"
            paths["swift"] = root / "supervisor.swift"
            paths["kotlin"].write_text(kotlin, encoding="utf-8")
            paths["swift"].write_text(swift, encoding="utf-8")
            with (
                patch.object(api_snapshots, "PROJECT_ROOT", root),
                patch.object(
                    api_snapshots,
                    "SUPERVISOR_RUST_SOURCES",
                    tuple(rust_paths),
                ),
                patch.object(
                    api_snapshots, "SUPERVISOR_KOTLIN_BINDING", paths["kotlin"]
                ),
                patch.object(api_snapshots, "SUPERVISOR_SWIFT_BINDING", paths["swift"]),
            ):
                yield paths

    def assert_rust_methods(self, snapshot: str) -> None:
        names = re.findall(r"(?m)^    pub (?:async )?fn (\w+)\(", snapshot)
        expected = (
            SUPERVISOR_LIFECYCLE
            | {row[1] for row in SUPERVISOR_OPERATIONS}
            | {"project", "begin_scan", "begin_scan_view"}
            | {re.search(r"fn (\w+)\(", row[0])[1] for row in SUPERVISED_SCAN_METHODS}
            | {
                re.search(r"fn (\w+)\(", row[0])[1]
                for methods in SCAN_VIEW_OBJECT_METHODS.values()
                for row in methods
            }
        )
        self.assertCountEqual(names, expected)
        self.assertIn(
            "pub async fn shutdown_view(&self) -> ForeignOwnerClose;", snapshot
        )
        for (handle, begin, *_), parameters in zip(
            SUPERVISOR_OPERATIONS, SUPERVISOR_PARAMETERS, strict=True
        ):
            match = re.search(
                rf"pub fn {begin}\((.*?)\) -> Result<Arc<{handle}>, SupervisorOperationError>;",
                snapshot,
            )
            self.assertIsNotNone(match, begin)
            self.assertEqual(
                [part.strip() for part in match[1].split(",") if part.strip()],
                ["&self", "context: ForeignOwnerContext", *parameters.split(", ")],
            )

    def test_actual_rust_includes_forwarding_and_all_lifecycle_methods(self) -> None:
        self.assertEqual(
            {
                path.relative_to(
                    api_snapshots.PROJECT_ROOT / "crates/bleng-supervisor/src"
                ).as_posix()
                for path in api_snapshots.SUPERVISOR_RUST_SOURCES
            },
            set(SUPERVISOR_SOURCE_FILES),
        )
        snapshot, declarations = api_snapshots._rust_snapshot(supervisor=True)
        self.assertSetEqual(declarations, expected_supervisor_declarations("rust"))
        self.assert_rust_methods(snapshot)
        self.assertNotIn("SessionSupervisor", declarations)
        self.assertNotIn("SupervisorError", declarations)
        self.assertIn(
            "pub enum SupervisorOperationError {\n"
            + "".join(f"    {variant},\n" for variant in SUPERVISOR_ERROR_VARIANTS)
            + "    InvalidContext {detail: String},\n"
            "    Bleng {error: BlengError,},\n}",
            snapshot,
        )

    def test_actual_generated_bindings_include_forwarding_and_typed_error(self) -> None:
        for language, extractor, protocol in (
            ("kotlin", api_snapshots._kotlin_snapshot, "SupervisorInterface"),
            ("swift", api_snapshots._swift_snapshot, "SupervisorProtocol"),
        ):
            with self.subTest(language=language):
                snapshot, declarations = extractor(supervisor=True)
                self.assertSetEqual(
                    declarations, expected_supervisor_declarations(language)
                )
                block = snapshot.split(f"{protocol} {{\n", 1)[1].split("\n}", 1)[0]
                for handle, _, begin, *_ in SUPERVISOR_OPERATIONS:
                    self.assertRegex(
                        block,
                        rf"{begin}\(context: ForeignOwnerContext, [^\n]*\).*{handle}",
                    )
                self.assertIn("start()", block)
                self.assertIn("activeOwnerStart()", block)
                self.assertIn("shutdown()", block)
                self.assertIn(
                    "suspend fun shutdownView(): ForeignOwnerClose"
                    if language == "kotlin"
                    else "func shutdownView() async -> ForeignOwnerClose",
                    block,
                )

    def assert_scan_proxy(self, snapshots: dict[str, str]) -> None:
        for index, language, header in (
            (0, "rust", "impl SupervisedScan"),
            (1, "kotlin", "interface SupervisedScanInterface"),
            (2, "swift", "protocol SupervisedScanProtocol"),
        ):
            with self.subTest(language=language):
                block = (
                    snapshots[language].split(header + " {\n", 1)[1].split("\n}", 1)[0]
                )
                suffix = ";" if language == "rust" else ""
                self.assertCountEqual(
                    block.splitlines(),
                    [f"    {row[index]}{suffix}" for row in SUPERVISED_SCAN_METHODS],
                )
                self.assertNotIn("observeWithTimeout", snapshots[language])
                for variant in SCAN_DELIVERY_ERRORS:
                    self.assertRegex(snapshots[language], rf"\b{variant}\b")
        self.assertIn(
            "pub fn begin_scan(self: Arc<Self>, context: ForeignOwnerContext, "
            "options: ScanOptions,) -> Result<Arc<SupervisedScan>, ScanSessionError>;",
            snapshots["rust"],
        )
        for language, signature in (
            (
                "kotlin",
                "fun beginScan(context: ForeignOwnerContext, options: ScanOptions): SupervisedScan",
            ),
            (
                "swift",
                "func beginScan(context: ForeignOwnerContext, options: ScanOptions) throws -> SupervisedScan",
            ),
        ):
            self.assertIn(signature, snapshots[language])
            self.assertNotRegex(snapshots[language], r"beginScan\([^)]*\bself\b")
        _, declarations = api_snapshots._rust_snapshot(supervisor=True)
        self.assertTrue(
            {"ScanSessionError", "ScanDeliveryError", "ForeignScanDeliverySnapshot"}
            <= declarations
        )
        self.assertTrue(
            {
                "ScanHandoff",
                "ScanDelivery",
                "ScanDeliverySnapshot",
                "ScanSubmissionError",
                "MAX_SCAN_ADVERTISEMENTS",
                "MAX_SCAN_DATA_BYTES",
            }.isdisjoint(declarations)
        )

    def assert_scan_view_contract(self, snapshots: dict[str, str]) -> None:
        forbidden = r"\b(?:CommandId|ScanSessionId|PeripheralId|ConnectionId|GattSessionId|ForeignOwnerContext|BlengError|ScanSessionError|ScanDeliveryError|SupervisorOperationError)\b"
        for index, language in enumerate(("rust", "kotlin", "swift")):
            snapshot = snapshots[language]
            for name, fields in SCAN_VIEW_RECORD_FIELDS.items():
                with self.subTest(language=language, record=name):
                    if language == "rust":
                        block = "\n".join(
                            [
                                f"pub struct {name} {{",
                                *(f"    pub {row[0]}: {row[2]}," for row in fields),
                                "}",
                            ]
                        )
                    elif language == "kotlin":
                        block = "\n".join(
                            [
                                f"data class {name}(",
                                *(f"    var {row[1]}: {row[3]}," for row in fields),
                                ")",
                            ]
                        )
                    else:
                        block = "\n".join(
                            [
                                f"struct {name} {{",
                                *(
                                    f"    public var {row[1]}: {row[4]}"
                                    for row in fields
                                ),
                                "}",
                            ]
                        )
                    self.assertIn(block, snapshot)
                    self.assertNotRegex(block, forbidden)
            for name, variants in SCAN_VIEW_ENUM_VARIANTS.items():
                header = (
                    f"pub enum {name}"
                    if language == "rust"
                    else f"enum class {name}"
                    if language == "kotlin"
                    else f"enum {name}"
                )
                members = [
                    f"    case {row[index]}"
                    if language == "swift"
                    else f"    {row[index]},"
                    for row in variants
                ]
                self.assertIn("\n".join([header + " {", *members, "}"]), snapshot)
            for name, methods in SCAN_VIEW_OBJECT_METHODS.items():
                with self.subTest(language=language, opaque=name):
                    if language == "rust":
                        self.assertIn(f"pub struct {name};\n", snapshot)
                        header = f"impl {name}"
                    else:
                        suffix = "Interface" if language == "kotlin" else "Protocol"
                        self.assertIn(f"class {name} : {name}{suffix}\n", snapshot)
                        header = (
                            f"interface {name}{suffix}"
                            if language == "kotlin"
                            else f"protocol {name}{suffix}"
                        )
                    block = snapshot.split(header + " {\n", 1)[1].split("\n}", 1)[0]
                    self.assertCountEqual(
                        block.splitlines(),
                        [
                            f"    {row[index]}" + (";" if language == "rust" else "")
                            for row in methods
                        ],
                    )
                    self.assertNotRegex(block, forbidden)
            for row in SUPERVISED_SCAN_VIEW_METHODS:
                self.assertNotRegex(row[index], forbidden)
            self.assertNotIn("source_context(", snapshot)
        self.assertIn(
            "pub fn begin_scan_view(self: Arc<Self>, context: ForeignOwnerContext, "
            "options: ForeignScanOptions,) -> ForeignScanBeginResult;",
            snapshots["rust"],
        )
        for language, signature in (
            (
                "kotlin",
                "fun beginScanView(context: ForeignOwnerContext, options: ForeignScanOptions): ForeignScanBeginResult",
            ),
            (
                "swift",
                "func beginScanView(context: ForeignOwnerContext, options: ForeignScanOptions) -> ForeignScanBeginResult",
            ),
        ):
            self.assertIn(signature, snapshots[language])
            self.assertNotRegex(snapshots[language], r"beginScanView\([^)]*\bself\b")

    def test_actual_scan_view_product_contract_and_opaque_identity(self) -> None:
        self.assert_scan_view_contract(api_snapshots._generate_supervisor())

    def test_fixture_scan_view_product_contract_and_opaque_identity(self) -> None:
        with self.representative_bindings():
            self.assert_scan_view_contract(api_snapshots._generate_supervisor())

    def test_private_scan_view_state_and_helpers_do_not_become_public_fields(
        self,
    ) -> None:
        with self.representative_bindings() as paths:
            baseline, _ = api_snapshots._rust_snapshot(supervisor=True)
            path = paths["scan_view.rs"]
            source = path.read_text(encoding="utf-8")
            for name in SCAN_VIEW_OBJECT_METHODS:
                source = source.replace(
                    f"pub struct {name} {{",
                    f"pub struct {name} {{\n    private_cookie: u64,",
                    1,
                )
                source += f"\nimpl {name} {{\n    pub(in crate::ffi) fn internal_id(&self) -> PeripheralId {{ todo!() }}\n}}\n"
            path.write_text(source, encoding="utf-8")
            self.assertEqual(api_snapshots._rust_snapshot(supervisor=True)[0], baseline)
            for name in ("SupervisedScan", *SCAN_VIEW_OBJECT_METHODS):
                methods = [
                    method
                    for source_path in api_snapshots.SUPERVISOR_RUST_SOURCES
                    for method in api_snapshots._rust_public_methods(
                        source_path.read_text(encoding="utf-8"), name
                    )
                ]
                self.assertTrue(methods)
                self.assertFalse(
                    any(
                        "internal_id(" in method
                        or "source_context(" in method
                        or " fn new(" in method
                        for method in methods
                    )
                )

    def test_actual_scan_proxy_methods_and_arc_receiver_are_complete(self) -> None:
        self.assert_scan_proxy(api_snapshots._generate_supervisor())

    def test_fixture_scan_proxy_methods_and_arc_receiver_are_complete(self) -> None:
        with self.representative_bindings():
            self.assert_scan_proxy(api_snapshots._generate_supervisor())

    def test_split_nested_impls_and_foreign_errors_are_fully_extracted(self) -> None:
        with self.representative_bindings():
            snapshots = api_snapshots._generate_supervisor()
            self.assert_rust_methods(snapshots["rust"])
            for language, extractor in (
                ("rust", api_snapshots._rust_snapshot),
                ("kotlin", api_snapshots._kotlin_snapshot),
                ("swift", api_snapshots._swift_snapshot),
            ):
                with self.subTest(language=language):
                    snapshot, declarations = extractor(supervisor=True)
                    self.assertSetEqual(
                        declarations, expected_supervisor_declarations(language)
                    )
                    self.assertSetEqual(
                        declarations, api_snapshots.SUPERVISOR_DECLARATIONS[language]
                    )
                    for variant in SUPERVISOR_ERROR_VARIANTS:
                        self.assertIn(variant, snapshot)
                    if language == "rust":
                        continue
                    error = "BlengException" if language == "kotlin" else "BlengError"
                    self.assertIn(f"Bleng(error: {error})", snapshot)
                    self.assertIn("InvalidContext(detail: String)", snapshot)
                    for handle, _, begin, *_ in SUPERVISOR_OPERATIONS:
                        prefix = "fun" if language == "kotlin" else "func"
                        suffix = (
                            f": {handle}"
                            if language == "kotlin"
                            else f" throws -> {handle}"
                        )
                        self.assertRegex(
                            snapshot,
                            rf"{prefix} {begin}\(context: ForeignOwnerContext, [^\n]*\){suffix}\n",
                        )

    def test_missing_error_and_tightened_allowlist_are_rejected(self) -> None:
        with self.representative_bindings() as paths:
            product_names = (
                set(SCAN_VIEW_RECORD_FIELDS)
                | set(SCAN_VIEW_ENUM_VARIANTS)
                | set(SCAN_VIEW_OBJECT_METHODS)
            )
            product_declarations = [
                (language, filename, name)
                for language, filename, names in (
                    ("rust", "scan_view.rs", product_names),
                    (
                        "kotlin",
                        "kotlin",
                        product_names
                        | {f"{name}Interface" for name in SCAN_VIEW_OBJECT_METHODS},
                    ),
                    (
                        "swift",
                        "swift",
                        product_names
                        | {f"{name}Protocol" for name in SCAN_VIEW_OBJECT_METHODS},
                    ),
                )
                for name in sorted(names)
            ]
            for language, filename, name in (
                *product_declarations,
                ("rust", "types.rs", "ForeignOwnerClose"),
                ("rust", "types.rs", "ForeignOwnerCloseFailure"),
                ("kotlin", "kotlin", "ForeignOwnerClose"),
                ("kotlin", "kotlin", "ForeignOwnerCloseFailure"),
                ("swift", "swift", "ForeignOwnerClose"),
                ("swift", "swift", "ForeignOwnerCloseFailure"),
                ("rust", "scan_session.rs", "SupervisedScan"),
                ("rust", "scan_session.rs", "ScanSessionError"),
                ("rust", "scan_session.rs", "ForeignScanDeliverySnapshot"),
                ("rust", "scan_delivery.rs", "ScanDeliveryError"),
                ("kotlin", "kotlin", "SupervisedScan"),
                ("kotlin", "kotlin", "SupervisedScanInterface"),
                ("kotlin", "kotlin", "ScanSessionException"),
                ("kotlin", "kotlin", "ScanDeliveryException"),
                ("kotlin", "kotlin", "ForeignScanDeliverySnapshot"),
                ("swift", "swift", "SupervisedScan"),
                ("swift", "swift", "SupervisedScanProtocol"),
                ("swift", "swift", "ScanSessionError"),
                ("swift", "swift", "ScanDeliveryError"),
                ("swift", "swift", "ForeignScanDeliverySnapshot"),
                ("rust", "error.rs", "SupervisorOperationError"),
                ("kotlin", "kotlin", "SupervisorOperationException"),
                ("swift", "swift", "SupervisorOperationError"),
                ("swift", "swift", "SupervisorFfiError"),
                ("rust", "scan.rs", "ScanEventProjection"),
                ("rust", "scan.rs", "ForeignScanEventEnvelope"),
                ("rust", "core_scan.rs", "ScanEvent"),
                ("kotlin", "kotlin", "ScanEventProjection"),
                ("kotlin", "kotlin", "ScanEventProjectionInterface"),
                ("kotlin", "kotlin", "ForeignScanEventEnvelope"),
                ("kotlin", "kotlin", "ScanEvent"),
                ("swift", "swift", "ScanEventProjection"),
                ("swift", "swift", "ScanEventProjectionProtocol"),
                ("swift", "swift", "ForeignScanEventEnvelope"),
                ("swift", "swift", "ScanEvent"),
            ):
                with self.subTest(language=language, name=name):
                    path = paths[filename]
                    source = path.read_text(encoding="utf-8")
                    declaration = re.search(
                        rf"(?:(?:pub|public|open)\s+)?(?:struct|enum|class|interface|protocol) {name}\b",
                        source,
                    )
                    self.assertIsNotNone(declaration)
                    start = declaration.start()
                    opening = source.index("{", declaration.end())
                    closing = api_snapshots._find_matching(source, opening, "{", "}")
                    # Remove the entire declaration, including Swift's split visibility.
                    declaration_start = source.rfind("\n", 0, start) + 1

                    path.write_text(
                        source[:declaration_start] + source[closing + 1 :],
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(
                        RuntimeError, rf"{language}.*missing=\['{name}'\]"
                    ):
                        api_snapshots._generate_supervisor()
                    path.write_text(source, encoding="utf-8")
                    allowlist = {
                        key: set(value)
                        for key, value in api_snapshots.SUPERVISOR_DECLARATIONS.items()
                    }
                    allowlist[language].remove(name)
                    with (
                        patch.object(
                            api_snapshots, "SUPERVISOR_DECLARATIONS", allowlist
                        ),
                        self.assertRaisesRegex(
                            RuntimeError, rf"{language}.*unexpected=\['{name}'\]"
                        ),
                    ):
                        api_snapshots._generate_supervisor()

    def test_unknown_declarations_and_core_supervisor_are_not_silently_filtered(
        self,
    ) -> None:
        with self.representative_bindings() as paths:
            for language, filename, extra in (
                ("rust", "object.rs", "pub struct SessionSupervisor;\n"),
                ("rust", "scan_session.rs", "pub struct UnknownProxy;\n"),
                (
                    "rust",
                    "scan_view.rs",
                    "#[derive(uniffi::Object)]\npub struct UnknownProxy;\n",
                ),
                (
                    "rust",
                    "scan_view.rs",
                    "#[derive(uniffi::Record)]\npub struct UnknownProxy { pub value: u64 }\n",
                ),
                (
                    "rust",
                    "scan_view.rs",
                    "#[derive(uniffi::Enum)]\npub enum UnknownProxy { Unexpected }\n",
                ),
                (
                    "rust",
                    "scan_view.rs",
                    "#[uniffi::export(with_foreign)]\npub trait UnknownProxy: Send + Sync { fn leaked(&self); }\n",
                ),
                ("kotlin", "kotlin", "open class UnknownProxy {}\n"),
                ("swift", "swift", "public struct UnknownProxy {}\n"),
                (
                    "rust",
                    "error.rs",
                    '#[cfg_attr(feature = "ffi", derive(uniffi::Error))]\npub enum UnknownError { Unexpected }\n',
                ),
                ("kotlin", "kotlin", "open class SessionSupervisor {}\n"),
                ("swift", "swift", "public struct SessionSupervisor {}\n"),
            ):
                with self.subTest(language=language, extra=extra):
                    path = paths[filename]
                    source = path.read_text(encoding="utf-8")
                    path.write_text(source + extra, encoding="utf-8")
                    with self.assertRaisesRegex(
                        RuntimeError,
                        rf"{language}.*unexpected=.*(?:SessionSupervisor|UnknownError|UnknownProxy)",
                    ):
                        api_snapshots._generate_supervisor()
                    path.write_text(source, encoding="utf-8")

    def test_missing_operations_or_scan_module_fails_closed(self) -> None:
        for filename in (
            "operations.rs",
            "scan.rs",
            "core_scan.rs",
            "scan_session.rs",
            "scan_delivery.rs",
            "scan_view.rs",
        ):
            with (
                self.subTest(filename=filename),
                self.representative_bindings() as paths,
            ):
                paths[filename].unlink()
                with self.assertRaisesRegex(
                    RuntimeError,
                    r"supervisor snapshot inputs are missing:\s+- (?:ffi/)?(?:operations|scan|scan_session|scan_delivery|scan_view)\.rs",
                ):
                    api_snapshots._generate_supervisor()

    def test_projector_has_only_project_and_no_native_constructor(self) -> None:
        snapshots = api_snapshots._generate_supervisor()
        signatures = {
            "rust": (
                "impl ScanEventProjection",
                "pub fn project(&self, event: BleEvent) -> Option<ForeignScanEventEnvelope>;",
            ),
            "kotlin": (
                "interface ScanEventProjectionInterface",
                "fun project(event: BleEvent): ForeignScanEventEnvelope?",
            ),
            "swift": (
                "protocol ScanEventProjectionProtocol",
                "func project(event: BleEvent) -> ForeignScanEventEnvelope?",
            ),
        }
        for language, (header, method) in signatures.items():
            with self.subTest(language=language):
                block = (
                    snapshots[language].split(header + " {\n", 1)[1].split("\n}", 1)[0]
                )
                self.assertEqual(block.strip(), method)
                self.assertNotRegex(block, r"\b(?:new|rebind|revoke|retire)\(")
                for private in (
                    "SessionScanProjection",
                    "ScanEventEnvelope",
                    "ScanProjectionLease",
                ):
                    self.assertNotRegex(snapshots[language], rf"\b{private}\b")
        for path, constructor in (
            (api_snapshots.SUPERVISOR_KOTLIN_BINDING, r"\bconstructor\(\)\s*:"),
            (api_snapshots.SUPERVISOR_SWIFT_BINDING, r"\bconvenience init\("),
        ):
            source = path.read_text(encoding="utf-8")
            start = re.search(r"open class ScanEventProjection\b", source).end()
            opening = source.index("{", start)
            closing = api_snapshots._find_matching(source, opening, "{", "}")
            self.assertNotRegex(source[opening:closing], constructor)

    def test_projector_split_impls_exclude_private_methods_and_nested_public_items(
        self,
    ) -> None:
        with self.representative_bindings() as paths:
            baseline, _ = api_snapshots._rust_snapshot(supervisor=True)
            path = paths["scan.rs"]
            source = path.read_text(encoding="utf-8")
            path.write_text(
                source + "\nimpl ScanEventProjection {\n"
                "    fn private_helper(&self) {\n"
                "        fn nested() { pub fn nested_public() {} }\n"
                "    }\n"
                "    pub(crate) fn rebind(&self) {}\n"
                "    pub(super) fn revoke(&self) {}\n"
                "    fn retire(&self) {}\n}\n",
                encoding="utf-8",
            )
            snapshot, _ = api_snapshots._rust_snapshot(supervisor=True)
            self.assertEqual(snapshot, baseline)
            self.assertEqual(
                api_snapshots._rust_public_methods(
                    path.read_text(encoding="utf-8"), "ScanEventProjection"
                ),
                [
                    "pub fn project(&self, event: BleEvent) -> Option<ForeignScanEventEnvelope>"
                ],
            )

    def test_shared_core_modules_require_explicit_uniffi_derives(self) -> None:
        with self.representative_bindings() as paths:
            snapshot, declarations = api_snapshots._rust_snapshot(supervisor=True)
            self.assertIn("ScanEvent", declarations)
            self.assertIn(
                "Started {command_id: CommandId, scan_id: ScanSessionId,}", snapshot
            )
            self.assertIn(
                "Advertisement {advertisement: AdvertisementObservation,}", snapshot
            )
            self.assertIn(
                "Stopped {scan_id: ScanSessionId, result: ScanStopResult,}", snapshot
            )
            for filename in ("core_scan.rs", "error.rs", "scan_delivery.rs"):
                path = paths[filename]
                source = path.read_text(encoding="utf-8")
                path.write_text(
                    source
                    + "\n/// uniffi::Enum and uniffi::Error are documentation, not derives.\n"
                    "#[derive(Debug)]\npub enum CoreOnly { Hidden }\n",
                    encoding="utf-8",
                )
                self.assertEqual(
                    api_snapshots._rust_snapshot(supervisor=True)[0], snapshot
                )
                for derive in ("Error", "Enum", "Record", "Object"):
                    with self.subTest(filename=filename, derive=derive):
                        path.write_text(
                            source
                            + f'\n#[cfg_attr(feature = "ffi", derive(uniffi::{derive}))]\n'
                            + (
                                "pub struct UnknownFfi { pub value: u64 }\n"
                                if derive in {"Record", "Object"}
                                else "pub enum UnknownFfi { Unexpected }\n"
                            ),
                            encoding="utf-8",
                        )
                        with self.assertRaisesRegex(
                            RuntimeError, r"rust.*unexpected=\['UnknownFfi'\]"
                        ):
                            api_snapshots._generate_supervisor()
                path.write_text(source, encoding="utf-8")

    def test_changed_scan_fields_reader_argument_or_project_method_fail_snapshot_check(
        self,
    ) -> None:
        changes = (
            ("rust", "scan_session.rs", "pub queued_bytes: u64,", ""),
            ("rust", "scan_session.rs", "Observation { error: BlengError },", ""),
            ("rust", "scan_delivery.rs", "    PullBusy,\n", ""),
            ("kotlin", "kotlin", "    var `queuedBytes`: kotlin.ULong,\n", ""),
            (
                "kotlin",
                "kotlin",
                "    class PullBusy() : ScanDeliveryException()\n",
                "",
            ),
            (
                "kotlin",
                "kotlin",
                "    data class Observation(val `error`: BlengException) : ScanSessionException()\n",
                "",
            ),
            ("swift", "swift", "    public var queuedBytes: UInt64\n", ""),
            ("swift", "swift", "    case PullBusy\n", ""),
            ("swift", "swift", "    case Observation(error: BlengError)\n", ""),
            ("rust", "scan.rs", "pub source_context: ForeignOwnerContext,", ""),
            ("rust", "scan.rs", "pub event: ScanEvent,", ""),
            ("rust", "adapter.rs", "scan_projection: Arc<ScanEventProjection>,", ""),
            ("rust", "scan.rs", "pub fn project(", "fn project("),
            (
                "rust",
                "scan.rs",
                "impl ScanEventProjection {",
                "impl ScanEventProjection { pub fn new() {}",
            ),
            (
                "rust",
                "scan.rs",
                "impl ScanEventProjection {",
                "impl ScanEventProjection { pub fn rebind(&self) {}",
            ),
            (
                "rust",
                "scan.rs",
                "impl ScanEventProjection {",
                "impl ScanEventProjection { pub fn revoke(&self) {}",
            ),
            ("rust", "core_scan.rs", "command_id: CommandId,", ""),
            ("rust", "core_scan.rs", "advertisement: AdvertisementObservation,", ""),
            ("rust", "core_scan.rs", "result: ScanStopResult,", ""),
            ("kotlin", "kotlin", "    var `sourceContext`: ForeignOwnerContext,\n", ""),
            ("kotlin", "kotlin", "    var `event`: ScanEvent\n", ""),
            ("kotlin", "kotlin", ", `scanProjection`: ScanEventProjection", ""),
            (
                "kotlin",
                "kotlin",
                "    fun `project`(`event`: BleEvent): ForeignScanEventEnvelope?\n",
                "",
            ),
            ("kotlin", "kotlin", "val `commandId`: CommandId, ", ""),
            ("kotlin", "kotlin", "val `advertisement`: AdvertisementObservation", ""),
            ("kotlin", "kotlin", ", val `result`: ScanStopResult", ""),
            (
                "swift",
                "swift",
                "    public var sourceContext: ForeignOwnerContext\n",
                "",
            ),
            ("swift", "swift", "    public var event: ScanEvent\n", ""),
            ("swift", "swift", ", scanProjection: ScanEventProjection", ""),
            (
                "swift",
                "swift",
                "    func project(event: BleEvent) -> ForeignScanEventEnvelope?\n",
                "",
            ),
            ("swift", "swift", "commandId: CommandId, ", ""),
            ("swift", "swift", "advertisement: AdvertisementObservation", ""),
            ("swift", "swift", ", result: ScanStopResult", ""),
        )
        with self.representative_bindings() as paths:
            reviewed = {}
            for language, snapshot in api_snapshots._generate_supervisor().items():
                reviewed[language] = paths["swift"].parent / f"reviewed-{language}.txt"
                reviewed[language].write_text(snapshot, encoding="utf-8")
            for language, filename, old, new in changes:
                with self.subTest(language=language, old=old):
                    path = paths[filename]
                    source = path.read_text(encoding="utf-8")
                    self.assertIn(old, source)
                    path.write_text(source.replace(old, new, 1), encoding="utf-8")
                    with (
                        patch.object(api_snapshots, "_generate", return_value={}),
                        patch.object(
                            api_snapshots, "SUPERVISOR_SNAPSHOT_PATHS", reviewed
                        ),
                        self.assertRaisesRegex(
                            RuntimeError,
                            rf"API snapshots do not match[\s\S]*reviewed-{language}",
                        ),
                    ):
                        api_snapshots.check(bleng=True)
                    path.write_text(source, encoding="utf-8")

    def test_changed_product_fields_variants_or_opaque_visibility_fail_snapshot_check(
        self,
    ) -> None:
        changes = []
        for name, fields in SCAN_VIEW_RECORD_FIELDS.items():
            for language, filename, header, members, closing in (
                (
                    "rust",
                    "scan_view.rs",
                    f"pub struct {name} {{",
                    [f"    pub {row[0]}: {row[2]}," for row in fields],
                    "}",
                ),
                (
                    "kotlin",
                    "kotlin",
                    f"data class {name}(",
                    [f"    var `{row[1]}`: {row[3]}," for row in fields],
                    ")",
                ),
                (
                    "swift",
                    "swift",
                    f"public struct {name} {{",
                    [f"    public var {row[1]}: {row[4]}" for row in fields],
                    "}",
                ),
            ):
                declaration = "\n".join([header, *members, closing])
                for member in members:
                    changes.append(
                        (
                            language,
                            filename,
                            declaration,
                            declaration.replace(member + "\n", "", 1),
                        )
                    )
        for variants in SCAN_VIEW_ENUM_VARIANTS.values():
            for rust, kotlin, swift in variants:
                changes.extend(
                    (
                        ("rust", "scan_view.rs", f"    {rust},", ""),
                        ("kotlin", "kotlin", f"    {kotlin},", ""),
                        ("swift", "swift", f"    case {swift}\n", ""),
                    )
                )
        changes.extend(
            (
                (
                    "rust",
                    "scan_view.rs",
                    "    id: PeripheralId,",
                    "    pub id: PeripheralId,",
                ),
                (
                    "rust",
                    "scan_view.rs",
                    "    context: ForeignOwnerContext,",
                    "    pub context: ForeignOwnerContext,",
                ),
                (
                    "rust",
                    "scan_view.rs",
                    "    operation: Arc<StopScanOperation>,",
                    "    pub operation: Arc<StopScanOperation>,",
                ),
            )
        )
        with self.representative_bindings() as paths:
            reviewed = {}
            for language, snapshot in api_snapshots._generate_supervisor().items():
                reviewed[language] = paths["swift"].parent / f"reviewed-{language}.txt"
                reviewed[language].write_text(snapshot, encoding="utf-8")
            for language, filename, old, new in changes:
                with self.subTest(language=language, old=old):
                    path = paths[filename]
                    source = path.read_text(encoding="utf-8")
                    self.assertIn(old, source)
                    path.write_text(source.replace(old, new, 1), encoding="utf-8")
                    with (
                        patch.object(api_snapshots, "_generate", return_value={}),
                        patch.object(
                            api_snapshots, "SUPERVISOR_SNAPSHOT_PATHS", reviewed
                        ),
                        self.assertRaisesRegex(
                            RuntimeError,
                            rf"API snapshots do not match[\s\S]*reviewed-{language}",
                        ),
                    ):
                        api_snapshots.check(bleng=True)
                    path.write_text(source, encoding="utf-8")

    def test_dropped_startup_or_forwarding_method_changes_reviewed_snapshot(
        self,
    ) -> None:
        with self.representative_bindings() as paths:
            snapshots = api_snapshots._generate_supervisor()
            reviewed = {}
            for language, snapshot in snapshots.items():
                reviewed[language] = paths["swift"].parent / f"reviewed-{language}.txt"
                reviewed[language].write_text(snapshot, encoding="utf-8")
            scan_methods = [
                (language, filename, pattern)
                for row in SUPERVISED_SCAN_METHODS
                for language, filename, pattern in (
                    (
                        "rust",
                        "scan_view.rs"
                        if row in SUPERVISED_SCAN_VIEW_METHODS
                        else "scan_session.rs",
                        re.escape(row[0].split("(", 1)[0]) + r"\b",
                    ),
                    ("kotlin", "kotlin", "    " + re.escape(row[1]) + "\n"),
                    ("swift", "swift", "    " + re.escape(row[2]) + "\n"),
                )
            ]
            scan_methods.extend(
                (language, filename, pattern)
                for methods in SCAN_VIEW_OBJECT_METHODS.values()
                for row in methods
                for language, filename, pattern in (
                    (
                        "rust",
                        "scan_view.rs",
                        re.escape(row[0].split("(", 1)[0]) + r"\b",
                    ),
                    ("kotlin", "kotlin", "    " + re.escape(row[1]) + "\n"),
                    ("swift", "swift", "    " + re.escape(row[2]) + "\n"),
                )
            )
            for language, filename, pattern in (
                *scan_methods,
                ("rust", "scan_view.rs", r"pub fn begin_scan_view\b"),
                ("kotlin", "kotlin", r"    fun `beginScanView`[^\n]*\n"),
                ("swift", "swift", r"    func beginScanView[^\n]*\n"),
                ("rust", "operations.rs", r"pub fn begin_scan\b"),
                ("kotlin", "kotlin", r"    fun `beginScan`[^\n]*\n"),
                ("swift", "swift", r"    func beginScan[^\n]*\n"),
                ("rust", "object.rs", r"pub async fn start\b"),
                ("rust", "object.rs", r"pub async fn shutdown_view\b"),
                ("rust", "object.rs", r"pub fn active_owner_start\b"),
                ("rust", "operations.rs", r"pub fn begin_read_characteristic\b"),
                ("kotlin", "kotlin", r"    suspend fun `start`[^\n]*\n"),
                ("kotlin", "kotlin", r"    suspend fun `shutdownView`[^\n]*\n"),
                ("kotlin", "kotlin", r"    fun `activeOwnerStart`[^\n]*\n"),
                ("kotlin", "kotlin", r"    fun `beginReadCharacteristic`[^\n]*\n"),
                ("swift", "swift", r"    func start[^\n]*\n"),
                ("swift", "swift", r"    func shutdownView[^\n]*\n"),
                ("swift", "swift", r"    func activeOwnerStart[^\n]*\n"),
                ("swift", "swift", r"    func beginReadCharacteristic[^\n]*\n"),
            ):
                with self.subTest(
                    language=language, filename=filename, pattern=pattern
                ):
                    path = paths[filename]
                    source = path.read_text(encoding="utf-8")
                    match = re.search(pattern, source)
                    self.assertIsNotNone(match)
                    closing = match.end()
                    if language == "rust":
                        opening = source.index("{", closing)
                        closing = (
                            api_snapshots._find_matching(source, opening, "{", "}") + 1
                        )
                    path.write_text(
                        source[: match.start()] + source[closing:], encoding="utf-8"
                    )
                    with (
                        patch.object(api_snapshots, "_generate", return_value={}),
                        patch.object(
                            api_snapshots, "SUPERVISOR_SNAPSHOT_PATHS", reviewed
                        ),
                        self.assertRaisesRegex(
                            RuntimeError, "API snapshots do not match"
                        ),
                    ):
                        api_snapshots.check(bleng=True)
                    path.write_text(source, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
