// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "BleHubAppleArtifactConsumer",
    platforms: [.macOS(.v13)],
    dependencies: [
        .package(
            name: "BleHubUniFfi",
            path: "../../../../../.work/staged/apple-production"
        ),
        .package(
            name: "BleHubApple",
            path: "../../../../../.work/staged/apple-backend-production"
        ),
    ],
    targets: [
        .executableTarget(
            name: "BleHubAppleArtifactConsumer",
            dependencies: [
                .product(name: "BleHubUniFfi", package: "BleHubUniFfi"),
                .product(name: "BleHubApple", package: "BleHubApple"),
            ]
        ),
    ]
)
