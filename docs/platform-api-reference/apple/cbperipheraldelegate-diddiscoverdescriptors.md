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
  "identifier" : "/documentation/CoreBluetooth/CBPeripheralDelegate/peripheral(_:didDiscoverDescriptorsFor:error:)",
  "metadataVersion" : "0.1.0",
  "role" : "Instance Method",
  "symbol" : {
    "kind" : "Instance Method",
    "modules" : [
      "Core Bluetooth"
    ],
    "preciseIdentifier" : "c:objc(pl)CBPeripheralDelegate(im)peripheral:didDiscoverDescriptorsForCharacteristic:error:"
  },
  "title" : "peripheral(_:didDiscoverDescriptorsFor:error:)"
}
-->

# peripheral(_:didDiscoverDescriptorsFor:error:)

Tells the delegate that the peripheral found descriptors for a characteristic.

```
optional func peripheral(_ peripheral: CBPeripheral, didDiscoverDescriptorsFor characteristic: CBCharacteristic, error: (any Error)?)
```

## Parameters

`peripheral`

The peripheral providing this information.

`characteristic`

The characteristic to which the characteristic descriptors belong.

`error`

The reason the call failed, or `nil` if no error occurred.

## Discussion

Core Bluetooth invokes this method when your app calls the [`discoverDescriptors(for:)`](/documentation/CoreBluetooth/CBPeripheral/discoverDescriptors(for:)) method. If the peripheral successfully discovers the descriptors of the specified characteristic, you can access them through the characteristic’s [`descriptors`](/documentation/CoreBluetooth/CBCharacteristic/descriptors) property. If successful, the `error` parameter is `nil`. If unsuccessful, the `error` parameter returns the cause of the failure.

---

Copyright &copy; 2026 Apple Inc. All rights reserved. | [Terms of Use](https://www.apple.com/legal/internet-services/terms/site.html) | [Privacy Policy](https://www.apple.com/privacy/privacy-policy)