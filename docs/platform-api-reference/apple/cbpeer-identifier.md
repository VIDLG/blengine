<!--
{
  "availability" : [
    "iOS: 7.0.0 -",
    "iPadOS: 7.0.0 -",
    "macCatalyst: 13.1.0 -",
    "macOS: 10.13.0 -",
    "tvOS: 9.0.0 -",
    "visionOS: 1.0.0 -",
    "watchOS: 2.0.0 -"
  ],
  "documentType" : "symbol",
  "framework" : "CoreBluetooth",
  "identifier" : "/documentation/CoreBluetooth/CBPeer/identifier",
  "metadataVersion" : "0.1.0",
  "role" : "Instance Property",
  "symbol" : {
    "kind" : "Instance Property",
    "modules" : [
      "Core Bluetooth"
    ],
    "preciseIdentifier" : "c:objc(cs)CBPeer(py)identifier"
  },
  "title" : "identifier"
}
-->

# identifier

The UUID associated with the peer.

```
var identifier: UUID { get }
```

## Discussion

The value of this property represents the unique identifier of the peer. The first time a local manager encounters a peer, the system assigns the peer a UUID, represented by a new <doc://com.apple.documentation/documentation/Foundation/UUID> object. Peers use <doc://com.apple.documentation/documentation/Foundation/UUID> instances to identify themselves, instead of by the [`CBUUID`](/documentation/CoreBluetooth/CBUUID) objects that identify a peripheral’s services, characteristics, and descriptors.

For more information, see [Core Bluetooth Programming Guide](https://developer.apple.com/library/archive/documentation/NetworkingInternetWeb/Conceptual/CoreBluetooth_concepts/AboutCoreBluetooth/Introduction.html#//apple_ref/doc/uid/TP40013257).

---

Copyright &copy; 2026 Apple Inc. All rights reserved. | [Terms of Use](https://www.apple.com/legal/internet-services/terms/site.html) | [Privacy Policy](https://www.apple.com/privacy/privacy-policy)