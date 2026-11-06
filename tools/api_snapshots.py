from __future__ import annotations

import argparse
import difflib
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUST_SOURCE_ROOT = PROJECT_ROOT / "crates" / "blehub" / "src"
GENERATED_ROOT = PROJECT_ROOT / ".work" / "api-snapshot-generated"
KOTLIN_BINDING = GENERATED_ROOT / "kotlin" / "dev" / "blehub" / "ffi" / "blehub.kt"
SWIFT_BINDING = GENERATED_ROOT / "swift" / "blehub.swift"
SNAPSHOT_ROOT = PROJECT_ROOT / "api-snapshots"
BLENG_GENERATED_ROOT = PROJECT_ROOT / ".work" / "bleng-snapshot-generated"
BLENG_RUST_SOURCES = (
    PROJECT_ROOT / "crates" / "bleng" / "src" / "facade" / "mod.rs",
    PROJECT_ROOT / "crates" / "bleng" / "src" / "facade" / "operations.rs",
    PROJECT_ROOT / "crates" / "bleng" / "src" / "error.rs",
    PROJECT_ROOT / "crates" / "bleng" / "src" / "operation_observation.rs",
)
BLENG_KOTLIN_BINDING = BLENG_GENERATED_ROOT / "kotlin" / "uniffi" / "bleng" / "bleng.kt"
BLENG_SWIFT_BINDING = BLENG_GENERATED_ROOT / "swift" / "bleng.swift"
SUPERVISOR_RUST_SOURCES = (
    PROJECT_ROOT / "crates" / "bleng-supervisor" / "src" / "ffi" / "adapter.rs",
    PROJECT_ROOT / "crates" / "bleng-supervisor" / "src" / "ffi" / "object.rs",
    PROJECT_ROOT / "crates" / "bleng-supervisor" / "src" / "ffi" / "operations.rs",
    PROJECT_ROOT / "crates" / "bleng-supervisor" / "src" / "ffi" / "types.rs",
    PROJECT_ROOT / "crates" / "bleng-supervisor" / "src" / "ffi" / "scan.rs",
    PROJECT_ROOT / "crates" / "bleng-supervisor" / "src" / "ffi" / "scan_session.rs",
    PROJECT_ROOT / "crates" / "bleng-supervisor" / "src" / "ffi" / "scan_view.rs",
    PROJECT_ROOT / "crates" / "bleng-supervisor" / "src" / "error.rs",
    PROJECT_ROOT / "crates" / "bleng-supervisor" / "src" / "scan.rs",
    PROJECT_ROOT / "crates" / "bleng-supervisor" / "src" / "scan_delivery.rs",
)
SUPERVISOR_KOTLIN_BINDING = (
    BLENG_GENERATED_ROOT
    / "kotlin"
    / "uniffi"
    / "bleng_supervisor"
    / "bleng_supervisor.kt"
)
SUPERVISOR_SWIFT_BINDING = BLENG_GENERATED_ROOT / "swift" / "bleng_supervisor.swift"
SUPERVISOR_SNAPSHOT_PATHS = {
    language: SNAPSHOT_ROOT / f"supervisor-{language}.txt"
    for language in ("rust", "kotlin", "swift")
}
SUPERVISOR_DECLARATIONS = {
    "rust": {
        "ForeignPlatformOwnerSession",
        "ForeignPlatformOwnerSessionFactory",
        "ForeignPlatformOwnerSessionFactoryAdapter",
        "ForeignOwnerClose",
        "ForeignOwnerCloseFailure",
        "ForeignExternalRecoverySignal",
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
        "SupervisorFfiError",
        "SupervisorOperationError",
        "Supervisor",
        "ForeignScanEventEnvelope",
        "ScanEvent",
        "ScanEventProjection",
        "SupervisedScan",
        "ScanSessionError",
        "ScanDeliveryError",
        "ForeignScanDeliverySnapshot",
        "ForeignScanFailureDomain",
        "ForeignScanFailure",
        "ForeignScanOptions",
        "ForeignScanCommandResult",
        "ForeignScanBeginResult",
        "ForeignScanStopResult",
        "ForeignScanObservation",
        "ForeignScanAdvertisement",
        "ForeignScanEndReason",
        "ForeignScanEnd",
        "SupervisedPeripheral",
        "SupervisedScanStop",
    },
    "kotlin": {
        "ForeignPlatformOwnerSession",
        "ForeignPlatformOwnerSessionFactory",
        "SupervisorInterface",
        "ForeignOwnerClose",
        "ForeignOwnerCloseFailure",
        "Supervisor",
        "ForeignExternalRecoverySignal",
        "ForeignOwnerContext",
        "ForeignOwnerStart",
        "ForeignPlatformActionResult",
        "ForeignRecoveryLink",
        "ForeignRecoveryMode",
        "ForeignRecoveryOutcome",
        "ForeignRecoveryObservation",
        "ForeignRecoveryPlan",
        "ForeignRecoveryPolicyConfig",
        "ForeignRecoveryTarget",
        "ForeignResourceCount",
        "ForeignSupervisorConfig",
        "ForeignPlatformRecoveryAction",
        "ForeignRecoveryAction",
        "ForeignRecoverySignal",
        "SupervisorFfiException",
        "SupervisorOperationException",
        "ForeignScanEventEnvelope",
        "ScanEvent",
        "ScanEventProjection",
        "ScanEventProjectionInterface",
        "SupervisedScan",
        "SupervisedScanInterface",
        "ScanSessionException",
        "ScanDeliveryException",
        "ForeignScanDeliverySnapshot",
        "ForeignScanFailureDomain",
        "ForeignScanFailure",
        "ForeignScanOptions",
        "ForeignScanCommandResult",
        "ForeignScanBeginResult",
        "ForeignScanStopResult",
        "ForeignScanObservation",
        "ForeignScanAdvertisement",
        "ForeignScanEndReason",
        "ForeignScanEnd",
        "SupervisedPeripheral",
        "SupervisedPeripheralInterface",
        "SupervisedScanStop",
        "SupervisedScanStopInterface",
    },
    "swift": {
        "ForeignPlatformOwnerSession",
        "ForeignPlatformOwnerSessionFactory",
        "SupervisorProtocol",
        "ForeignOwnerClose",
        "ForeignOwnerCloseFailure",
        "Supervisor",
        "ForeignExternalRecoverySignal",
        "ForeignOwnerContext",
        "ForeignOwnerStart",
        "ForeignPlatformActionResult",
        "ForeignRecoveryLink",
        "ForeignRecoveryMode",
        "ForeignRecoveryOutcome",
        "ForeignRecoveryObservation",
        "ForeignRecoveryPlan",
        "ForeignRecoveryPolicyConfig",
        "ForeignRecoveryTarget",
        "ForeignResourceCount",
        "ForeignSupervisorConfig",
        "ForeignPlatformRecoveryAction",
        "ForeignRecoveryAction",
        "ForeignRecoverySignal",
        "SupervisorFfiError",
        "SupervisorOperationError",
        "ForeignScanEventEnvelope",
        "ScanEvent",
        "ScanEventProjection",
        "ScanEventProjectionProtocol",
        "SupervisedScan",
        "SupervisedScanProtocol",
        "ScanSessionError",
        "ScanDeliveryError",
        "ForeignScanDeliverySnapshot",
        "ForeignScanFailureDomain",
        "ForeignScanFailure",
        "ForeignScanOptions",
        "ForeignScanCommandResult",
        "ForeignScanBeginResult",
        "ForeignScanStopResult",
        "ForeignScanObservation",
        "ForeignScanAdvertisement",
        "ForeignScanEndReason",
        "ForeignScanEnd",
        "SupervisedPeripheral",
        "SupervisedPeripheralProtocol",
        "SupervisedScanStop",
        "SupervisedScanStopProtocol",
    },
}
BLENG_OPERATIONS = {
    "InitializeOperation",
    "StartScanOperation",
    "StopScanOperation",
    "ConnectOperation",
    "DisconnectOperation",
    "DiscoverGattOperation",
    "ReadOperation",
    "WriteOperation",
    "EnableNotificationsOperation",
    "DisableNotificationsOperation",
    "ReadRssiOperation",
}

