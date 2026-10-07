require "json"

package = JSON.parse(File.read(File.join(__dir__, "..", "..", "package.json")))
production_sources = "Sources/BleHubApple/**/*.swift"

Pod::Spec.new do |spec|
  spec.name         = "BleHubApple"
  spec.version      = package["version"]
  spec.summary      = "BleHub production Apple CoreBluetooth backend"
  spec.description  = "Internal React Native integration dependency that compiles the repository-owned production Apple backend against staged Stable UniFFI bindings."
  spec.homepage     = "https://github.com/VIDLG/blehub"
  spec.authors      = "BleHub contributors"
  spec.license      = { :type => "UNLICENSED" }
  spec.source       = { :path => "." }

  spec.platforms    = { :ios => "16.0" }
  spec.module_name  = "BleHubApple"
  spec.source_files = production_sources
  spec.frameworks   = "CoreBluetooth"
  spec.dependency   "BleHubUniFfi"
  spec.pod_target_xcconfig = {
    "DEFINES_MODULE" => "YES",
    "SWIFT_INSTALL_OBJC_HEADER" => "NO",
  }
end
