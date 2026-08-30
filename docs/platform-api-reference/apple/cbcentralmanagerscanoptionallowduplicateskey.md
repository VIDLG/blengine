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
  "identifier" : "/documentation/CoreBluetooth/CBCentralManagerScanOptionAllowDuplicatesKey",
  "metadataVersion" : "0.1.0",
  "role" : "Global Variable",
  "symbol" : {
    "kind" : "Global Variable",
    "modules" : [
      "Core Bluetooth"
    ],
    "preciseIdentifier" : "c:@CBCentralManagerScanOptionAllowDuplicatesKey"
  },
  "title" : "CBCentralManagerScanOptionAllowDuplicatesKey"
}
-->

# CBCentralManagerScanOptionAllowDuplicatesKey

A Boolean value that specifies whether the scan should run without duplicate filtering.

```
let CBCentralManagerScanOptionAllowDuplicatesKey: String
```

## Discussion

The value for this key is an <doc://com.apple.documentation/documentation/Foundation/NSNumber> object. If <doc://com.apple.documentation/documentation/Swift/true>, the central disables filtering and generates a discovery event each time it receives an advertising packet from the peripheral. If <doc://com.apple.documentation/documentation/Swift/false> (the default), the central coalesces multiple discoveries of the same peripheral into a single discovery event.

> Important:
> Disabling this filtering can have an adverse effect on battery life; use it only if necessary.

---

Copyright &copy; 2026 Apple Inc. All rights reserved. | [Terms of Use](https://www.apple.com/legal/internet-services/terms/site.html) | [Privacy Policy](https://www.apple.com/privacy/privacy-policy)