BLENG_DECLARATIONS = {
    "rust": {
        "Bleng",
        "BlengBusyReason",
        "BlengContractFailure",
        "BlengError",
        "BlengNotReadyReason",
        "BlengResource",
        "OperationFailureObserver",
        "OperationFailureRegistration",
    }
    | BLENG_OPERATIONS,
    "kotlin": {
        "Bleng",
        "BlengInterface",
        "BlengBusyReason",
        "BlengContractFailure",
        "BlengException",
        "BlengNotReadyReason",
        "BlengResource",
    }
    | BLENG_OPERATIONS
    | {f"{name}Interface" for name in BLENG_OPERATIONS},
    "swift": {
        "Bleng",
        "BlengProtocol",
        "BlengBusyReason",
        "BlengContractFailure",
        "BlengError",
        "BlengNotReadyReason",
        "BlengResource",
    }
    | BLENG_OPERATIONS
    | {f"{name}Protocol" for name in BLENG_OPERATIONS},
}
BLENG_SNAPSHOT_PATHS = {
    language: SNAPSHOT_ROOT / f"bleng-{language}.txt"
    for language in ("rust", "kotlin", "swift")
}

SNAPSHOT_PATHS = {
    "rust": SNAPSHOT_ROOT / "rust.txt",
    "kotlin": SNAPSHOT_ROOT / "kotlin.txt",
    "swift": SNAPSHOT_ROOT / "swift.txt",
}

