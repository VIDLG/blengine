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
  "identifier" : "/documentation/CoreBluetooth/CBPeripheral/writeValue(_:for:type:)",
  "metadataVersion" : "0.1.0",
  "role" : "Instance Method",
  "symbol" : {
    "kind" : "Instance Method",
    "modules" : [
      "Core Bluetooth"
    ],
    "preciseIdentifier" : "c:objc(cs)CBPeripheral(im)writeValue:forCharacteristic:type:"
  },
  "title" : "writeValue(_:for:type:)"
}
-->

# writeValue(_:for:type:)

Writes the value of a characteristic.

```
func writeValue(_ data: Data, for characteristic: CBCharacteristic, type: CBCharacteristicWriteType)
```

## Parameters

`data`

The value to write.

`characteristic`

The characteristic containing the value to write.

`type`

The type of write to execute. For a list of the possible types of writes to a characteristic’s value, see [`CBCharacteristicWriteType`](/documentation/CoreBluetooth/CBCharacteristicWriteType).

## Discussion

When you call this method to write the value of a characteristic, the peripheral calls the [`peripheral(_:didWriteValueFor:error:)`](/documentation/CoreBluetooth/CBPeripheralDelegate/peripheral(_:didWriteValueFor:error:)-4f5ea) method of its delegate object only if you specified the write type as [`CBCharacteristicWriteType.withResponse`](/documentation/CoreBluetooth/CBCharacteristicWriteType/withResponse). The response you receive through the [`peripheral(_:didWriteValueFor:error:)`](/documentation/CoreBluetooth/CBPeripheralDelegate/peripheral(_:didWriteValueFor:error:)-4f5ea) delegate method indicates whether the write was successful; if the write failed, it details the cause of the failure in an error.

On the other hand, if you specify the write type as [`CBCharacteristicWriteType.withoutResponse`](/documentation/CoreBluetooth/CBCharacteristicWriteType/withoutResponse), Core Bluetooth attempts to write the value but doesn’t guarantee success. If the write doesn’t succeed in this case, you aren’t notified and you don’t receive an error indicating the cause of the failure.

Use the [`write`](/documentation/CoreBluetooth/CBCharacteristicProperties/write) and [`writeWithoutResponse`](/documentation/CoreBluetooth/CBCharacteristicProperties/writeWithoutResponse) members of the characteristic’s [`properties`](/documentation/CoreBluetooth/CBCharacteristic/properties) enumeration to determine which kinds of writes you can perform.

This method copies the data passed into the `data` parameter, and you can dispose of it after the method returns.

---

Copyright &copy; 2026 Apple Inc. All rights reserved. | [Terms of Use](https://www.apple.com/legal/internet-services/terms/site.html) | [Privacy Policy](https://www.apple.com/privacy/privacy-policy)