<!--
{
  "availability" : [
    "iOS: 10.0.0 -",
    "iPadOS: 10.0.0 -",
    "macCatalyst: 13.1.0 -",
    "macOS: 10.13.0 -",
    "tvOS: 10.0.0 -",
    "visionOS: 1.0.0 -",
    "watchOS: 3.0.0 -"
  ],
  "documentType" : "symbol",
  "framework" : "CoreBluetooth",
  "identifier" : "/documentation/CoreBluetooth/CBManager",
  "metadataVersion" : "0.1.0",
  "role" : "Class",
  "symbol" : {
    "kind" : "Class",
    "modules" : [
      "Core Bluetooth"
    ],
    "preciseIdentifier" : "c:objc(cs)CBManager"
  },
  "title" : "CBManager"
}
-->

# CBManager

The abstract base class that manages central and peripheral objects.

```
class CBManager
```

## Topics

### Accessing the Manager’s Properties

[`var state: CBManagerState`](/documentation/CoreBluetooth/CBManager/state)

The current state of the manager.

[`enum CBManagerState`](/documentation/CoreBluetooth/CBManagerState)

The possible states of a Core Bluetooth manager.

### Determining Authorization State

[`class var authorization: CBManagerAuthorization`](/documentation/CoreBluetooth/CBManager/authorization-swift.type.property)

The current authorization status for using Bluetooth.

[`enum CBManagerAuthorization`](/documentation/CoreBluetooth/CBManagerAuthorization)

The current authorization state of a Core Bluetooth manager.

### Deprecated Properties

[`var authorization: CBManagerAuthorization`](/documentation/CoreBluetooth/CBManager/authorization-swift.property)

The current authorization status for using Bluetooth.

## Relationships

### Inherited By

[`CBCentralManager`](/documentation/CoreBluetooth/CBCentralManager)

[`CBPeripheralManager`](/documentation/CoreBluetooth/CBPeripheralManager)

### Conforms To

[`CustomDebugStringConvertible`](/documentation/Swift/CustomDebugStringConvertible)

[`NSObjectProtocol`](/documentation/ObjectiveC/NSObjectProtocol)

[`CVarArg`](/documentation/Swift/CVarArg)

[`CustomStringConvertible`](/documentation/Swift/CustomStringConvertible)

[`Hashable`](/documentation/Swift/Hashable)

[`Equatable`](/documentation/Swift/Equatable)

### Inherits From

[`NSObject-swift.class`](/documentation/ObjectiveC/NSObject-swift.class)

---

Copyright &copy; 2026 Apple Inc. All rights reserved. | [Terms of Use](https://www.apple.com/legal/internet-services/terms/site.html) | [Privacy Policy](https://www.apple.com/privacy/privacy-policy)