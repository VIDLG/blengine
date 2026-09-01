# Windows + QEMU/BLESS managed peripheral bring-up — 2026-08-31

## Result

**PASS for initial provider bring-up, Windows scan/connect, and public GATT discovery.**

This report records the first end-to-end run of the authoritative Linux
Peripheral VM topology on the intended Windows host. It is not the final HIL,
fault-injection, or soak report.

## Environment identity

| Field | Value |
|---|---|
| Execution time | 2026-08-31 11:58 +08:00 |
| Base revision | `fa97ad9be8a44d4bb1226c8830f79086ca9b5236`; VM/BLESS orchestration and current Windows backend work were uncommitted working-tree changes |
| Host | Windows 11 Home, build `26200` |
| QEMU | `11.1.0`, portable extraction of pinned `qemu-w64-setup-20260811.exe` |
| Accelerator | TCG; Windows `HypervisorPlatform` enable was requested with no automatic reboot, but WHPX was not active or tested in this run |
| Guest image | Debian 13 generic image build `20260826-2582` |
| Guest kernel | `6.12.105+deb13-amd64` |
| BlueZ | `5.82` |
| Guest Pixi | `0.67.2` |
| BLESS | `0.3.0`, with Bleak pinned to `1.1.1` in the guest fixture lock |
| Central controller | Internal MediaTek Bluetooth Adapter, USB `VID_13D3&PID_3585`, Windows status `OK` |
| Peripheral controller | TP-Link UB500, USB `2357:0604`, serial `D4D6DFEB8369`, address `D4:D6:DF:EB:83:69` |
| Host UB500 binding | WinUSB through pinned Zadig/libwdi, generated package `oem72.inf`; inbox rollback recipe retained |
| Fixture | `blehub-basic-gatt-v1`, readiness run ID `76839db7-e6c2-4963-807a-c84e10e9942b` |

The MediaTek controller remained owned by Windows as the Central controller.
Only the exact UB500 instance was switched from the inbox `BTHUSB` driver to
WinUSB and passed through to QEMU.

## Provisioning and provider commands

```text
pixi run just peripheral vm-bootstrap
pixi run just peripheral vm-build
pixi run just peripheral vm-start
pixi run just peripheral vm-usb-bind
pixi run just peripheral vm-usb-attach
pixi run just peripheral vm-doctor
pixi run just peripheral vm-fixture-start
```

The pinned artifacts were checksum-verified before use:

- QEMU Windows installer: SHA-512 from `vm/linux-peripheral/image.lock.toml`;
- Debian image: official immutable build URL and SHA-512;
- Pixi Linux binary: pinned version and SHA-256;
- Zadig: pinned libwdi release and SHA-256;
- BLESS/Bleak/Python dependencies: `tests/peripherals/bless/pixi.lock`.

The first `genericcloud` image probe booted and accepted SSH but exposed no
`/sys/bus/usb`, because its cloud kernel omits the physical USB host path needed
for passthrough. The provider was corrected to use the Debian `generic` image
from the same immutable build. The generic kernel exposed QEMU XHCI and the
passed-through controller.

## Guest doctor and readiness

The passing doctor reported:

```text
guest_os=debian 13
guest_kernel=6.12.105+deb13-amd64
dbus_service=active
pixi_version=pixi 0.67.2
bless_version=0.3.0
fixture_profile=blehub-basic-gatt-v1
usb_2357_0604=present
bluetooth_service=active
bluetooth_controllers=1
Controller D4:D6:DF:EB:83:69 blehub-peripheral [default]
rfkill: soft blocked no, hard blocked no
bluez_adapter_path=/org/bluez/hci0
bluez_gatt_manager=true
bluez_le_advertising_manager=true
bluez_supported_advertising_instances=3
```

BLESS then strictly loaded the original shared scenario (subsequently split into
[`qemu-basic-gatt-peripheral.json`](../../../tests/conformance/scenarios/qemu-basic-gatt-peripheral.json)
and its Android counterpart), registered the GATT application, started
advertising, and wrote a structured readiness document containing the profile,
run ID, and advertised service UUID.

## Windows RF commands

```text
pixi run just windows scan-smoke 60
pixi run just windows connect-smoke 60
pixi run just windows gatt-smoke 60
```

Observed results:

- Windows discovered `fff00000-0000-4000-8000-000000000000` over real RF with
  `connectable: true`;
- requested scan stop completed cleanly;
- connect emitted ordered `Connecting`, `Connected`, and terminal success;
- logical disconnect emitted `Disconnected` and terminal success;
- uncached public GATT discovery mapped services, characteristics, properties,
  descriptors, and the fixture CCCD into the unified `GattDatabase`;
- all four project characteristics matched the shared profile:
  `fff10000`, `fff20000`, `fff30000`, and `fff40000`;
- BlueZ also exposed standard Generic Access, Generic Attribute, and Device
  Information services, which the unified database preserved rather than
  hiding.

