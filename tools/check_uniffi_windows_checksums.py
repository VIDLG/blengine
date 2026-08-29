from __future__ import annotations

import argparse
import ctypes
import re
from pathlib import Path

CHECKSUM_PATTERN = re.compile(
    r"if\s*\(\s*\(?\s*lib\."
    r"(?P<symbol>uniffi_[A-Za-z0-9_]+_checksum_[A-Za-z0-9_]+)\(\)"
    r"(?:\s+and\s+0xFFFF\))?\s*!=\s*(?P<expected>\d+)\s*\)"
)
CONTRACT_PATTERN = re.compile(
    r"val\s+bindings_contract_version\s*=\s*(?P<expected>\d+)"
    r".*?val\s+scaffolding_contract_version\s*=\s*lib\."
    r"(?P<symbol>ffi_[A-Za-z0-9_]+_uniffi_contract_version)\(\)",
    re.DOTALL,
)


def _record_expected(
    expected_by_symbol: dict[str, int], symbol: str, expected: int
) -> None:
    previous = expected_by_symbol.setdefault(symbol, expected)
    if previous != expected:
        raise RuntimeError(
            f"generated bindings disagree for {symbol}: {previous} != {expected}"
        )


def _load_expected(generated_dir: Path) -> tuple[dict[str, int], dict[str, int]]:
    checksums: dict[str, int] = {}
    contracts: dict[str, int] = {}
    kotlin_files = sorted(generated_dir.rglob("*.kt"))
    if not kotlin_files:
        raise RuntimeError(f"no generated Kotlin files found under {generated_dir}")

    for kotlin_file in kotlin_files:
        source = kotlin_file.read_text(encoding="utf-8")
        for match in CHECKSUM_PATTERN.finditer(source):
            _record_expected(
                checksums,
                match.group("symbol"),
                int(match.group("expected")),
            )
        for match in CONTRACT_PATTERN.finditer(source):
            _record_expected(
                contracts,
                match.group("symbol"),
                int(match.group("expected")),
            )

    if not checksums:
        raise RuntimeError(
            "no generated UniFFI checksum comparisons found; "
            "use bindings generated without omit_checksums"
        )
    if not contracts:
        raise RuntimeError("no generated UniFFI contract-version comparisons found")
    return checksums, contracts


def _call_u32(library: ctypes.CDLL, symbol: str) -> int:
    function = getattr(library, symbol)
    function.argtypes = []
    function.restype = ctypes.c_uint32
    return int(function())


def check(library_path: Path, generated_dir: Path) -> None:
    checksums, contracts = _load_expected(generated_dir)
    library = ctypes.CDLL(str(library_path.resolve()))
    failures: list[str] = []

    for symbol, expected in sorted(checksums.items()):
        raw_actual = _call_u32(library, symbol)
        actual = raw_actual & 0xFFFF
        if actual != expected:
            failures.append(
                f"{symbol}: checksum {actual} != {expected} "
                f"(raw Windows carrier: {raw_actual})"
            )

    for symbol, expected in sorted(contracts.items()):
        actual = _call_u32(library, symbol)
        if actual != expected:
            failures.append(f"{symbol}: contract version {actual} != {expected}")

    if failures:
        raise RuntimeError(
            "UniFFI integrity verification failed:\n" + "\n".join(failures)
        )

    print(
        "verified "
        f"{len(checksums)} UniFFI checksums by their declared uint16_t values "
        f"and {len(contracts)} exact contract versions"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify Stable UniFFI checksums despite the Windows/JNA widened "
            "uint16_t return carrier"
        )
    )
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--generated-dir", type=Path, required=True)
    arguments = parser.parse_args()

    if not arguments.library.is_file():
        parser.error(f"library does not exist: {arguments.library}")
    if not arguments.generated_dir.is_dir():
        parser.error(f"generated directory does not exist: {arguments.generated_dir}")

    check(arguments.library, arguments.generated_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
