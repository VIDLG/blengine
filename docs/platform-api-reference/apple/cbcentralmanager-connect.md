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
  "identifier" : "/documentation/CoreBluetooth/CBCentralManager/connect(_:options:)",
  "metadataVersion" : "0.1.0",
  "role" : "Instance Method",
  "symbol" : {
    "kind" : "Instance Method",
    "modules" : [
      "Core Bluetooth"
    ],
    "preciseIdentifier" : "c:objc(cs)CBCentralManager(im)connectPeripheral:options:"
  },
  "title" : "connect(_:options:)"
}
-->

# connect(_:options:)

Establishes a local connection to a peripheral.

```
func connect(_ peripheral: CBPeripheral, options: [String : Any]? = nil)
```

## Parameters

`peripheral`

The peripheral to which the central is attempting to connect.

`options`

A dictionary to customize the behavior of the connection. For available options, see [Peripheral Connection Options](/documentation/CoreBluetooth/peripheral-connection-options).

## Discussion

After successfully establishing a local connection to a peripheral, the central manager object calls the [`centralManager(_:didConnect:)`](/documentation/CoreBluetooth/CBCentralManagerDelegate/centralManager(_:didConnect:)) method of its delegate object. If the connection attempt fails, the central manager object calls the [`centralManager(_:didFailToConnect:error:)`](/documentation/CoreBluetooth/CBCentralManagerDelegate/centralManager(_:didFailToConnect:error:)) method of its delegate object instead. Attempts to connect to a peripheral don’t time out. To explicitly cancel a pending connection to a peripheral, call the [`cancelPeripheralConnection(_:)`](/documentation/CoreBluetooth/CBCentralManager/cancelPeripheralConnection(_:)) method. Deallocating `peripheral` also implicitly calls [`cancelPeripheralConnection(_:)`](/documentation/CoreBluetooth/CBCentralManager/cancelPeripheralConnection(_:)).

---

Copyright &copy; 2026 Apple Inc. All rights reserved. | [Terms of Use](https://www.apple.com/legal/internet-services/terms/site.html) | [Privacy Policy](https://www.apple.com/privacy/privacy-policy)