"""Read-only, fresh Settings inspection evidence; never radio control or lease release."""

from __future__ import annotations

import hashlib
import json
import math
import os
import plistlib
import re
import stat
import subprocess
import time
from pathlib import Path

from tools.react_native.evidence_io import (
    canonical as _canonical,
)
from tools.react_native.evidence_io import (
    hash_stream as _hash_stream,
)
from tools.react_native.evidence_io import (
    no_links as _no_links,
)
from tools.react_native.evidence_io import (
    open_regular as _open_regular,
)
from tools.react_native.evidence_io import (
    read_small as _read_small,
)
from tools.react_native.evidence_io import (
    recovery_json as _recovery_json,
)
from tools.react_native.evidence_io import (
    require as _require,
)
from tools.react_native.evidence_io import (
    safe_name as _safe_name,
)
from tools.react_native.evidence_io import (
    signature as _signature,
)
from tools.react_native.evidence_io import (
    walk as _walk,
)
from tools.react_native.host_paths import PROJECT_ROOT
from tools.react_native.ios_device import canonical_physical_udid

TARGET = "BleHubNitroHostUITests"
METHOD = TARGET + "/testBluetoothSettingsOffAndRestore"
NOTE_PREFIX = "ios-radio-inspection-only"
TREE_PREFIX = "bluetooth-settings-accessibility-toggle-inspection-only-no-mutation"
NOTE = (
    "scope=settings-bluetooth-switch-inspection-only; switch_value=1; "
    "radio-control-gate-not-run"
)
LIMIT = 1024 * 1024
INSPECTION_MAX_AGE_SECONDS = 60 * 60
INSPECTION_MAX_DURATION_SECONDS = 60 * 60


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _inventory(root: Path) -> tuple[list[dict], list[tuple]]:
    rows, signatures = [], [(".", _signature(root.lstat()))]
    entries = _walk(root)
    for name, path, info in entries:
        signatures.append((name, _signature(info)))
        if stat.S_ISDIR(info.st_mode):
            continue
        with _open_regular(path) as stream:
            digest, size = _hash_stream(stream)
            _require(
                _signature(info)
                == _signature(os.fstat(stream.fileno()))
                == _signature(path.lstat()),
                "inspection file drift while hashing",
            )
        rows.append({"path": name, "sha256": digest, "size": size})
    _require(
        [(name, _signature(info)) for name, _, info in _walk(root)] == signatures[1:]
        and signatures[0][1] == _signature(root.lstat()),
        "inspection directory drift",
    )
    return rows, signatures


def _staging(raw: bytes) -> dict:
    value = plistlib.loads(raw)
    _require(
        type(value) is dict
        and TARGET in value
        and set(value) <= {TARGET, "__xctestrun_metadata__"},
        "inspection requires only the UI test target",
    )
    target = value[TARGET]
    _require(
        type(target) is dict and target.get("IsUITestBundle") is True,
        "inspection UI test bundle required",
    )
    for key, expected in {
        "OnlyTestIdentifiers": [METHOD],
        "SkipTestIdentifiers": [],
        "CommandLineArguments": [],
        "UITargetAppCommandLineArguments": [],
    }.items():
        _require(target.get(key) == expected, f"inspection xctestrun {key}")
    environment = target.get("EnvironmentVariables")
    expected = {
        "BLEHUB_IOS_RADIO_CONTROL_GATE": "1",
        "BLEHUB_IOS_RADIO_CONTROL_INSPECT_ONLY": "1",
    }

    def visit(item: object) -> None:
        if type(item) is dict:
            for key, child in item.items():
                _require(type(key) is str, "inspection dictionary keys must be strings")
                _require(
                    not any(
                        part in key.lower()
                        for part in ("retri", "retry", "repet", "iteration")
                    ),
                    "inspection repetition/retry option",
                )
                _require(
                    "BLEHUB" not in key or item is environment,
                    "BLEHUB setting outside inspection EnvironmentVariables",
                )
                visit(child)
        elif type(item) is list:
            for child in item:
                visit(child)

    visit(value)
    _require(
        type(environment) is dict
        and {k: v for k, v in environment.items() if "BLEHUB" in k} == expected,
        "inspection requires exact BLEHUB opt-ins",
    )
    return target


