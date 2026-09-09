"""Update focused official BLE platform API documentation snapshots."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.parse
import zipfile
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

import hishel
import httpx
import pooch
from hishel.httpx import SyncCacheClient
from tqdm import tqdm

REPO_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_ROOT = REPO_ROOT / "docs" / "platform-api-reference"
LOCK_PATH = REFERENCE_ROOT / "sources.lock.json"
CACHE_ROOT = pooch.os_cache("blehub") / "platform-api-reference"
CACHE_ROOT.mkdir(parents=True, exist_ok=True)
PLATFORMS = ("windows", "android", "apple", "bluez")
USER_AGENT = (
    "blehub-platform-docs/0.1 (+https://github.com/blehub; API contract research)"
)
TIMEOUT_SECONDS = 15
RETRIES = 2
DOWNLOAD_WORKERS = 8

WINDOWS_REPO = "MicrosoftDocs/winrt-api"
WINDOWS_REF = "docs"
ANDROID_SOURCES_REVISION = "android-36/source-36_r01"
ANDROID_SOURCES_URL = "https://dl.google.com/android/repository/source-36_r01.zip"
ANDROID_SOURCES_SHA1 = "cbd2cf0e67abf996c4ad6cf6955c3a781d75852a"
BLUEZ_REPO = "bluez/bluez"

WINDOWS_TERMS = (
    "See the MicrosoftDocs/winrt-api repository LICENSE, Microsoft Learn terms, "
    "and notices. This snapshot does not alter upstream licensing."
)
ANDROID_TERMS = (
    "Android SDK/source package terms and each extracted file's open-source "
    "copyright/license header apply. This snapshot does not alter upstream licensing."
)
APPLE_TERMS = (
    "Apple Developer documentation terms and applicable Apple terms apply. "
    "This live snapshot does not grant or alter any upstream rights."
)
BLUEZ_TERMS = (
    "See the bluez/bluez repository COPYING and per-file notices. "
    "This snapshot does not alter upstream licensing."
)


@dataclass(frozen=True)
class LockedFile:
    platform: str
    source_name: str
    source_kind: str
    revision: str
    source_url: str
    local_path: str
    sha256: str
    upstream_terms_note: str


class UpdateError(RuntimeError):
    pass


Item = TypeVar("Item")


def _headers() -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json, text/plain, */*",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


HTTP_CLIENT = httpx.Client(
    headers=_headers(),
    timeout=httpx.Timeout(TIMEOUT_SECONDS, connect=10),
    follow_redirects=True,
    limits=httpx.Limits(
        max_connections=DOWNLOAD_WORKERS, max_keepalive_connections=DOWNLOAD_WORKERS
    ),
)
APPLE_HTTP_CLIENT = SyncCacheClient(
    storage=hishel.SyncSqliteStorage(
        database_path=CACHE_ROOT / "http-cache.sqlite3",
    ),
    headers=_headers(),
    timeout=httpx.Timeout(TIMEOUT_SECONDS, connect=10),
    follow_redirects=True,
    limits=httpx.Limits(
        max_connections=DOWNLOAD_WORKERS, max_keepalive_connections=DOWNLOAD_WORKERS
    ),
)


def fetch_bytes(
    url: str,
    *,
    optional: bool = False,
    client: httpx.Client = HTTP_CLIENT,
) -> bytes | None:
    last_error: Exception | None = None

    for attempt in range(1, RETRIES + 1):
        try:
            response = client.get(url)
            if optional and response.status_code == httpx.codes.NOT_FOUND:
                tqdm.write(
                    f"warning: optional source not found: {url}", file=sys.stderr
                )
                return None
            response.raise_for_status()
            return response.content
        except httpx.HTTPStatusError as error:
            last_error = error
            if error.response.status_code not in {429, 500, 502, 503, 504}:
                break
        except httpx.RequestError as error:
            last_error = error

        if attempt < RETRIES:
            tqdm.write(
                f"retry {attempt + 1}/{RETRIES}: {url} ({last_error})", file=sys.stderr
            )
            time.sleep(attempt)

    if optional:
        tqdm.write(
            f"warning: optional source unavailable: {url}: {last_error}",
            file=sys.stderr,
        )
        return None
    raise UpdateError(f"failed to download required source {url}: {last_error}")


def download_many(
    items: Sequence[Item],
    worker: Callable[[Item], LockedFile | None],
    *,
    label: str,
    item_name: Callable[[Item], str],
) -> list[LockedFile]:
    files: list[LockedFile] = []
    workers = min(DOWNLOAD_WORKERS, len(items))
    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix=f"docs-{label.lower()}"
    ) as executor:
        futures = {executor.submit(worker, item): item for item in items}
        with tqdm(
            total=len(items), desc=label, unit="file", dynamic_ncols=True
        ) as progress:
            for future in as_completed(futures):
                item = futures[future]
                result = future.result()
                if result is not None:
                    files.append(result)
                progress.set_postfix_str(item_name(item))
                progress.update()
    return files


def fetch_json(url: str) -> Any:
    content = fetch_bytes(url)
    assert content is not None
    try:
        return json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UpdateError(f"invalid JSON from {url}: {error}") from error


def github_api(repo: str, suffix: str) -> str:
    base_url = f"https://api.github.com/repos/{repo}"
    if not suffix:
        return base_url
    quoted_suffix = urllib.parse.quote(suffix, safe="/?:=&")
    return f"{base_url}/{quoted_suffix}"


def resolve_github_revision(repo: str, ref: str | None = None) -> tuple[str, str]:
    if ref is None:
        metadata = fetch_json(github_api(repo, ""))
        ref = metadata.get("default_branch")
        if not isinstance(ref, str) or not ref:
            raise UpdateError(f"could not resolve default branch for {repo}")

    commit = fetch_json(github_api(repo, f"commits/{ref}"))
    sha = commit.get("sha")
    if not isinstance(sha, str) or len(sha) < 40:
        raise UpdateError(f"could not resolve commit for {repo}@{ref}")
    return ref, sha


def github_directory(repo: str, revision: str, directory: str) -> list[dict[str, Any]]:
    url = github_api(repo, f"contents/{directory}?ref={revision}")
    listing = fetch_json(url)
    if not isinstance(listing, list):
        raise UpdateError(f"expected directory listing from {url}")
    return [
        item
        for item in listing
        if isinstance(item, dict) and item.get("type") == "file"
    ]


def raw_github_url(repo: str, revision: str, source_path: str) -> str:
    path = urllib.parse.quote(source_path, safe="/")
    return f"https://raw.githubusercontent.com/{repo}/{revision}/{path}"


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


_previous_source_hashes: dict[str, str] | None = None


def previous_source_hashes() -> dict[str, str]:
    global _previous_source_hashes
    if _previous_source_hashes is not None:
        return _previous_source_hashes

    hashes: dict[str, str] = {}
    if LOCK_PATH.is_file():
        try:
            payload = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
            files = payload.get("files", [])
            if isinstance(files, list):
                for item in files:
                    if not isinstance(item, dict):
                        continue
                    source_url = item.get("source_url")
                    digest = item.get("sha256")
                    if (
                        isinstance(source_url, str)
                        and isinstance(digest, str)
                        and len(digest) == 64
                    ):
                        hashes[source_url] = digest
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            tqdm.write(
                f"warning: could not reuse hashes from {LOCK_PATH}: {error}",
                file=sys.stderr,
            )

    _previous_source_hashes = hashes
    return hashes


def fetch_immutable_bytes(url: str) -> bytes:
    known_sha256 = previous_source_hashes().get(url)
    known_hash = f"sha256:{known_sha256}" if known_sha256 is not None else None
    url_suffix = Path(urllib.parse.urlparse(url).path).suffix
    cache_name = f"{sha256(url.encode('utf-8'))}{url_suffix}"
    last_error: Exception | None = None

    for attempt in range(1, RETRIES + 1):
        try:
            cached_path = pooch.retrieve(
                url=url,
                known_hash=known_hash,
                fname=cache_name,
                path=CACHE_ROOT / "immutable",
                progressbar=False,
            )
            return Path(cached_path).read_bytes()
        except (OSError, ValueError) as error:
            last_error = error
            if attempt < RETRIES:
                tqdm.write(
                    f"retry {attempt + 1}/{RETRIES}: {url} ({error})",
                    file=sys.stderr,
                )
                time.sleep(attempt)

    raise UpdateError(f"failed to obtain immutable source {url}: {last_error}")


def validate_text(content: bytes, source_url: str) -> None:
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise UpdateError(
            f"downloaded text is not UTF-8: {source_url}: {error}"
        ) from error


def write_snapshot(
    staging_root: Path,
    *,
    platform: str,
    local_name: str,
    content: bytes,
    source_name: str,
    source_kind: str,
    revision: str,
    source_url: str,
    terms_note: str,
) -> LockedFile:
    validate_text(content, source_url)
    local_path = Path(platform) / local_name
    destination = staging_root / local_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    return LockedFile(
        platform=platform,
        source_name=source_name,
        source_kind=source_kind,
        revision=revision,
        source_url=source_url,
        local_path=local_path.as_posix(),
        sha256=sha256(content),
        upstream_terms_note=terms_note,
    )


def select_directory_files(
    listing: Iterable[dict[str, Any]],
    required_patterns: Iterable[str],
    optional_patterns: Iterable[str] = (),
) -> list[dict[str, Any]]:
    by_name = {
        item["name"]: item
        for item in listing
        if isinstance(item.get("name"), str) and isinstance(item.get("path"), str)
    }
    selected: dict[str, dict[str, Any]] = {}

    for pattern in required_patterns:
        matches = {
            name: item
            for name, item in by_name.items()
            if fnmatch.fnmatchcase(name, pattern)
        }
        if not matches:
            raise UpdateError(
                f"required upstream filename pattern did not match: {pattern}"
            )
        selected.update(matches)

    for pattern in optional_patterns:
        matches = {
            name: item
            for name, item in by_name.items()
            if fnmatch.fnmatchcase(name, pattern)
        }
        if not matches:
            print(
                f"warning: optional upstream filename pattern did not match: {pattern}",
                file=sys.stderr,
            )
        selected.update(matches)

    return [selected[name] for name in sorted(selected)]


def download_windows(staging_root: Path) -> list[LockedFile]:
    tqdm.write("Windows: resolving source revision...")
    _, revision = resolve_github_revision(WINDOWS_REPO, WINDOWS_REF)
    source_name = f"GitHub repository {WINDOWS_REPO}"

    selections = (
        (
            "windows.devices.bluetooth.advertisement",
            (
                "bluetoothleadvertisementwatcher.md",
                "bluetoothleadvertisementwatcher_start_*.md",
                "bluetoothleadvertisementwatcher_stop_*.md",
                "bluetoothleadvertisementwatcher_stopped.md",
                "bluetoothleadvertisementwatcher_status.md",
                "bluetoothleadvertisementwatcherstatus.md",
                "bluetoothleadvertisement.md",
                "bluetoothleadvertisement_datasections.md",
                "bluetoothleadvertisement_localname.md",
                "bluetoothleadvertisement_manufacturerdata.md",
                "bluetoothleadvertisement_serviceuuids.md",
                "bluetoothleadvertisementdatasection.md",
                "bluetoothleadvertisementdatasection_data.md",
                "bluetoothleadvertisementdatasection_datatype.md",
                "bluetoothleadvertisementdatatypes_servicedata16bituuids.md",
                "bluetoothleadvertisementdatatypes_servicedata32bituuids.md",
                "bluetoothleadvertisementdatatypes_servicedata128bituuids.md",
                "bluetoothleadvertisementreceivedeventargs.md",
                "bluetoothleadvertisementreceivedeventargs_advertisement.md",
                "bluetoothleadvertisementreceivedeventargs_isconnectable.md",
                "bluetoothleadvertisementreceivedeventargs_isscanresponse.md",
                "bluetoothleadvertisementreceivedeventargs_rawsignalstrengthindbm.md",
                "bluetoothleadvertisementreceivedeventargs_timestamp.md",
                "bluetoothlemanufacturerdata.md",
                "bluetoothlemanufacturerdata_companyid.md",
                "bluetoothlemanufacturerdata_data.md",
            ),
            (),
        ),
        (
            "windows.devices.bluetooth",
            (
                "bluetoothadapter.md",
                "bluetoothadapter_getdefaultasync_*.md",
                "bluetoothadapter_getradioasync_*.md",
                "bluetoothadapter_iscentralrolesupported.md",
                "bluetoothadapter_islowenergysupported.md",
                "bluetoothcachemode.md",
                "bluetoothledevice.md",
                "bluetoothledevice_fromidasync_*.md",
                "bluetoothledevice_getgattservicesasync_*.md",
                "bluetoothledevice_connectionstatuschanged.md",
            ),
            ("bluetoothledevice_close_*.md", "bluetoothledevice_connectionstatus.md"),
        ),
        (
            "windows.devices.radios",
            (
                "radio.md",
                "radio_requestaccessasync_*.md",
                "radio_state.md",
                "radio_statechanged.md",
                "radioaccessstatus.md",
                "radiokind.md",
                "radiostate.md",
            ),
            (),
        ),
        (
            "windows.devices.bluetooth.genericattributeprofile",
            (
                "gattcharacteristic.md",
                "gattcharacteristic_readvalueasync_*.md",
                "gattcharacteristic_writevaluewithresultasync_*.md",
                "gattcharacteristic_writeclientcharacteristicconfigurationdescriptorwithresultasync_*.md",
                "gattcharacteristic_characteristicproperties.md",
                "gattcharacteristic_getdescriptorsasync_*.md",
                "gattcharacteristic_uuid.md",
                "gattcharacteristic_valuechanged.md",
                "gattcharacteristicproperties.md",
                "gattcharacteristicsresult.md",
                "gattcharacteristicsresult_characteristics.md",
                "gattcharacteristicsresult_protocolerror.md",
                "gattcharacteristicsresult_status.md",
                "gattclientcharacteristicconfigurationdescriptorvalue.md",
                "gattdescriptor.md",
                "gattdescriptor_uuid.md",
                "gattdescriptorsresult.md",
                "gattdescriptorsresult_descriptors.md",
                "gattdescriptorsresult_protocolerror.md",
                "gattdescriptorsresult_status.md",
                "gattdeviceservice.md",
                "gattdeviceservice_getcharacteristicsasync_*.md",
                "gattdeviceservice_getincludedservicesasync_*.md",
                "gattdeviceservice_uuid.md",
                "gattdeviceservicesresult.md",
                "gattdeviceservicesresult_protocolerror.md",
                "gattdeviceservicesresult_services.md",
                "gattdeviceservicesresult_status.md",
                "gattreadresult.md",
                "gattreadresult_protocolerror.md",
                "gattreadresult_status.md",
                "gattreadresult_value.md",
                "gattvaluechangedeventargs.md",
                "gattvaluechangedeventargs_characteristicvalue.md",
                "gattvaluechangedeventargs_timestamp.md",
                "gattwriteoption.md",
                "gattwriteresult.md",
                "gattwriteresult_protocolerror.md",
                "gattwriteresult_status.md",
                "gattcommunicationstatus.md",
                "gattsession.md",
                "gattsession_close_*.md",
            ),
            (
                "gattcharacteristic_writevalueasync_*.md",
                "gattdescriptor_readvalueasync_*.md",
                "gattdescriptor_writevalueasync_*.md",
            ),
        ),
    )

    tqdm.write("Windows: resolving documentation index...")

    def load_selection(
        selection: tuple[str, tuple[str, ...], tuple[str, ...]],
    ) -> list[dict[str, Any]]:
        directory, required, optional = selection
        listing = github_directory(WINDOWS_REPO, revision, directory)
        return select_directory_files(listing, required, optional)

    selected_files: list[dict[str, Any]] = []
    with ThreadPoolExecutor(
        max_workers=len(selections), thread_name_prefix="docs-windows-index"
    ) as executor:
        for selected in executor.map(load_selection, selections):
            selected_files.extend(selected)

    def download(item: dict[str, Any]) -> LockedFile:
        source_path = item["path"]
        source_url = raw_github_url(WINDOWS_REPO, revision, source_path)
        content = fetch_immutable_bytes(source_url)
        return write_snapshot(
            staging_root,
            platform="windows",
            local_name=item["name"],
            content=content,
            source_name=source_name,
            source_kind="official GitHub documentation source",
            revision=revision,
            source_url=source_url,
            terms_note=WINDOWS_TERMS,
        )

    return download_many(
        selected_files,
        download,
        label="Windows",
        item_name=lambda item: item["name"],
    )


def download_android(staging_root: Path) -> list[LockedFile]:
    source_suffixes = (
        "android/bluetooth/BluetoothAdapter.java",
        "android/bluetooth/BluetoothManager.java",
        "android/bluetooth/BluetoothDevice.java",
        "android/bluetooth/BluetoothGatt.java",
        "android/bluetooth/BluetoothGattCallback.java",
        "android/bluetooth/BluetoothGattCharacteristic.java",
        "android/bluetooth/BluetoothGattDescriptor.java",
        "android/bluetooth/BluetoothGattService.java",
        "android/bluetooth/BluetoothProfile.java",
        "android/bluetooth/BluetoothStatusCodes.java",
        "android/bluetooth/le/BluetoothLeScanner.java",
        "android/bluetooth/le/ScanCallback.java",
        "android/bluetooth/le/ScanFilter.java",
        "android/bluetooth/le/ScanRecord.java",
        "android/bluetooth/le/ScanResult.java",
        "android/bluetooth/le/ScanSettings.java",
    )

    archive_path: Path | None = None
    last_error: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            tqdm.write("Android: checking cached Android 36 SDK Sources package...")
            archive_path = Path(
                pooch.retrieve(
                    url=ANDROID_SOURCES_URL,
                    known_hash=f"sha1:{ANDROID_SOURCES_SHA1}",
                    fname="source-36_r01.zip",
                    path=CACHE_ROOT / "android",
                    progressbar=True,
                )
            )
            break
        except (OSError, ValueError) as error:
            last_error = error
            if attempt < RETRIES:
                tqdm.write(
                    f"retry {attempt + 1}/{RETRIES}: {ANDROID_SOURCES_URL} ({error})",
                    file=sys.stderr,
                )
                time.sleep(attempt)
    else:
        raise UpdateError(
            f"failed to obtain Android SDK Sources: {last_error}"
        ) from last_error

    assert archive_path is not None
    files: list[LockedFile] = []
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = {
                info.filename.replace("\\", "/"): info
                for info in archive.infolist()
                if not info.is_dir()
            }
            with tqdm(
                total=len(source_suffixes),
                desc="Android",
                unit="file",
                dynamic_ncols=True,
            ) as progress:
                for suffix in source_suffixes:
                    matches = [
                        (member_name, info)
                        for member_name, info in members.items()
                        if member_name.endswith(suffix)
                    ]
                    if len(matches) != 1:
                        raise UpdateError(
                            "expected exactly one Android SDK Sources member ending "
                            f"with {suffix!r}, found {len(matches)}"
                        )
                    member_name, member = matches[0]
                    content = archive.read(member)
                    member_url = (
                        f"{ANDROID_SOURCES_URL}#"
                        f"{urllib.parse.quote(member_name, safe='/')}"
                    )
                    files.append(
                        write_snapshot(
                            staging_root,
                            platform="android",
                            local_name=Path(suffix).name,
                            content=content,
                            source_name="Google Android SDK Sources",
                            source_kind="official Android SDK source and Javadocs",
                            revision=ANDROID_SOURCES_REVISION,
                            source_url=member_url,
                            terms_note=ANDROID_TERMS,
                        )
                    )
                    progress.set_postfix_str(Path(suffix).name)
                    progress.update()
    except zipfile.BadZipFile as error:
        archive_path.unlink(missing_ok=True)
        raise UpdateError(f"invalid Android SDK Sources archive: {error}") from error

    return files


def apple_markdown_url(path: str) -> str:
    quoted_path = urllib.parse.quote(path, safe="/():,-")
    return f"https://developer.apple.com/documentation/corebluetooth/{quoted_path}.md"


def download_apple(staging_root: Path) -> list[LockedFile]:
    sources = (
        ("cbcentralmanager", "cbcentralmanager.md", False),
        ("cbcentralmanagerdelegate", "cbcentralmanagerdelegate.md", False),
        ("cbperipheral", "cbperipheral.md", False),
        ("cbperipheraldelegate", "cbperipheraldelegate.md", False),
        ("cbmanager", "cbmanager.md", False),
        (
            "cbcentralmanager/scanforperipherals(withservices:options:)",
            "cbcentralmanager-scanforperipherals.md",
            True,
        ),
        ("cbcentralmanager/stopscan()", "cbcentralmanager-stopscan.md", True),
        ("cbcentralmanager/connect(_:options:)", "cbcentralmanager-connect.md", True),
        (
            "cbcentralmanager/cancelperipheralconnection(_:)",
            "cbcentralmanager-cancelperipheralconnection.md",
            True,
        ),
        ("cbperipheral/discoverservices(_:)", "cbperipheral-discoverservices.md", True),
        (
            "cbperipheral/discoverincludedservices(_:for:)",
            "cbperipheral-discoverincludedservices.md",
            True,
        ),
        (
            "cbperipheral/discovercharacteristics(_:for:)",
            "cbperipheral-discovercharacteristics.md",
            True,
        ),
        (
            "cbperipheral/discoverdescriptors(for:)",
            "cbperipheral-discoverdescriptors.md",
            True,
        ),
        ("cbperipheral/readvalue(for:)-6u2kr", "cbperipheral-readvalue.md", True),
        ("cbperipheral/writevalue(_:for:type:)", "cbperipheral-writevalue.md", True),
        ("cbperipheral/setnotifyvalue(_:for:)", "cbperipheral-setnotifyvalue.md", True),
        ("cbperipheral/readrssi()", "cbperipheral-readrssi.md", True),
        (
            "cbperipheral/maximumwritevaluelength(for:)",
            "cbperipheral-maximumwritevaluelength.md",
            True,
        ),
        (
            "cbcentralmanagerdelegate/centralmanager(_:diddiscover:advertisementdata:rssi:)",
            "cbcentralmanagerdelegate-diddiscover.md",
            True,
        ),
        (
            "advertisement-data-retrieval-keys",
            "advertisement-data-retrieval-keys.md",
            True,
        ),
        ("peripheral-scanning-options", "peripheral-scanning-options.md", True),
        (
            "cbadvertisementdatalocalnamekey",
            "cbadvertisementdatalocalnamekey.md",
            True,
        ),
        (
            "cbadvertisementdataserviceuuidskey",
            "cbadvertisementdataserviceuuidskey.md",
            True,
        ),
        (
            "cbadvertisementdatamanufacturerdatakey",
            "cbadvertisementdatamanufacturerdatakey.md",
            True,
        ),
        (
            "cbadvertisementdataservicedatakey",
            "cbadvertisementdataservicedatakey.md",
            True,
        ),
        (
            "cbadvertisementdataisconnectable",
            "cbadvertisementdataisconnectable.md",
            True,
        ),
        (
            "cbcentralmanagerscanoptionallowduplicateskey",
            "cbcentralmanagerscanoptionallowduplicateskey.md",
            True,
        ),
        ("cbattribute", "cbattribute.md", True),
        ("cbattribute/uuid", "cbattribute-uuid.md", True),
        ("cbpeer", "cbpeer.md", True),
        ("cbpeer/identifier", "cbpeer-identifier.md", True),
        ("cbservice", "cbservice.md", True),
        ("cbservice/isprimary", "cbservice-isprimary.md", True),
        (
            "cbservice/includedservices",
            "cbservice-includedservices.md",
            True,
        ),
        (
            "cbservice/characteristics",
            "cbservice-characteristics.md",
            True,
        ),
        ("cbcharacteristic", "cbcharacteristic.md", True),
        (
            "cbcharacteristic/properties",
            "cbcharacteristic-properties.md",
            True,
        ),
        (
            "cbcharacteristic/descriptors",
            "cbcharacteristic-descriptors.md",
            True,
        ),
        ("cbcharacteristic/value", "cbcharacteristic-value.md", True),
        (
            "cbcharacteristic/isnotifying",
            "cbcharacteristic-isnotifying.md",
            True,
        ),
        ("cbdescriptor", "cbdescriptor.md", True),
        (
            "cbcharacteristicproperties",
            "cbcharacteristicproperties.md",
            True,
        ),
        (
            "cbcharacteristicwritetype",
            "cbcharacteristicwritetype.md",
            True,
        ),
        (
            "cbperipheraldelegate/peripheral(_:diddiscoverservices:)",
            "cbperipheraldelegate-diddiscoverservices.md",
            True,
        ),
        (
            "cbperipheraldelegate/peripheral(_:diddiscoverincludedservicesfor:error:)",
            "cbperipheraldelegate-diddiscoverincludedservices.md",
            True,
        ),
        (
            "cbperipheraldelegate/peripheral(_:diddiscovercharacteristicsfor:error:)",
            "cbperipheraldelegate-diddiscovercharacteristics.md",
            True,
        ),
        (
            "cbperipheraldelegate/peripheral(_:diddiscoverdescriptorsfor:error:)",
            "cbperipheraldelegate-diddiscoverdescriptors.md",
            True,
        ),
        (
            "cbperipheraldelegate/peripheral(_:didupdatevaluefor:error:)-1xyna",
            "cbperipheraldelegate-didupdatecharacteristicvalue.md",
            True,
        ),
        (
            "cbperipheraldelegate/peripheral(_:didwritevaluefor:error:)-4f5ea",
            "cbperipheraldelegate-didwritecharacteristicvalue.md",
            True,
        ),
        (
            "cbperipheraldelegate/peripheral(_:didupdatenotificationstatefor:error:)",
            "cbperipheraldelegate-didupdatenotificationstate.md",
            True,
        ),
        (
            "cbperipheraldelegate/peripheral(_:didmodifyservices:)",
            "cbperipheraldelegate-didmodifyservices.md",
            True,
        ),
        (
            "cbperipheraldelegate/peripheral(_:didreadrssi:error:)",
            "cbperipheraldelegate-didreadrssi.md",
            True,
        ),
    )

    def download(source: tuple[str, str, bool]) -> LockedFile | None:
        path, local_name, optional = source
        source_url = apple_markdown_url(path)
        content = fetch_bytes(
            source_url,
            optional=optional,
            client=APPLE_HTTP_CLIENT,
        )
        if content is None:
            return None
        return write_snapshot(
            staging_root,
            platform="apple",
            local_name=local_name,
            content=content,
            source_name="Apple Developer Documentation",
            source_kind="official live Markdown documentation snapshot",
            revision="live-snapshot",
            source_url=source_url,
            terms_note=APPLE_TERMS,
        )

    return download_many(
        sources,
        download,
        label="Apple",
        item_name=lambda source: source[1],
    )


def download_bluez(staging_root: Path) -> list[LockedFile]:
    tqdm.write("BlueZ: resolving source revision and documentation index...")
    branch, revision = resolve_github_revision(BLUEZ_REPO)
    listing = github_directory(BLUEZ_REPO, revision, "doc")
    required_names = (
        "org.bluez.Adapter.rst",
        "org.bluez.Device.rst",
        "org.bluez.GattService.rst",
        "org.bluez.GattCharacteristic.rst",
        "org.bluez.GattDescriptor.rst",
        "org.bluez.GattProfile.rst",
        "org.bluez.GattManager.rst",
    )
    selected = select_directory_files(listing, required_names)

    def download(item: dict[str, Any]) -> LockedFile:
        source_url = raw_github_url(BLUEZ_REPO, revision, item["path"])
        content = fetch_immutable_bytes(source_url)
        return write_snapshot(
            staging_root,
            platform="bluez",
            local_name=item["name"],
            content=content,
            source_name=f"GitHub repository {BLUEZ_REPO}",
            source_kind="official BlueZ D-Bus API documentation source",
            revision=revision,
            source_url=source_url,
            terms_note=f"{BLUEZ_TERMS} Resolved from default branch {branch!r}.",
        )

    return download_many(
        selected,
        download,
        label="BlueZ",
        item_name=lambda item: item["name"],
    )


def write_lock(staging_root: Path, files: list[LockedFile]) -> None:
    payload = {
        "schema_version": 1,
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generator": "tools/update_platform_docs.py",
        "files": [
            asdict(item) for item in sorted(files, key=lambda item: item.local_path)
        ],
    }
    (staging_root / "sources.lock.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def install_staging(staging_root: Path) -> None:
    REFERENCE_ROOT.mkdir(parents=True, exist_ok=True)
    for platform in PLATFORMS:
        source = staging_root / platform
        destination = REFERENCE_ROOT / platform
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination)

    shutil.copy2(staging_root / "sources.lock.json", LOCK_PATH)


def update() -> None:
    REFERENCE_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="blehub-platform-docs-") as temporary:
        staging_root = Path(temporary)
        for platform in PLATFORMS:
            (staging_root / platform).mkdir(parents=True)

        files: list[LockedFile] = []
        files.extend(download_windows(staging_root))
        files.extend(download_android(staging_root))
        files.extend(download_apple(staging_root))
        files.extend(download_bluez(staging_root))

        write_lock(staging_root, files)
        install_staging(staging_root)

    verify_installed_snapshot()
    print(f"updated {len(files)} files; lock: {LOCK_PATH.relative_to(REPO_ROOT)}")


def verify_installed_snapshot() -> None:
    if not LOCK_PATH.is_file():
        raise UpdateError(f"missing lock file: {LOCK_PATH.relative_to(REPO_ROOT)}")

    try:
        payload = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UpdateError(f"invalid UTF-8 or JSON in {LOCK_PATH}: {error}") from error

    if payload.get("schema_version") != 1:
        raise UpdateError(f"unsupported schema_version in {LOCK_PATH}")
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise UpdateError(f"lock contains no files: {LOCK_PATH}")

    locked_paths: set[str] = set()
    errors: list[str] = []
    for item in files:
        if not isinstance(item, dict):
            errors.append("lock contains a non-object file entry")
            continue
        local_path = item.get("local_path")
        expected_hash = item.get("sha256")
        if not isinstance(local_path, str) or not isinstance(expected_hash, str):
            errors.append(f"invalid lock entry: {item!r}")
            continue
        if local_path in locked_paths:
            errors.append(f"duplicate locked path: {local_path}")
            continue
        locked_paths.add(local_path)

        relative = Path(local_path)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            errors.append(f"unsafe locked path: {local_path}")
            continue
        if relative.parts[0] not in PLATFORMS:
            errors.append(
                f"locked path is outside managed platform directories: {local_path}"
            )
            continue

        path = REFERENCE_ROOT / relative
        if not path.is_file():
            errors.append(f"missing file: {local_path}")
            continue
        content = path.read_bytes()
        actual_hash = sha256(content)
        if actual_hash != expected_hash:
            errors.append(
                f"SHA-256 mismatch: {local_path}: expected {expected_hash}, got {actual_hash}"
            )
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as error:
            errors.append(f"not UTF-8: {local_path}: {error}")

    actual_paths = {
        path.relative_to(REFERENCE_ROOT).as_posix()
        for platform in PLATFORMS
        for path in (REFERENCE_ROOT / platform).rglob("*")
        if path.is_file()
    }
    for extra in sorted(actual_paths - locked_paths):
        errors.append(f"unlocked generated file: {extra}")

    if errors:
        raise UpdateError("documentation check failed:\n- " + "\n- ".join(errors))


def main() -> int:
    try:
        update()
    except UpdateError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    finally:
        HTTP_CLIENT.close()
        APPLE_HTTP_CLIENT.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