MANDATORY_DECLARATIONS = {
    "rust": {
        "BackendCapabilities",
        "BackendEventIngressHandle",
        "BleBackend",
        "BleEvent",
        "BleHub",
    },
    "kotlin": {
        "BackendCapabilities",
        "BackendEventIngressHandle",
        "BleBackend",
        "BleEvent",
    },
    "swift": {
        "BackendCapabilities",
        "BackendEventIngressHandle",
        "BleBackend",
        "BleEvent",
    },
}


def _collapse(value: str) -> str:
    value = re.sub(r"\s+", " ", value.strip())
    value = re.sub(r"\s+([,;:)\]>}])", r"\1", value)
    value = re.sub(r"([(\[<{])\s+", r"\1", value)
    return value


def _find_matching(source: str, opening: int, opener: str, closer: str) -> int:
    depth = 0
    index = opening
    quote: str | None = None
    escaped = False
    line_comment = False
    block_comment_depth = 0

    while index < len(source):
        character = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""

        if line_comment:
            if character == "\n":
                line_comment = False
            index += 1
            continue
        if block_comment_depth:
            if character == "/" and following == "*":
                block_comment_depth += 1
                index += 2
                continue
            if character == "*" and following == "/":
                block_comment_depth -= 1
                index += 2
                continue
            index += 1
            continue
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            index += 1
            continue
        if character == "/" and following == "/":
            line_comment = True
            index += 2
            continue
        if character == "/" and following == "*":
            block_comment_depth = 1
            index += 2
            continue
        if character == '"':
            quote = character
            index += 1
            continue
        if character == opener:
            depth += 1
        elif character == closer:
            depth -= 1
            if depth == 0:
                return index
        index += 1

    raise RuntimeError(f"unbalanced {opener}{closer} block at offset {opening}")


def _split_top_level(value: str, delimiter: str = ",") -> list[str]:
    parts: list[str] = []
    start = 0
    depths = {"(": 0, "[": 0, "{": 0, "<": 0}
    closers = {
        ")": "(",
        "]": "[",
        "}": "{",
        ">": "<",
    }
    quote: str | None = None
    escaped = False

    for index, character in enumerate(value):
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character == '"':
            quote = character
            continue
        if character in depths:
            depths[character] += 1
            continue
        if character in closers:
            opener = closers[character]
            depths[opener] = max(0, depths[opener] - 1)
            continue
        if character == delimiter and not any(depths.values()):
            parts.append(value[start:index])
            start = index + 1
    parts.append(value[start:])
    return parts


def _without_rust_noise(value: str) -> str:
    lines = []
    for line in value.splitlines():
        stripped = line.strip()
        if stripped.startswith(("///", "//!", "#[")):
            continue
        lines.append(line)
    return "\n".join(lines)


def _without_rust_test_modules(source: str) -> str:
    # A test module can precede or sit between production impls. Remove only
    # that item, not the rest of the file, and do not scan its public helpers.
    pattern = re.compile(
        r"(?m)^[ \t]*#\[cfg\(test\)\]\s*"
        r"(?:#\[[^\n]*\]\s*)*"
        r"(?:pub(?:\([^)]*\))?\s+)?mod\s+[A-Za-z_][A-Za-z0-9_]*\s*"
        r"(?P<terminator>[;{])"
    )
    parts = []
    previous = 0
    for match in pattern.finditer(source):
        if match.start() < previous:
            continue
        parts.append(source[previous : match.start()])
        if match.group("terminator") == "{":
            previous = _find_matching(source, match.end() - 1, "{", "}") + 1
        else:
            previous = match.end()
    parts.append(source[previous:])
    return "".join(parts)


def _rust_public_methods(source: str, type_name: str) -> list[str]:
    methods: list[str] = []
    pattern = re.compile(rf"(?m)^[ \t]*impl\s+{re.escape(type_name)}\s*\{{")
    for implementation in pattern.finditer(source):
        opening = source.find("{", implementation.start())
        closing = _find_matching(source, opening, "{", "}")
        body = source[opening + 1 : closing]
        body_offset = opening + 1
        depth = 0
        index = 0
        while index < len(body):
            if body[index] == "{":
                depth += 1
                index += 1
                continue
            if body[index] == "}":
                depth -= 1
                index += 1
                continue
            method = re.match(r"pub\s+(?:async\s+)?(?:const\s+)?fn\s+", body[index:])
            if method and depth == 0:
                signature_start = index
                signature_opening = source.find("{", body_offset + index)
                if signature_opening < 0 or signature_opening > closing:
                    raise RuntimeError(
                        f"cannot find body for {type_name} public method"
                    )
                methods.append(
                    _collapse(source[body_offset + signature_start : signature_opening])
                )
                method_closing = _find_matching(source, signature_opening, "{", "}")
                index = method_closing - body_offset + 1
                continue
            index += 1
    return methods


