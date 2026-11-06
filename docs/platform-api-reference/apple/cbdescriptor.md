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
  "identifier" : "/documentation/CoreBluetooth/CBDescriptor",
  "metadataVersion" : "0.1.0",
  "role" : "Class",
  "symbol" : {
    "kind" : "Class",
    "modules" : [
      "Core Bluetooth"
    ],
    "preciseIdentifier" : "c:objc(cs)CBDescriptor"
  },
  "title" : "CBDescriptor"
}
-->

# CBDescriptor

An object that provides further information about a remote peripheral’s characteristic.

```
class CBDescriptor
```

## Overview

[`CBDescriptor`](/documentation/CoreBluetooth/CBDescriptor) and its subclass [`CBMutableDescriptor`](/documentation/CoreBluetooth/CBMutableDescriptor) represent a descriptor of a peripheral’s characteristic. In partcular, [`CBDescriptor`](/documentation/CoreBluetooth/CBDescriptor) objects represent the descriptors of a remote peripheral’s characteristic. Descriptors provide further information about a characteristic’s value. For example, they may describe the value in human-readable form and describe how to format the value for presentation purposes. Characteristic descriptors also indicate whether a characteristic’s value indicates or notifies a client (a central) when the value of the characteristic changes.

[`CBUUID`](/documentation/CoreBluetooth/CBUUID) details six predefined descriptors and their corresponding value types. [`CBDescriptor`](/documentation/CoreBluetooth/CBDescriptor) lists the predefined descriptors and the [`CBUUID`](/documentation/CoreBluetooth/CBUUID) constants that represent them.

|Descriptor type                    |Descriptor constant                                                                                        |
|-----------------------------------|-----------------------------------------------------------------------------------------------------------|
|Characteristic extended properties |``doc://com.apple.corebluetooth/documentation/CoreBluetooth/CBUUIDCharacteristicExtendedPropertiesString`` |
|Characteristic user description    |``doc://com.apple.corebluetooth/documentation/CoreBluetooth/CBUUIDCharacteristicUserDescriptionString``    |
|Client characteristic configuration|``doc://com.apple.corebluetooth/documentation/CoreBluetooth/CBUUIDClientCharacteristicConfigurationString``|
|Server characteristic configuration|``doc://com.apple.corebluetooth/documentation/CoreBluetooth/CBUUIDServerCharacteristicConfigurationString``|
|Characteristic format              |``doc://com.apple.corebluetooth/documentation/CoreBluetooth/CBUUIDCharacteristicFormatString``             |
|Characteristic aggregate format    |``doc://com.apple.corebluetooth/documentation/CoreBluetooth/CBUUIDCharacteristicAggregateFormatString``    |

## Topics

### Identifying a Descriptor

[`var characteristic: CBCharacteristic?`](/documentation/CoreBluetooth/CBDescriptor/characteristic)

The characteristic to which this descriptor belongs.

### Accessing Descriptor Data

[`var value: Any?`](/documentation/CoreBluetooth/CBDescriptor/value)

The value of the descriptor.

## Relationships

### Conforms To

[`CustomStringConvertible`](/documentation/Swift/CustomStringConvertible)

[`Equatable`](/documentation/Swift/Equatable)

[`NSObjectProtocol`](/documentation/ObjectiveC/NSObjectProtocol)

[`CVarArg`](/documentation/Swift/CVarArg)

[`Hashable`](/documentation/Swift/Hashable)

[`CustomDebugStringConvertible`](/documentation/Swift/CustomDebugStringConvertible)

### Inherits From

[`CBAttribute`](/documentation/CoreBluetooth/CBAttribute)

### Inherited By

[`CBMutableDescriptor`](/documentation/CoreBluetooth/CBMutableDescriptor)

---

Copyright &copy; 2026 Apple Inc. All rights reserved. | [Terms of Use](https://www.apple.com/legal/internet-services/terms/site.html) | [Privacy Policy](https://www.apple.com/privacy/privacy-policy)