def _command(command: object, staged: Path, result: Path, device: str) -> None:
    _require(type(command) is list, "inspection command must be an argument list")
    prefix = ["pixi", "run", "--frozen", "--no-install", "-e", "apple-ci"]
    tail = [
        "xcodebuild",
        "-quiet",
        "-xctestrun",
        str(staged),
        "-destination",
        "id=" + device,
        "-resultBundlePath",
        str(result),
        "-parallel-testing-enabled",
        "NO",
        "-collect-test-diagnostics",
        "never",
        "-only-testing:" + METHOD,
        "test-without-building",
    ]
    legacy_tail = list(tail)
    legacy_tail[legacy_tail.index("-only-testing:" + METHOD)] = (
        "-only-testing:" + TARGET + "/" + METHOD
    )
    if command in (tail, prefix + tail, legacy_tail, prefix + legacy_tail):
        return
    _require(type(command) is list, "inspection command must be an argument list")
    _require(command[:3] == ["pixi", "run", "-e"], "inspection launcher drift")
    _require(command[3:5] == ["apple-ci", "xcodebuild"], "inspection tool drift")
    _require("-workspace" in command and "-scheme" in command, "inspection workspace")
    workspace = Path(command[command.index("-workspace") + 1])
    _require(
        workspace.name == "BleHubNitroHost.xcworkspace",
        "inspection workspace drift",
    )
    _require(
        command[command.index("-scheme") + 1] == "BleHubNitroHostUIOnly",
        "inspection scheme drift",
    )
    _require(
        command[command.index("-destination") + 1] == "id=" + device,
        "inspection device drift",
    )
    _require(
        command[command.index("-resultBundlePath") + 1] == str(result),
        "inspection result drift",
    )
    selected = next(
        (value for value in command if value.startswith("-only-testing:")), None
    )
    _require(
        selected
        in {"-only-testing:" + METHOD, "-only-testing:" + TARGET + "/" + METHOD}
        and command[-1] == "test",
        "inspection command/method drift",
    )


def _summary(value: dict, device: str) -> tuple[float, float]:
    configurations = value.get("devicesAndConfigurations")
    _require(
        type(configurations) is list and len(configurations) == 1,
        "inspection summary device count",
    )
    configuration = configurations[0]
    _require(
        type(configuration) is dict
        and configuration.get("device", {}).get("deviceId") == device,
        "inspection summary device",
    )
    for row in (value, configuration):
        for key, expected in {
            "passedTests": 1,
            "failedTests": 0,
            "skippedTests": 0,
            "expectedFailures": 0,
        }.items():
            _require(
                type(row.get(key)) is int and row[key] == expected,
                "inspection summary " + key,
            )
    _require(
        type(value.get("totalTestCount")) is int
        and value["totalTestCount"] == 1
        and value.get("result") == "Passed"
        and value.get("testFailures") == [],
        "inspection summary must pass exactly one test",
    )
    start, finish = value.get("startTime"), value.get("finishTime")
    _require(
        all(
            type(t) in (int, float) and math.isfinite(t) and t > 1_500_000_000
            for t in (start, finish)
        ),
        "inspection finite wall times",
    )
    _require(
        0 < finish - start <= INSPECTION_MAX_DURATION_SECONDS
        and -5 <= time.time() - finish <= INSPECTION_MAX_AGE_SECONDS,
        "inspection expired/future/implausible wall times",
    )
    return start, finish