def _rust_snapshot(
    *, bleng: bool = False, supervisor: bool = False
) -> tuple[str, set[str]]:
    if bleng and supervisor:
        raise ValueError("BLeng and supervisor snapshots are mutually exclusive")
    definitions: dict[str, str] = {}
    source_by_file: dict[Path, str] = {}

    paths = (
        list(SUPERVISOR_RUST_SOURCES)
        if supervisor
        else list(BLENG_RUST_SOURCES)
        if bleng
        else sorted(RUST_SOURCE_ROOT.rglob("*.rs"))
    )
    for path in paths:
        source = path.read_text(encoding="utf-8")
        source = _without_rust_test_modules(source)
        source_by_file[path] = source

        declaration_pattern = re.compile(
            r"(?m)^(?P<attributes>(?:[ \t]*#\[[^\n]*\]\n|[ \t]*///[^\n]*\n)*)"
            r"[ \t]*(?P<declaration>pub\s+(?P<kind>const|struct|enum|trait)\s+"
            r"(?P<name>[A-Za-z_][A-Za-z0-9_]*))"
        )
        for match in declaration_pattern.finditer(source):
            kind = match.group("kind")
            name = match.group("name")
            # Shared core modules mix Rust-only types with explicitly derived FFI types.
            # Detect exports independently of the allowlist so unknown exports still fail.
            if (
                supervisor
                and path.name in {"error.rs", "scan.rs", "scan_delivery.rs"}
                and path.parent.name != "ffi"
                and not re.search(
                    r"(?m)^[ \t]*#\[[^\n]*\bderive\([^)]*\buniffi::(?:Error|Enum|Record|Object)\b",
                    match.group("attributes"),
                )
            ):
                continue
            declaration_start = match.start("declaration")
            if kind == "const":
                end = source.find(";", declaration_start)
                definitions[name] = _collapse(source[declaration_start : end + 1])
                continue

            semicolon = source.find(";", declaration_start)
            opening = source.find("{", declaration_start)
            if semicolon >= 0 and (opening < 0 or semicolon < opening):
                definitions[name] = _collapse(source[declaration_start : semicolon + 1])
                continue
            if opening < 0:
                raise RuntimeError(f"cannot find body for Rust {kind} {name}")
            closing = _find_matching(source, opening, "{", "}")
            header = _collapse(source[declaration_start:opening])
            body = _without_rust_noise(source[opening + 1 : closing])

            if kind == "struct":
                fields = [
                    _collapse(field)
                    for field in _split_top_level(body)
                    if field.strip().startswith("pub ")
                ]
                if fields:
                    rendered = "\n".join(
                        [f"{header} {{", *(f"    {field}," for field in fields), "}"]
                    )
                else:
                    rendered = f"{header};"
            elif kind == "enum":
                variants = [
                    _collapse(variant)
                    for variant in _split_top_level(body)
                    if variant.strip()
                ]
                rendered = "\n".join(
                    [f"{header} {{", *(f"    {variant}," for variant in variants), "}"]
                )
            else:
                methods = [
                    _collapse(method) + ";"
                    for method in _split_top_level(body, ";")
                    if method.strip().startswith("fn ")
                ]
                rendered = "\n".join(
                    [f"{header} {{", *(f"    {method}" for method in methods), "}"]
                )
            definitions[name] = rendered

    for name in list(definitions):
        if supervisor and name not in {
            "Supervisor",
            "ScanEventProjection",
            "SupervisedScan",
            "SupervisedPeripheral",
            "SupervisedScanStop",
        }:
            continue
        if not definitions[name].startswith(("pub struct", "pub enum")):
            continue
        methods = []
        for source in source_by_file.values():
            methods.extend(_rust_public_methods(source, name))
        if methods:
            definitions[name] += "\n\n" + "\n".join(
                [f"impl {name} {{", *(f"    {method};" for method in methods), "}"]
            )

    if not definitions:
        raise RuntimeError("Rust API extraction produced no declarations")
    rendered = "\n\n".join(definitions[name] for name in sorted(definitions))
    header = (
        "# Reviewed BLeng public API shape; ABI remains unfrozen."
        if bleng
        else "# Reviewed BLeng supervisor FFI API shape; ABI remains unfrozen."
        if supervisor
        else "# Reviewed Rust public API shape. Generated by `just binding snapshot-update`."
    )
    return f"{header}\n\n{rendered}\n", set(definitions)


