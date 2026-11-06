<!--
{
  "availability" : [
    "iOS: 8.0.0 -",
    "iPadOS: 8.0.0 -",
    "macCatalyst: 13.1.0 -",
    "macOS: 10.13.0 -",
    "tvOS: 9.0.0 -",
    "visionOS: 1.0.0 -",
    "watchOS: 2.0.0 -"
  ],
  "documentType" : "symbol",
  "framework" : "CoreBluetooth",
  "identifier" : "/documentation/CoreBluetooth/CBPeer",
  "metadataVersion" : "0.1.0",
  "role" : "Class",
  "symbol" : {
    "kind" : "Class",
    "modules" : [
      "Core Bluetooth"
    ],
    "preciseIdentifier" : "c:objc(cs)CBPeer"
  },
  "title" : "CBPeer"
}
-->

# CBPeer

An object that represents a remote device.

```
class CBPeer
```

## Overview

The [`CBPeer`](/documentation/CoreBluetooth/CBPeer) class is an abstract base class that defines common behavior for objects representing remote devices. You typically don’t create instances of either [`CBPeer`](/documentation/CoreBluetooth/CBPeer) or its concrete subclasses. Instead, the system creates them for you during the process of peer discovery.

Your app takes the role of either a central (by creating an instance of [`CBCentralManager`](/documentation/CoreBluetooth/CBCentralManager)) or a peripheral (by creating an instance of [`CBPeripheralManager`](/documentation/CoreBluetooth/CBPeripheralManager)), and interacts through the manager with remote devices in the opposite role. During the process of peer discovery, where a central device scans for peripherals advertising services, the system creates objects from the concrete subclasses of [`CBPeer`](/documentation/CoreBluetooth/CBPeer) to represent discovered remote devices. The concrete subclasses of [`CBPeer`](/documentation/CoreBluetooth/CBPeer) are [`CBPeripheral`](/documentation/CoreBluetooth/CBPeripheral) and [`CBCentral`](/documentation/CoreBluetooth/CBCentral).

## Topics

### Identifying a Peer

[`var identifier: UUID`](/documentation/CoreBluetooth/CBPeer/identifier)

The UUID associated with the peer.

## Relationships

### Inherited By

[`CBCentral`](/documentation/CoreBluetooth/CBCentral)

[`CBPeripheral`](/documentation/CoreBluetooth/CBPeripheral)

### Conforms To

[`NSCopying`](/documentation/Foundation/NSCopying)

[`Equatable`](/documentation/Swift/Equatable)

[`CustomStringConvertible`](/documentation/Swift/CustomStringConvertible)

[`Hashable`](/documentation/Swift/Hashable)

[`CVarArg`](/documentation/Swift/CVarArg)

[`CustomDebugStringConvertible`](/documentation/Swift/CustomDebugStringConvertible)

[`NSObjectProtocol`](/documentation/ObjectiveC/NSObjectProtocol)

### Inherits From

[`NSObject-swift.class`](/documentation/ObjectiveC/NSObject-swift.class)

---

Copyright &copy; 2026 Apple Inc. All rights reserved. | [Terms of Use](https://www.apple.com/legal/internet-services/terms/site.html) | [Privacy Policy](https://www.apple.com/privacy/privacy-policy)