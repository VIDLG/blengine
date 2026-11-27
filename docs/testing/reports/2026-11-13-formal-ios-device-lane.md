# Formal iOS device lane: what is implemented, and the one human step that blocks it — 2026-11-13

## Scope

The formal RN workbench (`tests/consumers/react-native-bleng`) on a physical iPhone, against the
physical nRF52840, driving the same managed scenes Android already passes. This is the lane the
release order requires before Android's ABI freeze and before `react-native-internal` can retire.

Not in scope here: RF verdicts, permission mutation, app-review distribution.

## What already exists

- `formal-host-ios-build` builds an **unsigned simulator** App and the native-test bundle. It is
  the lane's compile/typecheck gate and it passes.
- `tools.apple.ios_preflight` already validates an **existing signed `.app`** read-only:
  artifact fingerprint, `codesign --verify --deep --strict`, signed entitlements, the embedded
  profile (team, expiry, `ProvisionedDevices`, bundle-id pattern), device readiness, and
  artifact stability across the whole check.
- `IosCandidateOwner` already owns install/launch/finalize and the radio lease, and (as of this
  round) supports `release_retained_app` with the same two-shape receipt contract as Android.

## What this round added

1. **`ios_preflight.development_signing(device)`** — resolves the local signing material without
   inventing any of it: the Team from the certificate OU, a valid `Apple Development` identity
   bound to that Team, and a development profile that authorises the Team, carries explicit
   `ProvisionedDevices`, is unexpired, is `get-task-allow`, and — when a device is named —
   actually lists it. It is pure enumeration: no keychain unlock, no password, no licence
   acceptance, no signing, no provisioning update. Unreadable blobs are skipped rather than
   failing the query, because one bad file must not hide a usable profile.
2. **`signed_bundle_id(signing)`** — a profile authorises a *pattern*, so the App's bundle id is
   derived from it: `<team>.*` becomes `<team>.bleng-rn-host`, an explicit id is used exactly.
   Both shapes therefore sign correctly instead of assuming the wildcard one.
3. **`formal-host-ios-device-build <udid>`** — a signed `Release` build into its own derived-data
   directory (a device product must never be confused with the simulator one), signing style
   chosen from the profile's own shape (Xcode-managed → automatic; manual → manual naming that
   profile), and **no** `-allowProvisioningUpdates`, so nothing is created or requested from
   Apple. The result is then verified by `ios_preflight.require_prebuilt()`, so a wrongly signed
   or wrongly profiled App fails at build time instead of at install time.
4. **`ios_preflight.require_prebuilt()`** — reuses the existing preflight as the single owner of
   "is this App installable", instead of a second, drifting implementation.

Verified on this Mac (read-only): team `FWVYQ287W5`, identity
`Apple Development: ZiJian LIU (6J7DUSCP6X)`, profile `iOS Team Provisioning Profile: *`
(Xcode-managed, wildcard, expires 2027-09-18) whose `ProvisionedDevices` includes the connected
iPhone `00008110-000C61160C39401E`. Device readiness and Developer Mode both verified.

## The blocker was keychain access — now resolved

**First attempt failed at the same place both times, and the cause was isolated to keychain
access, not to Xcode, CocoaPods or this project:**

- `xcodebuild` sat at **0.0% CPU with no child process** and the log stopped advancing for 11+
  minutes at `[CP] Embed Pods Frameworks`.
- Each interrupted attempt left one orphaned `codesign --force --sign … React.framework` process.
- `security show-keychain-info ~/Library/Keychains/login.keychain-db` **also hung**, proving the
  block was login-keychain access rather than the build.
- Compilation itself was fine: the `.app`, its executable, `main.jsbundle` and `React.framework`
  were all produced before the hang.

A human then granted the signing key access through Keychain Access (private-key row → Access
Control → allow all applications), and both diagnostic probes returned immediately afterwards.

### Verified build

```
bleng_formal_host_device_app=…/ios/build/device-derived/Build/Products/Release-iphoneos/BleHubBlengHost.app
bleng_formal_host_device_bundle_id=FWVYQ287W5.bleng-rn-host
bleng_formal_host_device_signed_by=Apple Development: ZiJian LIU (6J7DUSCP6X)
```

