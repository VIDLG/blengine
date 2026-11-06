# nRF52840 multifunction fault v2 fixture

This is the maintained entry for BleHub's standalone nRF52840 Peripheral fixture:
**one v2 image family for normal RF and nonblocking read / response-write / managed
CCC faults**. There is no live v1 read-hold compatibility lane or legacy-wire fallback.
The provider is test infrastructure, not a BleHub Peripheral product API.

BleHub owns the declarations, profiles, expected contracts, BTP control and scenario
coordination. The configured independent `nrftest` checkout supplies the firmware
builder/toolchain and shared physical lease authority during migration; it is not a
production BLE dependency. Runtime BTP control belongs to this provider.

## Canonical inputs and evidence boundary

All maintained firmware JSON inputs are under this directory, not the historical
`tests/conformance/scenarios/fixture-contracts/` location:

| Input | A | B |
|---|---|---|
| Image declaration | [images/multifunction-rf-v2-a.json](firmware/images/multifunction-rf-v2-a.json) | [images/multifunction-rf-v2-b.json](firmware/images/multifunction-rf-v2-b.json) |
| Canonical profile | [profiles/multifunction-rf-v2-a.json](firmware/profiles/multifunction-rf-v2-a.json) | [profiles/multifunction-rf-v2-b.json](firmware/profiles/multifunction-rf-v2-b.json) |
| Expected build contract | [contracts/multifunction-rf-v2-a.json](firmware/contracts/multifunction-rf-v2-a.json) | [contracts/multifunction-rf-v2-b.json](firmware/contracts/multifunction-rf-v2-b.json) |

A/B have distinct image/profile identities, service UUIDs and local names, with the
same five characteristic roles. Select a matching declaration/profile/contract trio;
do not infer identity from a board label, port, remembered installation or UUID alone.
The declarations carry the exact eleven capabilities and fixed-bearer, single-connection
settings. A characteristic's CCC target is its discovered descriptor, not the first `2902`.

**A and B have completed real two-pass builds and contract export/check; building is
not installed-image proof.** The independently authorized DK-B recovery, installed-image
assertion and Android/iPhone full-suite results are recorded in the
[current candidate report](../../../docs/testing/reports/2026-10-18-v2-dk-b-mobile-full-suite.md).
They do not establish installation of A, write/CCC fault coverage, native HIL or
Linux/BabbleSim runtime results. Machine identities, hashes and failed attempts belong
in named evidence, not a maintained table of what a DK is presumed to run.

The execution priority is a real Android/iPhone Central and physical DK Peripheral,
controlled by the Mac. Linux/BabbleSim is not a prerequisite to start real RF;
unexecuted scheduling/resource/protocol assertions remain explicit coverage gaps.
Native Mac CoreBluetooth is a separate backend/HIL lane, not a replacement for phone
RN evidence. Native Mac RF shortcuts currently fail closed until v2 session,
live proof and owner-bound cleanup are connected. Old native iOS RF shortcuts still
have the same ownership gap and must not be used; their fix is a separate review candidate.
See the [real-Central preflight](../../../docs/testing/reports/2026-10-18-real-central-rf-preflight.md).

## Build, export, check, then explicit provision

Run these recipes from the configured `nrftest` checkout with its prerequisites already
prepared. Replace placeholders with absolute paths; do not assume sibling checkouts or
reuse an unrelated build manifest:

```text
pixi run just firmware-build-dk-image <declaration.json> <profile.json>
pixi run just firmware-export-contract <build-manifest.json> <declaration.json> <profile.json> <new-contract.json>
pixi run just firmware-check-contract <build-manifest.json> <declaration.json> <profile.json> <tracked-contract.json>
```

Use the manifest printed by the builder under configured
`build_dir/images/<image_id>/nrftest-build-manifest.json`. It binds the input snapshots,
actual source/configuration and two-pass normalized digest. Export creates a new contract
and refuses overwrite; check revalidates an existing contract without modifying it.
Neither operation accesses or proves the installed image.

Programming is a separate, explicitly authorized operation against an explicitly selected DK:

```text
pixi run just firmware-provision-dk <build-manifest.json> <raw-hex-sha256> <contract.json> <new-installed-attestation.json>
```

The confirmation is the manifest's **raw HEX artifact SHA-256**, not the contract's
normalized image digest. Provision holds the physical lease through programming and BTP
installed-image assertion. Lower-level `firmware-flash-dk` only programs; it does not
replace that assertion. Ordinary build/export/check/RF runs never flash or reset firmware,
install tools, accept licenses or retry destructive gates implicitly.

