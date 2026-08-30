<!--
{
  "availability" : [
    "iOS: 7.0.0 -",
    "iPadOS: 7.0.0 -",
    "macCatalyst: 13.1.0 -",
    "macOS: 10.9.0 -",
    "tvOS: 9.0.0 -",
    "visionOS: 1.0.0 -",
    "watchOS: 2.0.0 -"
  ],
  "documentType" : "symbol",
  "framework" : "CoreBluetooth",
  "identifier" : "/documentation/CoreBluetooth/CBPeripheralDelegate/peripheral(_:didModifyServices:)",
  "metadataVersion" : "0.1.0",
  "role" : "Instance Method",
  "symbol" : {
    "kind" : "Instance Method",
    "modules" : [
      "Core Bluetooth"
    ],
    "preciseIdentifier" : "c:objc(pl)CBPeripheralDelegate(im)peripheral:didModifyServices:"
  },
  "title" : "peripheral(_:didModifyServices:)"
}
-->

# peripheral(_:didModifyServices:)

Tells the delegate that a peripheral’s services changed.

```
optional func peripheral(_ peripheral: CBPeripheral, didModifyServices invalidatedServices: [CBService])
```

## Parameters

`peripheral`

The peripheral providing this information.

`invalidatedServices`

A list of services invalidated by this change.

## Discussion

Core Bluetooth invokes this method whenever one or more services of a peripheral change. A peripheral’s services have changed if:

- The peripheral removes a service from its database.
- The peripheral adds a new service to its database.
- The peripheral adds back a previously-removed service, but at a different location in the database.

The `invalidatedServices` parameter includes any changed services that you previously discovered; you can no longer use these services. You can use the [`discoverServices(_:)`](/documentation/CoreBluetooth/CBPeripheral/discoverServices(_:)) method to discover any new services that the peripheral added to its database. Use this same method to find out whether any of the invalidated services that you were using (and want to continue using) now have a different location in the peripheral’s database.

---

Copyright &copy; 2026 Apple Inc. All rights reserved. | [Terms of Use](https://www.apple.com/legal/internet-services/terms/site.html) | [Privacy Policy](https://www.apple.com/privacy/privacy-policy)