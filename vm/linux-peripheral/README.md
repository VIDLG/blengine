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
also installs the exact official QEMU Packer plugin declared by the HCL template
into `.work/vm/linux-peripheral/packer/plugins`. `vm-build` delegates image
creation, the minimal NoCloud SSH bootstrap, file upload, provisioning, and
shutdown to Packer under TCG. The image contains Debian, BlueZ, D-Bus, Pixi,
and a generic systemd unit, but no BLESS source, fixture lock, or scenario.
`vm-start` only starts an already provisioned runtime overlay through the
project-specific Python runtime controller. Accelerator selection defaults to
`auto`: on Windows it uses WHPX when the `HypervisorPlatform` feature is enabled
and otherwise falls back to TCG. Set `BLEHUB_VM_ACCEL=tcg` or `whpx` only when an
explicit override is needed; the selected mode is always reported.

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
`blehub.local.toml` `[toolchains].qemu_root`, then
`~/.cache/blehub/toolchains/qemu`.

USB passthrough and the UB500 WinUSB driver transition are separate gated
steps. The initial VM build deliberately does not capture any Bluetooth
controller.
