# Apple BLESS/QEMU macOS bring-up

Date: 2026-09-02

## Scope

This report records the attempt to use the project-managed Linux/BlueZ/BLESS Peripheral fixture from an Apple Silicon Mac mini. The Apple DUT remained the Mac mini built-in `BCM_4388C2` controller selected by CoreBluetooth. The Peripheral controller was the pinned TP-Link UB500 `2357:0604`, serial `D4D6DFEB8369`, passed exclusively to QEMU.

This was fixture-infrastructure bring-up. It was not an Apple backend scan result.

## Host and fixture setup

- Host: Apple Silicon Mac mini, macOS 26.2
- QEMU: Homebrew QEMU 11.1.1
- Packer: 1.16.0 with official QEMU plugin 1.1.6
- Guest: Debian 13 build `20260826-2582`
- Guest architecture: arm64
- Runtime accelerator: HVF
- Guest kernel: `6.12.105+deb13-arm64`
- BlueZ: 5.82
- BLESS: 0.3.0
- Fixture profile: `blehub-qemu-basic-gatt-v1`
- Advertised/root service UUID intended for this provider: `ffe00000-0000-4000-8000-000000000000`

The macOS-specific image lock pins the official Debian arm64 image and official Pixi aarch64 asset by checksum. The original Windows amd64 image lock and Packer template remain separate. The BLESS fixture lock now covers both `linux-64` and `linux-aarch64` while remaining readable by the guest-pinned Pixi 0.67.2.

## Passed setup gates

The following completed successfully:

1. macOS USB discovery found exactly one pinned UB500 and verified its serial.
2. Homebrew QEMU exposed `qemu-system-aarch64`, `hvf`, and `usb-host` support.
3. The official Debian arm64 image and Pixi aarch64 archive passed their pinned SHA-512/SHA-256 checks.
4. Packer built and provisioned the arm64 image under HVF.
5. The runtime VM booted under HVF and became SSH-ready.
6. Pre-USB guest doctor passed with the expected absence of a Bluetooth controller.
7. The `linux-aarch64` BLESS environment installed from the tracked lock.
8. The BLESS source and provider-specific scenario synchronized and validated.
9. QEMU exclusively claimed the UB500, and Linux enumerated both USB interfaces with `btusb`.

Representative runtime evidence:

```text
qemu_accelerator=hvf
guest_kernel=6.12.105+deb13-arm64
Bus 001 Device 002: ID 2357:0604 TP-Link TP-Link UB500 Adapter
Driver=btusb, 12M
```

## Blocking result

Linux created an `hci0` rfkill object, but the Realtek initialization command timed out before the controller became available through the Bluetooth management interface:

```text
Bluetooth: hci0: command 0xfc61 tx timeout
Bluetooth: hci0: RTL: Failed to generate devcoredump
Bluetooth: hci0: RTL: RTL: Read reg16 failed (-110)
bluetooth_controllers=0
```

The same failure occurred in all tested combinations:

- amd64 guest under TCG with QEMU `qemu-xhci`;
- arm64 guest under HVF with QEMU `qemu-xhci`;
- arm64 guest under HVF with a temporary `pci-ohci` Full-Speed controller test.

A guest-side `btusb` unload/reload also reproduced the same timeout. The OHCI experiment was not retained as project configuration. After testing, QEMU was stopped and the UB500 was released back to macOS.

This isolates the current blocker to the macOS QEMU/libusb passthrough path for this UB500/Realtek initialization sequence. It is not evidence of an Apple backend failure, a BLESS application failure, or a BlueZ D-Bus mapping failure. A physical unplug/replug power cycle was not executed during this run and remains an optional bounded recheck, not a claimed fix.

## RF classification

The BLESS fixture never reached advertising readiness, so the macOS provider-specific scan command was deliberately not run. None of the following are recorded as passed or failed:

- macOS BLESS advertisement discovery;
- Apple scan start/stop RF behavior;
- provider-specific `FFE0...` filtering over RF;
- Apple connect or GATT behavior.

At the end of the run no physical Android device was visible to ADB, so the project Android Peripheral fallback could not be started either.

## Next path

1. Prefer the project Android Peripheral for immediate macOS scan/connect/GATT development once a physical Android phone is connected, unlocked, and USB debugging is authorized.
2. Keep the arm64/HVF VM path as the macOS-native BLESS guest configuration; its provision, boot, environment, and source-sync gates are valid even though UB500 HCI initialization is blocked.
3. For authoritative BLESS RF, use the already validated Windows passthrough path or an independent Linux host unless a later bounded macOS passthrough experiment closes the `0xfc61` timeout.
4. Do not classify macOS BLESS scan as failed until the Peripheral reaches advertising readiness and the Apple scan gate actually executes.
