Pod::Spec.new do |spec|
  spec.name         = "BleHubUniFfi"
  spec.version      = "0.0.0"
  spec.summary      = "Staged BleHub production Stable UniFFI Swift bindings"
  spec.description  = "The staged production BleHub UniFFI image."
  spec.homepage     = "https://github.com/VIDLG/blehub"
  spec.authors      = "BleHub contributors"
  spec.license      = { :type => "UNLICENSED" }
  spec.source       = { :path => "." }

  spec.platforms    = { :ios => "16.0" }
  spec.module_name  = "BleHubUniFfi"
  spec.source_files = "Sources/BleHubUniFfi/blehub.swift"
  spec.vendored_frameworks = "BleHubUniFfiNative.xcframework"
  spec.prefix_header_contents = '#include "blehubFFI.h"'
  spec.pod_target_xcconfig = {
    "DEFINES_MODULE" => "YES",
    "SWIFT_INSTALL_OBJC_HEADER" => "NO",
  }
  spec.user_target_xcconfig = {
    "OTHER_LDFLAGS[sdk=iphonesimulator*]" => %Q[$(inherited) -force_load "#{File.join(__dir__, "BleHubUniFfiNative.xcframework", "ios-arm64_x86_64-simulator", "libblehub_uniffi.a")}"],
    "OTHER_LDFLAGS[sdk=iphoneos*]" => %Q[$(inherited) -force_load "#{File.join(__dir__, "BleHubUniFfiNative.xcframework", "ios-arm64", "libblehub_uniffi.a")}"],
  }
end
