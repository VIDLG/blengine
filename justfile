jvm_run := "pixi run -e jvm"
android_run := "pixi run -e android"

import "just/spikes.just"
import "just/tests.just"

default:
    @just --list

docs-update:
    python tools/update_platform_docs.py


docs-check-mermaid:
    npm install --ignore-scripts --no-audit --no-fund
    npm run check:mermaid


generate-kotlin:
    python -m tools.run_boltffi_cli --project . generate kotlin --deny-skipped

generate-swift:
    python -m tools.run_boltffi_cli --project . generate swift --deny-skipped

generate-bindings: generate-kotlin generate-swift


boltffi-bootstrap:
    python -m tools.boltffi_toolchain install

boltffi-doctor:
    python -m tools.boltffi_toolchain verify

boltffi-report:
    python -m tools.boltffi_toolchain report

toolchain-paths:
    python -m tools.local_config report

sccache-stats:
    sccache --show-stats

swift-bootstrap:
    python -m tools.swift_toolchain install

swift-doctor:
    python -m tools.swift_toolchain verify

swift-report:
    python -m tools.swift_toolchain report

android-bootstrap:
    {{android_run}} python -m tools.android_toolchain install

android-doctor:
    {{android_run}} python -m tools.android_toolchain verify

android-report:
    {{android_run}} python -m tools.android_toolchain report

android-avd-bootstrap:
    {{android_run}} python -m tools.android_toolchain avd-create

android-emulator-start:
    {{android_run}} python -m tools.android_toolchain emulator-start

android-emulator-stop:
    {{android_run}} python -m tools.android_toolchain emulator-stop