| check | result |
|---|---|
| build wall time (warm derived data) | ~90 s |
| executable / `main.jsbundle` | 12.9 MB / 1.3 MB |
| `embedded.mobileprovision` | present, 14.5 KB |
| `CFBundleIdentifier` | `FWVYQ287W5.bleng-rn-host` (derived from the wildcard profile) |
| `codesign --verify --deep --strict` | exit 0 |
| `ios_preflight.require_prebuilt()` | verified |
| installed on device | **no** — nothing was mutated |
| App tree fingerprint | `628506068d3e883d56f5edd6ce3050edf2516a2408aa6e3c7a1cdf61adead9a3` (**see note**) |
| App tree fingerprint, recomputed later | `e92002ff9a8ee23b1cdd181d6f48e598fcc9ecd9486c2848107b1bc7f86b1f88` |

> **Fingerprint note (2026-11-13, later read-only check).** The value recorded at build time no
> longer reproduces: recomputing `app_artifact.fingerprint()` on the same
> `Build/Products/Release-iphoneos/BleHubBlengHost.app` gives `e92002ff…`, stably across repeated
> calls, and `codesign --verify --deep --strict` still exits 0 on the unchanged 18:37 tree. No other
> `.app` under `ios/build` hashes to `628506…` either. The cause is **unverified** — either the tree
> changed after the value was recorded, or this worktree's `app_artifact.py` (untracked) hashes a
> different serialization. The current deterministic value is the one to review and pass to
> `--app-tree-sha256`; the guard refusing a stale hash is the behaviour that caught this.

## What still blocks a device scene, in order

The signing chain and the toolchain are no longer blockers. These remain:

1. **A WDA owner.** `ScanAutomationOwner` deliberately requires an already-running,
   host-prepared WDA URL (`create_ios_attached_driver` "never fall back to building it"), so that
   Appium cannot silently own an opaque runner lifecycle. Android's symmetric piece is
   `AndroidAutomationOwner`, which owns helper provisioning. iOS has no such module: nothing
   builds, installs, launches, forwards or retires WDA for this device.
2. **An iOS scene producer** (`bleng_ios_scan.py`): lease → candidate install/launch → automation
   → `run_prepared_scan` → evidence publication → cleanup. `run_prepared_scan`,
   `exercise_owned_scan`, `IosCandidateOwner`, `IosRadioLease` and `ScanAutomationOwner` all
   already accept iOS, so this is wiring rather than new semantics.
3. **iOS branches in evidence verification**, which today hard-codes Android helper/permission
   facts (`bleng_scan_evidence.py`).

Until 1 exists, no scene can run; until 2 and 3 exist, a run would have nothing to publish.

## This round: the three blockers are implemented, one human step remains

All three items above are now code, unit-tested offline, and wired to recipes.

### 1. `tools/react_native/ios_wda.py` — the runner owner

WebDriverAgent is the process that actually drives an iPhone; Appium's XCUITest driver is only a
proxy in front of it. So "is WDA running, and did *we* start it" gets the same treatment as
"did we start the UiAutomator2 helper": one owner that builds, signs, launches, verifies and
retires it, publishing a receipt per command.

| phase | what it does | refusal instead of repair |
|---|---|---|
| `new → verified` | `require_remotexpc_tunnel(port)` plus the pinned WDA sources | fails naming the resolved tunnel-creation command; never escalates |
| `verified → built` | `xcodebuild … build-for-testing` for exactly this device/team/bundle | a non-zero exit is a failure with a log receipt (this was a real defect: the check used to sit inside the capture-only branch) |
| `built → running` | `test-without-building` held as an owned child, then poll `/status` for a nested `ready: true` | a child that exits or never serves is a launch failure naming its log |
| `running → finalized` | SIGTERM the process group, escalate to SIGKILL, verify the child is gone | `resource_zero` stays `not-claimed` and `host_gate_status` stays `pending` |

`wda_bundle_id(team)` derives the runner id from the same Team that signs the App, so one wildcard
development profile authorises both `FWVYQ287W5.bleng-rn-host` and `FWVYQ287W5.bleng-wda` instead of
requiring a second registered identifier. `prepare`/`launch` are one-shot even on failure, because
a half-built or half-launched runner is exactly the state a caller must not reuse.

### 2. `tools/react_native/bleng_ios_scan.py` — the managed scene producer

Same stage order, retirements and receipt vocabulary as the Android producer. Three facts are
iOS-specific and stated rather than inherited:

- **The candidate is a directory.** The signed `.app` is copied into the attempt and identified by
  `app_artifact.fingerprint`, so the sealed bundle carries the exact bytes that were installed —
  the Android producer's frozen APK, one platform over.
- **The runner is built, not installed.** `IosWdaOwner` runs before the Appium session starts, and
  its `require_owner`/`finalize` are handed to `ScanAutomationOwner` as the platform-owner pair.
  The producer separately finalizes WDA when the automation owner was never admitted (a server
  that fails to start must not leave an XCTest runner running).
- **No helper, no permission scope.** The request authorizes `authorize_wda_build` instead of
  helper settings, and the evidence requires `helper_restoration == "not_owned"` rather than
  inventing a pass.

`ScanAutomationOwner` gained one explicit input, `appium_home`, so the iOS session runs against the
isolated XCUITest candidate; Android still derives its home from its concrete owner and refuses an
overridden one.

### 3. `bleng_scan_evidence.py` — per-platform shapes instead of one Android shape

Result and request field sets are now keyed by platform (`ANDROID_RESULT_FIELDS` with
`apk_sha256`, `IOS_RESULT_FIELDS` with `app_tree_sha256`), so neither bundle can claim the other's
evidence. The shared stages (fixture snapshot/lifecycle, phone journal, inner prepared result,
cleanup order) stay shared; the platform-specific facts are validated by their own functions:

- iOS candidate: frozen tree fingerprint, official install/launch receipts for this device and
  bundle, finalization identity, and a fresh native identity challenge directory.
- iOS automation: session start/close, the WDA launch receipt, and a finalization receipt that
  proves *this* child exited without claiming the device is idle. A `helper_restoration` value other
  than `not_owned`, a surviving runner, or a mismatched PID fails the bundle.
- iOS phone journal: an `ios-formal-scan-phone-owner` lease that reached `released`, with the same
  inner hash binding as Android. Result status keeps the shared vocabulary
  (`released` → `passed`, `quarantined` → `quarantined`) so a consumer never sees platform words.

Android bundles replay unchanged: the Android field set, `RESULT_FIELDS`, and every existing check
are byte-identical in behaviour (26 evidence tests still pass).

### 4. Recipes and offline verification

`formal-host-ios-managed-{scan,notification,indication}` mirror the Android recipes. The CLI
derives what the artifact already states — bundle id and executable from `Info.plist`, Team and
identity from `ios_preflight.development_signing(device)`, Appium home/entry and the pinned WDA
sources from the toolchain registry — and requires only the reviewed `--app-tree-sha256` that a
human took from the signed device build.

Verified offline this round: `test_ios_wda` 22, `test_bleng_ios_scan` 15, and the 403-test
targeted regression set (UI scenes, scan producer/admission, evidence, preflight, toolchain,
recipes) all pass with ruff clean.

### 5. The runner build is proven (device untouched)

The exact `build-for-testing` invocation `IosWdaOwner.prepare` issues was run once as a
diagnostic, because a build that only fails after a human starts the tunnel wastes their step:

