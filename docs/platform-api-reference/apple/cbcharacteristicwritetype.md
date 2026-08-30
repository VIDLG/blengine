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
  "identifier" : "/documentation/CoreBluetooth/CBCharacteristicWriteType",
  "metadataVersion" : "0.1.0",
  "role" : "Enumeration",
  "symbol" : {
    "kind" : "Enumeration",
    "modules" : [
      "Core Bluetooth"
    ],
    "preciseIdentifier" : "c:@E@CBCharacteristicWriteType"
  },
  "title" : "CBCharacteristicWriteType"
}
-->

# CBCharacteristicWriteType

Values representing the possible write types to a characteristic’s value.

```
enum CBCharacteristicWriteType
```

## Overview

Characteristic write types have corresponding restrictions on the length of the data that you can write to a characteristic’s value. For the [`CBCharacteristicWriteType.withResponse`](/documentation/CoreBluetooth/CBCharacteristicWriteType/withResponse) write type’s restrictions, see the Bluetooth 4.0 specification, Volume 3, Part G, Sections 4.9.3–4. For the [`CBCharacteristicWriteType.withoutResponse`](/documentation/CoreBluetooth/CBCharacteristicWriteType/withoutResponse) write type restrictions, see the Bluetooth 4.0 specification, Volume 3, Part G, Sections 4.9.1–2.

> Tip:
> When you write with a response, you can write a characteristic value that’s longer than permitted when you write without a response.

## Topics

### Write Types

[`CBCharacteristicWriteWithResponse`](/documentation/CoreBluetooth/CBCharacteristicWriteType/withResponse)

Write a characteristic value, with a response from the peripheral to indicate whether the write was successful.

[`CBCharacteristicWriteWithoutResponse`](/documentation/CoreBluetooth/CBCharacteristicWriteType/withoutResponse)

Write a characteristic value, without any response from the peripheral to indicate whether the write was successful.

## Relationships

### Conforms To

[`RawRepresentable`](/documentation/Swift/RawRepresentable)

[`Sendable`](/documentation/Swift/Sendable)

[`Equatable`](/documentation/Swift/Equatable)

[`BitwiseCopyable`](/documentation/Swift/BitwiseCopyable)

[`SendableMetatype`](/documentation/Swift/SendableMetatype)

[`Hashable`](/documentation/Swift/Hashable)

---

Copyright &copy; 2026 Apple Inc. All rights reserved. | [Terms of Use](https://www.apple.com/legal/internet-services/terms/site.html) | [Privacy Policy](https://www.apple.com/privacy/privacy-policy)