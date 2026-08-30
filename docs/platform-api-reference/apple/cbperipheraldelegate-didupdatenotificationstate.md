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
  "identifier" : "/documentation/CoreBluetooth/CBPeripheralDelegate/peripheral(_:didUpdateNotificationStateFor:error:)",
  "metadataVersion" : "0.1.0",
  "role" : "Instance Method",
  "symbol" : {
    "kind" : "Instance Method",
    "modules" : [
      "Core Bluetooth"
    ],
    "preciseIdentifier" : "c:objc(pl)CBPeripheralDelegate(im)peripheral:didUpdateNotificationStateForCharacteristic:error:"
  },
  "title" : "peripheral(_:didUpdateNotificationStateFor:error:)"
}
-->

# peripheral(_:didUpdateNotificationStateFor:error:)

Tells the delegate that the peripheral received a request to start or stop providing notifications for a specified characteristic’s value.

```
optional func peripheral(_ peripheral: CBPeripheral, didUpdateNotificationStateFor characteristic: CBCharacteristic, error: (any Error)?)
```

## Parameters

`peripheral`

The peripheral providing this information.

`characteristic`

The characteristic for which to configure value notifications.

`error`

The reason the call failed, or `nil` if no error occurred.

## Discussion

Core Bluetooth invokes this method when your app calls the [`setNotifyValue(_:for:)`](/documentation/CoreBluetooth/CBPeripheral/setNotifyValue(_:for:)) method. If successful, the `error` parameter is `nil`. If unsuccessful, the `error` parameter returns the cause of the failure.

---

Copyright &copy; 2026 Apple Inc. All rights reserved. | [Terms of Use](https://www.apple.com/legal/internet-services/terms/site.html) | [Privacy Policy](https://www.apple.com/privacy/privacy-policy)