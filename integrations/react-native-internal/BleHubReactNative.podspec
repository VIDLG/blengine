require "json"

package = JSON.parse(File.read(File.join(__dir__, "package.json")))


Pod::Spec.new do |spec|
  spec.name         = "BleHubReactNative"
  spec.version      = package["version"]
  spec.summary      = package["description"]
  spec.description  = "Repository-owned private React Native Nitro integration for the low-level BleHub contract and lifecycle tests."
  spec.homepage     = "https://github.com/VIDLG/blehub"
  spec.authors      = "BleHub contributors"
  spec.license      = { :type => "UNLICENSED" }
  spec.source       = { :path => "." }
  spec.platforms    = { :ios => "16.0" }

  # The Nitro target owns only the generated bridge and the internal adapter. The staged
  # UniFFI image and the production Apple backend are separate pod targets so
  # Swift can import their real modules without creating another Rust image.
  # Keep the standalone SwiftPM manifest and native unit tests out of the pod.
  spec.source_files = ["ios/HybridBleHubProbe.swift", "ios/BoundedEventHandoff.swift"]
  spec.dependency "BleHubApple"
  spec.dependency "BleHubUniFfi"

  load "nitrogen/generated/ios/BleHubReactNative+autolinking.rb"
  add_nitrogen_files(spec)
end
