// swift-tools-version: 5.9
import PackageDescription

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
            path: "../../.work/staged/apple-production"
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
