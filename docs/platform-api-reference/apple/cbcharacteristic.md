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
  "identifier" : "/documentation/CoreBluetooth/CBCharacteristic",
  "metadataVersion" : "0.1.0",
  "role" : "Class",
  "symbol" : {
    "kind" : "Class",
    "modules" : [
      "Core Bluetooth"
    ],
    "preciseIdentifier" : "c:objc(cs)CBCharacteristic"
  },
  "title" : "CBCharacteristic"
}
-->

# CBCharacteristic

A characteristic of a remote peripheral’s service.

```
class CBCharacteristic
```

## Overview

[`CBCharacteristic`](/documentation/CoreBluetooth/CBCharacteristic) and its subclass [`CBMutableCharacteristic`](/documentation/CoreBluetooth/CBMutableCharacteristic) represent further information about a peripheral’s service. In particular, [`CBCharacteristic`](/documentation/CoreBluetooth/CBCharacteristic) objects represent the characteristics of a remote peripheral’s service. A characteristic contains a single value and any number of descriptors describing that value. The properties of a characteristic determine how you can use a characteristic’s value, and how you access the descriptors.

## Topics

### Identifying a Characteristic

[`service`](/documentation/CoreBluetooth/CBCharacteristic/service)

The service to which this characteristic belongs.

### Accessing Characteristic Data

[`value`](/documentation/CoreBluetooth/CBCharacteristic/value)

The value of the characteristic.

[`descriptors`](/documentation/CoreBluetooth/CBCharacteristic/descriptors)

A list of the descriptors discovered in this characteristic.

[`properties`](/documentation/CoreBluetooth/CBCharacteristic/properties)

The properties of the characteristic.

[`CBCharacteristicProperties`](/documentation/CoreBluetooth/CBCharacteristicProperties)

Values that represent the possible properties of a characteristic.

[`isNotifying`](/documentation/CoreBluetooth/CBCharacteristic/isNotifying)

A Boolean value that indicates whether the characteristic is currently notifying a subscribed central of its value.

[`isBroadcasted`](/documentation/CoreBluetooth/CBCharacteristic/isBroadcasted)

A Boolean value that indicates whether the characteristic the service broadcasts this characteristic.

## Relationships

### Inherits From

[`CBAttribute`](/documentation/CoreBluetooth/CBAttribute)

### Inherited By

[`CBMutableCharacteristic`](/documentation/CoreBluetooth/CBMutableCharacteristic)

### Conforms To

[`CustomStringConvertible`](/documentation/Swift/CustomStringConvertible)

[`CustomDebugStringConvertible`](/documentation/Swift/CustomDebugStringConvertible)

[`Hashable`](/documentation/Swift/Hashable)

[`CVarArg`](/documentation/Swift/CVarArg)

[`Equatable`](/documentation/Swift/Equatable)

[`NSObjectProtocol`](/documentation/ObjectiveC/NSObjectProtocol)

---

Copyright &copy; 2026 Apple Inc. All rights reserved. | [Terms of Use](https://www.apple.com/legal/internet-services/terms/site.html) | [Privacy Policy](https://www.apple.com/privacy/privacy-policy)