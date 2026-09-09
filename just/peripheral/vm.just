# Download pinned VM assets and install the exact Packer QEMU plugin.
[windows]
vm-bootstrap:
    {{vm_control}} bootstrap

# Build and provision the Debian + BlueZ + BLESS guest image under TCG.
[windows]
vm-build:
    {{vm_control}} build

# Check the running guest, BlueZ, Pixi, BLESS, and optional USB controller.
[windows]
vm-doctor:
    {{vm_control}} doctor

# Start the already provisioned Linux Peripheral VM.
[windows]
vm-start:
    {{vm_control}} start

# Gracefully stop the Linux Peripheral VM.
[windows]
vm-stop:
    {{vm_control}} stop

# Stop the VM and discard its mutable runtime overlay.
[windows]
vm-reset:
    {{vm_control}} reset

# Report host assets, image state, QMP state, and guest reachability.
[windows]
vm-status:
    {{vm_control}} status

# Print retained runtime/Packer serial, cloud-init, and fixture service logs.
[windows]
vm-logs:
    {{vm_control}} logs

# Remove all Linux Peripheral VM runtime/download/build state, but keep QEMU.
[windows]
vm-clean:
    {{vm_control}} clean

# Report configured USB controller identity and QEMU attachment state.
[windows]
vm-usb-status:
    {{vm_control}} usb-status

# Hotplug the configured USB controller into the running QEMU guest.
[windows]
vm-usb-attach:
    {{vm_control}} usb-attach

# Stop the fixture and hot-unplug the configured USB controller from QEMU.
[windows]
vm-usb-detach:
    {{vm_control}} usb-detach


# Restart BlueZ and prove controller, fixture, and guest readiness recover.
[windows]
vm-bluez-restart:
    {{vm_control}} bluez-restart

# Reboot the running guest and prove SSH, controller, fixture, and doctor recovery.
[windows]
vm-guest-reboot:
    {{vm_control}} guest-reboot

# Resolve and install the locked BLESS fixture environment in the guest.
[windows]
vm-fixture-install:
    {{vm_control}} fixture-install

# Synchronize and validate BLESS source/scenario without network access.
[windows]
vm-fixture-sync:
    {{vm_control}} fixture-sync

# Synchronize and start the shared basic-gatt-v1 BLESS fixture in the guest.
[windows]
vm-fixture-start:
    {{vm_control}} fixture-start

# Trigger the BLESS updates-role characteristic with a deterministic payload.
[windows]
vm-fixture-update value="deadbeef":
    {{vm_control}} fixture-update {{value}}

# Trigger a paced guest-local burst of deterministic 20-byte notifications.
[windows]
vm-fixture-burst burst_id count interval_us="0" payload_bytes="20":
    {{vm_control}} fixture-burst {{burst_id}} {{count}} {{interval_us}} {{payload_bytes}}

# Reset BLESS's callback-side sequence ledger for one write benchmark batch.
[windows]
vm-fixture-write-batch-start batch_id count:
    {{vm_control}} fixture-write-batch-start {{batch_id}} {{count}}

# Query BLESS's callback-side sequence ledger once after a write benchmark batch.
[windows]
vm-fixture-write-batch-status batch_id:
    {{vm_control}} fixture-write-batch-status {{batch_id}}

# Request and verify LE 2M on the fixture's single active connection.
[windows]
vm-fixture-request-throughput-phy:
    {{vm_control}} fixture-request-throughput-phy

# Stop the guest BLESS fixture.
[windows]
vm-fixture-stop:
    {{vm_control}} fixture-stop

# Report the guest BLESS fixture service and readiness document.
[windows]
vm-fixture-status:
    {{vm_control}} fixture-status

# Report whether the Windows Hypervisor Platform feature is enabled.
[windows]
vm-whpx-status:
    {{vm_control}} whpx-status

# Enable Windows Hypervisor Platform without rebooting automatically.
[windows]
vm-whpx-enable:
    {{vm_control}} whpx-enable
