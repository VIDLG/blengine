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
  "identifier" : "/documentation/CoreBluetooth/CBPeripheralDelegate",
  "metadataVersion" : "0.1.0",
  "role" : "Protocol",
  "symbol" : {
    "kind" : "Protocol",
    "modules" : [
      "Core Bluetooth"
    ],
    "preciseIdentifier" : "c:objc(pl)CBPeripheralDelegate"
  },
  "title" : "CBPeripheralDelegate"
}
-->

# CBPeripheralDelegate

A protocol that provides updates on the use of a peripheral’s services.

```
protocol CBPeripheralDelegate : NSObjectProtocol
```

## Overview

The delegate of a [`CBPeripheral`](/documentation/CoreBluetooth/CBPeripheral) object must adopt the [`CBPeripheralDelegate`](/documentation/CoreBluetooth/CBPeripheralDelegate) protocol. The delegate uses this protocol’s methods to monitor the discovery, exploration, and interaction of a remote peripheral’s services and properties. This protocol doesn’t have any required methods.

## Topics

### Discovering Services

[`func peripheral(CBPeripheral, didDiscoverServices: (any Error)?)`](/documentation/CoreBluetooth/CBPeripheralDelegate/peripheral(_:didDiscoverServices:))

Tells the delegate that peripheral service discovery succeeded.

[`func peripheral(CBPeripheral, didDiscoverIncludedServicesFor: CBService, error: (any Error)?)`](/documentation/CoreBluetooth/CBPeripheralDelegate/peripheral(_:didDiscoverIncludedServicesFor:error:))

Tells the delegate that discovering included services within the indicated service completed.

### Discovering Characteristics and their Descriptors

[`func peripheral(CBPeripheral, didDiscoverCharacteristicsFor: CBService, error: (any Error)?)`](/documentation/CoreBluetooth/CBPeripheralDelegate/peripheral(_:didDiscoverCharacteristicsFor:error:))

Tells the delegate that the peripheral found characteristics for a service.

[`func peripheral(CBPeripheral, didDiscoverDescriptorsFor: CBCharacteristic, error: (any Error)?)`](/documentation/CoreBluetooth/CBPeripheralDelegate/peripheral(_:didDiscoverDescriptorsFor:error:))

Tells the delegate that the peripheral found descriptors for a characteristic.

### Retrieving Characteristic and Descriptor Values

[`func peripheral(CBPeripheral, didUpdateValueFor: CBCharacteristic, error: (any Error)?)`](/documentation/CoreBluetooth/CBPeripheralDelegate/peripheral(_:didUpdateValueFor:error:)-1xyna)

Tells the delegate that retrieving the specified characteristic’s value succeeded, or that the characteristic’s value changed.

[`func peripheral(CBPeripheral, didUpdateValueFor: CBDescriptor, error: (any Error)?)`](/documentation/CoreBluetooth/CBPeripheralDelegate/peripheral(_:didUpdateValueFor:error:)-1t3wm)

Tells the delegate that retrieving a specified characteristic descriptor’s value succeeded.

### Writing Characteristic and Descriptor Values

[`func peripheral(CBPeripheral, didWriteValueFor: CBCharacteristic, error: (any Error)?)`](/documentation/CoreBluetooth/CBPeripheralDelegate/peripheral(_:didWriteValueFor:error:)-4f5ea)

Tells the delegate that the peripheral successfully set a value for the characteristic.

[`func peripheral(CBPeripheral, didWriteValueFor: CBDescriptor, error: (any Error)?)`](/documentation/CoreBluetooth/CBPeripheralDelegate/peripheral(_:didWriteValueFor:error:)-1ybl3)

Tells the delegate that the peripheral successfully set a value for the descriptor.

[`func peripheralIsReady(toSendWriteWithoutResponse: CBPeripheral)`](/documentation/CoreBluetooth/CBPeripheralDelegate/peripheralIsReady(toSendWriteWithoutResponse:))

Tells the delegate that a peripheral is again ready to send characteristic updates.

### Managing Notifications for a Characteristic’s Value

[`func peripheral(CBPeripheral, didUpdateNotificationStateFor: CBCharacteristic, error: (any Error)?)`](/documentation/CoreBluetooth/CBPeripheralDelegate/peripheral(_:didUpdateNotificationStateFor:error:))

Tells the delegate that the peripheral received a request to start or stop providing notifications for a specified characteristic’s value.

### Retrieving a Peripheral’s RSSI Data

[`func peripheral(CBPeripheral, didReadRSSI: NSNumber, error: (any Error)?)`](/documentation/CoreBluetooth/CBPeripheralDelegate/peripheral(_:didReadRSSI:error:))

Tells the delegate that retrieving the value of the peripheral’s current Received Signal Strength Indicator (RSSI) succeeded.

[`func peripheralDidUpdateRSSI(CBPeripheral, error: (any Error)?)`](/documentation/CoreBluetooth/CBPeripheralDelegate/peripheralDidUpdateRSSI(_:error:))

Tells the delegate that retrieving the value of the peripheral’s current Received Signal Strength Indicator (RSSI) succeeded.

### Monitoring Changes to a Peripheral’s Name or Services

[`func peripheralDidUpdateName(CBPeripheral)`](/documentation/CoreBluetooth/CBPeripheralDelegate/peripheralDidUpdateName(_:))

Tells the delegate that a peripheral’s name changed.

[`func peripheral(CBPeripheral, didModifyServices: [CBService])`](/documentation/CoreBluetooth/CBPeripheralDelegate/peripheral(_:didModifyServices:))

Tells the delegate that a peripheral’s services changed.

### Monitoring L2CAP Channels

[`func peripheral(CBPeripheral, didOpen: CBL2CAPChannel?, error: (any Error)?)`](/documentation/CoreBluetooth/CBPeripheralDelegate/peripheral(_:didOpen:error:))

Delivers the result of an attempt to open an L2CAP channel.

### Instance Methods

[`func peripheral(CBPeripheral, didCompleteChannelSoundingSession: (any Error)?)`](/documentation/CoreBluetooth/CBPeripheralDelegate/peripheral(_:didCompleteChannelSoundingSession:))

[`func peripheral(CBPeripheral, didReceive: CBChannelSoundingProcedureResults?, error: (any Error)?)`](/documentation/CoreBluetooth/CBPeripheralDelegate/peripheral(_:didReceive:error:))

## Relationships

### Inherits From

[`NSObjectProtocol`](/documentation/ObjectiveC/NSObjectProtocol)

---

Copyright &copy; 2026 Apple Inc. All rights reserved. | [Terms of Use](https://www.apple.com/legal/internet-services/terms/site.html) | [Privacy Policy](https://www.apple.com/privacy/privacy-policy)