`local_name: None` was observed by Windows. This remains valid because the
shared profile sets `require_local_name` to `false`; discovery is anchored on
the service UUID.

## Recovery evidence

After the first passing RF run, the following complete recovery sequence also
passed:

```text
fixture stop
UB500 QMP hot-unplug
VM stop
clean provisioned-image rebuild
VM start
UB500 QMP hotplug
hardware doctor
BLESS fixture start/readiness
```

This proves one controlled stop/rebuild/start/reattach recovery. It does not
prove repeated USB reset, host reboot, guest reboot, physical replug, or soak
stability.

## Not proven

- Public characteristic read or write with/without response.
- Notification delivery, explicit indication selection, or delivery
  completion. BLESS `0.3.0` does not itself provide enough evidence for those
  stronger claims; `btmon` and Central observations are still required.
- Passive disconnect delivery.
- Manufacturer-data run-ID filtering. The initial BLESS Linux path reports the
  run ID over the control plane but does not advertise it.
- Fault injection, BlueZ restart recovery, controller reset, host reboot, or
  10-cycle scan/connect soak.
- WHPX operation after the required Windows reboot.
- Android, Apple, or Linux Central behavior against this provider.

These remain separate gates and must not be inferred from this bring-up pass.

## Packer build migration follow-up

Later on 2026-08-31, the image-build phase was migrated from custom Python
CIDATA ISO generation and build-time QEMU/QMP/SSH polling to Packer `1.16.0`
with the official QEMU plugin pinned to `1.1.6`. Packer generated a labeled
VFAT floppy internally; the guest reported
`DataSourceNoCloud [seed=/dev/fd0]`. Cloud-Init only created the build-time SSH
entry, while Packer file and shell provisioners uploaded and ran the tracked
guest inputs. The pinned Pixi archive was downloaded and checksum-verified by
`vm-bootstrap`, then uploaded to the guest, avoiding a guest-side GitHub fetch.

The Packer-produced image passed build, clean shutdown, runtime start, UB500
attach, guest doctor, BLESS readiness (run ID
`405ebbfb-8ce9-4947-ae33-e5ae274631bf`), and the Windows scan/connect/public
GATT discovery RF smokes. This follow-up validates the build-tool replacement;
it does not expand the BLE behavior claims listed above.

## External fixture deployment follow-up

Later on 2026-08-31, the image and fixture lifecycles were split into three
independently fingerprinted layers:

1. stable Packer-provisioned Debian/BlueZ/Pixi/systemd image;
2. versioned fixture Pixi environment installed only by explicit
   `vm-fixture-install`;
3. versioned BLESS source plus shared scenario deployed by no-network
   `vm-fixture-sync`.

The rebuilt Packer image uploaded only the pinned Pixi archive, provision script,
and generic systemd unit. It did not upload BLESS source, the fixture
manifest/lock, or the shared scenario. A fresh runtime passed core doctor with
both fixture layers absent, then passed UB500 attach and the BlueZ hardware gate:

```text
image_fingerprint=e294cad9a15bea954be666aaae9c4cc0017468b93d454ffb244023609eec5908
fixture_environment_fingerprint=missing
fixture_source_fingerprint=missing
usb_2357_0604=present
bluetooth_controllers=1
bluez_gatt_manager=true
bluez_le_advertising_manager=true
```

Starting the fixture at that point failed as intended with an instruction to run
`pixi run just peripheral vm-fixture-install`; no dependency installation was
performed implicitly. Explicit install and sync then activated these immutable
identities:

```text
fixture_environment_fingerprint=f53cd0aa2bd9ec2396013cc0b3097d21126a259f70d7de1aac0c7b374c641d50
fixture_source_fingerprint=a94d7b2d290562250939d9afec055a0aaec411c2af64d528b802ae703e472c08
fixture_profile=blehub-qemu-basic-gatt-v1
fixture_service_uuid=ffe00000-0000-4000-8000-000000000000
fixture_run_id=f2ea1819-89dc-41e2-9038-815f25fc4a72
```

The service executes the installed environment's Python directly. Source is
copied to a staging release, validated there, and only then exposed through an
atomic `current` symlink update. Repeated install and start operations reported
cache hits. Running `vm-build` after fixture deployment also reported
`vm_build_cached=true`, confirming that fixture state no longer invalidates the
stable image.

The hand-written QMP socket/protocol client was also replaced by the official
QEMU Python `qemu.qmp` package pinned to `0.0.6`. VM status, USB property query,
USB attach, and graceful stop passed through the new client. After the split,
Windows scan, connect, logical disconnect, and public GATT discovery RF smokes
passed again against the ready BLESS fixture, explicitly filtering the QEMU
provider's `FFE0...` service rather than the concurrently usable Android
provider's `FFF0...` service. This validates infrastructure separation without
expanding the BLE feature claims in the earlier sections.
