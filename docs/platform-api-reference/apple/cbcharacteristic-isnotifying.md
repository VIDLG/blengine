<!--
{
  "availability" : [
    "iOS: 5.0.0 -",
    "iPadOS: 5.0.0 -",
    "macCatalyst: 13.1.0 -",
    "macOS: 10.7.0 -",
    "tvOS: 9.0.0 -",
    "visionOS: 1.0.0 -",
    "watchOS: 2.0.0 -"
  ],
  "documentType" : "symbol",
  "framework" : "CoreBluetooth",
  "identifier" : "/documentation/CoreBluetooth/CBCharacteristic/isNotifying",
  "metadataVersion" : "0.1.0",
  "role" : "Instance Property",
  "symbol" : {
    "kind" : "Instance Property",
    "modules" : [
      "Core Bluetooth"
    ],
    "preciseIdentifier" : "c:objc(cs)CBCharacteristic(py)isNotifying"
  },
  "title" : "isNotifying"
}
-->

# isNotifying

A Boolean value that indicates whether the characteristic is currently notifying a subscribed central of its value.

```
var isNotifying: Bool { get }
```

## Discussion

This value is <doc://com.apple.documentation/documentation/Swift/true> if you enabled notifications or indications for the characteristic by successfully calling the [`setNotifyValue(_:for:)`](/documentation/CoreBluetooth/CBPeripheral/setNotifyValue(_:for:)) method of the [`CBPeripheral`](/documentation/CoreBluetooth/CBPeripheral) class. In this case, the peripheral updates its connected central that whenever the characteristic’s value changes.

If the value of the property is <doc://com.apple.documentation/documentation/Swift/false>, notifications (or indications) aren’t enabled for the characteristic, and the peripheral doesn’t update its connected central when the characteristic’s value changes.

## Topics

### Related Documentation

[`func setNotifyValue(Bool, for: CBCharacteristic)`](/documentation/CoreBluetooth/CBPeripheral/setNotifyValue(_:for:))

Sets notifications or indications for the value of a specified characteristic.



---

Copyright &copy; 2026 Apple Inc. All rights reserved. | [Terms of Use](https://www.apple.com/legal/internet-services/terms/site.html) | [Privacy Policy](https://www.apple.com/privacy/privacy-policy)