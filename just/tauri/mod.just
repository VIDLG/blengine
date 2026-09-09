set working-directory := "../.."

tauri_ui := "examples/tauri-central/ui"
tauri_host_manifest := "examples/tauri-central/src-tauri/Cargo.toml"
tauri_workspace_manifest := "examples/tauri-central/Cargo.toml"
tauri_plugin_manifest := "integrations/tauri-plugin-blehub/Cargo.toml"
tauri_native_e2e := "examples/tauri-central/e2e/tauri"
tauri_macos_assembly := "integrations/tauri-plugin-blehub/plugins/blehub/assembly/macos"

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
tauri-macos-assembly-test:
    pixi run -e apple-ci swift test --package-path {{tauri_macos_assembly}}

# Run all deterministic Tauri integration checks.
[macos]
tauri: tauri-ui-test tauri-ui-visual tauri-rust-test tauri-macos-assembly-test tauri-host-build

# Install the isolated WebdriverIO dependencies for the native Tauri suite.
tauri-e2e-install:
    pixi run npm --prefix {{tauri_native_e2e}} ci --no-audit --no-fund

# Run the native WebDriver suite through WebdriverIO's embedded provider.
# The embedded provider supports macOS WKWebView as well as Linux/Windows.
[macos]
tauri-native-e2e: tauri-ui-build tauri-e2e-install
    pixi run npm --prefix {{tauri_native_e2e}} test

[linux]
tauri-native-e2e: tauri-ui-build tauri-e2e-install
    pixi run npm --prefix {{tauri_native_e2e}} test

[windows]
tauri-native-e2e: tauri-ui-build tauri-e2e-install
    pixi run npm --prefix {{tauri_native_e2e}} test

# Run the Tauri RF suite against the managed Android Peripheral.
[macos]
tauri-native-rf: tauri-ui-build tauri-e2e-install
    BLEHUB_TAURI_RF=1 pixi run npm --prefix {{tauri_native_e2e}} test

# Run the release-candidate continuous Tauri RF soak.
# This is intentionally separate from the normal development checks.
[macos]
tauri-native-rf-soak minutes="30": tauri-ui-build tauri-e2e-install
    BLEHUB_TAURI_RF=1 BLEHUB_TAURI_SOAK_MINUTES={{ minutes }} pixi run npm --prefix {{tauri_native_e2e}} test