def _tests(value: dict, device: str) -> None:
    devices = value.get("devices")
    _require(
        type(devices) is list
        and len(devices) == 1
        and devices[0].get("deviceId") == device,
        "inspection tests device",
    )
    cases = []

    def visit(nodes: object, depth: int = 0) -> None:
        _require(
            type(nodes) is list and len(nodes) <= 256 and depth <= 32,
            "unbounded inspection tests tree",
        )
        for node in nodes:
            _require(
                type(node) is dict
                and "System Failures" not in str(node.get("name", ""))
                and node.get("result", "Passed") == "Passed",
                "inspection tests failure",
            )
            if node.get("nodeType") == "Test Case":
                cases.append(node)
            visit(node.get("children", []), depth + 1)

    visit(value.get("testNodes"))
    _require(
        len(cases) == 1
        and cases[0].get("nodeIdentifier") == METHOD + "()"
        and cases[0].get("result") == "Passed",
        "inspection actual selected method required",
    )


def _switch_tree(raw: bytes) -> None:
    lines = raw.decode("utf-8").splitlines()
    parents = [
        i
        for i, line in enumerate(lines)
        if line.lstrip().startswith("Switch,") and "identifier: 'BLUETOOTH'" in line
    ]
    _require(len(parents) == 1, "inspection unique BLUETOOTH switch required")
    index = parents[0]
    parent = lines[index]
    indent = len(parent) - len(parent.lstrip())
    children = []
    for line in lines[index + 1 :]:
        level = len(line) - len(line.lstrip())
        if line.strip() and level <= indent:
            break
        if line.lstrip().startswith("Switch,"):
            _require(level == indent + 2, "inspection switch must be a direct child")
            children.append(line)
    _require(
        len(children) == 1
        and all(re.search(r", value: 1\s*$", line) for line in (parent, *children)),
        "inspection BLUETOOTH and child switch must both be ON",
    )


