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
  "identifier" : "/documentation/CoreBluetooth/CBPeripheral/readRSSI()",
  "metadataVersion" : "0.1.0",
  "role" : "Instance Method",
  "symbol" : {
    "kind" : "Instance Method",
    "modules" : [
      "Core Bluetooth"
    ],
    "preciseIdentifier" : "c:objc(cs)CBPeripheral(im)readRSSI"
  },
  "title" : "readRSSI()"
}
-->

# readRSSI()

Retrieves the current RSSI value for the peripheral while connected to the central manager.

```
func readRSSI()
```

## Discussion

On macOS, when you call this method to retrieve the Received Signal Strength Indicator (RSSI) of the peripheral while connected to the central manager, the peripheral calls the [`peripheralDidUpdateRSSI(_:error:)`](/documentation/CoreBluetooth/CBPeripheralDelegate/peripheralDidUpdateRSSI(_:error:)) method of its delegate object. If retrieving the RSSI value of the peripheral succeeds, you can access it through the peripheral’s [`rssi`](/documentation/CoreBluetooth/CBPeripheral/rssi) property.

On iOS and tvOS, when you call this method to retrieve the RSSI of the peripheral while connected to the central manager, the peripheral calls the [`peripheral(_:didReadRSSI:error:)`](/documentation/CoreBluetooth/CBPeripheralDelegate/peripheral(_:didReadRSSI:error:)) method of its delegate object, which includes the RSSI value as a parameter.

---

Copyright &copy; 2026 Apple Inc. All rights reserved. | [Terms of Use](https://www.apple.com/legal/internet-services/terms/site.html) | [Privacy Policy](https://www.apple.com/privacy/privacy-policy)