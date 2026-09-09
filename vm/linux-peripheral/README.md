# Linux Peripheral VM

This directory contains the pinned Packer template, tracked metadata, and
provisioning inputs for the project-managed Debian + BlueZ Peripheral VM.
The BLESS environment and fixture source are deployed independently after boot.
Runtime images, overlays, QMP state, SSH keys, and logs are written under
`.work/vm/linux-peripheral/` and are not committed.

Use the project recipes through Pixi:

```sh
pixi run just peripheral vm-bootstrap
pixi run just peripheral vm-build
pixi run just peripheral vm-start
pixi run just peripheral vm-doctor
pixi run just peripheral vm-usb-attach
pixi run just peripheral vm-fixture-install
pixi run just peripheral vm-fixture-sync
pixi run just peripheral vm-fixture-start
pixi run just peripheral vm-logs
pixi run just peripheral vm-stop
pixi run just peripheral vm-reset
pixi run just peripheral vm-clean
```

`vm-bootstrap` is the only recipe that downloads or installs host assets. It
also installs the exact official QEMU Packer plugin declared by the selected HCL
template into `.work/vm/linux-peripheral/packer/plugins`. Windows selects the
original amd64 image lock and `q35` template. Apple Silicon macOS selects the
separate arm64 image lock and `virt`/HVF template, including pinned Debian and
Pixi assets for that guest architecture. `vm-build` delegates image creation,
the minimal NoCloud SSH bootstrap, file upload, provisioning, and shutdown to
Packer. The image contains Debian, BlueZ, D-Bus, Pixi, and a generic systemd
unit, but no BLESS source, fixture lock, or scenario.
`vm-start` only starts an already provisioned runtime overlay through the
project-specific Python runtime controller. Accelerator selection defaults to
`auto`: on Windows it uses WHPX when the `HypervisorPlatform` feature is enabled
and otherwise falls back to TCG; on Apple Silicon macOS it uses HVF. Set
`BLEHUB_VM_ACCEL=tcg` or the host-appropriate `whpx`/`hvf` only when an explicit
override is needed; the selected mode is always reported.

`vm-fixture-install` is the only command allowed to resolve/download guest
fixture dependencies. It installs the exact manifest and lock into a versioned
environment and atomically updates `current-environment`. `vm-fixture-sync`
copies and validates the allowlisted fixture source and scenario without network
access, then atomically updates `current`. `vm-fixture-start` requires the exact
active environment, performs source sync, restarts the service, and waits for
readiness; it never runs `pixi install` implicitly.

Runtime QMP sessions use the Pixi-pinned official `qemu.qmp` client. The Python
controller retains only project policy such as USB device commands and retry
behavior; it does not implement the QMP wire protocol.

QEMU resolves from `BLEHUB_QEMU_ROOT`, then
`blehub.local.toml` `[toolchains].qemu_root`. When no override is configured,
Windows uses the versioned bootstrap fallback at
`%LOCALAPPDATA%/QEMU/<version>`; the QEMU binary is host-shared and Pixi only
supplies the pinned `qemu.qmp` client and Packer tooling.

USB passthrough is an optional, host-specific gated step. The initial VM build deliberately does not capture a Bluetooth controller. When a real controller is available, configure its VID/PID (and optionally serial) in ignored `blehub.local.toml` under `[peripheral_usb]`, or use the corresponding `BLEHUB_PERIPHERAL_USB_*` environment variables. The generic `vm-usb-status`, `vm-usb-attach`, and `vm-usb-detach` recipes then provide QMP hotplug and guest readiness checks. No controller identity, vendor driver transition, or device-specific recovery procedure is tracked before the nRF52840 Dongle arrives.

The maintained Apple RF path does not use this VM: macOS/iOS use the Android fixture now and the nRF52840 DK when available. The Linux VM remains the Windows-hosted QEMU + BlueZ + BLESS control plane and is retained for future nRF52840 Dongle HCI firmware. A Mac QEMU run is not a maintained default Apple provider.
