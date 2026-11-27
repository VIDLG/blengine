# Android active-loss recovery candidate

Date: 2026-10-31

Status: **real Android active-loss candidate passed at the maintained RN/Nitro + BleHub backend layer.** This is not yet the formal Android `Bleng` Supervisor gate.

## Candidate

```text
device=3ac5cc09
model=OnePlus 11
adapter_mode=active-loss
run_id=b831fc3b-1efb-4650-b3c3-32afe1a40816
boot_id=cf75f100-79c6-4f76-a084-3e9bda06e17d
fixture=multifunction-rf-v2-a
```

The v2-A fixture identity was selected from the live provider attestation. A v2-B attempt was rejected before radio mutation because the installed image was v2-A; no false profile substitution was made.

Evidence bundle:

```text
tests/consumers/react-native-internal/android/build/rf-adapter-transition-c291a39b7ef84192aba62ee0f3dcd08e/evidence-bundle.json
```

## Observed result

The active-loss scenario completed with:

```text
status=passed
completed=true
primary_error=null
cleanup_errors=[]
policy=disconnect-first
app_runtimes=1
hub_probe_lifecycles=2
closed_zero_snapshots=2
```

The operation path included a real scan, advertisement, connection, GATT discovery and two old read terminals after the active loss. The Android backend reported real connection errors for the retired operations rather than synthetic success:

```text
command_id=32008 category=connection
command_id=32009 category=connection
```

Both the old/off Probe and the new/on Probe closed with zero logical resources, including:

```text
activeScans=0
backendLeases=0
callbackRoutes=0
connections=0
gattAttributes=0
handoffRoots=0
hubRoots=0
ingressRoots=0
lifecycleOwners=0
pendingIo=0
subscriptions=0
watchdogs=0
workerThreads=0
```

The provider finished with advertising stopped, hold inactive and no peer. The phone and fixture cleanup paths completed without errors.

## What this proves

This is stronger than the iOS current result for the Android layer: Android produced fresh native delivery and fresh post-loss lifecycle evidence after radio restoration. The existing Android integration already replaces the old `BleHub`/`AndroidBleBackend` probe inside one App runtime and proves resource-zero barriers.

It does **not** yet prove:

- the new Rust `bleng-recovery` policy is wired into Android;
- a formal Android `Bleng` owner/backend `BleSessionSupervisor` exists;
- same-owner/fresh-owner/fresh-process recovery outcomes are exposed through the public Nitro contract;
- all Android OS versions or API tiers, which remain outside the current mandatory matrix;
- long-running recovery stress or final ABI/package gates.

The production `blehub-android` library now owns `AndroidBleSessionSupervisor` and its shared session contract. RN integration supplies `AndroidBleProbeSession`; the existing `HybridBleHubProbe` routes its assembled Hub/backend worker/handoff retirement through that barrier. Probe-specific pending-read markers and React lifecycle ownership remain outside the session object. This preserves the existing cleanup contract without making production code depend on tests.

The Rust recovery policy and formal Android `Bleng` owner/backend recovery contract are still separate follow-up work. The passed candidate remains evidence for the current Android BleHub path and is not relabeled as a complete Android Bleng recovery gate.

The formal two-component Android BLeng artifact was also rebuilt and executed independently on the same physical Android device:

```text
artifact=dev.blehub:bleng:0.0.0
PublishedBlengTest=9 tests passed
```

This validates the published BLeng UniFFI/AAR runtime and its artifact-only consumer. It is intentionally not counted as RN Supervisor evidence: the current RN production module consumes the separate `blehub` package line, and the formal RN Bleng module still needs its own production Gradle/package boundary.

## Post-Supervisor migration rerun

After routing the existing `HybridBleHubProbe` native retire through `AndroidBleSessionSupervisor` and `AndroidBleProbeSession`, the same real active-loss candidate was rerun with the live v2-A fixture:

```text
serial=3ac5cc09
mode=active-loss
bundle=tests/consumers/react-native-internal/android/build/rf-adapter-transition-923c09fb97d74938a582ad992a94c40c/evidence-bundle.json
status=passed
completed=true
primary_error=null
cleanup_errors=[]
```

This rerun is the evidence for the migrated session boundary; the earlier candidate remains retained as historical evidence.
