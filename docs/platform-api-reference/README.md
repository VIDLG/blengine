# Platform BLE API reference snapshots

This directory contains focused snapshots of official platform BLE Central API documentation used as design evidence for BleHub's common command/event contract.

These files are references, not BleHub's public API contract. The reviewed contract remains in [`.plan/PLAN.md`](../../.plan/PLAN.md), and future user-facing documentation will be derived from that plan and the implementation. Project-authored interpretation of platform differences, information loss, and compatibility policy is maintained in [`docs/research/platform-abstraction-and-compatibility.md`](../research/platform-abstraction-and-compatibility.md).

## Usage

Run all project tooling through Pixi:

```text
pixi run just docs-update
```

`docs-update` downloads a fresh snapshot, validates UTF-8 and content hashes, atomically replaces the generated platform directories, and writes `sources.lock.json` with source revisions, URLs, hashes, and retrieval time.

Downloaded content is cached in the operating system's standard user cache directory:

- Pooch manages immutable Android, Windows, and BlueZ artifacts. Android's archive is checked against its official SHA-1. Windows and BlueZ URLs include the resolved Git commit; unchanged revisions reuse cached files, while a new commit creates new cache entries. Hashes from the previous `sources.lock.json` are reused to verify existing immutable cache entries.
- Hishel provides an RFC-compliant persistent HTTP cache for Apple's live documentation. Fresh entries are reused immediately; stale entries are conditionally revalidated with metadata such as `ETag` and `Last-Modified`, so unchanged pages do not need to transfer their bodies again.

Files below the four platform subdirectories are generated. Do not edit them by hand; update them through the script. This README is project-authored and is not replaced by the updater.

## Sources

- `windows/`: selected Microsoft WinRT API Markdown from the official `MicrosoftDocs/winrt-api` repository, pinned to a commit on its `docs` branch.
- `android/`: selected Android Bluetooth Framework source files and Javadocs extracted from Google's official Android 36 SDK Sources package (`source-36_r01.zip`). Android backends are implemented in Kotlin, which calls these Java-defined Framework APIs directly; the Java source remains the authoritative reference for return values, callback timing, and error semantics.
- `apple/`: selected Apple CoreBluetooth documentation Markdown endpoints. Apple does not expose a public source commit for these pages, so the lock records them as live snapshots and preserves content hashes.
- `bluez/`: selected BlueZ D-Bus API RST documents from the official `bluez/bluez` repository, pinned to a commit on its default branch.

## Licensing and provenance

The snapshots remain governed by each upstream project's license, documentation terms, and trademarks. `sources.lock.json` records a provenance/terms note and the exact upstream URL for every downloaded file. Keeping a file here does not relicense it under BleHub's project license.
