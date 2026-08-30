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
  "identifier" : "/documentation/CoreBluetooth/CBPeripheralDelegate/peripheral(_:didUpdateValueFor:error:)-1xyna",
  "metadataVersion" : "0.1.0",
  "role" : "Instance Method",
  "symbol" : {
    "kind" : "Instance Method",
    "modules" : [
      "Core Bluetooth"
    ],
    "preciseIdentifier" : "c:objc(pl)CBPeripheralDelegate(im)peripheral:didUpdateValueForCharacteristic:error:"
  },
  "title" : "peripheral(_:didUpdateValueFor:error:)"
}
-->

# peripheral(_:didUpdateValueFor:error:)

Tells the delegate that retrieving the specified characteristic’s value succeeded, or that the characteristic’s value changed.

```
optional func peripheral(_ peripheral: CBPeripheral, didUpdateValueFor characteristic: CBCharacteristic, error: (any Error)?)
```

## Parameters

`peripheral`

The peripheral providing this information.

`characteristic`

The characteristic containing the value.

`error`

The reason the call failed, or `nil` if no error occurred.

## Discussion

Core Bluetooth invokes this method when your app calls the [`readValue(for:)`](/documentation/CoreBluetooth/CBPeripheral/readValue(for:)-6u2kr) method. A peripheral also invokes this method to notify your app of a change to the value of the characteristic for which the app previously enabled notifications by calling [`setNotifyValue(_:for:)`](/documentation/CoreBluetooth/CBPeripheral/setNotifyValue(_:for:)). If successful, the `error` parameter is `nil`. If unsuccessful, the `error` parameter returns the cause of the failure.

---

Copyright &copy; 2026 Apple Inc. All rights reserved. | [Terms of Use](https://www.apple.com/legal/internet-services/terms/site.html) | [Privacy Policy](https://www.apple.com/privacy/privacy-policy)