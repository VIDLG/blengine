# Historical Tauri recipes; no longer imported by the root justfile.
# RF recipes are not runnable without restoring the retired nRF Tauri-only
# coordination modes from their historical revision; do not treat them as gates.
set working-directory := "../../.."

tauri_ui := "examples/archive/tauri-blehub-internal/ui"
tauri_host_manifest := "examples/archive/tauri-blehub-internal/src-tauri/Cargo.toml"
tauri_workspace_manifest := "examples/archive/tauri-blehub-internal/Cargo.toml"
tauri_plugin_manifest := "integrations/archive/tauri-plugin-blehub/Cargo.toml"
tauri_native_e2e := "examples/archive/tauri-blehub-internal/e2e/tauri"
tauri_apple_assembly := "integrations/archive/tauri-plugin-blehub/plugins/blehub/assembly/apple-assembly"

# Install the pinned official CLI at the example root for npm lifecycle discovery.
[working-directory: "examples/archive/tauri-blehub-internal"]
tauri-cli-install:
    pixi run npm ci --no-audit --no-fund

# Install the isolated React/Astryx/StyleX dependencies for the Tauri example.
tauri-ui-install:
    pixi run npm --prefix {{tauri_ui}} ci --no-audit --no-fund

# Build the isolated React/Astryx frontend consumed by the Tauri example.
tauri-ui-build: tauri-ui-install
    pixi run npm --prefix {{tauri_ui}} run build

# Run headless component tests through Vitest Browser Mode.
tauri-ui-test: tauri-ui-install
    pixi run npm --prefix {{tauri_ui}} run test:component

# Run desktop and narrow-viewport Playwright visual/interaction tests.
tauri-ui-visual *args: tauri-ui-install
    pixi run npm --prefix {{tauri_ui}} run test:visual -- {{args}}

# No native package or hardware is needed for the IPC/lifecycle and demo tests.
tauri-rust-test:
    pixi run rtk cargo test --locked --manifest-path {{tauri_plugin_manifest}} --workspace --no-default-features
    pixi run rtk cargo test --locked --manifest-path {{tauri_workspace_manifest}} --workspace --no-default-features --features demo

# Check both independent workspaces; root Cargo does not include these members.
fmt:
    pixi run rtk cargo fmt --manifest-path {{tauri_plugin_manifest}} --all
    pixi run rtk cargo fmt --manifest-path {{tauri_workspace_manifest}} --all

lint:
    pixi run rtk cargo fmt --manifest-path {{tauri_plugin_manifest}} --all -- --check
    pixi run rtk cargo fmt --manifest-path {{tauri_workspace_manifest}} --all -- --check
    pixi run rtk cargo clippy --locked --manifest-path {{tauri_plugin_manifest}} --workspace --all-targets --no-default-features -- -D warnings
    pixi run rtk cargo clippy --locked --manifest-path {{tauri_workspace_manifest}} --workspace --all-targets --no-default-features --features demo -- -D warnings

# Compile the Tauri host against the generated frontend assets.
tauri-host-build: tauri-ui-build
    pixi run rtk cargo build --locked --features wdio --manifest-path {{tauri_host_manifest}}

# Build and test the real Swift Apple backend assembly owned by the plugin.
[macos]
tauri-macos-assembly-test: apple-prepare
    pixi run -e apple-ci swift test --quiet --package-path {{tauri_apple_assembly}}

# Run all deterministic Tauri integration checks.
[macos]
tauri: tauri-ui-test tauri-ui-visual tauri-rust-test tauri-macos-assembly-test tauri-host-build

[private]
[macos]
apple-prepare:
    pixi run -e apple-ci python -m tools.apple.package_uniffi

# Generate the ignored official Tauri iOS project. The wrapper passes the
# machine-local APPLE_DEVELOPMENT_TEAM only when a signed build is requested.
[macos]
ios-init: tauri-cli-install
    pixi run -e apple-ci python -m tools.apple.archive.tauri_ios init

# Compile and link the complete iOS app without signing or RF evidence.
# Run ios-init once beforehand. target: aarch64-sim or aarch64.
[macos]
ios-build target="aarch64-sim": tauri-cli-install tauri-ui-build apple-prepare
    pixi run -e apple-ci python -m tools.apple.archive.tauri_ios build --debug --target {{target}} --no-sign --archive-only

# Build the device archive with the team selected in blehub.local.toml.
[macos]
ios-build-signed target="aarch64": tauri-cli-install tauri-ui-build apple-prepare
    pixi run -e apple-ci python -m tools.apple.archive.tauri_ios build --debug --target {{target}} --archive-only

# Run the signed iPhone 14 Tauri-to-nRF52840 RF gate. This recipe owns
# xctestrun pairing, provider startup, caffeinate, result capture, and cleanup.
[macos]
ios-rf: tauri-cli-install tauri-ui-build apple-prepare
    pixi run -e apple-ci python -m tools.apple.archive.tauri_ios build --debug --target aarch64 --archive-only
    pixi run -e apple-ci python -m tools.apple.archive.run_tauri_ios_rf

# Build the same debug/instrumented bundle used by the embedded WebDriver gate.
[macos]
[working-directory: "examples/archive/tauri-blehub-internal"]
macos-bundle: tauri-cli-install tauri-ui-build apple-prepare
    pixi run npm run tauri -- build --debug --bundles app --features wdio -- --locked --quiet

# Validate LaunchServices ownership without initializing Bluetooth or requiring TCC.
[macos]
macos-owner-test: macos-bundle
    pixi run node --test examples/archive/tauri-blehub-internal/e2e/tauri/macos-owner.test.js

# Human-only Bluetooth authorization: click Initialize in the app, allow, then quit.
# This launches an app, not an automated test or an authorization bypass.
[macos]
macos-authorize: macos-bundle
    open -n ".work/cargo-target/debug/bundle/macos/BleHub Tauri Internal.app"

# Install the isolated WebdriverIO dependencies for the native Tauri suite.
tauri-e2e-install:
    pixi run npm --prefix {{tauri_native_e2e}} ci --no-audit --no-fund

# Run the native WebDriver suite through WebdriverIO's embedded provider.
# The embedded provider supports macOS WKWebView as well as Linux/Windows.
[macos]
tauri-native-e2e: tauri-cli-install tauri-ui-build tauri-e2e-install apple-prepare
    pixi run npm --prefix {{tauri_native_e2e}} test

[linux]
tauri-native-e2e: tauri-ui-build tauri-e2e-install
    pixi run npm --prefix {{tauri_native_e2e}} test

[windows]
tauri-native-e2e: tauri-ui-build tauri-e2e-install
    pixi run npm --prefix {{tauri_native_e2e}} test

# Run the Tauri internal low-level RF suite against the standalone nRF52840 Peripheral.
[macos]
tauri-native-rf: tauri-cli-install tauri-ui-build tauri-e2e-install apple-prepare
    BLEHUB_TAURI_RF=1 pixi run npm --prefix {{tauri_native_e2e}} test

# Run the short continuous Tauri RF regression (five minutes by default).
# This is intentionally separate from the normal development checks.
[macos]
tauri-native-rf-soak minutes="5": tauri-cli-install tauri-ui-build tauri-e2e-install apple-prepare
    BLEHUB_TAURI_RF=1 BLEHUB_TAURI_SOAK_MINUTES={{ minutes }} pixi run npm --prefix {{tauri_native_e2e}} test
