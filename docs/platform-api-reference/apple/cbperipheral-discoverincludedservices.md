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
  "identifier" : "/documentation/CoreBluetooth/CBPeripheral/discoverIncludedServices(_:for:)",
  "metadataVersion" : "0.1.0",
  "role" : "Instance Method",
  "symbol" : {
    "kind" : "Instance Method",
    "modules" : [
      "Core Bluetooth"
    ],
    "preciseIdentifier" : "c:objc(cs)CBPeripheral(im)discoverIncludedServices:forService:"
  },
  "title" : "discoverIncludedServices(_:for:)"
}
-->

# discoverIncludedServices(_:for:)

Discovers the specified included services of a previously-discovered service.

```
func discoverIncludedServices(_ includedServiceUUIDs: [CBUUID]?, for service: CBService)
```

## Parameters

`includedServiceUUIDs`

An array of [`CBUUID`](/documentation/CoreBluetooth/CBUUID) objects that you are interested in. Here, each [`CBUUID`](/documentation/CoreBluetooth/CBUUID) object represents a UUID that identifies the type of included service you want to discover.

`service`

The previously-discovered service whose included services you want to discover.

## Discussion

You can provide an array of [`CBUUID`](/documentation/CoreBluetooth/CBUUID) objects—representing included service UUIDs—in the `includedServiceUUIDs` parameter. When you do, the peripheral returns only the services of the peripheral that match the provided UUIDs.

> Note:
> If the `servicesUUIDs` parameter is `nil`, this method returns all of the peripheral’s available services. This is much slower than providing an array of service UUIDs to search for.

When the peripheral discovers one or more included services of the specified service, it calls the [`peripheral(_:didDiscoverIncludedServicesFor:error:)`](/documentation/CoreBluetooth/CBPeripheralDelegate/peripheral(_:didDiscoverIncludedServicesFor:error:)) method of its delegate object. After the service discovers its included services, you can access them through the service’s [`includedServices`](/documentation/CoreBluetooth/CBService/includedServices) property.

---

Copyright &copy; 2026 Apple Inc. All rights reserved. | [Terms of Use](https://www.apple.com/legal/internet-services/terms/site.html) | [Privacy Policy](https://www.apple.com/privacy/privacy-policy)