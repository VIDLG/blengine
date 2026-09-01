# Download pinned VM assets and install the exact Packer QEMU plugin.
vm-bootstrap:
    {{vm_control}} bootstrap

# Build and provision the Debian + BlueZ + BLESS guest image under TCG.
vm-build:
    {{vm_control}} build

# Check the running guest, BlueZ, Pixi, BLESS, and optional USB controller.
vm-doctor:
    {{vm_control}} doctor

# Start the already provisioned Linux Peripheral VM.
vm-start:
    {{vm_control}} start

# Gracefully stop the Linux Peripheral VM.
vm-stop:
    {{vm_control}} stop

# Stop the VM and discard its mutable runtime overlay.
vm-reset:
    {{vm_control}} reset

# Report host assets, image state, QMP state, and guest reachability.
vm-status:
    {{vm_control}} status

# Print retained runtime/Packer serial, cloud-init, and fixture service logs.
vm-logs:
    {{vm_control}} logs

# Remove all Linux Peripheral VM runtime/download/build state, but keep QEMU.
vm-clean:
    {{vm_control}} clean

# Report the exact UB500 Windows driver and QEMU attachment state.
vm-usb-status:
    {{vm_control}} usb-status

# Launch pinned Zadig for the explicit UB500 BTHUSB-to-WinUSB transition.
vm-usb-bind:
    {{vm_control}} usb-bind

# Remove the generated WinUSB package and restore the inbox BTHUSB binding.
vm-usb-rollback:
    {{vm_control}} usb-rollback

# Hotplug the WinUSB-bound UB500 into the running QEMU guest.
vm-usb-attach:
    {{vm_control}} usb-attach

# Stop the fixture and hot-unplug the UB500 from QEMU.
vm-usb-detach:
    {{vm_control}} usb-detach

# Resolve and install the locked BLESS fixture environment in the guest.
vm-fixture-install:
    {{vm_control}} fixture-install

# Synchronize and validate BLESS source/scenario without network access.
vm-fixture-sync:
    {{vm_control}} fixture-sync

# Synchronize and start the shared basic-gatt-v1 BLESS fixture in the guest.
vm-fixture-start:
    {{vm_control}} fixture-start

# Trigger the BLESS updates-role characteristic with a deterministic payload.
vm-fixture-update value="deadbeef":
    {{vm_control}} fixture-update {{value}}

# Trigger a paced guest-local burst of deterministic 20-byte notifications.
vm-fixture-burst burst_id count interval_us="0" payload_bytes="20":
    {{vm_control}} fixture-burst {{burst_id}} {{count}} {{interval_us}} {{payload_bytes}}

# Reset BLESS's callback-side sequence ledger for one write benchmark batch.
vm-fixture-write-batch-start batch_id count:
    {{vm_control}} fixture-write-batch-start {{batch_id}} {{count}}

# Query BLESS's callback-side sequence ledger once after a write benchmark batch.
vm-fixture-write-batch-status batch_id:
    {{vm_control}} fixture-write-batch-status {{batch_id}}

# Request and verify LE 2M on the fixture's single active connection.
vm-fixture-request-throughput-phy:
    {{vm_control}} fixture-request-throughput-phy

# Stop the guest BLESS fixture.
vm-fixture-stop:
    {{vm_control}} fixture-stop

# Report the guest BLESS fixture service and readiness document.
vm-fixture-status:
    {{vm_control}} fixture-status

# Report whether the Windows Hypervisor Platform feature is enabled.
vm-whpx-status:
    {{vm_control}} whpx-status

# Enable Windows Hypervisor Platform without rebooting automatically.
vm-whpx-enable:
    {{vm_control}} whpx-enable
