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
  "identifier" : "/documentation/CoreBluetooth/CBPeripheral/discoverServices(_:)",
  "metadataVersion" : "0.1.0",
  "role" : "Instance Method",
  "symbol" : {
    "kind" : "Instance Method",
    "modules" : [
      "Core Bluetooth"
    ],
    "preciseIdentifier" : "c:objc(cs)CBPeripheral(im)discoverServices:"
  },
  "title" : "discoverServices(_:)"
}
-->

# discoverServices(_:)

Discovers the specified services of the peripheral.

```
func discoverServices(_ serviceUUIDs: [CBUUID]?)
```

## Parameters

`serviceUUIDs`

An array of [`CBUUID`](/documentation/CoreBluetooth/CBUUID) objects that you are interested in. Each [`CBUUID`](/documentation/CoreBluetooth/CBUUID) object represents a UUID that identifies the type of service you want to discover.

## Discussion

You can provide an array of [`CBUUID`](/documentation/CoreBluetooth/CBUUID) objects—representing service UUIDs—in the `serviceUUIDs` parameter. When you do, the peripheral returns only the services of the peripheral that match the provided UUIDs.

> Note:
> If the `servicesUUIDs` parameter is `nil`, this method returns all of the peripheral’s available services. This is much slower than providing an array of service UUIDs to search for.

When the peripheral discovers one or more services, it calls the [`peripheral(_:didDiscoverServices:)`](/documentation/CoreBluetooth/CBPeripheralDelegate/peripheral(_:didDiscoverServices:)): method of its delegate object. After a peripheral discovers services, you can access them through the peripheral’s [`services`](/documentation/CoreBluetooth/CBPeripheral/services) property.

---

Copyright &copy; 2026 Apple Inc. All rights reserved. | [Terms of Use](https://www.apple.com/legal/internet-services/terms/site.html) | [Privacy Policy](https://www.apple.com/privacy/privacy-policy)