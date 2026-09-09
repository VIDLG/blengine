"""Isolate macOS codesign identity discovery from Xcode provisioning."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ARTIFACT = (
    PROJECT_ROOT
    / ".work/apple-ios-runtime/DerivedData/Build/Products/Debug-iphoneos/"
    / "BleHubAppleIOSProbe.app/BleHubAppleIOSProbe.debug.dylib"
)


def _executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise SystemExit(f"missing required executable: {name}")
    return executable


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    displayed = list(command)
    if "--sign" in displayed:
        index = displayed.index("--sign")
        if index + 1 < len(displayed) and displayed[index + 1] != "-":
            displayed[index + 1] = "<identity-redacted>"
    print("+", " ".join(displayed), flush=True)
    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="")
    return result


def _default_keychain(security: str) -> str:
    result = _run([security, "default-keychain", "-d", "user"])
    if result.returncode != 0:
        raise SystemExit("unable to determine the default login keychain")
    keychain = result.stdout.strip().strip('"')
    if not keychain:
        raise SystemExit("the default login keychain path is empty")
    return keychain


def _identity_inventory(security: str, keychain: str) -> bool:
    result = subprocess.run(
        [security, "find-identity", "-v", "-p", "codesigning", keychain],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.stderr:
        print(result.stderr, end="")
    match = re.search(r"^\s*(\d+) valid identities found$", result.stdout, re.MULTILINE)
    count = int(match.group(1)) if match else 0
    team_ids = sorted(
        set(
            re.findall(
                r'^\s*\d+\)\s+[A-F0-9]{40} "[^"\n]+ \(([A-Z0-9]{10})\)"$',
                result.stdout,
                re.MULTILINE,
            )
        )
    )
    print(f"codesign_valid_identity_count={count}")
    print(f"codesign_identity_team_ids={','.join(team_ids) or 'none'}")
    return result.returncode == 0 and count > 0


def _copy_artifact(artifact: Path, directory: Path) -> Path:
    if not artifact.is_file():
        raise SystemExit(f"missing codesign artifact: {artifact}")
    target = directory / artifact.name
    shutil.copyfile(artifact, target)
    return target


def _ad_hoc_probe(codesign: str, artifact: Path) -> bool:
    with tempfile.TemporaryDirectory(prefix="blehub-codesign-spike-") as directory:
        target = _copy_artifact(artifact, Path(directory))
        signed = _run(
            [codesign, "--force", "--sign", "-", "--timestamp=none", str(target)]
        )
        verified = _run([codesign, "--verify", "--strict", str(target)])
    passed = signed.returncode == 0 and verified.returncode == 0
    print(f"codesign_ad_hoc_probe={'passed' if passed else 'failed'}")
    return passed


def _development_probe(codesign: str, artifact: Path, identity: str) -> bool:
    with tempfile.TemporaryDirectory(prefix="blehub-codesign-spike-") as directory:
        target = _copy_artifact(artifact, Path(directory))
        signed = _run(
            [
                codesign,
                "--force",
                "--sign",
                identity,
                "--timestamp=none",
                str(target),
            ]
        )
        verified = (
            _run([codesign, "--verify", "--strict", str(target)])
            if signed.returncode == 0
            else None
        )
    passed = (
        signed.returncode == 0 and verified is not None and verified.returncode == 0
    )
    print(f"codesign_development_probe={'passed' if passed else 'failed'}")
    return passed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe codesign and keychain access without changing project artifacts."
    )
    parser.add_argument(
        "--artifact",
        type=Path,
        default=DEFAULT_ARTIFACT,
        help="Mach-O artifact to copy and sign temporarily",
    )
    parser.add_argument(
        "--identity",
        help="Apple Development identity selector for the private-key probe",
    )
    args = parser.parse_args()

    if shutil.which("security") is None or shutil.which("codesign") is None:
        raise SystemExit("this spike requires macOS security and codesign tools")
    security = _executable("security")
    codesign = _executable("codesign")
    keychain = _default_keychain(security)
    print(f"codesign_default_keychain={keychain}")
    identity_available = _identity_inventory(security, keychain)
    artifact = args.artifact.resolve()
    print(f"codesign_artifact={artifact}")
    ad_hoc_passed = _ad_hoc_probe(codesign, artifact)
    if not ad_hoc_passed:
        return 1
    if args.identity is None:
        print("codesign_development_probe=skipped")
        return 0 if identity_available else 1
    return 0 if _development_probe(codesign, artifact, args.identity) else 1


if __name__ == "__main__":
    raise SystemExit(main())
