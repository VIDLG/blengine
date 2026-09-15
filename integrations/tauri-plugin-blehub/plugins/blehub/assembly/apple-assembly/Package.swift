// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    // Tauri's official ios_path linker passes CARGO_PKG_NAME here.
    name: "tauri-plugin-blehub",
    platforms: [
        .iOS(.v16),
        .macOS(.v13),
    ],
    products: [
        .library(
            name: "tauri-plugin-blehub",
            type: .static,
            targets: ["BleHubTauriAppleAssembly"]
        ),
    ],
    dependencies: [
        .package(path: "../../../../../../platforms/apple"),
        .package(path: "../../../../../../.work/staged/apple-production"),
        .package(
            url: "https://github.com/Brendonovich/swift-rs",
            revision: "fd82965"
        ),
    ],
    targets: [
        .target(
            name: "BleHubTauriAppleAssembly",
            dependencies: [
                .product(name: "BleHubApple", package: "apple"),
                .product(name: "BleHubUniFfi", package: "apple-production"),
                .product(name: "SwiftRs", package: "swift-rs"),
            ]
        ),
        .testTarget(
            name: "BleHubTauriAppleAssemblyTests",
            dependencies: [
                "BleHubTauriAppleAssembly",
                .product(name: "BleHubApple", package: "apple"),
                .product(name: "BleHubUniFfi", package: "apple-production"),
            ]
        ),
    ]
)
