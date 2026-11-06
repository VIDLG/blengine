"""Scoped, read-only iOS observations; never a runtime or RF authorization."""

from __future__ import annotations

import argparse
import io
import json
import platform
import plistlib
import re
import shutil
import subprocess
from collections.abc import Callable
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.parsers.expat import ExpatError

from tools.apple import xcodegen_toolchain
from tools.apple.native_hil import fingerprint
from tools.apple.signing_identity import visible_development_identities
from tools.local_config import resolve_local_value
from tools.react_native import ios_sdk
from tools.react_native.ios_device import (
    canonical_physical_udid,
    require_ready_unlocked_device,
)

ROOT = Path(__file__).resolve().parents[2]
MODES = ("build-signing", "prebuilt")


def optional_string(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip() or None


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError(
            f"read-only command failed (exit {result.returncode}): {command[0]}: "
            f"{result.stderr.strip()}"
        )
    return result


def _executable(name: str) -> str:
    value = shutil.which(name)
    if value is None:
        raise RuntimeError(f"missing required executable: {name}")
    return value


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _toolchain() -> dict[str, str]:
    xcodebuild = _executable("xcodebuild")
    xcrun = _executable("xcrun")
    version = _run([xcodebuild, "-version"]).stdout.strip()
    _require(version.startswith("Xcode "), "selected toolchain is not full Xcode")
    sdk = _run([xcrun, "--sdk", "iphoneos", "--show-sdk-path"]).stdout.strip()
    swift = _run([xcrun, "--find", "swiftc"]).stdout.strip()
    _require(bool(sdk and swift), "selected Xcode is missing iPhoneOS SDK or Swift")
    _executable("codesign")
    _executable("security")
    return {"xcode": version, "iphoneos_sdk": sdk, "swiftc": swift}


def _xcodegen() -> dict[str, str]:
    # The owner's human-readable diagnostics must not contaminate JSON stdout.
    with redirect_stdout(io.StringIO()):
        executable = xcodegen_toolchain.verify()
    return {"path": str(executable)}


def _team() -> dict[str, str]:
    configured = resolve_local_value(
        "apple", "development_team", "BLEHUB_APPLE_DEVELOPMENT_TEAM", None
    )
    team = configured.value
    _require(
        isinstance(team, str) and re.fullmatch(r"[A-Z0-9]{10}", team) is not None,
        "configure a 10-character Apple development Team ID explicitly",
    )
    return {"team": str(team), "source": configured.source}


def _identities(team: str) -> dict[str, Any]:
    count, teams = visible_development_identities(
        _executable("security"), _executable("openssl")
    )
    matching = sum(value == team for value in teams.values())
    _require(
        matching > 0,
        "no visible valid Apple Development identity matched by SHA-1 to a public "
        "certificate with the configured Team OU (2.5.4.11); "
        "this does not mean no public certificate exists",
    )
    return {
        "visible_count": count,
        "configured_team": team,
        "matching_development_identities": matching,
        "team_match_source": "certificate-subject-ou",
        "detail": (
            "Valid identity/public certificate SHA-1 correlation; Team from certificate "
            "OU (2.5.4.11), not CN. Enumeration only; actual signing and private-key "
            "access remain unproved."
        ),
    }


def _info(app: Path) -> dict[str, str]:
    with (app / "Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    _require(isinstance(info, dict), "Info.plist must be a dictionary")
    bundle = info.get("CFBundleIdentifier")
    executable = info.get("CFBundleExecutable")
    _require(
        isinstance(bundle, str)
        and re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", bundle) is not None,
        "invalid explicit Info bundle identifier",
    )
    _require(
        isinstance(executable, str)
        and bool(executable)
        and executable not in {".", ".."}
        and "/" not in executable
        and "\\" not in executable,
        "invalid Info executable name",
    )
    _require(
        (app / executable).is_file() and (app / executable).stat().st_size > 0,
        "Info executable is missing or empty",
    )
    _require(
        info.get("CFBundleSupportedPlatforms") == ["iPhoneOS"]
        and info.get("CFBundlePackageType") == "APPL",
        "only physical iPhoneOS application artifacts are supported",
    )
    return {"bundle_id": bundle, "executable": executable}


def _signature(app: Path, info: dict[str, str]) -> dict[str, Any]:
    codesign = _executable("codesign")
    _run([codesign, "--verify", "--deep", "--strict", str(app)])
    result = _run([codesign, "-d", "--verbose=4", str(app)])
    metadata = result.stdout + "\n" + result.stderr
    identifiers = re.findall(r"^Identifier=(.+)$", metadata, re.MULTILINE)
    teams = re.findall(r"^TeamIdentifier=(.+)$", metadata, re.MULTILINE)
    _require(identifiers == [info["bundle_id"]], "signature/Info bundle mismatch")
    _require(
        len(teams) == 1 and re.fullmatch(r"[A-Z0-9]{10}", teams[0]) is not None,
        "missing or invalid signature TeamIdentifier",
    )
    result = _run([codesign, "-d", "--entitlements", "-", "--xml", str(app)])
    entitlements = plistlib.loads(result.stdout.encode())
    _require(
        isinstance(entitlements, dict), "signed App entitlements must be a dictionary"
    )
    return {
        "bundle_id": identifiers[0],
        "team": teams[0],
        "application_identifier": entitlements.get("application-identifier"),
        "entitlement_team": entitlements.get("com.apple.developer.team-identifier"),
        "get_task_allow": entitlements.get("get-task-allow"),
    }


def _profile(
    app: Path, info: dict[str, str], signature: dict[str, Any], device: str | None
) -> dict[str, Any]:
    profile_path = app / "embedded.mobileprovision"
    _require(profile_path.is_file(), "embedded.mobileprovision is required")
    result = _run([_executable("security"), "cms", "-D", "-i", str(profile_path)])
    profile = plistlib.loads(result.stdout.encode())
    _require(isinstance(profile, dict), "invalid provisioning profile")
    expiration = profile.get("ExpirationDate")
    _require(
        isinstance(expiration, datetime), "profile ExpirationDate is missing/invalid"
    )
    if expiration.tzinfo is None:
        expiration = expiration.replace(tzinfo=timezone.utc)
    _require(expiration > datetime.now(timezone.utc), "provisioning profile is expired")
    teams = profile.get("TeamIdentifier")
    _require(teams == [signature["team"]], "signature/profile team mismatch")
    entitlements = profile.get("Entitlements")
    _require(isinstance(entitlements, dict), "profile Entitlements are missing")
    _require(
        entitlements.get("com.apple.developer.team-identifier") == signature["team"],
        "profile entitlement team mismatch",
    )
    prefixes = profile.get("ApplicationIdentifierPrefix")
    _require(
        isinstance(prefixes, list)
        and len(prefixes) == 1
        and isinstance(prefixes[0], str)
        and re.fullmatch(r"[A-Z0-9]{10}", prefixes[0]) is not None,
        "profile must have one explicit App ID prefix",
    )
    application_identifier = f"{prefixes[0]}.{info['bundle_id']}"
    permitted_identifier = entitlements.get("application-identifier")
    explicit_match = permitted_identifier == application_identifier
    wildcard_match = (
        isinstance(permitted_identifier, str)
        and re.fullmatch(
            re.escape(prefixes[0]) + r"\.(?:[A-Za-z0-9-]+\.)*\*", permitted_identifier
        )
        is not None
        and application_identifier.startswith(permitted_identifier[:-1])
    )
    _require(
        explicit_match or wildcard_match,
        "profile application identifier does not authorize this bundle/prefix",
    )
    devices = profile.get("ProvisionedDevices")
    _require(
        entitlements.get("get-task-allow") is True
        and profile.get("ProvisionsAllDevices", False) is False
        and isinstance(devices, list)
        and bool(devices),
        "a physical development profile with explicit ProvisionedDevices is required",
    )
    _require(
        signature["application_identifier"] == application_identifier
        and signature["entitlement_team"] == signature["team"]
        and signature["get_task_allow"] is True,
        "signed App entitlements do not match its development profile",
    )
    udids = {canonical_physical_udid(value) for value in devices}
    if device is not None:
        _require(device in udids, "explicit device is absent from provisioning profile")
    return {
        "team": signature["team"],
        "application_identifier": application_identifier,
        "profile_application_identifier": permitted_identifier,
        "app_id_match": "explicit"
        if explicit_match
        else "wildcard-profile-exact-signed-app",
        "expiration": expiration.isoformat(),
        "device_membership": "verified" if device is not None else "not-checked",
        "signed_entitlements_match": True,
        "entitlements_scope": "application-identifier, team, get-task-allow; not TCC",
    }


def _device(device: str) -> dict[str, str]:
    require_ready_unlocked_device(device, cwd=ROOT, timeout=25)
    return {
        "device": device,
        "detail": "Read-only availability and lock-state queries only.",
    }


def _json_command(command: list[str]) -> dict[str, Any]:
    try:
        document = json.loads(_run(command).stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("CoreDevice returned invalid JSON") from error
    _require(isinstance(document, dict), "CoreDevice JSON result is not an object")
    result = document.get("result")
    _require(isinstance(result, dict), "CoreDevice JSON result lacks result object")
    return result


def _developer_mode(device: str) -> dict[str, str]:
    result = _json_command(ios_sdk.devicectl_device_details(device))
    properties = result.get("deviceProperties")
    if not isinstance(properties, dict):
        properties = result.get("properties", {}).get("deviceProperties")
    _require(
        isinstance(properties, dict)
        and properties.get("developerModeStatus") == "enabled",
        "iOS Developer Mode is not enabled on the device",
    )
    return {
        "status": "enabled",
        "detail": "CoreDevice reports Developer Mode enabled.",
    }


def _xctestrun_runner(xctestrun: Path) -> dict[str, str]:
    try:
        document = plistlib.loads(xctestrun.read_bytes())
    except (OSError, plistlib.InvalidFileException) as error:
        raise RuntimeError(f"invalid xctestrun: {xctestrun}") from error
    for value in document.values():
        if not isinstance(value, dict):
            continue
        runner = value.get("TestHostBundleIdentifier")
        if isinstance(runner, str) and runner:
            base = runner.removesuffix(".xctrunner")
            families = {base}
            for suffix in (".UITests", "-uitests"):
                if base.endswith(suffix):
                    families.add(base[: -len(suffix)])
            product = value.get("ProductModuleName")
            _require(
                isinstance(product, str) and product,
                "xctestrun does not declare ProductModuleName",
            )
            runner_app = xctestrun.parent / "Debug-iphoneos" / f"{product}-Runner.app"
            return {
                "runner_bundle_id": runner,
                "family": "|".join(sorted(families)),
                "runner_app": str(runner_app),
            }
    raise RuntimeError("xctestrun does not declare TestHostBundleIdentifier")


def _ui_test_runner_inventory(
    device: str, xctestrun: Path, expected_team: str | None = None
) -> dict[str, Any]:
    expected = _xctestrun_runner(xctestrun)
    runner_app = Path(expected["runner_app"])
    _require(
        runner_app.is_dir() and not runner_app.is_symlink(),
        f"xctestrun runner artifact is missing: {runner_app}",
    )
    runner_info = _info(runner_app)
    _require(
        runner_info["bundle_id"] == expected["runner_bundle_id"],
        "xctestrun runner bundle identifier mismatch",
    )
    runner_signature = _signature(runner_app, runner_info)
    if expected_team is not None:
        _require(
            runner_signature["team"] == expected_team,
            "xctestrun runner TeamIdentifier does not match the App",
        )
    runner_profile = _profile(runner_app, runner_info, runner_signature, device)
    apps = _json_command(ios_sdk.devicectl_device_apps(device)).get("apps")
    apps = _json_command(ios_sdk.devicectl_device_apps(device)).get("apps")
    _require(isinstance(apps, list), "CoreDevice app inventory is not a list")
    families = set(expected["family"].split("|"))
    installed = sorted(
        value.get("bundleIdentifier")
        for value in apps
        if isinstance(value, dict)
        and isinstance(value.get("bundleIdentifier"), str)
        and value["bundleIdentifier"].endswith(".xctrunner")
        and any(
            value["bundleIdentifier"] == family + ".xctrunner" for family in families
        )
    )
    unexpected = [value for value in installed if value != expected["runner_bundle_id"]]
    _require(
        not unexpected,
        "stale or duplicate XCTest runners are installed: " + ", ".join(unexpected),
    )
    return {
        "expected_runner_bundle_id": expected["runner_bundle_id"],
        "installed_family_runners": installed,
        "runner_artifact": {
            "path": str(runner_app),
            "bundle_id": runner_signature["bundle_id"],
            "team": runner_signature["team"],
            "signature": "verified",
            "provisioning_profile": runner_profile,
        },
        "detail": "Runner artifact signature/profile and installed family inventory are verified.",
    }


def preflight(
    *,
    mode: str = "build-signing",
    app: str | None = None,
    device: str | None = None,
    xctestrun: str | None = None,
) -> dict[str, Any]:
    """Observe only this invocation; unknown or skipped required checks block success."""
    app, device, xctestrun = (
        optional_string(app),
        optional_string(device),
        optional_string(xctestrun),
    )
    required = ["admission", "host"]
    if mode == "prebuilt":
        required.extend(
            ["artifact", "signature", "provisioning-profile", "artifact-stability"]
        )
    elif mode == "build-signing":
        required.extend(
            ["build-toolchain", "xcodegen", "configured-team", "signing-identity"]
        )
    if device is not None:
        required.append("device-readiness")
    if xctestrun is not None:
        _require(device is not None, "--xctestrun requires an explicit device")
        required.extend(["developer-mode", "ui-test-runner-inventory"])
    observations: dict[str, dict[str, Any]] = {
        name: {
            "status": "not-checked",
            "required": True,
            "detail": "Prerequisite not verified.",
        }
        for name in required
    }
    for name, detail in {
        "private-key-access": (
            "Not proved; identity visibility is not private-key usability. "
            "No trial signing or unlock."
        ),
        "corebluetooth-tcc": "Not proved; no permission or CoreBluetooth operation.",
        "ui-automation": "Not proved; no XCUITest or UI automation.",
        "rf": "Not proved; no App launch, radio operation or RF test.",
    }.items():
        observations[name] = {
            "status": "not-checked",
            "required": False,
            "detail": detail,
        }
    if device is None:
        observations["device-readiness"] = {
            "status": "not-checked",
            "required": False,
            "detail": "No explicit device; no SDK device query or auto-selection.",
        }
    report: dict[str, Any] = {
        "schema_version": 1,
        "scope": "ios-read-only-preflight",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "app": app,
        "device": device,
        "xctestrun": xctestrun,
        "status_scope": (
            "Only required read-only preflight observations in this invocation; "
            "does not prove build/install/runtime/permission/UI execution/RF readiness."
        ),
        "observations": observations,
    }

    def check(name: str, action: Callable[[], dict[str, Any]]) -> dict[str, Any] | None:
        try:
            evidence = action()
        except (
            OSError,
            RuntimeError,
            ValueError,
            TypeError,
            KeyError,
            ExpatError,
            subprocess.SubprocessError,
        ) as error:
            observations[name].update(status="blocked", detail=str(error))
            return None
        observations[name].update(
            {
                "detail": "Read-only observation verified.",
                **evidence,
                "status": "verified",
            }
        )
        return evidence

    def admission() -> dict[str, Any]:
        nonlocal device
        _require(mode in MODES, "unsupported preflight mode")
        _require(mode != "build-signing" or app is None, "--app requires prebuilt mode")
        _require(
            mode != "prebuilt" or app is not None,
            "prebuilt mode requires an existing --app",
        )
        if device is not None:
            device = canonical_physical_udid(device)
            report["device"] = device
        return {"detail": "Explicit scoped inputs admitted."}

    def host() -> dict[str, Any]:
        _require(platform.system() == "Darwin", "iOS preflight requires macOS")
        return {"detail": "macOS host; this does not prove device readiness."}

    if check("admission", admission) is not None and check("host", host) is not None:
        if mode == "build-signing":
            check("build-toolchain", _toolchain)
            check("xcodegen", _xcodegen)
            team = check("configured-team", _team)
            if team is not None:
                check("signing-identity", lambda: _identities(team["team"]))
            if device is not None:
                check("device-readiness", lambda: _device(device))
                if xctestrun is not None:
                    check("developer-mode", lambda: _developer_mode(device))
                    check(
                        "ui-test-runner-inventory",
                        lambda: _ui_test_runner_inventory(
                            device,
                            Path(xctestrun),
                            team["team"] if team is not None else None,
                        ),
                    )
        else:
            assert app is not None  # Established by admission.
            path = Path(app)

            def artifact() -> dict[str, Any]:
                _require(path.suffix == ".app", "an explicit existing .app is required")
                sha256 = fingerprint(path)
                return {"sha256": sha256, **_info(path)}

            before = check("artifact", artifact)
            if before is not None:
                try:
                    signature = check("signature", lambda: _signature(path, before))
                    if signature is not None:
                        profile = check(
                            "provisioning-profile",
                            lambda: _profile(path, before, signature, device),
                        )
                        if profile is not None and device is not None:
                            check("device-readiness", lambda: _device(device))
                            if xctestrun is not None:
                                check("developer-mode", lambda: _developer_mode(device))
                                check(
                                    "ui-test-runner-inventory",
                                    lambda: _ui_test_runner_inventory(
                                        device, Path(xctestrun), signature["team"]
                                    ),
                                )
                finally:

                    def stability() -> dict[str, Any]:
                        after = fingerprint(path)
                        _require(
                            after == before["sha256"],
                            "App fingerprint changed during preflight",
                        )
                        return {"sha256": after}

                    check("artifact-stability", stability)
    report["status"] = (
        "verified"
        if all(observations[name]["status"] == "verified" for name in required)
        else "blocked"
    )
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    return report


def run(
    *,
    mode: str = "build-signing",
    app: str | None = None,
    device: str | None = None,
    xctestrun: str | None = None,
) -> int:
    report = preflight(mode=mode, app=app, device=device, xctestrun=xctestrun)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "verified" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=MODES, required=True)
    parser.add_argument(
        "--app", help="Existing physical development .app (prebuilt only)"
    )
    parser.add_argument(
        "--device", help="Explicit physical UDID; omitted/empty means no device query"
    )
    parser.add_argument(
        "--xctestrun",
        help="Existing xctestrun for read-only Developer Mode and stale-runner checks",
    )
    args = parser.parse_args(argv)
    return run(
        mode=args.mode,
        app=args.app,
        device=args.device,
        xctestrun=args.xctestrun,
    )


if __name__ == "__main__":
    raise SystemExit(main())
