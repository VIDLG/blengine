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
  "identifier" : "/documentation/CoreBluetooth/CBPeripheral/setNotifyValue(_:for:)",
  "metadataVersion" : "0.1.0",
  "role" : "Instance Method",
  "symbol" : {
    "kind" : "Instance Method",
    "modules" : [
      "Core Bluetooth"
    ],
    "preciseIdentifier" : "c:objc(cs)CBPeripheral(im)setNotifyValue:forCharacteristic:"
  },
  "title" : "setNotifyValue(_:for:)"
}
-->

# setNotifyValue(_:for:)

Sets notifications or indications for the value of a specified characteristic.

```
func setNotifyValue(_ enabled: Bool, for characteristic: CBCharacteristic)
```

## Parameters

`enabled`

A Boolean value that indicates whether to receive notifications or indications whenever the characteristic’s value changes. <doc://com.apple.documentation/documentation/Swift/true> if you want to enable notifications or indications for the characteristic’s value. <doc://com.apple.documentation/documentation/Swift/false> if you don’t want to receive notifications or indications whenever the characteristic’s value changes.

`characteristic`

The specified characteristic.

## Discussion

When you enable notifications for the characteristic’s value, the peripheral calls the [`peripheral(_:didUpdateNotificationStateFor:error:)`](/documentation/CoreBluetooth/CBPeripheralDelegate/peripheral(_:didUpdateNotificationStateFor:error:)) method of its delegate object to indicate if the action succeeded. If successful, the peripheral then calls the [`peripheral(_:didUpdateValueFor:error:)`](/documentation/CoreBluetooth/CBPeripheralDelegate/peripheral(_:didUpdateValueFor:error:)-1xyna) method of its delegate object whenever the characteristic value changes. Because the peripheral chooses when it sends an update, your app should prepare to handle them as long as notifications or indications remain enabled. If the specified characteristic’s configuration allows both notifications and indications, calling this method enables notifications only. You can disable notifications and indications for a characteristic’s value by calling this method with the `enabled` parameter set to <doc://com.apple.documentation/documentation/Swift/false>.

## Topics

### Related Documentation

[`let CBConnectPeripheralOptionNotifyOnNotificationKey: String`](/documentation/CoreBluetooth/CBConnectPeripheralOptionNotifyOnNotificationKey)

A Boolean value that specifies whether the system should display an alert for any notification sent by a peripheral.



---

Copyright &copy; 2026 Apple Inc. All rights reserved. | [Terms of Use](https://www.apple.com/legal/internet-services/terms/site.html) | [Privacy Policy](https://www.apple.com/privacy/privacy-policy)