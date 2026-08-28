<!--
{
  "availability" : [
    "iOS: 5.0.0 -",
    "iPadOS: 5.0.0 -",
    "macCatalyst: 13.0.0 -",
    "macOS: 10.10.0 -",
    "tvOS: 9.0.0 -",
    "visionOS: 1.0.0 -",
    "watchOS: 4.0.0 -"
  ],
  "documentType" : "symbol",
  "framework" : "CoreBluetooth",
  "identifier" : "/documentation/CoreBluetooth/CBCentralManagerDelegate",
  "metadataVersion" : "0.1.0",
  "role" : "Protocol",
  "symbol" : {
    "kind" : "Protocol",
    "modules" : [
      "Core Bluetooth"
    ],
    "preciseIdentifier" : "c:objc(pl)CBCentralManagerDelegate"
  },
  "title" : "CBCentralManagerDelegate"
}
-->

# CBCentralManagerDelegate

A protocol that provides updates for the discovery and management of peripheral devices.

```
protocol CBCentralManagerDelegate : NSObjectProtocol
```

## Overview

The [`CBCentralManagerDelegate`](/documentation/CoreBluetooth/CBCentralManagerDelegate) protocol defines the methods that a delegate of a [`CBCentralManager`](/documentation/CoreBluetooth/CBCentralManager) object must adopt. The optional methods of the protocol allow the delegate to monitor the discovery, connectivity, and retrieval of peripheral devices. The only required method is [`centralManagerDidUpdateState(_:)`](/documentation/CoreBluetooth/CBCentralManagerDelegate/centralManagerDidUpdateState(_:)); the central manager calls this when its state updates, thereby indicating the availability of the central manager.

## Topics

### Monitoring Connections with Peripherals

[`-  centralManager:didConnectPeripheral:`](/documentation/CoreBluetooth/CBCentralManagerDelegate/centralManager(_:didConnect:))

Tells the delegate that the central manager connected to a peripheral.

[`-  centralManager:didDisconnectPeripheral:error:`](/documentation/CoreBluetooth/CBCentralManagerDelegate/centralManager(_:didDisconnectPeripheral:error:))

Tells the delegate that the central manager disconnected from a peripheral.

[`-  centralManager:didFailToConnectPeripheral:error:`](/documentation/CoreBluetooth/CBCentralManagerDelegate/centralManager(_:didFailToConnect:error:))

Tells the delegate the central manager failed to create a connection with a peripheral.

[`-  centralManager:connectionEventDidOccur:forPeripheral:`](/documentation/CoreBluetooth/CBCentralManagerDelegate/centralManager(_:connectionEventDidOccur:for:))

Tells the delegate that a connection event occurred which matches the registered options.

### Discovering and Retrieving Peripherals

[`-  centralManager:didDiscoverPeripheral:advertisementData:RSSI:`](/documentation/CoreBluetooth/CBCentralManagerDelegate/centralManager(_:didDiscover:advertisementData:rssi:))

Tells the delegate the central manager discovered a peripheral while scanning for devices.

[Advertisement Data Retrieval Keys](/documentation/CoreBluetooth/advertisement-data-retrieval-keys)

Keys used to specify items in a dictionary of peripheral advertisement data.

### Monitoring the Central Manager’s State

[`-  centralManagerDidUpdateState:`](/documentation/CoreBluetooth/CBCentralManagerDelegate/centralManagerDidUpdateState(_:))

Tells the delegate the central manager’s state updated.

[`-  centralManager:willRestoreState:`](/documentation/CoreBluetooth/CBCentralManagerDelegate/centralManager(_:willRestoreState:))

Tells the delegate the system is about to restore the central manager.

### Monitoring the Central Manager’s Authorization

[`-  centralManager:didUpdateANCSAuthorizationForPeripheral:`](/documentation/CoreBluetooth/CBCentralManagerDelegate/centralManager(_:didUpdateANCSAuthorizationFor:))

Tells the delegate the authorization status changed for a ANCS-requiring connected peripheral.

### Instance Methods

[`-  centralManager:didDisconnectPeripheral:timestamp:isReconnecting:error:`](/documentation/CoreBluetooth/CBCentralManagerDelegate/centralManager(_:didDisconnectPeripheral:timestamp:isReconnecting:error:))

## Relationships

### Inherits From

[`NSObjectProtocol`](/documentation/ObjectiveC/NSObjectProtocol)

---

Copyright &copy; 2026 Apple Inc. All rights reserved. | [Terms of Use](https://www.apple.com/legal/internet-services/terms/site.html) | [Privacy Policy](https://www.apple.com/privacy/privacy-policy)