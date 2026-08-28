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
  "identifier" : "/documentation/CoreBluetooth/CBPeripheral/discoverCharacteristics(_:for:)",
  "metadataVersion" : "0.1.0",
  "role" : "Instance Method",
  "symbol" : {
    "kind" : "Instance Method",
    "modules" : [
      "Core Bluetooth"
    ],
    "preciseIdentifier" : "c:objc(cs)CBPeripheral(im)discoverCharacteristics:forService:"
  },
  "title" : "discoverCharacteristics(_:for:)"
}
-->

# discoverCharacteristics(_:for:)

Discovers the specified characteristics of a service.

```
func discoverCharacteristics(_ characteristicUUIDs: [CBUUID]?, for service: CBService)
```

## Parameters

`characteristicUUIDs`

An array of [`CBUUID`](/documentation/CoreBluetooth/CBUUID) objects that you are interested in. Each [`CBUUID`](/documentation/CoreBluetooth/CBUUID) object represents a UUID that identifies the type of a characteristic you want to discover.

`service`

The service whose characteristics you want to discover.

## Discussion

You can provide an array of [`CBUUID`](/documentation/CoreBluetooth/CBUUID) objects—representing characteristic UUIDs— in the `characteristicUUIDs` parameter. When you do, the peripheral returns only the characteristics of the service that match the provided UUIDs. If the `characteristicUUIDs` parameter is `nil`, this method returns all characteristics of the service.

> Note:
> If the `characteristicUUIDs` parameter is `nil`, this method returns all of the service’s characteristics. This is much slower than providing an array of characteristic UUIDs to search for.

When the peripheral discovers one or more characteristics of the specified service, it calls the [`peripheral(_:didDiscoverCharacteristicsFor:error:)`](/documentation/CoreBluetooth/CBPeripheralDelegate/peripheral(_:didDiscoverCharacteristicsFor:error:)) method of its delegate object. After the peripheral discovers the service’s characteristics, you can access them through the service’s [`characteristics`](/documentation/CoreBluetooth/CBService/characteristics) property.

---

Copyright &copy; 2026 Apple Inc. All rights reserved. | [Terms of Use](https://www.apple.com/legal/internet-services/terms/site.html) | [Privacy Policy](https://www.apple.com/privacy/privacy-policy)