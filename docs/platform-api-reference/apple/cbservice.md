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
  "identifier" : "/documentation/CoreBluetooth/CBService",
  "metadataVersion" : "0.1.0",
  "role" : "Class",
  "symbol" : {
    "kind" : "Class",
    "modules" : [
      "Core Bluetooth"
    ],
    "preciseIdentifier" : "c:objc(cs)CBService"
  },
  "title" : "CBService"
}
-->

# CBService

A collection of data and associated behaviors that accomplish a function or feature of a device.

```
class CBService
```

## Overview

[`CBService`](/documentation/CoreBluetooth/CBService) objects represent services of a remote peripheral. Services are either primary or secondary and may contain multiple characteristics or included services (references to other services).

## Topics

### Identifying a Service

[`var peripheral: CBPeripheral?`](/documentation/CoreBluetooth/CBService/peripheral)

The peripheral to which this service belongs.

[`var isPrimary: Bool`](/documentation/CoreBluetooth/CBService/isPrimary)

A Boolean value that indicates whether the type of service is primary or secondary.

### Accessing Service Data

[`var characteristics: [CBCharacteristic]?`](/documentation/CoreBluetooth/CBService/characteristics)

A list of characteristics discovered in this service.

[`var includedServices: [CBService]?`](/documentation/CoreBluetooth/CBService/includedServices)

A list of included services discovered in this service.

## Relationships

### Conforms To

[`NSObjectProtocol`](/documentation/ObjectiveC/NSObjectProtocol)

[`Equatable`](/documentation/Swift/Equatable)

[`CustomStringConvertible`](/documentation/Swift/CustomStringConvertible)

[`Hashable`](/documentation/Swift/Hashable)

[`CustomDebugStringConvertible`](/documentation/Swift/CustomDebugStringConvertible)

[`CVarArg`](/documentation/Swift/CVarArg)

### Inherited By

[`CBMutableService`](/documentation/CoreBluetooth/CBMutableService)

### Inherits From

[`CBAttribute`](/documentation/CoreBluetooth/CBAttribute)

---

Copyright &copy; 2026 Apple Inc. All rights reserved. | [Terms of Use](https://www.apple.com/legal/internet-services/terms/site.html) | [Privacy Policy](https://www.apple.com/privacy/privacy-policy)