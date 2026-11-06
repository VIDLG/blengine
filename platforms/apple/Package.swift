// swift-tools-version: 5.9
import Foundation
import PackageDescription

// The selected UniFFI package is a build input, not a checkout-relative dependency. The
// invoking recipe must provide BLEHUB_APPLE_UNIFFI_PACKAGE_PATH explicitly.
guard let uniffiPackagePath = ProcessInfo.processInfo.environment[
    "BLEHUB_APPLE_UNIFFI_PACKAGE_PATH"
] else {
    fatalError("BLEHUB_APPLE_UNIFFI_PACKAGE_PATH is required to build BleHubApple")
}

let package = Package(
    name: "BleHubApple",
    platforms: [
        .iOS(.v16),
        .macOS(.v13),
    ],
    products: [
        .library(name: "BleHubApple", targets: ["BleHubApple"]),
    ],
    dependencies: [
        .package(
            name: "BleHubUniFfiPackage",
            path: uniffiPackagePath
        ),
    ],
    targets: [
        .target(
            name: "BleHubApple",
            dependencies: [
                .product(
                    name: "BleHubUniFfi",
                    package: "BleHubUniFfiPackage"
                ),
            ]
        ),
        .testTarget(
            name: "BleHubAppleTests",
            dependencies: ["BleHubApple"]
        ),
    ]
)