def _kotlin_type(value: str) -> str:
    value = value.replace("`", "")
    value = value.replace("dev.blehub.ffi.", "")
    value = value.replace("kotlin.", "")
    return _collapse(value)


def _kotlin_snapshot(
    *, bleng: bool = False, supervisor: bool = False
) -> tuple[str, set[str]]:
    if bleng and supervisor:
        raise ValueError("BLeng and supervisor snapshots are mutually exclusive")
    binding = (
        SUPERVISOR_KOTLIN_BINDING
        if supervisor
        else BLENG_KOTLIN_BINDING
        if bleng
        else KOTLIN_BINDING
    )
    source = binding.read_text(encoding="utf-8")
    declarations = SUPERVISOR_DECLARATIONS["kotlin"] if supervisor else None
    definitions: dict[str, str] = {}
    pattern = re.compile(
        r"(?m)^(?P<kind>public interface|open class|data class|enum class|sealed class)\s+"
        r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    )

    for match in pattern.finditer(source):
        kind = match.group("kind")
        name = match.group("name")
        if name in {"RustBuffer", "FfiConverter", "FfiConverterRustBuffer"}:
            continue
        if supervisor and name in {
            "ForeignPlatformOwnerSessionImpl",
            "ForeignPlatformOwnerSessionFactoryImpl",
        }:
            continue
        if supervisor and name not in declarations:
            raise RuntimeError(
                f"supervisor kotlin API snapshot declarations: missing=[], unexpected={[name]}"
            )
        if bleng and name not in BLENG_DECLARATIONS["kotlin"]:
            raise RuntimeError(
                f"kotlin API snapshot declarations: missing=[], unexpected={[name]}"
            )
        if (
            kind == "public interface"
            and not bleng
            and not supervisor
            and name
            not in {
                "BackendEventIngressHandleInterface",
                "BleBackend",
            }
        ):
            continue
        if kind == "open class":
            if bleng and name in BLENG_OPERATIONS:
                definitions[name] = f"class {name} : {name}Interface"
            elif bleng and name == "Bleng":
                definitions[name] = "class Bleng : BlengInterface"
                class_body_start = source.find("{", match.end())
                class_body_end = _find_matching(source, class_body_start, "{", "}")
                if not re.search(
                    r"(?m)^\s*constructor\(\)\s*:",
                    source[class_body_start:class_body_end],
                ):
                    raise RuntimeError("BLeng Kotlin constructor is missing")

            elif supervisor and name in {
                "Supervisor",
                "ScanEventProjection",
                "SupervisedScan",
                "SupervisedPeripheral",
                "SupervisedScanStop",
            }:
                definitions[name] = f"class {name} : {name}Interface"
            elif name == "BackendEventIngressHandle":
                definitions[name] = (
                    "class BackendEventIngressHandle : BackendEventIngressHandleInterface"
                )
            continue
        opening = source.find("{", match.end())
        if opening < 0:
            raise RuntimeError(f"cannot find body for Kotlin {kind} {name}")
        closing = _find_matching(source, opening, "{", "}")
        body = source[opening + 1 : closing]

        if kind == "data class":
            parameter_opening = source.find("(", match.end())
            parameter_closing = _find_matching(source, parameter_opening, "(", ")")
            parameters = [
                _kotlin_type(parameter)
                for parameter in _split_top_level(
                    source[parameter_opening + 1 : parameter_closing]
                )
                if parameter.strip()
            ]
            definitions[name] = "\n".join(
                [f"data class {name}(", *(f"    {item}," for item in parameters), ")"]
            )
        elif kind == "public interface":
            methods = [
                _kotlin_type(line)
                for line in body.splitlines()
                if line.strip().startswith(("fun ", "suspend fun "))
            ]
            definitions[name] = "\n".join(
                [f"interface {name} {{", *(f"    {item}" for item in methods), "}"]
            )
        elif kind == "enum class":
            variants = []
            depth = 0
            for line in body.splitlines():
                stripped = line.strip()
                if depth == 0 and re.fullmatch(r"[A-Z][A-Z0-9_]*[,;]?", stripped):
                    variants.append(stripped.rstrip(",;"))
                depth += line.count("{") - line.count("}")
            if not variants:
                raise RuntimeError(
                    f"Kotlin enum class {name} has no extracted variants"
                )
            definitions[name] = "\n".join(
                [f"enum class {name} {{", *(f"    {item}," for item in variants), "}"]
            )
        else:
            variant_entries: list[tuple[int, str]] = []
            class_pattern = re.compile(
                rf"(?:data\s+)?class\s+(?P<variant>[A-Za-z_][A-Za-z0-9_]*)\s*"
                rf"\((?P<parameters>.*?)\)\s*:\s*{re.escape(name)}\(\)",
                re.DOTALL,
            )
            for variant in class_pattern.finditer(body):
                parameters = [
                    _kotlin_type(parameter).removeprefix("val ")
                    for parameter in _split_top_level(variant.group("parameters"))
                    if parameter.strip()
                ]
                rendered = (
                    f"{variant.group('variant')}({', '.join(parameters)})"
                    if parameters
                    else variant.group("variant")
                )
                variant_entries.append((variant.start(), rendered))
            object_pattern = re.compile(
                rf"(?m)^\s*(?:data\s+)?object\s+(?P<variant>[A-Za-z_][A-Za-z0-9_]*)\s*"
                rf":\s*{re.escape(name)}\(\)"
            )
            variant_entries.extend(
                (variant.start(), variant.group("variant"))
                for variant in object_pattern.finditer(body)
            )
            variants = [item for _, item in sorted(variant_entries)]
            if not variants:
                raise RuntimeError(
                    f"Kotlin sealed class {name} has no extracted variants"
                )
            definitions[name] = "\n".join(
                [f"sealed class {name} {{", *(f"    {item}," for item in variants), "}"]
            )

    if not definitions:
        raise RuntimeError("Kotlin API extraction produced no declarations")
    rendered = "\n\n".join(definitions[name] for name in sorted(definitions))
    header = (
        "# Reviewed BLeng Kotlin API shape; ABI remains unfrozen."
        if bleng
        else "# Reviewed BLeng supervisor Kotlin API shape; ABI remains unfrozen."
        if supervisor
        else "# Reviewed generated Kotlin API shape. Generated by `just binding snapshot-update`."
    )
    return f"{header}\n\n{rendered}\n", set(definitions)


