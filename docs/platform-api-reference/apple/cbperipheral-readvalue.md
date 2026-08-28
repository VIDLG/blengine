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
  "identifier" : "/documentation/CoreBluetooth/CBPeripheral/readValue(for:)-6u2kr",
  "metadataVersion" : "0.1.0",
  "role" : "Instance Method",
  "symbol" : {
    "kind" : "Instance Method",
    "modules" : [
      "Core Bluetooth"
    ],
    "preciseIdentifier" : "c:objc(cs)CBPeripheral(im)readValueForCharacteristic:"
  },
  "title" : "readValue(for:)"
}
-->

# readValue(for:)

Retrieves the value of a specified characteristic.

```
func readValue(for characteristic: CBCharacteristic)
```

## Parameters

`characteristic`

The characteristic whose value you want to read.

## Discussion

When you call this method to read the value of a characteristic, the peripheral calls the [`peripheral(_:didUpdateValueFor:error:)`](/documentation/CoreBluetooth/CBPeripheralDelegate/peripheral(_:didUpdateValueFor:error:)-1xyna) method of its delegate object. If the peripheral successfully reads the value of the characteristic, you can access it through the characteristic’s [`value`](/documentation/CoreBluetooth/CBCharacteristic/value) property.

Not all characteristics have a readable value. You can determine whether a characteristic’s value is readable by accessing the relevant properties of the [`CBCharacteristicProperties`](/documentation/CoreBluetooth/CBCharacteristicProperties) enumeration.

---

Copyright &copy; 2026 Apple Inc. All rights reserved. | [Terms of Use](https://www.apple.com/legal/internet-services/terms/site.html) | [Privacy Policy](https://www.apple.com/privacy/privacy-policy)