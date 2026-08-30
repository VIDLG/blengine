<!--
{
  "availability" : [
    "iOS: 5.0.0 -",
    "iPadOS: 5.0.0 -",
    "macCatalyst: 13.0.0 -",
    "macOS: 10.10.0 -",
    "tvOS: 9.0.0 -",
    "visionOS: 1.0.0 -",
    "watchOS: 4.0.0 -"
  ],
  "documentType" : "symbol",
  "framework" : "CoreBluetooth",
  "identifier" : "/documentation/CoreBluetooth/CBCharacteristicProperties",
  "metadataVersion" : "0.1.0",
  "role" : "Structure",
  "symbol" : {
    "kind" : "Structure",
    "modules" : [
      "Core Bluetooth"
    ],
    "preciseIdentifier" : "c:@E@CBCharacteristicProperties"
  },
  "title" : "CBCharacteristicProperties"
}
-->

# CBCharacteristicProperties

Values that represent the possible properties of a characteristic.

```
struct CBCharacteristicProperties
```

## Overview

Since you can combine characteristic properties, a characteristic may have multiple property values set.

## Topics

### Creating a Characteristic Properties Instance

[`init(rawValue:)`](/documentation/CoreBluetooth/CBCharacteristicProperties/init(rawValue:))

Creates a characteristic properties instance from the given raw value.

### Characteristic Properties

[`CBCharacteristicPropertyBroadcast`](/documentation/CoreBluetooth/CBCharacteristicProperties/broadcast)

A property that indicates the characteristic can broadcast its value using a characteristic configuration descriptor.

[`CBCharacteristicPropertyRead`](/documentation/CoreBluetooth/CBCharacteristicProperties/read)

A property that indicates a peripheral can read the characteristic’s value.

[`CBCharacteristicPropertyWriteWithoutResponse`](/documentation/CoreBluetooth/CBCharacteristicProperties/writeWithoutResponse)

A property that indicates a peripheral can write the characteristic’s value, without a response to indicate that the write succeeded.

[`CBCharacteristicPropertyWrite`](/documentation/CoreBluetooth/CBCharacteristicProperties/write)

A property that indicates a peripheral can write the characteristic’s value, with a response to indicate that the write succeeded.

[`CBCharacteristicPropertyNotify`](/documentation/CoreBluetooth/CBCharacteristicProperties/notify)

A property that indicates the peripheral permits notifications of the characteristic’s value, without a response from the central to indicate receipt of the notification.

[`CBCharacteristicPropertyIndicate`](/documentation/CoreBluetooth/CBCharacteristicProperties/indicate)

A property that indicates the peripheral permits notifications of the characteristic’s value, with a response from the central to indicate receipt of the notification.

[`CBCharacteristicPropertyAuthenticatedSignedWrites`](/documentation/CoreBluetooth/CBCharacteristicProperties/authenticatedSignedWrites)

A property that indicates the perhipheral allows signed writes of the characteristic’s value, without a response to indicate the write succeeded.

[`CBCharacteristicPropertyExtendedProperties`](/documentation/CoreBluetooth/CBCharacteristicProperties/extendedProperties)

A property that indicates the characteristic defines additional properties in the extended properties descriptor.

[`CBCharacteristicPropertyNotifyEncryptionRequired`](/documentation/CoreBluetooth/CBCharacteristicProperties/notifyEncryptionRequired)

A property that indicates that only trusted devices can enable notifications of the characteristic’s value.

[`CBCharacteristicPropertyIndicateEncryptionRequired`](/documentation/CoreBluetooth/CBCharacteristicProperties/indicateEncryptionRequired)

A property that indicates only trusted devices can enable indications of the characteristic’s value.

## Relationships

### Conforms To

[`BitwiseCopyable`](/documentation/Swift/BitwiseCopyable)

[`Sendable`](/documentation/Swift/Sendable)

[`ExpressibleByArrayLiteral`](/documentation/Swift/ExpressibleByArrayLiteral)

[`OptionSet`](/documentation/Swift/OptionSet)

[`RawRepresentable`](/documentation/Swift/RawRepresentable)

[`SendableMetatype`](/documentation/Swift/SendableMetatype)

[`Equatable`](/documentation/Swift/Equatable)

[`SetAlgebra`](/documentation/Swift/SetAlgebra)

---

Copyright &copy; 2026 Apple Inc. All rights reserved. | [Terms of Use](https://www.apple.com/legal/internet-services/terms/site.html) | [Privacy Policy](https://www.apple.com/privacy/privacy-policy)