def _direct_swift_members(body: str, keyword: str) -> list[str]:
    members: list[str] = []
    depth = 0
    lines = body.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if depth == 0 and stripped.startswith(f"{keyword} "):
            member = stripped
            parentheses = member.count("(") - member.count(")")
            while parentheses > 0 and index + 1 < len(lines):
                index += 1
                continuation = lines[index].strip()
                member += " " + continuation
                parentheses += continuation.count("(") - continuation.count(")")
            members.append(_collapse(member))
        depth += line.count("{") - line.count("}")
        index += 1
    return members


def _swift_snapshot(
    *, bleng: bool = False, supervisor: bool = False
) -> tuple[str, set[str]]:
    if bleng and supervisor:
        raise ValueError("BLeng and supervisor snapshots are mutually exclusive")
    binding = (
        SUPERVISOR_SWIFT_BINDING
        if supervisor
        else BLENG_SWIFT_BINDING
        if bleng
        else SWIFT_BINDING
    )
    source = binding.read_text(encoding="utf-8")
    declarations = SUPERVISOR_DECLARATIONS["swift"] if supervisor else None
    if bleng or supervisor:
        # UniFFI 0.32 emits `public` and the error `enum` on separate lines.
        source = re.sub(r"(?m)^public\s*\n\s*enum ", "public enum ", source)
    definitions: dict[str, str] = {}
    pattern = re.compile(
        r"(?m)^(?P<visibility>public|open)\s+"
        r"(?P<kind>protocol|struct|enum|class)\s+"
        r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    )

    for match in pattern.finditer(source):
        kind = match.group("kind")
        name = match.group("name")
        if name.startswith(("FfiConverter", "Uniffi")) or name in {
            "InitializationResult",
            "RustBuffer",
        }:
            continue
        if supervisor and name in {
            "ForeignPlatformOwnerSessionImpl",
            "ForeignPlatformOwnerSessionFactoryImpl",
        }:
            continue
        if supervisor and name not in declarations:
            raise RuntimeError(
                f"supervisor swift API snapshot declarations: missing=[], unexpected={[name]}"
            )
        if bleng and name not in BLENG_DECLARATIONS["swift"]:
            raise RuntimeError(
                f"swift API snapshot declarations: missing=[], unexpected={[name]}"
            )
        if kind == "class":
            if bleng and name in BLENG_OPERATIONS:
                definitions[name] = f"class {name} : {name}Protocol"
            elif bleng and name == "Bleng":
                definitions[name] = "class Bleng : BlengProtocol"
                class_body_start = source.find("{", match.end())
                class_body_end = _find_matching(source, class_body_start, "{", "}")
                if (
                    "public convenience init()"
                    not in source[class_body_start:class_body_end]
                ):
                    raise RuntimeError("BLeng Swift initializer is missing")

            elif supervisor and name in {
                "Supervisor",
                "ScanEventProjection",
                "SupervisedScan",
                "SupervisedPeripheral",
                "SupervisedScanStop",
            }:
                definitions[name] = f"class {name} : {name}Protocol"
            elif name == "BackendEventIngressHandle":
                definitions[name] = (
                    "class BackendEventIngressHandle : BackendEventIngressHandleProtocol"
                )
            continue
        opening = source.find("{", match.end())
        if opening < 0:
            raise RuntimeError(f"cannot find body for Swift {kind} {name}")
        closing = _find_matching(source, opening, "{", "}")
        body = source[opening + 1 : closing]

        if kind == "struct":
            fields = _direct_swift_members(body, "public var")
            definitions[name] = "\n".join(
                [f"struct {name} {{", *(f"    {item}" for item in fields), "}"]
            )
        elif kind == "protocol":
            methods = _direct_swift_members(body, "func")
            definitions[name] = "\n".join(
                [f"protocol {name} {{", *(f"    {item}" for item in methods), "}"]
            )
        else:
            variants = _direct_swift_members(body, "case")
            definitions[name] = "\n".join(
                [f"enum {name} {{", *(f"    {item}" for item in variants), "}"]
            )

    if not definitions:
        raise RuntimeError("Swift API extraction produced no declarations")
    rendered = "\n\n".join(definitions[name] for name in sorted(definitions))
    header = (
        "# Reviewed BLeng Swift API shape; ABI remains unfrozen."
        if bleng
        else "# Reviewed BLeng supervisor Swift API shape; ABI remains unfrozen."
        if supervisor
        else "# Reviewed generated Swift API shape. Generated by `just binding snapshot-update`."
    )
    return f"{header}\n\n{rendered}\n", set(definitions)