## Runtime ownership and fault semantics

- **Host/provider:** freeze contract/profile and artifact identities; own the physical
  lease, sole BTP session, live installed-image receipt, exact peer/ATT target/ticket,
  scenario ledger, cleanup and final evidence-bundle verdict. Live attestation precedes
  GATT mutation/advertising and App installation/launch. Historical receipts are not live authority.
- **App/native:** use the same **Tests → Current run → Evidence** workbench and production
  BLE operations. Browsing creates no Probe or BLE/BTP command; changing views must not
  unmount, cancel or restart the scenario. UART control stays outside the App.
- **Firmware:** capture bounded request bytes before the target callback and return to
  RX; own the original bearer, reserved response buffer, pinned lifetime and bounded
  reply/event handoff. Disconnect does not require a release to unblock an RX callback.

The ARM barrier is **connected + GATT-discovered exact route → host validates peer,
handle and pre-state → ARM acknowledged as ARMED → App dispatch → matching HELD receipt**.
Do not ARM a fabricated future peer before launch, or treat native dispatch acceptance
as proof that the Peripheral received a request. Controls use vendor service/index
`0xff/0xff`, protocol version 2, opcodes `0x10–0x16`; the codec and typed controller are in
[fault_control.py](fault_control.py). Read, response-write and CCC share one slot; Write
Commands and long/prepared writes are not deferred-response fault operations.

ARM is bounded by **10 s**, HELD by **20 s**, response resolution by **2 s**, and retirement
by **5 s**. STATUS does not renew deadlines. The production native watchdog remains its
**10 s** default: a watchdog gate must observe that native terminal before firmware expiry,
not relabel a firmware timeout as native watchdog success.

COMMIT uses the ordinary permission/authorization/callback path; REJECT or held ABORT uses
ATT Unlikely Error without invoking the target callback. Ordinary reads remain ordinary
reads without an ARM. Successful read resolution is `NORMAL_NO_COMMIT`, not a write commit.
Write/CCC callback invocation, callback result, committed value and response/TX facts are
separate evidence. CCC disable requires a verified enabled pre-state; a no-op zero is not
an in-flight disable. Supporting these controls does not itself prove RN write/CCC fault coverage.

After disconnect, STATUS can read the exact **retained receipt**; old RESOLVE/ABORT cannot
mutate a retired owner. Response queued / answer-ordered / TX completion are not peer or
Central-application delivery proof. Before rearm or successful handoff, require RETIRED,
REARMABLE, no infrastructure failure and all six firmware owner counters zero: connection,
bearer, response buffer, resolver work, TX and evidence. Quarantine stays failed even after
late safe reclamation. Independently require the scenario's native close/resource-zero and
old-event isolation proof; neither process death nor a host ACK proves old Probe cleanup.

## Entry points and separate gates

Machine roots, ports, full USB serials and per-DK profile mappings belong in ignored
`blehub.local.toml` (`toolchains.nrftest_root`, `peripheral_nrf52840` and its `devices`
entries), with `BLEHUB_NRFTEST_ROOT` / `BLEHUB_NRF52840_PORT` overrides where applicable.
Resolve the actual debugger/BTP serial pair; no A/B device installation is assumed here.
A/B multi-DK runs need explicit matching per-device mappings and independent live proof.

From the BleHub root, the maintained full-suite recipes default to the canonical v2 A
contract/profile above; pass both paths explicitly to select B:

```text
pixi run just react-native host-android-rf-lab-suite <android-serial>
pixi run just react-native host-ios-device-rf-lab-suite <ios-udid> <team>
```

These are execution instructions, not claims that this candidate passed those gates.
See [host tooling ownership](../../../docs/research/react-native/react-native-host-tooling-architecture.md)
for the named runners and [RN RF Lab architecture](../../../docs/research/react-native/react-native-rf-lab-test-architecture.md)
for workbench behavior.

