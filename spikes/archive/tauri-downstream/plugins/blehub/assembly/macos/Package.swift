// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "BleHubTauriMacOSAssembly",
    platforms: [.macOS(.v13)],
    products: [
        .library(
            name: "BleHubTauriMacOSAssembly",
            type: .static,
            targets: ["BleHubTauriMacOSAssembly"]
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
            name: "BleHubTauriMacOSAssembly",
            dependencies: [
                .product(name: "BleHubApple", package: "apple"),
                .product(name: "BleHubUniFfi", package: "apple-production"),
                .product(name: "SwiftRs", package: "swift-rs"),
            ]
        ),
        .testTarget(
            name: "BleHubTauriMacOSAssemblyTests",
            dependencies: [
                "BleHubTauriMacOSAssembly",
                .product(name: "BleHubUniFfi", package: "apple-production"),
            ]
        ),
    ]
)
