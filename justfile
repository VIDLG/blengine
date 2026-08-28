default:
    @just --list

docs-update:
    python tools/update_platform_docs.py

fmt:
    ruff check --fix tools
    ruff format tools

lint:
    ruff check tools

ffi-spike-check-class-handle-param:
    python tools/check_expected_cargo_failure.py --manifest-path spikes/boltffi-class-handle-callback-param/Cargo.toml --expected "unsupported expansion: callback method parameter shape"

ffi-spike-test-lifecycle:
    cargo test --manifest-path spikes/boltffi-lifecycle/Cargo.toml

ffi-spike-generate-kotlin:
    python tools/run_boltffi_cli.py --project spikes/boltffi-lifecycle generate kotlin --deny-skipped

llvm-mingw-install:
    python tools/llvm_mingw.py install

llvm-mingw-report:
    python tools/llvm_mingw.py report

ffi-spike-build-windows-jvm-bridge: ffi-spike-generate-kotlin
    python tools/build_windows_jvm_bridge.py

ffi-spike-test-kotlin-jvm: ffi-spike-build-windows-jvm-bridge
    spikes/boltffi-lifecycle/platforms/kotlin-jvm/gradlew.bat -p spikes/boltffi-lifecycle/platforms/kotlin-jvm test sinkExceptionProbe