| Gate | Boundary retained despite sharing the image and App |
|---|---|
| `full-suite` | Ten flows including cancellation/watchdog, then RecoveryTen in one runtime/session; host cleanup and bundle verdict remain mandatory. |
| Standalone RF / watchdog / RecoveryTen / multi-DK | Explicit diagnostics and isolation boundaries; overlap does not make them dead code or substitute for another gate. Watchdog uses v2's 20 s hold, not a separate legacy 30 s image. |
| Response-write / CCC enable/disable REJECT | Independent opt-in gates, not full-suite flows; exact native ATT14 and original uncommitted receipt. Enable recovery uses a fresh subscription; disable continuity must retain the original subscription and receive another value without re-enable. Both CCC gates require native-fenced post-disable silence. See [write](../../../docs/testing/reports/2026-10-19-rn-response-write-reject.md), [enable](../../../docs/testing/reports/2026-10-19-rn-ccc-enable-reject.md), and [disable results/gaps](../../../docs/testing/reports/2026-10-19-rn-ccc-disable-reject.md). |
| Pending read engine reload | Same App/PID, distinct RN engines; held request, module invalidation, retained receipt and old native resource-zero barrier. Not an ordinary relaunch or same-runtime suite. |
| Adapter / permission transitions | Separate opt-in gates with physical mutation, OS evidence and restoration policy; never implicitly part of full-suite. iOS authorization-only XCTest is not Android permission-death or RF evidence. |
| `IOS-RADIO-GATE` | **Reopened and queued after the current firmware/CCC work**, by explicit user instruction. Follow the Plan for execution order; preserve prior failed evidence, no-password controls and independent RF/cleanup requirements. Reopening is neither passed nor waived. |

Direct fixture lifecycle is exposed through `peripheral nrf52840-direct-start` with a v2
fixture request, plus `nrf52840-status` / `nrf52840-stop`. The old `nrf52840-start` bridge
fails closed; do not revive it or archived read-hold commands as compatibility shortcuts.
Successful result pages stay visible unless closing is explicitly requested. App Passed
is not host-gate Passed, and RN gates do not replace native HIL or clean artifact consumers.

### Implementation and test ownership

`direct_fixture.py` owns the live leased BTP session, fixture startup/teardown and
reusable physical operations. `full_suite.run_continuous()` owns the ten-flow order,
passive-recovery close and cancellation preparation, while `FullSuite` owns the
handoff into RecoveryTen. Both use the same fixture object; no second serial owner,
compatibility dispatcher or generic scenario registry is introduced.

Tests follow the boundary they protect:

- `test_direct_fixture.py`: startup ordering, identity/lease checks and rollback.
- `test_suite_cancellation.py`: the real continuous dispatcher, writable synthetic
  GATT state, exact GAP handoff and production-writer/control-relay interaction.
  Each subcase owns an isolated context-managed scenario from `support/`.
- `test_full_suite.py`: composition, canonical handoff, retained-ticket transfer and
  RecoveryTen. Its base coordinator is mocked; this is not a replacement for the
  real-dispatcher regressions or RF evidence.
- `test_write_reject.py` / `test_ccc_enable_reject.py` / `test_ccc_disable_reject.py`: real provider/relay boundaries and original-ticket retirement; CCC additionally binds exact descriptor topology, disabled peer-specific readbacks, notification submission versus App receipt and silence. Host ledger/bundle adversarial replay belongs to `tools/tests/react_native/`, not a second firmware codec.
- `test_recovery_retained.py`: exact retired-owner witness, bounded evidence drainage
  and rejection of active/foreign owners. Shared success assertions belong in the
  canonical full-suite composition test instead of rerunning it here.

Synthetic setup and emulators stay in `support/`; live modules never import them.
Scenario-specific safety assertions stay in their owning tests, rather than being
hidden behind a universal test harness.

Hardware-free regression entry points (not RF or installation proof):

```text
pixi run just peripheral nrf52840-test
pixi run just react-native tools-test
pixi run just react-native tools-fixture-test
```

Use the focused entry for a local change and `tools-fixture-test` for the combined
host/provider/controller regression. These entries intentionally overlap; running
all three is not required to establish additional coverage.

## Historical evidence

The following reports retain their original candidate/device scope, not current v2 authority:

- [Maintained iPhone pending-ATT rerun](../../../docs/testing/reports/2026-09-27-react-native-ios-dk-pending-att-maintained.md).
- [Normal-attested RN RF Lab](../../../docs/testing/reports/2026-10-13-normal-attested-rn-rf-lab.md).
- [Native watchdog RF](../../../docs/testing/reports/2026-10-13-rn-native-watchdog-rf.md).
- [Multifunction A/B firmware and RN RF](../../../docs/testing/reports/2026-10-14-multifunction-nrf-rn-rf.md).

Archived implementations and v1 evidence do not enter current recipes or authorize v2 runs.
