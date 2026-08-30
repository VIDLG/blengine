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
  "identifier" : "/documentation/CoreBluetooth/CBCentralManagerDelegate/centralManager(_:didDiscover:advertisementData:rssi:)",
  "metadataVersion" : "0.1.0",
  "role" : "Instance Method",
  "symbol" : {
    "kind" : "Instance Method",
    "modules" : [
      "Core Bluetooth"
    ],
    "preciseIdentifier" : "c:objc(pl)CBCentralManagerDelegate(im)centralManager:didDiscoverPeripheral:advertisementData:RSSI:"
  },
  "title" : "centralManager(_:didDiscover:advertisementData:rssi:)"
}
-->

# centralManager(_:didDiscover:advertisementData:rssi:)

Tells the delegate the central manager discovered a peripheral while scanning for devices.

```
optional func centralManager(_ central: CBCentralManager, didDiscover peripheral: CBPeripheral, advertisementData: [String : Any], rssi RSSI: NSNumber)
```

## Parameters

`central`

The central manager that provides the update.

`peripheral`

The discovered peripheral.

`advertisementData`

A dictionary containing any advertisement data.

`RSSI`

The current received signal strength indicator (RSSI) of the peripheral, in decibels.

## Discussion

You can access the advertisement data with the keys listed in [Advertisement Data Retrieval Keys](/documentation/CoreBluetooth/advertisement-data-retrieval-keys). You must retain a local copy of the peripheral if you want to perform commands on it. Use the RSSI data to determine the proximity of a discoverable peripheral device, and whether you want to connect to it automatically.

---

Copyright &copy; 2026 Apple Inc. All rights reserved. | [Terms of Use](https://www.apple.com/legal/internet-services/terms/site.html) | [Privacy Policy](https://www.apple.com/privacy/privacy-policy)