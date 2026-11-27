<!--
{
  "availability" : [
    "iOS: 8.0.0 -",
    "iPadOS: 8.0.0 -",
    "macCatalyst: 13.1.0 -",
    "macOS: 10.13.0 -",
    "tvOS: 9.0.0 -",
    "visionOS: 1.0.0 -",
    "watchOS: 2.0.0 -"
  ],
  "documentType" : "symbol",
  "framework" : "CoreBluetooth",
  "identifier" : "/documentation/CoreBluetooth/CBAttribute",
  "metadataVersion" : "0.1.0",
  "role" : "Class",
  "symbol" : {
    "kind" : "Class",
    "modules" : [
      "Core Bluetooth"
    ],
    "preciseIdentifier" : "c:objc(cs)CBAttribute"
  },
  "title" : "CBAttribute"
}
-->

# CBAttribute

A representation of common aspects of services offered by a peripheral.

```
class CBAttribute
```

## Overview

Concrete subclasses of [`CBAttribute`](/documentation/CoreBluetooth/CBAttribute) (and their mutable counterparts) represent the services a peripheral offers, the characteristics of those services, and the descriptors attached to those characteristics. The concrete subclasses are:

- [`CBService`](/documentation/CoreBluetooth/CBService)
- [`CBCharacteristic`](/documentation/CoreBluetooth/CBCharacteristic)
- [`CBDescriptor`](/documentation/CoreBluetooth/CBDescriptor)

## Topics

### Identifying an Attribute

[`var uuid: CBUUID`](/documentation/CoreBluetooth/CBAttribute/uuid)

The Bluetooth-specific UUID of the attribute.

## Relationships

### Conforms To

[`CustomStringConvertible`](/documentation/Swift/CustomStringConvertible)

[`CustomDebugStringConvertible`](/documentation/Swift/CustomDebugStringConvertible)

[`CVarArg`](/documentation/Swift/CVarArg)

[`NSObjectProtocol`](/documentation/ObjectiveC/NSObjectProtocol)

[`Hashable`](/documentation/Swift/Hashable)

[`Equatable`](/documentation/Swift/Equatable)

### Inherited By

[`CBCharacteristic`](/documentation/CoreBluetooth/CBCharacteristic)

[`CBDescriptor`](/documentation/CoreBluetooth/CBDescriptor)

[`CBService`](/documentation/CoreBluetooth/CBService)

### Inherits From

[`NSObject-swift.class`](/documentation/ObjectiveC/NSObject-swift.class)

---

Copyright &copy; 2026 Apple Inc. All rights reserved. | [Terms of Use](https://www.apple.com/legal/internet-services/terms/site.html) | [Privacy Policy](https://www.apple.com/privacy/privacy-policy)