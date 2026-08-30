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
  "identifier" : "/documentation/CoreBluetooth/CBCharacteristic/descriptors",
  "metadataVersion" : "0.1.0",
  "role" : "Instance Property",
  "symbol" : {
    "kind" : "Instance Property",
    "modules" : [
      "Core Bluetooth"
    ],
    "preciseIdentifier" : "c:objc(cs)CBCharacteristic(py)descriptors"
  },
  "title" : "descriptors"
}
-->

# descriptors

A list of the descriptors discovered in this characteristic.

```
var descriptors: [CBDescriptor]? { get }
```

## Discussion

The value of this property is an array of [`CBDescriptor`](/documentation/CoreBluetooth/CBDescriptor) objects that represent a characteristic’s descriptors. Characteristic descriptors provide more information about a characteristic’s value. For example, they may describe the value in human-readable form and describe how to format the value for presentation purposes. For more information about characteristic descriptors, see [`CBDescriptor`](/documentation/CoreBluetooth/CBDescriptor).

---

Copyright &copy; 2026 Apple Inc. All rights reserved. | [Terms of Use](https://www.apple.com/legal/internet-services/terms/site.html) | [Privacy Policy](https://www.apple.com/privacy/privacy-policy)