def collect_inspection(
    input_path: Path,
    output_directory: Path,
    *,
    device: str,
    owner_sha256: str,
    journal_sha256: str,
) -> dict:
    """Re-export original xcresult locally; write only into an existing empty directory.

    The result directory SHA256 binds its sorted file inventory (path, size, hash),
    not an inferred radio/lease verdict. Local App inventories bind bytes at
    collection, not their build or installation. SDK commands address no live device.
    """
    device = canonical_physical_udid(device)
    for digest in (owner_sha256, journal_sha256):
        _require(
            type(digest) is str and re.fullmatch(r"[0-9a-f]{64}", digest),
            "invalid original quarantine hash",
        )
    input_path, output_directory = input_path.absolute(), output_directory.absolute()
    _no_links(output_directory)
    _require(
        output_directory.is_dir() and not any(output_directory.iterdir()),
        "inspection output must be an existing empty directory",
    )
    output_identity = output_directory.stat().st_dev, output_directory.stat().st_ino
    original = _read_small(input_path, LIMIT)
    receipt = _recovery_json(original.decode("utf-8"))
    _require(
        receipt.get("owner_sha256") == owner_sha256
        and receipt.get("original_journal_sha256") == journal_sha256,
        "original quarantine hashes do not match",
    )
    _require(
        all(
            type(receipt.get(k)) is str and Path(receipt[k]).is_absolute()
            for k in ("staged", "result")
        ),
        "inspection absolute source paths required",
    )
    staged, result = Path(receipt["staged"]), Path(receipt["result"])
    _require(
        not output_directory.is_relative_to(result)
        and not result.is_relative_to(output_directory)
        and not staged.is_relative_to(output_directory)
        and not input_path.is_relative_to(output_directory),
        "inspection output overlaps sources",
    )
    staged_raw = _read_small(staged, LIMIT)
    _require(
        _digest(staged_raw) == receipt.get("staged_sha256"),
        "inspection staged hash mismatch",
    )
    target = _staging(staged_raw)
    _command(receipt.get("command"), staged, result, device)
    sources = {result: _inventory(result)}
    candidate_artifacts = {}
    for role in ("TestHostPath", "UITargetAppPath"):
        value = target.get(role)
        _require(type(value) is str, f"inspection candidate {role} required")
        path = Path(value.replace("__TESTROOT__", str(staged.parent)))
        _require(
            path.is_absolute() and path.suffix == ".app" and ".." not in path.parts,
            f"inspection candidate {role} must identify an absolute App",
        )
        _require(
            not path.is_relative_to(output_directory)
            and not output_directory.is_relative_to(path),
            "inspection output overlaps candidate",
        )
        sources[path] = _inventory(path)
        rows = sources[path][0]
        candidate_artifacts[role] = {
            "path": str(path),
            "files": rows,
            "sha256": _digest(
                json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
            ),
        }
    _require(
        all(snapshot[0] for snapshot in sources.values()), "empty inspection source"
    )
    source_files = [
        (path, raw, _signature(path.lstat()))
        for path, raw in ((input_path, original), (staged, staged_raw))
    ]

    def unchanged() -> None:
        _no_links(output_directory)
        current = output_directory.stat()
        _require(
            (current.st_dev, current.st_ino) == output_identity,
            "inspection output directory replaced",
        )
        for path, raw, signature in source_files:
            _require(
                _read_small(path, LIMIT) == raw
                and _signature(path.lstat()) == signature,
                "inspection source proof drift",
            )
        for path, snapshot in sources.items():
            _require(_inventory(path) == snapshot, "inspection source/candidate drift")

    for name, raw in (
        ("source-input.json", original),
        ("source.xctestrun", staged_raw),
    ):
        with (output_directory / name).open("xb") as stream:
            stream.write(raw)
    for operation in ("summary", "tests", "attachments"):
        unchanged()
        command = ["xcrun", "xcresulttool"]
        if operation == "attachments":
            destination = output_directory / "attachments"
            _require(
                not destination.exists() and not destination.is_symlink(),
                "inspection attachment export already exists",
            )
            command += [
                "export",
                "attachments",
                "--path",
                str(result),
                "--output-path",
                str(output_directory / "attachments"),
            ]
        else:
            command += [
                "get",
                "test-results",
                operation,
                "--path",
                str(result),
                "--compact",
            ]
        name = operation + (
            "-export.stdout.log" if operation == "attachments" else ".json"
        )
        with (
            (output_directory / name).open("xb") as output,
            (output_directory / (operation + ".log")).open("xb") as log,
        ):
            subprocess.run(
                command,
                cwd=PROJECT_ROOT,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=log,
                start_new_session=True,
                timeout=30,
                check=True,
            )
        unchanged()

    observations = _inspection_output(output_directory, device)
    unchanged()
    _require(
        _canonical(_inventory(output_directory))
        == _canonical((observations["files"], observations["output_signatures"])),
        "inspection exported proof drift",
    )
    return {
        "scope": "ios-radio-restoration-inspection-only",
        "device": device,
        "owner_sha256": owner_sha256,
        "original_journal_sha256": journal_sha256,
        "input_path": str(input_path),
        "input_sha256": _digest(original),
        "staged_path": str(staged),
        "staged_sha256": _digest(staged_raw),
        "result_path": str(result),
        "result_sha256": _digest(_canonical(sources[result][0])),
        "candidate_artifacts": candidate_artifacts,
        "output_path": str(output_directory),
        "output_identity": list(output_identity),
        "source_signatures": {
            "files": {str(path): signature for path, _, signature in source_files},
            "directories": {
                str(path): snapshot[1] for path, snapshot in sources.items()
            },
        },
        **observations,
    }


