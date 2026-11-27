# RN iOS CCC disable REJECT：v2-A 空口与 CoreBluetooth delegate 边界

**日期：** 2026-10-24  
**范围：** iPhone RN/Nitro → Apple CoreBluetooth → nRF52840 Multifunction Fault v2-A  运行 `ccc-disable-reject`，同时使用独立 nRF52840 Dongle passive sniffer 抓包。

## Candidate

- image ID：`nrf-multifunction-rf-v2-a`
- normalized image SHA-256：`9b97e9e0b332930de940e2c7c4ff2331e11d402e7f7f75215da870eef00ce7e1`
- DK-A：J-Link `1050252028`
- iPhone：`00008110-000C61160C39401E`
- contract/profile：nrftest release-mode candidate A
- sniffer：官方 nRF Sniffer firmware `4.1.1`
- sniffer firmware package SHA-256：`a5ec3b26bfe1d5db7bf77c4fb0d4470924778ccf440ea846965b8a2bcb719048`

## Execution

iOS gate：

```text
host-ios-device-rf-ccc-disable-reject
```

本次额外设置了：

```text
DEVICECTL_CHILD_BLEHUB_APPLE_NOTIFICATION_DIAGNOSTICS=1
```

Sniffer 使用 Scapy-backed capture tool，并对 DK-A 使用 address follow：

```text
dc:26:e2:e1:5b:69
```

Evidence：

- iOS evidence bundle：`tests/consumers/react-native-internal/ios/build/rf-ccc-disable-reject-6f436e341bb24f2781b87899dcb77f4f/`
- valid PCAP：`.work/sniffer/ios-ccc-disable-follow-address-dka.pcap`
- sniffer metadata：`.work/sniffer/ios-ccc-disable-follow-address-dka.pcap.json`
- Nordic PCAP：9,439 packets；2,122 data packets；ATT events 33

## Air evidence

Scapy decoded the following ordered sequence:

```text
ATT Write Request  handle=0x2A  value=01 00  direction=Central → Peripheral
Notification       handle=0x29  value=EF     direction=Peripheral → Central
ATT Write Request  handle=0x2A  value=00 00  direction=Central → Peripheral
Notification       handle=0x29  value=E1     direction=Peripheral → Central
```

The second notification is after the rejected CCC disable request. The Peripheral therefore submitted and transmitted the continued notification on the observed connection. This PCAP is the authoritative link-layer evidence for this candidate/run; it does not prove that iOS delivered the notification to the app.

## Apple native diagnostics

Before the CCC disable request, diagnostics showed:

```text
value.entry: characteristic=FFF2, valueHex=ef, routeMatches=true
value.disposition: notification
notification.ingress: result=delivered
```

For CCC disable:

```text
notification-state.entry: errorDomain=CBATTErrorDomain, errorCode=14
notification-state.result: completion=failed, phase=active
```

After the air notification carrying `E1`, there was no new `value.entry` for FFF2. The only later value callback was the queued read on FFF1. Consequently, the evidence does not show a CoreBluetooth delegate callback for the continued notification at all.

## Verdict and boundary

- Firmware identity and contract: passed for the recorded candidate.
- Peripheral CCC disable REJECT behavior: passed at the air/link layer.
- Continued notification after rejected disable: transmitted by the Peripheral and observed by the independent sniffer.
- Apple CoreBluetooth delivery of that notification: not observed; gate remains failed.
- Apple backend route projection: no evidence of a route drop for `E1`, because no corresponding `didUpdateValueFor` callback reached the backend.
- RN/App: no notification can be projected when CoreBluetooth does not deliver the callback.

This does **not** justify synthesizing the missing notification, weakening continuity assertions, or changing the firmware. The remaining gap is an iOS/CoreBluetooth controller/delegate delivery boundary after the failed CCC disable operation (`CBATTErrorDomain` code 14). Keep this run independent from `IOS-RADIO-GATE`; the radio gate is not a substitute for this continuity result.

## Official CoreBluetooth contract boundary

Apple's current CoreBluetooth documentation describes the ordinary contract, but not this failure combination:

- `CBPeripheral.setNotifyValue(_:for:)` requests enabling or disabling notifications/indications.
- `peripheral(_:didUpdateNotificationStateFor:error:)` reports whether that state request succeeded; an unsuccessful request supplies the failure reason through `error`.
- `peripheral(_:didUpdateValueFor:error:)` is used for reads and for value changes received while notifications are enabled.

The documentation does **not** specify whether a value transmitted by a Peripheral after a failed disable request must still produce a later `didUpdateValueFor` callback. Therefore this report treats the post-error `E1` behavior as an observed iOS/CoreBluetooth implementation boundary, not as a documented guarantee or a firmware contract. The Native HIL reproduction remains necessary to determine whether the same boundary occurs outside the RN/Nitro host.

Official references:

- [`setNotifyValue(_:for:)`](https://developer.apple.com/documentation/corebluetooth/cbperipheral/setnotifyvalue(_:for:))
- [`peripheral(_:didUpdateNotificationStateFor:error:)`](https://developer.apple.com/documentation/corebluetooth/cbperipheraldelegate/peripheral(_:didupdatenotificationstatefor:error:))
- [`peripheral(_:didUpdateValueFor:error:)`](https://developer.apple.com/documentation/corebluetooth/cbperipheraldelegate/peripheral(_:didupdatevaluefor:error:)-1xyna)
