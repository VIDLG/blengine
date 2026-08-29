// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "BleHubUniFfiLifecycleConsumer",
    platforms: [
        .macOS(.v13),
    ],
    dependencies: [
        .package(path: "../../generated/apple"),
    ],
    targets: [
        .executableTarget(
            name: "Consumer",
            dependencies: [
                .product(
                    name: "BleHubUniFfiLifecycle",
                    package: "apple"
                ),
            ]
        ),
    ]
)
