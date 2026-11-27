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
  "identifier" : "/documentation/CoreBluetooth/CBPeripheral",
  "metadataVersion" : "0.1.0",
  "role" : "Class",
  "symbol" : {
    "kind" : "Class",
    "modules" : [
      "Core Bluetooth"
    ],
    "preciseIdentifier" : "c:objc(cs)CBPeripheral"
  },
  "title" : "CBPeripheral"
}
-->

# CBPeripheral

A remote peripheral device.

```
class CBPeripheral
```

## Overview

The [`CBPeripheral`](/documentation/CoreBluetooth/CBPeripheral) class represents remote peripheral devices that your app discovers with a central manager (an instance of [`CBCentralManager`](/documentation/CoreBluetooth/CBCentralManager)). Peripherals use universally unique identifiers (UUIDs), represented by <doc://com.apple.documentation/documentation/Foundation/NSUUID> objects, to identify themselves. Peripherals may contain one or more services or provide useful information about their connected signal strength.

You use this class to discover, explore, and interact with the services available on a remote peripheral that supports Bluetooth low energy. A service encapsulates the way part of the device behaves. For example, one service of a heart rate monitor may be to expose heart rate data from a sensor. Services themselves contain of characteristics or included services (references to other services). Characteristics provide further details about a peripheral’s service. For example, the heart rate service may contain multiple characteristics. One characteristic could describe the intended body location of the device’s heart rate sensor, and another characteristic could transmit the heart rate measurement data. Finally, characteristics contain any number of descriptors that provide more information about the characteristic’s value, such as a human-readable description and a way to format the value.

## Topics

### Identifying a Peripheral

[`var name: String?`](/documentation/CoreBluetooth/CBPeripheral/name)

The name of the peripheral.

[`var delegate: (any CBPeripheralDelegate)?`](/documentation/CoreBluetooth/CBPeripheral/delegate)

The delegate object specified to receive peripheral events.

### Discovering Services

[`func discoverServices([CBUUID]?)`](/documentation/CoreBluetooth/CBPeripheral/discoverServices(_:))

Discovers the specified services of the peripheral.

[`func discoverIncludedServices([CBUUID]?, for: CBService)`](/documentation/CoreBluetooth/CBPeripheral/discoverIncludedServices(_:for:))

Discovers the specified included services of a previously-discovered service.

[`var services: [CBService]?`](/documentation/CoreBluetooth/CBPeripheral/services)

A list of a peripheral’s discovered services.

### Discovering Characteristics and Descriptors

[`func discoverCharacteristics([CBUUID]?, for: CBService)`](/documentation/CoreBluetooth/CBPeripheral/discoverCharacteristics(_:for:))

Discovers the specified characteristics of a service.

[`func discoverDescriptors(for: CBCharacteristic)`](/documentation/CoreBluetooth/CBPeripheral/discoverDescriptors(for:))

Discovers the descriptors of a characteristic.

### Reading Characteristic and Descriptor Values

[`func readValue(for: CBCharacteristic)`](/documentation/CoreBluetooth/CBPeripheral/readValue(for:)-6u2kr)

Retrieves the value of a specified characteristic.

[`func readValue(for: CBDescriptor)`](/documentation/CoreBluetooth/CBPeripheral/readValue(for:)-91hhp)

Retrieves the value of a specified characteristic descriptor.

### Writing Characteristic and Descriptor Values

[`func writeValue(Data, for: CBCharacteristic, type: CBCharacteristicWriteType)`](/documentation/CoreBluetooth/CBPeripheral/writeValue(_:for:type:))

Writes the value of a characteristic.

[`func writeValue(Data, for: CBDescriptor)`](/documentation/CoreBluetooth/CBPeripheral/writeValue(_:for:))

Writes the value of a characteristic descriptor.

[`func maximumWriteValueLength(for: CBCharacteristicWriteType) -> Int`](/documentation/CoreBluetooth/CBPeripheral/maximumWriteValueLength(for:))

The maximum amount of data, in bytes, you can send to a characteristic in a single write type.

[`enum CBCharacteristicWriteType`](/documentation/CoreBluetooth/CBCharacteristicWriteType)

Values representing the possible write types to a characteristic’s value.

### Setting Notifications for a Characteristic’s Value

[`func setNotifyValue(Bool, for: CBCharacteristic)`](/documentation/CoreBluetooth/CBPeripheral/setNotifyValue(_:for:))

Sets notifications or indications for the value of a specified characteristic.

### Monitoring a Peripheral’s Connection State

[`var state: CBPeripheralState`](/documentation/CoreBluetooth/CBPeripheral/state)

The connection state of the peripheral.

[`enum CBPeripheralState`](/documentation/CoreBluetooth/CBPeripheralState)

Values representing the connection state of a peripheral.

[`var canSendWriteWithoutResponse: Bool`](/documentation/CoreBluetooth/CBPeripheral/canSendWriteWithoutResponse)

A Boolean value that indicates whether the remote device can send a write without a response.

### Accessing a Peripheral’s Signal Strength

[`func readRSSI()`](/documentation/CoreBluetooth/CBPeripheral/readRSSI())

Retrieves the current RSSI value for the peripheral while connected to the central manager.

[`var rssi: NSNumber?`](/documentation/CoreBluetooth/CBPeripheral/rssi)

The Received Signal Strength Indicator (RSSI), in decibels, of the peripheral.

### Working with L2CAP Channels

[`func openL2CAPChannel(CBL2CAPPSM)`](/documentation/CoreBluetooth/CBPeripheral/openL2CAPChannel(_:))

Attempts to open an L2CAP channel to the peripheral using the supplied Protocol/Service Multiplexer (PSM).

[`class CBL2CAPChannel`](/documentation/CoreBluetooth/CBL2CAPChannel)

A live L2CAP connection to a remote device.

[`typealias CBL2CAPPSM`](/documentation/CoreBluetooth/CBL2CAPPSM)

The type of PSM identifiers.

### Working with Apple Notification Center Service (ANCS)

[`var ancsAuthorized: Bool`](/documentation/CoreBluetooth/CBPeripheral/ancsAuthorized)

A Boolean value that indicates if the remote device has authorization to receive data over ANCS protocol.

### Instance Methods

[`func cancelChannelSoundingSession()`](/documentation/CoreBluetooth/CBPeripheral/cancelChannelSoundingSession())

[`func startChannelSoundingSession(CBChannelSoundingSessionConfiguration)`](/documentation/CoreBluetooth/CBPeripheral/startChannelSoundingSession(_:))

## Relationships

### Conforms To

[`NSCopying`](/documentation/Foundation/NSCopying)

[`NSObjectProtocol`](/documentation/ObjectiveC/NSObjectProtocol)

[`CustomStringConvertible`](/documentation/Swift/CustomStringConvertible)

[`CVarArg`](/documentation/Swift/CVarArg)

[`Equatable`](/documentation/Swift/Equatable)

[`Hashable`](/documentation/Swift/Hashable)

[`CustomDebugStringConvertible`](/documentation/Swift/CustomDebugStringConvertible)

### Inherits From

[`CBPeer`](/documentation/CoreBluetooth/CBPeer)

---

Copyright &copy; 2026 Apple Inc. All rights reserved. | [Terms of Use](https://www.apple.com/legal/internet-services/terms/site.html) | [Privacy Policy](https://www.apple.com/privacy/privacy-policy)