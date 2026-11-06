Pod::Spec.new do |spec|
  spec.name         = "BleHubApple"
  spec.version      = "0.0.0"
  spec.summary      = "BleHub production Apple CoreBluetooth backend"
  spec.description  = "The repository-owned production Apple backend package."
  spec.homepage     = "https://github.com/VIDLG/blehub"
  spec.authors      = "BleHub contributors"
  spec.license      = { :type => "UNLICENSED" }
  spec.source       = { :path => "." }

  spec.platforms    = { :ios => "16.0" }
  spec.module_name  = "BleHubApple"
  spec.source_files = "Sources/BleHubApple/**/*.swift"
  spec.frameworks   = "CoreBluetooth"
  spec.dependency   "BleHubUniFfi"
  spec.pod_target_xcconfig = {
    "DEFINES_MODULE" => "YES",
    "SWIFT_INSTALL_OBJC_HEADER" => "NO",
  }
end