def _inspection_output(output_directory: Path, device: str) -> dict:
    before = _inventory(output_directory)
    proof = {}

    def read(name: str) -> bytes:
        raw = _read_small(output_directory / name, LIMIT)
        proof[name] = _digest(raw)
        return raw

    summary = _recovery_json(read("summary.json").decode("utf-8"))
    start, finish = _summary(summary, device)
    _tests(_recovery_json(read("tests.json").decode("utf-8")), device)
    manifest = _recovery_json(
        '{"groups":' + read("attachments/manifest.json").decode("utf-8") + "}"
    )["groups"]
    _require(
        type(manifest) is list
        and len(manifest) == 1
        and manifest[0].get("testIdentifier") == METHOD + "()",
        "inspection manifest selected method",
    )
    attachments = manifest[0].get("attachments")
    _require(
        type(attachments) is list and 0 < len(attachments) <= 256,
        "inspection bounded attachments required",
    )
    matches = {NOTE_PREFIX: [], TREE_PREFIX: []}
    exported = set()
    inventory = {row["path"] for row in before[0]}
    for item in attachments:
        _require(
            type(item) is dict
            and item.get("deviceId") == device
            and item.get("isAssociatedWithFailure") is False,
            "inspection foreign/failure attachment",
        )
        name = _safe_name(item.get("exportedFileName"))
        _require(
            len(name) <= 255
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name)
            and name.casefold() not in exported
            and "attachments/" + name in inventory,
            "inspection unsafe/duplicate/missing attachment",
        )
        exported.add(name.casefold())
        suggested = item.get("suggestedHumanReadableName")
        _require(
            type(suggested) is str and len(suggested) <= 512,
            "inspection invalid suggested name",
        )
        for prefix, paths in matches.items():
            if suggested.startswith(prefix):
                _require(
                    re.fullmatch(
                        re.escape(prefix) + r"_[0-9]+_[0-9A-Fa-f-]+\.txt", suggested
                    ),
                    "inspection proof attachment name",
                )
                paths.append("attachments/" + name)
    _require(
        all(len(paths) == 1 for paths in matches.values()),
        "inspection requires exactly one note and tree",
    )
    note, tree = matches[NOTE_PREFIX][0], matches[TREE_PREFIX][0]
    _require(
        read(note).decode("utf-8") in (NOTE, NOTE + "\n"),
        "inspection note exact scope/ON proof required",
    )
    _switch_tree(read(tree))
    _require(_inventory(output_directory) == before, "inspection exported proof drift")
    _summary(
        summary, device
    )  # Export/validation time cannot extend the freshness window.
    return {
        "summary_sha256": proof["summary.json"],
        "tests_sha256": proof["tests.json"],
        "manifest_sha256": proof["attachments/manifest.json"],
        "note_sha256": proof[note],
        "tree_sha256": proof[tree],
        "output_signatures": before[1],
        "observed_start_time": start,
        "observed_finish_time": finish,
        "inspected_switch_value": "1",
        "files": before[0],
    }


