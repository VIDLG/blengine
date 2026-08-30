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
  "identifier" : "/documentation/CoreBluetooth/CBPeripheralDelegate/peripheral(_:didWriteValueFor:error:)-4f5ea",
  "metadataVersion" : "0.1.0",
  "role" : "Instance Method",
  "symbol" : {
    "kind" : "Instance Method",
    "modules" : [
      "Core Bluetooth"
    ],
    "preciseIdentifier" : "c:objc(pl)CBPeripheralDelegate(im)peripheral:didWriteValueForCharacteristic:error:"
  },
  "title" : "peripheral(_:didWriteValueFor:error:)"
}
-->

# peripheral(_:didWriteValueFor:error:)

Tells the delegate that the peripheral successfully set a value for the characteristic.

```
optional func peripheral(_ peripheral: CBPeripheral, didWriteValueFor characteristic: CBCharacteristic, error: (any Error)?)
```

## Parameters

`peripheral`

The peripheral providing this information.

`characteristic`

The characteristic containing the value.

`error`

The reason the call failed, or `nil` if no error occurred.

## Discussion

Core Bluetooth invokes this method only when your app calls the [`writeValue(_:for:type:)`](/documentation/CoreBluetooth/CBPeripheral/writeValue(_:for:type:)) method with the [`CBCharacteristicWriteType.withResponse`](/documentation/CoreBluetooth/CBCharacteristicWriteType/withResponse) constant specified as the write type. If successful, the `error` parameter is `nil`. If unsuccessful, the `error` parameter returns the cause of the failure.

---

Copyright &copy; 2026 Apple Inc. All rights reserved. | [Terms of Use](https://www.apple.com/legal/internet-services/terms/site.html) | [Privacy Policy](https://www.apple.com/privacy/privacy-policy)