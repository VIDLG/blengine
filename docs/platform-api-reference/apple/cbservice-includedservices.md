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
  "identifier" : "/documentation/CoreBluetooth/CBService/includedServices",
  "metadataVersion" : "0.1.0",
  "role" : "Instance Property",
  "symbol" : {
    "kind" : "Instance Property",
    "modules" : [
      "Core Bluetooth"
    ],
    "preciseIdentifier" : "c:objc(cs)CBService(py)includedServices"
  },
  "title" : "includedServices"
}
-->

# includedServices

A list of included services discovered in this service.

```
var includedServices: [CBService]? { get }
```

## Discussion

This array contains [`CBService`](/documentation/CoreBluetooth/CBService) objects that represent the included services of a service. A service of a peripheral may contain a reference to other services that are available on the peripheral. These other services are the included services of the service. You discover included services using the [`discoverIncludedServices(_:for:)`](/documentation/CoreBluetooth/CBPeripheral/discoverIncludedServices(_:for:)) method of the [`CBPeripheral`](/documentation/CoreBluetooth/CBPeripheral) class.

---

Copyright &copy; 2026 Apple Inc. All rights reserved. | [Terms of Use](https://www.apple.com/legal/internet-services/terms/site.html) | [Privacy Policy](https://www.apple.com/privacy/privacy-policy)