| check | result |
|---|---|
| `xcodebuild … build-for-testing` | **`** TEST BUILD SUCCEEDED **`** in ~30 s (fresh derived data) |
| `.xctestrun` products | exactly one: `WebDriverAgentRunner_iphoneos27.2-arm64.xctestrun` |
| runner bundle | `…/Debug-iphoneos/WebDriverAgentRunner-Runner.app` |
| signed authority | `Apple Development: ZiJian LIU (6J7DUSCP6X)` |
| runner identifier | `FWVYQ287W5.bleng-wda.xctrunner` (the `.xctrunner` suffix is Xcode's) |
| installed / launched on the device | **no** — `build-for-testing` only compiles |

That run also caught a would-fail-on-device defect in this round's own code: `wda_bundle_id`
lowercased the Team (`fwvyq287w5.bleng-wda`), but bundle ids are case-sensitive and the wildcard
profile — and the App's own `FWVYQ287W5.bleng-rn-host` — spell it uppercase. The id is now derived
from the Team exactly as the certificate and profile do, and the signed runner above confirms the
wildcard profile authorises it.

## The remaining human step

On iOS 18+ a real device needs a RemoteXPC tunnel registry, started with root. **The upstream
spelling of that command does not work on this machine**: `sudo appium driver run xcuitest
tunnel-creation` needs an `appium` binary on `PATH`, and this repository deliberately installs
Appium into an isolated candidate (`~/.cache/blehub/toolchains/appium/ios-…`). The first attempt of
this round failed exactly there with `appium: command not found`.

So the command is now *resolved* rather than spelled: `appium_toolchain.driver_script()` finds
`<candidate>/node_modules/<driver>/scripts/<name>.mjs`, and `tunnel_creation_command()` quotes node
plus that script. Both the precondition error and the recipe below use it, so guidance can never
again name a binary this repository does not put on `PATH`.

```sh
pixi run just react-native::appium-ios-tunnel-command
# sudo <node> …/appium-xcuitest-driver/scripts/tunnel-creation.mjs
```

The recipe only prints it. Run the printed line in its own terminal and leave it running — it is a
long-lived registry server, not a one-shot command.

Measured at the end of this round: `curl 127.0.0.1:42314/remotexpc/tunnels` → `000`, no owning
process. `sudo -n true` → `sudo: a password is required`, so this is a human step both by repository
rule and by fact.

**Why the tunnel is genuinely required — measured, not assumed.** The XCUITest driver has a
`usbmux` fallback tier (`remote-xpc/facade.js`: `initialize()` prefers a tunnel and only warns when
it falls back), and `webDriverAgentUrl` makes it skip real-device discovery entirely
(`device-discovery.js`). Both suggested our attach-only path might not need the tunnel, so it was
probed directly: launch the runner from the already-built `.xctestrun` with **no** tunnel, then
forward the port over usbmuxd (`iproxy`, no root):

```
xcodebuild test-without-building -xctestrun …/WebDriverAgentRunner_iphoneos27.2-arm64.xctestrun \
  -destination platform=iOS,id=00008110-000C61160C39401E

Testing failed:
  WebDriverAgentRunner-Runner (9200) encountered an error
  (The test runner failed to initialize for UI testing.
   Underlying Error: Timed out while enabling automation mode.)
```

The runner installs and starts, then XCTest cannot enable automation mode: that service is
tunnel-only, so the usbmux fallback covers lockdown and port forwarding but **not** XCTest. The
precondition therefore reflects a real device constraint rather than an extra guard of ours. After
the probe both the local child and the device-side runner were gone (no `iproxy`, `xcodebuild` or
`WebDriverAgentRunner` process left; the failed runner exits on its own).

That probe also found a defect that would have failed **every** real run of this lane's own code:
`IosWdaOwner.launch()` passed `-project`/`-scheme`/`-configuration`/`-derivedDataPath` together
with `-xctestrun`, and xcodebuild rejects the combination outright
(`Cannot use -xctestrun with -project, -workspace or -scheme options`). The launch is now just the
built product plus the destination, and the test asserts the four exclusive flags are absent.

Once the registry answers, the remaining path is:

```sh
pixi run just react-native::formal-host-ios-managed-scan --device 00008110-000C61160C39401E \
  --app-tree-sha256 e92002ff9a8ee23b1cdd181d6f48e598fcc9ecd9486c2848107b1bc7f86b1f88 \
  --appium-port 4723 \
  --fixture-contract tests/peripherals/nrf52840/firmware/contracts/multifunction-rf-v2-a.json \
  --fixture-profile tests/peripherals/nrf52840/firmware/profiles/multifunction-rf-v2-a.json \
  --fixture-port <serial> --fixture-resource-identity <jlink|nrf-usb>:<serial> \
  --output .work/ios-managed --authorize-install --authorize-wda-build \
  --authorize-fixture-start
```

The nRF52840 DK is attached on this Mac (`/dev/cu.usbmodem…`), so only the tunnel is missing.

## First real-device run: the chain works, XCTest cannot enable automation mode

The lane ran end to end for the first time with the tunnel up: frozen App tree, install, launch,
the WDA build, the WDA launch, and the ordered retirement all executed, and the failure was contained
exactly as designed.

```
status=failed  host_gate=failed
primary_error: the WDA runner exited before answering /status (exit 65)
scenario_status=not_run  session_status=not_run
phone_status=quarantined  fixture_status=passed
cleanup_order=[wda.finalize, candidate.finalize, fixture.close,
               fixture.close-record, fixture.lease-release, fixture.lease-record,
               bundle.prepare, phone.finish, cleanup.complete]
```

The passive fixture reached a verified `advertising_inactive` + `resources_closed` close, the phone
was **quarantined rather than released**, and the bundle published both facts. Two cleanup errors
are the honest consequence of never having bound native identity:

- `platform.prepare: resource unwind unverified` — the platform owner did not reach `running`.
- `candidate.finalize: no bound native identity; refusing cleanup adoption` — the App was launched
  but never pinned, so the run refuses to adopt it during cleanup instead of guessing.

### Root cause: Apple's automation-mode authorization, not this lane

The runner installs and starts, then dies after ~60 s with:

```
WebDriverAgentRunner-Runner[9224] Failed to initialize for UI testing:
  Error Domain=com.apple.dt.XCTest.XCTFuture Code=1000 "Timed out while enabling automation mode."
```

It is not the tunnel (this run had a live tunnel with 85 published services), not the runner's
signing (`get-task-allow: true`, wildcard dev profile, `codesign --verify --deep --strict` exit 0),
not device lock (`passcodeRequired: false`, `unlockedSinceBoot: true`), not the host session
(`launchctl managername` = `Aqua`, console user = `vision`), and not the `-xctestrun` form (the
canonical `-project … -scheme … test-without-building` form fails identically).

Evidence for what it actually is:

- `/usr/bin/automationmodetool` on this Mac: **"Automation Mode is disabled. This device requires
  user authentication to enable Automation Mode."** That is also the source of the password prompt
  seen during the run: the authorization step asks, and never gets a usable answer.
- Appium maintainer on the same error for real iPhones/iPads (WebDriverAgent #587): since iOS 15 the
  XCTest session start can require device authentication; **removing the passcode is the documented
  workaround**, and it is "out of Appium thing by Apple".
- WebDriverAgent #1273 states the same failure mode for a passcode-protected device.

So this is an Apple device/host authorization constraint, and it took two attempts to narrow it
down because the first remedy was the wrong half:

- `automationmodetool enable-automationmode-without-authentication` was applied on this Mac and
  reports `This device DOES NOT REQUIRE user authentication` — and the next run still timed out.
  That matches upstream exactly: WebDriverAgent #587 is someone reporting the same thing, and the
  maintainer's answer is that the tool is not the fix.
- The device-side half is **Settings → Developer → UI Automation**, plus accepting its first-use
  prompt on the phone. That is the one-time-per-device step other projects document as required
  before any WDA session starts (e.g. GoCarrot's iOS runner: "the first driver start timed out
  'enabling automation mode' until alex accepted a one-time prompt on the phone").
- A green lock state proves nothing here. `KyleMit/Splotch` #2244 records that
  "`devicectl lockState` reports `passcodeRequired: false` and `unlockedSinceBoot: true` while that
  prompt is on screen, so the device cannot be asked" — which is exactly the state observed on this
  device.

So the order of remedies is: device `UI Automation` switch first, host `automationmodetool` second,
and the upstream passcode removal last. All three are human decisions on hardware this repository
may not reconfigure.

Neither the settings nor the device are changed by this repository. Instead `IosWdaOwner` now
**diagnoses** this exact failure: when the runner's log carries that message, the launch error names
all three remedies in that order, warns that a green `lockState` is not evidence, and states that
this owner never changes them. The diagnosis is best-effort (a missing log is simply no diagnosis)
and never replaces the launch failure. Two tests cover the guidance and the negative case.

## Appium toolchain: one registry, two exclusive candidates

Installed with the user's explicit authorization.

| | Android | iOS |
|---|---|---|
| inputs | `tools/appium/android/` | `tools/appium/ios/` |
| core | `appium` 4.0.0-beta.7 | `appium` 4.0.0-beta.7 (same) |
| driver | `appium-uiautomator2-driver` 9.0.1 | `appium-xcuitest-driver` 13.1.0 |
| helper artifacts | server/settings APKs (hashed) | `appium-webdriveragent` 17.0.2 sources |
| home | `android-4.0.0-beta.7-uia2-9.0.1-4d4153d22890` | `ios-4.0.0-beta.7-xcui-13.1.0-b6e978f1ed7e` |
| discovered drivers | exactly `uiautomator2` | exactly `xcuitest` |

The two candidates are **deliberately exclusive**: each installer asserts its own driver is the
only one present, so a run can never silently depend on the other platform's runner. That was
already true for Android (a pre-existing test); this round added the mirror assertion for iOS.

Why `appium-xcuitest-driver` 13.1.0 and not 13.1.1: 13.1.1 requires `appium ^4.0.0-beta.8`, and
the Android candidate is pinned to `4.0.0-beta.7` with a proven scene, helper APK hashes and run
commands that all name that pin. One shared core beats an iOS-driven re-pin of Android; the choice
and its reason are recorded in `tools/appium/ios/selection.json`.

`appium_toolchain.py` is now one platform registry rather than a second installer. Three details
were kept deliberately, because each is load-bearing for the already-provisioned Android home:

- **Input bytes unchanged.** The four files moved into `tools/appium/android/` without edits, so
  `inputs_sha256`, the home-name suffix and the receipt stay identical and the existing Android
  toolchain is still the selected one.
- **Home tag `uia2` preserved**, so the resolved directory name is byte-for-byte the one already on
  disk. A rename would have silently stopped selecting a provisioned, proven home.
- **Receipt key `helper_apks_sha256` preserved** for Android; iOS records `helper_assets_sha256`
  instead, because hashing a WDA *source tree* would attest a tarball rather than an installable
  runner.

Verified after installation: `appium report --platform ios` resolves the candidate, and the server
answers `/status` with `ready: true` on the same `4.0.0-beta.7` core. Nothing was built, signed,
installed or launched on the device by the installer.

## First passing iOS scenes

All three managed scenes pass on the physical iPhone 14 Plus (`00008110-000C61160C39401E`,
iOS 27.2) against the physical nRF52840, with the frozen App tree
`640c5039da892e8e1b3ffc5bbc21d44b92e1105ddc3e66fdf5f92c296d7ba641`:

| scene | run | trace |
|---|---|---|
| `scan` | `ff5adf3b` | `idle … scan:advertisement-matched … scan:stopped … closed` |
| `notification` | `fd2cf329` | `… link:connected … notify:value-matched … notify:ended …` |
| `indication` | `d65fd045` | same shape, strict indication |

Every run: `status=passed`, `scenario_status=passed`, `session_status=passed`,
`phone_status=passed`, `fixture_status=passed`, `cleanup_errors=[]`,
`phone_cleanup_errors=[]`, fixture close receipt `advertising_inactive=true` /
`resources_closed=true` from `btp-gap-controller-info-advertising-bit-clear`.
`host_gate_status` stays `pending`: a UI lane passing does not upgrade to a release gate.

Measured cost per scene, from its own `automation/appium.log`: 34/44/44 element clicks and
159/187/187 accessibility finds — the same order as the Android lane, not the 23 478
requests the earlier failed attempt issued. That earlier run polled a settled screen for
the whole ceiling because one navigation tap produced no page change; the two fixes below
are what closed that gap.

### Authorization: the device asks once per boot

After the reboot every attempt died at
`The test runner failed to initialize for UI testing. (Underlying Error: Timed out while
enabling automation mode.)`, with the host waiver still reported as
`This device DOES NOT REQUIRE user authentication to enable Automation Mode` and the device
unlocked. The missing step was **entering the device passcode on the XCTest automation prompt
that appears when the runner starts**; it is a device-side action, it happened once, and after
it every later scene started without a prompt. Nothing in this repository can or should do it.
This refines the earlier three-step diagnosis: the waiver removes the *host* half, the device
toggle enables the feature, and the prompt authorizes the *session*.

### Fixed at their owners

- **Navigation is verified and re-dispatched, and a settled screen fails fast.** Page changes go
  through `navigate` (identity-checked dispatch, page re-read, at most three idempotent
  dispatches), and `verify_page` now has a 20 s settle bound that reports the observed
  page/state/title instead of polling to the scene ceiling.
- **The offline verifier compared two different device namespaces.**
  `_ios_candidate_facts` required a CoreDevice `deviceIdentifier` (a UUID) to equal the run's
  UDID, which can never hold; the check had simply never run because no iOS scene had passed.
  It now ties each receipt to the device through the argv the receipt recorded
  (`--device <udid>`), requires install and launch to name one CoreDevice, and keeps the
  App/bundle cross-checks. The test fixtures had baked in the impossible equality, so they now
  carry both namespaces and a disagreement case.


Observed on the recheck after the iPhone was rebooted: the tunnel registry that had been started
before the reboot was still listening and still answered `GET /remotexpc/tunnels/metadata` with
`status: OK`, but with `totalTunnels: 0`. Its per-device endpoint returned HTTP 408/404, and
`POST …/refresh-services` answered `Tunnel not found for UDID`. An RSD tunnel belongs to one boot,
so that registry could never recreate it, and the only remedy was to stop it and start a fresh one
(it was also resolved from the older `beta.7` candidate home, which is exactly the kind of stale
process the pinned toolchain is supposed to prevent).

Two defects came out of that, both fixed at their owner:

- **Readiness was measured on the wrong endpoint.** `ios_appium.require_remotexpc_tunnel(port)`
  accepted any `200` from the registry root, which says only that *some* registry is listening. It
  now takes the device and requires that device's own catalog entry (`…/tunnels/<udid>?waitMs=…`),
  and the failure names the case instead of leaving it to appear later as an unexplained WDA
  connection error. `IosWdaOwner.verify_preconditions` passes its device and keeps one guidance
  path, now also stating that a registry predating a reboot must be replaced.
- **Starting the registry took a copy of two absolute paths.** `pixi run just
  react-native::appium-ios-tunnel-script` materializes the same resolved argv as
  `.work/ios-tunnel-registry.sh` (mode 700) so the human runs `sudo .work/ios-tunnel-registry.sh`.
  The script deliberately carries no `sudo` of its own: `appium_toolchain.tunnel_execution_command()`
  owns the argv, and `tunnel_creation_command()` is that same argv prefixed with `sudo`, so the
  pasted form and the script form cannot drift or double-escalate. The recipe still never executes
  it — the registry is a long-lived root server, and root stays a human step here.

## One command for the three scenes

`pixi run just react-native::formal-host-ios-managed-sequence --device … --app-tree-sha256 …`
runs `scan`, then `notification`, then `indication`, each with its own run id, phone lease and
evidence root (`<output>/<scene>`), stopping at the first scene that does not pass. Verified on
the device (`wu2pzngz`): `scene_sequence=passed scenes=scan,notification,indication`, with
`scan=560db428`, `notification=dcf71107`, `indication=dba37558`; every scene
`scenario/session/phone/fixture=passed`, `cleanup_errors=[]`, fixture close
`advertising_inactive=true` + `resources_closed=true`. The sequence takes about 18 minutes,
six of them per scene; that is round-trip bound rather than a wait, and the per-scene request
count matches the single-scene runs above.

Two lane defects had to be fixed before a sequence was worth anything:

- **A text input could lose characters.** `indication` failed with
  `did not keep the requested text; observed 'ff0000-…', requested 'fff50000-…'` — a measured
  dropped `f5` inside a UUID. The cause is `clear()` and the keystrokes landing in the same
  render pass of the controlled input, whose trailing render overwrites what was just typed.
  iOS also reports an *empty* text field's placeholder as its text, so "wait until it reads
  empty" is unavailable without hardcoding UI copy. The input is now dispatched at most twice —
  the second dispatch only re-states the same declared value — and the exact text is always
  required; a field that still disagrees fails the scene naming what it actually held.
- **The platform restarts the App a scene just terminated.** A release reported `absent` and
  the next scene's admission saw the same installation running immediately afterwards, so runs
  back to back needed a manual stop. `pixi run just
  react-native::formal-host-ios-release-app --device … --output <dir>` now owns that step: it
  resolves this bundle's own installation from the device inventory, terminates at most one
  process inside that exact directory, and requires the installation to stay absent across
  consecutive inventories (bounded terminations, no retries). The sequence applies it before
  every scene, the first included; the run above records `terminated×1` before `scan` and
  `absent×0` before the other two. It never touches another bundle and claims no resource-zero,
  radio, permission or gate state.

## Deliberately not claimed

- **No iOS scene has passed.** No scene trace, no App observation, no `host_gate`. The one
  real-device run above failed before the App was driven, and its fixture receipt is a *fixture*
  fact only.
- `development_signing` proves *visible* identity and *profile* consistency. It does not prove
  private-key usability for an arbitrary later process; the successful signed build does prove it
  for this one.
- Simulator-only results (native tests, Jest, typecheck) remain what they are: not RF, not device.
