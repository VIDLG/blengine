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

[`UUID`](/documentation/CoreBluetooth/CBAttribute/uuid)

The Bluetooth-specific UUID of the attribute.

## Relationships

### Conforms To

[`NSObjectProtocol`](/documentation/ObjectiveC/NSObjectProtocol)

[`Hashable`](/documentation/Swift/Hashable)

[`CustomDebugStringConvertible`](/documentation/Swift/CustomDebugStringConvertible)

[`Equatable`](/documentation/Swift/Equatable)

[`CustomStringConvertible`](/documentation/Swift/CustomStringConvertible)

[`CVarArg`](/documentation/Swift/CVarArg)

### Inherited By

[`CBService`](/documentation/CoreBluetooth/CBService)

[`CBCharacteristic`](/documentation/CoreBluetooth/CBCharacteristic)

[`CBDescriptor`](/documentation/CoreBluetooth/CBDescriptor)

### Inherits From

[`NSObject-swift.class`](/documentation/ObjectiveC/NSObject-swift.class)

---

Copyright &copy; 2026 Apple Inc. All rights reserved. | [Terms of Use](https://www.apple.com/legal/internet-services/terms/site.html) | [Privacy Policy](https://www.apple.com/privacy/privacy-policy)