def _generate(*, bleng: bool = False) -> dict[str, str]:
    kotlin_binding = BLENG_KOTLIN_BINDING if bleng else KOTLIN_BINDING
    swift_binding = BLENG_SWIFT_BINDING if bleng else SWIFT_BINDING
    missing_inputs = [
        path for path in (kotlin_binding, swift_binding) if not path.is_file()
    ]
    if missing_inputs:
        rendered = "\n".join(
            f"- {path.relative_to(PROJECT_ROOT) if path.is_relative_to(PROJECT_ROOT) else path}"
            for path in missing_inputs
        )
        raise RuntimeError(f"generated bindings are missing:\n{rendered}")

    if bleng:
        binding_files = {
            path.relative_to(BLENG_GENERATED_ROOT).as_posix()
            for path in BLENG_GENERATED_ROOT.rglob("*")
            if path.is_file()
        }
        expected_files = {
            "kotlin/dev/blehub/ffi/blehub.kt",
            "kotlin/uniffi/bleng/bleng.kt",
            "swift/blehub.swift",
            "swift/bleng.swift",
            "swift/blehubFFI.h",
            "swift/blengFFI.h",
            "swift/blehubFFI.modulemap",
            "swift/blengFFI.modulemap",
        }
        optional_supervisor_files = {
            "kotlin/uniffi/bleng_supervisor/bleng_supervisor.kt",
            "swift/bleng_supervisor.swift",
            "swift/bleng_supervisorFFI.h",
            "swift/bleng_supervisorFFI.modulemap",
        }
        allowed_files = expected_files | optional_supervisor_files
        missing = expected_files - binding_files
        unexpected = binding_files - allowed_files
        if missing or unexpected:
            raise RuntimeError(
                "BLeng bindings are missing canonical components or contain unknown files: "
                f"missing={sorted(missing)}, unexpected={sorted(unexpected)}"
            )
        kotlin_source = kotlin_binding.read_text(encoding="utf-8")
        if not all(
            f"import dev.blehub.ffi.{name}\n" in kotlin_source
            for name in ("BleBackend", "BleEvent", "AdapterSnapshot")
        ):
            raise RuntimeError(
                "BLeng Kotlin binding must import canonical blehub types"
            )
        for name in ("BleBackend", "BleEvent", "AdapterSnapshot", "ScanOptions"):
            if re.search(
                rf"(?m)^(?:public interface|open class|data class|sealed class|enum class) {name}\b",
                kotlin_source,
            ):
                raise RuntimeError(f"BLeng Kotlin binding duplicates blehub {name}")
        swift_source = swift_binding.read_text(encoding="utf-8")
        if any(
            re.search(
                rf"(?m)^(?:public|open) (?:protocol|class|struct|enum) {name}\b",
                swift_source,
            )
            for name in ("BleBackend", "BleEvent", "AdapterSnapshot", "ScanOptions")
        ):
            raise RuntimeError("BLeng Swift binding duplicates canonical blehub types")
    snapshots: dict[str, str] = {}
    for language, extractor in {
        "rust": _rust_snapshot,
        "kotlin": _kotlin_snapshot,
        "swift": _swift_snapshot,
    }.items():
        snapshot, declarations = extractor(bleng=bleng)
        mandatory = (
            BLENG_DECLARATIONS[language] if bleng else MANDATORY_DECLARATIONS[language]
        )
        missing = mandatory - declarations
        unexpected = declarations - mandatory if bleng else set()
        if missing or unexpected:
            raise RuntimeError(
                f"{language} API snapshot declarations: "
                f"missing={sorted(missing)}, unexpected={sorted(unexpected)}"
            )
        snapshots[language] = snapshot
    return snapshots


