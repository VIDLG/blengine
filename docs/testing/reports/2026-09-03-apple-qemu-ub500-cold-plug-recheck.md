# Apple-hosted QEMU/UB500 cold-plug recheck

Date: 2026-09-03

## Scope

This report records a bounded physical cold-plug recheck of the Mac mini's complete local QEMU/Debian/BlueZ/BLESS control path. The design goal is retained: macOS should use the same managed Linux Peripheral control semantics as Windows when the hardware path is available, while Windows QEMU support remains intact. This report does not claim BLESS advertising or Apple RF success.

## Host and controller

The physically reinserted controller matched the pinned identity:

```text
host_usb_friendly_name=TP-Link UB500 Adapter
host_usb_serial=D4D6DFEB8369
host_usb_vendor_id=2357
host_usb_product_id=0604
host_usb_matching_present_devices=1
```

The provisioned Debian arm64 image and QEMU 11.1.1/HVF path were reused.

## Unix process-lifetime correction

The first VM start reached SSH, but the QEMU process received host signal 1 after the launching shell command exited. The Windows path already creates a separate process group. The Unix controller now starts QEMU in a new session, after which a separate `vm-status` invocation confirmed:

```text
vm_running=true
vm_qmp_status={"running": true, "status": "running"}
guest_ssh=ready
```

This closes a macOS host-controller persistence bug without changing Windows process flags.

## Default usb-host cold-plug result

With the default QEMU `usb-host` reset behavior:

- QEMU claimed the exact UB500;
- the guest enumerated `2357:0604`;
- Linux registered and bound `btusb`;
- Bluetooth core initialized;
- Realtek vendor command `0xfc61` timed out;
- BlueZ exposed zero controllers.

Observed guest evidence:

```text
usb_2357_0604=present
bluetooth_service=active
bluetooth_controllers=0
Bluetooth: hci0: command 0xfc61 tx timeout
Bluetooth: hci0: RTL: Failed to generate devcoredump
Bluetooth: hci0: RTL: RTL: Read reg16 failed (-110)
```

The physical power cycle therefore did not close the previously observed HCI blocker.

## Bounded reset experiment

QEMU 11.1.1 advertises a documented `usb-host` property named `guest-resets-all`, defaulting to off. Because UB500 exposes two interfaces, one bounded macOS-only experiment enabled this option. The guest then failed to observe the USB device within the attach timeout, so the option was removed and is not part of the retained configuration.

That timeout exposed a rollback gap: a timed-out guest `lsusb` subprocess bypassed `delete_qmp_usb()`. The attach controller now converts that timeout into a failure-atomic QMP detach before returning an error.

## Cleanup

The experimental QEMU process was stopped and the controller returned to the macOS host:

```text
host_usb_status=present
qemu_usb_attached=unavailable
vm_running=false
```

No BLESS fixture was started and no RF advertisement was emitted.

## Decision

- Keep the full Apple Silicon arm64/HVF QEMU control implementation.
- Keep the existing Windows QEMU/UB500 implementation unchanged.
- Align Windows and macOS fixture commands, readiness documents, profiles, and test assertions.
- Do not mark Mac QEMU/BLESS as a ready RF provider until Linux exposes exactly one BlueZ controller and BLESS reaches run-correlated readiness.
- Continue macOS backend functional RF against the Android fixture.
- Use native macOS BLESS/CoreBluetooth as a separately controlled Peripheral for iPhone tests; it does not replace BlueZ/controller-fault evidence.
- A different Linux-compatible USB controller or a future QEMU/libusb/kernel fix may close the Mac HCI blocker without redesigning the control plane.
