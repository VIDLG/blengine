// swift-tools-version: 5.9
import PackageDescription

// The RN host is not required: this target compiles only the production handoff.
let package = Package(
  name: "BleHubNitroHandoffTests",
  platforms: [.macOS(.v13)],
  targets: [
    .target(
      name: "BleHubNitroHandoff",
      path: ".",
      exclude: ["Tests", "HybridBleHubProbe.swift"],
      sources: ["BoundedEventHandoff.swift"]
    ),
    .testTarget(
      name: "BoundedEventHandoffTests",
      dependencies: ["BleHubNitroHandoff"],
      path: "Tests/BoundedEventHandoffTests"
    ),
  ]
)
