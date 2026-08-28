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
  "identifier" : "/documentation/CoreBluetooth/CBCentralManager/cancelPeripheralConnection(_:)",
  "metadataVersion" : "0.1.0",
  "role" : "Instance Method",
  "symbol" : {
    "kind" : "Instance Method",
    "modules" : [
      "Core Bluetooth"
    ],
    "preciseIdentifier" : "c:objc(cs)CBCentralManager(im)cancelPeripheralConnection:"
  },
  "title" : "cancelPeripheralConnection(_:)"
}
-->

# cancelPeripheralConnection(_:)

Cancels an active or pending local connection to a peripheral.

```
func cancelPeripheralConnection(_ peripheral: CBPeripheral)
```

## Parameters

`peripheral`

The peripheral to which the central manager is either trying to connect or has already connected.

## Discussion

This method is nonblocking, and any [`CBPeripheral`](/documentation/CoreBluetooth/CBPeripheral) class commands that are still pending to `peripheral` may not complete. Because other apps may still have a connection to the peripheral, canceling a local connection doesn’t guarantee that the underlying physical link is immediately disconnected. From the app’s perspective, however, the peripheral is effectively disconnected, and the central manager object calls the [`centralManager(_:didDisconnectPeripheral:error:)`](/documentation/CoreBluetooth/CBCentralManagerDelegate/centralManager(_:didDisconnectPeripheral:error:)) method of its delegate object.

---

Copyright &copy; 2026 Apple Inc. All rights reserved. | [Terms of Use](https://www.apple.com/legal/internet-services/terms/site.html) | [Privacy Policy](https://www.apple.com/privacy/privacy-policy)