require "json"

package = JSON.parse(File.read(File.join(__dir__, "package.json")))

Pod::Spec.new do |spec|
  spec.name         = "BleHubReactNativeBleng"
  spec.version      = package["version"]
  spec.summary      = package["description"]
  spec.description  = "Private React Native Nitro integration for the formal BLeng package line."
  spec.homepage     = "https://github.com/VIDLG/blehub"
  spec.authors      = "BleHub contributors"
  spec.license      = { :type => "UNLICENSED" }
  spec.source       = { :path => "." }
  spec.platforms    = { :ios => "16.0" }

  # The formal BLeng Apple package supplies the combined bleng + blehub UniFFI image;
  # BleHubApple supplies the production CoreBluetooth backend. Neither dependency is
  # sourced from tests or from the BleHub-only React Native package line.
  # The old AppleBlengSessionSupervisor.swift remains as migration history, but is not part of
  # the active Nitro target. Rust Supervisor ownership is provided by AppleRustSupervisorOwner.
  spec.source_files = [
    "ios/ApplePlatformSession.swift",
    "ios/RecoveryEventProjection.swift",
    "ios/RecoveryEventInbox.swift",
    "ios/AppleRustSupervisorOwner.swift",
    "ios/HybridBlengCentral.swift",
    "ios/BlengEngineLifecycle.h",
    "ios/BlengEngineLifecycle.mm",
    "ios/BlengScanMapping.swift",
    "ios/HybridBlengScan.swift",
    "ios/BlengAdapterMapping.swift",
    "ios/HybridBlengAdapterControl.swift",
    "ios/HybridBlengAdapterObservation.swift",
    "ios/HybridBlengCompanionPresence.swift",
    "ios/HybridBlengDeviceQuery.swift",
    "ios/BlengFailureMapping.swift",
    "ios/HybridBlengConnection.swift",
    "ios/HybridBlengGattCacheRefresh.swift",
    "ios/HybridBlengGatt.swift",
    "ios/HybridBlengSubscription.swift",
  ]
  spec.dependency "BleHubApple"
  spec.dependency "BleHubUniFfi"

  # Explicit hook dependencies also support standalone `pod ipc spec` validation.
  spec.dependency "React-Core"
  spec.dependency "React-jsi"
  spec.dependency "ReactCommon/turbomodule/core"

  load "nitrogen/generated/ios/BleHubReactNativeBleng+autolinking.rb"
  add_nitrogen_files(spec)
end
