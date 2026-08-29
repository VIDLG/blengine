// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "BleHubPhase0PackageConsumer",
    platforms: [
        .iOS(.v16),
        .macOS(.v13),
    ],
    products: [
        .library(name: "ConsumerLibrary", targets: ["ConsumerLibrary"]),
        .executable(name: "Consumer", targets: ["Consumer"]),
    ],
    dependencies: [
        .package(path: "../../generated/apple")
    ],
    targets: [
        .target(
            name: "ConsumerLibrary",
            dependencies: [
                .product(
                    name: "BleHubPhase0FfiFixtureFFI",
                    package: "apple"
                )
            ]
        ),
        .executableTarget(
            name: "Consumer",
            dependencies: ["ConsumerLibrary"]
        ),
    ]
)
