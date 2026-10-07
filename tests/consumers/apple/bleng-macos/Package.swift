// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "BlengArtifactConsumer",
    platforms: [.macOS(.v13)],
    dependencies: [
        .package(
            name: "BleHubUniFfi",
            path: "../../../../.work/staged/apple-bleng"
        ),
    ],
    targets: [
        .executableTarget(
            name: "BlengArtifactConsumer",
            dependencies: [
                .product(name: "BleHubUniFfi", package: "BleHubUniFfi"),
            ]
        ),
    ]
)
