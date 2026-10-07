# Phase 3 Apple staged-artifact consumer checkpoint

Date: 2026-09-25

## Scope and candidate

This checkpoint checks the independent Apple consumers against the same locally staged production `BleHubUniFfi` package. The local BleHub base is `dbfd1e6cb1e1bacf688bcd7ecda7fd0bdb1d597d` **plus uncommitted worktree changes**; this is not an immutable release candidate or a Phase 3 exit approval. Both recipes regenerate the stage at `.work/staged/apple-production` through `just apple prepare`. Neither consumer imports `platforms/apple` source or the repository-owned HIL harness. Their Swift test backend/sink objects exercise the real exported `BleHub` and weak event ingress without using CoreBluetooth or a Peripheral.

| Gate | Actual result | Boundary |
|---|---|---|
| `pixi run just apple consumer-macos` | `apple_production_uniffi_component_symbols=passed`; `apple_xcframework_slices=passed`; `apple_artifact_consumer=passed` | Independent SwiftPM executable consumes the staged package and verifies capabilities, exact `ScanStarted` then successful `CommandCompleted` callback delivery, explicit dispose, and `Closed` for a retained ingress after close. |
| `pixi run just apple consumer-ios` | Unsigned generic device and simulator builds passed. | Independent XcodeGen app and build graph; compile/link only, not a simulator or device runtime result. |
| `pixi run just apple consumer-ios-device` | Development-signed iPhone build/install/launch passed; `apple_ios_artifact_consumer=passed` with run ID `541dcf16-f2ec-4396-a7af-2d38be293199`. | Physical iPhone 14 Plus; app writes a run-ID-correlated result after capabilities, exact two-event ingress order, explicit dispose and post-close `Closed`. No Bluetooth permission or RF is required. |

The iOS consumer lives under [`tests/consumers/apple/ios/`](../../../tests/consumers/apple/ios/); its project spec names only the staged production Swift package. Its Xcode project and build output are generated under ignored `.work/apple-ios-artifact-consumer/`; the signed device gate is explicitly opt-in. The macOS consumer remains a separate SwiftPM package at [`tests/consumers/apple/macos/`](../../../tests/consumers/apple/macos/). The stage supplies the platform-appropriate slices; neither host builds a second BleHub native image. The iOS result file remains local at `.work/apple-ios-artifact-consumer/result.txt`.

## Phase 3 exit boundaries

- These artifact-only tests close the earlier missing iOS consumer runtime and missing macOS callback-ingress coverage for this worktree; they do not by themselves close all Phase 3 exit criteria in [the reviewed plan](../../../.plan/PLAN.md). The project specs and local package references are independent, but no tracked candidate-specific consumer lock/manifest attestation was added; verify that packaging/lock requirement explicitly at exit review instead of implying it passed from build success.
- The earlier [macOS-to-Android Peripheral matrix](2026-09-08-apple-macos-android-rf-completion.md) and [iPhone-to-Android Peripheral IO pass](2026-09-08-apple-ios-android-rf-pass.md) were not rerun here. The latter explicitly excludes iPhone strict Indication, connected RSSI, and passive-loss/recovery. [iPhone-to-nRF IO](2026-09-13-apple-ios-nrf-rf-blocked.md) remains blocked after scan at `Connecting`; macOS Tauri-to-nRF RF does not prove iPhone RF.
- A direct Swift production runtime, deterministic Apple backend tests, full Swift ARC/queue/exception/close catalog, package metadata/duplicate-symbol audit, and applicable RF gates must be recorded against the actual exit candidate. Phase 5 still requires an eventual same-final-candidate rerun after ABI freeze.
- The [RN/Nitro internal checkpoint](2026-09-21-react-native-nitro-apple-feasibility.md) is separate bridge evidence. The optional Tauri downstream remains historical/current evidence in its own lane, not an Apple backend substitute. Choosing to archive it requires a reviewed plan change; simply prioritizing RN work does not reclassify Tauri's evidence.