def _generate_supervisor() -> dict[str, str]:
    missing_inputs = [
        path
        for path in (
            *SUPERVISOR_RUST_SOURCES,
            SUPERVISOR_KOTLIN_BINDING,
            SUPERVISOR_SWIFT_BINDING,
        )
        if not path.is_file()
    ]
    if missing_inputs:
        rendered = "\n".join(
            f"- {path.relative_to(PROJECT_ROOT) if path.is_relative_to(PROJECT_ROOT) else path}"
            for path in missing_inputs
        )
        raise RuntimeError(f"supervisor snapshot inputs are missing:\n{rendered}")

    snapshots: dict[str, str] = {}
    for language, extractor in {
        "rust": _rust_snapshot,
        "kotlin": _kotlin_snapshot,
        "swift": _swift_snapshot,
    }.items():
        snapshot, declarations = extractor(supervisor=True)
        mandatory = SUPERVISOR_DECLARATIONS[language]
        missing = mandatory - declarations
        unexpected = declarations - mandatory
        if missing or unexpected:
            raise RuntimeError(
                f"supervisor {language} API snapshot declarations: "
                f"missing={sorted(missing)}, unexpected={sorted(unexpected)}"
            )
        snapshots[language] = snapshot
    return snapshots


def update(*, bleng: bool = False) -> None:
    snapshots = _generate(bleng=bleng)
    entries = [
        (
            BLENG_SNAPSHOT_PATHS[language] if bleng else SNAPSHOT_PATHS[language],
            snapshot,
        )
        for language, snapshot in snapshots.items()
    ]
    if bleng:
        entries.extend(
            (SUPERVISOR_SNAPSHOT_PATHS[language], snapshot)
            for language, snapshot in _generate_supervisor().items()
        )
    SNAPSHOT_ROOT.mkdir(parents=True, exist_ok=True)
    for path, snapshot in entries:
        path.write_text(snapshot, encoding="utf-8", newline="\n")
    print(
        f"updated {len(entries)} API snapshots under {SNAPSHOT_ROOT.relative_to(PROJECT_ROOT)}"
    )


def check(*, bleng: bool = False) -> None:
    entries = [
        (
            BLENG_SNAPSHOT_PATHS[language] if bleng else SNAPSHOT_PATHS[language],
            snapshot,
        )
        for language, snapshot in _generate(bleng=bleng).items()
    ]
    if bleng:
        entries.extend(
            (SUPERVISOR_SNAPSHOT_PATHS[language], snapshot)
            for language, snapshot in _generate_supervisor().items()
        )
    failures: list[str] = []
    for path, actual in entries:
        if not path.is_file():
            failures.append(f"missing snapshot: {path.relative_to(PROJECT_ROOT)}")
            continue
        expected = path.read_text(encoding="utf-8")
        if expected == actual:
            continue
        diff = difflib.unified_diff(
            expected.splitlines(),
            actual.splitlines(),
            fromfile=str(path.relative_to(PROJECT_ROOT)),
            tofile=f"generated-{path.stem}-api",
            lineterm="",
        )
        failures.append("\n".join(diff))

    if failures:
        raise RuntimeError(
            "API snapshots do not match the current reviewed API. "
            "Review the diff, then run the matching `pixi run just binding "
            "snapshot-update` or `pixi run just binding bleng-snapshot-update` "
            "recipe to accept intentional changes.\n\n" + "\n\n".join(failures)
        )
    print(f"verified {len(entries)} reviewed API snapshots")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Update or verify reviewed Rust/Kotlin/Swift public API snapshots"
    )
    parser.add_argument("action", choices=("update", "check"))
    parser.add_argument(
        "--bleng", action="store_true", help="check or update the BLeng API snapshots"
    )
    arguments = parser.parse_args()

    if arguments.action == "update":
        update(bleng=arguments.bleng)
    else:
        check(bleng=arguments.bleng)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
