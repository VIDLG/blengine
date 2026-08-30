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
  "identifier" : "/documentation/CoreBluetooth/CBPeripheralDelegate/peripheral(_:didDiscoverIncludedServicesFor:error:)",
  "metadataVersion" : "0.1.0",
  "role" : "Instance Method",
  "symbol" : {
    "kind" : "Instance Method",
    "modules" : [
      "Core Bluetooth"
    ],
    "preciseIdentifier" : "c:objc(pl)CBPeripheralDelegate(im)peripheral:didDiscoverIncludedServicesForService:error:"
  },
  "title" : "peripheral(_:didDiscoverIncludedServicesFor:error:)"
}
-->

# peripheral(_:didDiscoverIncludedServicesFor:error:)

Tells the delegate that discovering included services within the indicated service completed.

```
optional func peripheral(_ peripheral: CBPeripheral, didDiscoverIncludedServicesFor service: CBService, error: (any Error)?)
```

## Parameters

`peripheral`

The peripheral providing this information.

`service`

The [`CBService`](/documentation/CoreBluetooth/CBService) object containing the included service.

`error`

The reason the call failed, or `nil` if no error occurred.

## Discussion

Core Bluetooth invokes this method when your app calls the [`discoverIncludedServices(_:for:)`](/documentation/CoreBluetooth/CBPeripheral/discoverIncludedServices(_:for:)) method. If the peripheral successfully discovers services, you can access them through the service’s [`includedServices`](/documentation/CoreBluetooth/CBService/includedServices) property. If successful, the `error` parameter is `nil`. If unsuccessful, the `error` parameter returns the cause of the failure.

---

Copyright &copy; 2026 Apple Inc. All rights reserved. | [Terms of Use](https://www.apple.com/legal/internet-services/terms/site.html) | [Privacy Policy](https://www.apple.com/privacy/privacy-policy)