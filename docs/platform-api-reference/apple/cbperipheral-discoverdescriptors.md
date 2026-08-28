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
  "identifier" : "/documentation/CoreBluetooth/CBPeripheral/discoverDescriptors(for:)",
  "metadataVersion" : "0.1.0",
  "role" : "Instance Method",
  "symbol" : {
    "kind" : "Instance Method",
    "modules" : [
      "Core Bluetooth"
    ],
    "preciseIdentifier" : "c:objc(cs)CBPeripheral(im)discoverDescriptorsForCharacteristic:"
  },
  "title" : "discoverDescriptors(for:)"
}
-->

# discoverDescriptors(for:)

Discovers the descriptors of a characteristic.

```
func discoverDescriptors(for characteristic: CBCharacteristic)
```

## Parameters

`characteristic`

The characteristic whose descriptors you want to discover.

## Discussion

When the peripheral discovers one or more descriptors of the specified characteristic, it calls the [`peripheral(_:didDiscoverDescriptorsFor:error:)`](/documentation/CoreBluetooth/CBPeripheralDelegate/peripheral(_:didDiscoverDescriptorsFor:error:)) method of its delegate object. After the peripheral discovers the descriptors of the characteristic, you can access them through the characteristic’s [`descriptors`](/documentation/CoreBluetooth/CBCharacteristic/descriptors) property.

---

Copyright &copy; 2026 Apple Inc. All rights reserved. | [Terms of Use](https://www.apple.com/legal/internet-services/terms/site.html) | [Privacy Policy](https://www.apple.com/privacy/privacy-policy)