def verify_inspection(
    proof: dict,
    output_directory: Path,
    *,
    added_files: dict[str, bytes] | None = None,
) -> None:
    """Recheck one collected proof without exports, writes, fsync or lease actions.

    Only explicitly supplied top-level extra files may accompany the original
    output. This verifies observations, not build/installation identity or release.
    """
    _require(type(proof) is dict, "inspection proof must be an object")
    device = canonical_physical_udid(proof.get("device"))
    _require(
        proof.get("scope") == "ios-radio-restoration-inspection-only"
        and proof.get("device") == device,
        "foreign inspection proof",
    )
    output_directory = output_directory.absolute()
    _no_links(output_directory)
    info = output_directory.stat()
    _require(
        proof.get("output_path") == str(output_directory)
        and _canonical(proof.get("output_identity"))
        == _canonical([info.st_dev, info.st_ino]),
        "inspection output directory replaced",
    )
    added_files = {} if added_files is None else added_files
    _require(type(added_files) is dict, "inspection extra files must be a mapping")
    expected = list(proof["files"])
    names = {row["path"].casefold() for row in expected}
    for name, raw in added_files.items():
        _safe_name(name)
        _require(
            "/" not in name
            and len(name) <= 255
            and type(raw) is bytes
            and name.casefold() not in names,
            "unsafe or overlapping inspection extra file",
        )
        names.add(name.casefold())
        expected.append({"path": name, "size": len(raw), "sha256": _digest(raw)})

    _require(
        all(
            type(proof.get(key + "_path")) is str
            and Path(proof[key + "_path"]).is_absolute()
            for key in ("input", "staged", "result")
        ),
        "inspection absolute source paths required",
    )
    input_path, staged, result = (
        Path(proof[key + "_path"]) for key in ("input", "staged", "result")
    )
    original, staged_raw = (_read_small(path, LIMIT) for path in (input_path, staged))
    for key, raw, copy in (
        ("input", original, "source-input.json"),
        ("staged", staged_raw, "source.xctestrun"),
    ):
        _require(
            _digest(raw) == proof.get(key + "_sha256")
            and _read_small(output_directory / copy, LIMIT) == raw,
            "inspection source/copy bytes drift",
        )
    receipt = _recovery_json(original.decode("utf-8"))
    _require(
        receipt.get("staged") == str(staged)
        and receipt.get("result") == str(result)
        and receipt.get("staged_sha256") == proof["staged_sha256"]
        and receipt.get("owner_sha256") == proof.get("owner_sha256")
        and receipt.get("original_journal_sha256")
        == proof.get("original_journal_sha256"),
        "inspection source receipt binding drift",
    )
    target = _staging(staged_raw)
    _command(receipt.get("command"), staged, result, device)
    sources = {result: _inventory(result)}
    artifacts = {}
    for role in ("TestHostPath", "UITargetAppPath"):
        value = target.get(role)
        _require(type(value) is str, "inspection App path required")
        path = Path(value.replace("__TESTROOT__", str(staged.parent)))
        sources[path] = _inventory(path)
        rows = sources[path][0]
        artifacts[role] = {
            "path": str(path),
            "files": rows,
            "sha256": _digest(_canonical(rows)),
        }
    files = {str(path): _signature(path.lstat()) for path in (input_path, staged)}
    signatures = {
        "files": files,
        "directories": {str(path): snapshot[1] for path, snapshot in sources.items()},
    }
    _require(
        _canonical(signatures) == _canonical(proof.get("source_signatures"))
        and _canonical(artifacts) == _canonical(proof.get("candidate_artifacts"))
        and _digest(_canonical(sources[result][0])) == proof.get("result_sha256"),
        "inspection source/candidate inventory or identity drift",
    )
    observed = _inspection_output(output_directory, device)
    _require(
        _canonical(observed["files"])
        == _canonical(sorted(expected, key=lambda row: row["path"])),
        "inspection output inventory drift",
    )
    original_entries = [
        row for row in observed["output_signatures"][1:] if row[0] not in added_files
    ]
    _require(
        _canonical(original_entries) == _canonical(proof["output_signatures"][1:]),
        "inspection exported file/directory identity drift",
    )
    for key, value in observed.items():
        if key not in ("files", "output_signatures"):
            _require(
                _canonical(value) == _canonical(proof.get(key)),
                "inspection returned proof drift: " + key,
            )
    for path, raw in ((input_path, original), (staged, staged_raw)):
        _require(
            _read_small(path, LIMIT) == raw
            and _canonical(_signature(path.lstat())) == _canonical(files[str(path)]),
            "inspection source drift during verification",
        )
    for path, snapshot in sources.items():
        _require(
            _inventory(path) == snapshot,
            "inspection candidate/result drift during verification",
        )
    _require(
        _canonical(_inventory(output_directory))
        == _canonical((observed["files"], observed["output_signatures"])),
        "inspection output drift during verification",
    )
    _require(
        -5
        <= time.time() - observed["observed_finish_time"]
        <= INSPECTION_MAX_AGE_SECONDS,
        "inspection expired during verification",
    )
