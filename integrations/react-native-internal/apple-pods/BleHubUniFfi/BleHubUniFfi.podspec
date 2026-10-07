require "json"

package = JSON.parse(File.read(File.join(__dir__, "..", "..", "package.json")))
staged_root = __dir__
production_bindings = "Sources/BleHubUniFfi/blehub.swift"
production_xcframework = "BleHubUniFfiNative.xcframework"

unless File.file?(File.join(staged_root, "Sources/BleHubUniFfi/blehub.swift")) &&
       Dir.exist?(File.join(staged_root, "BleHubUniFfiNative.xcframework"))
  raise "missing staged Apple production package; run `pixi run just ffi apple-package`"
end

Pod::Spec.new do |spec|
  spec.name         = "BleHubUniFfi"
  spec.version      = package["version"]
  spec.summary      = "Staged BleHub production Stable UniFFI Swift bindings"
  spec.description  = "Internal React Native integration dependency for the one staged BleHub production UniFFI image."
  spec.homepage     = "https://github.com/VIDLG/blehub"
  spec.authors      = "BleHub contributors"
  spec.license      = { :type => "UNLICENSED" }
  spec.source       = { :path => "." }

  spec.platforms    = { :ios => "16.0" }
  spec.module_name  = "BleHubUniFfi"

  spec.source_files = production_bindings
  spec.vendored_frameworks = production_xcframework
  spec.prefix_header_contents = '#include "blehubFFI.h"'
  spec.pod_target_xcconfig = {
    "DEFINES_MODULE" => "YES",
    "SWIFT_INSTALL_OBJC_HEADER" => "NO",
  }

  # The vendored artifact is the only Rust/UniFFI native image. Force-load the
  # selected architecture slice so the final host cannot accidentally omit it.
  spec.user_target_xcconfig = {
    "OTHER_LDFLAGS[sdk=iphonesimulator*]" => "$(inherited) -force_load \"#{File.join(staged_root, production_xcframework, 'ios-arm64_x86_64-simulator', 'libblehub_uniffi.a')}\"",
    "OTHER_LDFLAGS[sdk=iphoneos*]" => "$(inherited) -force_load \"#{File.join(staged_root, production_xcframework, 'ios-arm64', 'libblehub_uniffi.a')}\"",
  }
end
